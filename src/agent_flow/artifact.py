"""Artifact directory management.

Each run lives under the active state root's `.agent-flow/runs/<run-id>/`.
Git worktree runs use a git-private state root so runtime files do not dirty
the worktree checkout. Phases write a single .md artifact when they complete;
the runner uses these files as the source of truth for "what has been done."
An `active` marker file distinguishes the in-flight run.

Robustness fixes applied (post-review):
  - read_meta tolerates missing / malformed JSON (returns {} with warning)
  - write_meta is atomic (tmpfile + os.replace) so Ctrl-C mid-write cannot
    leave a half-written meta.json that crashes the next invocation
  - create_run refuses if an active run already exists (concurrent-run race)
  - create_run produces unique run_ids with a counter suffix when timestamp
    collision occurs (sub-second double-invocation)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from agent_flow.core.markers import has_failure_markers, missing_markers, normalize_required_markers
from agent_flow.core.local_skills import (
    LOCAL_SKILL_PLAN_HASH_VERSION,
    missing_local_skill_markers,
)
from agent_flow.core.phase_workflow import find_kit_root, load_phase_workflow_definition
from agent_flow.core.skill_plan import (
    installed_skill_plan_pin,
    reconcile_skill_plan_pin,
    runtime_changed_files,
)


RUNS_DIRNAME = ".agent-flow/runs"
ACTIVE_MARKER = "active"
META_FILE = "meta.json"
ACTIVE_LOCK = "active.lock"


class ActiveRunExists(RuntimeError):
    """Raised when create_run is called but an active run already exists."""


@dataclass
class ActiveRun:
    path: Path
    run_id: str
    workflow: str
    task: str
    started_at: str

    def print_status(
        self,
        *,
        next_command: str = "agent-flow-python continue",
        config_root: Path | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        status_config_root = config_root or self.path.parents[2]
        meta, _changed = _reconciled_run_skill_plan(
            self.path,
            status_config_root,
        )
        resolved_workspace = (workspace_root or status_config_root).resolve()
        artifacts = sorted(
            str(path.relative_to(self.path))
            for path in self.path.rglob("*")
            if path.is_file()
            and path.relative_to(self.path).parts[:2]
            != ("artifacts", "execution-ledger")
        )
        current_phase = meta.get("current_phase") or "-"
        contract = _phase_status_contract(
            self.path,
            self.workflow,
            current_phase,
            meta=meta,
        )
        required_artifact = (
            _existing_phase_artifact(self.path, current_phase, contract.artifact)
            if current_phase != "-" and contract is not None
            else None
        )
        structured_status = "running"
        reason = "in_progress"
        if required_artifact is not None and not required_artifact.exists():
            structured_status = "awaiting_host"
            reason = "missing_phase_artifact"
        elif required_artifact is not None:
            structured_status = "blocked"
            if _artifact_is_stale(required_artifact, meta):
                reason = "stale_artifact"
            else:
                stub_reason = _artifact_block_reason(required_artifact)
                if stub_reason:
                    reason = stub_reason
                else:
                    missing = _missing_completion_markers_for_contract(
                        self.path,
                        current_phase,
                        contract,
                        config_root=status_config_root,
                        task_scope=str(meta.get("task") or self.task),
                        changed_files=runtime_changed_files(
                            status_config_root,
                            resolved_workspace,
                            meta.get("base_commit")
                            if isinstance(meta.get("base_commit"), str)
                            else None,
                        ),
                    )
                    if missing:
                        reason = "missing_completion_markers"
                    elif contract.pause_after and not _pause_after_approval_matches(
                        meta,
                        current_phase,
                        required_artifact,
                    ):
                        reason = (
                            "pause_after"
                            if _pause_after_pending_matches(
                                meta,
                                current_phase,
                                required_artifact,
                            )
                            else "phase_artifact_written_advance_required"
                        )
                    elif _route_is_blocked(contract, required_artifact, meta):
                        reason = "route_blocked"
                    else:
                        reason = "phase_artifact_written_advance_required"
        status_next_command = (
            _pause_approval_command(next_command)
            if reason == "pause_after"
            else next_command
        )
        payload = {
            "status": structured_status,
            "run": f"{self.workflow}/{self.run_id}",
            "task": self.task,
            "current_phase": current_phase,
            "reason": reason,
            "required_artifact": str(required_artifact) if required_artifact is not None else None,
            "report": None,
            "next_command": status_next_command,
            "workspace_root": str(resolved_workspace),
        }
        print(f"Run id     : {self.run_id}")
        print(f"Workflow   : {self.workflow}")
        print(f"Task       : {_status_value(self.task)}")
        print(f"Started at : {self.started_at}")
        print(f"Phase      : {current_phase}")
        print(f"Artifacts  : {len(artifacts)} written")
        for a in artifacts:
            print(f"  - {a}")
        print(f"status: {_status_value(structured_status)}")
        print(f"run: {_status_value(payload['run'])}")
        print(f"task: {_status_value(self.task)}")
        print(f"current_phase: {_status_value(current_phase)}")
        print(f"workspace_root: {_status_value(resolved_workspace)}")
        print(
            "work_cwd_policy: keep every source, build, test, and write tool call "
            f"in {resolved_workspace}; transition commands may run from the leader checkout"
        )
        print(f"reason: {_status_value(reason)}")
        if required_artifact is not None:
            print(f"required_artifact: {_status_value(required_artifact)}")
        print(f"next_command: {_status_value(status_next_command)}")
        print(f"status_json: {json.dumps(payload, sort_keys=True)}")


def find_active_run(project_root: Path) -> ActiveRun | None:
    agent_flow_dir = project_root / ".agent-flow"
    if not agent_flow_dir.exists() and not agent_flow_dir.is_symlink():
        return None
    if agent_flow_dir.is_symlink() or not agent_flow_dir.is_dir():
        raise RuntimeError(f"blocked: unsafe Python state root: {agent_flow_dir}")
    runs_dir = agent_flow_dir / "runs"
    if not runs_dir.exists() and not runs_dir.is_symlink():
        return None
    if runs_dir.is_symlink() or not runs_dir.is_dir():
        raise RuntimeError(f"blocked: unsafe Python runs root: {runs_dir}")
    try:
        runs_dir.resolve().relative_to(project_root.resolve())
    except ValueError as exc:
        raise RuntimeError(
            f"blocked: Python runs root escapes its state root: {runs_dir}"
        ) from exc
    resolved_runs_dir = runs_dir.resolve()
    actives: list[Path] = []
    for candidate in sorted(runs_dir.iterdir(), key=lambda path: path.name):
        if candidate.is_symlink():
            raise RuntimeError(f"blocked: unsafe Python run root: {candidate}")
        if candidate.is_file():
            continue
        if not candidate.is_dir():
            raise RuntimeError(f"blocked: unsafe Python run root: {candidate}")
        try:
            candidate.resolve().relative_to(resolved_runs_dir)
        except ValueError as exc:
            raise RuntimeError(
                f"blocked: Python run root escapes its state root: {candidate}"
            ) from exc
        marker = candidate / ACTIVE_MARKER
        if not marker.exists() and not marker.is_symlink():
            continue
        if marker.is_symlink() or not marker.is_file():
            raise RuntimeError(f"blocked: unsafe Python active marker: {marker}")
        actives.append(candidate)
    if not actives:
        return None
    if len(actives) > 1:
        names = ", ".join(p.name for p in actives)
        raise RuntimeError(
            f"blocked: multiple active Python runs found: {names}; "
            "resolve the conflicting active markers before continuing"
        )
    chosen = actives[0]
    meta = read_meta(chosen)
    return ActiveRun(
        path=chosen,
        run_id=chosen.name,
        workflow=meta.get("workflow", "unknown"),
        task=meta.get("task", ""),
        started_at=meta.get("started_at", ""),
    )


def create_run(
    project_root: Path,
    workflow: str,
    task: str,
    *,
    architecture: str | None = None,
    config_root: Path | None = None,
    initial_meta: dict | None = None,
) -> Path:
    """Create a new run directory. Refuses if an active run exists."""
    runs_dir = project_root / RUNS_DIRNAME
    runs_dir.mkdir(parents=True, exist_ok=True)
    lock_dir = runs_dir / ACTIVE_LOCK
    try:
        lock_dir.mkdir()
    except FileExistsError as e:
        raise ActiveRunExists(
            "another agent-flow run is starting. Retry after it finishes "
            "or inspect `.agent-flow/runs/active.lock` if the process died."
        ) from e

    try:
        existing = find_active_run(project_root)
        if existing is not None:
            raise ActiveRunExists(
                f"active run already exists: {existing.run_id} "
                f"(task: {existing.task!r}). Use `agent-flow-python continue` to resume "
                f"or `agent-flow-python abort` to clear."
            )

        now = datetime.now(timezone.utc)
        base_id = now.strftime("%Y%m%d-%H%M%S")
        run_id = base_id
        suffix = 1
        while (runs_dir / run_id).exists():
            run_id = f"{base_id}-{suffix}"
            suffix += 1

        skill_plan_pin = installed_skill_plan_pin(config_root or project_root)
        meta = {
            "run_id": run_id,
            "workflow": workflow,
            "task": task,
            "started_at": now.isoformat(),
            "current_phase": None,
        }
        if architecture:
            meta["architecture"] = architecture
        if initial_meta:
            for key, value in initial_meta.items():
                if key not in meta:
                    meta[key] = value
        meta.update(skill_plan_pin)
        run_path = runs_dir / run_id
        run_path.mkdir()
        write_meta(run_path, meta)
        (run_path / ACTIVE_MARKER).write_text("")
        return run_path
    finally:
        try:
            lock_dir.rmdir()
        except OSError:
            pass


def read_meta(run_path: Path) -> dict:
    """Read meta.json tolerantly. Returns {} on missing/malformed.

    The runner depends on this for status / resume; a parse error must not
    block recovery. Surface the corruption to stderr so the user sees it.
    """
    meta_path = run_path / META_FILE
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(
            f"⚠️  meta.json at {meta_path} is unreadable ({e}); "
            f"treating as empty. Use `agent-flow-python abort` to clear if needed.",
            file=sys.stderr,
        )
        return {}


def write_meta(run_path: Path, meta: dict) -> None:
    """Atomic write: tmpfile + os.replace. Survives Ctrl-C mid-write.

    On disk-full or other write failure, the tmpfile is unlinked so we
    don't leave `meta.json.tmp` cruft for the next run to wonder about.
    """
    target = run_path / META_FILE
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(meta, indent=2))
        os.replace(tmp, target)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def ensure_run_skill_plan_pinned(run_path: Path, config_root: Path) -> dict:
    """Verify the active run's v2 skill pin and atomically migrate legacy meta."""
    updated, changed = _reconciled_run_skill_plan(run_path, config_root)
    if changed:
        write_meta(run_path, updated)
    return updated


def _reconciled_run_skill_plan(run_path: Path, config_root: Path) -> tuple[dict, bool]:
    """Validate a run pin and return any migration in memory for read-only callers."""
    meta = read_meta(run_path)
    updated, changed = reconcile_skill_plan_pin(meta, config_root)
    if changed:
        if not all(isinstance(meta.get(key), str) and meta[key] for key in ("run_id", "workflow")):
            raise RuntimeError(
                "blocked: legacy run metadata is missing its identity; "
                "restore meta.json before migrating the skill plan pin"
            )
        legacy_pin = (
            not meta.get("skill_plan_hash")
            or meta.get("skill_plan_hash_version") != 2
            or not meta.get("local_skill_plan_hash")
            or meta.get("local_skill_plan_hash_version")
            != LOCAL_SKILL_PLAN_HASH_VERSION
        )
        if legacy_pin and (
            meta.get("current_phase") not in (None, "")
            or int(meta.get("phase_index", 0) or 0) != 0
        ):
            raise RuntimeError(
                "blocked: a legacy run already emitted a phase prompt; "
                "its original skill snapshot cannot be reconstructed safely"
            )
    return updated, changed


def mark_inactive(run_path: Path) -> None:
    marker = run_path / ACTIVE_MARKER
    if marker.exists():
        marker.unlink()


def has_artifact(run_path: Path, phase_id: str) -> bool:
    meta = read_meta(run_path)
    workflow = str(meta.get("workflow") or "full-feature")
    artifact_rel, _markers = _phase_contract(run_path, workflow, phase_id)
    return _existing_phase_artifact(run_path, phase_id, artifact_rel).exists()


def _missing_completion_markers(
    run_path: Path,
    workflow: str,
    phase_id: str,
    *,
    config_root: Path | None = None,
    task_scope: str = "",
    changed_files: tuple[str, ...] = (),
) -> list[str]:
    artifact_rel, markers = _phase_contract(run_path, workflow, phase_id)
    artifact = _existing_phase_artifact(run_path, phase_id, artifact_rel)
    if not artifact.exists():
        return []
    text = artifact.read_text(encoding="utf-8")
    missing = _missing_markers(text, markers) if markers else []
    missing.extend(
        missing_local_skill_markers(
            text,
            config_root or run_path,
            phase_id,
            task_scope,
            changed_files,
        )
    )
    return missing


def _required_markers(run_path: Path, workflow: str, phase_id: str) -> tuple[str, ...]:
    _artifact_rel, markers = _phase_contract(run_path, workflow, phase_id)
    return markers


def _phase_contract(run_path: Path, workflow: str, phase_id: str) -> tuple[Path | None, tuple[str, ...]]:
    contract = _phase_status_contract(run_path, workflow, phase_id)
    if contract is None:
        return (Path(f"{phase_id}.md") if phase_id != "-" else None, ())
    return contract.artifact, contract.required_markers


@dataclass(frozen=True)
class _PhaseStatusContract:
    id: str
    artifact: Path
    required_markers: tuple[str, ...]
    pause_after: bool
    multi_review: bool
    routes: dict[str, str] | None


def _phase_status_contract(
    run_path: Path,
    workflow: str,
    phase_id: str,
    *,
    meta: dict[str, object] | None = None,
) -> _PhaseStatusContract | None:
    explicit_pilot = meta is not None and meta.get("experiment_enabled") is True
    if phase_id == "-" and not explicit_pilot:
        return None
    project_root = run_path.parents[2] if len(run_path.parents) >= 3 else None
    candidates: list[Path] = []
    if project_root is not None:
        candidates.append(project_root / ".agent-flow" / "workflows" / f"{workflow}.yaml")
    # runner와 같은 kit root를 우선해 phase routing과 marker 검증이 같은 YAML을 본다.
    try:
        candidates.append(find_kit_root() / "workflows" / f"{workflow}.yaml")
    except RuntimeError:
        pass
    candidates.append(Path(__file__).resolve().parent / "workflows" / f"{workflow}.yaml")
    candidates.append(Path(__file__).resolve().parents[2] / "workflows" / f"{workflow}.yaml")
    for path in candidates:
        if not path.exists():
            continue
        try:
            definition = load_phase_workflow_definition(path.parent.parent, workflow)
        except (OSError, ValueError):
            if explicit_pilot:
                raise RuntimeError(
                    "blocked: active pilot runner workflow snapshot changed; "
                    "restore the installed workflow or start a new run"
                )
            definition = None
        if definition is not None:
            _assert_status_runner_workflow_pinned(meta, workflow, definition)
            if phase_id == "-":
                return None
            for phase in definition.phases:
                if phase.id == phase_id:
                    return _PhaseStatusContract(
                        id=phase.id,
                        artifact=Path(phase.artifact),
                        required_markers=phase.required_markers,
                        pause_after=phase.pause_after,
                        multi_review=phase.multi_review,
                        routes=phase.routes,
                    )
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        phases = raw.get("phases") if isinstance(raw, dict) else None
        if not isinstance(phases, list):
            continue
        for phase in phases:
            if not isinstance(phase, dict) or str(phase.get("id")) != phase_id:
                continue
            raw_artifact = phase.get("artifact")
            artifact = (
                Path(raw_artifact)
                if isinstance(raw_artifact, str) and raw_artifact
                else Path(_default_phase_artifact(workflow, phase_id))
            )
            raw_routes = phase.get("routes")
            routes = (
                {
                    str(key): str(value)
                    for key, value in raw_routes.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
                if isinstance(raw_routes, dict)
                else None
            )
            return _PhaseStatusContract(
                id=phase_id,
                artifact=artifact,
                required_markers=normalize_required_markers(phase.get("required_markers")),
                pause_after=phase.get("pause_after") is True,
                multi_review=phase.get("multi_review") is True,
                routes=routes,
            )
    if explicit_pilot:
        raise RuntimeError(
            "blocked: active pilot runner workflow snapshot changed; "
            "restore the installed workflow or start a new run"
        )
    if phase_id == "-":
        return None
    return _PhaseStatusContract(
        id=phase_id,
        artifact=Path(_default_phase_artifact(workflow, phase_id)),
        required_markers=(),
        pause_after=False,
        multi_review=False,
        routes=None,
    )


def _assert_status_runner_workflow_pinned(
    meta: dict[str, object] | None,
    workflow: str,
    definition: object,
) -> None:
    if meta is None or meta.get("experiment_enabled") is not True:
        return
    workflow_hash = meta.get("runner_workflow_hash")
    if (
        meta.get("runner_workflow_hash_version") != 1
        or not isinstance(workflow_hash, str)
        or re.fullmatch(r"[a-f0-9]{64}", workflow_hash) is None
    ):
        raise RuntimeError(
            "blocked: active pilot run has an invalid runner workflow snapshot"
        )
    phases = getattr(definition, "phases", ())
    payload = {
        "workflow_id": workflow,
        "workflow_phases": [
            {
                "id": phase.id,
                "artifact": phase.artifact,
                "description": phase.description,
                "instruction": phase.prompt or phase.description,
                "required_markers": list(phase.required_markers),
                "pause_after": phase.pause_after,
                "optional": phase.optional,
                "multi_review": phase.multi_review,
                "cite_lore": phase.cite_lore,
                "routes": dict(phase.routes) if phase.routes is not None else None,
            }
            for phase in phases
        ],
    }
    live_hash = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if workflow_hash != live_hash:
        raise RuntimeError(
            "blocked: active pilot runner workflow snapshot changed; "
            "restore the installed workflow or start a new run"
        )


def _default_phase_artifact(workflow: str, phase_id: str) -> str:
    if workflow != "full-feature":
        return f"{phase_id}.md"
    if phase_id == "red":
        return "artifacts/red.log"
    if phase_id == "green":
        return "artifacts/green.log"
    if phase_id == "gates":
        return "artifacts/gate-results.json"
    return f"artifacts/{phase_id}.md"


def _existing_phase_artifact(run_path: Path, phase_id: str, artifact_rel: Path | None) -> Path:
    return run_path / artifact_rel if artifact_rel is not None else run_path / f"{phase_id}.md"


def _missing_markers(text: str, markers: tuple[str, ...]) -> list[str]:
    return missing_markers(text, markers)


def _status_value(value: object) -> str:
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def _pause_approval_command(command: str) -> str:
    if "--approve-pause" in command.split():
        return command
    return f"{command} --approve-pause"


def _artifact_block_reason(artifact: Path) -> str | None:
    if not artifact.exists():
        return None
    text = artifact.read_text(encoding="utf-8")
    if (
        "_stub artifact written by GenericAdapter (stub mode)._" in text
        and os.environ.get("AGENT_FLOW_GENERIC_MODE") != "stub-success"
    ):
        return "generic_stub_artifact"
    return None


def _artifact_is_stale(artifact: Path, meta: dict) -> bool:
    entered_at = _meta_timestamp(
        meta.get("phase_entered_at")
        or meta.get("updated_at")
        or meta.get("started_at")
    )
    return entered_at is not None and artifact.stat().st_mtime < entered_at


def _meta_timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _missing_completion_markers_for_contract(
    run_path: Path,
    phase_id: str,
    contract: _PhaseStatusContract,
    *,
    config_root: Path | None,
    task_scope: str = "",
    changed_files: tuple[str, ...] = (),
) -> list[str]:
    artifact = run_path / contract.artifact
    text = artifact.read_text(encoding="utf-8")
    missing = _missing_markers(text, contract.required_markers)
    missing.extend(
        missing_local_skill_markers(
            text,
            config_root or run_path,
            phase_id,
            task_scope,
            changed_files,
        )
    )
    return missing


def _pause_after_pending_matches(meta: dict, phase_id: str, artifact: Path) -> bool:
    pending = meta.get("pause_after_pending")
    return bool(
        isinstance(pending, dict)
        and pending.get("phase") == phase_id
        and pending.get("artifact_sha256")
        == hashlib.sha256(artifact.read_bytes()).hexdigest()
    )


def _pause_after_approval_matches(meta: dict, phase_id: str, artifact: Path) -> bool:
    approval = meta.get("pause_after_approval")
    return bool(
        isinstance(approval, dict)
        and approval.get("phase") == phase_id
        and approval.get("artifact_sha256")
        == hashlib.sha256(artifact.read_bytes()).hexdigest()
    )


def _route_is_blocked(
    contract: _PhaseStatusContract,
    artifact: Path,
    meta: dict,
) -> bool:
    if not contract.routes:
        return False

    from agent_flow.runner import (
        FIX_LOOP_MAX_ROUNDS,
        _fix_loop_rounds,
        _gates_route_key,
        _multi_review_route_key,
        _route_key,
    )

    text = artifact.read_text(encoding="utf-8")
    if contract.multi_review:
        key = _multi_review_route_key(text, contract.id)
        if key in {"missing-reviewer", "insufficient-reviewers", "invalid-verdict", "default"}:
            return True
    elif contract.id == "gates":
        key = _gates_route_key(text)
    else:
        key = _route_key(text, contract.id)
    if key in {"invalid-route", "invalid-verdict"}:
        return True
    if (
        key == "approve"
        and contract.routes.get("request-changes")
        and has_failure_markers(text)
    ):
        key = "request-changes"
    target = contract.routes.get(key)
    if target is None:
        target = contract.routes.get("default")
    if target == "fix-loop" and _fix_loop_rounds(meta) + 1 > FIX_LOOP_MAX_ROUNDS:
        return True
    return target in {None, "block"}
