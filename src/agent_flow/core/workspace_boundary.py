from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
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
        "version": 1,
        "execution": execution.to_dict(),
        "workspace": identity.to_dict(),
        "workspace_name": Path(identity.workspace_root).name,
        "run_id": run_id,
        "run_dir": str(resolved_run_dir),
        "bound_at": datetime.now(timezone.utc).isoformat(),
    }
    if binding_path.is_file():
        existing = _read_json(binding_path)
        if _binding_is_active(existing) and (
            existing.get("run_dir") != payload["run_dir"]
            or existing.get("workspace") != payload["workspace"]
        ):
            raise WorkspaceBoundaryError(
                "execution_binding_conflict: execution is already bound to an active workspace"
            )
    binding_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = binding_path.with_name(f".{binding_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, binding_path)
    return binding_path


def acquire_workspace_start_claim(identity: WorkspaceIdentity) -> WorkspaceStartClaim:
    claims_root = Path(identity.git_common_dir).resolve(strict=True) / "agent-flow" / "workspace-start-claims"
    claims_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(identity.workspace_root.encode("utf-8")).hexdigest()
    claim_path = claims_root / f"{digest}.lock"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    token = os.urandom(16).hex()
    descriptor: int | None = None
    for attempt in range(2):
        try:
            descriptor = os.open(claim_path, flags, 0o600)
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
    if descriptor is None:
        raise WorkspaceBoundaryError(
            "execution_binding_conflict: workspace start claim is unavailable"
        )
    try:
        payload = json.dumps(
            {
                "version": 1,
                "pid": os.getpid(),
                "token": token,
                "workspace_root": identity.workspace_root,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("workspace claim write failed")
            view = view[written:]
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
    except Exception:
        try:
            claim_path.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)
    return WorkspaceStartClaim(claim_path, metadata.st_dev, metadata.st_ino)


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
    claim.path.unlink()


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
    if (
        payload.get("version") != 1
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(token, str)
        or not token
        or payload.get("workspace_root") != identity.workspace_root
        or _process_is_alive(pid)
    ):
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
        or moved_payload.get("token") != token
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
    if not binding_path.is_file():
        return
    payload = _read_json(binding_path)
    if run_dir is not None and payload.get("run_dir") != str(run_dir.resolve(strict=False)):
        raise WorkspaceBoundaryError("execution_binding_conflict: run identity changed")
    binding_path.unlink()


def resolve_execution_workspace(
    leader_root: Path,
    execution: ExecutionIdentity,
) -> ActivePinnedWorkspace:
    leader = leader_root.resolve(strict=True)
    common = Path(
        _git(leader, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve(strict=True)
    binding_path = _execution_binding_path(common, execution)
    if not binding_path.is_file():
        raise WorkspaceBoundaryError(
            "execution_binding_missing: active execution is not bound to a worktree"
        )
    binding = _read_json(binding_path)
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


def capture_workspace_identity(workspace_root: Path) -> WorkspaceIdentity:
    root = workspace_root.resolve(strict=True)
    if not root.is_dir():
        raise WorkspaceBoundaryError(f"pinned workspace is not a directory: {root}")
    metadata = root.stat()
    branch = _git(root, "branch", "--show-current")
    if not branch:
        raise WorkspaceBoundaryError(f"pinned workspace has no branch: {root}")
    return WorkspaceIdentity(
        workspace_root=str(root),
        git_common_dir=_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"),
        git_dir=_git(root, "rev-parse", "--path-format=absolute", "--git-dir"),
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
    actual_common = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)
    actual_git_dir = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-dir")).resolve(strict=True)
    if actual_common != Path(identity.git_common_dir).resolve(strict=True):
        raise WorkspaceBoundaryError("pinned workspace git common directory changed")
    if actual_git_dir != Path(identity.git_dir).resolve(strict=True):
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
    common = Path(identity.git_common_dir).resolve(strict=True)
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
) -> Path:
    root = validate_workspace_identity(identity)
    requested = Path(requested_path)
    base = (base_dir or root).resolve(strict=True)
    candidate = requested if requested.is_absolute() else base / requested
    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
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
    while not existing_parent.exists() and existing_parent != root:
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
    if parent_resolved != root and root not in parent_resolved.parents:
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
    common = Path(_git(leader, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)
    runtime_root = common / "agent-flow" / "worktrees"
    active: list[ActivePinnedWorkspace] = []
    if runtime_root.is_dir():
        for worktree_runtime in sorted(runtime_root.iterdir()):
            runs_root = worktree_runtime / ".agent-flow" / "runs"
            if not runs_root.is_dir():
                continue
            for run_dir in sorted(runs_root.iterdir()):
                if not (run_dir / "active").is_file():
                    continue
                meta = _read_json(run_dir / "meta.json")
                workspace_payload = meta.get("workspace")
                if workspace_payload is None:
                    workspace_payload = _read_json(worktree_runtime / "manifest.json").get("identity")
                identity = workspace_identity_from_dict(workspace_payload)
                validate_workspace_identity(identity)
                active.append(ActivePinnedWorkspace(worktree_runtime.name, identity, run_dir))
    bindings_root = common / "agent-flow" / "executions"
    if bindings_root.is_dir():
        for binding_path in sorted(bindings_root.glob("*.json")):
            binding = _read_json(binding_path)
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
    node_states_root = common / "agent-flow" / "current-runs"
    if node_states_root.is_dir():
        for state_path in sorted(node_states_root.glob("*.json")):
            node_state = _read_json(state_path)
            if node_state.get("status") in {"complete", "aborted"} or node_state.get("phase") == "complete":
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
        if node_state.get("status") not in {"complete", "aborted"} and node_state.get("phase") != "complete":
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


def _execution_binding_path(git_common_dir: Path, execution: ExecutionIdentity) -> Path:
    return git_common_dir.resolve(strict=True) / "agent-flow" / "executions" / f"{execution.digest}.json"


def _binding_is_active(payload: Mapping[str, object]) -> bool:
    run_dir_value = payload.get("run_dir")
    if not isinstance(run_dir_value, str) or not run_dir_value:
        return False
    run_dir = Path(run_dir_value)
    if (run_dir / "active").is_file():
        return True
    manifest = run_dir / "manifest.json"
    if not manifest.is_file():
        return False
    state = _read_json(manifest)
    return state.get("status") not in {"complete", "aborted"} and state.get("phase") != "complete"


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
