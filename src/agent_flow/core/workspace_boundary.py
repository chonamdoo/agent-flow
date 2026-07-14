from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


GIT_TIMEOUT_SECONDS = 10


class WorkspaceBoundaryError(RuntimeError):
    pass


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
        ("git", "-C", str(root), "merge-base", "--is-ancestor", identity.head, current_head),
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


def resolve_mutation_path(
    identity: WorkspaceIdentity,
    requested_path: str | Path,
    *,
    host: str,
    phase: str,
) -> Path:
    root = validate_workspace_identity(identity)
    requested = Path(requested_path)
    candidate = requested if requested.is_absolute() else root / requested
    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise WorkspaceBoundaryError(
            _boundary_diagnostic(
                requested=requested,
                resolved=resolved,
                root=root,
                host=host,
                phase=phase,
                reason="resolved path escapes pinned workspace",
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
                reason="target has no existing parent inside pinned workspace",
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
                reason="existing parent escapes pinned workspace",
            )
        )
    return resolved


def find_active_pinned_workspace(leader_root: Path) -> ActivePinnedWorkspace | None:
    leader = leader_root.resolve(strict=True)
    common = Path(_git(leader, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)
    runtime_root = common / "agent-flow" / "worktrees"
    if not runtime_root.is_dir():
        return None
    active: list[ActivePinnedWorkspace] = []
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
    if len(active) > 1:
        names = ", ".join(item.name for item in active)
        raise WorkspaceBoundaryError(f"multiple active pinned workspaces: {names}")
    return active[0] if active else None


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
    try:
        result = subprocess.run(
            ("git", "-C", str(root), *args),
            text=True,
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceBoundaryError(f"git workspace identity check failed: {root}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise WorkspaceBoundaryError(f"git workspace identity check failed: {detail}")
    return result.stdout.strip()
