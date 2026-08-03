from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from agent_flow.core.artifacts import init_project
from agent_flow.core.runtime_binding import bind_run_runtime, unbind_run_runtime
from agent_flow.core.workflow import load_workflow


@dataclass(frozen=True)
class RunRequest:
    workflow_id: str
    task: str
    adapter: str
    profile: str
    hook_runtime_digest: str
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
    hook_runtime_digest: str
    status: str
    created_at: str
    run_dir: Path
    worktree: dict[str, str] | None = None


def start_run(
    *, root: Path, request: RunRequest, project_root: Path | None = None
) -> RunState:
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
        hook_runtime_digest=request.hook_runtime_digest,
        status="running",
        created_at=_now(),
        run_dir=run_dir,
        worktree=request.worktree,
    )
    try:
        bind_run_runtime(run_dir, request.hook_runtime_digest)
        # `root`는 worktree run에서 git-private state root다. checkout 경로는 그
        # 기준으로 적으면 `../../../..` 체인이 되므로 leader 프로젝트 루트로 적는다.
        _write_json(
            run_dir / "manifest.json",
            _state_payload(state, root=project_root or root),
        )
        _append_event(
            run_dir, "started", {"profile": state.profile, "adapter": state.adapter}
        )
    except Exception:
        unbind_run_runtime(run_dir)
        shutil.rmtree(run_dir)
        raise
    return state


def status_summary(root: Path) -> str:
    runs_root = root / ".agent-flow" / "runs"
    if not runs_root.exists():
        return "no runs"
    manifests = sorted(
        runs_root.glob("*/*/manifest.json"), key=lambda p: p.stat().st_mtime
    )
    if not manifests:
        return "no runs"
    manifest = None
    payload = None
    for candidate in reversed(manifests):
        try:
            candidate_payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if all(
            candidate_payload.get(key) for key in ("workflow_id", "run_id", "status")
        ):
            manifest = candidate
            payload = candidate_payload
            break
    if manifest is None or payload is None:
        return "no runs"
    workflow_id = payload["workflow_id"]
    run_id = payload["run_id"]
    raw_status = payload["status"]
    task = payload.get("task", "")
    raw_run_dir = payload.get("run_dir") or str(manifest.parent)
    resolved_run_dir = _resolve_run_dir(root, raw_run_dir)
    run_dir = _relative_run_dir(str(resolved_run_dir))
    current_phase, required_artifact = _current_stage_status(
        workflow_id,
        resolved_run_dir,
        payload.get("current_phase") or payload.get("phase"),
    )
    required_artifact = (
        _relative_run_dir(required_artifact) if required_artifact is not None else None
    )
    status = _structured_status(raw_status, required_artifact)
    reason = _reason_for_status(raw_status, required_artifact)
    next_command, next_command_template, required_action = _next_command_for_status(
        root,
        raw_status,
        payload,
        current_phase,
        raw_run_dir,
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
    return "\n".join(lines)


def _append_event(run_dir: Path, event: str, details: dict[str, str]) -> None:
    payload = {"ts": _now(), "event": event, "details": details}
    with (run_dir / "events.jsonl").open("a", encoding="utf-8") as file:
        file.write(f"{json.dumps(payload, sort_keys=True)}\n")


def _state_payload(state: RunState, *, root: Path | None = None) -> dict[str, str]:
    payload = asdict(state)
    del payload["hook_runtime_digest"]
    # design-spec.md의 task digest와 대조된다. `artifact.create_run`과 같은 계약이다.
    payload["task_digest"] = hashlib.sha256(
        state.task.strip().encode("utf-8")
    ).hexdigest()
    payload["run_dir"] = str(
        Path(".agent-flow") / "runs" / state.workflow_id / state.run_id
    )
    if isinstance(payload.get("worktree"), dict):
        worktree = dict(payload["worktree"])
        raw_path = worktree.get("path")
        if raw_path:
            worktree["path"] = _safe_relative_path(str(raw_path), root=root)
        payload["worktree"] = worktree
    if payload["worktree"] is None:
        del payload["worktree"]
    return payload


def _resolve_run_dir(root: Path, run_dir: str) -> Path:
    path = Path(run_dir)
    return path if path.is_absolute() else root / path


def _write_json(path: Path, payload: object) -> None:
    # 중단(Ctrl-C 등) 시 manifest가 반쯤 쓰인 채 남지 않도록 원자적으로 교체한다.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8"
    )
    tmp.replace(path)


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
        if not artifact.exists():
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
            artifact_ids.append(
                stage.stage_id if count == 1 else f"{stage.stage_id}-{replica}"
            )
    return artifact_ids


def _relative_run_dir(run_dir: str) -> str:
    path = Path(run_dir)
    parts = path.parts
    marker = ".agent-flow"
    if marker in parts:
        index = parts.index(marker)
        return str(Path(*parts[index:]))
    return run_dir


def _is_absolute_path_text(value: str) -> bool:
    return bool(Path(value).is_absolute() or re.match(r"^[A-Za-z]:[\\/]", value))


def _safe_relative_path(path: str, *, root: Path | None = None) -> str:
    """상태 파일에 호스트 절대 경로를 남기지 않으면서 어디인지는 잃지 않는다.

    checkout이 leader 밖(현재 기본 자리)이면 leader 기준 상대 경로가 되므로 `..`로
    시작한다. 그마저 불가능할 때만 이름으로 내려간다 — 이름만 남으면 진단에서
    "어디에 있었는가"가 사라진다.

    판정은 **원본** 경로로 한다. `_relative_run_dir`를 먼저 태우면 프로젝트가
    `.agent-flow`를 포함한 경로에 있을 때(`/x/.agent-flow/repos/app`) 절대 경로가
    거기서 잘려 leader 아래의 없는 자리를 가리킨다.
    """
    if _is_absolute_path_text(path):
        if root is not None:
            try:
                # 양쪽을 realpath로 맞춘다. macOS의 `/var` -> `/private/var`처럼 한쪽만
                # 심링크를 지나면 상대 경로가 엉뚱한 자리를 가리킨다.
                return os.path.relpath(os.path.realpath(path), os.path.realpath(root))
            except (OSError, ValueError):
                pass
        trimmed = _relative_run_dir(path)
        if not _is_absolute_path_text(trimmed):
            return trimmed
        return Path(path.replace("\\", "/")).name or "worktree"
    return _relative_run_dir(path)


def _status_value(value: object) -> str:
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid4().hex[:8]
