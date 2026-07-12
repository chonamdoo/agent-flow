from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import stat
import sys
from dataclasses import dataclass
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
from agent_flow.core.execution_state_ledger import record_execution_state_usage
from agent_flow.core.host_bridge import ensure_worktree_host_bridge
from agent_flow.core.local_skills import local_skill_prompt_block
from agent_flow.core.phase_workflow import load_phase_workflow_definition
from agent_flow.core.profiles import (
    active_profile_ids,
    detect_profile,
    load_profile,
    load_project_profile_payload,
    primary_profile_id,
)
from agent_flow.core.skill_plan import (
    hash_skill_tree,
    installed_skill_plan_pin,
    profile_skill_prompt_block,
    runtime_changed_files,
)
from agent_flow.core.start_lock import assert_no_install_transaction, project_start_lock
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
    PROTECTED_WORKTREE_BRANCHES,
    WorktreeStatus,
    create_worktree,
    get_worktree_status,
    known_worktree_names,
    plan_fresh_worktree,
    plan_worktree,
    remove_worktree_metadata,
    remove_worktree,
    validate_worktree_identifier,
    worktree_branch_exists,
    worktree_runtime_root,
    write_worktree_manifest,
)
from agent_flow.core.state import (
    RunRequest,
    RunState,
    start_run,
    status_summary,
    verify_run_state_snapshots,
)
from agent_flow.core.workflow import load_workflow
from agent_flow.eval import run_eval
from agent_flow.memory.entities import EntityMemoryIndex
from agent_flow.artifact import (
    ActiveRun,
    ensure_run_skill_plan_pinned,
    find_active_run,
    mark_inactive,
)
from agent_flow.runner import Runner, ResumeMode, _find_kit_root
from agent_flow.providers.host import list_host_providers
from agent_flow.providers.subprocess import ProviderCommand, run_provider
from agent_flow.pr_watch import fetch_pr, watch_pr


class _ExactArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)


def _javascript_safe_integer(value: str) -> int:
    if re.fullmatch(r"[+-]?[0-9]+", value) is None:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}")
    parsed = int(value)
    if abs(parsed) > 9_007_199_254_740_991:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = _ExactArgumentParser(prog="agent-flow-python")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--root", default=".")

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("task")
    run_parser.add_argument("--root", default=".")
    run_parser.add_argument("--workflow", default="full-feature")
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
    continue_parser.add_argument("--approve-pause", action="store_true")

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
    gates_parser.add_argument("--timeout", type=_javascript_safe_integer, default=600)
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

    experiment_parser = subparsers.add_parser("experiment")
    experiment_subparsers = experiment_parser.add_subparsers(
        dest="experiment_command",
        required=True,
    )
    experiment_record_usage = experiment_subparsers.add_parser(
        "record-usage",
        description=(
            "Use --receipt with --receipt-sha256 for verified provider evidence. "
            "Direct numeric arguments are retained as summary-only measurements."
        ),
    )
    experiment_record_usage.add_argument("--run-dir", required=True)
    experiment_record_usage.add_argument("--event-id")
    experiment_record_usage.add_argument("--generated-at")
    experiment_record_usage.add_argument(
        "--scope",
        choices=("phase", "run-total"),
    )
    experiment_record_usage.add_argument("--phase-id")
    experiment_record_usage.add_argument("--round", type=_javascript_safe_integer)
    experiment_record_usage.add_argument("--model-id")
    experiment_record_usage.add_argument("--input-tokens", type=_javascript_safe_integer)
    experiment_record_usage.add_argument("--output-tokens", type=_javascript_safe_integer)
    experiment_record_usage.add_argument(
        "--additional-tokens",
        type=_javascript_safe_integer,
        help=(
            "condition-total additional input tokens caused by the experiment; "
            "include ledger prompts or the bounded inline action-self-review view"
        ),
    )
    experiment_record_usage.add_argument("--latency-ms", type=_javascript_safe_integer)
    experiment_record_usage.add_argument("--estimated-cost-usd")
    experiment_record_usage.add_argument(
        "--receipt",
        help=(
            "run-local immutable provider usage receipt JSON; when present, "
            "usage figures are derived from the receipt"
        ),
    )
    experiment_record_usage.add_argument(
        "--receipt-sha256",
        help="expected sha256 of --receipt (required for verified usage evidence)",
    )

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
    args._worktree_explicit = getattr(args, "worktree", None) is not None
    requested_root = Path(getattr(args, "root", ".")).resolve()
    root = requested_root
    root, inferred_worktree = _resolve_cli_root_context(root, getattr(args, "worktree", None))
    if inferred_worktree is not None and hasattr(args, "worktree") and args.worktree is None:
        args.worktree = inferred_worktree
    if hasattr(args, "worktree") and args.worktree is not None:
        try:
            validate_worktree_identifier(args.worktree)
        except ValueError as exc:
            print(_format_cli_error(exc), file=sys.stderr)
            return 2
    try:
        assert_no_install_transaction(root)
    except RuntimeError as exc:
        print(_format_cli_error(exc), file=sys.stderr)
        return 2

    if args.command == "init":
        init_project(root)
        print(f"initialized {root / '.agent-flow'}")
        return 0

    if args.command == "run":
        return _run_start_command(args, root=root, requested_root=requested_root)

    if args.command == "continue":
        try:
            run_root, state_root = _worktree_context(root, args.worktree) if args.worktree else (root, root)
        except (ValueError, RuntimeError) as exc:
            print(_format_cli_error(exc), file=sys.stderr)
            return 2
        if run_root is None:
            return 1
        try:
            active = find_active_run(state_root)
        except RuntimeError as exc:
            print(_format_cli_error(exc), file=sys.stderr)
            return 2
        if active is None:
            if args.approve_pause:
                print(
                    "blocked: --approve-pause requires an existing paused run",
                    file=sys.stderr,
                )
                return 2
            if args.worktree:
                print(
                    f'진행 중인 run 없음. `agent-flow-python run "<task>" '
                    f'--worktree "{_slug_for_hint(root, args.worktree)}"`로 시작하세요.'
                )
            else:
                print('진행 중인 run 없음. `agent-flow-python run "<task>"`로 시작하세요.')
            return 0
        try:
            _assert_required_workspace(
                root,
                run_root,
                _profile_worktree_mode(root, active_profile_ids(root)),
            )
        except (OSError, ValueError, RuntimeError) as exc:
            print(_format_cli_error(exc), file=sys.stderr)
            return 2
        try:
            Runner(
                run_root,
                run_dir=active.path,
                state_root=state_root,
                config_root=root,
                next_command=_continue_command(root, args.worktree),
            ).run(mode=ResumeMode.RESUME, approve_pause=args.approve_pause)
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
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
        try:
            active = find_active_run(state_root)
        except RuntimeError as exc:
            print(_format_cli_error(exc), file=sys.stderr)
            return 2
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
        active_worktree = args.worktree
        active_workspace = root
        if args.worktree:
            try:
                run_root, state_root = _worktree_context(root, args.worktree)
            except ValueError as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
            if run_root is None:
                return 1
            active_workspace = run_root
            try:
                active = find_active_run(state_root)
            except RuntimeError as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
        else:
            state_root = root
            try:
                project_active = _find_project_active_run(root)
                node_active = _find_node_active_run(root)
            except RuntimeError as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
            if project_active is not None and node_active is not None:
                print(
                    "blocked: multiple cross-runtime active runs found: "
                    f"{project_active[1].run_id}, {node_active.get('run_id', 'unknown')}",
                    file=sys.stderr,
                )
                return 2
            if project_active is None and node_active is not None:
                return _relay_node_status(root)
            if project_active is None:
                active = None
            else:
                active_worktree, active = project_active
                if active_worktree is not None:
                    active_workspace = get_worktree_status(
                        root=root,
                        name=active_worktree,
                    ).path
        if active is not None:
            try:
                _assert_required_workspace(
                    root,
                    active_workspace,
                    _profile_worktree_mode(root, active_profile_ids(root)),
                )
            except (OSError, ValueError, RuntimeError) as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
            try:
                active.print_status(
                    next_command=_continue_command(root, active_worktree),
                    config_root=root,
                    workspace_root=active_workspace,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
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
            profile_source_root = _profile_source_root(
                root,
                requested_root,
                getattr(args, "worktree", None),
            )
            _preflight_installed_skill_snapshot(profile_source_root)
            profile_ids = active_profile_ids(
                profile_source_root,
                args.profile,
            )
            commands = _profile_gate_commands(profile_ids)
        except (ValueError, RuntimeError) as exc:
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
            profile_source_root = _profile_source_root(
                root,
                requested_root,
                getattr(args, "worktree", None),
            )
            _preflight_installed_skill_snapshot(profile_source_root)
            profile_ids = active_profile_ids(
                profile_source_root,
                args.profile,
            )
            findings_by_profile = lint_profiles(command_root, profile_ids, files=args.files)
        except (ValueError, RuntimeError) as exc:
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

    if args.command == "experiment":
        if args.experiment_command == "record-usage":
            run_dir = _resolve_project_path(root, args.run_dir).resolve()
            try:
                config = _read_execution_ledger_config(run_dir)
                result = record_execution_state_usage(
                    run_dir=run_dir,
                    run_id=config["run_id"],
                    mode=config["mode"],
                    experiment_enabled=True,
                    event_id=args.event_id,
                    generated_at=args.generated_at,
                    scope=args.scope,
                    phase_id=args.phase_id,
                    round=args.round,
                    model_id=args.model_id,
                    input_tokens=args.input_tokens,
                    output_tokens=args.output_tokens,
                    additional_tokens=args.additional_tokens,
                    latency_ms=args.latency_ms,
                    estimated_cost_usd=args.estimated_cost_usd,
                    receipt_path=(
                        _resolve_project_path(root, args.receipt)
                        if args.receipt
                        else None
                    ),
                    receipt_sha256=args.receipt_sha256,
                )
            except (OSError, ValueError, RuntimeError, json.JSONDecodeError, KeyError) as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
            if result.get("ok") is not True:
                print(str(result.get("error") or "execution usage recording failed"), file=sys.stderr)
                return 2
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
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
                        "Preserve agent-flow-python status/next_command as workflow source of truth.",
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
                    "agent-flow-python record-stage --stage fix --status completed "
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
                validate_worktree_identifier(args.name)
                plan = plan_worktree(
                    root=root,
                    name=args.name,
                    branch=args.branch,
                    max_slug_length=_worktree_max_slug_length(root),
                )
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
                if stale_dir.exists() or status.name in _known_worktree_names(root):
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
                remove_worktree(root=root, status=status, delete_branch=not args.keep_branch)
            except subprocess.CalledProcessError as exc:
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
        return _compat_start_command(args, root=root, requested_root=requested_root)

    return 1


def _run_start_command(args, *, root: Path, requested_root: Path) -> int:
    profile_ids = active_profile_ids(root)
    profile_id = primary_profile_id(root)
    worktree_mode = _profile_worktree_mode(root, profile_ids)
    try:
        _assert_start_workspace_supported(root, worktree_mode)
        with project_start_lock(root, runtime="python"):
            return _run_start_command_locked(
                args,
                root=root,
                requested_root=requested_root,
                profile_id=profile_id,
                worktree_mode=worktree_mode,
            )
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(_format_cli_error(exc), file=sys.stderr)
        return 2


def _run_start_command_locked(
    args,
    *,
    root: Path,
    requested_root: Path,
    profile_id: str,
    worktree_mode: str,
) -> int:
    assert_no_install_transaction(root)
    run_root = root
    state_root = root
    worktree_status = None
    worktree_preexisting = False
    node_active = _find_node_active_run(root)
    if node_active is not None:
        print(f"already active: {node_active['run_id']} (task: {node_active.get('task', '')!r})")
        print("continue: ./.agent-flow/bin/agent-flow status")
        return 2
    existing = _find_project_active_run(root)
    if existing is not None:
        worktree, active = existing
        _verify_python_active_run_snapshot(root, active)
        print(f"already active: {active.run_id} (task: {active.task!r})")
        print(f"continue: {_continue_command(root, worktree)}")
        return 2
    _preflight_installed_skill_snapshot(root)
    try:
        current_checkout = _prepare_registered_checkout(
            root,
            requested_root,
            task=args.task,
            max_slug_length=_worktree_max_slug_length(root, profile_id),
        )
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(_format_cli_error(exc), file=sys.stderr)
        return 2
    worktree_name = args.worktree if args.worktree is not None else (args.task if _is_git_repo(root) else None)
    if worktree_name is not None:
        if not _is_git_repo(root):
            print("worktree runs require a git repository", file=sys.stderr)
            return 2
        try:
            plan = plan_fresh_worktree(
                root=root,
                name=worktree_name,
                branch=args.worktree_branch,
                max_slug_length=_worktree_max_slug_length(root, profile_id),
                base_ref=_worktree_base_ref(root, profile_id),
                reuse_registered=args._worktree_explicit,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if current_checkout is not None:
            checkout_path, checkout_branch, checkout_name = current_checkout
            worktree_status = WorktreeStatus(
                name=checkout_name,
                branch=checkout_branch,
                path=checkout_path,
                exists=True,
                branch_created_by_agent_flow=False,
                requested_name=args.task,
            )
            write_worktree_manifest(root=root, status=worktree_status)
            worktree_preexisting = True
        else:
            worktree_preexisting = plan.path.exists()
            try:
                worktree_status = create_worktree(
                    root=root,
                    plan=plan,
                    allow_dirty=args.allow_dirty or args.worktree is None,
                )
            except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
        state_root = worktree_runtime_root(root=root, name=worktree_status.name)
        try:
            _assert_required_workspace(root, worktree_status.path, worktree_mode)
            if (root / ".agent-flow" / "kit.json").is_file():
                ensure_worktree_host_bridge(
                    leader_root=root,
                    worktree_root=worktree_status.path,
                )
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            if not worktree_preexisting:
                _cleanup_worktree_after_failure(root, worktree_status, exc)
            else:
                print(_format_cli_error(exc), file=sys.stderr)
            return 2
        print(f"worktree: {worktree_status.name} {worktree_status.path}")
        run_root = worktree_status.path
        state_root = worktree_runtime_root(root=root, name=worktree_status.name)
        worktree_name = worktree_status.name
    else:
        try:
            _assert_required_workspace(root, root, worktree_mode)
        except RuntimeError as exc:
            print(_format_cli_error(exc), file=sys.stderr)
            return 2
    active = find_active_run(state_root)
    if active is not None:
        if worktree_status is not None and not worktree_preexisting:
            _cleanup_worktree_after_failure(
                root,
                worktree_status,
                RuntimeError(f"active run already exists: {active.run_id}"),
            )
        else:
            print(f"already active: {active.run_id} (task: {active.task!r})")
        return 2
    try:
        Runner(
            run_root,
            state_root=state_root,
            config_root=root,
            workflow=args.workflow,
            architecture=args.architecture,
            next_command=_continue_command(root, worktree_name),
            worktree_mode=worktree_mode,
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


def _compat_start_command(args, *, root: Path, requested_root: Path) -> int:
    try:
        profile_ids = active_profile_ids(root, args.profile)
        worktree_mode = _profile_worktree_mode(root, profile_ids)
        _assert_start_workspace_supported(root, worktree_mode)
        with project_start_lock(root, runtime="python"):
            return _compat_start_command_locked(
                args,
                root=root,
                requested_root=requested_root,
                profile_ids=profile_ids,
                worktree_mode=worktree_mode,
            )
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(_format_cli_error(exc), file=sys.stderr)
        return 2


def _compat_start_command_locked(
    args,
    *,
    root: Path,
    requested_root: Path,
    profile_ids: list[str],
    worktree_mode: str,
) -> int:
    assert_no_install_transaction(root)
    worktree = None
    worktree_status = None
    worktree_preexisting = False
    state = None
    node_active = _find_node_active_run(root)
    if node_active is not None:
        print(f"already active: {node_active['run_id']} (task: {node_active.get('task', '')!r})")
        print("continue: ./.agent-flow/bin/agent-flow status")
        return 2
    existing = _find_project_active_run(root)
    if existing is not None:
        active_worktree, active = existing
        _verify_python_active_run_snapshot(root, active)
        print(f"already active: {active.run_id} (task: {active.task!r})")
        print(f"continue: {_continue_command(root, active_worktree)}")
        return 2
    _preflight_installed_skill_snapshot(root)
    worktree_name = args.worktree if args.worktree is not None else (args.task if _is_git_repo(root) else None)
    if worktree_name is not None and not _is_git_repo(root):
        print("worktree runs require a git repository", file=sys.stderr)
        return 2
    try:
        workflow = load_workflow(args.workflow)
        profile = ",".join(profile_ids)
        branch_profile = primary_profile_id(root, args.profile)
        adapter = detect_adapter() if args.adapter == "auto" else args.adapter
        current_checkout = _prepare_registered_checkout(
            root,
            requested_root,
            task=args.task,
            max_slug_length=_worktree_max_slug_length(root, branch_profile),
        )
        if worktree_name is not None:
            plan = plan_fresh_worktree(
                root=root,
                name=worktree_name,
                branch=args.worktree_branch,
                max_slug_length=_worktree_max_slug_length(root, branch_profile),
                base_ref=_worktree_base_ref(root, branch_profile),
                reuse_registered=args._worktree_explicit,
            )
            if current_checkout is not None:
                checkout_path, checkout_branch, checkout_name = current_checkout
                status = WorktreeStatus(
                    name=checkout_name,
                    branch=checkout_branch,
                    path=checkout_path,
                    exists=True,
                    branch_created_by_agent_flow=False,
                    requested_name=args.task,
                )
                write_worktree_manifest(root=root, status=status)
                worktree_preexisting = True
            else:
                worktree_preexisting = plan.path.exists()
                status = create_worktree(
                    root=root,
                    plan=plan,
                    allow_dirty=args.allow_dirty or args.worktree is None,
                )
            worktree_status = status
            worktree_name = status.name
            _assert_required_workspace(root, status.path, worktree_mode)
            if (root / ".agent-flow" / "kit.json").is_file():
                ensure_worktree_host_bridge(
                    leader_root=root,
                    worktree_root=status.path,
                )
            worktree = {
                "name": worktree_name,
                "branch": status.branch,
                "path": str(status.path),
            }
        else:
            _assert_required_workspace(root, root, worktree_mode)
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
                config_root=root,
                worktree_mode=worktree_mode,
            ),
        )
        assert_no_install_transaction(root)
        _write_stage_prompts(
            root=state_root,
            config_root=root,
            project_root=worktree_status.path if worktree_status is not None else root,
            state=state,
            workflow=workflow,
        )
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


def _write_stage_prompts(
    *,
    root: Path,
    config_root: Path,
    project_root: Path,
    state: RunState,
    workflow,
) -> None:
    verify_run_state_snapshots(
        state_root=root,
        run_dir=state.run_dir,
        config_root=config_root,
        prompt_root=project_root,
    )
    for stage in workflow.stages:
        count = stage.replicas if stage.parallel else 1
        for replica in range(1, count + 1):
            prompt_id = stage.stage_id if count == 1 else f"{stage.stage_id}-{replica}"
            base_prompt = render_stage_prompt(
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
            )
            write_prompt(
                root=root,
                run_dir=state.run_dir,
                stage_id=prompt_id,
                content=(
                    base_prompt
                    + profile_skill_prompt_block(
                        config_root,
                        stage.stage_id,
                        project_root,
                        state.task,
                    )
                    + local_skill_prompt_block(
                        config_root,
                        stage.stage_id,
                        state.task,
                        runtime_changed_files(config_root, project_root),
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


def _read_execution_ledger_config(run_dir: Path) -> dict[str, str]:
    config_path = run_dir / "artifacts" / "execution-ledger" / "config.json"
    if config_path.is_symlink() or not config_path.is_file():
        raise RuntimeError(f"unsafe execution ledger config: {config_path}")
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError(f"invalid execution ledger config: {config_path}")
    run_id = payload.get("run_id")
    mode = payload.get("mode")
    if not isinstance(run_id, str) or not run_id or not isinstance(mode, str) or not mode:
        raise RuntimeError(f"invalid execution ledger config: {config_path}")
    return {"run_id": run_id, "mode": mode}


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
        try:
            python_active = _find_project_active_run(config_root)
            node_active = _find_node_active_run(config_root)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return None
        if python_active is not None and node_active is not None:
            print("blocked: Node and Python runs are active at the same time", file=sys.stderr)
            return None
        if python_active is not None:
            name, _active = python_active
            return config_root if name is None else _worktree_root(config_root, name)
        if node_active is not None:
            raw_workspace = node_active.get("workspace_root")
            if not isinstance(raw_workspace, str) or not raw_workspace:
                print("blocked: active Node run workspace_root is invalid", file=sys.stderr)
                return None
            workspace = Path(raw_workspace)
            if not workspace.is_absolute():
                workspace = config_root / workspace
            workspace = workspace.resolve()
            if _same_path(workspace, config_root):
                return config_root
            checkout = _registered_worktree_checkout(config_root, workspace)
            if checkout is None or not checkout[1] or checkout[1] in PROTECTED_WORKTREE_BRANCHES:
                print(
                    f"blocked: active Node run workspace_root is not a safe registered worktree: {workspace}",
                    file=sys.stderr,
                )
                return None
            return checkout[0]
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
    command = f"agent-flow-python continue --root {shlex.quote(str(root))}"
    if worktree is None:
        return command
    return command + f" --worktree {shlex.quote(_slug_for_hint(root, worktree))}"


def _known_worktree_names(root: Path) -> list[str]:
    return known_worktree_names(root=root)


@dataclass(frozen=True)
class _CompatibilityActiveRun:
    path: Path
    state_root: Path
    run_id: str
    workflow: str
    task: str
    started_at: str

    def print_status(self, **_kwargs: object) -> None:
        print(status_summary(self.state_root))


def _find_project_active_run(root: Path):
    matches: list[tuple[str | None, ActiveRun | _CompatibilityActiveRun]] = []
    runtime_state_root = worktree_runtime_root(root=root, name="_").parent
    state_roots = [(None, root)] + [
        (name, worktree_runtime_root(root=root, name=name))
        for name in _known_worktree_names(root)
    ]
    for name, state_root in state_roots:
        if name is not None:
            if not state_root.exists() and not state_root.is_symlink():
                continue
            if state_root.is_symlink() or not state_root.is_dir():
                raise RuntimeError(f"blocked: unsafe Python worktree state root: {state_root}")
            try:
                state_root.resolve().relative_to(runtime_state_root.resolve())
            except ValueError as exc:
                raise RuntimeError(
                    f"blocked: Python worktree state root escapes git-private storage: {state_root}"
                ) from exc
        legacy_active = find_active_run(state_root)
        if legacy_active is not None:
            matches.append((name, legacy_active))
        compatibility_active = _find_compatibility_active_run(
            root=root,
            state_root=state_root,
            worktree_name=name,
        )
        if compatibility_active is not None:
            matches.append((name, compatibility_active))
    if len(matches) > 1:
        labels = ", ".join(
            f"{name or 'leader'}:{active.run_id}"
            for name, active in matches
        )
        raise RuntimeError(
            f"blocked: multiple active Python runs found across project worktrees: {labels}"
        )
    return matches[0] if matches else None


def _find_compatibility_active_run(
    *,
    root: Path,
    state_root: Path,
    worktree_name: str | None,
) -> _CompatibilityActiveRun | None:
    agent_flow_root = state_root / ".agent-flow"
    if not agent_flow_root.exists() and not agent_flow_root.is_symlink():
        return None
    if agent_flow_root.is_symlink() or not agent_flow_root.is_dir():
        raise RuntimeError(f"blocked: unsafe Python state root: {agent_flow_root}")
    runs_root = agent_flow_root / "runs"
    if not runs_root.exists() and not runs_root.is_symlink():
        return None
    if runs_root.is_symlink() or not runs_root.is_dir():
        raise RuntimeError(f"blocked: unsafe Python runs root: {runs_root}")
    try:
        runs_root.resolve().relative_to(state_root.resolve())
    except ValueError as exc:
        raise RuntimeError(
            f"blocked: Python runs root escapes its state root: {runs_root}"
        ) from exc

    active: list[_CompatibilityActiveRun] = []
    for workflow_root in sorted(runs_root.iterdir(), key=lambda path: path.name):
        if workflow_root.is_symlink():
            raise RuntimeError(f"blocked: unsafe Python workflow run root: {workflow_root}")
        if not workflow_root.is_dir():
            continue
        for run_root in sorted(workflow_root.iterdir(), key=lambda path: path.name):
            if run_root.is_symlink():
                raise RuntimeError(f"blocked: unsafe Python run root: {run_root}")
            if not run_root.is_dir():
                continue
            manifest = run_root / "manifest.json"
            if not manifest.exists() and not manifest.is_symlink():
                continue
            payload = _read_compatibility_run_manifest(manifest)
            if "workflow_id" not in payload:
                if (
                    isinstance(payload.get("workflow"), str)
                    and payload["workflow"]
                ):
                    continue
                raise RuntimeError(f"blocked: invalid Python run manifest: {manifest}")
            workflow_id = payload.get("workflow_id")
            run_id = payload.get("run_id")
            status = payload.get("status")
            task = payload.get("task")
            created_at = payload.get("created_at")
            run_dir = payload.get("run_dir")
            expected_run_dir = Path(".agent-flow") / "runs" / workflow_root.name / run_root.name
            resolved_run_dir = (
                Path(run_dir) if isinstance(run_dir, str) and Path(run_dir).is_absolute()
                else state_root / str(run_dir or "")
            ).resolve()
            if (
                not isinstance(workflow_id, str)
                or not workflow_id
                or workflow_id != workflow_root.name
                or not isinstance(run_id, str)
                or not run_id
                or run_id != run_root.name
                or not isinstance(status, str)
                or not status
                or not isinstance(task, str)
                or not isinstance(created_at, str)
                or not created_at
                or not isinstance(run_dir, str)
                or not run_dir
                or Path(run_dir) != expected_run_dir
                or resolved_run_dir != run_root.resolve()
            ):
                raise RuntimeError(f"blocked: Python run manifest identity mismatch: {manifest}")
            if status in {"complete", "aborted"}:
                continue
            _validate_compatibility_worktree_pin(
                root=root,
                state_root=state_root,
                worktree_name=worktree_name,
                payload=payload,
                manifest=manifest,
            )
            active.append(
                _CompatibilityActiveRun(
                    path=run_root,
                    state_root=state_root,
                    run_id=run_id,
                    workflow=workflow_id,
                    task=task,
                    started_at=created_at,
                )
            )
    if len(active) > 1:
        names = ", ".join(item.run_id for item in active)
        raise RuntimeError(f"blocked: multiple Python active runs found: {names}")
    return active[0] if active else None


def _read_compatibility_run_manifest(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"blocked: unsafe Python run manifest: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"blocked: unreadable Python run manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"blocked: invalid Python run manifest: {path}")
    return payload


def _validate_compatibility_worktree_pin(
    *,
    root: Path,
    state_root: Path,
    worktree_name: str | None,
    payload: dict[str, object],
    manifest: Path,
) -> None:
    worktree = payload.get("worktree")
    if worktree_name is None:
        if worktree is not None:
            raise RuntimeError(f"blocked: Python run manifest worktree mismatch: {manifest}")
        return
    if not isinstance(worktree, dict):
        raise RuntimeError(f"blocked: Python run manifest is missing its worktree pin: {manifest}")
    branch = worktree.get("branch")
    if (
        worktree.get("name") != worktree_name
        or not isinstance(branch, str)
        or not branch
        or not isinstance(worktree.get("path"), str)
        or not worktree["path"]
        or state_root.resolve()
        != worktree_runtime_root(root=root, name=worktree_name).resolve()
    ):
        raise RuntimeError(f"blocked: Python run manifest worktree mismatch: {manifest}")

    worktree_manifest = state_root / "manifest.json"
    worktree_payload = _read_compatibility_run_manifest(worktree_manifest)
    workspace = worktree_payload.get("path")
    leader_root = worktree_payload.get("leader_root")
    workspace_path = (
        Path(workspace) if isinstance(workspace, str) and Path(workspace).is_absolute()
        else root / str(workspace or "")
    ).resolve()
    registered = _registered_worktree_checkout(root, workspace_path)
    if (
        worktree_payload.get("name") != worktree_name
        or worktree_payload.get("branch") != branch
        or not isinstance(workspace, str)
        or not workspace
        or not isinstance(leader_root, str)
        or Path(leader_root).resolve() != root.resolve()
        or registered is None
        or registered[1] != branch
    ):
        raise RuntimeError(f"blocked: Python run manifest worktree provenance mismatch: {manifest}")


def _preflight_installed_skill_snapshot(root: Path) -> None:
    kit_path = root / ".agent-flow" / "kit.json"
    index_path = root / ".agent-flow" / "skills" / "index.json"
    kit_present = kit_path.exists() or kit_path.is_symlink()
    index_present = index_path.exists() or index_path.is_symlink()
    if not kit_present and not index_present:
        return
    current_pin = installed_skill_plan_pin(root)
    if not current_pin:
        raise RuntimeError("blocked: installed skill snapshot is missing")
    python_active = _find_project_active_run(root)
    if python_active is not None:
        _verify_python_active_run_snapshot(root, python_active[1])
    node_active = _find_node_active_run(root)
    if node_active is None:
        return
    for key in (
        "skill_plan_hash",
        "skill_plan_hash_version",
        "local_skill_plan_hash",
        "local_skill_plan_hash_version",
    ):
        if node_active.get(key) != current_pin.get(key):
            raise RuntimeError(
                "blocked: active Node run skill plan pin does not match the installed snapshot"
            )


def _verify_python_active_run_snapshot(
    root: Path,
    active: ActiveRun | _CompatibilityActiveRun,
) -> None:
    if isinstance(active, _CompatibilityActiveRun):
        verify_run_state_snapshots(
            state_root=active.state_root,
            run_dir=active.path,
            config_root=root,
        )
        return
    ensure_run_skill_plan_pinned(active.path, root)


def _find_node_active_run(root: Path) -> dict[str, object] | None:
    active: list[dict[str, object]] = []
    runtime_root = worktree_runtime_root(root=root, name="_").parent
    state_roots = [root, *(worktree_runtime_root(root=root, name=name) for name in _known_worktree_names(root))]
    for state_root in state_roots:
        if state_root != root:
            if not state_root.exists() and not state_root.is_symlink():
                continue
            if state_root.is_symlink() or not state_root.is_dir():
                raise RuntimeError(f"blocked: unsafe Node worktree state root: {state_root}")
            try:
                state_root.resolve().relative_to(runtime_root.resolve())
            except ValueError as exc:
                raise RuntimeError(
                    f"blocked: Node worktree state root escapes git-private storage: {state_root}"
                ) from exc
        agent_flow_root = state_root / ".agent-flow"
        if not agent_flow_root.exists() and not agent_flow_root.is_symlink():
            continue
        if agent_flow_root.is_symlink() or not agent_flow_root.is_dir():
            raise RuntimeError(f"blocked: unsafe Node state root: {agent_flow_root}")
        runs_root = agent_flow_root / "runs"
        if not runs_root.exists() and not runs_root.is_symlink():
            continue
        if runs_root.is_symlink() or not runs_root.is_dir():
            raise RuntimeError(f"blocked: unsafe Node runs root: {runs_root}")
        try:
            runs_root.resolve().relative_to(state_root.resolve())
        except ValueError as exc:
            raise RuntimeError(f"blocked: Node runs root escapes its state root: {runs_root}") from exc
        for workflow_root in sorted(runs_root.iterdir(), key=lambda path: path.name):
            if workflow_root.is_symlink():
                raise RuntimeError(f"blocked: unsafe Node workflow run root: {workflow_root}")
            if not workflow_root.is_dir():
                continue
            for run_root in sorted(workflow_root.iterdir(), key=lambda path: path.name):
                if run_root.is_symlink():
                    raise RuntimeError(f"blocked: unsafe Node run root: {run_root}")
                if not run_root.is_dir():
                    continue
                manifest = run_root / "manifest.json"
                if not manifest.exists() and not manifest.is_symlink():
                    continue
                if manifest.is_symlink() or not manifest.is_file():
                    raise RuntimeError(f"blocked: unsafe Node run manifest: {manifest}")
                try:
                    payload = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise RuntimeError(
                        f"blocked: unreadable Node run manifest {manifest}: {exc}"
                    ) from exc
                if not isinstance(payload, dict):
                    raise RuntimeError(f"blocked: invalid Node run manifest: {manifest}")
                if "workflow" not in payload and "workflow_id" in payload:
                    continue
                workflow = workflow_root.name
                run_id = run_root.name
                run_dir = payload.get("run_dir")
                resolved_run_dir = (
                    Path(run_dir).resolve()
                    if isinstance(run_dir, str) and Path(run_dir).is_absolute()
                    else (state_root / str(run_dir or "")).resolve()
                )
                if (
                    payload.get("workflow") != workflow
                    or payload.get("run_id") != run_id
                    or not isinstance(run_dir, str)
                    or not run_dir
                    or resolved_run_dir != run_root.resolve()
                    or not isinstance(payload.get("status"), str)
                ):
                    raise RuntimeError(f"blocked: Node run manifest identity mismatch: {manifest}")
                if payload["status"] in {"complete", "aborted"} or payload.get("phase") == "complete":
                    continue
                active.append({**payload, "run_dir": str(resolved_run_dir)})
    if len(active) > 1:
        names = ", ".join(str(payload.get("run_id", "unknown")) for payload in active)
        raise RuntimeError(f"blocked: multiple Node active runs found: {names}")
    return active[0] if active else None


def _relay_node_status(root: Path) -> int:
    node = shutil.which("node")
    try:
        cli = _pinned_node_status_cli(root)
    except (OSError, ValueError, RuntimeError) as exc:
        print(_format_cli_error(exc), file=sys.stderr)
        return 2
    if node is None:
        print(
            "blocked: a Node run is active but the Node executable is unavailable",
            file=sys.stderr,
        )
        return 2
    result = subprocess.run(
        (node, str(cli), "status"),
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "AGENT_FLOW_STATUS_RELAY": "python-to-node"},
    )
    if result.returncode != 0:
        print(
            (result.stderr or result.stdout or "Node status relay failed").strip(),
            file=sys.stderr,
        )
        return result.returncode or 2
    print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    return 0


def _pinned_node_status_cli(root: Path) -> Path:
    kit_path = root / ".agent-flow" / "kit.json"
    if not _runtime_path_is_regular(root, kit_path):
        raise RuntimeError(
            "blocked: a Node run is active but installed kit metadata is unavailable"
        )
    try:
        kit = json.loads(kit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("blocked: installed kit metadata is unreadable") from exc
    if not isinstance(kit, dict):
        raise RuntimeError("blocked: installed kit metadata is invalid")
    contract = kit.get("project_runtime_contract")
    if not isinstance(contract, dict) or set(contract) != {
        "version", "launcher", "node_runtime", "python_runtime"
    }:
        raise RuntimeError("blocked: installed project runtime contract is invalid")
    launcher = contract.get("launcher")
    node_runtime = contract.get("node_runtime")
    python_runtime = contract.get("python_runtime")
    is_sha256 = lambda value: isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )
    if (
        contract.get("version") != 2
        or not isinstance(launcher, dict)
        or set(launcher) != {"path", "sha256"}
        or launcher.get("path") != ".agent-flow/bin/agent-flow"
        or not is_sha256(launcher.get("sha256"))
        or not isinstance(node_runtime, dict)
        or set(node_runtime) != {"root", "entrypoint", "tree_hash"}
        or node_runtime.get("root") != ".agent-flow/runtime/node"
        or node_runtime.get("entrypoint")
        != ".agent-flow/runtime/node/bin/agent-flow-kit.mjs"
        or not is_sha256(node_runtime.get("tree_hash"))
        or not isinstance(python_runtime, dict)
        or set(python_runtime) != {"root", "tree_hash"}
        or python_runtime.get("root") != ".agent-flow/runtime/python"
        or not is_sha256(python_runtime.get("tree_hash"))
    ):
        raise RuntimeError("blocked: installed project runtime contract is invalid")
    normalized = {
        "version": 2,
        "launcher": {
            "path": ".agent-flow/bin/agent-flow",
            "sha256": launcher["sha256"],
        },
        "node_runtime": {
            "root": ".agent-flow/runtime/node",
            "entrypoint": ".agent-flow/runtime/node/bin/agent-flow-kit.mjs",
            "tree_hash": node_runtime["tree_hash"],
        },
        "python_runtime": {
            "root": ".agent-flow/runtime/python",
            "tree_hash": python_runtime["tree_hash"],
        },
    }
    encoded = json.dumps(
        {"version": 2, "contract": normalized},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if (
        kit.get("project_runtime_contract_commitment_version") != 2
        or not is_sha256(kit.get("project_runtime_contract_commitment"))
        or hashlib.sha256(encoded).hexdigest()
        != kit["project_runtime_contract_commitment"]
    ):
        raise RuntimeError("blocked: installed project runtime commitment is invalid")
    if kit.get("node_runtime") != {
        "path": node_runtime["entrypoint"],
        "tree_hash": node_runtime["tree_hash"],
    } or kit.get("python_runtime") != {
        "path": python_runtime["root"],
        "tree_hash": python_runtime["tree_hash"],
    }:
        raise RuntimeError("blocked: installed runtime compatibility metadata is invalid")
    launcher_path = _require_pinned_runtime_path(
        root, launcher["path"], "file", "pinned project launcher"
    )
    if not os.access(launcher_path, os.X_OK):
        raise RuntimeError("blocked: pinned project launcher is not executable")
    if hashlib.sha256(launcher_path.read_bytes()).hexdigest() != launcher["sha256"]:
        raise RuntimeError("blocked: pinned project launcher changed after install")
    node_root = _require_pinned_runtime_path(
        root, node_runtime["root"], "directory", "pinned Node runtime root"
    )
    cli = _require_pinned_runtime_path(
        root, node_runtime["entrypoint"], "file", "pinned Node runtime entrypoint"
    )
    if _hash_pinned_runtime_tree(node_root, "pinned Node runtime") != node_runtime["tree_hash"]:
        raise RuntimeError("blocked: pinned Node runtime changed after install")
    python_root = _require_pinned_runtime_path(
        root, python_runtime["root"], "directory", "pinned Python runtime root"
    )
    if _hash_pinned_runtime_tree(python_root, "pinned Python runtime") != python_runtime["tree_hash"]:
        raise RuntimeError("blocked: pinned Python runtime changed after install")
    return cli


def _require_pinned_runtime_path(root: Path, relative: str, kind: str, label: str) -> Path:
    cursor = root.resolve()
    parts = relative.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise RuntimeError(f"blocked: {label} path is invalid")
    for index, part in enumerate(parts):
        cursor /= part
        try:
            metadata = cursor.lstat()
        except OSError as exc:
            raise RuntimeError(f"blocked: {label} is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError(f"blocked: {label} path may not use symlinks")
        final = index == len(parts) - 1
        valid = stat.S_ISREG(metadata.st_mode) if final and kind == "file" else stat.S_ISDIR(metadata.st_mode)
        if not valid:
            raise RuntimeError(f"blocked: {label} path has an invalid component")
        if final and kind == "file" and metadata.st_nlink != 1:
            raise RuntimeError(f"blocked: {label} may not be hard-linked")
    return cursor


def _hash_pinned_runtime_tree(root: Path, label: str) -> str:
    files: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(directory.iterdir())
        except OSError as exc:
            raise RuntimeError(f"blocked: {label} is unreadable") from exc
        for entry in entries:
            metadata = entry.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise RuntimeError(f"blocked: {label} may not contain symlinks")
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(entry)
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise RuntimeError(f"blocked: {label} may not contain hard-linked files")
                files.append(entry)
            else:
                raise RuntimeError(f"blocked: {label} may contain only regular files")
    digest = hashlib.sha256()
    for file in sorted(files, key=lambda candidate: candidate.as_posix()):
        digest.update(file.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(file.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _runtime_path_is_regular(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    cursor = root
    for index, part in enumerate(relative.parts):
        cursor /= part
        if cursor.is_symlink():
            return False
        if index == len(relative.parts) - 1:
            return cursor.is_file()
        if not cursor.is_dir():
            return False
    return False


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
    retry_command = f"agent-flow-python review retry --reviewer {shlex.quote(reviewer)}"
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
        return plan_worktree(
            root=root,
            name=value,
            max_slug_length=_worktree_max_slug_length(root),
        ).name
    except ValueError:
        return value


def _worktree_max_slug_length(root: Path, profile: str | None = None) -> int:
    profile_id = primary_profile_id(root) if profile in {None, "auto"} else profile
    try:
        payload = load_project_profile_payload(root, profile_id)
    except (OSError, ValueError):
        return 60
    branching = payload.get("branching") if isinstance(payload, dict) else None
    naming = branching.get("naming") if isinstance(branching, dict) else None
    value = naming.get("max_slug_length") if isinstance(naming, dict) else None
    return value if isinstance(value, int) and 12 <= value <= 100 else 60


def _worktree_base_ref(root: Path, profile: str | None = None) -> str | None:
    profile_id = primary_profile_id(root) if profile in {None, "auto"} else profile
    try:
        payload = load_project_profile_payload(root, profile_id)
    except (OSError, ValueError):
        return None
    branching = payload.get("branching") if isinstance(payload, dict) else None
    value = branching.get("base") if isinstance(branching, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def _profile_worktree_mode(root: Path, profile_ids: list[str]) -> str:
    del profile_ids
    profile_id = primary_profile_id(root)
    payload = load_project_profile_payload(root, profile_id)
    branching = payload.get("branching") if isinstance(payload, dict) else None
    mode = branching.get("worktree") if isinstance(branching, dict) else None
    return "disabled" if mode == "disabled" else "required"


def _assert_start_workspace_supported(root: Path, worktree_mode: str) -> None:
    if worktree_mode != "disabled" and not _is_git_repo(root):
        raise RuntimeError(
            "blocked: active profile requires a registered git worktree, "
            "but the project is not a git repository"
        )


def _assert_required_workspace(root: Path, workspace: Path, worktree_mode: str) -> None:
    if worktree_mode == "disabled":
        return
    if not _is_git_repo(root):
        raise RuntimeError(
            "blocked: active profile requires a registered git worktree, "
            "but the project is not a git repository"
        )
    if _same_path(root, workspace):
        raise RuntimeError(
            "blocked: active run workspace_root is the leader checkout while "
            "the profile requires a worktree"
        )
    checkout = _registered_worktree_checkout(root, workspace)
    if checkout is None:
        raise RuntimeError(
            f"blocked: active run workspace_root is not a registered git worktree: {workspace}"
        )
    _, branch = checkout
    if not branch:
        raise RuntimeError(f"blocked: active run workspace_root uses detached HEAD: {workspace}")
    if branch in PROTECTED_WORKTREE_BRANCHES:
        raise RuntimeError(f"blocked: active run workspace_root uses protected branch {branch}")


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
        checkout = _registered_worktree_checkout(git_common_root, root)
        if checkout is not None:
            checkout_path, branch = checkout
            manifest_name = _worktree_name_for_checkout(git_common_root, checkout_path)
            if manifest_name is not None:
                return git_common_root, worktree or manifest_name
            if branch.startswith("feat/"):
                inferred = plan_worktree(
                    root=git_common_root,
                    name=branch.removeprefix("feat/"),
                    max_slug_length=_worktree_max_slug_length(git_common_root),
                ).name
                return git_common_root, worktree or inferred
        return git_common_root, worktree
    return root, worktree


def _managed_worktree_context(path: Path) -> tuple[Path, str] | None:
    resolved = path.resolve()
    parts = resolved.parts
    markers = {".agent-flow", ".codex", ".Codex", ".claude", ".omp"}
    for index in range(len(parts) - 2, 0, -1):
        if parts[index] not in markers or parts[index + 1] != "worktrees":
            continue
        root = Path(*parts[:index])
        if parts[index] in {".codex", ".Codex", ".claude", ".omp"} and _same_path(root, _home_path()):
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


def _registered_worktree_checkout(root: Path, candidate: Path) -> tuple[Path, str] | None:
    top = run_safe_command(("git", "rev-parse", "--show-toplevel"), cwd=candidate)
    branch = run_safe_command(("git", "branch", "--show-current"), cwd=candidate)
    worktrees = run_safe_command(("git", "worktree", "list", "--porcelain"), cwd=root)
    if not top.ok or not branch.ok or not worktrees.ok:
        return None
    checkout = Path(top.stdout.strip()).resolve()
    branch_name = branch.stdout.strip()
    if _same_path(checkout, root):
        return None
    registered = any(
        block.startswith(f"worktree {checkout}\n")
        for block in worktrees.stdout.split("\n\n")
    )
    return (checkout, branch_name) if registered else None


def _prepare_registered_checkout(
    root: Path,
    candidate: Path,
    *,
    task: str,
    max_slug_length: int,
) -> tuple[Path, str, str] | None:
    checkout = _registered_worktree_checkout(root, candidate)
    if checkout is None:
        return None
    checkout_path, branch = checkout
    if branch in PROTECTED_WORKTREE_BRANCHES:
        raise ValueError(f"registered worktree uses protected branch: {branch}")
    if branch:
        if _workspace_has_run_history(root, checkout_path):
            return None
        checkout_name = _worktree_name_for_checkout(root, checkout_path)
        if checkout_name is None:
            seed = branch.removeprefix("feat/") if branch.startswith("feat/") else checkout_path.name
            checkout_name = _available_runtime_worktree_name(
                root,
                seed or task,
                max_slug_length=max_slug_length,
            )
        return checkout_path, branch, checkout_name
    plan = plan_fresh_worktree(
        root=root,
        name=task,
        max_slug_length=max_slug_length,
    )
    switch_args = ("git", "switch", "-c", plan.branch)
    result = run_safe_command(switch_args, cwd=checkout_path)
    if not result.ok:
        raise RuntimeError(_format_safe_command_error(result))
    return checkout_path, plan.branch, plan.name


def _workspace_has_run_history(root: Path, checkout: Path) -> bool:
    for manifest in sorted((root / ".agent-flow" / "runs").glob("*/*/manifest.json")):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw_workspace = payload.get("workspace_root") if isinstance(payload, dict) else None
        if not isinstance(raw_workspace, str) or not raw_workspace:
            continue
        workspace = Path(raw_workspace)
        if not workspace.is_absolute():
            workspace = root / workspace
        if _same_path(workspace, checkout):
            return True

    checkout_name = _worktree_name_for_checkout(root, checkout)
    if checkout_name is None:
        return False
    runs_root = worktree_runtime_root(root=root, name=checkout_name) / ".agent-flow" / "runs"
    if not runs_root.exists() or runs_root.is_symlink() or not runs_root.is_dir():
        return False
    return any(
        candidate.is_file() and not candidate.is_symlink()
        for pattern in ("*/meta.json", "*/*/manifest.json")
        for candidate in runs_root.glob(pattern)
    )


def _available_runtime_worktree_name(
    root: Path,
    seed: str,
    *,
    max_slug_length: int,
) -> str:
    initial = plan_worktree(
        root=root,
        name=seed,
        max_slug_length=max_slug_length,
    )
    base_slug = initial.name.removeprefix("feat-")
    limit = max(12, min(int(max_slug_length), 100))
    known = set(_known_worktree_names(root))
    counter = 1
    while True:
        suffix = "" if counter == 1 else f"-{counter}"
        stem = base_slug[: max(1, limit - len(suffix))].rstrip("-") or "task"
        name = f"feat-{stem}{suffix}"
        runtime_root = worktree_runtime_root(root=root, name=name)
        if name not in known and not runtime_root.exists() and not runtime_root.is_symlink():
            return name
        counter += 1


def _worktree_name_for_checkout(root: Path, checkout: Path) -> str | None:
    for name in _known_worktree_names(root):
        try:
            status = get_worktree_status(root=root, name=name)
        except ValueError:
            continue
        if _same_path(status.path, checkout):
            return status.name
    return None


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
