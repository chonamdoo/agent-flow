from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


GIT_TIMEOUT_SECONDS = 10


class WorkspaceBoundaryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionIdentity:
    host: str
    session_id: str
    agent_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @property
    def digest(self) -> str:
        payload = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class WorkspaceIdentity:
    workspace_root: str
    git_common_dir: str
    git_dir: str
    branch: str
    head: str
    device: int
    inode: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ActivePinnedWorkspace:
    name: str
    identity: WorkspaceIdentity
    run_dir: Path


@dataclass(frozen=True)
class WorkspaceStartClaim:
    path: Path
    device: int
    inode: int
    token: str
    pid: int
    process_start_id: str
    run_id: str


@dataclass(frozen=True)
class _CompletedFinalizer:
    workspace: ActivePinnedWorkspace
    execution: ExecutionIdentity
    run_id: str
    completed_ns: int


def execution_identity_from_context(
    payload: Mapping[str, object] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    host_hint: str = "",
) -> ExecutionIdentity | None:
    context = payload or {}
    environment = os.environ if env is None else env
    host = str(
        context.get("host")
        or host_hint
        or environment.get("AGENT_FLOW_ACTIVE_HOST")
        or environment.get("AGENT_FLOW_HOST")
        or _detected_execution_host(environment)
        or "unknown"
    ).strip().lower()
    session_id = str(
        environment.get("AGENT_FLOW_EXECUTION_ID")
        or context.get("execution_id")
        or context.get("thread_id")
        or context.get("session_id")
        or environment.get("AGENT_FLOW_SESSION_ID")
        or _host_session_id(host, environment)
        or ""
    ).strip()
    if not session_id:
        return None
    agent_id = str(
        context.get("agent_id")
        or environment.get("AGENT_FLOW_AGENT_ID")
        or _host_agent_id(host, environment)
        or ""
    ).strip()
    if not host or len(host) > 32 or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in host):
        raise WorkspaceBoundaryError("execution_identity_invalid: host")
    if len(session_id) > 512 or len(agent_id) > 512:
        raise WorkspaceBoundaryError("execution_identity_invalid: identifier too long")
    return ExecutionIdentity(host=host, session_id=session_id, agent_id=agent_id)


def execution_identity_from_dict(payload: object) -> ExecutionIdentity:
    if not isinstance(payload, dict):
        raise WorkspaceBoundaryError("execution_identity_invalid: missing")
    identity = execution_identity_from_context(payload, {})
    if identity is None:
        raise WorkspaceBoundaryError("execution_identity_invalid: session")
    return identity


def bind_execution_to_workspace(
    execution: ExecutionIdentity,
    identity: WorkspaceIdentity,
    run_dir: Path,
    *,
    run_id: str,
) -> Path:
    validate_workspace_identity(identity)
    binding_path = _execution_binding_path(Path(identity.git_common_dir), execution)
    resolved_run_dir = run_dir.resolve(strict=True)
    payload = {
        "version": 2,
        "execution": execution.to_dict(),
        "workspace": identity.to_dict(),
        "workspace_name": Path(identity.workspace_root).name,
        "run_id": run_id,
        "run_dir": str(resolved_run_dir),
        "bound_at": datetime.now(timezone.utc).isoformat(),
    }
    temporary = binding_path.with_name(
        f".{binding_path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    encoded = f"{json.dumps(payload, indent=2, sort_keys=True)}\n".encode("utf-8")
    descriptor = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("execution binding write failed")
            view = view[written:]
        os.fsync(descriptor)
        temporary_metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        for attempt in range(2):
            try:
                os.link(temporary, binding_path, follow_symlinks=False)
                break
            except FileExistsError as exc:
                existing = _read_owned_json(binding_path)
                if _binding_is_active(existing):
                    if _same_execution_binding(existing, payload):
                        return binding_path
                    raise WorkspaceBoundaryError(
                        "execution_binding_conflict: execution is already bound to an active workspace"
                    ) from exc
                if attempt == 0:
                    _remove_owned_json_atomic(binding_path, existing)
                    continue
                raise WorkspaceBoundaryError(
                    "execution_binding_conflict: execution binding publication raced"
                ) from exc
        else:
            raise WorkspaceBoundaryError(
                "execution_binding_conflict: execution binding is unavailable"
            )
        metadata = binding_path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_dev != temporary_metadata.st_dev
            or metadata.st_ino != temporary_metadata.st_ino
        ):
            raise WorkspaceBoundaryError(
                "execution_binding_conflict: execution binding publication changed"
            )
        return binding_path
    finally:
        temporary.unlink(missing_ok=True)


def acquire_workspace_start_claim(
    identity: WorkspaceIdentity,
    *,
    run_id: str = "pending",
) -> WorkspaceStartClaim:
    claims_root = _git_private_directory(
        Path(identity.git_common_dir),
        "agent-flow",
        "workspace-start-claims",
        create=True,
    )
    digest = hashlib.sha256(identity.workspace_root.encode("utf-8")).hexdigest()
    claim_path = claims_root / f"{digest}.lock"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    token = os.urandom(16).hex()
    process_start_id = _process_start_identity(os.getpid())
    if process_start_id is None:
        raise WorkspaceBoundaryError(
            "execution_binding_conflict: process start identity is unavailable"
        )
    temporary = claims_root / f".{digest}.{os.getpid()}.{token}.tmp"
    claim_payload = _workspace_start_claim_payload(
        identity,
        run_id=run_id,
        token=token,
        pid=os.getpid(),
        process_start_id=process_start_id,
    )
    payload = json.dumps(
        claim_payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    descriptor = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("workspace claim write failed")
            view = view[written:]
        os.fsync(descriptor)
        temporary_metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        for attempt in range(2):
            try:
                os.link(temporary, claim_path, follow_symlinks=False)
                break
            except FileExistsError as exc:
                if attempt == 0 and _recover_stale_workspace_start_claim(
                    claim_path,
                    identity,
                ):
                    continue
                raise WorkspaceBoundaryError(
                    "execution_binding_conflict: workspace start is already in progress"
                ) from exc
        else:
            raise WorkspaceBoundaryError(
                "execution_binding_conflict: workspace start claim is unavailable"
            )
        metadata = claim_path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_dev != temporary_metadata.st_dev
            or metadata.st_ino != temporary_metadata.st_ino
        ):
            raise WorkspaceBoundaryError(
                "execution_binding_conflict: workspace start claim publication changed"
            )
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return WorkspaceStartClaim(
        claim_path,
        metadata.st_dev,
        metadata.st_ino,
        token,
        os.getpid(),
        process_start_id,
        run_id,
    )


def release_workspace_start_claim(claim: WorkspaceStartClaim) -> None:
    try:
        metadata = claim.path.lstat()
    except FileNotFoundError as exc:
        raise WorkspaceBoundaryError("workspace start claim disappeared") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_dev != claim.device
        or metadata.st_ino != claim.inode
    ):
        raise WorkspaceBoundaryError("workspace start claim identity changed")
    payload = _read_json(claim.path)
    if (
        payload.get("version") != 2
        or payload.get("token") != claim.token
        or payload.get("pid") != claim.pid
        or payload.get("process_start_id") != claim.process_start_id
        or payload.get("run_id") != claim.run_id
    ):
        raise WorkspaceBoundaryError("workspace start claim ownership changed")
    claim.path.unlink()


def _workspace_start_claim_payload(
    identity: WorkspaceIdentity,
    *,
    run_id: str,
    token: str,
    pid: int,
    process_start_id: str,
) -> dict[str, object]:
    common = _authenticated_directory_root(Path(identity.git_common_dir))
    leader = common.parent.resolve(strict=True)
    leader_metadata = leader.stat()
    return {
        "version": 2,
        "pid": pid,
        "process_start_id": process_start_id,
        "token": token,
        "run_id": run_id,
        "leader_root": str(leader),
        "leader_device": leader_metadata.st_dev,
        "leader_inode": leader_metadata.st_ino,
        "workspace_root": identity.workspace_root,
        "workspace_git_dir": identity.git_dir,
        "workspace_branch": identity.branch,
        "workspace_head": identity.head,
        "workspace_device": identity.device,
        "workspace_inode": identity.inode,
        "acquired_at": datetime.now(timezone.utc).isoformat(),
    }


def _workspace_claim_matches_identity(
    payload: Mapping[str, object],
    identity: WorkspaceIdentity,
    *,
    include_revision: bool = True,
) -> bool:
    common = _authenticated_directory_root(Path(identity.git_common_dir))
    leader = common.parent.resolve(strict=True)
    leader_metadata = leader.stat()
    return (
        payload.get("leader_root") == str(leader)
        and payload.get("leader_device") == leader_metadata.st_dev
        and payload.get("leader_inode") == leader_metadata.st_ino
        and payload.get("workspace_root") == identity.workspace_root
        and payload.get("workspace_git_dir") == identity.git_dir
        and (
            not include_revision
            or (
                payload.get("workspace_branch") == identity.branch
                and payload.get("workspace_head") == identity.head
            )
        )
        and payload.get("workspace_device") == identity.device
        and payload.get("workspace_inode") == identity.inode
    )


def _recover_stale_workspace_start_claim(
    claim_path: Path,
    identity: WorkspaceIdentity,
) -> bool:
    try:
        metadata = claim_path.lstat()
    except FileNotFoundError:
        return True
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        return False
    payload = _read_json(claim_path)
    pid = payload.get("pid")
    token = payload.get("token")
    version = payload.get("version")
    if (
        version not in {1, 2}
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(token, str)
        or not token
        or payload.get("workspace_root") != identity.workspace_root
    ):
        return False
    if version == 2:
        process_start_id = payload.get("process_start_id")
        run_id = payload.get("run_id")
        if (
            not isinstance(process_start_id, str)
            or not process_start_id
            or not isinstance(run_id, str)
            or not run_id
            or not _workspace_claim_matches_identity(
                payload,
                identity,
                include_revision=run_id != "worktree-lifecycle",
            )
        ):
            return False
        if _process_is_alive(pid):
            current_start_id = _process_start_identity(pid)
            if current_start_id is None or current_start_id == process_start_id:
                return False
    elif _process_is_alive(pid):
        return False
    quarantine = claim_path.with_name(
        f".{claim_path.name}.stale-{os.getpid()}-{os.urandom(8).hex()}"
    )
    try:
        claim_path.rename(quarantine)
    except FileNotFoundError:
        return True
    moved = quarantine.lstat()
    moved_payload = _read_json(quarantine)
    if (
        moved.st_dev != metadata.st_dev
        or moved.st_ino != metadata.st_ino
        or moved_payload != payload
    ):
        if not claim_path.exists():
            quarantine.rename(claim_path)
        raise WorkspaceBoundaryError("workspace start claim changed during stale recovery")
    quarantine.unlink()
    return True


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
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
            timeout=GIT_TIMEOUT_SECONDS,
            env={"LC_ALL": "C", "LANG": "C", "TZ": "UTC0", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def assert_workspace_start_available(
    identity: WorkspaceIdentity,
    *,
    current_run_dir: Path | None = None,
) -> None:
    leader = leader_root_for_identity(identity)
    current = current_run_dir.resolve(strict=False) if current_run_dir is not None else None
    for active in find_active_pinned_workspaces(leader):
        if active.identity.workspace_root != identity.workspace_root:
            continue
        if current is not None and active.run_dir.resolve(strict=False) == current:
            continue
        raise WorkspaceBoundaryError(
            f"execution_binding_conflict: workspace already owns active run {active.run_dir.name}"
        )


def release_execution_binding(
    execution: ExecutionIdentity,
    *,
    git_common_dir: Path,
    run_dir: Path | None = None,
) -> None:
    binding_path = _execution_binding_path(git_common_dir, execution)
    try:
        payload = _read_owned_json(binding_path)
    except FileNotFoundError:
        return
    if run_dir is not None and payload.get("run_dir") != str(run_dir.resolve(strict=False)):
        raise WorkspaceBoundaryError("execution_binding_conflict: run identity changed")
    if payload.get("execution") != execution.to_dict():
        raise WorkspaceBoundaryError("execution_binding_conflict: execution identity changed")
    _remove_owned_json_atomic(binding_path, payload)


def resolve_execution_workspace(
    leader_root: Path,
    execution: ExecutionIdentity,
) -> ActivePinnedWorkspace:
    leader = leader_root.resolve(strict=True)
    common = _authenticated_directory_root(Path(
        _git(leader, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ))
    binding_path = _execution_binding_path(common, execution)
    if not binding_path.exists() and not binding_path.is_symlink():
        raise WorkspaceBoundaryError(
            "execution_binding_missing: active execution is not bound to a worktree"
        )
    binding = _read_owned_json(binding_path)
    if execution_identity_from_dict(binding.get("execution")) != execution:
        raise WorkspaceBoundaryError("execution_binding_invalid: execution identity mismatch")
    if not _binding_is_active(binding):
        raise WorkspaceBoundaryError("execution_binding_stale: bound run is not active")
    identity = workspace_identity_from_dict(binding.get("workspace"))
    validate_workspace_identity(identity)
    run_dir_value = binding.get("run_dir")
    if not isinstance(run_dir_value, str) or not run_dir_value:
        raise WorkspaceBoundaryError("execution_binding_invalid: run directory")
    run_dir = Path(run_dir_value).resolve(strict=True)
    return ActivePinnedWorkspace(
        str(binding.get("workspace_name") or Path(identity.workspace_root).name),
        identity,
        run_dir,
    )


def select_execution_workspace(
    leader_root: Path,
    execution: ExecutionIdentity | None,
) -> ActivePinnedWorkspace | None:
    active = find_active_pinned_workspaces(leader_root)
    if not active:
        return None
    if execution is None:
        reason = (
            "execution_identity_ambiguous: multiple active worktrees require a host session identity"
            if len(active) > 1
            else "execution_identity_missing: active worktree runs require a host session identity"
        )
        raise WorkspaceBoundaryError(
            reason
        )
    selected = resolve_execution_workspace(leader_root, execution)
    active_keys = {(item.identity.workspace_root, str(item.run_dir.resolve(strict=False))) for item in active}
    selected_key = (selected.identity.workspace_root, str(selected.run_dir.resolve(strict=False)))
    if selected_key not in active_keys:
        raise WorkspaceBoundaryError("execution_binding_stale: bound workspace is not active")
    return selected


def execution_binding_exists(
    leader_root: Path,
    execution: ExecutionIdentity | None,
) -> bool:
    """현재 execution identity의 binding 파일 존재 여부를 활성/비활성과 무관하게 반환한다."""
    if execution is None:
        return False
    leader = leader_root.resolve(strict=True)
    common = _authenticated_directory_root(Path(
        _git(leader, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ))
    binding_path = _execution_binding_path(common, execution)
    return binding_path.exists() or binding_path.is_symlink()


def record_workspace_finalizer(
    identity: WorkspaceIdentity,
    execution: ExecutionIdentity,
    run_dir: Path,
    *,
    run_id: str,
    completed_at: str,
) -> Path:
    runtime = authenticated_worktree_runtime_root(
        Path(identity.git_common_dir),
        Path(identity.workspace_root).name,
    )
    if not runtime.exists():
        raise WorkspaceBoundaryError("execution_finalizer_invalid: worktree runtime is missing")
    finalizer_path = runtime / "finalizer.json"
    generation = 1
    if finalizer_path.exists() or finalizer_path.is_symlink():
        existing = _read_owned_json(finalizer_path)
        previous_generation = existing.get("generation")
        if not isinstance(previous_generation, int) or previous_generation < 1:
            raise WorkspaceBoundaryError("execution_finalizer_invalid: generation")
        same_completion = (
            existing.get("execution") == execution.to_dict()
            and existing.get("workspace") == identity.to_dict()
            and existing.get("workspace_name") == Path(identity.workspace_root).name
            and existing.get("run_id") == run_id
            and existing.get("run_dir") == str(run_dir.resolve(strict=False))
        )
        if same_completion:
            return finalizer_path
        generation = previous_generation + 1
    payload = {
        "version": 1,
        "generation": generation,
        "execution": execution.to_dict(),
        "workspace": identity.to_dict(),
        "workspace_name": Path(identity.workspace_root).name,
        "run_id": run_id,
        "run_dir": str(run_dir.resolve(strict=False)),
        "completed_at": completed_at,
    }
    temporary = finalizer_path.with_name(
        f".{finalizer_path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        encoded = f"{json.dumps(payload, indent=2, sort_keys=True)}\n".encode("utf-8")
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("finalizer record write failed")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, finalizer_path)
    finally:
        temporary.unlink(missing_ok=True)
    return finalizer_path


def resolve_execution_finalizer_workspace(
    leader_root: Path,
    execution: ExecutionIdentity | None,
    workspace_name: str,
) -> ActivePinnedWorkspace:
    if execution is None:
        raise WorkspaceBoundaryError(
            "execution_identity_missing: completed worktree cleanup requires a host session identity"
        )
    if (
        not workspace_name
        or Path(workspace_name).name != workspace_name
        or workspace_name in {".", ".."}
    ):
        raise WorkspaceBoundaryError("execution_finalizer_invalid: worktree name")
    leader = leader_root.resolve(strict=True)
    common = _authenticated_directory_root(Path(
        _git(leader, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ))
    runtime = authenticated_worktree_runtime_root(common, workspace_name)
    finalizer_path = runtime / "finalizer.json"
    if finalizer_path.exists() or finalizer_path.is_symlink():
        record = _read_owned_json(finalizer_path)
        if (
            record.get("version") != 1
            or not isinstance(record.get("generation"), int)
            or record.get("generation", 0) < 1
            or record.get("execution") != execution.to_dict()
        ):
            raise WorkspaceBoundaryError(
                "execution_finalizer_stale: requested execution is not the current cleanup owner"
            )
        current = _completed_finalizer_from_record(
            leader,
            runtime,
            workspace_name,
            record,
        )
    else:
        candidates = _completed_finalizer_candidates(
            leader,
            common,
            runtime,
            workspace_name,
        )
        if not candidates:
            raise WorkspaceBoundaryError(
                "execution_finalizer_missing: no authenticated completed run owns the requested worktree"
            )
        latest_ns = max(candidate.completed_ns for candidate in candidates)
        latest = [
            candidate for candidate in candidates if candidate.completed_ns == latest_ns
        ]
        latest_keys = {
            (
                candidate.execution.digest,
                candidate.run_id,
                str(candidate.workspace.run_dir.resolve(strict=False)),
            )
            for candidate in latest
        }
        if len(latest_keys) != 1:
            raise WorkspaceBoundaryError(
                "execution_finalizer_ambiguous: completed runs have the same finalizer generation"
            )
        current = latest[0]
        if current.execution != execution:
            raise WorkspaceBoundaryError(
                "execution_finalizer_stale: requested execution is not the current cleanup owner"
            )
    owned_root = current.workspace.identity.workspace_root
    for active in find_active_pinned_workspaces(leader):
        if Path(active.identity.workspace_root).resolve(strict=False) == Path(
            owned_root
        ).resolve(strict=False):
            raise WorkspaceBoundaryError(
                "execution_finalizer_active: requested worktree is owned by an active run"
            )
    return current.workspace


def _completed_finalizer_from_record(
    leader: Path,
    runtime: Path,
    workspace_name: str,
    record: dict[str, object],
) -> _CompletedFinalizer:
    execution = execution_identity_from_dict(record.get("execution"))
    identity = workspace_identity_from_dict(record.get("workspace"))
    _validate_finalizer_workspace(identity, leader, workspace_name)
    run_id = record.get("run_id")
    run_dir_value = record.get("run_dir")
    if not isinstance(run_id, str) or not run_id:
        raise WorkspaceBoundaryError("execution_finalizer_invalid: run id")
    if not isinstance(run_dir_value, str) or not run_dir_value:
        raise WorkspaceBoundaryError("execution_finalizer_invalid: run directory")
    run_dir = Path(run_dir_value)
    if not run_dir.is_absolute():
        raise WorkspaceBoundaryError("execution_finalizer_invalid: run directory is relative")
    allowed_roots = (
        _owned_directory(leader, ".agent-flow", "runs"),
        _owned_directory(Path(identity.workspace_root), ".agent-flow", "runs"),
        _owned_directory(runtime, ".agent-flow", "runs"),
    )
    authenticated_run_dir: Path | None = None
    for allowed_root in allowed_roots:
        try:
            relative = run_dir.relative_to(allowed_root)
        except ValueError:
            continue
        if not relative.parts:
            continue
        authenticated_run_dir = _owned_directory(allowed_root, *relative.parts)
        break
    if authenticated_run_dir is None or not authenticated_run_dir.exists():
        raise WorkspaceBoundaryError(
            "execution_finalizer_invalid: run directory is outside an authenticated run root"
        )
    metadata_path = authenticated_run_dir / "meta.json"
    node_manifest_path = authenticated_run_dir / "manifest.json"
    if metadata_path.exists() or metadata_path.is_symlink():
        metadata = _read_owned_json(metadata_path)
        complete = metadata.get("status") == "complete"
    elif node_manifest_path.exists() or node_manifest_path.is_symlink():
        metadata_path = node_manifest_path
        metadata = _read_owned_json(metadata_path)
        complete = (
            metadata.get("status") == "complete"
            or metadata.get("phase") == "complete"
        )
    else:
        raise WorkspaceBoundaryError(
            "execution_finalizer_invalid: completed run manifest is missing"
        )
    if (
        not complete
        or metadata.get("run_id") != run_id
        or metadata.get("execution") != execution.to_dict()
        or metadata.get("workspace") != identity.to_dict()
    ):
        raise WorkspaceBoundaryError(
            "execution_finalizer_invalid: finalizer does not match its completed run manifest"
        )
    return _CompletedFinalizer(
        ActivePinnedWorkspace(
            workspace_name,
            identity,
            authenticated_run_dir,
        ),
        execution,
        run_id,
        _completion_time_ns(metadata, metadata_path),
    )


def _completed_finalizer_candidates(
    leader: Path,
    common: Path,
    runtime: Path,
    workspace_name: str,
) -> list[_CompletedFinalizer]:
    candidates: list[_CompletedFinalizer] = []
    if runtime.exists():
        python_runs = _owned_directory(runtime, ".agent-flow", "runs")
        if python_runs.exists():
            for run_dir in sorted(python_runs.iterdir()):
                metadata = run_dir.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise WorkspaceBoundaryError(
                        "execution_finalizer_invalid: Python run directory is a symbolic link"
                    )
                if not stat.S_ISDIR(metadata.st_mode):
                    continue
                active_path = run_dir / "active"
                if active_path.exists() or active_path.is_symlink():
                    active_metadata = active_path.lstat()
                    if stat.S_ISLNK(active_metadata.st_mode) or not stat.S_ISREG(
                        active_metadata.st_mode
                    ):
                        raise WorkspaceBoundaryError(
                            "execution_finalizer_invalid: active marker is not regular"
                        )
                    continue
                meta_path = run_dir / "meta.json"
                if not meta_path.exists() and not meta_path.is_symlink():
                    continue
                meta = _read_owned_json(meta_path)
                if meta.get("status") != "complete":
                    continue
                execution = execution_identity_from_dict(meta.get("execution"))
                identity = workspace_identity_from_dict(meta.get("workspace"))
                _validate_finalizer_workspace(identity, leader, workspace_name)
                run_id = str(meta.get("run_id") or run_dir.name)
                candidates.append(
                    _CompletedFinalizer(
                        ActivePinnedWorkspace(workspace_name, identity, run_dir),
                        execution,
                        run_id,
                        _completion_time_ns(meta, meta_path),
                    )
                )
    node_states_root = _git_private_directory(
        common,
        "agent-flow",
        "current-runs",
    )
    if node_states_root.exists():
        for state_path in sorted(node_states_root.iterdir()):
            if state_path.suffix != ".json":
                continue
            state = _read_owned_json(state_path)
            if state.get("status") != "complete" and state.get("phase") != "complete":
                continue
            identity = workspace_identity_from_dict(state.get("workspace"))
            if Path(identity.workspace_root).name != workspace_name:
                continue
            _validate_finalizer_workspace(identity, leader, workspace_name)
            node_execution = execution_identity_from_dict(state.get("execution"))
            run_dir_value = state.get("run_dir")
            if not isinstance(run_dir_value, str) or not run_dir_value:
                raise WorkspaceBoundaryError(
                    "execution_finalizer_invalid: Node run directory"
                )
            run_dir = Path(run_dir_value)
            if not run_dir.is_absolute():
                run_dir = leader / run_dir
            run_id = state.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise WorkspaceBoundaryError("execution_finalizer_invalid: Node run id")
            candidates.append(
                _CompletedFinalizer(
                    ActivePinnedWorkspace(
                        workspace_name,
                        identity,
                        run_dir.resolve(strict=False),
                    ),
                    node_execution,
                    run_id,
                    _completion_time_ns(state, state_path),
                )
            )
    unique = {
        (
            candidate.execution.digest,
            candidate.run_id,
            str(candidate.workspace.run_dir.resolve(strict=False)),
        ): candidate
        for candidate in candidates
    }
    return list(unique.values())


def _completion_time_ns(payload: Mapping[str, object], source: Path) -> int:
    for key in ("completed_at", "updated_at", "started_at"):
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1_000_000_000)
    return source.lstat().st_mtime_ns


def _validate_finalizer_workspace(
    identity: WorkspaceIdentity,
    leader: Path,
    workspace_name: str,
) -> None:
    common = _authenticated_directory_root(Path(
        _git(leader, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ))
    expected_workspace = leader / ".agent-flow" / "worktrees" / workspace_name
    configured_workspace = Path(identity.workspace_root)
    if not configured_workspace.is_absolute() or configured_workspace != expected_workspace:
        raise WorkspaceBoundaryError(
            "execution_finalizer_invalid: worktree path differs from managed checkout path"
        )
    try:
        workspace_metadata = expected_workspace.lstat()
    except FileNotFoundError:
        workspace_metadata = None
    if workspace_metadata is not None and stat.S_ISLNK(workspace_metadata.st_mode):
        raise WorkspaceBoundaryError(
            "execution_finalizer_invalid: worktree path is a symbolic link"
        )
    if _authenticated_directory_root(Path(identity.git_common_dir)) != common:
        raise WorkspaceBoundaryError(
            "execution_finalizer_invalid: worktree belongs to a different git common directory"
        )
    if expected_workspace.name != workspace_name:
        raise WorkspaceBoundaryError(
            "execution_finalizer_invalid: worktree name differs from pinned identity"
        )
    if workspace_metadata is not None and (expected_workspace / ".git").exists():
        validate_workspace_identity(identity)
    elif Path(identity.workspace_root).name != workspace_name:
        raise WorkspaceBoundaryError(
            "execution_finalizer_invalid: stale worktree identity is inconsistent"
        )


def capture_workspace_identity(workspace_root: Path) -> WorkspaceIdentity:
    root = workspace_root.resolve(strict=True)
    if not root.is_dir():
        raise WorkspaceBoundaryError(f"pinned workspace is not a directory: {root}")
    metadata = root.stat()
    branch = _git(root, "branch", "--show-current")
    if not branch:
        raise WorkspaceBoundaryError(f"pinned workspace has no branch: {root}")
    git_common_dir = _git(
        root,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    git_dir = _git(root, "rev-parse", "--path-format=absolute", "--git-dir")
    git_common_dir = str(_authenticated_directory_root(Path(git_common_dir)))
    git_dir = str(_authenticated_directory_root(Path(git_dir)))
    return WorkspaceIdentity(
        workspace_root=str(root),
        git_common_dir=git_common_dir,
        git_dir=git_dir,
        branch=branch,
        head=_git(root, "rev-parse", "HEAD"),
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )


def workspace_identity_from_dict(payload: object) -> WorkspaceIdentity:
    if not isinstance(payload, dict):
        raise WorkspaceBoundaryError("pinned workspace identity is missing")
    try:
        identity = WorkspaceIdentity(
            workspace_root=str(payload["workspace_root"]),
            git_common_dir=str(payload["git_common_dir"]),
            git_dir=str(payload["git_dir"]),
            branch=str(payload["branch"]),
            head=str(payload["head"]),
            device=int(payload["device"]),
            inode=int(payload["inode"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkspaceBoundaryError("pinned workspace identity is invalid") from exc
    if not all((identity.workspace_root, identity.git_common_dir, identity.git_dir, identity.branch, identity.head)):
        raise WorkspaceBoundaryError("pinned workspace identity is invalid")
    return identity


def validate_workspace_identity(identity: WorkspaceIdentity) -> Path:
    configured = Path(identity.workspace_root)
    try:
        root = configured.resolve(strict=True)
    except FileNotFoundError as exc:
        raise WorkspaceBoundaryError(f"pinned workspace is missing: {configured}") from exc
    if not root.is_dir():
        raise WorkspaceBoundaryError(f"pinned workspace is not a directory: {root}")
    if str(root) != identity.workspace_root:
        raise WorkspaceBoundaryError(
            f"pinned workspace canonical path changed: expected={identity.workspace_root} actual={root}"
        )
    metadata = root.stat()
    if (metadata.st_dev, metadata.st_ino) != (identity.device, identity.inode):
        raise WorkspaceBoundaryError(
            f"pinned workspace filesystem identity changed: expected={identity.device}:{identity.inode} "
            f"actual={metadata.st_dev}:{metadata.st_ino}"
        )
    actual_common = _authenticated_directory_root(Path(
        _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ))
    actual_git_dir = _authenticated_directory_root(Path(
        _git(root, "rev-parse", "--path-format=absolute", "--git-dir")
    ))
    expected_common = _authenticated_directory_root(Path(identity.git_common_dir))
    expected_git_dir = _authenticated_directory_root(Path(identity.git_dir))
    if actual_common != expected_common:
        raise WorkspaceBoundaryError("pinned workspace git common directory changed")
    if actual_git_dir != expected_git_dir:
        raise WorkspaceBoundaryError("pinned workspace git directory changed")
    if _git(root, "branch", "--show-current") != identity.branch:
        raise WorkspaceBoundaryError("pinned workspace branch changed")
    current_head = _git(root, "rev-parse", "HEAD")
    ancestor = subprocess.run(
        (_git_executable(), "-C", str(root), "merge-base", "--is-ancestor", identity.head, current_head),
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if ancestor.returncode != 0:
        raise WorkspaceBoundaryError(
            f"pinned workspace HEAD diverged: pinned={identity.head} current={current_head}"
        )
    return root


def leader_root_for_identity(identity: WorkspaceIdentity) -> Path:
    common = _authenticated_directory_root(Path(identity.git_common_dir))
    if common.name != ".git":
        raise WorkspaceBoundaryError("pinned workspace git common directory is not a leader checkout")
    return common.parent.resolve(strict=True)


def capture_git_mutation_snapshot(root: Path) -> dict[str, object]:
    resolved = root.resolve(strict=True)
    try:
        status = subprocess.run(
            (
                _git_executable(),
                "-C",
                str(resolved),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ),
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceBoundaryError(f"git mutation snapshot failed: {resolved}") from exc
    if status.returncode != 0:
        raise WorkspaceBoundaryError(f"git mutation snapshot failed: {resolved}")
    records = status.stdout.split(b"\0")
    paths: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise WorkspaceBoundaryError("git mutation snapshot returned invalid status data")
        code = record[:2].decode("ascii", errors="strict")
        paths.append(record[3:].decode("utf-8", errors="surrogateescape"))
        if "R" in code or "C" in code:
            if index >= len(records) or not records[index]:
                raise WorkspaceBoundaryError("git mutation snapshot returned invalid rename data")
            paths.append(records[index].decode("utf-8", errors="surrogateescape"))
            index += 1
    states = {
        relative: _mutation_path_digest(resolved, relative)
        for relative in sorted(set(paths))
    }
    return {
        "head": _git(resolved, "rev-parse", "HEAD"),
        "status_hash": hashlib.sha256(status.stdout).hexdigest(),
        "paths": states,
    }


def mutation_paths_since(
    before: dict[str, object],
    after: dict[str, object],
) -> tuple[str, ...]:
    before_paths = before.get("paths") if isinstance(before.get("paths"), dict) else {}
    after_paths = after.get("paths") if isinstance(after.get("paths"), dict) else {}
    changed = [
        path
        for path in sorted(set(before_paths) | set(after_paths))
        if before_paths.get(path) != after_paths.get(path)
    ]
    if before.get("head") != after.get("head"):
        changed.insert(0, "<HEAD>")
    return tuple(changed)


def _mutation_path_digest(root: Path, relative: str) -> str:
    target = root / relative
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return "missing"
    digest = hashlib.sha256()
    digest.update(str(metadata.st_mode).encode("ascii"))
    digest.update(b"\0")
    if target.is_symlink():
        digest.update(target.readlink().as_posix().encode("utf-8", errors="surrogateescape"))
    elif target.is_file():
        digest.update(target.read_bytes())
    elif target.is_dir():
        digest.update(b"directory")
    else:
        digest.update(b"special")
    return digest.hexdigest()


def resolve_mutation_path(
    identity: WorkspaceIdentity,
    requested_path: str | Path,
    *,
    base_dir: Path | None = None,
    host: str,
    phase: str,
    run_dir: Path | None = None,
) -> Path:
    root = validate_workspace_identity(identity)
    allowed_roots = [root]
    if run_dir is not None:
        try:
            allowed_roots.append(Path(run_dir).resolve(strict=True))
        except OSError:
            pass
    requested = Path(requested_path)
    base = (base_dir or root).resolve(strict=True)
    candidate = requested if requested.is_absolute() else base / requested
    resolved = candidate.resolve(strict=False)
    containing = next(
        (
            allowed
            for allowed in allowed_roots
            if resolved == allowed or allowed in resolved.parents
        ),
        None,
    )
    if containing is None:
        raise WorkspaceBoundaryError(
            _boundary_diagnostic(
                requested=requested,
                resolved=resolved,
                root=root,
                host=host,
                phase=phase,
                reason="reason_code=target_outside_pinned_workspace resolved path escapes pinned workspace",
            )
        )
    existing_parent = resolved
    while not existing_parent.exists() and existing_parent != containing:
        existing_parent = existing_parent.parent
    try:
        parent_resolved = existing_parent.resolve(strict=True)
    except FileNotFoundError as exc:
        raise WorkspaceBoundaryError(
            _boundary_diagnostic(
                requested=requested,
                resolved=resolved,
                root=root,
                host=host,
                phase=phase,
                reason="reason_code=target_parent_missing target has no existing parent inside pinned workspace",
            )
        ) from exc
    if parent_resolved != containing and containing not in parent_resolved.parents:
        raise WorkspaceBoundaryError(
            _boundary_diagnostic(
                requested=requested,
                resolved=resolved,
                root=root,
                host=host,
                phase=phase,
                reason="reason_code=target_parent_outside_pinned_workspace existing parent escapes pinned workspace",
            )
        )
    return resolved


def find_active_pinned_workspaces(leader_root: Path) -> tuple[ActivePinnedWorkspace, ...]:
    leader = leader_root.resolve(strict=True)
    common = _authenticated_directory_root(Path(
        _git(leader, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ))
    runtime_root = _git_private_directory(common, "agent-flow", "worktrees")
    active: list[ActivePinnedWorkspace] = []
    if runtime_root.exists():
        for worktree_runtime in sorted(runtime_root.iterdir()):
            runtime_metadata = worktree_runtime.lstat()
            if stat.S_ISLNK(runtime_metadata.st_mode):
                raise WorkspaceBoundaryError(
                    "pinned worktree runtime metadata is a symbolic link"
                )
            if not stat.S_ISDIR(runtime_metadata.st_mode):
                continue
            runs_root = _owned_directory(worktree_runtime, ".agent-flow", "runs")
            if not runs_root.exists():
                continue
            for run_dir in sorted(runs_root.iterdir()):
                run_metadata = run_dir.lstat()
                if stat.S_ISLNK(run_metadata.st_mode):
                    raise WorkspaceBoundaryError("pinned run directory is a symbolic link")
                if not stat.S_ISDIR(run_metadata.st_mode):
                    continue
                active_path = run_dir / "active"
                if not active_path.exists() and not active_path.is_symlink():
                    continue
                active_metadata = active_path.lstat()
                if stat.S_ISLNK(active_metadata.st_mode) or not stat.S_ISREG(
                    active_metadata.st_mode
                ):
                    raise WorkspaceBoundaryError("pinned active marker is not regular")
                meta = _read_owned_json(run_dir / "meta.json")
                workspace_payload = meta.get("workspace")
                if workspace_payload is None:
                    workspace_payload = _read_owned_json(
                        worktree_runtime / "manifest.json"
                    ).get("identity")
                identity = workspace_identity_from_dict(workspace_payload)
                validate_workspace_identity(identity)
                active.append(ActivePinnedWorkspace(worktree_runtime.name, identity, run_dir))
    bindings_root = _git_private_directory(common, "agent-flow", "executions")
    if bindings_root.exists():
        for binding_path in sorted(bindings_root.glob("*.json")):
            binding = _read_owned_json(binding_path)
            if not _binding_is_active(binding):
                continue
            identity = workspace_identity_from_dict(binding.get("workspace"))
            validate_workspace_identity(identity)
            run_dir_value = binding.get("run_dir")
            if not isinstance(run_dir_value, str) or not run_dir_value:
                raise WorkspaceBoundaryError("execution_binding_invalid: run directory")
            active.append(
                ActivePinnedWorkspace(
                    str(binding.get("workspace_name") or Path(identity.workspace_root).name),
                    identity,
                    Path(run_dir_value).resolve(strict=True),
                )
            )
    node_states_root = _git_private_directory(common, "agent-flow", "current-runs")
    if node_states_root.exists():
        for state_path in sorted(node_states_root.glob("*.json")):
            node_state = _read_owned_json(state_path)
            if not _node_state_publication_is_active(leader, node_state):
                if _node_state_is_semantically_active(node_state):
                    raise WorkspaceBoundaryError(
                        _node_state_publication_error(leader, node_state)
                    )
                continue
            workspace_payload = node_state.get("workspace")
            if workspace_payload is None:
                continue
            identity = workspace_identity_from_dict(workspace_payload)
            workspace_root = validate_workspace_identity(identity)
            run_dir_value = node_state.get("run_dir")
            if not isinstance(run_dir_value, str) or not run_dir_value:
                raise WorkspaceBoundaryError("pinned Node run directory is invalid")
            run_dir = Path(run_dir_value)
            if not run_dir.is_absolute():
                run_dir = leader / run_dir
            active.append(
                ActivePinnedWorkspace(
                    Path(identity.workspace_root).name,
                    identity,
                    run_dir.resolve(strict=False),
                )
            )
    node_state_path = leader / ".agent-flow" / "state" / "current-run.json"
    if node_state_path.is_file():
        node_state = _read_json(node_state_path)
        publication_active = _node_state_publication_is_active(leader, node_state)
        if not publication_active and _node_state_is_semantically_active(node_state):
            raise WorkspaceBoundaryError(
                _node_state_publication_error(leader, node_state)
            )
        if publication_active:
            workspace_payload = node_state.get("workspace")
            if workspace_payload is not None:
                identity = workspace_identity_from_dict(workspace_payload)
                validate_workspace_identity(identity)
                run_dir_value = node_state.get("run_dir")
                if not isinstance(run_dir_value, str) or not run_dir_value:
                    raise WorkspaceBoundaryError("pinned Node run directory is invalid")
                run_dir = Path(run_dir_value)
                if not run_dir.is_absolute():
                    run_dir = leader / run_dir
                active.append(
                    ActivePinnedWorkspace(
                        Path(identity.workspace_root).name,
                        identity,
                        run_dir.resolve(strict=False),
                    )
                )
    unique = {
        (item.identity.workspace_root, str(item.run_dir.resolve(strict=False))): item
        for item in active
    }
    return tuple(unique[key] for key in sorted(unique))


def find_active_pinned_workspace(leader_root: Path) -> ActivePinnedWorkspace | None:
    active = find_active_pinned_workspaces(leader_root)
    if len(active) > 1:
        names = ", ".join(item.name for item in active)
        raise WorkspaceBoundaryError(f"multiple active pinned workspaces: {names}")
    return active[0] if active else None


def _git_private_directory(
    git_common_dir: Path,
    *components: str,
    create: bool = False,
) -> Path:
    return _owned_directory(
        _authenticated_directory_root(git_common_dir),
        *components,
        create=create,
    )


def _authenticated_directory_root(directory: Path) -> Path:
    if not directory.is_absolute():
        raise WorkspaceBoundaryError("git-private metadata root is not absolute")
    current = Path(directory.anchor)
    for component in directory.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError as exc:
            raise WorkspaceBoundaryError(
                f"git-private metadata root is missing: {current}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            trusted_aliases = {
                Path("/tmp"): Path("/private/tmp"),
                Path("/var"): Path("/private/var"),
            }
            expected_alias = trusted_aliases.get(current) if sys.platform == "darwin" else None
            if expected_alias is not None:
                try:
                    resolved_alias = current.resolve(strict=True)
                    private_metadata = Path("/private").lstat()
                    target_metadata = expected_alias.lstat()
                    followed_metadata = current.stat()
                except (OSError, RuntimeError) as exc:
                    raise WorkspaceBoundaryError(
                        f"git-private metadata root is not an owned directory: {current}"
                    ) from exc
                if (
                    metadata.st_uid == 0
                    and resolved_alias == expected_alias
                    and private_metadata.st_uid == 0
                    and stat.S_ISDIR(private_metadata.st_mode)
                    and target_metadata.st_uid == 0
                    and stat.S_ISDIR(target_metadata.st_mode)
                    and followed_metadata.st_dev == target_metadata.st_dev
                    and followed_metadata.st_ino == target_metadata.st_ino
                ):
                    current = resolved_alias
                    continue
            raise WorkspaceBoundaryError(
                f"git-private metadata root is not an owned directory: {current}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise WorkspaceBoundaryError(
                f"git-private metadata root is not an owned directory: {current}"
            )
    return current


def _owned_directory(
    root: Path,
    *components: str,
    create: bool = False,
) -> Path:
    current = root
    for component in components:
        if not component or Path(component).name != component or component in {".", ".."}:
            raise WorkspaceBoundaryError("git-private metadata path is invalid")
        candidate = current / component
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            if not create:
                return candidate
            try:
                candidate.mkdir(mode=0o700)
            except FileExistsError:
                pass
            metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise WorkspaceBoundaryError(
                f"git-private metadata path is not an owned directory: {candidate}"
            )
        current = candidate
    return current


def authenticated_worktree_runtime_root(
    git_common_dir: Path,
    workspace_name: str,
    *,
    create: bool = False,
) -> Path:
    if (
        not workspace_name
        or Path(workspace_name).name != workspace_name
        or workspace_name in {".", ".."}
    ):
        raise WorkspaceBoundaryError("git-private worktree name is invalid")
    return _git_private_directory(
        git_common_dir,
        "agent-flow",
        "worktrees",
        workspace_name,
        create=create,
    )


def authenticated_git_private_directory(
    git_common_dir: Path,
    *components: str,
    create: bool = False,
) -> Path:
    return _git_private_directory(
        git_common_dir,
        *components,
        create=create,
    )


def _execution_binding_path(git_common_dir: Path, execution: ExecutionIdentity) -> Path:
    root = _git_private_directory(
        git_common_dir,
        "agent-flow",
        "executions",
        create=True,
    )
    return root / f"{execution.digest}.json"


def _read_owned_json(path: Path) -> dict[str, object]:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise WorkspaceBoundaryError(f"git-private metadata file is not regular: {path}")
    payload = _read_json(path)
    repeated = path.lstat()
    if repeated.st_dev != metadata.st_dev or repeated.st_ino != metadata.st_ino:
        raise WorkspaceBoundaryError(f"git-private metadata file changed while reading: {path}")
    return payload


def _remove_owned_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise WorkspaceBoundaryError(f"git-private metadata file is not regular: {path}")
    quarantine = path.with_name(
        f".{path.name}.remove-{os.getpid()}-{os.urandom(8).hex()}"
    )
    try:
        path.rename(quarantine)
    except FileNotFoundError as exc:
        raise WorkspaceBoundaryError(
            f"git-private metadata file disappeared: {path}"
        ) from exc
    moved = quarantine.lstat()
    moved_payload = _read_owned_json(quarantine)
    if (
        moved.st_dev != metadata.st_dev
        or moved.st_ino != metadata.st_ino
        or moved_payload != payload
    ):
        if not path.exists():
            quarantine.rename(path)
        raise WorkspaceBoundaryError(
            f"git-private metadata file changed before removal: {path}"
        )
    quarantine.unlink()


def _same_execution_binding(
    existing: Mapping[str, object],
    expected: Mapping[str, object],
) -> bool:
    return all(
        existing.get(key) == expected.get(key)
        for key in ("execution", "workspace", "run_id", "run_dir")
    )


def _binding_is_active(payload: Mapping[str, object]) -> bool:
    run_dir_value = payload.get("run_dir")
    if not isinstance(run_dir_value, str) or not run_dir_value:
        return False
    run_dir = Path(run_dir_value)
    active = run_dir / "active"
    if active.exists() or active.is_symlink():
        metadata = active.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return False
        if payload.get("start_claim_token") is None:
            return True
    manifest = run_dir / "manifest.json"
    if not manifest.exists() and not manifest.is_symlink():
        return False
    try:
        state = _read_owned_json(manifest)
    except WorkspaceBoundaryError:
        return False
    if state.get("status") in {"complete", "aborted"} or state.get("phase") == "complete":
        return False
    token = payload.get("start_claim_token")
    if token is None:
        return True
    if (
        not isinstance(token, str)
        or not token
        or state.get("start_claim_token") != token
        or state.get("run_id") != payload.get("run_id")
    ):
        return False
    try:
        identity = workspace_identity_from_dict(payload.get("workspace"))
    except WorkspaceBoundaryError:
        return False
    if state.get("publication_status") == "starting":
        return _starting_binding_claim_is_live(payload, identity)
    if state.get("publication_status") != "active":
        return False
    try:
        execution = execution_identity_from_dict(payload.get("execution"))
        current_root = _git_private_directory(
            Path(identity.git_common_dir),
            "agent-flow",
            "current-runs",
        )
        current_path = current_root / f"{execution.digest}.json"
        current = _read_owned_json(current_path)
    except (FileNotFoundError, WorkspaceBoundaryError):
        return False
    return (
        current.get("publication_status") == "active"
        and current.get("start_claim_token") == token
        and current.get("run_id") == payload.get("run_id")
    )


def _starting_binding_claim_is_live(
    payload: Mapping[str, object],
    identity: WorkspaceIdentity,
) -> bool:
    claims_root = _git_private_directory(
        Path(identity.git_common_dir),
        "agent-flow",
        "workspace-start-claims",
    )
    digest = hashlib.sha256(identity.workspace_root.encode("utf-8")).hexdigest()
    claim_path = claims_root / f"{digest}.lock"
    try:
        metadata = claim_path.lstat()
        claim = _read_owned_json(claim_path)
    except (FileNotFoundError, WorkspaceBoundaryError):
        return False
    pid = claim.get("pid")
    process_start_id = claim.get("process_start_id")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or claim.get("version") != 2
        or claim.get("token") != payload.get("start_claim_token")
        or claim.get("run_id") != payload.get("run_id")
        or not _workspace_claim_matches_identity(claim, identity)
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(process_start_id, str)
        or not process_start_id
        or not _process_is_alive(pid)
    ):
        return False
    current_start_id = _process_start_identity(pid)
    return current_start_id is None or current_start_id == process_start_id


def _node_state_publication_is_active(
    leader: Path,
    state: Mapping[str, object],
) -> bool:
    if state.get("status") in {"complete", "aborted"} or state.get("phase") == "complete":
        return False
    publication = state.get("publication_status")
    if publication is None:
        return True
    token = state.get("start_claim_token")
    run_dir_value = state.get("run_dir")
    if publication != "active" or not isinstance(token, str) or not token:
        return False
    if not isinstance(run_dir_value, str) or not run_dir_value:
        return False
    run_dir = Path(run_dir_value)
    if not run_dir.is_absolute():
        run_dir = leader / run_dir
    try:
        manifest = _read_owned_json(run_dir / "manifest.json")
    except (FileNotFoundError, WorkspaceBoundaryError):
        return False
    if (
        manifest.get("publication_status") != "active"
        or manifest.get("start_claim_token") != token
        or manifest.get("run_id") != state.get("run_id")
    ):
        return False
    execution_payload = state.get("execution")
    workspace_payload = state.get("workspace")
    if execution_payload is None or workspace_payload is None:
        return True
    try:
        execution = execution_identity_from_dict(execution_payload)
        identity = workspace_identity_from_dict(workspace_payload)
        binding_root = _git_private_directory(
            Path(identity.git_common_dir),
            "agent-flow",
            "executions",
        )
        binding = _read_owned_json(binding_root / f"{execution.digest}.json")
    except (FileNotFoundError, WorkspaceBoundaryError):
        return False
    return (
        binding.get("start_claim_token") == token
        and binding.get("run_id") == state.get("run_id")
        and binding.get("run_dir") == str(run_dir.resolve(strict=False))
    )


def _node_state_is_semantically_active(state: Mapping[str, object]) -> bool:
    return (
        state.get("status") not in {"complete", "aborted"}
        and state.get("phase") != "complete"
    )


def _node_state_publication_error(
    leader: Path,
    state: Mapping[str, object],
) -> str:
    if state.get("publication_status") == "active":
        try:
            execution = execution_identity_from_dict(state.get("execution"))
            identity = workspace_identity_from_dict(state.get("workspace"))
            binding_root = _git_private_directory(
                Path(identity.git_common_dir),
                "agent-flow",
                "executions",
            )
            binding_path = binding_root / f"{execution.digest}.json"
            if not binding_path.exists() and not binding_path.is_symlink():
                return "execution_binding_missing: active Node run binding is missing"
        except WorkspaceBoundaryError:
            pass
    return "execution_binding_incomplete: active Node run publication is incomplete"


def _detected_execution_host(env: Mapping[str, str]) -> str:
    if env.get("CODEX_THREAD_ID") or env.get("CODEX_CLI"):
        return "codex"
    if env.get("CLAUDE_SESSION_ID") or env.get("CLAUDECODE") or env.get("CLAUDE_CLI"):
        return "claude"
    if env.get("OMP_SESSION_ID") or env.get("OMP_PROFILE"):
        return "omp"
    return ""


def _host_session_id(host: str, env: Mapping[str, str]) -> str:
    names = {
        "codex": ("CODEX_THREAD_ID", "CODEX_SESSION_ID"),
        "claude": ("CLAUDE_SESSION_ID",),
        "omp": ("OMP_SESSION_ID",),
    }.get(host, ())
    return next((env[name] for name in names if env.get(name)), "")


def _host_agent_id(host: str, env: Mapping[str, str]) -> str:
    names = {
        "codex": ("CODEX_AGENT_ID",),
        "claude": ("CLAUDE_AGENT_ID",),
        "omp": ("OMP_AGENT_ID",),
    }.get(host, ())
    return next((env[name] for name in names if env.get(name)), "")


def _boundary_diagnostic(
    *,
    requested: Path,
    resolved: Path,
    root: Path,
    host: str,
    phase: str,
    reason: str,
) -> str:
    return (
        f"write boundary rejected: requested_path={requested} resolved_path={resolved} "
        f"pinned_workspace_root={root} host={host or 'unknown'} phase={phase or 'unknown'} reason={reason}"
    )


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceBoundaryError(f"pinned workspace metadata is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise WorkspaceBoundaryError(f"pinned workspace metadata is invalid: {path}")
    return payload


def _git(root: Path, *args: str) -> str:
    command = _git_executable()
    try:
        result = subprocess.run(
            (command, "-C", str(root), *args),
            text=True,
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceBoundaryError(f"git workspace identity check failed: {root}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise WorkspaceBoundaryError(f"git workspace identity check failed at {root}: {detail}")
    return result.stdout.strip()


def _git_executable() -> str:
    configured = os.environ.get("AGENT_FLOW_GIT_EXECUTABLE")
    return configured if configured and Path(configured).is_absolute() else "git"
