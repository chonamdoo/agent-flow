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

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from agent_flow.core.markers import missing_markers, normalize_required_markers


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

    def print_status(self, *, next_command: str = "agent-flow continue") -> None:
        artifacts = sorted(p.name for p in self.path.glob("*.md"))
        meta = read_meta(self.path)
        current_phase = meta.get("current_phase") or "-"
        required_artifact = (
            self.path / f"{current_phase}.md"
            if current_phase != "-"
            else None
        )
        structured_status = "running"
        reason = "in_progress"
        if required_artifact is not None and not required_artifact.exists():
            structured_status = "awaiting_host"
            reason = "missing_phase_artifact"
        elif required_artifact is not None:
            missing_markers = _missing_completion_markers(
                self.path,
                self.workflow,
                current_phase,
            )
            structured_status = "blocked"
            reason = (
                "missing_completion_markers"
                if missing_markers
                else "phase_artifact_written_continue_required"
            )
        payload = {
            "status": structured_status,
            "run": f"{self.workflow}/{self.run_id}",
            "task": self.task,
            "current_phase": current_phase,
            "reason": reason,
            "required_artifact": str(required_artifact) if required_artifact is not None else None,
            "next_command": next_command,
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
        print(f"reason: {_status_value(reason)}")
        if required_artifact is not None:
            print(f"required_artifact: {_status_value(required_artifact)}")
        print(f"next_command: {_status_value(next_command)}")
        print(f"status_json: {json.dumps(payload, sort_keys=True)}")


def find_active_run(project_root: Path) -> ActiveRun | None:
    runs_dir = project_root / RUNS_DIRNAME
    if not runs_dir.exists():
        return None
    actives = [p for p in runs_dir.iterdir() if (p / ACTIVE_MARKER).exists()]
    if not actives:
        return None
    if len(actives) > 1:
        # Should never happen in normal flow; surface so user can choose.
        names = ", ".join(p.name for p in actives)
        print(
            f"⚠️  multiple active runs detected: {names}. "
            f"Resuming the most recent; abort the others with `agent-flow abort` "
            f"after switching directories or by hand-deleting the marker file.",
            file=sys.stderr,
        )
    chosen = max(actives, key=lambda p: p.name)
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
                f"(task: {existing.task!r}). Use `agent-flow continue` to resume "
                f"or `agent-flow abort` to clear."
            )

        now = datetime.now(timezone.utc)
        base_id = now.strftime("%Y%m%d-%H%M%S")
        run_id = base_id
        suffix = 1
        while (runs_dir / run_id).exists():
            run_id = f"{base_id}-{suffix}"
            suffix += 1

        run_path = runs_dir / run_id
        run_path.mkdir()
        meta = {
            "run_id": run_id,
            "workflow": workflow,
            "task": task,
            "started_at": now.isoformat(),
            "current_phase": None,
        }
        if architecture:
            meta["architecture"] = architecture
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
            f"treating as empty. Use `agent-flow abort` to clear if needed.",
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


def mark_inactive(run_path: Path) -> None:
    marker = run_path / ACTIVE_MARKER
    if marker.exists():
        marker.unlink()


def has_artifact(run_path: Path, phase_id: str) -> bool:
    return (run_path / f"{phase_id}.md").exists()


def _missing_completion_markers(
    run_path: Path,
    workflow: str,
    phase_id: str,
) -> list[str]:
    markers = _required_markers(run_path, workflow, phase_id)
    artifact = run_path / f"{phase_id}.md"
    if not markers or not artifact.exists():
        return []
    return _missing_markers(artifact.read_text(encoding="utf-8"), markers)


def _required_markers(run_path: Path, workflow: str, phase_id: str) -> tuple[str, ...]:
    project_root = run_path.parents[2] if len(run_path.parents) >= 3 else None
    candidates = []
    if project_root is not None:
        candidates.append(project_root / ".agent-flow" / "workflows" / f"{workflow}.yaml")
    candidates.append(Path(__file__).resolve().parent / "workflows" / f"{workflow}.yaml")
    candidates.append(Path(__file__).resolve().parents[2] / "workflows" / f"{workflow}.yaml")
    for path in candidates:
        if not path.exists():
            continue
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
            return normalize_required_markers(phase.get("required_markers"))
    return ()


def _missing_markers(text: str, markers: tuple[str, ...]) -> list[str]:
    return missing_markers(text, markers)


def _status_value(value: object) -> str:
    return str(value).replace("\r", "\\r").replace("\n", "\\n")
