from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from agent_flow.adapters.registry import detect_adapter
from agent_flow.adapters.templates import PromptContext, render_stage_prompt
from agent_flow.core.artifacts import (
    init_project,
    write_gate_results,
    write_handoff,
    write_prompt,
    write_recovery,
    write_stage_result,
)
from agent_flow.core.architecture_lint import lint_profiles
from agent_flow.core.context_contract import (
    append_context_event,
    check_system_invariants,
    ensure_context_contract,
    offload_tool_output,
    write_system_invariants,
)
from agent_flow.core.commands import run_safe_command
from agent_flow.core.gates import GateCommand, run_gates
from agent_flow.core.phase_workflow import load_phase_workflow_definition
from agent_flow.core.profiles import (
    active_profile_ids,
    detect_profile,
    load_profile,
    load_profile_payload,
)
from agent_flow.core.local_skills import (
    changed_files,
    local_skill_prompt_block,
    merged_profile_payload,
    missing_local_skill_markers,
    phase_skill_resolution,
)
from agent_flow.core.skill_sync import parse_skill_sources, sync_skill_sources
from agent_flow.core.review import summarize_reviews, write_review_summary
from agent_flow.core.report import write_run_report
from agent_flow.core.query import explain_run, query_run
from agent_flow.core.security import resolve_project_path
from agent_flow.core.tool_lint import lint_tools
from agent_flow.core.watch import write_watch_snapshot
from agent_flow.core.team import (
    acknowledge_shutdown,
    add_task,
    add_worker,
    approve_worker_call,
    approved_worker_scopes,
    approve_task_result,
    archive_team,
    claim_task,
    complete_task,
    apply_team_state_import,
    export_team_archive,
    export_team_state,
    fail_task,
    init_team,
    list_team_archives,
    list_teams,
    list_messages,
    mark_message_read,
    request_shutdown,
    restore_team_archive,
    send_message,
    summarize_team_state_import,
    safe_team_name,
    safe_worker_name,
    team_status,
    update_worker_heartbeat,
    validate_team_state_import,
    write_worker_brief,
    write_worker_result,
)
from agent_flow.core.worktrees import (
    create_worktree,
    get_worktree_status,
    known_worktree_names,
    plan_worktree,
    remove_worktree_metadata,
    remove_worktree,
    worktree_branch_exists,
    worktree_runtime_root,
)
from agent_flow.core.worktree_isolation import (
    WorkerScope,
    WorktreeIsolationError,
    assert_cwd_bound,
    assert_leader_unchanged,
    assert_scopes_isolated,
    capture_leader_snapshot,
    git_repo_state,
    max_worker_capacity,
    sanitized_worker_env,
    verify_linked_worktree,
    worker_claim_lock,
)
from agent_flow.core.state import RunRequest, RunState, start_run, status_summary
from agent_flow.core.workflow import load_workflow
from agent_flow.eval import run_eval
from agent_flow.memory.entities import EntityMemoryIndex
from agent_flow.artifact import find_active_run, mark_inactive, read_meta
from agent_flow.runner import Runner, ResumeMode, _find_kit_root
from agent_flow.providers.host import list_host_providers
from agent_flow.providers.subprocess import ProviderCommand, run_provider
from agent_flow.pr_watch import fetch_pr, watch_pr


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-flow")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--root", default=".")

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("task")
    run_parser.add_argument("--root", default=".")
    run_parser.add_argument("--workflow", default="default")
    run_parser.add_argument("--worktree")
    run_parser.add_argument("--worktree-branch")
    run_parser.add_argument("--allow-dirty", action="store_true")
    run_parser.add_argument(
        "--architecture",
        choices=("default", "ddd", "service-layer"),
        default="default",
    )

    continue_parser = subparsers.add_parser("continue")
    continue_parser.add_argument("--root", default=".")
    continue_parser.add_argument("--worktree")

    abort_parser = subparsers.add_parser("abort")
    abort_parser.add_argument("--root", default=".")
    abort_parser.add_argument("--worktree")
    abort_parser.add_argument("--yes", "-y", action="store_true")

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("workflow")
    start_parser.add_argument("--root", default=".")
    start_parser.add_argument("--task", required=True)
    start_parser.add_argument("--run-id")
    start_parser.add_argument("--adapter", default="auto")
    start_parser.add_argument("--profile", default="auto")
    start_parser.add_argument(
        "--architecture",
        choices=("default", "ddd", "service-layer"),
        default="default",
    )
    start_parser.add_argument("--worktree")
    start_parser.add_argument("--worktree-branch")
    start_parser.add_argument("--allow-dirty", action="store_true")

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--root", default=".")
    status_parser.add_argument("--worktree")

    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("--root", default=".")
    report_parser.add_argument("--run-dir")

    query_parser = subparsers.add_parser("query")
    query_parser.add_argument("query")
    query_parser.add_argument("--root", default=".")
    query_parser.add_argument("--run-dir")
    query_parser.add_argument("--limit", type=int, default=10)

    explain_parser = subparsers.add_parser("explain")
    explain_parser.add_argument("question")
    explain_parser.add_argument("--root", default=".")
    explain_parser.add_argument("--run-dir")

    watch_parser = subparsers.add_parser("watch")
    watch_parser.add_argument("--root", default=".")
    watch_parser.add_argument("--run-dir")

    pr_watch_parser = subparsers.add_parser("pr-watch")
    pr_watch_parser.add_argument("number", type=int)
    pr_watch_parser.add_argument("--repo")
    pr_watch_parser.add_argument("--once", action="store_true")
    pr_watch_parser.add_argument("--poll-interval", type=int, default=30)
    pr_watch_parser.add_argument("--max-polls", type=int, default=20)

    detect_parser = subparsers.add_parser("detect-profile")
    detect_parser.add_argument("--root", default=".")

    provider_parser = subparsers.add_parser("provider")
    provider_subparsers = provider_parser.add_subparsers(dest="provider_command", required=True)
    provider_subparsers.add_parser("list")

    gates_parser = subparsers.add_parser("gates")
    gates_parser.add_argument("--root", default=".")
    gates_parser.add_argument("--profile", default="auto")
    gates_parser.add_argument("--run-dir")
    gates_parser.add_argument("--timeout", type=int, default=600)
    gates_parser.add_argument("--worktree")

    architecture_lint_parser = subparsers.add_parser("architecture-lint")
    architecture_lint_parser.add_argument("--root", default=".")
    architecture_lint_parser.add_argument("--profile", default="auto")
    architecture_lint_parser.add_argument("--files", nargs="*")
    architecture_lint_parser.add_argument("--worktree")

    eval_parser = subparsers.add_parser("eval")
    eval_parser.add_argument("--root", default=".")
    eval_parser.add_argument("--fixtures")
    eval_parser.add_argument("--judge-command", nargs=argparse.REMAINDER)
    eval_parser.add_argument("--run-dir")

    tools_parser = subparsers.add_parser("tools")
    tools_subparsers = tools_parser.add_subparsers(dest="tools_command", required=True)
    tools_lint = tools_subparsers.add_parser("lint")
    tools_lint.add_argument("--root", default=".")

    workflow_parser = subparsers.add_parser("workflow")
    workflow_subparsers = workflow_parser.add_subparsers(dest="workflow_command", required=True)
    workflow_export = workflow_subparsers.add_parser("export")
    workflow_export.add_argument("--workflow", default="full-feature")
    workflow_export.add_argument("--format", choices=("json",), default="json")

    skills_parser = subparsers.add_parser("skills")
    skills_subparsers = skills_parser.add_subparsers(dest="skills_command", required=True)
    for name in ("sync", "resolve", "prompt", "markers"):
        sub = skills_subparsers.add_parser(name)
        sub.add_argument("--root", default=".")
        sub.add_argument("--profile")
        if name != "sync":
            sub.add_argument("--phase", required=True)
            sub.add_argument("--workflow", default="default")
        if name == "markers":
            sub.add_argument("--artifact", required=True)
            # 읽음 증거를 현재 phase로 한정한다. 없으면 과거 기록까지 인정돼 강제가 약해진다.
            sub.add_argument("--since", type=float, default=None)

    context_parser = subparsers.add_parser("context")
    context_subparsers = context_parser.add_subparsers(dest="context_command", required=True)
    context_init = context_subparsers.add_parser("init")
    context_init.add_argument("--root", default=".")
    context_init.add_argument("--run-dir")
    context_event = context_subparsers.add_parser("event")
    context_event.add_argument("--root", default=".")
    context_event.add_argument("--run-dir")
    context_event.add_argument("--event", required=True)
    context_event.add_argument("--details-json", default="{}")
    context_offload = context_subparsers.add_parser("offload")
    context_offload.add_argument("--root", default=".")
    context_offload.add_argument("--run-dir")
    context_offload.add_argument("--name", required=True)
    context_offload.add_argument("--content", required=True)
    context_invariants = context_subparsers.add_parser("check-invariants")
    context_invariants.add_argument("--root", default=".")
    context_invariants.add_argument("--run-dir")
    context_write_invariants = context_subparsers.add_parser("write-invariants")
    context_write_invariants.add_argument("--root", default=".")

    memory_parser = subparsers.add_parser("memory")
    memory_subparsers = memory_parser.add_subparsers(dest="memory_command", required=True)
    memory_entities = memory_subparsers.add_parser("entities")
    memory_entities.add_argument("--root", default=".")
    memory_entities.add_argument("--dir")

    record_parser = subparsers.add_parser("record-stage")
    record_parser.add_argument("--root", default=".")
    record_parser.add_argument("--run-dir", required=True)
    record_parser.add_argument("--stage", required=True)
    record_parser.add_argument("--status", default="completed")
    record_parser.add_argument("--evidence-type", default="observed")
    record_parser.add_argument("--confidence", default="unknown")
    record_parser.add_argument("--content", required=True)

    handoff_parser = subparsers.add_parser("handoff")
    handoff_parser.add_argument("--root", default=".")
    handoff_parser.add_argument("--run-dir", required=True)
    handoff_parser.add_argument("--from-stage", required=True)
    handoff_parser.add_argument("--to-stage", required=True)
    handoff_parser.add_argument("--decided", default="")
    handoff_parser.add_argument("--rejected", default="")
    handoff_parser.add_argument("--risks", default="")
    handoff_parser.add_argument("--files", default="")
    handoff_parser.add_argument("--remaining", default="")

    review_parser = subparsers.add_parser("review-summary")
    review_parser.add_argument("--root", default=".")
    review_parser.add_argument("--run-dir", required=True)
    review_parser.add_argument("--reviews", nargs="+", required=True)

    review_command_parser = subparsers.add_parser("review")
    review_command_subparsers = review_command_parser.add_subparsers(dest="review_command", required=True)
    review_retry = review_command_subparsers.add_parser("retry")
    review_retry.add_argument("--root", default=".")
    review_retry.add_argument("--reviewer", required=True)
    review_retry.add_argument("--retry-after")

    worktree_parser = subparsers.add_parser("worktree")
    worktree_subparsers = worktree_parser.add_subparsers(dest="worktree_command", required=True)
    worktree_create = worktree_subparsers.add_parser("create")
    worktree_create.add_argument("--root", default=".")
    worktree_create.add_argument("--name", required=True)
    worktree_create.add_argument("--branch")
    worktree_create.add_argument("--allow-dirty", action="store_true")
    worktree_status = worktree_subparsers.add_parser("status")
    worktree_status.add_argument("--root", default=".")
    worktree_status.add_argument("--name", required=True)
    worktree_list = worktree_subparsers.add_parser("list")
    worktree_list.add_argument("--root", default=".")
    worktree_remove = worktree_subparsers.add_parser("remove")
    worktree_remove.add_argument("--root", default=".")
    worktree_remove.add_argument("--name", required=True)
    worktree_remove.add_argument("--keep-branch", action="store_true")
    worktree_remove.add_argument("--allow-unmerged", action="store_true")

    team_parser = subparsers.add_parser("team")
    team_subparsers = team_parser.add_subparsers(dest="team_command", required=True)
    team_list = team_subparsers.add_parser("list")
    team_list.add_argument("--root", default=".")
    team_archive = team_subparsers.add_parser("archive")
    team_archive.add_argument("--root", default=".")
    team_archive.add_argument("--team", required=True)
    team_archive.add_argument("--reason", default="")
    team_archive_list = team_subparsers.add_parser("archive-list")
    team_archive_list.add_argument("--root", default=".")
    team_archive_restore = team_subparsers.add_parser("archive-restore")
    team_archive_restore.add_argument("--root", default=".")
    team_archive_restore.add_argument("--archive-path", required=True)
    team_archive_restore.add_argument("--report")
    team_init = team_subparsers.add_parser("init")
    team_init.add_argument("--root", default=".")
    team_init.add_argument("--name", required=True)
    team_init.add_argument("--description", default="")
    team_task = team_subparsers.add_parser("task")
    team_task.add_argument("--root", default=".")
    team_task.add_argument("--team", required=True)
    team_task.add_argument("--id", required=True)
    team_task.add_argument("--subject", required=True)
    team_task.add_argument("--description", default="")
    team_brief = team_subparsers.add_parser("brief")
    team_brief.add_argument("--root", default=".")
    team_brief.add_argument("--team", required=True)
    team_brief.add_argument("--task", required=True)
    team_brief.add_argument("--worker", required=True)
    team_brief.add_argument("--brief", required=True)
    team_brief.add_argument("--write-scope", default="none")
    team_approve_worker = team_subparsers.add_parser("approve-worker")
    team_approve_worker.add_argument("--root", default=".")
    team_approve_worker.add_argument("--team", required=True)
    team_approve_worker.add_argument("--task", required=True)
    team_approve_worker.add_argument("--worker", required=True)
    team_approve_worker.add_argument("--reviewer", default="lead")
    team_approve_worker.add_argument("--write-scope", default="none")
    team_result = team_subparsers.add_parser("result")
    team_result.add_argument("--root", default=".")
    team_result.add_argument("--team", required=True)
    team_result.add_argument("--task", required=True)
    team_result.add_argument("--worker", required=True)
    team_result.add_argument("--result", required=True)
    team_approve = team_subparsers.add_parser("approve")
    team_approve.add_argument("--root", default=".")
    team_approve.add_argument("--team", required=True)
    team_approve.add_argument("--task", required=True)
    team_approve.add_argument("--reviewer", default="lead")
    team_approve.add_argument("--verdict", choices=("approve", "request-changes"), required=True)
    team_approve.add_argument("--notes", default="")
    team_worker = team_subparsers.add_parser("worker")
    team_worker.add_argument("--root", default=".")
    team_worker.add_argument("--team", required=True)
    team_worker.add_argument("--name", required=True)
    team_worker.add_argument("--role", required=True)
    team_run_next = team_subparsers.add_parser("run-next")
    team_run_next.add_argument("--root", default=".")
    team_run_next.add_argument("--team", required=True)
    team_run_next.add_argument("--worker", required=True)
    team_run_next.add_argument("--command", dest="command_argv", nargs=argparse.REMAINDER, required=True)
    team_claim = team_subparsers.add_parser("claim")
    team_claim.add_argument("--root", default=".")
    team_claim.add_argument("--team", required=True)
    team_claim.add_argument("--task", required=True)
    team_claim.add_argument("--worker", required=True)
    team_complete = team_subparsers.add_parser("complete")
    team_complete.add_argument("--root", default=".")
    team_complete.add_argument("--team", required=True)
    team_complete.add_argument("--task", required=True)
    team_complete.add_argument("--claim-token", required=True)
    team_complete.add_argument("--result", default="")
    team_fail = team_subparsers.add_parser("fail")
    team_fail.add_argument("--root", default=".")
    team_fail.add_argument("--team", required=True)
    team_fail.add_argument("--task", required=True)
    team_fail.add_argument("--claim-token", required=True)
    team_fail.add_argument("--result", default="")
    team_message = team_subparsers.add_parser("message")
    team_message.add_argument("--root", default=".")
    team_message.add_argument("--team", required=True)
    team_message.add_argument("--from-actor", required=True)
    team_message.add_argument("--to-worker", required=True)
    team_message.add_argument("--body", required=True)
    team_messages = team_subparsers.add_parser("messages")
    team_messages.add_argument("--root", default=".")
    team_messages.add_argument("--team", required=True)
    team_messages.add_argument("--worker", required=True)
    team_messages.add_argument("--unread-only", action="store_true")
    team_read = team_subparsers.add_parser("mark-read")
    team_read.add_argument("--root", default=".")
    team_read.add_argument("--team", required=True)
    team_read.add_argument("--worker", required=True)
    team_read.add_argument("--message", required=True)
    team_heartbeat = team_subparsers.add_parser("heartbeat")
    team_heartbeat.add_argument("--root", default=".")
    team_heartbeat.add_argument("--team", required=True)
    team_heartbeat.add_argument("--worker", required=True)
    team_heartbeat.add_argument("--status", required=True)
    heartbeat_alive = team_heartbeat.add_mutually_exclusive_group()
    heartbeat_alive.add_argument("--alive", action="store_true", default=True)
    heartbeat_alive.add_argument("--dead", action="store_true")
    team_shutdown = team_subparsers.add_parser("shutdown")
    team_shutdown.add_argument("--root", default=".")
    team_shutdown.add_argument("--team", required=True)
    team_shutdown.add_argument("--worker", required=True)
    team_shutdown.add_argument("--reason", required=True)
    team_ack_shutdown = team_subparsers.add_parser("ack-shutdown")
    team_ack_shutdown.add_argument("--root", default=".")
    team_ack_shutdown.add_argument("--team", required=True)
    team_ack_shutdown.add_argument("--worker", required=True)
    team_ack_shutdown.add_argument("--signal", required=True)
    team_status_parser = team_subparsers.add_parser("status")
    team_status_parser.add_argument("--root", default=".")
    team_status_parser.add_argument("--team", required=True)
    team_status_parser.add_argument("--detail", action="store_true")
    team_export = team_subparsers.add_parser("export")
    team_export.add_argument("--root", default=".")
    team_export.add_argument("--team", required=True)
    team_archive_export = team_subparsers.add_parser("archive-export")
    team_archive_export.add_argument("--archive-path", required=True)
    team_import_validate = team_subparsers.add_parser("import-validate")
    team_import_validate.add_argument("--file", required=True)
    team_import_dry_run = team_subparsers.add_parser("import-dry-run")
    team_import_dry_run.add_argument("--file", required=True)
    team_import_dry_run.add_argument("--report")
    team_import_apply = team_subparsers.add_parser("import-apply")
    team_import_apply.add_argument("--root", default=".")
    team_import_apply.add_argument("--file", required=True)
    team_import_apply.add_argument("--report")

    args = parser.parse_args(argv)
    requested_root = Path(getattr(args, "root", ".")).resolve()
    root = requested_root
    root, inferred_worktree = _resolve_cli_root_context(root, getattr(args, "worktree", None))
    if inferred_worktree is not None and hasattr(args, "worktree") and args.worktree is None:
        args.worktree = inferred_worktree

    if args.command == "init":
        init_project(root)
        print(f"initialized {root / '.agent-flow'}")
        return 0

    if args.command == "run":
        run_root = root
        state_root = root
        worktree_status = None
        worktree_preexisting = False
        # git repo에서는 별도 지정이 없어도 task 이름으로 격리 worktree를 먼저 만든다.
        # git이 답을 못 주는 상태(unknown)를 non-git으로 접으면 격리 없이 leader에서
        # 그대로 진행하게 되므로 fail-closed로 멈춘다.
        repo_state = git_repo_state(root)
        if repo_state == "unknown":
            print(
                "cannot determine git repo state; refusing to run unisolated in the leader checkout",
                file=sys.stderr,
            )
            return 2
        worktree_name = args.worktree if args.worktree is not None else (args.task if repo_state == "repo" else None)
        if worktree_name is not None:
            if repo_state != "repo":
                print("worktree runs require a git repository", file=sys.stderr)
                return 2
            try:
                plan = plan_worktree(root=root, name=worktree_name, branch=args.worktree_branch)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            state_root = worktree_runtime_root(root=root, name=plan.name)
            worktree_preexisting = plan.path.exists()
            try:
                worktree_status = create_worktree(root=root, plan=plan, allow_dirty=args.allow_dirty)
            except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
            print(f"worktree: {worktree_status.name} {worktree_status.path}")
            run_root = worktree_status.path
            state_root = worktree_runtime_root(root=root, name=worktree_status.name)
        active = find_active_run(state_root)
        if active is not None:
            print(f"already active: {active.run_id} (task: {active.task!r})")
            if worktree_name is None and repo_state == "repo":
                print(
                    "parallel worktree run: "
                    'agent-flow run "<task>"'
                )
            return 2
        try:
            Runner(
                run_root,
                state_root=state_root,
                config_root=root,
                workflow=args.workflow,
                architecture=args.architecture,
                next_command=_continue_command(root, worktree_name),
            ).run(
                mode=ResumeMode.START,
                task=args.task,
            )
        except (OSError, ValueError, RuntimeError, KeyError, subprocess.CalledProcessError) as exc:
            if worktree_status is not None and not worktree_preexisting:
                _cleanup_worktree_after_failure(root, worktree_status, exc)
            else:
                print(_format_cli_error(exc), file=sys.stderr)
            return 2
        return 0

    if args.command == "continue":
        try:
            run_root, state_root = _worktree_context(root, args.worktree) if args.worktree else (root, root)
        except ValueError as exc:
            print(_format_cli_error(exc), file=sys.stderr)
            return 2
        if run_root is None:
            return 1
        active = find_active_run(state_root)
        if active is None:
            if args.worktree:
                print(
                    f'진행 중인 run 없음. `agent-flow run "<task>" '
                    f'--worktree "{_slug_for_hint(root, args.worktree)}"`로 시작하세요.'
                )
            else:
                print('진행 중인 run 없음. `agent-flow run "<task>"`로 시작하세요.')
            return 0
        try:
            Runner(
                run_root,
                run_dir=active.path,
                state_root=state_root,
                config_root=root,
                next_command=_continue_command(root, args.worktree),
            ).run(mode=ResumeMode.RESUME)
        except (OSError, ValueError, RuntimeError, KeyError, subprocess.CalledProcessError) as exc:
            # `run`과 같은 처리다. tripwire가 raise하면 traceback 대신 사유를
            # 보여야 사용자가 다음 수를 안다.
            print(_format_cli_error(exc), file=sys.stderr)
            return 2
        return 0

    if args.command == "abort":
        try:
            run_root, state_root = _worktree_context(root, args.worktree) if args.worktree else (root, root)
        except ValueError as exc:
            print(_format_cli_error(exc), file=sys.stderr)
            return 2
        if run_root is None:
            return 1
        active = find_active_run(state_root)
        if active is None:
            print("진행 중인 run 없음 — abort할 대상이 없습니다.")
            return 0
        mark_inactive(active.path)
        print(f"aborted: {active.run_id} (artifacts preserved at {active.path})")
        return 0

    if args.command == "detect-profile":
        print(detect_profile(root))
        return 0

    if args.command == "provider":
        if args.provider_command == "list":
            for provider in list_host_providers():
                state = "available" if provider.available else "unavailable"
                print(f"{provider.name} {state} command={provider.command}")
            return 0

    if args.command == "status":
        try:
            run_root, state_root = _worktree_context(root, args.worktree) if args.worktree else (root, root)
        except ValueError as exc:
            print(_format_cli_error(exc), file=sys.stderr)
            return 2
        if run_root is None:
            return 1
        active = find_active_run(state_root)
        if active is not None:
            active.print_status(next_command=_continue_command(root, args.worktree), config_root=root)
            return 0
        if not (state_root / ".agent-flow" / "runs").exists():
            print("진행 중인 run 없음.")
            return 0
        print(status_summary(state_root))
        return 0

    if args.command == "report":
        run_dir = _resolve_run_dir(root, args.run_dir)
        if run_dir is None:
            return 1
        print(write_run_report(run_dir))
        return 0

    if args.command == "query":
        run_dir = _resolve_run_dir(root, args.run_dir)
        if run_dir is None:
            return 1
        for hit in query_run(run_dir, args.query, limit=args.limit):
            rel = hit.path.relative_to(run_dir)
            print(f"{rel}:{hit.line}: {hit.text}")
        return 0

    if args.command == "explain":
        run_dir = _resolve_run_dir(root, args.run_dir)
        if run_dir is None:
            return 1
        print(explain_run(run_dir, args.question), end="")
        return 0

    if args.command == "watch":
        run_dir = _resolve_run_dir(root, args.run_dir)
        if run_dir is None:
            return 1
        print(write_watch_snapshot(run_dir))
        return 0

    if args.command == "pr-watch":
        snapshot = (
            fetch_pr(args.number, repo=args.repo)
            if args.once
            else watch_pr(
                args.number,
                repo=args.repo,
                poll_interval_s=args.poll_interval,
                max_poll_count=args.max_polls,
            )
        )
        if snapshot is None:
            print(json.dumps({"number": args.number, "status": "error"}))
            return 1
        print(json.dumps(snapshot.to_summary(), ensure_ascii=False, indent=2))
        return 1 if snapshot.status == "error" else 0

    if args.command == "gates":
        command_root = _command_project_root(root, requested_root, getattr(args, "worktree", None))
        if command_root is None:
            return 1
        try:
            profile_ids = active_profile_ids(
                _profile_source_root(root, requested_root, getattr(args, "worktree", None)),
                args.profile,
            )
            commands = _profile_gate_commands(profile_ids)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        results = run_gates(commands, cwd=command_root, timeout_s=args.timeout)
        if args.run_dir is not None:
            write_gate_results(run_dir=_resolve_project_path(command_root, args.run_dir), results=results)
        failed = [result for result in results if not result.passed]
        required_results = [result for result in results if result.required]
        failed_required = [result for result in required_results if not result.passed]
        if failed and any(not result.required for result in failed):
            print(
                f"{','.join(profile_ids)}: "
                f"{len(required_results) - len(failed_required)}/{len(required_results)} required gates passed "
                f"({len(results) - len(failed)}/{len(results)} total gates passed)"
            )
        else:
            print(f"{','.join(profile_ids)}: {len(results) - len(failed)}/{len(results)} gates passed")
        return 1 if failed_required else 0

    if args.command == "architecture-lint":
        command_root = _command_project_root(root, requested_root, getattr(args, "worktree", None))
        if command_root is None:
            return 1
        try:
            profile_ids = active_profile_ids(
                _profile_source_root(root, requested_root, getattr(args, "worktree", None)),
                args.profile,
            )
            findings_by_profile = lint_profiles(command_root, profile_ids, files=args.files)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if not any(findings_by_profile.values()):
            print(f"{','.join(profile_ids)}: architecture lint passed")
            return 0
        print(f"{','.join(profile_ids)}: architecture lint failed", file=sys.stderr)
        for profile_id, findings in findings_by_profile.items():
            for finding in findings:
                print(f"- [{profile_id}] {finding.path}: {finding.message}", file=sys.stderr)
        return 1

    if args.command == "eval":
        fixture_path = _resolve_project_path(root, args.fixtures) if args.fixtures else None
        run_dir = _resolve_project_path(root, args.run_dir) if args.run_dir else None
        results = run_eval(
            root=root,
            fixture_path=fixture_path,
            judge_command=tuple(args.judge_command) if args.judge_command else None,
            run_dir=run_dir,
        )
        failed = [result for result in results if not result.passed]
        print(f"eval: {len(results) - len(failed)}/{len(results)} fixtures passed")
        for result in failed:
            print(f"{result.fixture_id}: {result.reason}")
        return 1 if failed else 0

    if args.command == "tools":
        if args.tools_command == "lint":
            findings = lint_tools(root)
            for finding in findings:
                print(f"{finding.severity}: {finding.path}: {finding.message}")
            print(f"tools lint: {len(findings)} findings")
            return 1 if any(finding.severity == "error" for finding in findings) else 0

    if args.command == "skills":
        return _run_skills_command(args, root)

    if args.command == "workflow":
        if args.workflow_command == "export":
            try:
                definition = load_phase_workflow_definition(_find_kit_root(), args.workflow)
            except (OSError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(json.dumps(definition.to_json_dict(), ensure_ascii=False, sort_keys=True))
            return 0

    if args.command == "context":
        run_dir = _resolve_project_path(root, args.run_dir) if getattr(args, "run_dir", None) else None
        if args.context_command == "init":
            print(ensure_context_contract(root=root, run_dir=run_dir))
            return 0
        if args.context_command == "event":
            try:
                details = json.loads(args.details_json)
            except json.JSONDecodeError as exc:
                print(f"invalid details JSON: {exc}", file=sys.stderr)
                return 2
            if not isinstance(details, dict):
                print("details JSON must be an object", file=sys.stderr)
                return 2
            print(append_context_event(root=root, run_dir=run_dir, event=args.event, details=details))
            return 0
        if args.context_command == "offload":
            print(offload_tool_output(root=root, run_dir=run_dir, name=args.name, content=args.content))
            return 0
        if args.context_command == "check-invariants":
            failures = check_system_invariants(root=root, run_dir=run_dir)
            for failure in failures:
                print(failure)
            print(f"system-invariants: {len(failures)} failures")
            return 1 if failures else 0
        if args.context_command == "write-invariants":
            print(
                write_system_invariants(
                    root=root,
                    invariants=[
                        "Preserve agent-flow status/next_command as workflow source of truth.",
                        "Do not reinstall or regenerate skill links from a managed worktree.",
                        "Use path-only inputs for long context; store large output under context tool_outputs/.",
                    ],
                )
            )
            return 0

    if args.command == "memory":
        if args.memory_command == "entities":
            memory_dir = _resolve_project_path(root, args.dir) if args.dir else root / ".agent-flow" / "memory" / "entities"
            index = EntityMemoryIndex.load(memory_dir)
            print(
                f"entities: {len(index.entries)} entries "
                f"stale={len(index.stale)} conflicts={len(index.conflicts)} skipped={len(index.skipped)}"
            )
            for entity, entries in index.conflicts:
                sources = ", ".join(str(entry.path) for entry in entries)
                print(f"conflict: {entity}: {sources}")
            for path, reason in index.skipped:
                print(f"skipped: {path}: {reason}")
            return 1 if index.conflicts or index.skipped else 0

    if args.command == "record-stage":
        path = write_stage_result(
            run_dir=_resolve_project_path(root, args.run_dir),
            stage_id=args.stage,
            status=args.status,
            evidence_type=args.evidence_type,
            confidence=args.confidence,
            content=args.content,
        )
        print(path)
        return 0

    if args.command == "handoff":
        path = write_handoff(
            root=root,
            run_dir=_resolve_project_path(root, args.run_dir),
            from_stage=args.from_stage,
            to_stage=args.to_stage,
            decided=args.decided,
            rejected=args.rejected,
            risks=args.risks,
            files=args.files,
            remaining=args.remaining,
        )
        print(path)
        return 0

    if args.command == "review-summary":
        run_dir = _resolve_project_path(root, args.run_dir)
        review_paths = [_resolve_project_path(root, value) for value in args.reviews]
        summary = summarize_reviews(review_paths)
        summary_path = write_review_summary(run_dir=run_dir, summary=summary)
        if summary.verdict == "NEEDS_CHANGES":
            write_recovery(
                run_dir=run_dir,
                title="Review needs changes",
                cause="Review findings require a fix stage.",
                artifacts=[str(summary_path), *(str(path) for path in review_paths)],
                rerun_command=(
                    "agent-flow record-stage --stage fix --status completed "
                    f"--run-dir {args.run_dir} --content '<fix summary>'"
                ),
                manual_action="Apply fixes, record the fix stage, then rerun review-summary.",
            )
        print(f"{summary.verdict}: {len(summary.findings)} findings")
        return 1 if summary.verdict == "NEEDS_CHANGES" else 0

    if args.command == "review":
        if args.review_command == "retry":
            try:
                _print_review_retry_status(args.reviewer, args.retry_after)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            return 0

    if args.command == "worktree":
        if args.worktree_command == "create":
            try:
                plan = plan_worktree(root=root, name=args.name, branch=args.branch)
                status = create_worktree(root=root, plan=plan, allow_dirty=args.allow_dirty)
            except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
            print(f"{status.name} {status.branch} {status.path}")
            return 0
        if args.worktree_command == "status":
            try:
                status = get_worktree_status(root=root, name=args.name)
            except ValueError as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
            state = "exists" if status.exists else "missing"
            print(f"{status.name} {status.branch} {status.path} {state}")
            return 0
        if args.worktree_command == "list":
            # 복구 명령이다. git이 대답하지 않아도 traceback으로 죽지 않고
            # 아는 만큼 보여준 뒤 정상 종료한다.
            try:
                names = _known_worktree_names(root)
            except (OSError, RuntimeError, ValueError) as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                names = []
            if not names:
                print("no worktrees")
                return 0
            for name in names:
                try:
                    status = get_worktree_status(root=root, name=name)
                except (ValueError, RuntimeError):
                    path = root / ".agent-flow" / "worktrees" / name
                    print(f"{name} - {path} stale")
                else:
                    state = "exists" if _worktree_checkout_exists(status) else "stale"
                    print(f"{status.name} {status.branch} {status.path} {state}")
            return 0
        if args.worktree_command == "remove":
            try:
                status = get_worktree_status(root=root, name=args.name)
            except (ValueError, RuntimeError) as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
            if not _worktree_checkout_exists(status):
                stale_dir = root / ".agent-flow" / "worktrees" / status.name
                try:
                    known = _known_worktree_names(root)
                except (OSError, RuntimeError, ValueError):
                    known = []
                if stale_dir.exists() or status.name in known:
                    if not args.keep_branch and status.branch_created_by_agent_flow:
                        prune = run_safe_command(("git", "worktree", "prune"), cwd=root)
                        if not prune.ok:
                            print(_format_safe_command_error(prune), file=sys.stderr)
                            return 2
                        if worktree_branch_exists(root=root, branch=status.branch):
                            delete = run_safe_command(("git", "branch", "-D", status.branch), cwd=root)
                            if not delete.ok:
                                print(_format_safe_command_error(delete), file=sys.stderr)
                                return 2
                    if stale_dir.is_dir():
                        shutil.rmtree(stale_dir)
                    elif stale_dir.exists():
                        stale_dir.unlink()
                    remove_worktree_metadata(root=root, name=status.name)
                    print(f"removed stale worktree manifest {status.name}")
                    return 0
                print(f"worktree not found or missing path: {status.name}", file=sys.stderr)
                return 1
            try:
                remove_worktree(
                    root=root,
                    status=status,
                    delete_branch=not args.keep_branch,
                    allow_unmerged=args.allow_unmerged,
                )
            except (subprocess.CalledProcessError, WorktreeIsolationError) as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
            print(f"removed {status.name} {status.path}")
            return 0

    if args.command == "team":
        if args.team_command == "list":
            teams = list_teams(root=root)
            for team in teams:
                print(f"{team.name} tasks={team.task_count} workers={team.worker_count} path={team.path}")
            return 0
        if args.team_command == "archive":
            archive = archive_team(root=root, team_name=args.team, reason=args.reason)
            print(f"{archive.name} archived {archive.archive_path}")
            return 0
        if args.team_command == "archive-list":
            archives = list_team_archives(root=root)
            for archive in archives:
                print(f"{archive.name} archived_at={archive.archived_at} reason={archive.reason} path={archive.archive_path}")
            return 0
        if args.team_command == "archive-restore":
            try:
                archive = restore_team_archive(root=root, archive_path=Path(args.archive_path))
            except Exception as exc:
                summary = {"valid": False, "errors": [f"cannot restore team archive: {exc}"]}
                report_error = _write_import_report(args.report, summary)
                if report_error is not None:
                    print(report_error)
                    return 1
                print(summary["errors"][0])
                return 1
            summary = {
                "valid": True,
                "team": archive.name,
                "source_path": archive.source_path,
                "archive_path": archive.archive_path,
                "archived_at": archive.archived_at,
                "reason": archive.reason,
            }
            report_error = _write_import_report(args.report, summary)
            if report_error is not None:
                print(report_error)
                return 1
            print(f"{archive.name} restored {archive.source_path}")
            return 0
        if args.team_command == "init":
            config = init_team(root=root, name=args.name, description=args.description)
            print(f"{config.name} {root / '.agent-flow' / 'state' / 'team' / config.name}")
            return 0
        if args.team_command == "task":
            task = add_task(
                root=root,
                team_name=args.team,
                task_id=args.id,
                subject=args.subject,
                description=args.description,
            )
            print(f"{task.task_id} {task.status}")
            return 0
        if args.team_command == "brief":
            brief = write_worker_brief(
                root=root,
                team_name=args.team,
                task_id=args.task,
                worker_name=args.worker,
                brief=args.brief,
                write_scope=args.write_scope,
            )
            print(f"{brief.task_id} brief {brief.worker}")
            return 0
        if args.team_command == "approve-worker":
            approval = approve_worker_call(
                root=root,
                team_name=args.team,
                task_id=args.task,
                worker_name=args.worker,
                reviewer=args.reviewer,
                write_scope=args.write_scope,
            )
            print(f"{approval.task_id} approved-worker {approval.worker} {approval.write_scope}")
            return 0
        if args.team_command == "result":
            result = write_worker_result(
                root=root,
                team_name=args.team,
                task_id=args.task,
                worker_name=args.worker,
                result=args.result,
            )
            print(f"{result.task_id} result {result.worker}")
            return 0
        if args.team_command == "approve":
            approval = approve_task_result(
                root=root,
                team_name=args.team,
                task_id=args.task,
                reviewer=args.reviewer,
                verdict=args.verdict,
                notes=args.notes,
            )
            print(f"{approval.task_id} {approval.verdict} {approval.reviewer}")
            return 0
        if args.team_command == "worker":
            worker = add_worker(root=root, team_name=args.team, worker_name=args.name, role=args.role)
            print(f"{worker.name} {worker.role} {worker.status}")
            return 0
        if args.team_command == "run-next":
            if not args.command_argv:
                print("command is required")
                return 1
            status = team_status(root=root, team_name=args.team, detail=True)
            pending = next((task for task in status["tasks"] if task.status == "pending"), None)
            if pending is None:
                print("no pending task")
                return 1
            prompt = _team_run_next_prompt(
                root=root,
                team_name=args.team,
                task_id=pending.task_id,
                worker_name=args.worker,
                fallback_subject=pending.subject,
                fallback_description=pending.description,
            )
            if prompt is None:
                print(f"worker not approved for task: {pending.task_id}")
                return 1
            # Worker isolation decision. A git repo runs every worker in its own
            # verified worktree; a git call that cannot answer is fail-closed
            # (return 2), never a silent fallback to the leader checkout.
            repo_state = git_repo_state(root)
            if repo_state == "unknown":
                print("cannot determine git repo state; refusing to run unisolated", file=sys.stderr)
                return 2
            isolate = repo_state == "repo"
            worker_cwd = root
            worker_env = None
            worktree_status = None
            claimed = None
            leader_before = None
            if isolate:
                try:
                    with worker_claim_lock(root):
                        # capacity를 세는 시점과 task를 잡는 시점이 갈라져 있으면
                        # 두 워커가 같은 마지막 슬롯을 함께 통과한다. 세기와 잡기를
                        # 한 락 안에 묶고, 카운트도 락 안에서 다시 읽는다.
                        capacity = max_worker_capacity()
                        live = team_status(root=root, team_name=args.team, detail=True)
                        in_progress = sum(1 for task in live["tasks"] if task.status == "in_progress")
                        if in_progress >= capacity:
                            print(f"worker capacity reached ({in_progress}/{capacity})", file=sys.stderr)
                            return 2
                        # Per-worker worktrees isolate every write, so overlapping
                        # scopes are safe here; the scope gate only bites when there
                        # is no worktree isolation (the else branch).
                        plan = plan_worktree(root=root, name=pending.task_id, unique=args.worker)
                        worktree_status = create_worktree(root=root, plan=plan, allow_dirty=True)
                        worker_cwd = verify_linked_worktree(
                            root=root, path=worktree_status.path, expected_branch=plan.branch
                        )
                        assert_cwd_bound(worktree_path=worktree_status.path, cwd=worker_cwd)
                        # 격리된 워커가 자기 worktree 밖으로 새어 나갔는지는
                        # 명령이 아니라 leader의 파일시스템 상태 변화로만
                        # 판정한다. 스냅샷을 claim **앞**에서 찍는다 — 뒤에서
                        # 찍다 raise하면 claim된 task가 영구 in_progress로
                        # 고착되고 claim token이 없어 복구도 못 한다.
                        leader_before = capture_leader_snapshot(root)
                        claimed = claim_task(
                            root=root,
                            team_name=args.team,
                            task_id=pending.task_id,
                            worker_name=args.worker,
                        )
                except (OSError, ValueError, RuntimeError, WorktreeIsolationError, subprocess.CalledProcessError) as exc:
                    print(_format_cli_error(exc), file=sys.stderr)
                    return 2
                worker_env = sanitized_worker_env()
            else:
                # No worktree isolation available: concurrent workers share the
                # leader checkout, so overlapping write scopes would collide.
                active = {safe_worker_name(task.owner) for task in status["tasks"]
                          if task.status == "in_progress" and task.owner}
                active.add(safe_worker_name(args.worker))
                scopes = [
                    WorkerScope(worker=worker_name, paths=paths, worktree_isolated=False)
                    for worker_name, paths in approved_worker_scopes(root=root, team_name=args.team)
                    if worker_name in active
                ]
                try:
                    assert_scopes_isolated(scopes)
                except WorktreeIsolationError as exc:
                    print(_format_cli_error(exc), file=sys.stderr)
                    return 2
                claimed = claim_task(
                    root=root,
                    team_name=args.team,
                    task_id=pending.task_id,
                    worker_name=args.worker,
                )
            result = run_provider(
                ProviderCommand(name="host-command", argv=tuple(args.command_argv)),
                prompt=prompt,
                cwd=worker_cwd,
                env=worker_env,
            )
            if leader_before is not None:
                try:
                    assert_leader_unchanged(root, leader_before, run_id=claimed.task_id)
                except WorktreeIsolationError as exc:
                    message = _format_cli_error(exc)
                    print(message, file=sys.stderr)
                    fail_task(
                        root=root,
                        team_name=args.team,
                        task_id=claimed.task_id,
                        claim_token=claimed.claim_token or "",
                        result=message,
                    )
                    return 2
            output = result.stdout.strip() or result.stderr.strip()
            if result.failed:
                task = fail_task(
                    root=root,
                    team_name=args.team,
                    task_id=claimed.task_id,
                    claim_token=claimed.claim_token or "",
                    result=output,
                )
            else:
                task = complete_task(
                    root=root,
                    team_name=args.team,
                    task_id=claimed.task_id,
                    claim_token=claimed.claim_token or "",
                    result=output,
                )
            suffix = f" worktree={worktree_status.path}" if worktree_status is not None else ""
            print(f"{task.task_id} {task.status}{suffix}")
            return 1 if result.failed else 0
        if args.team_command == "claim":
            task = claim_task(root=root, team_name=args.team, task_id=args.task, worker_name=args.worker)
            print(f"{task.task_id} {task.status} {task.owner} {task.claim_token}")
            return 0
        if args.team_command == "complete":
            task = complete_task(
                root=root,
                team_name=args.team,
                task_id=args.task,
                claim_token=args.claim_token,
                result=args.result,
            )
            print(f"{task.task_id} {task.status}")
            return 0
        if args.team_command == "fail":
            task = fail_task(
                root=root,
                team_name=args.team,
                task_id=args.task,
                claim_token=args.claim_token,
                result=args.result,
            )
            print(f"{task.task_id} {task.status}")
            return 0
        if args.team_command == "message":
            message = send_message(
                root=root,
                team_name=args.team,
                from_actor=args.from_actor,
                to_worker=args.to_worker,
                body=args.body,
            )
            print(f"{message.message_id} {message.to_worker} unread")
            return 0
        if args.team_command == "messages":
            messages = list_messages(
                root=root,
                team_name=args.team,
                worker_name=args.worker,
                unread_only=args.unread_only,
            )
            for message in messages:
                state = "read" if message.read else "unread"
                print(f"{message.message_id} {message.from_actor} {state} {message.body}")
            return 0
        if args.team_command == "mark-read":
            message = mark_message_read(
                root=root,
                team_name=args.team,
                worker_name=args.worker,
                message_id=args.message,
            )
            print(f"{message.message_id} read")
            return 0
        if args.team_command == "heartbeat":
            heartbeat = update_worker_heartbeat(
                root=root,
                team_name=args.team,
                worker_name=args.worker,
                status=args.status,
                alive=not args.dead,
            )
            state = "alive" if heartbeat.alive else "dead"
            print(f"{heartbeat.worker} {heartbeat.status} {state} {heartbeat.updated_at}")
            return 0
        if args.team_command == "shutdown":
            signal = request_shutdown(
                root=root,
                team_name=args.team,
                worker_name=args.worker,
                reason=args.reason,
            )
            print(f"{signal.signal_id} {signal.worker} pending")
            return 0
        if args.team_command == "ack-shutdown":
            signal = acknowledge_shutdown(
                root=root,
                team_name=args.team,
                worker_name=args.worker,
                signal_id=args.signal,
            )
            print(f"{signal.signal_id} {signal.worker} acknowledged")
            return 0
        if args.team_command == "status":
            status = team_status(root=root, team_name=args.team, detail=args.detail)
            print(
                f"{status['team']} tasks={status['task_count']} "
                f"workers={status['worker_count']} exists={status['exists']}"
            )
            for heartbeat in status["heartbeats"]:
                state = "alive" if heartbeat.alive else "dead"
                print(f"{heartbeat.worker} {heartbeat.status} {state} {heartbeat.updated_at}")
            if args.detail:
                for task in status["tasks"]:
                    owner = task.owner or "-"
                    print(f"task {task.task_id} {task.status} owner={owner} subject={task.subject}")
                unread_counts = status["unread_counts"]
                for worker in status["workers"]:
                    unread = unread_counts.get(worker.name, 0)
                    print(f"worker {worker.name} role={worker.role} status={worker.status} unread={unread}")
                for signal in status["shutdowns"]:
                    state = "acknowledged" if signal.acknowledged else "pending"
                    print(f"shutdown {signal.signal_id} worker={signal.worker} {state} reason={signal.reason}")
            return 0
        if args.team_command == "export":
            print(json.dumps(export_team_state(root=root, team_name=args.team), indent=2, sort_keys=True))
            return 0
        if args.team_command == "archive-export":
            print(json.dumps(export_team_archive(archive_path=Path(args.archive_path)), indent=2, sort_keys=True))
            return 0
        if args.team_command == "import-validate":
            payload = _read_json_file(args.file)
            if isinstance(payload, str):
                print(payload)
                return 1
            errors = validate_team_state_import(payload)
            if errors:
                for error in errors:
                    print(error)
                return 1
            print("OK")
            return 0
        if args.team_command == "import-dry-run":
            payload = _read_json_file(args.file)
            if isinstance(payload, str):
                report_error = _write_import_report(args.report, {"valid": False, "errors": [payload]})
                if report_error is not None:
                    print(report_error)
                    return 1
                print(payload)
                return 1
            summary = summarize_team_state_import(payload)
            if not summary["valid"]:
                report_error = _write_import_report(args.report, summary)
                if report_error is not None:
                    print(report_error)
                    return 1
                for error in summary["errors"]:
                    print(error)
                return 1
            report_error = _write_import_report(args.report, summary)
            if report_error is not None:
                print(report_error)
                return 1
            print(
                f"{summary['team']} tasks={summary['task_count']} workers={summary['worker_count']} "
                f"heartbeats={summary['heartbeat_count']} mailboxes={summary['mailbox_count']} "
                f"messages={summary['message_count']} shutdowns={summary['shutdown_count']}"
            )
            return 0
        if args.team_command == "import-apply":
            payload = _read_json_file(args.file)
            if isinstance(payload, str):
                report_error = _write_import_report(args.report, {"valid": False, "errors": [payload]})
                if report_error is not None:
                    print(report_error)
                    return 1
                print(payload)
                return 1
            summary = apply_team_state_import(root=root, payload=payload)
            if not summary["valid"]:
                report_error = _write_import_report(args.report, summary)
                if report_error is not None:
                    print(report_error)
                    return 1
                for error in summary["errors"]:
                    print(error)
                return 1
            report_error = _write_import_report(args.report, summary)
            if report_error is not None:
                print(report_error)
                return 1
            print(f"{summary['team']} imported")
            return 0

    if args.command == "start":
        worktree = None
        worktree_status = None
        worktree_preexisting = False
        state = None
        # start 명령도 run과 동일하게 git repo에서는 worktree를 기본 시작점으로 삼는다.
        # git 상태가 unknown이면 격리 여부를 증명할 수 없으므로 진행하지 않는다.
        repo_state = git_repo_state(root)
        if repo_state == "unknown":
            print(
                "cannot determine git repo state; refusing to start unisolated in the leader checkout",
                file=sys.stderr,
            )
            return 2
        worktree_name = args.worktree if args.worktree is not None else (args.task if repo_state == "repo" else None)
        if worktree_name is not None and repo_state != "repo":
            print("worktree runs require a git repository", file=sys.stderr)
            return 2
        try:
            workflow = load_workflow(args.workflow)
            profile = detect_profile(root) if args.profile == "auto" else args.profile
            adapter = detect_adapter() if args.adapter == "auto" else args.adapter
            if worktree_name is not None:
                plan = plan_worktree(root=root, name=worktree_name, branch=args.worktree_branch)
                worktree_preexisting = plan.path.exists()
                status = create_worktree(root=root, plan=plan, allow_dirty=args.allow_dirty)
                worktree_status = status
                worktree = {
                    "name": status.name,
                    "branch": status.branch,
                    "path": str(status.path),
                }
            state_root = worktree_runtime_root(root=root, name=worktree["name"]) if worktree is not None else root
            state = start_run(
                root=state_root,
                request=RunRequest(
                    workflow_id=workflow.workflow_id,
                    task=args.task,
                    adapter=adapter,
                    profile=profile,
                    architecture=args.architecture,
                    run_id=args.run_id,
                    worktree=worktree,
                ),
            )
            _write_stage_prompts(root=state_root, state=state, workflow=workflow)
        except (OSError, ValueError, RuntimeError, KeyError, subprocess.CalledProcessError) as exc:
            if state is not None and state.run_dir.exists():
                shutil.rmtree(state.run_dir)
            if worktree_status is not None and not worktree_preexisting:
                _cleanup_worktree_after_failure(root, worktree_status, exc)
            else:
                print(_format_cli_error(exc), file=sys.stderr)
            return 2
        print(state.run_dir)
        return 0

    return 1


def _write_stage_prompts(*, root: Path, state: RunState, workflow) -> None:
    for stage in workflow.stages:
        count = stage.replicas if stage.parallel else 1
        for replica in range(1, count + 1):
            prompt_id = stage.stage_id if count == 1 else f"{stage.stage_id}-{replica}"
            write_prompt(
                root=root,
                run_dir=state.run_dir,
                stage_id=prompt_id,
                content=render_stage_prompt(
                    PromptContext(
                        adapter=state.adapter,
                        stage_id=stage.stage_id,
                        role=stage.role,
                        workflow_id=state.workflow_id,
                        run_id=state.run_id,
                        architecture=state.architecture,
                        replica=replica,
                        replicas=count,
                        task=state.task,
                    )
                ),
            )


def _read_json_file(path: str) -> object | str:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        return f"cannot read import file: {exc}"
    except json.JSONDecodeError as exc:
        return f"invalid JSON: {exc}"


def _write_import_report(path: str | None, report: dict[str, object]) -> str | None:
    if path is None:
        return None
    report_path = Path(path)
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(f"{json.dumps(report, indent=2, sort_keys=True)}\n", encoding="utf-8")
    except OSError as exc:
        return f"cannot write import report: {exc}"
    return None


def _team_run_next_prompt(
    *,
    root: Path,
    team_name: str,
    task_id: str,
    worker_name: str,
    fallback_subject: str,
    fallback_description: str,
) -> str | None:
    safe_team = safe_team_name(team_name)
    safe_worker = safe_worker_name(worker_name)
    contract_dir = root / ".agent-flow" / "state" / "team" / safe_team / "tasks" / task_id
    approvals_path = contract_dir / "workers_approved.json"
    brief_path = contract_dir / "worker-brief.md"
    if not approvals_path.exists() or not brief_path.exists():
        return None
    try:
        approvals = json.loads(approvals_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(approvals, list):
        return None
    approval = next(
        (
            item
            for item in approvals
            if isinstance(item, dict)
            and item.get("task_id") == task_id
            and item.get("worker") == safe_worker
        ),
        None,
    )
    if approval is None:
        return None
    write_scope = str(approval.get("write_scope", "none"))
    lines = [
        fallback_subject,
        "",
        fallback_description,
        "",
        "## Worker Contract",
        "",
        f"worker-brief.md: {brief_path}",
        f"workers_approved.json: {approvals_path}",
        f"write_scope: {write_scope}",
        "Use path-only input for large context. Read only the files needed for this task.",
        "",
    ]
    return "\n".join(lines)


def _resolve_project_path(root: Path, value: str) -> Path:
    return resolve_project_path(root, value)


def _profile_gate_commands(profile_ids: list[str]) -> list[GateCommand]:
    commands: list[tuple[int, GateCommand]] = []
    seen: set[tuple[str, ...]] = set()
    multi_profile = len(profile_ids) > 1
    architecture_lint_added = False
    architecture_lint_profile = ",".join(profile_ids)
    order = 0
    for profile_id in profile_ids:
        profile = load_profile(profile_id)
        for gate in profile.gates:
            command = _normalize_profile_gate_command(profile.profile_id, gate.gate_id, gate.command)
            required = gate.required
            gate_id = f"{profile.profile_id}:{gate.gate_id}" if multi_profile else gate.gate_id
            if multi_profile and _is_architecture_lint_gate(gate.gate_id, gate.command):
                if architecture_lint_added:
                    continue
                command = _architecture_lint_command(architecture_lint_profile)
                gate_id = "architecture-lint"
                required = True
                architecture_lint_added = True
            if command in seen:
                continue
            seen.add(command)
            commands.append((order, GateCommand(gate_id, command, required=required)))
            order += 1
    return [
        command
        for _, command in sorted(
            commands,
            key=lambda item: (*_gate_order_key(item[1]), item[0]),
        )
    ]


def _is_architecture_lint_gate(gate_id: str, command: tuple[str, ...]) -> bool:
    return gate_id == "architecture-lint" or "architecture-lint" in command


def _normalize_profile_gate_command(profile_id: str, gate_id: str, command: tuple[str, ...]) -> tuple[str, ...]:
    if _is_architecture_lint_gate(gate_id, command):
        profile_index = command.index("--profile") + 1 if "--profile" in command else -1
        if profile_index > 0 and profile_index < len(command):
            return _architecture_lint_command(command[profile_index])
    if profile_id == "python" and command:
        if command[0] in {"mypy", "pytest", "ruff"}:
            return (sys.executable, "-m", command[0], *command[1:])
    return command


def _architecture_lint_command(profile_ids: str) -> tuple[str, ...]:
    return (sys.executable, "-m", "agent_flow.core.architecture_lint", "--profile", profile_ids)


def _gate_order_key(gate: GateCommand) -> tuple[int, int, str]:
    gate_id = gate.gate_id
    command = " ".join(gate.command).lower()
    lowered = f"{gate_id} {command}".lower()
    if any(token in lowered for token in ("build", "assemble", "xcodebuild")):
        return (0, _profile_gate_kind_tiebreaker(lowered), gate_id)
    if any(token in lowered for token in ("typecheck", "tsc", "mypy", "pyright", "type ")):
        return (1, _profile_gate_kind_tiebreaker(lowered), gate_id)
    if any(token in lowered for token in ("lint", "ruff", "detekt", "ktlint", "check-context-docs", "architecture-lint")):
        return (2, _profile_gate_kind_tiebreaker(lowered), gate_id)
    if "test" in lowered or "pytest" in lowered:
        return (3, _profile_gate_kind_tiebreaker(lowered), gate_id)
    return (4, _profile_gate_kind_tiebreaker(lowered), gate_id)


def _profile_gate_kind_tiebreaker(text: str) -> int:
    if "architecture-lint" in text:
        return 0
    if "context" in text:
        return 1
    return 2


def _resolve_run_dir(root: Path, value: str | None) -> Path | None:
    run_dir = _resolve_project_path(root, value) if value else _latest_run_dir(root)
    if run_dir is None:
        print("no runs" if value is None else f"run dir not found: {_resolve_project_path(root, value)}", file=sys.stderr)
        return None
    if not run_dir.is_dir():
        print(f"run dir not found: {run_dir}", file=sys.stderr)
        return None
    return run_dir


def _worktree_root(root: Path, name: str) -> Path | None:
    status = get_worktree_status(root=root, name=name)
    if _worktree_checkout_exists(status):
        return status.path
    known = _known_worktree_names(root)
    suffix = f" known worktrees: {', '.join(known)}" if known else " no known worktrees"
    print(f"worktree not found or missing path: {status.name}.{suffix}", file=sys.stderr)
    return None


def _worktree_context(root: Path, name: str) -> tuple[Path | None, Path]:
    status = get_worktree_status(root=root, name=name)
    if _worktree_checkout_exists(status):
        return status.path, worktree_runtime_root(root=root, name=status.name)
    known = _known_worktree_names(root)
    suffix = f" known worktrees: {', '.join(known)}" if known else " no known worktrees"
    print(f"worktree not found or missing path: {status.name}.{suffix}", file=sys.stderr)
    return None, worktree_runtime_root(root=root, name=status.name)


def _command_project_root(config_root: Path, requested_root: Path, worktree: str | None) -> Path | None:
    if worktree is None:
        return config_root
    managed = _managed_worktree_context(requested_root)
    if managed is not None and managed[1] == worktree:
        return requested_root
    literal_checkout = config_root / ".agent-flow" / "worktrees" / worktree
    if (literal_checkout / ".git").exists():
        return literal_checkout
    return _worktree_root(config_root, worktree)


def _profile_source_root(config_root: Path, requested_root: Path, worktree: str | None) -> Path:
    if worktree is None:
        return config_root
    managed = _managed_worktree_context(requested_root)
    if managed is not None and managed[1] == worktree:
        return managed[0]
    return config_root


def _continue_command(root: Path, worktree: str | None) -> str:
    command = f"agent-flow continue --root {shlex.quote(str(root))}"
    if worktree is None:
        return command
    return command + f" --worktree {shlex.quote(_slug_for_hint(root, worktree))}"


def _known_worktree_names(root: Path) -> list[str]:
    return known_worktree_names(root=root)


def _worktree_checkout_exists(status) -> bool:
    return status.exists and (status.path / ".git").exists()


def _format_cli_error(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        return detail or str(exc)
    return str(exc)


def _format_safe_command_error(result) -> str:
    # safe command 오류는 CLI 루프가 죽지 않게 stderr 중심으로 짧게 노출한다.
    detail = (result.stderr or result.stdout or result.error or "").strip()
    command = " ".join(result.args)
    if result.timed_out:
        return f"{command} timed out: {detail}"
    return f"{command} failed: {detail}".strip()


def _status_value(value: object) -> str:
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def _print_review_retry_status(reviewer: str, retry_after: str | None) -> None:
    retry_at = _parse_retry_after_arg(retry_after)
    retry_command = f"agent-flow review retry --reviewer {shlex.quote(reviewer)}"
    if retry_after is not None:
        retry_command += f" --retry-after {shlex.quote(retry_after)}"
    if retry_at is not None and retry_at > datetime.now(timezone.utc):
        print("status: blocked")
        print("reason: reviewer_rate_limited")
        print(f"reviewer: {_status_value(reviewer)}")
        print(f"retry_after: {_status_value(retry_after)}")
        print("required_action: wait_until_retry_after")
        print(f"next_command: {_status_value(retry_command)}")
        return
    print("status: awaiting_retry")
    print("reason: reviewer_retry_ready")
    print(f"reviewer: {_status_value(reviewer)}")
    if retry_after is not None:
        print(f"retry_after: {_status_value(retry_after)}")
    print("required_action: rerun_review_now")
    print("next_command: none")


def _parse_retry_after_arg(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            "--retry-after must be ISO-8601, e.g. 2026-05-08T10:40:00+00:00"
        ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _cleanup_worktree_after_failure(root: Path, status, original: BaseException) -> None:
    try:
        remove_worktree(root=root, status=status)
    except WorktreeIsolationError as preserve_exc:
        # Fail closed: the worktree holds uncommitted or unmerged work. Preserve
        # it so a run failure does not also destroy the worker's changes.
        print(
            f"warning: preserving worktree {status.name} at {status.path}: "
            f"{_format_cli_error(preserve_exc)}",
            file=sys.stderr,
        )
    except (subprocess.CalledProcessError, OSError) as cleanup_exc:
        print(
            f"warning: failed to clean up worktree {status.name}: "
            f"{_format_cli_error(cleanup_exc)}",
            file=sys.stderr,
        )
    print(_format_cli_error(original), file=sys.stderr)


def _slug_for_hint(root: Path, value: str) -> str:
    try:
        return plan_worktree(root=root, name=value).name
    except ValueError:
        return value


def _resolve_cli_root_context(root: Path, worktree: str | None) -> tuple[Path, str | None]:
    managed = _managed_worktree_context(root)
    if managed is not None:
        leader_root, inferred_worktree = managed
        return leader_root, worktree or inferred_worktree
    cwd_managed = _managed_worktree_context(Path.cwd())
    if cwd_managed is not None and (_same_path(root, Path.cwd()) or _same_path(root, cwd_managed[0])):
        leader_root, inferred_worktree = cwd_managed
        return leader_root, worktree or inferred_worktree
    git_common_root = _git_common_worktree_root(root)
    if git_common_root is not None:
        return git_common_root, worktree
    return root, worktree


def _managed_worktree_context(path: Path) -> tuple[Path, str] | None:
    resolved = path.resolve()
    parts = resolved.parts
    markers = {".agent-flow", ".codex", ".Codex"}
    for index in range(len(parts) - 2, 0, -1):
        if parts[index] not in markers or parts[index + 1] != "worktrees":
            continue
        root = Path(*parts[:index])
        if parts[index] in {".codex", ".Codex"} and _same_path(root, _home_path()):
            continue
        return root, parts[index + 2]
    return None


def _git_common_worktree_root(root: Path) -> Path | None:
    # worktree root 탐지는 relay 진입점이므로 git hang을 짧게 실패 처리한다.
    top_level = run_safe_command(("git", "rev-parse", "--show-toplevel"), cwd=root)
    common_dir = run_safe_command(("git", "rev-parse", "--git-common-dir"), cwd=root)
    if not top_level.ok or not common_dir.ok:
        return None
    common_path = Path(common_dir.stdout.strip())
    if not common_path.is_absolute():
        common_path = Path(top_level.stdout.strip()) / common_path
    if common_path.name != ".git":
        return None
    return common_path.parent.resolve()


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left.absolute() == right.absolute()


def _home_path() -> Path:
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or str(Path.home())
    return Path(home).expanduser()


def _latest_run_dir(root: Path) -> Path | None:
    runs_root = root / ".agent-flow" / "runs"
    if not runs_root.exists():
        return None
    candidates = [path.parent for path in runs_root.glob("*/*/manifest.json")]
    candidates.extend(path for path in runs_root.iterdir() if _is_legacy_run_dir(path))
    if not candidates:
        return None
    return max(candidates, key=_run_activity_mtime)


def _run_activity_mtime(path: Path) -> float:
    mtimes = [path.stat().st_mtime]
    for child in _run_activity_files(path):
        mtimes.append(child.stat().st_mtime)
    return max(mtimes)


def _run_activity_files(path: Path) -> list[Path]:
    files = [
        child
        for pattern in ("*.json", "*.jsonl", "*.md", "artifacts/*", "handoffs/*")
        for child in path.glob(pattern)
        if child.is_file()
    ]
    return files


def _is_legacy_run_dir(path: Path) -> bool:
    if not (path / "meta.json").exists():
        return False
    return not any(child.is_dir() and (child / "manifest.json").exists() for child in path.iterdir())


def _run_skills_command(args: argparse.Namespace, root: Path) -> int:
    profile_ids = active_profile_ids(root, getattr(args, "profile", None) or "auto")
    payloads = [load_profile_payload(profile_id) for profile_id in profile_ids]

    if args.skills_command == "sync":
        exit_code = 0
        for profile_id, payload in zip(profile_ids, payloads):
            sources = parse_skill_sources(payload)
            if not sources:
                print(f"{profile_id}: no skill_sources declared")
                continue
            for result in sync_skill_sources(sources):
                print(f"{profile_id}: {result.source_id} {result.status} {result.detail}".rstrip())
                if result.status == "failed":
                    exit_code = 1
        return exit_code

    if args.skills_command in {"resolve", "prompt", "markers"}:
        try:
            definition = load_phase_workflow_definition(_find_kit_root(), args.workflow)
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        phase = next((item for item in definition.phases if item.id == args.phase), None)
        if phase is None:
            # 조용히 "요구 없음"으로 답하면 gate가 통과해 버린다. phase가
            # workflow에 없다는 건 상태가 어긋났다는 뜻이므로 fail-closed다.
            print(
                f"phase {args.phase!r} is not in workflow {args.workflow!r}",
                file=sys.stderr,
            )
            return 2
        merged = merged_profile_payload(payloads)
        # 호출자가 컨텍스트를 넘겨주길 기대하면 경로마다 갈라진다(JS는 안 넘겨서 자동
        # 활성화가 통째로 죽었다). 여기서 직접 도출해 모든 호출자가 같은 답을 받게 한다.
        context = _skill_context(root, args)

        if args.skills_command == "prompt":
            sys.stdout.write(
                local_skill_prompt_block(
                    root,
                    phase.id,
                    phase_skills=phase.skills,
                    profile=merged,
                    changed_files=context["changed_files"],
                    task_text=context["task_text"],
                )
            )
            return 0

        if args.skills_command == "markers":
            artifact = _resolve_project_path(root, args.artifact)
            text = artifact.read_text(encoding="utf-8") if artifact.is_file() else ""
            print(
                json.dumps(
                    missing_local_skill_markers(
                        text,
                        root,
                        phase.id,
                        phase_skills=phase.skills,
                        profile=merged,
                        changed_files=context["changed_files"],
                        task_text=context["task_text"],
                        since=context["since"],
                    ),
                    ensure_ascii=False,
                )
            )
            return 0

        resolution = phase_skill_resolution(
            root,
            phase.id,
            phase_skills=phase.skills,
            profile=merged,
            changed_files=context["changed_files"],
            task_text=context["task_text"],
        )
        for skill in resolution.required:
            state = skill.display_path(root) if skill.exists else f"MISSING ({skill.install_hint})"
            print(f"required {skill.name}: {state}")
        for skill in resolution.optional:
            state = skill.display_path(root) if skill.exists else "not installed"
            print(f"optional {skill.name}: {state}")
        print(f"skill-availability: {'degraded' if resolution.missing else 'pass'}")
        return 0

    return 2


def _skill_context(root: Path, args: argparse.Namespace) -> dict:
    """활성 run에서 task/변경파일/phase 진입시각을 도출한다.

    CLI 인자로도 override할 수 있지만 기본값이 있어야 JS wrapper와 `status`가
    Python runner와 같은 결론에 도달한다.
    """
    task = getattr(args, "task", None)
    since = getattr(args, "since", None)
    if task is None or since is None:
        meta = _active_run_meta(root)
        if task is None:
            task = str(meta.get("task", ""))
        if since is None:
            since = _run_meta_timestamp(meta)
    return {
        "task_text": task or "",
        "since": since,
        "changed_files": changed_files(root),
    }


def _active_run_meta(root: Path) -> dict:
    """활성 run의 meta. Python run과 JS state 중 **더 최근** 것이 이긴다.

    둘 다 존재할 수 있고(같은 프로젝트를 두 경로로 몰아본 흔적), 오래된 쪽이
    이기면 지난 phase의 task/시각으로 skill을 판정하게 된다.
    """
    candidates: list[dict] = []
    for state_root in _skill_state_roots(root):
        try:
            active = find_active_run(state_root)
        except OSError:
            continue
        if active is None:
            continue
        meta = read_meta(active.path)
        if meta:
            candidates.append(meta)
    # JS runner는 Python meta.json이 아니라 이 파일에 run 상태를 쓴다.
    # 여기를 안 보면 JS 경로에서 task/시각이 통째로 비어 자동 활성화가 죽는다.
    js_state = _js_run_state(root)
    if js_state:
        candidates.append(js_state)
    if not candidates:
        return {}
    dated = [(ts, meta) for meta in candidates if (ts := _run_meta_timestamp(meta)) is not None]
    if dated:
        return max(dated, key=lambda pair: pair[0])[1]
    return candidates[0]


def _js_run_state(root: Path) -> dict:
    state_path = root / ".agent-flow" / "state" / "current-run.json"
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _skill_state_roots(root: Path):
    """run meta가 있을 수 있는 자리들. leader와 관리형 worktree 런타임 루트."""
    yield root
    try:
        names = known_worktree_names(root=root)
    except (OSError, RuntimeError):
        return
    for name in names:
        try:
            yield worktree_runtime_root(root=root, name=name)
        except (OSError, RuntimeError, ValueError):
            continue


def _run_meta_timestamp(meta: dict) -> float | None:
    for key in ("phase_entered_at", "updated_at", "started_at"):
        raw = meta.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
    return None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
