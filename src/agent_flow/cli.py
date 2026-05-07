from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
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
from agent_flow.core.gates import GateCommand, run_gates
from agent_flow.core.profiles import detect_profile, load_profile
from agent_flow.core.review import summarize_reviews, write_review_summary
from agent_flow.core.report import write_run_report
from agent_flow.core.query import explain_run, query_run
from agent_flow.core.security import resolve_project_path
from agent_flow.core.watch import write_watch_snapshot
from agent_flow.core.team import (
    acknowledge_shutdown,
    add_task,
    add_worker,
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
    team_status,
    update_worker_heartbeat,
    validate_team_state_import,
)
from agent_flow.core.worktrees import (
    create_worktree,
    get_worktree_status,
    plan_worktree,
    remove_worktree,
    worktree_branch_exists,
)
from agent_flow.core.state import RunRequest, RunState, start_run, status_summary
from agent_flow.core.workflow import load_workflow
from agent_flow.artifact import find_active_run, mark_inactive
from agent_flow.runner import Runner, ResumeMode
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
    root = Path(getattr(args, "root", ".")).resolve()

    if args.command == "init":
        init_project(root)
        print(f"initialized {root / '.agent-flow'}")
        return 0

    if args.command == "run":
        run_root = root
        worktree_status = None
        worktree_preexisting = False
        if args.worktree is not None:
            if not _is_git_repo(root):
                print("worktree runs require a git repository", file=sys.stderr)
                return 2
            try:
                plan = plan_worktree(root=root, name=args.worktree, branch=args.worktree_branch)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            worktree_preexisting = plan.path.exists()
            if plan.path.exists():
                active = find_active_run(plan.path)
                if active is not None:
                    print(f"already active: {active.run_id} (task: {active.task!r})")
                    return 2
            try:
                worktree_status = create_worktree(root=root, plan=plan, allow_dirty=args.allow_dirty)
            except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
                print(_format_cli_error(exc), file=sys.stderr)
                return 2
            print(f"worktree: {worktree_status.name} {worktree_status.path}")
            run_root = worktree_status.path
        active = find_active_run(run_root) if args.worktree is None else None
        if active is not None:
            print(f"already active: {active.run_id} (task: {active.task!r})")
            if args.worktree is None and _is_git_repo(root):
                print(
                    "parallel worktree run: "
                    'agent-flow run "<task>" --worktree "<short-task-slug>"'
                )
            return 2
        try:
            Runner(run_root, workflow=args.workflow, architecture=args.architecture).run(
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
            run_root = _worktree_root(root, args.worktree) if args.worktree else root
        except ValueError as exc:
            print(_format_cli_error(exc), file=sys.stderr)
            return 2
        if run_root is None:
            return 1
        active = find_active_run(run_root)
        if active is None:
            if args.worktree:
                print(
                    f'진행 중인 run 없음. `agent-flow run "<task>" '
                    f'--worktree "{_slug_for_hint(root, args.worktree)}"`로 시작하세요.'
                )
            else:
                print('진행 중인 run 없음. `agent-flow run "<task>"`로 시작하세요.')
            return 0
        Runner(run_root, run_dir=active.path).run(mode=ResumeMode.RESUME)
        return 0

    if args.command == "abort":
        try:
            run_root = _worktree_root(root, args.worktree) if args.worktree else root
        except ValueError as exc:
            print(_format_cli_error(exc), file=sys.stderr)
            return 2
        if run_root is None:
            return 1
        active = find_active_run(run_root)
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
            run_root = _worktree_root(root, args.worktree) if args.worktree else root
        except ValueError as exc:
            print(_format_cli_error(exc), file=sys.stderr)
            return 2
        if run_root is None:
            return 1
        active = find_active_run(run_root)
        if active is not None:
            active.print_status()
            return 0
        if not (run_root / ".agent-flow" / "runs").exists():
            print("진행 중인 run 없음.")
            return 0
        print(status_summary(run_root))
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
        profile_id = detect_profile(root) if args.profile == "auto" else args.profile
        profile = load_profile(profile_id)
        commands = [GateCommand(gate.gate_id, gate.command) for gate in profile.gates]
        results = run_gates(commands, cwd=root, timeout_s=args.timeout)
        if args.run_dir is not None:
            write_gate_results(run_dir=_resolve_project_path(root, args.run_dir), results=results)
        failed = [result for result in results if not result.passed]
        print(f"{profile.profile_id}: {len(results) - len(failed)}/{len(results)} gates passed")
        return 1 if failed else 0

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
                if stale_dir.exists():
                    if not args.keep_branch and status.branch_created_by_agent_flow:
                        try:
                            subprocess.run(
                                ("git", "worktree", "prune"),
                                cwd=root,
                                text=True,
                                capture_output=True,
                                check=True,
                            )
                            if worktree_branch_exists(root=root, branch=status.branch):
                                subprocess.run(
                                    ("git", "branch", "-D", status.branch),
                                    cwd=root,
                                    text=True,
                                    capture_output=True,
                                    check=True,
                                )
                        except (OSError, subprocess.CalledProcessError) as exc:
                            print(_format_cli_error(exc), file=sys.stderr)
                            return 2
                    if stale_dir.is_dir():
                        shutil.rmtree(stale_dir)
                    else:
                        stale_dir.unlink()
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
            claimed = claim_task(
                root=root,
                team_name=args.team,
                task_id=pending.task_id,
                worker_name=args.worker,
            )
            prompt = f"{claimed.subject}\n\n{claimed.description}\n"
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
        if args.worktree is not None and not _is_git_repo(root):
            print("worktree runs require a git repository", file=sys.stderr)
            return 2
        try:
            workflow = load_workflow(args.workflow)
            profile = detect_profile(root) if args.profile == "auto" else args.profile
            adapter = detect_adapter() if args.adapter == "auto" else args.adapter
            if args.worktree is not None:
                plan = plan_worktree(root=root, name=args.worktree, branch=args.worktree_branch)
                worktree_preexisting = plan.path.exists()
                status = create_worktree(root=root, plan=plan, allow_dirty=args.allow_dirty)
                worktree_status = status
                worktree = {
                    "name": status.name,
                    "branch": status.branch,
                    "path": str(status.path),
                }
            state_root = Path(worktree["path"]) if worktree is not None else root
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


def _resolve_project_path(root: Path, value: str) -> Path:
    return resolve_project_path(root, value)


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


def _known_worktree_names(root: Path) -> list[str]:
    worktrees_root = root / ".agent-flow" / "worktrees"
    if not worktrees_root.exists():
        return []
    return sorted(path.name for path in worktrees_root.iterdir() if path.is_dir())


def _worktree_checkout_exists(status) -> bool:
    return status.exists and (status.path / ".git").exists()


def _format_cli_error(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        return detail or str(exc)
    return str(exc)


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


def _is_git_repo(root: Path) -> bool:
    result = subprocess.run(
        ("git", "rev-parse", "--git-dir"),
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


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
