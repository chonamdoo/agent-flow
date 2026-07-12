from __future__ import annotations

import json
import re
import shlex
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_flow.core.artifacts import init_project
from agent_flow.core.local_skills import LOCAL_SKILL_PLAN_HASH_VERSION
from agent_flow.core.lore_snapshot import lore_snapshot_metadata, reconcile_lore_snapshot
from agent_flow.core.skill_plan import installed_skill_plan_pin, reconcile_skill_plan_pin
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
    config_root: Path | None = None
    worktree_mode: str = "required"


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
    skill_plan_hash: str | None = None
    skill_plan_hash_version: int = 2
    local_skill_plan_hash: str | None = None
    local_skill_plan_hash_version: int = LOCAL_SKILL_PLAN_HASH_VERSION
    lore_snapshot_version: int = 1
    lore_snapshot_hash: str = ""
    lore_citations: tuple[dict[str, Any], ...] = ()
    worktree_mode: str = "required"


def start_run(*, root: Path, request: RunRequest) -> RunState:
    config_root = request.config_root or _snapshot_config_root(root)
    skill_plan_pin = installed_skill_plan_pin(config_root)
    lore_snapshot = lore_snapshot_metadata(config_root, request.task)
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
        worktree_mode=request.worktree_mode,
        skill_plan_hash=skill_plan_pin.get("skill_plan_hash"),
        skill_plan_hash_version=skill_plan_pin.get("skill_plan_hash_version", 2),
        local_skill_plan_hash=skill_plan_pin.get("local_skill_plan_hash"),
        local_skill_plan_hash_version=skill_plan_pin.get(
            "local_skill_plan_hash_version",
            LOCAL_SKILL_PLAN_HASH_VERSION,
        ),
        lore_snapshot_version=lore_snapshot["lore_snapshot_version"],
        lore_snapshot_hash=lore_snapshot["lore_snapshot_hash"],
        lore_citations=tuple(lore_snapshot["lore_citations"]),
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
    manifest = None
    payload = None
    for candidate in reversed(manifests):
        try:
            candidate_payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if all(candidate_payload.get(key) for key in ("workflow_id", "run_id", "status")):
            manifest = candidate
            payload = candidate_payload
            break
    if manifest is None or payload is None:
        return "no runs"
    if payload.get("status") not in {"complete", "aborted"}:
        payload = _verify_manifest_snapshots(root, manifest, payload)
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
    required_artifact = _relative_run_dir(required_artifact) if required_artifact is not None else None
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
    return "\n".join(
        lines
    )


def verify_run_state_snapshots(
    *,
    state_root: Path,
    run_dir: Path,
    config_root: Path | None = None,
    prompt_root: Path | None = None,
) -> dict[str, Any]:
    """Verify a manifest immediately before a compatibility-runner prompt."""
    manifest = run_dir / "manifest.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"blocked: run manifest is unreadable: {manifest}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"blocked: run manifest must be a JSON object: {manifest}")
    return _verify_manifest_snapshots(
        state_root,
        manifest,
        payload,
        config_root=config_root,
        prompt_root=prompt_root,
    )


def _verify_manifest_snapshots(
    state_root: Path,
    manifest: Path,
    payload: dict[str, Any],
    *,
    config_root: Path | None = None,
    prompt_root: Path | None = None,
) -> dict[str, Any]:
    resolved_config_root = config_root or _snapshot_config_root(state_root)
    if not all(
        isinstance(payload.get(key), str) and payload[key]
        for key in ("run_id", "workflow_id")
    ):
        raise RuntimeError(
            "blocked: legacy run manifest is missing its identity; restore manifest.json"
        )
    prompt_emitted = any((manifest.parent / "prompts").glob("*.md"))
    legacy_skill_pin = (
        not payload.get("skill_plan_hash")
        or payload.get("skill_plan_hash_version") != 2
        or not payload.get("local_skill_plan_hash")
        or payload.get("local_skill_plan_hash_version")
        != LOCAL_SKILL_PLAN_HASH_VERSION
    )
    if legacy_skill_pin and prompt_emitted:
        current_pin = installed_skill_plan_pin(resolved_config_root)
        if current_pin:
            raise RuntimeError(
                "blocked: a legacy run already emitted stage prompts; "
                "its original skill snapshot cannot be reconstructed safely"
            )
    updated, skill_changed = reconcile_skill_plan_pin(payload, resolved_config_root)
    updated, _citations, lore_changed = reconcile_lore_snapshot(
        updated,
        resolved_config_root,
        prompt_root or resolved_config_root,
        allow_migration=not prompt_emitted,
    )
    if skill_changed or lore_changed:
        _write_json(manifest, updated)
    return updated


def _snapshot_config_root(state_root: Path) -> Path:
    direct_index = state_root / ".agent-flow" / "skills" / "index.json"
    if direct_index.exists():
        return state_root
    for candidate in (state_root, *state_root.parents):
        if candidate.name == ".git":
            leader = candidate.parent
            if (leader / ".agent-flow" / "skills" / "index.json").exists():
                return leader
    return state_root


def _append_event(run_dir: Path, event: str, details: dict[str, str]) -> None:
    payload = {"ts": _now(), "event": event, "details": details}
    with (run_dir / "events.jsonl").open("a", encoding="utf-8") as file:
        file.write(f"{json.dumps(payload, sort_keys=True)}\n")


def _state_payload(state: RunState) -> dict[str, Any]:
    payload = asdict(state)
    payload["run_dir"] = str(Path(".agent-flow") / "runs" / state.workflow_id / state.run_id)
    if isinstance(payload.get("worktree"), dict):
        worktree = dict(payload["worktree"])
        raw_path = worktree.get("path")
        if raw_path:
            worktree["path"] = _safe_relative_path(str(raw_path))
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
    tmp.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
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
            "agent-flow-python record-stage "
            f"--root {shlex.quote(str(root))} "
            f"--run-dir {shlex.quote(_relative_run_dir(run_dir))} "
            f"--stage {shlex.quote(current_phase)} "
            "--content '<stage result>'"
        )
        return "none", command_template, "write_stage_artifact"
    worktree = payload.get("worktree")
    if isinstance(worktree, dict) and worktree.get("name"):
        command = (
            f"agent-flow-python continue --root {shlex.quote(str(root))} "
            f"--worktree {shlex.quote(str(worktree['name']))}"
        )
        return command, command, "continue"
    command = f"agent-flow-python continue --root {shlex.quote(str(root))}"
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


def _safe_relative_path(path: str) -> str:
    rel_path = _relative_run_dir(path)
    if Path(rel_path).is_absolute() or re.match(r"^[A-Za-z]:[\\/]", rel_path):
        return Path(path.replace("\\", "/")).name or "worktree"
    return rel_path


def _status_value(value: object) -> str:
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid4().hex[:8]
