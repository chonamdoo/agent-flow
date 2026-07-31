from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

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
from agent_flow.core.architecture_lint import main as architecture_lint_main
from agent_flow.core.context_contract import (
    append_context_event,
    check_system_invariants,
    ensure_context_contract,
    offload_tool_output,
    write_system_invariants,
)
from agent_flow.core.design_ledger import (
    LEDGER_SOURCE_PHASES,
    SPEC_SET_USER_REPLY,
    prepare_and_attest_user_spec_confirmation,
    capture_design_ledger,
    ledger_prompt_block,
    manual_spec_approval_statement,
    parse_spec_item_section,
    record_manual_spec_approval,
    prepare_user_spec_confirmation,
    record_spec_set_confirmation,
    spec_set_confirmation_statement,
    spec_set_is_confirmed,
)
from agent_flow.core.design_value_check import missing_spec_item_evidence
from agent_flow.core.gates import GateCommand, run_gates
from agent_flow.core.kit_digest import warn_if_installed_kit_is_stale
from agent_flow.core.phase_workflow import (
    DeclaredPhaseSkills,
    declared_phase_skills,
    load_phase_workflow_definition,
)
from agent_flow.core.profiles import (
    DEFAULT_GATE_PHASE,
    GATE_PHASE_ALL,
    GATE_PHASES,
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
    resolved_profile,
)
from agent_flow.core import skill_catalog
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
    WorktreeLockedError,
    WorktreeAlreadyExistsError,
    WorktreeStatus,
    adopt_worktree,
    assert_worktree_unlocked,
    attach_worktree,
    create_worktree,
    get_worktree_status,
    cleanup_state_root,
    UnknownWorktreeSetupAction,
    WORKTREE_SETUP_ACTIONS,
    copy_declared_worktree_files,
    DEFAULT_SLUG_MAX_LENGTH,
    describe_slug,
    delegated_slug,
    run_declared_worktree_actions,
    find_pending_worktree_cleanup,
    existing_checkout_path,
    known_worktree_names,
    legacy_managed_root,
    managed_worktrees_root,
    plan_worktree,
    provision_host_hook_registrations,
    remove_worktree_metadata,
    remove_worktree,
    removable_worktrees,
    resolve_worktree,
    worktree_branch_exists,
    worktree_runtime_root,
    worktree_run_activation,
)
from agent_flow.core.host_write_boundary import (
    HostCheckoutBinding,
    HostWriteBoundaryError,
    bound_worktree_for_session,
)
from agent_flow.core.hook_integrity import (
    HookIntegrityError,
    assert_managed_hooks_registered,
)
from agent_flow.core.host_write_boundary import assert_adoption_allowed
from agent_flow.core.worktree_isolation import (
    WorktreeIsolationError,
    adopted_worktree_parent,
    assert_cwd_bound,
    assert_leader_unchanged,
    capture_leader_snapshot,
    git_common_dir,
    git_repo_state,
    git_safe,
    git_toplevel,
    leader_root_for,
    provider_lease,
    registered_worktree_at,
    same_worktree_path,
    verify_linked_worktree,
    worker_claim_lock,
    worktree_path_key,
)
from agent_flow.core.state import RunRequest, RunState, start_run, status_summary
from agent_flow.core.workflow import load_workflow
from agent_flow.eval import run_eval
from agent_flow.memory.entities import EntityMemoryIndex
from agent_flow.artifact import find_active_run, find_active_runs, mark_inactive, read_meta
from agent_flow.runner import Runner, ResumeMode, _find_kit_root, _load_profile
from agent_flow.providers.host import list_host_providers
from agent_flow.providers.subprocess import (
    ProviderCommand,
    run_provider,
    verify_provider_sandbox_backend,
)
from agent_flow.pr_watch import fetch_pr, watch_pr


# 사용자가 워크플로를 몰기 위해 직접 치는 명령. 래퍼가 위임하는 하위 명령
# (`skills markers`, `spec prompt` 등)까지 넣으면 한 phase에 경고가 여러 번 뜬다.
_KIT_FRESHNESS_COMMANDS = frozenset({"run", "start", "status", "continue"})


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
        "--reuse-existing-worktree",
        action="store_true",
        help="reuse the managed worktree inferred from the current directory",
    )
    run_parser.add_argument(
        "--architecture",
        choices=("default", "ddd", "service-layer"),
        default="default",
    )

    continue_parser = subparsers.add_parser("continue")
    continue_parser.add_argument("--root", default=".")
    continue_parser.add_argument("--worktree")
    continue_parser.add_argument("--checkout-identity", help=argparse.SUPPRESS)

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
    start_parser.add_argument(
        "--reuse-existing-worktree",
        action="store_true",
        help="reuse the managed worktree inferred from the current directory",
    )
    start_parser.add_argument("--phase-runner", action="store_true", help=argparse.SUPPRESS)
    start_parser.add_argument("--checkout-identity", help=argparse.SUPPRESS)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--root", default=".")
    status_parser.add_argument("--worktree")
    status_parser.add_argument("--checkout-identity", help=argparse.SUPPRESS)

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
    gates_parser.add_argument(
        "--phase",
        default=DEFAULT_GATE_PHASE,
        choices=(*GATE_PHASES, GATE_PHASE_ALL),
    )

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
    for name in ("sync", "resolve", "prompt", "markers", "scan", "doctor"):
        sub = skills_subparsers.add_parser(name)
        sub.add_argument("--root", default=".")
        sub.add_argument("--profile")
        if name == "sync":
            # 갱신 경로가 없으면 움직이는 ref가 최초 1회 받은 커밋에 영구히 굳는다.
            sub.add_argument("--refresh", action="store_true")
        if name in {"resolve", "prompt", "markers"}:
            sub.add_argument("--phase", required=True)
            sub.add_argument("--workflow", default="default")
        if name == "markers":
            sub.add_argument("--artifact", required=True)
            # 읽음 증거를 현재 phase로 한정한다. 없으면 과거 기록까지 인정돼 강제가 약해진다.
            sub.add_argument("--since", type=float, default=None)
        if name == "scan":
            sub.add_argument("--no-write", action="store_true")


    spec_parser = subparsers.add_parser("spec")
    spec_subparsers = spec_parser.add_subparsers(dest="spec_command", required=True)
    spec_approve = spec_subparsers.add_parser("approve")
    spec_approve.add_argument("spec_id")
    spec_approve.add_argument("--run-dir", required=True)
    spec_approve.add_argument("--root", default=".")
    spec_confirm = spec_subparsers.add_parser("confirm")
    spec_confirm.add_argument("--run-dir")
    spec_confirm.add_argument("--root", default=".")
    spec_confirm.add_argument("--artifact")
    spec_confirm.add_argument(
        "--from-user-prompt",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    spec_confirm.add_argument("--session-id", help=argparse.SUPPRESS)
    spec_confirm.add_argument("--hook-capability", help=argparse.SUPPRESS)
    spec_prepare_confirmation = spec_subparsers.add_parser(
        "prepare-confirmation",
        help=argparse.SUPPRESS,
    )
    spec_prepare_confirmation.add_argument("--root", default=".")
    spec_prepare_confirmation.add_argument("--session-id", required=True)
    spec_prepare_confirmation.add_argument(
        "--hook-capability-hash",
        required=True,
        help=argparse.SUPPRESS,
    )
    spec_capture = spec_subparsers.add_parser("capture")
    spec_capture.add_argument("--root", default=".")
    spec_capture.add_argument("--run-dir", required=True)
    spec_capture.add_argument("--phase", required=True)
    spec_capture.add_argument("--artifact", required=True)
    spec_prompt = spec_subparsers.add_parser("prompt")
    spec_prompt.add_argument("--root", default=".")
    spec_prompt.add_argument("--run-dir", required=True)
    spec_markers = spec_subparsers.add_parser("markers")
    spec_markers.add_argument("--root", default=".")
    spec_markers.add_argument("--run-dir", required=True)
    spec_markers.add_argument("--project-root")
    spec_markers.add_argument("--phase", required=True)
    spec_markers.add_argument("--artifact", required=True)
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
    worktree_adopt = worktree_subparsers.add_parser("adopt")
    worktree_adopt.add_argument("--root", default=".")
    worktree_adopt.add_argument("--path", required=True)
    worktree_adopt.add_argument("--allow-dirty", action="store_true")
    worktree_identity = worktree_subparsers.add_parser("identity")
    worktree_identity.add_argument("--root", default=".")
    worktree_identity.add_argument("--path", default=".")

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
    args._worktree_explicit = bool(getattr(args, "worktree", None))
    requested_root = Path(getattr(args, "root", ".")).resolve()
    root = requested_root
    root, inferred_worktree, unadopted_checkout = _resolve_cli_root_context(
        root,
        getattr(args, "worktree", None),
    )
    if unadopted_checkout is not None and args.command in {"run", "start"}:
        # 조용히 leader root만 반환하면 사용자가 서 있는 checkout이 아닌 곳에서 런이
        # 돌고, task 이름으로 세 번째 worktree까지 생긴다. 여기서 멈추는 쪽이 낫다.
        print(
            f"{unadopted_checkout} is a linked worktree of {root} that agent-flow has "
            f"not adopted. {_unadopted_next_step(root=root, checkout=unadopted_checkout)} "
            f"Or pass `--root {shlex.quote(str(root))}` to work in the leader checkout "
            "instead.",
            file=sys.stderr,
        )
        return 2
    inferred_registration_identity: str | None = None
    if (
        args.command in {"run", "start"}
        and inferred_worktree is not None
        and not args._worktree_explicit
    ):
        try:
            inferred_status = get_worktree_status(
                root=root,
                name=inferred_worktree,
            )
            if (
                not inferred_status.exists
                or inferred_status.registration_identity is None
            ):
                raise WorktreeIsolationError(
                    "cannot prove the inferred worktree registration before "
                    "requesting reuse consent"
                )
            inferred_registration_identity = (
                inferred_status.registration_identity
            )
        except (OSError, ValueError, RuntimeError) as exc:
            print(_format_cli_error(exc), file=sys.stderr)
            return 2
    if inferred_worktree is not None and hasattr(args, "worktree") and args.worktree is None:
        args.worktree = inferred_worktree
    reuse_entry_commands = {"run", "start"}
    reuse_preapproved = bool(
        getattr(args, "reuse_existing_worktree", False)
    )
    if (
        args.command in reuse_entry_commands
        and reuse_preapproved
        and inferred_worktree is None
        and not args._worktree_explicit
    ):
        print(
            "--reuse-existing-worktree requires a managed worktree cwd or "
            "an explicit --worktree selector",
            file=sys.stderr,
        )
        return 2
    if (
        args.command in reuse_entry_commands
        and inferred_worktree is not None
        and not args._worktree_explicit
        and not _confirm_inferred_worktree_reuse(
            name=inferred_worktree,
            path=requested_root,
            preapproved=reuse_preapproved,
        )
    ):
        return 2
    # 문서가 안내하는 진입점은 이쪽이다. JS 래퍼에만 검사가 있으면 일반적인
    # 사용자는 kit을 올린 뒤에도 낡은 설치본을 끝까지 못 본다.
    if args.command in _KIT_FRESHNESS_COMMANDS:
        warn_if_installed_kit_is_stale(root, _find_kit_root())


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
        if repo_state != "repo":
            print(
                "worktree runs require a git repository; refusing to start "
                "without isolation",
                file=sys.stderr,
            )
            return 2
        try:
            assert_managed_hooks_registered(root)
        except HookIntegrityError as exc:
            print(_format_cli_error(exc), file=sys.stderr)
            return 2
        worktree_name = args.worktree if args.worktree is not None else args.task
        if worktree_name is not None:
            try:
                worktree_status, worktree_preexisting = _resolve_entry_worktree(
                    root=root,
                    selector=worktree_name,
                    explicit=args.worktree is not None,
                    branch=args.worktree_branch,
                    allow_dirty=args.allow_dirty,
                    expected_registration_identity=inferred_registration_identity,
                )
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
                next_command=_continue_command(
                    root, worktree_status.name if worktree_status is not None else worktree_name
                ),
                checkout_identity=_checkout_identity(
                    worktree_status.name if worktree_status is not None else None
                ),
                checkout_registration_identity=(
                    worktree_status.registration_identity
                    if worktree_status is not None
                    else None
                ),
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
            _assert_relay_checkout_identity(root, args.worktree, args.checkout_identity)
            run_root, state_root = (
                _worktree_context(root, args.worktree, provision=True)
                if args.worktree
                else (root, root)
            )
        except (ValueError, RuntimeError) as exc:
            print(_format_cli_error(exc), file=sys.stderr)
            return 2
        if run_root is None:
            return 1
        pending_cleanup = (
            find_pending_worktree_cleanup(root=root, selector=args.worktree)
            if args.worktree
            else None
        )
        active = find_active_run(state_root)
        resume_run_dir = active.path if active is not None else (
            pending_cleanup.run_dir if pending_cleanup is not None else None
        )
        if resume_run_dir is None:
            if _legacy_js_state_exists(root):
                _print_legacy_js_state_migration(root)
                return 2
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
                run_dir=resume_run_dir,
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
            _assert_relay_checkout_identity(root, args.worktree, args.checkout_identity)
            run_root, state_root = (
                _worktree_context(root, args.worktree, provision=True)
                if args.worktree
                else (root, root)
            )
        except ValueError as exc:
            print(_format_cli_error(exc), file=sys.stderr)
            return 2
        if run_root is None:
            return 1
        active = find_active_run(state_root)
        if active is not None:
            active.print_status(
                next_command=_continue_command(root, args.worktree),
                config_root=root,
                project_root=run_root,
            )
            return 0
        if _legacy_js_state_exists(root):
            _print_legacy_js_state_migration(root)
            return 2
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
            commands = _profile_gate_commands(profile_ids, phase=args.phase)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        results = run_gates(commands, cwd=command_root, timeout_s=args.timeout)
        if args.run_dir is not None:
            # run을 소유하는 것은 leader checkout도 worktree checkout도 아니라
            # worktree runtime root다(`cli.py:497`가 state_root로 쓰는 자리).
            # 상대 --run-dir을 command_root 기준으로 풀면 runner가 읽지 않는
            # 자리에 결과가 남는다.
            # 절대경로면 base가 쓰이지 않는다. worktree_runtime_root는 git을 새로
            # 띄우고 실패 시 예외를 던지므로, 방금 끝난 게이트 결과를 그 조회
            # 때문에 잃지 않도록 필요한 때만 계산한다.
            run_base = (
                worktree_runtime_root(root=root, name=args.worktree)
                if getattr(args, "worktree", None) and not Path(args.run_dir).is_absolute()
                else root
            )
            write_gate_results(
                run_dir=_resolve_project_path(run_base, args.run_dir),
                results=results,
                cwd=command_root,
                phase=args.phase,
            )
            if args.phase != GATE_PHASE_ALL:
                # runner는 이 결과를 pass 라우팅으로 받아 주지 않는다. 이유를 여기서
                # 말하지 않으면 사용자는 전 게이트 green인 파일이 fix-loop로 되돌려지는
                # 것만 보고 왜인지 알 수 없다.
                print(
                    f"note: --phase {args.phase} skips the other gate phases "
                    "(build and test are declared pre-push). The workflow gates phase "
                    "requires `--phase all`; this result will not route as passing QA.",
                    file=sys.stderr,
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
        timed_out = [result for result in results if result.timed_out]
        if timed_out:
            # 검증이 끊긴 것을 성공으로 돌려주면 exit code를 읽는 shell/CI가
            # timeout을 통과로 본다. required 여부와 무관하게 실패다.
            print(
                f"{','.join(profile_ids)}: timed out: "
                f"{', '.join(result.gate_id for result in timed_out)}",
                file=sys.stderr,
            )
            return 1
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
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        lint_args = [
            "--root",
            str(command_root),
            "--profile",
            ",".join(profile_ids),
        ]
        if args.files is not None:
            lint_args.extend(["--files", *args.files])
        return architecture_lint_main(lint_args)

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

    if args.command == "spec":
        try:
            if args.spec_command == "confirm":
                if args.from_user_prompt and (
                    args.run_dir is not None or args.artifact is not None
                ):
                    raise ValueError(
                        "--from-user-prompt does not accept run or artifact paths"
                    )
                if args.from_user_prompt and not args.hook_capability:
                    raise ValueError(
                        "host user-prompt confirmation requires a hook capability"
                    )
                if not args.from_user_prompt and args.hook_capability is not None:
                    raise ValueError(
                        "--hook-capability is reserved for host user-prompt confirmation"
                    )
                target = _resolve_spec_confirmation_target(
                    root,
                    run_dir_value=args.run_dir,
                    artifact_value=args.artifact,
                    inferred_worktree=inferred_worktree,
                    interactive=not args.from_user_prompt,
                    session_id=args.session_id if args.from_user_prompt else None,
                )
                if target is None:
                    if args.from_user_prompt:
                        return 0
                    raise ValueError("no active run is waiting for SPEC confirmation")
                parsed = parse_spec_item_section(
                    target.artifact.read_text(encoding="utf-8")
                )
                if parsed.errors or not parsed.items:
                    raise ValueError(
                        "invalid SPEC set: "
                        + "; ".join(parsed.errors or ("no SPEC items",))
                    )
                if args.from_user_prompt:
                    if not args.session_id:
                        return 0
                    confirmation_path = prepare_and_attest_user_spec_confirmation(
                        target.run_dir,
                        parsed.items,
                        prompt=sys.stdin.read(),
                        session_id=args.session_id,
                        checkout_identity=target.checkout_identity,
                        hook_capability=args.hook_capability,
                    )
                    if confirmation_path is None:
                        return 0
                else:
                    _read_interactive_approval(SPEC_SET_USER_REPLY)
                    confirmation_path = record_spec_set_confirmation(
                        target.run_dir,
                        parsed.items,
                        spec_set_confirmation_statement(parsed.items),
                    )
                print(f"SPEC set confirmed: {confirmation_path}")
                return 0
            if args.spec_command == "prepare-confirmation":
                target = _resolve_spec_confirmation_target(
                    root,
                    run_dir_value=None,
                    artifact_value=None,
                    inferred_worktree=inferred_worktree,
                    interactive=False,
                    session_id=args.session_id,
                )
                if target is None:
                    return 0
                parsed = parse_spec_item_section(
                    target.artifact.read_text(encoding="utf-8")
                )
                if parsed.errors or not parsed.items:
                    return 0
                prepare_user_spec_confirmation(
                    target.run_dir,
                    parsed.items,
                    session_id=args.session_id,
                    checkout_identity=target.checkout_identity,
                    hook_capability_hash=args.hook_capability_hash,
                )
                return 0
            run_dir = _resolve_project_path(root, args.run_dir)
            if args.spec_command == "approve":
                expected = manual_spec_approval_statement(run_dir, args.spec_id)
                statement = _read_interactive_approval(expected)
                approval_path = record_manual_spec_approval(
                    run_dir,
                    args.spec_id,
                    statement,
                )
                print(f"SPEC approved: {approval_path}")
                return 0
            if args.spec_command == "capture":
                artifact = _resolve_project_path(root, args.artifact)
                ledger = capture_design_ledger(
                    run_dir,
                    args.phase,
                    artifact.read_text(encoding="utf-8"),
                )
                if ledger is not None:
                    print(run_dir / "design-spec.md")
                return 0
            if args.spec_command == "prompt":
                sys.stdout.write(ledger_prompt_block(run_dir))
                return 0
            if args.spec_command == "markers":
                artifact = _resolve_project_path(root, args.artifact)
                project = (
                    _resolve_project_path(root, args.project_root)
                    if args.project_root
                    else root
                )
                context = _spec_run_context(run_dir)
                print(
                    json.dumps(
                        missing_spec_item_evidence(
                            project,
                            run_dir,
                            args.phase,
                            artifact.read_text(encoding="utf-8"),
                            task_text=context["task_text"],
                            profile=resolved_profile(root),
                            since=context["since"],
                            evidence_root=root,
                        ),
                        ensure_ascii=False,
                    )
                )
                return 0
        except (OSError, RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 2

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
                _apply_worktree_setup(root=root, checkout=status.path)
            except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
            print(f"{status.name} {status.branch} {status.path}")
            return 0
        if args.worktree_command == "adopt":
            try:
                # 인가는 워커가 스스로 줄 수 없다. 명령 문자열도 cwd도 호출자가 고르니
                # 호출자가 고를 수 없는 것으로 판정한다 — 활성 run이 있으면 거절이다.
                assert_adoption_allowed(root=root)
                status = adopt_worktree(
                    root=root,
                    path=Path(args.path),
                    allow_dirty=args.allow_dirty,
                )
                _apply_worktree_setup(root=root, checkout=status.path)
            except (
                OSError,
                ValueError,
                RuntimeError,
                subprocess.CalledProcessError,
            ) as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
            print(f"{status.name} {status.branch} {status.path} adopted")
            return 0
        if args.worktree_command == "identity":
            # JS runner가 상태 루트를 고르는 authority다. 지문 검증을 두 언어로
            # 구현하면 갈라진다 — 판정은 여기 한 곳에만 둔다.
            checkout = Path(args.path).resolve()
            identity = _verified_checkout_identity(root=root, path=checkout)
            if identity is None:
                print(
                    f"cannot prove a managed checkout at {checkout}",
                    file=sys.stderr,
                )
                return 1
            print(identity)
            return 0
        if args.worktree_command == "status":
            try:
                status = get_worktree_status(root=root, name=args.name)
            except (ValueError, WorktreeIsolationError) as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
            state = "exists" if status.exists else "missing"
            print(f"{status.name} {status.branch} {status.path} {state}")
            return 0
        if args.worktree_command == "list":
            # 복구 명령이다. git이 대답하지 않아도 traceback으로 죽지 않고
            # 아는 만큼 보여준 뒤 정상 종료한다.
            registered: list = []
            names: list[str] = []
            try:
                registered = removable_worktrees(root=root)
                names = _known_worktree_names(root)
            except (OSError, RuntimeError, ValueError) as exc:
                print(_format_cli_error(exc), file=sys.stderr)
            # git 등록부가 목록의 authority다. 이름 정규화로는 찾을 수 없는
            # 체크아웃도 여기서는 보고된 경로 그대로 보인다.
            rows = [
                f"{entry.path.name} {entry.branch or '-'} {entry.path} "
                f"{'exists' if (entry.path / '.git').exists() else 'stale'}"
                for entry in sorted(registered, key=lambda item: str(item.path))
            ]
            listed = {worktree_path_key(entry.path) for entry in registered}
            for name in names:
                try:
                    status = get_worktree_status(root=root, name=name)
                except (ValueError, RuntimeError):
                    path = _stale_checkout_path(root, name)
                    if worktree_path_key(path) not in listed:
                        rows.append(f"{name} - {path} stale")
                else:
                    if worktree_path_key(status.path) not in listed:
                        state = "exists" if _worktree_checkout_exists(status) else "stale"
                        rows.append(f"{status.name} {status.branch} {status.path} {state}")
            if not rows:
                print("no worktrees")
                return 0
            for row in rows:
                print(row)
            return 0
        if args.worktree_command == "remove":
            try:
                status = get_worktree_status(root=root, name=args.name)
            except (ValueError, RuntimeError) as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
            if not _worktree_checkout_exists(status):
                try:
                    known = _known_worktree_names(root)
                except (OSError, RuntimeError, ValueError) as exc:
                    print(_format_cli_error(exc), file=sys.stderr)
                    return 2
                if not status.path.exists() and status.name not in known:
                    print(
                        f"worktree not found or missing path: {status.name}",
                        file=sys.stderr,
                    )
                    return 1
            checkout_was_live = _worktree_checkout_exists(status)
            try:
                remove_worktree(
                    root=root,
                    status=status,
                    delete_branch=not args.keep_branch,
                    allow_unmerged=args.allow_unmerged,
                )
            except (
                subprocess.CalledProcessError,
                WorktreeIsolationError,
                WorktreeLockedError,
            ) as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
            if checkout_was_live:
                print(f"removed {status.name} {status.path}")
            elif status.path.exists():
                print(f"removed stale metadata {status.name}; kept path {status.path}")
            else:
                print(f"removed stale metadata {status.name} {status.path}")
            if not args.keep_branch and worktree_branch_exists(root=root, branch=status.branch):
                # agent-flow가 만든 브랜치라는 증거가 없어 남긴 경우다. 조용히 두면
                # 사용자는 정리가 끝난 줄 안다.
                print(f"kept branch {status.branch}")
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
            repo_state = git_repo_state(root)
            if repo_state == "unknown":
                print(
                    "cannot determine git repo state; refusing external provider launch",
                    file=sys.stderr,
                )
                return 2
            if repo_state != "repo":
                print(
                    "external providers require a verified linked git worktree",
                    file=sys.stderr,
                )
                return 2
            try:
                verify_provider_sandbox_backend()
                assert_managed_hooks_registered(root)
                with provider_lease(root) as lease:
                    with worker_claim_lock(root):
                        current_status = team_status(
                            root=root,
                            team_name=args.team,
                            detail=True,
                        )
                        current_task = next(
                            (
                                task
                                for task in current_status["tasks"]
                                if task.task_id == pending.task_id
                            ),
                            None,
                        )
                        if current_task is None or current_task.status != "pending":
                            raise ValueError(
                                f"task is no longer pending: {pending.task_id}"
                            )
                        prompt = _team_run_next_prompt(
                            root=root,
                            team_name=args.team,
                            task_id=pending.task_id,
                            worker_name=args.worker,
                            fallback_subject=current_task.subject,
                            fallback_description=current_task.description,
                        )
                        if prompt is None:
                            raise ValueError(
                                f"worker is no longer approved for task: "
                                f"{pending.task_id}"
                            )
                        plan = plan_worktree(
                            root=root,
                            name=pending.task_id,
                            unique=args.worker,
                        )
                        worktree_status = create_worktree(
                            root=root,
                            plan=plan,
                            allow_dirty=True,
                            reuse_existing=False,
                        )
                        _apply_worktree_setup(root=root, checkout=worktree_status.path)
                        worker_cwd = verify_linked_worktree(
                            root=root,
                            path=worktree_status.path,
                            expected_branch=plan.branch,
                        )
                        assert_cwd_bound(
                            worktree_path=worktree_status.path,
                            cwd=worker_cwd,
                        )
                        leader_before = capture_leader_snapshot(root)
                        claimed = claim_task(
                            root=root,
                            team_name=args.team,
                            task_id=pending.task_id,
                            worker_name=args.worker,
                            lease=lease,
                        )

                    result = run_provider(
                        ProviderCommand(
                            name="host-command",
                            argv=tuple(args.command_argv),
                        ),
                        prompt=prompt,
                        cwd=worker_cwd,
                        lease=lease,
                    )
                    try:
                        assert_leader_unchanged(
                            root,
                            leader_before,
                            run_id=claimed.task_id,
                            worker_root=worker_cwd,
                        )
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
                    print(
                        f"{task.task_id} {task.status} "
                        f"worktree={worktree_status.path}"
                    )
                    return 1 if result.failed else 0
            except (
                HookIntegrityError,
                OSError,
                ValueError,
                RuntimeError,
                WorktreeIsolationError,
                subprocess.CalledProcessError,
            ) as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
        if args.team_command == "claim":
            recovery = shlex.join(
                (
                    "agent-flow",
                    "team",
                    "run-next",
                    "--root",
                    str(root),
                    "--team",
                    args.team,
                    "--worker",
                    args.worker,
                    "--command",
                    "<provider-command>",
                )
            )
            print(
                "direct team claim is disabled because it has no live provider "
                f"lease; use `{recovery}`",
                file=sys.stderr,
            )
            return 2
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
        repo_state = git_repo_state(root)
        if repo_state == "unknown":
            print(
                "cannot determine git repo state; refusing to start unisolated in the leader checkout",
                file=sys.stderr,
            )
            return 2
        if repo_state != "repo":
            print(
                "worktree runs require a git repository; refusing to start "
                "without isolation",
                file=sys.stderr,
            )
            return 2
        try:
            assert_managed_hooks_registered(root)
        except HookIntegrityError as exc:
            print(_format_cli_error(exc), file=sys.stderr)
            return 2
        worktree_name = args.worktree
        implicit_phase_worktree = args.phase_runner and worktree_name is None
        if worktree_name is None:
            worktree_name = args.task
        if args.phase_runner:
            if _legacy_js_state_exists(root):
                _print_legacy_js_state_migration(root)
                return 2
            try:
                _assert_entry_checkout_identity(
                    root=root,
                    worktree=None if implicit_phase_worktree else worktree_name,
                    branch=(
                        None if implicit_phase_worktree else args.worktree_branch
                    ),
                    claimed=args.checkout_identity,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
        try:
            if worktree_name is not None:
                status, worktree_preexisting = _resolve_entry_worktree(
                    root=root,
                    selector=worktree_name,
                    explicit=args.worktree is not None,
                    branch=args.worktree_branch,
                    allow_dirty=args.allow_dirty,
                    expected_registration_identity=inferred_registration_identity,
                )
                worktree_status = status
                worktree = {
                    "name": status.name,
                    "branch": status.branch,
                    "path": str(status.path),
                }
            state_root = (
                worktree_runtime_root(root=root, name=worktree["name"])
                if worktree is not None
                else root
            )
            if args.phase_runner:
                actual_identity = _checkout_identity(
                    worktree_status.name if worktree_status is not None else None
                )
                if (
                    not implicit_phase_worktree
                    and args.checkout_identity != actual_identity
                ):
                    raise ValueError(
                        "checkout identity changed during worktree resolution; refusing to start"
                    )
                run_root = (
                    worktree_status.path if worktree_status is not None else root
                )
                Runner(
                    run_root,
                    state_root=state_root,
                    config_root=root,
                    workflow=args.workflow,
                    architecture=args.architecture,
                    next_command=_continue_command(
                        root,
                        worktree_status.name
                        if worktree_status is not None
                        else None,
                    ),
                    requested_run_id=args.run_id,
                    checkout_identity=actual_identity,
                    checkout_registration_identity=(
                        worktree_status.registration_identity
                        if worktree_status is not None
                        else None
                    ),
                ).run(mode=ResumeMode.START, task=args.task)
                return 0

            workflow = load_workflow(args.workflow)
            profile = detect_profile(root) if args.profile == "auto" else args.profile
            adapter = detect_adapter() if args.adapter == "auto" else args.adapter
            if worktree_status is None:
                raise WorktreeIsolationError(
                    "worktree registration is unavailable before run activation"
                )
            with worktree_run_activation(
                root=root,
                path=worktree_status.path,
                registration_identity=worktree_status.registration_identity,
            ):
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
                    project_root=root,
                )
            _write_stage_prompts(root=state_root, state=state, workflow=workflow)
        except (
            OSError,
            ValueError,
            RuntimeError,
            KeyError,
            subprocess.CalledProcessError,
        ) as exc:
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


def _profile_gate_commands(profile_ids: list[str], *, phase: str = DEFAULT_GATE_PHASE) -> list[GateCommand]:
    commands: list[tuple[int, GateCommand]] = []
    seen: set[tuple[str, ...]] = set()
    multi_profile = len(profile_ids) > 1
    architecture_lint_added = False
    architecture_lint_profile = ",".join(profile_ids)
    order = 0
    for profile_id in profile_ids:
        profile = load_profile(profile_id)
        for gate in profile.gates:
            if phase != GATE_PHASE_ALL and gate.phase != phase:
                continue
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


def _worktree_context(
    root: Path,
    name: str,
    *,
    provision: bool = False,
) -> tuple[Path | None, Path]:
    status = get_worktree_status(root=root, name=name)
    pending = find_pending_worktree_cleanup(root=root, selector=name)
    if pending is not None:
        checkout_root = status.path if _worktree_checkout_exists(status) else root
        return checkout_root, cleanup_state_root(pending)
    if _worktree_checkout_exists(status):
        if provision:
            # 업그레이드 전에 만들어진 checkout도 여기서 등록 파일을 얻는다. host
            # 세션이 실제로 그 checkout에서 일하는 지점(`continue`/`status`)만
            # 부른다 — `abort`는 걷어낼 checkout이라 등록을 깔 이유가 없다.
            _provision_host_hooks(root=root, checkout=status.path)
        return status.path, worktree_runtime_root(root=root, name=status.name)
    known = _known_worktree_names(root)
    suffix = f" known worktrees: {', '.join(known)}" if known else " no known worktrees"
    print(f"worktree not found or missing path: {status.name}.{suffix}", file=sys.stderr)
    return None, worktree_runtime_root(root=root, name=status.name)


def _confirm_inferred_worktree_reuse(
    *,
    name: str,
    path: Path,
    preapproved: bool,
) -> bool:
    if preapproved:
        return True
    if not sys.stdin.isatty():
        print(
            f"existing worktree inferred from cwd: {name} ({path}). "
            "Refusing implicit reuse in a non-interactive session; pass "
            "--reuse-existing-worktree or an explicit --worktree selector.",
            file=sys.stderr,
        )
        return False
    try:
        answer = input(
            f"Existing worktree detected: {name} ({path}). Reuse it? [y/N] "
        )
    except EOFError:
        answer = ""
    if answer.strip().lower() in {"y", "yes"}:
        return True
    print(
        "run cancelled; start from the leader checkout or pass a different "
        "--worktree selector to create another worktree.",
        file=sys.stderr,
    )
    return False


def _slug_naming_for_active_host(root: Path) -> tuple[list[str], int]:
    """활성 host의 이름 짓기 명령과 profile이 선언한 길이 제한.

    제한을 안 읽으면 profile이 20을 선언해도 host가 낸 50자가 그대로 이름이 된다.
    """
    empty: tuple[list[str], int] = ([], DEFAULT_SLUG_MAX_LENGTH)
    try:
        _profile_id, profile = _load_profile(_find_kit_root(), root)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        # profile을 못 읽는다고 worktree 생성을 막지 않는다. 이름만 못 지을 뿐이다.
        return empty
    host = (os.environ.get("AGENT_FLOW_HOST") or "").strip().lower()
    if not host:
        return empty
    sources = [profile]
    nested = profile.get("profiles")
    if isinstance(nested, list):
        sources.extend(item for item in nested if isinstance(item, dict))
    for source in sources:
        naming = ((source.get("branching") or {}).get("naming") or {})
        declared = (naming.get("slug_command") or {}).get(host)
        if isinstance(declared, list) and declared:
            limit = naming.get("max_slug_length")
            if not isinstance(limit, int) or limit < 1:
                limit = DEFAULT_SLUG_MAX_LENGTH
            return [str(part) for part in declared], limit
    return empty


def _derive_worktree_selector(*, root: Path, task: str) -> str:
    """task에서 worktree 이름을 정한다. 위임 실패는 경고로 드러낸다."""
    try:
        quality = describe_slug(task)
    except ValueError:
        return task
    if quality.kind == "ascii":
        return task
    command, max_length = _slug_naming_for_active_host(root)
    if command:
        delegated = delegated_slug(task=task, command=command, max_length=max_length)
        if delegated:
            print(f"worktree 이름을 활성 host가 지었다: feat-{delegated}")
            return delegated
    _warn_if_slug_does_not_represent_the_task(task)
    return task


def _warn_if_slug_does_not_represent_the_task(task: str) -> None:
    """이름이 task를 대표하지 못하면 사실대로 말한다.

    `--worktree`로 사용자가 직접 지은 이름에는 아무 말도 하지 않는다.
    """
    try:
        quality = describe_slug(task)
    except ValueError:
        return
    if quality.kind == "ascii":
        return
    dropped = ", ".join(quality.dropped)
    if quality.kind == "digest":
        detail = f"task에서 쓸 수 있는 문자를 찾지 못했다 (버려진 말: {dropped})"
    else:
        detail = f"task의 일부만 이름에 남았다 (버려진 말: {dropped})"
    print(
        f"warning: worktree 이름 `feat-{quality.slug}`이 task를 대표하지 못한다 — "
        f"{detail}. 원하는 이름이 있으면 `--worktree <name>`으로 지정하라.",
        file=sys.stderr,
    )


def _resolve_entry_worktree(
    *,
    root: Path,
    selector: str,
    explicit: bool,
    branch: str | None,
    allow_dirty: bool,
    expected_registration_identity: str | None = None,
) -> tuple[WorktreeStatus, bool]:
    """run/start이 쓸 worktree 하나를 확정한다. 반환: (status, 이미 있던 것인가).

    명시 selector(`--worktree`, 또는 worktree cwd에서 추론된 이름)만 등록부 우선으로
    해석한다. task 이름에서 온 암묵 selector까지 넓히면 task 문자열이 남의 브랜치
    이름과 겹치는 순간 엉뚱한 checkout에 붙는다.

    모호한 selector는 여기서 잡지 않는다. 생성 경로도 같은 지점에서 다시 raise하므로
    (`create_worktree` → `get_worktree_status` → `resolve_worktree`) 폴백은 실패를
    두 번 알리기만 한다.
    """
    if explicit:
        attached = attach_worktree(
            root=root,
            selector=selector,
            branch=branch,
            allow_dirty=allow_dirty,
            expected_registration_identity=expected_registration_identity,
        )
        if attached is not None:
            # 재사용도 셋업 대상이다. 손으로 만든 checkout이거나 선언이 나중에 추가된
            # 경우 파일이 없을 수 있다. 이미 있으면 건너뛰므로 반복해도 무해하다.
            _apply_worktree_setup(root=root, checkout=attached.path)
            _warn_if_cwd_is_other_checkout(root=root, target=attached.path)
            return attached, True
    if not explicit:
        selector = _derive_worktree_selector(root=root, task=selector)
    plan = plan_worktree(root=root, name=selector, branch=branch)
    try:
        status = create_worktree(
            root=root,
            plan=plan,
            allow_dirty=allow_dirty,
            reuse_existing=False,
        )
    except WorktreeAlreadyExistsError as exc:
        if not explicit:
            raise WorktreeAlreadyExistsError(
                f"task-derived worktree already exists: {plan.path}. "
                f"Pass --worktree {plan.name} to reuse it explicitly."
            ) from exc
        raise
    _apply_worktree_setup(root=root, checkout=status.path)
    _warn_if_cwd_is_other_checkout(root=root, target=status.path)
    return status, False


def _warn_if_cwd_is_other_checkout(*, root: Path, target: Path) -> None:
    """cwd가 이 저장소의 다른 checkout인데 작업은 다른 자리에서 도는 경우를 알린다.

    관리 루트 밖 checkout에 붙는 것은 지원 범위가 아니다. 그렇다고 조용히 다른
    자리에서 돌면 사용자는 서 있던 checkout에서 작업이 도는 줄 안다.
    """
    result = git_safe(
        "rev-parse", "--show-toplevel", cwd=Path.cwd(), optional_locks=False
    )
    if not result.ok:
        return
    checkout = Path(result.stdout.strip())
    if same_worktree_path(checkout, root) or same_worktree_path(checkout, target):
        return
    print(
        f"notice: cwd is worktree {checkout}; this run uses {target}. "
        f"Pass --worktree to continue in the checkout you are standing in.",
        file=sys.stderr,
    )


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


def _stale_checkout_path(root: Path, name: str) -> Path:
    """이름만 남은 잔재의 자리. 판정은 `worktrees`가 소유한다."""
    return existing_checkout_path(root=root, name=name)


def _worktree_checkout_exists(status) -> bool:
    return status.exists and (status.path / ".git").exists()


def _is_managed_worktree_path(root: Path, path: Path) -> bool:
    managed = worktree_path_key(root / ".agent-flow" / "worktrees")
    return worktree_path_key(path).startswith(managed + os.sep)


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


# 디버깅 중에는 실패 현장을 남길 수 있어야 한다. 커밋되지 않은 작업이 있으면 아래
# `WorktreeIsolationError` 경로가 이미 보존하지만, 게이트 실패나 재현이 어려운 경합처럼
# 그 조건에 안 걸리는 실패는 증거가 worktree와 함께 사라진다.
#
# 사용자가 직접 부르는 `worktree remove`에는 걸지 않는다. 그건 정리하겠다는 명시적
# 의사이고, 여기서 막으면 끄고 나서 치울 방법이 없어진다.
KEEP_FAILED_WORKTREE_ENV = "AGENT_FLOW_KEEP_FAILED_WORKTREE"
_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})


def _keep_failed_worktree() -> bool:
    return os.environ.get(KEEP_FAILED_WORKTREE_ENV, "").strip().lower() in _TRUTHY_ENV


def _declared_worktree_copies(profile: dict) -> list[str]:
    """단일 profile과 multi-profile 합성본 양쪽에서 선언을 모은다.

    `_load_profile_union`이 만드는 합성 dict에는 최상위 `branching`이 없다 —
    개별 profile은 `profiles` 아래에 들어가고 최상위에는 `review_angles`/`gates`/
    `skills`/`architecture`만 합쳐진다. 최상위만 보면 android+react-native처럼
    profile이 둘 이상인 프로젝트에서 선언이 조용히 빈 목록이 되고, `local.properties`가
    영영 복사되지 않는다.
    """
    sources: list[dict] = [profile]
    nested = profile.get("profiles")
    if isinstance(nested, list):
        sources.extend(item for item in nested if isinstance(item, dict))
    names: list[str] = []
    for source in sources:
        branching = source.get("branching")
        if not isinstance(branching, dict):
            continue
        setup = branching.get("worktree_setup")
        if not isinstance(setup, dict):
            continue
        for name in setup.get("copy") or []:
            text = str(name)
            if text not in names:
                names.append(text)
    return names


def _provision_host_hooks(*, root: Path, checkout: Path) -> None:
    """checkout에서 연 host 세션도 UserPromptSubmit hook을 갖도록 등록 파일을 깐다.

    profile 해석과 무관하다 — profile이 깨져도 hook 등록까지 같이 잃으면 안 된다.
    이미 같은 내용이면 아무것도 쓰지 않으므로 매 실행마다 불러도 무해하다.
    """
    try:
        written = provision_host_hook_registrations(leader=root, checkout=checkout)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(
            f"warning: host hook registration skipped: {_format_cli_error(exc)}",
            file=sys.stderr,
        )
        return
    if written:
        # host는 등록 파일을 세션 시작 시점에 읽는다. 방금 깔았다고 지금 열려 있는
        # 세션에 hook이 생기지는 않으므로 다음 수를 함께 말해야 한다.
        print(
            f"host hook registrations provisioned: {', '.join(written)} — "
            f"이 checkout({checkout})에서 열린 host 세션을 다시 시작해야 hook이 "
            "로드됩니다."
        )


def _apply_worktree_setup(*, root: Path, checkout: Path) -> None:
    """새 checkout을 쓸 수 있게 만든다: host hook 등록 표면 + profile 선언 설정.

    실패해도 worktree 생성을 되돌리지 않는다. 설정이 없어서 빌드가 한 번 실패하는 것과
    방금 만든 checkout이 통째로 사라지는 것은 무게가 다르다. 대신 무엇이 빠졌는지 알린다.
    """
    _provision_host_hooks(root=root, checkout=checkout)
    try:
        _profile_id, profile = _load_profile(_find_kit_root(), root)
        declared = _declared_worktree_copies(profile)
    except Exception as exc:  # profile 해석 실패가 worktree 생성을 막을 이유는 없다
        print(f"warning: skipped worktree setup: {_format_cli_error(exc)}", file=sys.stderr)
        return
    # 복사와 동작은 서로 독립이다. 한쪽이 실패했다고 다른 쪽을 건너뛰면 선언한
    # 동작이 조용히 빠진다.
    if declared:
        try:
            copied = copy_declared_worktree_files(
                leader=root, checkout=checkout, names=[str(name) for name in declared]
            )
        except (ValueError, OSError) as exc:
            print(
                f"warning: worktree setup failed: {_format_cli_error(exc)}",
                file=sys.stderr,
            )
            copied = ()
        missing = [str(name) for name in declared if str(name) not in copied]
        if copied:
            print(f"worktree setup copied: {', '.join(copied)}")
        if missing:
            print(
                f"warning: worktree setup did not copy {', '.join(missing)} "
                f"(absent in {root}, already present, or a symlink)",
                file=sys.stderr,
            )
    _run_worktree_setup_actions(root=root, checkout=checkout, profile=profile)


def _declared_worktree_actions(profile: dict) -> dict:
    """`copy` 말고 선언된 동작 키들. 합성 profile도 함께 본다."""
    sources: list[dict] = [profile]
    nested = profile.get("profiles")
    if isinstance(nested, list):
        sources.extend(item for item in nested if isinstance(item, dict))
    actions: dict = {}
    for source in sources:
        setup = ((source.get("branching") or {}).get("worktree_setup") or {})
        if not isinstance(setup, dict):
            continue
        for key, value in setup.items():
            if key == "copy":
                continue
            if key not in WORKTREE_SETUP_ACTIONS:
                print(
                    f"warning: unknown worktree setup key {key!r}; known: "
                    f"{', '.join(sorted(WORKTREE_SETUP_ACTIONS))}",
                    file=sys.stderr,
                )
                continue
            actions[key] = bool(value) or actions.get(key, False)
    return actions


def _run_worktree_setup_actions(*, root: Path, checkout: Path, profile: dict) -> None:
    declared = _declared_worktree_actions(profile)
    if not declared:
        return
    try:
        ran = run_declared_worktree_actions(
            leader=root, checkout=checkout, declared=declared
        )
    except UnknownWorktreeSetupAction as exc:
        # 오타를 조용히 넘기면 선언했는데 아무 일도 일어나지 않는다.
        print(f"warning: {_format_cli_error(exc)}", file=sys.stderr)
        return
    if ran:
        print(f"worktree setup ran: {', '.join(ran)}")


def _cleanup_worktree_after_failure(root: Path, status, original: BaseException) -> None:
    if _keep_failed_worktree():
        # 왜 남았는지 말하지 않으면 유출과 구분되지 않는다.
        print(
            f"warning: keeping worktree {status.name} at {status.path} "
            f"({KEEP_FAILED_WORKTREE_ENV} is set); remove it with "
            f"`agent-flow worktree remove --name {status.name}`",
            file=sys.stderr,
        )
        print(_format_cli_error(original), file=sys.stderr)
        return
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
    # 등록부가 먼저다. 정규화로 이름을 뭉개면 출력된 next_command가 다른 checkout을
    # 가리키고, 그 명령이 세 번째 worktree를 만든다.
    try:
        registered = resolve_worktree(root=root, selector=value)
    except (OSError, ValueError, RuntimeError):
        registered = None
    if registered is not None:
        return registered.path.name
    try:
        return plan_worktree(root=root, name=value).name
    except ValueError:
        return value


def _expected_checkout_identity(
    *, root: Path, worktree: str | None, branch: str | None = None
) -> str:
    if worktree is None:
        return "leader"
    registered = resolve_worktree(root=root, selector=worktree)
    if registered is not None:
        return f"worktree:{registered.path.name}"
    planned = plan_worktree(root=root, name=worktree, branch=branch)
    return f"worktree:{planned.name}"


def _assert_entry_checkout_identity(
    *,
    root: Path,
    worktree: str | None,
    branch: str | None,
    claimed: str | None,
) -> None:
    if not claimed or claimed == "unknown":
        raise ValueError(
            "checkout identity is unknown; refusing to start before any state mutation"
        )
    expected = _expected_checkout_identity(
        root=root,
        worktree=worktree,
        branch=branch,
    )
    if claimed != expected:
        raise ValueError(
            f"checkout identity mismatch: claimed {claimed!r}, expected {expected!r}"
        )


def _assert_relay_checkout_identity(
    root: Path, worktree: str | None, claimed: str | None
) -> None:
    if claimed is None:
        return
    if claimed == "unknown":
        raise ValueError("checkout identity is unknown; refusing lifecycle relay")
    expected = _expected_checkout_identity(root=root, worktree=worktree)
    if claimed != expected:
        raise ValueError(
            f"checkout identity mismatch: claimed {claimed!r}, expected {expected!r}"
        )


def _legacy_js_state_exists(root: Path) -> bool:
    return (root / ".agent-flow" / "state" / "current-run.json").is_file()


def _print_legacy_js_state_migration(root: Path) -> None:
    path = root / ".agent-flow" / "state" / "current-run.json"
    print(
        f"legacy JS run state detected at {path}; automatic fallback is disabled. "
        "Archive the legacy run artifacts, remove current-run.json, then start a "
        "new Python-authoritative run.",
        file=sys.stderr,
    )




def _resolve_cli_root_context(
    root: Path, worktree: str | None
) -> tuple[Path, str | None, Path | None]:
    """(config root, worktree 이름, 채택되지 않은 checkout).

    세 번째 값이 non-None이면 cwd가 이 저장소의 linked worktree인데 agent-flow가
    채택한 기록이 없다는 뜻이다. 진입 명령은 그 상태에서 멈춘다.
    """
    managed = _managed_worktree_context(root)
    if managed is not None:
        leader_root, inferred_worktree = managed
        return leader_root, worktree or inferred_worktree, None
    cwd_managed = _managed_worktree_context(Path.cwd())
    if cwd_managed is not None and (_same_path(root, Path.cwd()) or _same_path(root, cwd_managed[0])):
        leader_root, inferred_worktree = cwd_managed
        return leader_root, worktree or inferred_worktree, None
    cwd_leader = leader_root_for(Path.cwd())
    if cwd_leader is not None and _same_path(root, cwd_leader):
        # `--root <leader>`로 불렸어도 서 있는 자리가 채택된 checkout이면 그 자리를 쓴다.
        # JS relay가 언제나 `--root`를 붙여 주므로(`relayPythonRunLifecycle`) 이 분기가
        # 없으면 채택된 worktree에서 부른 lifecycle 명령이 전부 leader로 접힌다.
        cwd_checkout = _registered_checkout(leader_root=cwd_leader, path=Path.cwd())
        if (
            cwd_checkout is not None
            and adopted_worktree_parent(root=cwd_leader, path=cwd_checkout) is not None
        ):
            return cwd_leader, worktree or cwd_checkout.name, None
    leader_root = leader_root_for(root)
    if leader_root is not None:
        # 경로 모양이 관리 규약과 달라도 이 저장소의 linked worktree다. 인식 근거를
        # 경로에서 등록부로 옮기는 자리가 여기다.
        checkout = _registered_checkout(leader_root=leader_root, path=root)
        if checkout is None:
            return leader_root, worktree, None
        if adopted_worktree_parent(root=leader_root, path=checkout) is not None:
            return leader_root, worktree or checkout.name, None
        return leader_root, worktree, checkout
    git_common_root = _git_common_worktree_root(root)
    if git_common_root is not None:
        return git_common_root, worktree, None
    return root, worktree, None


def _registered_checkout(*, leader_root: Path, path: Path) -> Path | None:
    """``path``가 leader가 아닌 checkout에 서 있으면 그 checkout 경로.

    등록부를 읽을 수 없거나 등록 행이 없어도 경로를 돌려준다. 호출자는 그 답을
    미채택으로 취급해 진입을 막는다 — 증명 실패를 통과로 접으면 격리 없이 leader에서
    도는 쪽으로 흐른다. leader(또는 그 하위 디렉터리)면 ``None``이다.
    """
    top_level = git_toplevel(path)
    if top_level is None or _same_path(top_level, leader_root):
        return None
    try:
        registered = registered_worktree_at(leader_root, top_level)
    except WorktreeIsolationError:
        return top_level
    if registered is None or registered.prunable:
        # 등록 행이 없다 = 이 checkout이 이 저장소의 관리 대상이라는 증명이 없다.
        # 예전처럼 leader로 접으면(디렉터리를 옮긴 worktree가 이 상태다) 사용자가
        # 서 있는 자리가 아닌 곳에서 런이 돈다. 증명 실패는 통과가 아니다.
        return top_level
    return registered.path


def _unadopted_next_step(*, root: Path, checkout: Path) -> str:
    """미채택 checkout에서 실제로 통하는 다음 명령 한 줄.

    상태를 보지 않고 `adopt`만 안내하면 닫힌 루프가 된다. 디렉터리만 옮겨진 checkout은
    등록이 prunable이라 `adopt`가 "등록된 worktree가 없다"로 거절하고, 그 사용자에게
    필요한 것은 `git worktree repair`다.
    """
    adopt = f"Adopt it with `agent-flow worktree adopt --path {shlex.quote(str(checkout))}`."
    try:
        registered = registered_worktree_at(root, checkout)
    except WorktreeIsolationError:
        return adopt
    if registered is None or registered.prunable:
        return (
            f"Its git registration is stale; run `git worktree repair "
            f"{shlex.quote(str(checkout))}` first, then {adopt[0].lower()}{adopt[1:]}"
        )
    return adopt


def _managed_worktree_context(path: Path) -> tuple[Path, str | None] | None:
    resolved = path.resolve()
    parts = resolved.parts
    markers = {".agent-flow", ".codex", ".Codex", ".omp"}
    for index in range(len(parts) - 2, 0, -1):
        if parts[index] not in markers or parts[index + 1] != "worktrees":
            continue
        root = Path(*parts[:index])
        if parts[index] in {".codex", ".Codex", ".omp"} and _same_path(root, _home_path()):
            continue
        # `<marker>/worktrees`로 끝나는 경로에는 이름이 없다. 무가드로 읽으면 그
        # 자리에서 IndexError로 죽는다. JS 쌍둥이도 여기서 null을 낸다.
        return root, parts[index + 2] if index + 2 < len(parts) else None
    return None


def _git_common_worktree_root(root: Path) -> Path | None:
    # anchor 유도는 sanitize된 git으로만 한다. ambient GIT_DIR/GIT_COMMON_DIR을
    # 상속한 채 물으면 대답이 다른 저장소를 가리키고, 그 부모가 그대로 config root와
    # state root가 된다(kit.json을 심은 decoy면 hook 무결성 게이트까지 통과한다).
    if git_toplevel(root) is None:
        return None
    common_path = git_common_dir(root)
    if common_path is None or common_path.name != ".git":
        return None
    return common_path.parent


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


def _workflow_declarations() -> DeclaredPhaseSkills:
    """doctor의 활성화 경로 축 하나 — workflow phase가 이름으로 선언한 skill.

    수집 실패를 조용히 빈 값으로 퇴화시키면 doctor가 정상 선언된 skill을 미라우팅으로
    오탐한다. 그래서 읽지 못한 workflow는 사유를 그대로 들고 나온다.
    """
    try:
        kit_root = _find_kit_root()
    except (OSError, RuntimeError) as exc:
        return DeclaredPhaseSkills((), (f"workflow declarations unavailable: {exc}",))
    return declared_phase_skills(kit_root)


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
            for result in sync_skill_sources(sources, refresh=bool(getattr(args, "refresh", False))):
                print(f"{profile_id}: {result.source_id} {result.status} {result.detail}".rstrip())
                if result.status == "failed":
                    exit_code = 1
        return exit_code

    if args.skills_command in {"scan", "doctor"}:
        merged = merged_profile_payload(payloads)
        declared = _workflow_declarations()
        for error in declared.errors:
            print(error, file=sys.stderr)
        result = skill_catalog.scan(root, profile=merged, workflow_skills=declared.names)
        sources = ", ".join(
            f"{source} {count}" for source, count in sorted(result.by_source().items())
        )
        print(f"catalog: {len(result.entries)} skills ({sources}) stamp={result.stamp[:12]}")
        findings = list(result.findings)
        if declared.errors and not declared.names:
            # 축이 통째로 없으면 UNROUTED는 전부 오탐이다. 진짜 finding과 형식이 같아
            # 구별되지 않으므로 인쇄하지 않고 그 사실을 남긴다.
            findings = [item for item in findings if item.kind != skill_catalog.UNROUTED]
            print("degraded: workflow declarations unavailable; unrouted findings suppressed")
        elif declared.errors:
            # 축소된 선언 집합으로 판정하면 그 workflow만 선언한 skill이 미라우팅
            # 오탐으로 찍힌다. stdout만 보는 소비자가 정상 결과와 구별할 수 있어야 한다.
            print(
                f"degraded: workflow declarations incomplete "
                f"({len(declared.errors)} workflow(s) unreadable); unrouted findings may be false"
            )
        if args.skills_command == "scan" and not getattr(args, "no_write", False):
            print(f"lock: {skill_catalog.write_lock(root, result).relative_to(root)}")
        for finding in findings:
            line = f"{finding.kind} {finding.name}"
            if finding.detail:
                line = f"{line} — {finding.detail}"
            print(line)
            if finding.fix:
                print(f"  Fix: {finding.fix}")
        return 0

    if args.skills_command in {"resolve", "prompt", "markers"}:
        try:
            definition = load_phase_workflow_definition(_find_kit_root(), args.workflow)
        except (OSError, ValueError, yaml.YAMLError) as exc:
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


def _read_interactive_approval(expected: str) -> str:
    if not _is_foreground_user_terminal():
        raise RuntimeError(
            "SPEC approval requires one foreground user terminal attached to "
            "stdin, stdout, and stderr; agents and redirected or synthetic "
            "sessions cannot approve"
        )
    print(f"Type exactly to approve: {expected}")
    statement = sys.stdin.readline().strip()
    if statement != expected:
        raise RuntimeError("approval statement did not match exactly")
    return statement


def _is_foreground_user_terminal() -> bool:
    if any(
        os.environ.get(name)
        for name in (
            "CLAUDECODE",
            "CLAUDE_CLI",
            "CODEX_CLI",
            "CODEX_HOME",
            "OMPCODE",
            "OMP_PROFILE",
        )
    ):
        return False
    streams = (sys.stdin, sys.stdout, sys.stderr)
    try:
        fds = tuple(stream.fileno() for stream in streams)
        if any(not stream.isatty() or not os.isatty(fd) for stream, fd in zip(streams, fds)):
            return False
        if len({os.fstat(fd).st_rdev for fd in fds}) != 1:
            return False
        getpgrp = getattr(os, "getpgrp", None)
        getsid = getattr(os, "getsid", None)
        tcgetpgrp = getattr(os, "tcgetpgrp", None)
        if getpgrp is None or getsid is None or tcgetpgrp is None:
            return False
        process_group = getpgrp()
        if any(tcgetpgrp(fd) != process_group for fd in fds):
            return False
        return getsid(os.getpid()) == getsid(os.getppid())
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _read_run_state(run_dir: Path) -> dict:
    meta = read_meta(run_dir)
    if meta:
        return meta
    try:
        payload = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


@dataclass(frozen=True)
class _SpecConfirmationTarget:
    run_dir: Path
    artifact: Path
    checkout_identity: str


def _resolve_spec_confirmation_target(
    root: Path,
    *,
    run_dir_value: str | None,
    artifact_value: str | None,
    inferred_worktree: str | None,
    interactive: bool,
    session_id: str | None = None,
) -> _SpecConfirmationTarget | None:
    if run_dir_value is not None:
        run_dir = _resolve_project_path(root, run_dir_value)
        if artifact_value is not None:
            artifact = _resolve_project_path(root, artifact_value)
        else:
            artifact = _spec_artifact_waiting_for_confirmation(run_dir, pending_only=False)
            if artifact is None:
                raise ValueError(f"run has no current SPEC source artifact: {run_dir}")
        return _SpecConfirmationTarget(
            run_dir=run_dir,
            artifact=artifact,
            checkout_identity=_checkout_identity(inferred_worktree),
        )

    candidates: list[_SpecConfirmationTarget] = []
    for state_root, checkout_identity in _spec_confirmation_state_roots(
        root,
        inferred_worktree=inferred_worktree,
        session_id=session_id,
    ):
        for active in find_active_runs(state_root):
            artifact = _spec_artifact_waiting_for_confirmation(
                active.path,
                pending_only=True,
            )
            if artifact is not None:
                candidates.append(
                    _SpecConfirmationTarget(
                        run_dir=active.path,
                        artifact=artifact,
                        checkout_identity=checkout_identity,
                    )
                )
        # 우선순위가 낮은 root의 candidate를 섞으면 양쪽이 pending일 때 아래 모호
        # 거부에 걸려 승인이 사라진다. 하나라도 나온 root에서 멈춘다.
        if candidates:
            break

    if not candidates:
        return None
    if len(candidates) == 1:
        target = candidates[0]
        if artifact_value is None:
            return target
        return _SpecConfirmationTarget(
            run_dir=target.run_dir,
            artifact=_resolve_project_path(root, artifact_value),
            checkout_identity=target.checkout_identity,
        )
    if not interactive:
        return None
    raise ValueError(
        "pathless SPEC confirmation requires exactly one pending run in the "
        "current checkout"
    )


def _checkout_identity(inferred_worktree: str | None) -> str:
    if inferred_worktree is None:
        return "leader"
    return f"worktree:{inferred_worktree}"


def _verified_checkout_identity(*, root: Path, path: Path) -> str | None:
    """``path``에 서 있는 checkout의 증명된 identity. 증명 못 하면 ``None``.

    leader면 ``leader``, 관리형 checkout이면 ``worktree:<name>``이다. 관리 경로 밖은
    채택 기록이 지문까지 맞아야 통과한다(`adopted_worktree_parent`) — 경로만 맞는
    낡은 기록은 거절이다. 그렇지 않으면 지운 뒤 같은 자리에 다시 만든 checkout이
    앞 checkout의 런타임 상태를 물려받는다.
    """
    checkout = _registered_checkout(leader_root=root, path=path)
    if checkout is None:
        return "leader"
    managed = _managed_worktree_context(checkout)
    if managed is not None and _same_path(managed[0], root):
        return f"worktree:{managed[1]}"
    if adopted_worktree_parent(root=root, path=checkout) is None:
        return None
    try:
        registered = registered_worktree_at(root, checkout)
    except WorktreeIsolationError:
        return None
    if registered is None or registered.prunable:
        return None
    return f"worktree:{checkout.name}"


def _spec_confirmation_state_roots(
    root: Path,
    *,
    inferred_worktree: str | None,
    session_id: str | None = None,
) -> tuple[tuple[Path, str], ...]:
    """SPEC 확인 대상을 찾을 state root를 우선순위 순으로 낸다.

    호출자는 candidate가 나온 첫 root에서 멈춘다. bound checkout이 leader를
    대치하면 leader run의 승인이 사라지고, 반대로 둘을 한꺼번에 스캔하면 양쪽이
    pending일 때 모호로 판정되어 역시 무음 실패다. 순서 있는 fallback만이 형제
    worktree 추측 금지와 leader 승인을 동시에 지킨다.
    """
    if inferred_worktree is not None:
        return (
            (
                worktree_runtime_root(root=root, name=inferred_worktree),
                _checkout_identity(inferred_worktree),
            ),
        )
    try:
        binding = _bound_host_checkout(root, session_id)
    except WorktreeIsolationError as exc:
        # leader가 binding 기록 이후 바뀌었다. 여기서 leader-only로 내려가면 승인이
        # 사용자가 고른 worktree가 아니라 leader run에 기록된다 — 생략보다 나쁘다.
        # 형제 소비자(`host_write_boundary_violation`)도 같은 예외를 위반으로 올린다.
        print(
            "warning: refusing to resolve a SPEC confirmation while the leader "
            f"checkout differs from the host session binding: {_format_cli_error(exc)}",
            file=sys.stderr,
        )
        return ()
    if binding is None:
        return ((root, "leader"),)
    return (
        (
            binding.checkout.runtime_root,
            _checkout_identity(binding.checkout.name),
        ),
        (root, "leader"),
    )


def _bound_host_checkout(
    root: Path,
    session_id: str | None,
) -> HostCheckoutBinding | None:
    """leader cwd에서 도는 host 세션이 증명적으로 묶인 managed checkout.

    binding 파일이 세션↔checkout 1:1 결합의 유일한 증거다. 없으면 형제 worktree를
    추측으로 고르지 않고 leader만 본다. 승인은 load-bearing 보안 경계이므로 다른
    binding 소비자(`host_write_boundary_violation`, binding 재확인)와 같은 leader
    tripwire 검증을 통과한 binding만 쓴다 — 그 검증 실패는 호출자가 fail-closed로
    처리하도록 그대로 올린다.

    binding을 **읽지** 못하는 것은 다른 사건이라 leader-only로 내려간다. 이 사유는
    hook 경로에서는 stderr가 버려져 사용자에게 닿지 않고, `spec confirm`을 직접
    실행할 때만 보인다.
    """
    if not session_id:
        return None
    try:
        binding = bound_worktree_for_session(session_id, root)
        if binding is None:
            return None
        assert_leader_unchanged(
            root,
            binding.leader_snapshot,
            worker_root=binding.checkout.checkout,
            include_ignored=False,
        )
        return binding
    except (HostWriteBoundaryError, OSError, ValueError) as exc:
        print(
            "warning: host session binding is unusable, falling back to the "
            f"leader checkout: {_format_cli_error(exc)}",
            file=sys.stderr,
        )
        return None


def _spec_artifact_waiting_for_confirmation(
    run_dir: Path,
    *,
    pending_only: bool,
) -> Path | None:
    meta = _read_run_state(run_dir)
    phase_id = str(meta.get("current_phase") or meta.get("phase") or "")
    if phase_id not in LEDGER_SOURCE_PHASES:
        return None
    for artifact in (
        run_dir / f"{phase_id}.md",
        run_dir / "artifacts" / f"{phase_id}.md",
    ):
        if not artifact.is_file():
            continue
        parsed = parse_spec_item_section(artifact.read_text(encoding="utf-8"))
        if parsed.errors or not parsed.items:
            continue
        if pending_only and spec_set_is_confirmed(run_dir, parsed.items):
            # 한 run의 현재 SPEC artifact는 하나다. 확인된 것을 건너뛰고 다음
            # 후보로 넘어가면, agent가 두 번째 artifact를 써 두는 것만으로
            # 사용자가 이미 승인한 집합이 다른 집합으로 갈아치워진다.
            return None
        return artifact
    return None


def _spec_run_context(run_dir: Path) -> dict:
    meta = _read_run_state(run_dir)
    started_at = meta.get("started_at")
    since = _run_meta_timestamp({"started_at": started_at})
    return {
        "task_text": str(meta.get("task", "")),
        "since": since,
    }


def _active_run_meta(root: Path) -> dict:
    """Return the newest Python-authoritative active run metadata."""
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
    if not candidates:
        return {}
    dated = [(ts, meta) for meta in candidates if (ts := _run_meta_timestamp(meta)) is not None]
    if dated:
        return max(dated, key=lambda pair: pair[0])[1]
    return candidates[0]




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
