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
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from agent_flow.core.markers import missing_markers, normalize_required_markers
from agent_flow.core.local_skills import missing_local_skill_markers
from agent_flow.core.phase_contract import (
    artifact_is_stale,
    declared_artifact_issues,
    phase_contract_issues,
    phase_entry_time,
    resolve_runtime_phase_contract,
)
from agent_flow.core.phase_workflow import (
    PhaseDefinition,
    find_kit_root,
    load_phase_workflow_definition,
)


RUNS_DIRNAME = ".agent-flow/runs"
ACTIVE_MARKER = "active"
META_FILE = "meta.json"
ACTIVE_LOCK = "active.lock"
LEGACY_EMPTY_LOCK_GRACE_SECONDS = 30


class ActiveRunExists(RuntimeError):
    """Raised when create_run is called but an active run already exists."""


@dataclass
class ActiveRun:
    path: Path
    run_id: str
    workflow: str
    task: str
    started_at: str

    def print_status(self, *, next_command: str = "agent-flow continue", config_root: Path | None = None) -> None:
        artifacts = sorted(str(p.relative_to(self.path)) for p in self.path.rglob("*") if p.is_file())
        meta = read_meta(self.path)
        current_phase = meta.get("current_phase") or "-"
        phase = _phase_definition(
            self.path,
            self.workflow,
            current_phase,
            config_root=config_root,
        )
        if phase is not None:
            project_root = _status_project_root(self.path, meta, config_root)
            phase = resolve_runtime_phase_contract(
                phase,
                config_root=config_root or project_root,
                project_root=project_root,
                meta=meta,
            )
            artifact_rel = Path(phase.artifact)
        else:
            artifact_rel, _markers = _phase_contract(self.path, self.workflow, current_phase)
        required_artifact = (
            _existing_phase_artifact(self.path, current_phase, artifact_rel)
            if current_phase != "-" and artifact_rel is not None
            else None
        )
        structured_status = "running"
        reason = "in_progress"
        if required_artifact is not None and not required_artifact.exists():
            structured_status = "awaiting_host"
            reason = "missing_phase_artifact"
        elif required_artifact is not None:
            structured_status = "blocked"
            block_reason = (
                "stale_artifact"
                if artifact_is_stale(required_artifact, phase_entry_time(meta))
                else _artifact_block_reason(required_artifact)
            )
            if block_reason:
                reason = block_reason
            else:
                missing_markers = _missing_completion_markers(
                    self.path,
                    self.workflow,
                    current_phase,
                    config_root=config_root,
                    phase=phase,
                    meta=meta,
                )
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


@dataclass(frozen=True)
class ActiveRunStartLock:
    path: Path
    device: int
    inode: int
    owner: dict[str, object]


def create_run(
    project_root: Path,
    workflow: str,
    task: str,
    *,
    architecture: str | None = None,
    run_id: str | None = None,
) -> Path:
    """Create a new run directory. Refuses if an active run exists."""
    runs_dir = project_root / RUNS_DIRNAME
    runs_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    selected_run_id = run_id or next_run_id(project_root, now=now)
    lock = _acquire_active_run_lock(runs_dir, selected_run_id)
    created = False

    try:
        existing = find_active_run(project_root)
        if existing is not None:
            raise ActiveRunExists(
                f"active run already exists: {existing.run_id} "
                f"(task: {existing.task!r}). Use `agent-flow continue` to resume "
                f"or `agent-flow abort` to clear."
            )

        if (runs_dir / selected_run_id).exists():
            raise ActiveRunExists(f"run already exists: {selected_run_id}")

        run_path = runs_dir / selected_run_id
        run_path.mkdir()
        meta = {
            "run_id": selected_run_id,
            "workflow": workflow,
            "task": task,
            "started_at": now.isoformat(),
            "current_phase": None,
            "status": "running",
        }
        if architecture:
            meta["architecture"] = architecture
        write_meta(run_path, meta)
        (run_path / ACTIVE_MARKER).write_text("")
        created = True
        return run_path
    finally:
        try:
            _release_active_run_lock(lock)
        except ActiveRunExists as exc:
            if not created:
                raise
            print(
                f"warning: run was created but its start lock could not be released: {exc}",
                file=sys.stderr,
            )


def next_run_id(
    project_root: Path,
    *,
    now: datetime | None = None,
) -> str:
    runs_dir = project_root / RUNS_DIRNAME
    runs_dir.mkdir(parents=True, exist_ok=True)
    base_id = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    run_id = base_id
    suffix = 1
    while (runs_dir / run_id).exists():
        run_id = f"{base_id}-{suffix}"
        suffix += 1
    return run_id


def _acquire_active_run_lock(runs_dir: Path, run_id: str) -> ActiveRunStartLock:
    lock_path = runs_dir / ACTIVE_LOCK
    token = os.urandom(16).hex()
    process_start_id = _process_start_identity(os.getpid())
    if process_start_id is None:
        raise ActiveRunExists("cannot authenticate active run start process")
    owner = {
        "version": 1,
        "pid": os.getpid(),
        "process_start_id": process_start_id,
        "token": token,
        "run_id": run_id,
        "runs_root": str(runs_dir.resolve(strict=True)),
        "acquired_at": datetime.now(timezone.utc).isoformat(),
    }
    temporary = runs_dir / f".{ACTIVE_LOCK}.{os.getpid()}.{token}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        payload = f"{json.dumps(owner, sort_keys=True)}\n".encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("active run start lock write failed")
            view = view[written:]
        os.fsync(descriptor)
        temporary_metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        for attempt in range(2):
            try:
                os.link(temporary, lock_path, follow_symlinks=False)
                break
            except FileExistsError as exc:
                if attempt == 0 and _recover_stale_active_run_lock(lock_path, runs_dir):
                    continue
                raise ActiveRunExists(
                    "another agent-flow run is starting; authenticated active.lock ownership is live"
                ) from exc
        else:
            raise ActiveRunExists("active run start lock is unavailable")
        metadata = lock_path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_dev != temporary_metadata.st_dev
            or metadata.st_ino != temporary_metadata.st_ino
        ):
            raise ActiveRunExists("active run start lock publication changed")
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return ActiveRunStartLock(lock_path, metadata.st_dev, metadata.st_ino, owner)


def _recover_stale_active_run_lock(lock_path: Path, runs_dir: Path) -> bool:
    try:
        metadata = lock_path.lstat()
    except FileNotFoundError:
        return True
    if stat.S_ISLNK(metadata.st_mode) or not (
        stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
    ):
        return False
    if stat.S_ISDIR(metadata.st_mode):
        try:
            empty_legacy_lock = not any(lock_path.iterdir())
            active_run_exists = any(
                child.is_dir()
                and child.name != ACTIVE_LOCK
                and (child / ACTIVE_MARKER).is_file()
                for child in runs_dir.iterdir()
            )
        except OSError:
            return False
        if empty_legacy_lock:
            if (
                active_run_exists
                or time.time() - metadata.st_mtime < LEGACY_EMPTY_LOCK_GRACE_SECONDS
            ):
                return False
            quarantine = lock_path.with_name(
                f".{ACTIVE_LOCK}.stale-{os.getpid()}-{os.urandom(8).hex()}"
            )
            try:
                lock_path.rename(quarantine)
            except FileNotFoundError:
                return True
            moved = quarantine.lstat()
            if (
                moved.st_dev != metadata.st_dev
                or moved.st_ino != metadata.st_ino
                or not stat.S_ISDIR(moved.st_mode)
                or any(quarantine.iterdir())
            ):
                if not lock_path.exists():
                    quarantine.rename(lock_path)
                raise ActiveRunExists(
                    "active run start lock changed during stale recovery"
                )
            quarantine.rmdir()
            return True
    owner_path = lock_path / "owner.json" if stat.S_ISDIR(metadata.st_mode) else lock_path
    try:
        owner_metadata = owner_path.lstat()
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return False
    pid = owner.get("pid")
    process_start_id = owner.get("process_start_id")
    if (
        not stat.S_ISREG(owner_metadata.st_mode)
        or stat.S_ISLNK(owner_metadata.st_mode)
        or owner.get("version") != 1
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(process_start_id, str)
        or not process_start_id
        or not isinstance(owner.get("token"), str)
        or not owner.get("token")
        or not isinstance(owner.get("run_id"), str)
        or not owner.get("run_id")
        or owner.get("runs_root") != str(runs_dir.resolve(strict=True))
    ):
        return False
    if _process_is_alive(pid):
        current_start_id = _process_start_identity(pid)
        if current_start_id is None or current_start_id == process_start_id:
            return False
    quarantine = lock_path.with_name(
        f".{ACTIVE_LOCK}.stale-{os.getpid()}-{os.urandom(8).hex()}"
    )
    try:
        lock_path.rename(quarantine)
    except FileNotFoundError:
        return True
    moved = quarantine.lstat()
    try:
        moved_owner_path = (
            quarantine / "owner.json"
            if stat.S_ISDIR(moved.st_mode)
            else quarantine
        )
        moved_owner = json.loads(moved_owner_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if not lock_path.exists():
            quarantine.rename(lock_path)
        raise ActiveRunExists("active run start lock changed during stale recovery") from exc
    if (
        moved.st_dev != metadata.st_dev
        or moved.st_ino != metadata.st_ino
        or moved_owner != owner
    ):
        if not lock_path.exists():
            quarantine.rename(lock_path)
        raise ActiveRunExists("active run start lock changed during stale recovery")
    if stat.S_ISDIR(moved.st_mode):
        shutil.rmtree(quarantine)
    else:
        quarantine.unlink()
    return True


def _release_active_run_lock(lock: ActiveRunStartLock) -> None:
    try:
        metadata = lock.path.lstat()
        owner = json.loads(lock.path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ActiveRunExists("active run start lock disappeared") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_dev != lock.device
        or metadata.st_ino != lock.inode
        or owner != lock.owner
    ):
        raise ActiveRunExists("active run start lock ownership changed")
    lock.path.unlink()


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _process_start_identity(pid: int) -> str | None:
    if Path("/proc").is_dir():
        try:
            stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            fields = stat_text[stat_text.rfind(")") + 2 :].split()
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
                encoding="utf-8"
            ).strip()
            if len(fields) > 19 and boot_id:
                return f"linux:{boot_id}:{fields[19]}"
        except (OSError, UnicodeError, ValueError):
            pass
    try:
        result = subprocess.run(
            ("/bin/ps", "-o", "lstart=", "-p", str(pid)),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
            env={"LC_ALL": "C", "LANG": "C", "TZ": "UTC0", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


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
    meta = read_meta(run_path)
    workspace = meta.get("workspace")
    project_root = (
        Path(str(workspace.get("workspace_root")))
        if isinstance(workspace, dict) and workspace.get("workspace_root")
        else None
    )
    if project_root is None:
        probe = subprocess.run(
            ("git", "-C", str(run_path), "rev-parse", "--show-toplevel"),
            text=True,
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            project_root = Path(probe.stdout.strip())
    if project_root is not None and project_root.is_dir():
        from agent_flow.core.artifacts import remove_gate_cache

        remove_gate_cache(root=project_root, run_dir=run_path)
    marker = run_path / ACTIVE_MARKER
    if marker.exists():
        marker.unlink()


def has_artifact(run_path: Path, phase_id: str) -> bool:
    return (run_path / f"{phase_id}.md").exists()


def _missing_completion_markers(
    run_path: Path,
    workflow: str,
    phase_id: str,
    *,
    config_root: Path | None = None,
    phase: PhaseDefinition | None = None,
    meta: dict | None = None,
) -> list[str]:
    if phase is None:
        artifact_rel, markers = _phase_contract(run_path, workflow, phase_id)
    else:
        artifact_rel, markers = Path(phase.artifact), phase.required_markers
    artifact = _existing_phase_artifact(run_path, phase_id, artifact_rel)
    if not artifact.exists():
        return []
    text = artifact.read_text(encoding="utf-8")
    missing = _missing_markers(text, markers) if markers else []
    missing.extend(missing_local_skill_markers(text, config_root or run_path, phase_id))
    if phase is not None:
        missing.extend(phase_contract_issues(phase, text))
        effective_meta = meta if meta is not None else read_meta(run_path)
        missing.extend(
            declared_artifact_issues(
                run_path,
                phase,
                phase_entry_time(effective_meta),
            )
        )
    return missing


def _required_markers(run_path: Path, workflow: str, phase_id: str) -> tuple[str, ...]:
    _artifact_rel, markers = _phase_contract(run_path, workflow, phase_id)
    return markers


def _phase_contract(run_path: Path, workflow: str, phase_id: str) -> tuple[Path | None, tuple[str, ...]]:
    definition = _phase_definition(run_path, workflow, phase_id)
    if definition is not None:
        return Path(definition.artifact), definition.required_markers
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
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        phases = raw.get("phases") if isinstance(raw, dict) else None
        if not isinstance(phases, list):
            continue
        for phase in phases:
            if not isinstance(phase, dict) or str(phase.get("id")) != phase_id:
                continue
            return Path(f"{phase_id}.md"), normalize_required_markers(phase.get("required_markers"))
    return (Path(f"{phase_id}.md") if phase_id != "-" else None, ())


def _phase_definition(
    run_path: Path,
    workflow: str,
    phase_id: str,
    *,
    config_root: Path | None = None,
) -> PhaseDefinition | None:
    project_root = run_path.parents[2] if len(run_path.parents) >= 3 else None
    candidates: list[Path] = []
    if config_root is not None:
        candidates.append(config_root / ".agent-flow" / "workflows" / f"{workflow}.yaml")
    if project_root is not None:
        candidates.append(project_root / ".agent-flow" / "workflows" / f"{workflow}.yaml")
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
            continue
        for phase in definition.phases:
            if phase.id == phase_id:
                return phase
    return None


def _status_project_root(
    run_path: Path,
    meta: dict,
    config_root: Path | None,
) -> Path:
    workspace = meta.get("workspace")
    workspace_root = workspace.get("workspace_root") if isinstance(workspace, dict) else None
    if isinstance(workspace_root, str) and workspace_root:
        return Path(workspace_root)
    if config_root is not None:
        return config_root
    return run_path.parents[2] if len(run_path.parents) >= 3 else run_path


def _existing_phase_artifact(run_path: Path, phase_id: str, artifact_rel: Path | None) -> Path:
    artifact = run_path / artifact_rel if artifact_rel is not None else run_path / f"{phase_id}.md"
    legacy_artifact = run_path / f"{phase_id}.md"
    if not artifact.exists() and legacy_artifact.exists():
        return legacy_artifact
    return artifact


def _missing_markers(text: str, markers: tuple[str, ...]) -> list[str]:
    return missing_markers(text, markers)


def _status_value(value: object) -> str:
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def _artifact_block_reason(artifact: Path) -> str | None:
    if not artifact.exists():
        return None
    text = artifact.read_text(encoding="utf-8")
    if "_stub artifact written by GenericAdapter (stub mode)._" in text:
        return "generic_stub_artifact"
    return None
