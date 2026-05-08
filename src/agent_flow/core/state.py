from __future__ import annotations

import json
import shlex
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from agent_flow.core.artifacts import init_project
from agent_flow.core.workflow import load_workflow


@dataclass(frozen=True)
class RunRequest:
    workflow_id: str
    task: str
    adapter: str
    profile: str
    architecture: str = "default"
    run_id: str | None = None
    worktree: dict[str, str] | None = None


@dataclass(frozen=True)
class RunState:
    run_id: str
    workflow_id: str
    task: str
    adapter: str
    profile: str
    architecture: str
    status: str
    created_at: str
    run_dir: Path
    worktree: dict[str, str] | None = None


def start_run(*, root: Path, request: RunRequest) -> RunState:
    init_project(root)
    run_id = request.run_id or _new_run_id()
    run_dir = root / ".agent-flow" / "runs" / request.workflow_id / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    state = RunState(
        run_id=run_id,
        workflow_id=request.workflow_id,
        task=request.task,
        adapter=request.adapter,
        profile=request.profile,
        architecture=request.architecture,
        status="running",
        created_at=_now(),
        run_dir=run_dir,
        worktree=request.worktree,
    )
    try:
        _write_json(run_dir / "manifest.json", _state_payload(state))
        _append_event(run_dir, "started", {"profile": state.profile, "adapter": state.adapter})
    except Exception:
        shutil.rmtree(run_dir)
        raise
    return state


def status_summary(root: Path) -> str:
    runs_root = root / ".agent-flow" / "runs"
    if not runs_root.exists():
        return "no runs"
    manifests = sorted(runs_root.glob("*/*/manifest.json"), key=lambda p: p.stat().st_mtime)
    if not manifests:
        return "no runs"
    payload = json.loads(manifests[-1].read_text(encoding="utf-8"))
    workflow_id = payload["workflow_id"]
    run_id = payload["run_id"]
    raw_status = payload["status"]
    task = payload.get("task", "")
    run_dir = payload.get("run_dir") or str(manifests[-1].parent)
    current_phase, required_artifact = _current_stage_status(
        workflow_id,
        Path(run_dir),
        payload.get("current_phase") or payload.get("phase"),
    )
    status = _structured_status(raw_status, required_artifact)
    reason = _reason_for_status(raw_status, required_artifact)
    next_command, next_command_template, required_action = _next_command_for_status(
        root,
        raw_status,
        payload,
        current_phase,
        run_dir,
    )
    status_payload = {
        "status": status,
        "run": f"{workflow_id}/{run_id}",
        "task": task,
        "current_phase": current_phase,
        "reason": reason,
        "run_dir": run_dir,
        "required_artifact": required_artifact,
        "next_command": next_command,
        "next_command_template": next_command_template,
        "required_action": required_action,
    }
    lines = [
        f"{workflow_id} {run_id} {status}",
        f"status: {_status_value(status)}",
        f"run: {_status_value(workflow_id + '/' + run_id)}",
        f"task: {_status_value(task)}",
        f"current_phase: {_status_value(current_phase)}",
        f"reason: {_status_value(reason)}",
        f"run_dir: {_status_value(run_dir)}",
    ]
    if required_artifact is not None:
        lines.append(f"required_artifact: {_status_value(required_artifact)}")
    lines.extend(
        [
            f"next_command: {_status_value(next_command)}",
            f"next_command_template: {_status_value(next_command_template)}",
            f"required_action: {_status_value(required_action)}",
            f"status_json: {json.dumps(status_payload, sort_keys=True)}",
        ]
    )
    return "\n".join(
        lines
    )


def _append_event(run_dir: Path, event: str, details: dict[str, str]) -> None:
    payload = {"ts": _now(), "event": event, "details": details}
    with (run_dir / "events.jsonl").open("a", encoding="utf-8") as file:
        file.write(f"{json.dumps(payload, sort_keys=True)}\n")


def _state_payload(state: RunState) -> dict[str, str]:
    payload = asdict(state)
    payload["run_dir"] = str(state.run_dir)
    if payload["worktree"] is None:
        del payload["worktree"]
    return payload


def _write_json(path: Path, payload: object) -> None:
    path.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")


def _next_command_for_status(
    root: Path,
    status: str,
    payload: dict,
    current_phase: str,
    run_dir: str,
) -> tuple[str, str, str]:
    if status in {"complete", "aborted"}:
        return "none", "none", "none"
    if current_phase != "-":
        command_template = (
            "agent-flow record-stage "
            f"--root {shlex.quote(str(root))} "
            f"--run-dir {shlex.quote(_relative_run_dir(run_dir))} "
            f"--stage {shlex.quote(current_phase)} "
            "--content '<stage result>'"
        )
        return "none", command_template, "write_stage_artifact"
    worktree = payload.get("worktree")
    if isinstance(worktree, dict) and worktree.get("name"):
        command = (
            f"agent-flow continue --root {shlex.quote(str(root))} "
            f"--worktree {shlex.quote(str(worktree['name']))}"
        )
        return command, command, "continue"
    command = f"agent-flow continue --root {shlex.quote(str(root))}"
    return command, command, "continue"


def _structured_status(status: str, required_artifact: str | None) -> str:
    if status == "running" and required_artifact is not None:
        return "awaiting_host"
    return status


def _reason_for_status(status: str, required_artifact: str | None) -> str:
    if status == "complete":
        return "workflow_complete"
    if status == "aborted":
        return "aborted"
    if required_artifact is not None:
        return "missing_stage_artifact"
    return "in_progress"


def _current_stage_status(
    workflow_id: str,
    run_dir: Path,
    manifest_phase: object,
) -> tuple[str, str | None]:
    if isinstance(manifest_phase, str) and manifest_phase:
        artifact = run_dir / "artifacts" / f"{manifest_phase}.md"
        return manifest_phase, str(artifact)
    try:
        workflow = load_workflow(workflow_id)
    except (OSError, ValueError):
        return "-", None
    for artifact_id in _expected_artifact_ids(workflow):
        artifact = run_dir / "artifacts" / f"{artifact_id}.md"
        if not artifact.exists():
            return artifact_id, str(artifact)
    return "-", None


def _expected_artifact_ids(workflow) -> list[str]:
    artifact_ids: list[str] = []
    for stage in workflow.stages:
        count = stage.replicas if stage.parallel else 1
        for replica in range(1, count + 1):
            artifact_ids.append(stage.stage_id if count == 1 else f"{stage.stage_id}-{replica}")
    return artifact_ids


def _relative_run_dir(run_dir: str) -> str:
    path = Path(run_dir)
    parts = path.parts
    marker = ".agent-flow"
    if marker in parts:
        index = parts.index(marker)
        return str(Path(*parts[index:]))
    return run_dir


def _status_value(value: object) -> str:
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid4().hex[:8]
