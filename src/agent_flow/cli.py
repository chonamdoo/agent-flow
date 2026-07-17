from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from agent_flow.adapters.registry import detect_adapter
from agent_flow.adapters.templates import PromptContext, render_stage_prompt
from agent_flow.core.artifacts import (
    gate_execution_fingerprint,
    init_project,
    reusable_gate_results,
    write_gate_cache,
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
from agent_flow.core.gates import GateCommand, GateResult, run_gates
from agent_flow.core.phase_workflow import load_phase_workflow_definition
from agent_flow.core.profiles import active_profile_ids, detect_profile, load_profile
from agent_flow.core.review import summarize_reviews, write_review_summary
from agent_flow.core.report import write_run_report
from agent_flow.core.query import explain_run, query_run
from agent_flow.core.security import resolve_project_path
from agent_flow.core.workspace_boundary import (
    WorkspaceBoundaryError,
    acquire_workspace_start_claim,
    execution_identity_from_context,
    execution_identity_from_dict,
    find_active_pinned_workspaces,
    release_execution_binding,
    release_workspace_start_claim,
    resolve_execution_finalizer_workspace,
    select_execution_workspace,
    workspace_identity_from_dict,
)
from agent_flow.core.tool_lint import lint_tools
from agent_flow.core.watch import write_watch_snapshot
from agent_flow.core.team import (
    acknowledge_shutdown,
    add_task,
    add_worker,
    approve_worker_call,
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
    delete_worktree_branch_at_tip,
    get_worktree_status,
    known_worktree_names,
    plan_worktree,
    preserved_worktree_branch_tip,
    remove_worktree_metadata,
    remove_worktree,
    worktree_branch_exists,
    worktree_branch_is_preserved,
    worktree_runtime_root,
    validate_worktree_removal,
    worktree_lifecycle_lock,
)
from agent_flow.core.state import RunRequest, RunState, start_run, status_summary
from agent_flow.core.workflow import load_workflow
from agent_flow.eval import run_eval
from agent_flow.memory.entities import EntityMemoryIndex
from agent_flow.artifact import find_active_run, mark_inactive, read_meta, write_meta
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
    gates_parser.add_argument("--files", nargs="*")
    gates_parser.add_argument("--full", action="store_true")

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
    inferred_worktree = None
    direct_managed_read = (
        args.command == "architecture-lint"
        and getattr(args, "worktree", None) is None
        and _managed_worktree_context(requested_root) is not None
    )
    pure_control_command = args.command == "review" and args.review_command == "retry"
    if hasattr(args, "root") and not direct_managed_read and not pure_control_command:
        try:
            root, inferred_worktree = _resolve_cli_root_context(
                root,
                getattr(args, "worktree", None),
                allow_unbound_execution=args.command == "run",
            )
        except WorkspaceBoundaryError as exc:
            print(_format_cli_error(exc), file=sys.stderr)
            return 2
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
        worktree_name = args.worktree if args.worktree is not None else (args.task if _is_git_repo(root) else None)
        if worktree_name is not None:
            if not _is_git_repo(root):
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
            if worktree_name is None and _is_git_repo(root):
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
        Runner(
            run_root,
            run_dir=active.path,
            state_root=state_root,
            config_root=root,
            next_command=_continue_command(root, args.worktree),
        ).run(mode=ResumeMode.RESUME)
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
        meta = read_meta(active.path)
        if meta.get("execution") is not None and meta.get("workspace") is not None:
            execution = execution_identity_from_dict(meta["execution"])
            workspace = workspace_identity_from_dict(meta["workspace"])
            release_execution_binding(
                execution,
                git_common_dir=Path(workspace.git_common_dir),
                run_dir=active.path,
            )
        meta["status"] = "aborted"
        write_meta(active.path, meta)
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
        if args.full and args.files is not None:
            print("--full cannot be combined with --files", file=sys.stderr)
            return 2
        try:
            profile_ids = active_profile_ids(
                _profile_source_root(root, requested_root, getattr(args, "worktree", None)),
                args.profile,
            )
            changed_files = (
                _normalize_changed_files(command_root, args.files)
                if args.files is not None
                else _git_changed_files(command_root)
            )
            command_changed_files = None if args.full else changed_files
            commands = _profile_gate_commands(
                profile_ids,
                project_root=command_root,
                changed_files=command_changed_files,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        android_modules = (
            _android_changed_modules(command_root, changed_files)
            if "android" in profile_ids and changed_files
            else None
        )
        verification_mode = (
            "targeted"
            if not args.full and changed_files and android_modules is not None
            else "full"
        )
        run_dir = (
            _resolve_project_path(command_root, args.run_dir)
            if args.run_dir is not None
            else None
        )
        fingerprint = gate_execution_fingerprint(
            root=command_root,
            profile_ids=profile_ids,
            verification_mode=verification_mode,
            changed_files=changed_files,
            commands=commands,
        )
        reusable = (
            reusable_gate_results(
                run_dir=run_dir,
                root=command_root,
                commands=commands,
                fingerprint=fingerprint,
            )
            if run_dir is not None
            else {}
        )
        pending = [command for index, command in enumerate(commands) if index not in reusable]
        fresh = iter(run_gates(pending, cwd=command_root, timeout_s=args.timeout))
        results = [
            reusable[index] if index in reusable else next(fresh)
            for index in range(len(commands))
        ]
        post_changed_files = (
            _normalize_changed_files(command_root, args.files)
            if args.files is not None
            else _git_changed_files(command_root)
        )
        post_fingerprint = gate_execution_fingerprint(
            root=command_root,
            profile_ids=profile_ids,
            verification_mode=verification_mode,
            changed_files=post_changed_files,
            commands=commands,
        )
        if post_fingerprint.get("fingerprint_id") != fingerprint.get("fingerprint_id"):
            stability_command = GateCommand(
                "workspace-stability",
                ("agent-flow", "workspace-stability"),
            )
            stability_result = GateResult(
                gate_id=stability_command.gate_id,
                command=stability_command.command,
                passed=False,
                exit_code=1,
                stdout="",
                stderr="workspace inputs changed while gates were running",
                executed_at=datetime.now(timezone.utc).isoformat(),
            )
            unstable_commands = [*commands, stability_command]
            unstable_results = [*results, stability_result]
            unstable_fingerprint = gate_execution_fingerprint(
                root=command_root,
                profile_ids=profile_ids,
                verification_mode=verification_mode,
                changed_files=post_changed_files,
                commands=unstable_commands,
            )
            if run_dir is not None:
                write_gate_results(
                    run_dir=run_dir,
                    results=unstable_results,
                    commands=unstable_commands,
                    fingerprint=unstable_fingerprint,
                    verification_mode=verification_mode,
                )
            print("gate inputs changed while gates were running", file=sys.stderr)
            return 1
        fingerprint = post_fingerprint
        if run_dir is not None:
            write_gate_cache(
                run_dir=run_dir,
                root=command_root,
                commands=commands,
                results=results,
                fingerprint=fingerprint,
                reused_indices=set(reusable),
            )
            write_gate_results(
                run_dir=run_dir,
                results=results,
                commands=commands,
                fingerprint=fingerprint,
                verification_mode=verification_mode,
            )
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
            names = _known_worktree_names(root)
            if not names:
                print("no worktrees")
                return 0
            for name in names:
                try:
                    status = get_worktree_status(root=root, name=name)
                except ValueError:
                    path = root / ".agent-flow" / "worktrees" / name
                    print(f"{name} - {path} stale")
                else:
                    state = "exists" if _worktree_checkout_exists(status) else "stale"
                    print(f"{status.name} {status.branch} {status.path} {state}")
            return 0
        if args.worktree_command == "remove":
            try:
                status = get_worktree_status(root=root, name=args.name)
            except ValueError as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
            if not _worktree_checkout_exists(status):
                stale_dir = root / ".agent-flow" / "worktrees" / status.name
                if (
                    stale_dir.exists()
                    or stale_dir.is_symlink()
                    or status.name in _known_worktree_names(root)
                ):
                    stale_issue = _stale_worktree_removal_issue(
                        root,
                        stale_dir,
                        status.name,
                    )
                    if stale_issue is not None:
                        print(stale_issue, file=sys.stderr)
                        return 2
                    if (
                        not args.keep_branch
                        and status.branch_created_by_agent_flow
                        and worktree_branch_exists(root=root, branch=status.branch)
                        and preserved_worktree_branch_tip(
                            root=root,
                            branch=status.branch,
                        )
                        is None
                    ):
                        print(
                            "refusing to delete worktree branch with unpreserved commits: "
                            f"{status.branch}",
                            file=sys.stderr,
                        )
                        return 2
                    claim = None
                    quarantine: Path | None = None
                    try:
                        with worktree_lifecycle_lock(root=root):
                            claim = _acquire_authenticated_cleanup_claim(
                                root,
                                status.name,
                            )
                            try:
                                status = get_worktree_status(root=root, name=args.name)
                                if _worktree_checkout_exists(status):
                                    raise RuntimeError(
                                        "worktree became active during stale cleanup: "
                                        f"{status.path}"
                                    )
                                prune = run_safe_command(("git", "worktree", "prune"), cwd=root)
                                if not prune.ok:
                                    raise RuntimeError(_format_safe_command_error(prune))
                                branch_tip = None
                                if (
                                    not args.keep_branch
                                    and status.branch_created_by_agent_flow
                                    and worktree_branch_exists(root=root, branch=status.branch)
                                ):
                                    branch_tip = preserved_worktree_branch_tip(
                                        root=root,
                                        branch=status.branch,
                                    )
                                    if branch_tip is None:
                                        raise RuntimeError(
                                            "refusing to delete worktree branch with unpreserved commits: "
                                            f"{status.branch}"
                                        )
                                quarantine = _quarantine_stale_worktree_checkout(
                                    root,
                                    stale_dir,
                                    status.name,
                                )
                                if quarantine is not None:
                                    _remove_quarantined_stale_worktree_checkout(
                                        root,
                                        quarantine,
                                        status.name,
                                    )
                                    quarantine = None
                                if branch_tip is not None:
                                    delete_worktree_branch_at_tip(
                                        root=root,
                                        branch=status.branch,
                                        expected_tip=branch_tip,
                                    )
                                remove_worktree_metadata(root=root, name=status.name)
                            except Exception:
                                if (
                                    quarantine is not None
                                    and quarantine.exists()
                                    and not stale_dir.exists()
                                ):
                                    quarantine.rename(stale_dir)
                                raise
                            finally:
                                if claim is not None:
                                    release_workspace_start_claim(claim)
                                    claim = None
                    except (
                        OSError,
                        RuntimeError,
                        ValueError,
                        WorkspaceBoundaryError,
                        subprocess.CalledProcessError,
                    ) as exc:
                        print(_format_cli_error(exc), file=sys.stderr)
                        return 2
                    print(f"removed stale worktree manifest {status.name}")
                    return 0
                print(f"worktree not found or missing path: {status.name}", file=sys.stderr)
                return 1
            try:
                validate_worktree_removal(
                    root=root,
                    status=status,
                    delete_branch=not args.keep_branch,
                )
            except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
            claim = None
            try:
                claim = _acquire_authenticated_cleanup_claim(root, status.name)
                status = get_worktree_status(root=root, name=args.name)
                remove_worktree(
                    root=root,
                    status=status,
                    delete_branch=not args.keep_branch,
                )
            except (
                OSError,
                RuntimeError,
                ValueError,
                WorkspaceBoundaryError,
                subprocess.CalledProcessError,
            ) as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
            finally:
                if claim is not None:
                    release_workspace_start_claim(claim)
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
            claimed = claim_task(
                root=root,
                team_name=args.team,
                task_id=pending.task_id,
                worker_name=args.worker,
            )
            result = run_provider(
                ProviderCommand(name="host-command", argv=tuple(args.command_argv)),
                prompt=prompt,
                cwd=root,
            )
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
            print(f"{task.task_id} {task.status}")
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
        worktree_name = args.worktree if args.worktree is not None else (args.task if _is_git_repo(root) else None)
        if worktree_name is not None and not _is_git_repo(root):
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


def _profile_gate_commands(
    profile_ids: list[str],
    *,
    project_root: Path | None = None,
    changed_files: list[str] | None = None,
) -> list[GateCommand]:
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
            candidate_commands = (command,)
            if project_root is not None and changed_files is not None:
                candidate_commands = _incremental_profile_gate_commands(
                    profile.profile_id,
                    gate.gate_id,
                    command,
                    project_root,
                    changed_files,
                )
                if not candidate_commands:
                    continue
            required = gate.required
            gate_id = f"{profile.profile_id}:{gate.gate_id}" if multi_profile else gate.gate_id
            if multi_profile and _is_architecture_lint_gate(gate.gate_id, gate.command):
                if architecture_lint_added:
                    continue
                command = _architecture_lint_command(architecture_lint_profile)
                if project_root is not None and changed_files:
                    command = (*command, "--files", *changed_files)
                candidate_commands = (command,)
                gate_id = "architecture-lint"
                required = True
                architecture_lint_added = True
            multiple = len(candidate_commands) > 1
            for candidate in candidate_commands:
                if candidate in seen:
                    continue
                seen.add(candidate)
                candidate_gate_id = (
                    f"{gate_id}[{_incremental_gate_scope_label(candidate)}]"
                    if multiple
                    else gate_id
                )
                commands.append(
                    (order, GateCommand(candidate_gate_id, candidate, required=required))
                )
                order += 1
    return [
        command
        for _, command in sorted(
            commands,
            key=lambda item: (*_gate_order_key(item[1]), item[0]),
        )
    ]


def _incremental_profile_gate_commands(
    profile_id: str,
    gate_id: str,
    command: tuple[str, ...],
    project_root: Path,
    changed_files: list[str],
) -> tuple[tuple[str, ...], ...]:
    if profile_id != "android" or not changed_files:
        return (command,)
    modules = _android_changed_modules(project_root, changed_files)
    if _is_architecture_lint_gate(gate_id, command):
        return ((*command, "--files", *changed_files),)
    if modules is None:
        return (command,)
    if not modules:
        return ()
    if not command or Path(command[0]).name not in {"gradle", "gradlew"}:
        return (command,)
    tasks = command[1:]
    if not tasks or any(task.startswith("-") or ":" in task for task in tasks):
        return (command,)
    test_scope = _android_test_scope(project_root, changed_files)
    if gate_id == "build" and not test_scope["production"]:
        return ()
    if gate_id == "test":
        selected_commands: list[tuple[str, ...]] = []
        filters = _android_unit_test_filters(project_root, changed_files)
        for module in modules:
            if module in test_scope["production"] or module in test_scope["unit"]:
                if (
                    len(modules) == 1
                    and not test_scope["production"]
                    and not test_scope["instrumented"]
                    and filters
                ):
                    test_task = _android_unit_test_task(project_root, module)
                    if test_task is not None:
                        selected_commands.append(
                            (
                                command[0],
                                f"{module}:{test_task}",
                                *(value for pattern in filters for value in ("--tests", pattern)),
                            )
                        )
                    else:
                        selected_commands.append((command[0], f"{module}:{tasks[0]}"))
                else:
                    selected_commands.append((command[0], f"{module}:{tasks[0]}"))
            if module in test_scope["instrumented"]:
                selected_commands.append((command[0], f"{module}:connectedDevDebugAndroidTest"))
        return tuple(selected_commands)
    return tuple(
        (command[0], *(f"{module}:{task}" for task in tasks))
        for module in modules
    )


def _incremental_gate_scope_label(command: tuple[str, ...]) -> str:
    task = next((value for value in command[1:] if value.startswith(":")), "gate")
    return task.strip(":").replace(":", "/")


def _android_unit_test_task(project_root: Path, module: str) -> str | None:
    module_root = project_root.joinpath(*module.strip(":").split(":"))
    build_file = next(
        (
            candidate
            for candidate in (module_root / "build.gradle.kts", module_root / "build.gradle")
            if candidate.is_file()
        ),
        None,
    )
    if build_file is None:
        return None
    try:
        content = build_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    android_plugin = re.search(
        r"(?:com\.android\.(?:application|library|test|dynamic-feature)|"
        r"libs\.plugins\.android(?:[.\w-]*))",
        content,
    )
    if android_plugin:
        return "testDevDebugUnitTest"
    jvm_plugin = re.search(
        r"(?:org\.jetbrains\.kotlin\.jvm|kotlin\s*\(\s*['\"]jvm['\"]\s*\)|"
        r"libs\.plugins\.kotlin\.jvm|\bid\s*\(\s*['\"]java(?:-library)?['\"]\s*\))",
        content,
    )
    return "test" if jvm_plugin else None


def _android_changed_modules(
    project_root: Path,
    changed_files: list[str],
) -> tuple[str, ...] | None:
    if _has_gradle_project_dir_mapping(project_root):
        return None
    high_risk_roots = {
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "gradle.properties",
        "gradlew",
        "gradlew.bat",
    }
    high_risk_prefixes = ("buildSrc/", "build-logic/", "gradle/")
    documentation_suffixes = {".md", ".adoc", ".rst"}
    modules: set[str] = set()
    for relative in changed_files:
        normalized = relative.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized:
            continue
        if normalized in high_risk_roots or normalized.startswith(high_risk_prefixes):
            return None
        if Path(normalized).suffix.lower() in documentation_suffixes:
            continue
        module_root = _android_module_root(project_root, normalized)
        if module_root is None:
            return None
        module_relative = module_root.relative_to(project_root)
        modules.add(":" + ":".join(module_relative.parts))
    return tuple(sorted(modules))


def _has_gradle_project_dir_mapping(project_root: Path) -> bool:
    for settings in (project_root / "settings.gradle.kts", project_root / "settings.gradle"):
        if not settings.is_file():
            continue
        try:
            content = settings.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return True
        if re.search(
            r"\b(?:projectDir|setProjectDir|includeFlat|includeBuild)\b",
            content,
        ):
            return True
    return False


def _android_module_root(project_root: Path, relative: str) -> Path | None:
    candidate = project_root / relative
    parent = candidate if candidate.is_dir() else candidate.parent
    while parent != project_root and project_root in parent.parents:
        if (parent / "build.gradle").is_file() or (parent / "build.gradle.kts").is_file():
            return parent
        parent = parent.parent
    return None


def _android_test_scope(
    project_root: Path,
    changed_files: list[str],
) -> dict[str, set[str]]:
    scope = {
        "production": set(),
        "unit": set(),
        "instrumented": set(),
    }
    for relative in changed_files:
        if Path(relative).suffix.lower() in {".md", ".adoc", ".rst"}:
            continue
        module_root = _android_module_root(project_root, relative)
        if module_root is None:
            continue
        module = ":" + ":".join(module_root.relative_to(project_root).parts)
        normalized = f"/{relative.replace(os.sep, '/').lower()}/"
        if "/src/androidtest/" in normalized:
            scope["instrumented"].add(module)
        elif "/src/test/" in normalized:
            scope["unit"].add(module)
        else:
            scope["production"].add(module)
    return scope


def _android_unit_test_filters(
    project_root: Path,
    changed_files: list[str],
) -> tuple[str, ...]:
    filters: set[str] = set()
    for relative in changed_files:
        if Path(relative).suffix.lower() in {".md", ".adoc", ".rst"}:
            continue
        normalized = relative.replace("\\", "/")
        match = re.search(r"/src/test/(?:java|kotlin)/(.+)\.(?:java|kt)$", f"/{normalized}", re.IGNORECASE)
        path = project_root / relative
        if not match or not path.is_file():
            return ()
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return ()
        package_match = re.search(
            r"(?m)^\s*package\s+([A-Za-z_][\w.]*)\s*;?\s*$",
            content,
        )
        declarations = re.findall(
            r"\b(?:class|object|interface|enum\s+class|record)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            content,
        )
        class_name = path.stem
        if declarations != [class_name]:
            return ()
        package_name = package_match.group(1) if package_match else ""
        filters.add(f"{package_name + '.' if package_name else ''}{class_name}*")
    return tuple(sorted(filters))


def _normalize_changed_files(root: Path, values: list[str]) -> list[str]:
    normalized: set[str] = set()
    for value in values:
        candidate = Path(value)
        absolute = candidate if candidate.is_absolute() else root / candidate
        try:
            relative = absolute.resolve(strict=False).relative_to(root.resolve(strict=True))
        except ValueError as exc:
            raise ValueError(f"gate file is outside project root: {value}") from exc
        normalized.add(relative.as_posix())
    return sorted(normalized)


def _git_changed_files(root: Path) -> list[str] | None:
    probe = subprocess.run(
        ("git", "-C", str(root), "rev-parse", "--is-inside-work-tree"),
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return None
    commands: list[tuple[str, ...]] = [
        ("git", "-C", str(root), "diff", "--no-renames", "--name-only", "--diff-filter=ACMRD"),
        ("git", "-C", str(root), "diff", "--cached", "--no-renames", "--name-only", "--diff-filter=ACMRD"),
        ("git", "-C", str(root), "ls-files", "--others", "--exclude-standard"),
    ]
    for base in ("origin/main", "main"):
        merge_base = subprocess.run(
            ("git", "-C", str(root), "merge-base", "HEAD", base),
            text=True,
            capture_output=True,
            check=False,
        )
        if merge_base.returncode == 0 and merge_base.stdout.strip():
            commands.append(
                (
                    "git",
                    "-C",
                    str(root),
                    "diff",
                    "--no-renames",
                    "--name-only",
                    "--diff-filter=ACMRD",
                    f"{merge_base.stdout.strip()}..HEAD",
                )
            )
            break
    changed: set[str] = set()
    for command in commands:
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            return None
        changed.update(line.strip() for line in result.stdout.splitlines() if line.strip())
    return _normalize_changed_files(root, sorted(changed))


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
    stable_gate_id = gate_id.split("[", 1)[0].rsplit(":", 1)[-1].lower()
    if stable_gate_id in {"build", "android-build", "ios-build"}:
        return (0, _profile_gate_kind_tiebreaker(gate_id), gate_id)
    if stable_gate_id == "typecheck":
        return (1, _profile_gate_kind_tiebreaker(gate_id), gate_id)
    if stable_gate_id in {"lint", "architecture-lint"}:
        return (2, _profile_gate_kind_tiebreaker(gate_id), gate_id)
    if stable_gate_id == "test" or stable_gate_id.endswith("-test"):
        return (3, _profile_gate_kind_tiebreaker(gate_id), gate_id)
    command = " ".join(
        (Path(gate.command[0]).name, *gate.command[1:])
        if gate.command
        else ()
    ).lower()
    lowered = f"{gate_id} {command}".lower()
    if any(token in lowered for token in ("build", "assemble", "xcodebuild")):
        return (0, _profile_gate_kind_tiebreaker(lowered), gate_id)
    if any(token in lowered for token in ("typecheck", "tsc", "mypy", "pyright", "type ")):
        return (1, _profile_gate_kind_tiebreaker(lowered), gate_id)
    if any(token in lowered for token in ("lint", "ruff", "detekt", "ktlint", "architecture-lint")):
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
    configured = os.environ.get("AGENT_FLOW_PROJECT_LAUNCHER")
    launcher = configured if configured and Path(configured).is_absolute() else "agent-flow"
    command = f"{shlex.quote(launcher)} continue --root {shlex.quote(str(root))}"
    if worktree is None:
        return command
    return command + f" --worktree {shlex.quote(_slug_for_hint(root, worktree))}"


def _known_worktree_names(root: Path) -> list[str]:
    return known_worktree_names(root=root)


def _worktree_checkout_exists(status) -> bool:
    return status.exists and (status.path / ".git").exists()


def _acquire_authenticated_cleanup_claim(root: Path, name: str):
    execution = execution_identity_from_context(env=os.environ)
    finalizer = resolve_execution_finalizer_workspace(root, execution, name)
    claim = acquire_workspace_start_claim(
        finalizer.identity,
        run_id=f"cleanup:{name}",
    )
    try:
        current = resolve_execution_finalizer_workspace(root, execution, name)
        if current.identity != finalizer.identity or current.run_dir != finalizer.run_dir:
            raise WorkspaceBoundaryError(
                "execution_finalizer_stale: cleanup ownership changed while acquiring the workspace lease"
            )
    except Exception:
        release_workspace_start_claim(claim)
        raise
    return claim


def _stale_worktree_removal_issue(
    root: Path,
    stale_path: Path,
    name: str,
) -> str | None:
    if not stale_path.exists() and not stale_path.is_symlink():
        return None
    if stale_path.is_symlink() or not stale_path.is_dir():
        return f"refusing to delete unowned stale worktree path: {stale_path}"
    runtime_manifest = worktree_runtime_root(root=root, name=name) / "manifest.json"
    if not runtime_manifest.is_file() or runtime_manifest.is_symlink():
        return f"refusing to delete stale worktree without ownership metadata: {stale_path}"
    stale_manifest = stale_path / "manifest.json"
    if not stale_manifest.is_file() or stale_manifest.is_symlink():
        return f"refusing to delete stale worktree without an owned manifest file: {stale_path}"
    unexpected = sorted(
        entry.name
        for entry in stale_path.iterdir()
        if entry.name != "manifest.json"
    )
    if unexpected:
        return (
            "refusing to delete stale worktree with unowned files: "
            f"{stale_path} ({', '.join(unexpected)})"
        )
    try:
        if stale_manifest.read_bytes() != runtime_manifest.read_bytes():
            return f"refusing to delete stale worktree with unauthenticated manifest content: {stale_path}"
    except OSError:
        return f"refusing to delete stale worktree with unreadable ownership metadata: {stale_path}"
    return None


def _quarantine_stale_worktree_checkout(
    root: Path,
    stale_path: Path,
    name: str,
) -> Path | None:
    if not stale_path.exists() and not stale_path.is_symlink():
        return None
    issue = _stale_worktree_removal_issue(root, stale_path, name)
    if issue is not None:
        raise RuntimeError(issue)
    metadata = stale_path.lstat()
    stale_manifest = stale_path / "manifest.json"
    manifest_metadata = stale_manifest.lstat()
    manifest_bytes = stale_manifest.read_bytes()
    quarantine = stale_path.with_name(
        f".{stale_path.name}.cleanup-{os.getpid()}-{os.urandom(8).hex()}"
    )
    stale_path.rename(quarantine)
    moved = quarantine.lstat()
    moved_manifest = quarantine / "manifest.json"
    moved_manifest_metadata = moved_manifest.lstat()
    if (
        moved.st_dev != metadata.st_dev
        or moved.st_ino != metadata.st_ino
        or moved_manifest_metadata.st_dev != manifest_metadata.st_dev
        or moved_manifest_metadata.st_ino != manifest_metadata.st_ino
        or moved_manifest.read_bytes() != manifest_bytes
    ):
        if not stale_path.exists():
            quarantine.rename(stale_path)
        raise RuntimeError("stale worktree changed during cleanup quarantine")
    return quarantine


def _remove_quarantined_stale_worktree_checkout(
    root: Path,
    quarantine: Path,
    name: str,
) -> None:
    issue = _stale_worktree_removal_issue(root, quarantine, name)
    if issue is not None:
        raise RuntimeError(issue)
    manifest = quarantine / "manifest.json"
    metadata = manifest.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("stale worktree manifest is not an owned regular file")
    runtime_manifest = worktree_runtime_root(root=root, name=name) / "manifest.json"
    runtime_metadata = runtime_manifest.lstat()
    payload = runtime_manifest.read_bytes()
    removal = quarantine.with_name(
        f".{quarantine.name}.manifest.remove-{os.getpid()}-{os.urandom(8).hex()}"
    )
    manifest.rename(removal)
    moved_owned = False
    try:
        moved = removal.lstat()
        moved_owned = not (
            stat.S_ISLNK(moved.st_mode)
            or not stat.S_ISREG(moved.st_mode)
            or moved.st_dev != metadata.st_dev
            or moved.st_ino != metadata.st_ino
        )
        if (
            runtime_manifest.is_symlink()
            or not runtime_manifest.is_file()
            or runtime_manifest.lstat().st_dev != runtime_metadata.st_dev
            or runtime_manifest.lstat().st_ino != runtime_metadata.st_ino
            or not moved_owned
            or removal.read_bytes() != payload
        ):
            raise RuntimeError("stale worktree manifest changed before removal")
        quarantine.rmdir()
    except (OSError, RuntimeError):
        if removal.exists() or removal.is_symlink():
            if manifest.exists() or manifest.is_symlink():
                raced = quarantine / (
                    f".manifest.json.raced-{os.getpid()}-{os.urandom(8).hex()}"
                )
                manifest.rename(raced)
            if moved_owned:
                removal.rename(manifest)
            else:
                raced = quarantine / (
                    f".manifest.json.raced-{os.getpid()}-{os.urandom(8).hex()}"
                )
                removal.rename(raced)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(manifest, flags, metadata.st_mode & 0o777)
                try:
                    view = memoryview(payload)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise OSError("stale worktree manifest restore failed")
                        view = view[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        raise
    removal.unlink()


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


def _resolve_cli_root_context(
    root: Path,
    worktree: str | None,
    *,
    allow_unbound_execution: bool = False,
) -> tuple[Path, str | None]:
    managed = _managed_worktree_context(root)
    if managed is not None:
        leader_root, inferred_worktree = managed
        requested_worktree = worktree or inferred_worktree
        active = _active_workspace_for_cli(
            leader_root,
            requested_worktree=requested_worktree,
            allow_unbound_execution=allow_unbound_execution,
        )
        return leader_root, active.name if active is not None else requested_worktree
    cwd_managed = _managed_worktree_context(Path.cwd())
    if cwd_managed is not None and (_same_path(root, Path.cwd()) or _same_path(root, cwd_managed[0])):
        leader_root, inferred_worktree = cwd_managed
        requested_worktree = worktree or inferred_worktree
        active = _active_workspace_for_cli(
            leader_root,
            requested_worktree=requested_worktree,
            allow_unbound_execution=allow_unbound_execution,
        )
        return leader_root, active.name if active is not None else requested_worktree
    git_common_root = _git_common_worktree_root(root)
    if git_common_root is not None:
        active = _active_workspace_for_cli(
            git_common_root,
            requested_worktree=worktree,
            allow_unbound_execution=allow_unbound_execution,
        )
        if active is not None:
            return git_common_root, active.name
        return git_common_root, worktree
    if (root / ".git").exists():
        active = _active_workspace_for_cli(
            root,
            requested_worktree=worktree,
            allow_unbound_execution=allow_unbound_execution,
        )
        if active is not None:
            return root, active.name
    return root, worktree


def _active_workspace_for_cli(
    root: Path,
    *,
    requested_worktree: str | None = None,
    allow_unbound_execution: bool = False,
):
    active = find_active_pinned_workspaces(root)
    if not active:
        return None
    requested_name = (
        _slug_for_hint(root, requested_worktree)
        if requested_worktree is not None
        else None
    )
    execution = execution_identity_from_context(env=os.environ)
    if execution is not None:
        try:
            selected = select_execution_workspace(root, execution)
        except WorkspaceBoundaryError as exc:
            if allow_unbound_execution and str(exc).startswith("execution_binding_missing:"):
                for workspace in active:
                    payload = read_meta(workspace.run_dir).get("execution")
                    if payload is not None and execution_identity_from_dict(payload) == execution:
                        raise
                if requested_name is not None and any(
                    workspace.name == requested_name for workspace in active
                ):
                    raise WorkspaceBoundaryError(
                        "execution_binding_conflict: requested worktree "
                        f"{requested_name} belongs to another active execution"
                    ) from exc
                return None
            raise
        if requested_name is not None and selected.name != requested_name:
            raise WorkspaceBoundaryError(
                "execution_binding_conflict: requested worktree "
                f"{requested_name} differs from bound worktree {selected.name}"
            )
        return selected
    return select_execution_workspace(root, None)


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
    common_dir = run_safe_command(
        ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"),
        cwd=root,
    )
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


def _is_git_repo(root: Path) -> bool:
    result = run_safe_command(("git", "rev-parse", "--git-dir"), cwd=root, timeout_s=5)
    # git이 없거나 응답하지 않는 환경은 non-git 프로젝트처럼 처리해서 fallback을 살린다.
    return result.ok


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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
