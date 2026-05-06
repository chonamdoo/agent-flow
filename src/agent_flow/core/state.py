from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from agent_flow.core.artifacts import init_project


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
    return f"{payload['workflow_id']} {payload['run_id']} {payload['status']}"


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid4().hex[:8]
