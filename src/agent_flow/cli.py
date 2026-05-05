from __future__ import annotations

import argparse
import json
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
from agent_flow.core.team import (
    acknowledge_shutdown,
    add_task,
    add_worker,
    claim_task,
    complete_task,
    export_team_state,
    fail_task,
    init_team,
    list_messages,
    mark_message_read,
    request_shutdown,
    send_message,
    summarize_team_state_import,
    team_status,
    update_worker_heartbeat,
    validate_team_state_import,
)
from agent_flow.core.worktrees import create_worktree, get_worktree_status, plan_worktree
from agent_flow.core.state import RunRequest, RunState, start_run, status_summary
from agent_flow.core.workflow import load_workflow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-flow")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--root", default=".")

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("workflow")
    start_parser.add_argument("--root", default=".")
    start_parser.add_argument("--task", required=True)
    start_parser.add_argument("--run-id")
    start_parser.add_argument("--adapter", default="auto")
    start_parser.add_argument("--profile", default="auto")

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--root", default=".")

    detect_parser = subparsers.add_parser("detect-profile")
    detect_parser.add_argument("--root", default=".")

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

    team_parser = subparsers.add_parser("team")
    team_subparsers = team_parser.add_subparsers(dest="team_command", required=True)
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
    team_import_validate = team_subparsers.add_parser("import-validate")
    team_import_validate.add_argument("--file", required=True)
    team_import_dry_run = team_subparsers.add_parser("import-dry-run")
    team_import_dry_run.add_argument("--file", required=True)
    team_import_dry_run.add_argument("--report")

    args = parser.parse_args(argv)
    root = Path(getattr(args, "root", ".")).resolve()

    if args.command == "init":
        init_project(root)
        print(f"initialized {root / '.agent-flow'}")
        return 0

    if args.command == "detect-profile":
        print(detect_profile(root))
        return 0

    if args.command == "status":
        print(status_summary(root))
        return 0

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
            plan = plan_worktree(root=root, name=args.name, branch=args.branch)
            status = create_worktree(root=root, plan=plan, allow_dirty=args.allow_dirty)
            print(f"{status.name} {status.branch} {status.path}")
            return 0
        if args.worktree_command == "status":
            status = get_worktree_status(root=root, name=args.name)
            state = "exists" if status.exists else "missing"
            print(f"{status.name} {status.branch} {status.path} {state}")
            return 0

    if args.command == "team":
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

    if args.command == "start":
        workflow = load_workflow(args.workflow)
        profile = detect_profile(root) if args.profile == "auto" else args.profile
        adapter = detect_adapter() if args.adapter == "auto" else args.adapter
        state = start_run(
            root=root,
            request=RunRequest(
                workflow_id=workflow.workflow_id,
                task=args.task,
                adapter=adapter,
                profile=profile,
                run_id=args.run_id,
            ),
        )
        _write_stage_prompts(root=root, state=state, workflow=workflow)
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
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
