from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path

from agent_flow.core.commands import run_safe_command
from agent_flow.core.workspace_boundary import (
    WorkspaceBoundaryError,
    acquire_workspace_start_claim,
    authenticated_git_private_directory,
    authenticated_worktree_runtime_root,
    capture_workspace_identity,
    leader_root_for_identity,
    release_workspace_start_claim,
    workspace_identity_from_dict,
)


PROTECTED_WORKTREE_BRANCHES = {"main", "master", "develop"}
GIT_WORKTREE_TIMEOUT_S = 300
_EXPECTED_DELETE_HOOK = """#!/bin/sh
state=$1
payload=$(cat) || exit 1
if [ -n "$AGENT_FLOW_ORIGINAL_REFERENCE_TRANSACTION_HOOK" ]; then
    printf '%s\n' "$payload" | "$AGENT_FLOW_ORIGINAL_REFERENCE_TRANSACTION_HOOK" "$state" || exit $?
fi
if [ "$state" != "prepared" ]; then
    exit 0
fi
case "$payload" in
*'
'*) exit 1 ;;
esac
set -f
IFS=' ' read -r old_oid new_oid ref_name extra <<EOF
$payload
EOF
if [ -n "$extra" ] || [ "$old_oid" != "$AGENT_FLOW_EXPECTED_OLD_OID" ] || [ "$ref_name" != "$AGENT_FLOW_EXPECTED_REF" ]; then
    exit 1
fi
case "$new_oid" in
0000000000000000000000000000000000000000|0000000000000000000000000000000000000000000000000000000000000000) ;;
*) exit 1 ;;
esac
worktrees=$("$AGENT_FLOW_GIT_EXECUTABLE" worktree list --porcelain) || exit 1
while IFS= read -r line; do
    if [ "$line" = "branch $AGENT_FLOW_EXPECTED_REF" ]; then
        exit 1
    fi
done <<EOF
$worktrees
EOF
exit 0
"""
_HELD_WORKTREE_LIFECYCLE_LOCKS: ContextVar[frozenset[str]] = ContextVar(
    "held_worktree_lifecycle_locks",
    default=frozenset(),
)


@dataclass(frozen=True)
class WorktreePlan:
    name: str
    branch: str
    path: Path
    base_ref: str
    branch_explicit: bool = False
    requested_name: str = ""


@dataclass(frozen=True)
class WorktreeStatus:
    name: str
    branch: str
    path: Path
    exists: bool
    branch_created_by_agent_flow: bool = False
    requested_name: str = ""
    identity: dict[str, object] | None = None


def plan_worktree(*, root: Path, name: str, branch: str | None = None) -> WorktreePlan:
    safe_name = _feature_worktree_name(name)
    selected_branch = branch or f"feat/{safe_name.removeprefix('feat-')}"
    _validate_branch(selected_branch)
    if selected_branch in PROTECTED_WORKTREE_BRANCHES:
        raise ValueError(f"protected worktree branch is not allowed: {selected_branch}")
    if not selected_branch.startswith("feat/"):
        raise ValueError(f"worktree branch must start with feat/: {selected_branch}")
    return WorktreePlan(
        name=safe_name,
        branch=selected_branch,
        path=root / ".agent-flow" / "worktrees" / safe_name,
        # leader worktree가 feature branch여도 새 작업은 기본 브랜치 commit에서 시작한다.
        base_ref=_default_base_ref(root),
        branch_explicit=branch is not None,
        requested_name=name,
    )


def create_worktree(*, root: Path, plan: WorktreePlan, allow_dirty: bool = False) -> WorktreeStatus:
    with worktree_lifecycle_lock(root=root):
        return _create_worktree(root=root, plan=plan, allow_dirty=allow_dirty)


def _create_worktree(*, root: Path, plan: WorktreePlan, allow_dirty: bool) -> WorktreeStatus:
    if not allow_dirty and _git_dirty(root):
        raise RuntimeError("leader workspace is dirty; pass --allow-dirty to create a worktree anyway")
    if plan.path.exists():
        if not (plan.path / ".git").exists():
            raise RuntimeError(
                f"worktree path exists but is not a git worktree: {plan.path}. "
                f"Run `agent-flow worktree remove --name {plan.name}` to clear stale state."
            )
        existing = get_worktree_status(root=root, name=plan.name)
        if plan.branch_explicit and existing.branch != plan.branch:
            raise ValueError(
                f"worktree {plan.name} already uses branch {existing.branch}; "
                f"requested {plan.branch}"
            )
        _assert_same_requested_name(root=root, plan=plan)
        return existing
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    if worktree_branch_exists(root=root, branch=plan.branch):
        _run_git(root, "worktree", "add", str(plan.path), plan.branch)
        branch_created = False
    else:
        _run_git(root, "worktree", "add", "-b", plan.branch, str(plan.path), plan.base_ref)
        branch_created = True
    status = WorktreeStatus(
        name=plan.name,
        branch=plan.branch,
        path=plan.path,
        exists=True,
        branch_created_by_agent_flow=branch_created,
        requested_name=plan.requested_name,
        identity=capture_workspace_identity(plan.path).to_dict(),
    )
    try:
        write_worktree_manifest(root=root, status=status)
    except Exception:
        try:
            _remove_worktree(root=root, status=status)
        except Exception:
            pass
        raise
    return status


def remove_worktree(*, root: Path, status: WorktreeStatus, delete_branch: bool = True) -> None:
    with worktree_lifecycle_lock(root=root):
        _remove_worktree(root=root, status=status, delete_branch=delete_branch)


def _remove_worktree(*, root: Path, status: WorktreeStatus, delete_branch: bool = True) -> None:
    branch_to_delete, preserved_tip = validate_worktree_removal(
        root=root,
        status=status,
        delete_branch=delete_branch,
    )

    if status.path.exists():
        _run_git(root, "worktree", "remove", str(status.path))
    if branch_to_delete is not None:
        assert preserved_tip is not None
        _delete_worktree_branch_at_tip(
            root=root,
            branch=branch_to_delete,
            expected_tip=preserved_tip,
        )
    remove_worktree_metadata(root=root, name=status.name)


def validate_worktree_removal(
    *,
    root: Path,
    status: WorktreeStatus,
    delete_branch: bool = True,
) -> tuple[str | None, str | None]:
    branch_to_delete = _owned_branch_for_live_worktree(root=root, status=status) if delete_branch else None
    preserved_tip = (
        preserved_worktree_branch_tip(root=root, branch=branch_to_delete)
        if branch_to_delete is not None
        else None
    )
    if branch_to_delete is not None and preserved_tip is None:
        raise RuntimeError(
            f"refusing to remove worktree with unpreserved branch commits: {branch_to_delete}"
        )
    if status.path.exists():
        dirty = _run_git(status.path, "status", "--porcelain=v1", "--untracked-files=all")
        if dirty.stdout.strip():
            raise RuntimeError(f"worktree has uncommitted changes: {status.path}")
    return branch_to_delete, preserved_tip


def _assert_same_requested_name(*, root: Path, plan: WorktreePlan) -> None:
    # 서로 다른 이름이 같은 safe name으로 정규화되면 기존 worktree를 조용히
    # 재사용해 상태가 섞일 수 있다. 명시적 별칭 재사용(--worktree)은 지원되는
    # 플로우라 차단하지 않고, 기록된 원본 이름과 다르면 경고만 남긴다.
    manifest = _worktree_manifest_path(root=root, name=plan.name)
    if not manifest.exists():
        return
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    recorded = payload.get("requested_name")
    if not isinstance(recorded, str) or not recorded or not plan.requested_name:
        return
    if recorded != plan.requested_name:
        print(
            f"warning: worktree {plan.name} was created for '{recorded}'; "
            f"reusing it for '{plan.requested_name}'",
            file=sys.stderr,
        )


def worktree_branch_exists(*, root: Path, branch: str) -> bool:
    result = run_safe_command(("git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"), cwd=root)
    return result.ok


def worktree_branch_is_preserved(*, root: Path, branch: str) -> bool:
    return preserved_worktree_branch_tip(root=root, branch=branch) is not None


def preserved_worktree_branch_tip(*, root: Path, branch: str) -> str | None:
    tip = run_safe_command(
        ("git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"),
        cwd=root,
    )
    if not tip.ok or not tip.stdout.strip():
        return None
    containing = run_safe_command(
        (
            "git",
            "for-each-ref",
            f"--contains={tip.stdout.strip()}",
            "--format=%(refname)",
            "refs/heads",
            "refs/remotes",
            "refs/tags",
        ),
        cwd=root,
    )
    if not containing.ok:
        return None
    owned_ref = f"refs/heads/{branch}"
    if not any(
        ref.strip() and ref.strip() != owned_ref
        for ref in containing.stdout.splitlines()
    ):
        return None
    return tip.stdout.strip()


def delete_worktree_branch_at_tip(
    *,
    root: Path,
    branch: str,
    expected_tip: str,
) -> None:
    with worktree_lifecycle_lock(root=root):
        _delete_worktree_branch_at_tip(
            root=root,
            branch=branch,
            expected_tip=expected_tip,
        )


def _delete_worktree_branch_at_tip(
    *,
    root: Path,
    branch: str,
    expected_tip: str,
) -> None:
    _validate_branch(branch)
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", expected_tip):
        raise RuntimeError("refusing to delete worktree branch with an invalid pinned tip")
    if _worktree_branch_is_checked_out(root=root, branch=branch):
        raise RuntimeError(
            f"refusing to delete worktree branch checked out in another worktree: {branch}"
        )
    branch_ref = f"refs/heads/{branch}"
    with _expected_delete_reference_transaction_hook(
        root=root,
        branch_ref=branch_ref,
        expected_tip=expected_tip,
    ) as (hook_dir, hook_env):
        _run_git(
            root,
            "-c",
            f"core.hooksPath={hook_dir}",
            "update-ref",
            "-d",
            branch_ref,
            expected_tip,
            env_extra=hook_env,
        )


def _worktree_branch_is_checked_out(*, root: Path, branch: str) -> bool:
    listed = _run_git(root, "worktree", "list", "--porcelain", "-z")
    branch_ref = f"branch refs/heads/{branch}"
    return branch_ref in listed.stdout.split("\0")


def _default_base_ref(root: Path) -> str:
    for ref in ("main", "origin/main", "master", "origin/master", "develop", "origin/develop"):
        if _git_commit_ref_exists(root=root, ref=ref):
            return ref
    return "HEAD"


def _git_commit_ref_exists(*, root: Path, ref: str) -> bool:
    result = run_safe_command(("git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"), cwd=root)
    # git을 호출할 수 없으면 기본 ref 후보가 없는 것으로 보고 HEAD fallback을 쓴다.
    return result.ok


def get_worktree_status(*, root: Path, name: str) -> WorktreeStatus:
    plan = plan_worktree(root=root, name=name)
    canonical_manifest = _worktree_manifest_path(root=root, name=plan.name)
    manifest = canonical_manifest
    legacy_manifest = _legacy_worktree_manifest_path(root=root, name=plan.name)
    if canonical_manifest.exists() or canonical_manifest.is_symlink():
        metadata = canonical_manifest.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise WorkspaceBoundaryError(
                f"worktree ownership manifest is not regular: {canonical_manifest}"
            )
    if not manifest.exists() and legacy_manifest.exists():
        manifest = legacy_manifest
    if manifest.exists():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return WorktreeStatus(
                name=plan.name,
                branch=plan.branch,
                path=plan.path,
                exists=plan.path.exists(),
                branch_created_by_agent_flow=False,
            )
        manifest_name = payload.get("name", plan.name)
        status_name = plan.name
        if isinstance(manifest_name, str):
            try:
                manifest_name_safe = _safe_component(manifest_name)
            except ValueError:
                manifest_name_safe = ""
            if manifest_name_safe == plan.name:
                status_name = plan.name

        manifest_branch = payload.get("branch", plan.branch)
        status_branch = plan.branch
        manifest_branch_valid = False
        if isinstance(manifest_branch, str):
            try:
                _validate_branch(manifest_branch)
            except ValueError:
                manifest_branch_valid = False
            else:
                status_branch = manifest_branch
                manifest_branch_valid = True

        branch_created = (
            canonical_manifest.is_file()
            and not canonical_manifest.is_symlink()
            and payload.get("branch_created_by_agent_flow") is True
            and manifest_branch_valid
            and status_branch == plan.branch
        )
        return WorktreeStatus(
            name=status_name,
            branch=status_branch,
            path=plan.path,
            exists=plan.path.exists(),
            branch_created_by_agent_flow=branch_created,
            identity=_validated_manifest_identity(payload.get("identity")),
        )
    return WorktreeStatus(
        name=plan.name,
        branch=plan.branch,
        path=plan.path,
        exists=plan.path.exists(),
        branch_created_by_agent_flow=False,
    )


def write_worktree_manifest(*, root: Path, status: WorktreeStatus) -> Path:
    path = authenticated_worktree_runtime_root(
        _agent_flow_git_dir(root).parent,
        status.name,
        create=True,
    ) / "manifest.json"
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise WorkspaceBoundaryError(
                f"worktree ownership manifest is not regular: {path}"
            )
    payload = asdict(status)
    payload["path"] = str(status.path.relative_to(root))
    payload["leader_root"] = str(root)
    payload["identity"] = status.identity or capture_workspace_identity(status.path).to_dict()
    path.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
    return path


def _validated_manifest_identity(payload: object) -> dict[str, object] | None:
    if payload is None:
        return None
    identity = workspace_identity_from_dict(payload)
    return identity.to_dict()


def worktree_runtime_root(*, root: Path, name: str) -> Path:
    return authenticated_worktree_runtime_root(
        _agent_flow_git_dir(root).parent,
        _feature_worktree_name(name),
    )


def known_worktree_names(*, root: Path) -> list[str]:
    names: set[str] = set()
    checkout_root = root / ".agent-flow" / "worktrees"
    if checkout_root.exists() or checkout_root.is_symlink():
        checkout_metadata = checkout_root.lstat()
        if stat.S_ISLNK(checkout_metadata.st_mode) or not stat.S_ISDIR(
            checkout_metadata.st_mode
        ):
            raise WorkspaceBoundaryError(
                f"worktree checkout root is not an owned directory: {checkout_root}"
            )
        names.update(
            path.name
            for path in checkout_root.iterdir()
            if path.is_dir() and not path.is_symlink()
        )
    runtime_root = authenticated_git_private_directory(
        _agent_flow_git_dir(root).parent,
        "agent-flow",
        "worktrees",
    )
    if runtime_root.exists():
        for path in runtime_root.iterdir():
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise WorkspaceBoundaryError(
                    f"worktree runtime entry is a symbolic link: {path}"
                )
            if stat.S_ISDIR(metadata.st_mode):
                names.add(path.name)
    return sorted(names)


def remove_worktree_metadata(*, root: Path, name: str) -> None:
    runtime_root = worktree_runtime_root(root=root, name=name)
    if runtime_root.exists() or runtime_root.is_symlink():
        metadata = runtime_root.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise WorkspaceBoundaryError(
                f"refusing to remove unowned worktree runtime metadata: {runtime_root}"
            )
        _remove_owned_worktree_runtime(
            runtime_root,
            expected_identity=(metadata.st_dev, metadata.st_ino, stat.S_IFDIR),
        )
    current_runs = authenticated_git_private_directory(
        _agent_flow_git_dir(root).parent,
        "agent-flow",
        "current-runs",
    )
    if current_runs.exists():
        for state_path in current_runs.glob("*.json"):
            metadata = state_path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise WorkspaceBoundaryError(
                    f"worktree run state is not regular: {state_path}"
                )
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                identity = workspace_identity_from_dict(state.get("workspace"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if (
                state.get("status") == "complete"
                or state.get("phase") == "complete"
            ) and (
                Path(identity.workspace_root).name == name
                and Path(identity.git_common_dir).resolve(strict=False)
                == _agent_flow_git_dir(root).parent.resolve(strict=False)
            ):
                _remove_completed_node_state_atomic(
                    state_path,
                    metadata,
                    state,
                )


def _remove_owned_worktree_runtime(
    runtime_root: Path,
    expected_identity: tuple[int, int, int] | None = None,
) -> None:
    metadata = runtime_root.lstat()
    identity = (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))
    if expected_identity is not None and identity != expected_identity:
        raise WorkspaceBoundaryError(
            f"worktree runtime changed before removal: {runtime_root}"
        )
    quarantine = runtime_root.with_name(
        f".{runtime_root.name}.remove-{os.getpid()}-{os.urandom(8).hex()}"
    )
    runtime_root.rename(quarantine)
    try:
        moved = quarantine.lstat()
        if (
            stat.S_ISLNK(moved.st_mode)
            or not stat.S_ISDIR(moved.st_mode)
            or moved.st_dev != metadata.st_dev
            or moved.st_ino != metadata.st_ino
        ):
            raise WorkspaceBoundaryError(
                f"worktree runtime changed before removal: {runtime_root}"
            )
        snapshot = _validate_worktree_runtime_layout(quarantine)
        _remove_owned_tree_snapshot(quarantine, snapshot)
    except (OSError, WorkspaceBoundaryError):
        if quarantine.exists() and not runtime_root.exists():
            quarantine.rename(runtime_root)
        raise


def _validate_worktree_runtime_layout(
    runtime_root: Path,
) -> dict[str, tuple[int, int, int]]:
    allowed_root = {"manifest.json", "finalizer.json", ".agent-flow"}
    unexpected = sorted(
        entry.name for entry in runtime_root.iterdir() if entry.name not in allowed_root
    )
    if unexpected:
        raise WorkspaceBoundaryError(
            "refusing to remove worktree runtime with unowned files: "
            f"{runtime_root} ({', '.join(unexpected)})"
        )
    state_root = runtime_root / ".agent-flow"
    if state_root.exists() or state_root.is_symlink():
        metadata = state_root.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise WorkspaceBoundaryError("worktree runtime state root is not an owned directory")
        allowed_state = {"handoffs", "runs", "state", "team"}
        unexpected_state = sorted(
            entry.name for entry in state_root.iterdir() if entry.name not in allowed_state
        )
        if unexpected_state:
            raise WorkspaceBoundaryError(
                "refusing to remove worktree runtime with unowned state: "
                + ", ".join(unexpected_state)
            )
    snapshot: dict[str, tuple[int, int, int]] = {}
    _validate_owned_tree_types(runtime_root, runtime_root, snapshot)
    return snapshot


def _validate_owned_tree_types(
    root: Path,
    snapshot_root: Path,
    snapshot: dict[str, tuple[int, int, int]],
) -> None:
    for entry in root.iterdir():
        metadata = entry.lstat()
        relative = entry.relative_to(snapshot_root).as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            raise WorkspaceBoundaryError(
                f"refusing to remove symbolic link from worktree runtime: {entry}"
            )
        if stat.S_ISDIR(metadata.st_mode):
            snapshot[relative] = (metadata.st_dev, metadata.st_ino, stat.S_IFDIR)
            _validate_owned_tree_types(entry, snapshot_root, snapshot)
        elif not stat.S_ISREG(metadata.st_mode):
            raise WorkspaceBoundaryError(
                f"refusing to remove special file from worktree runtime: {entry}"
            )
        else:
            snapshot[relative] = (metadata.st_dev, metadata.st_ino, stat.S_IFREG)


def _remove_owned_tree_snapshot(
    root: Path,
    snapshot: dict[str, tuple[int, int, int]],
    relative_root: str = "",
) -> None:
    prefix = f"{relative_root}/" if relative_root else ""
    children = sorted(
        relative
        for relative in snapshot
        if relative.startswith(prefix) and "/" not in relative[len(prefix) :]
    )
    for relative in children:
        entry = root / relative[len(prefix) :]
        expected_device, expected_inode, expected_type = snapshot[relative]
        metadata = entry.lstat()
        if (
            metadata.st_dev != expected_device
            or metadata.st_ino != expected_inode
            or stat.S_IFMT(metadata.st_mode) != expected_type
        ):
            raise WorkspaceBoundaryError(
                f"worktree runtime entry changed before removal: {entry}"
            )
        if expected_type == stat.S_IFDIR:
            removal = entry.with_name(
                f".{entry.name}.remove-{os.getpid()}-{os.urandom(8).hex()}"
            )
            entry.rename(removal)
            moved = removal.lstat()
            if (
                stat.S_ISLNK(moved.st_mode)
                or not stat.S_ISDIR(moved.st_mode)
                or moved.st_dev != metadata.st_dev
                or moved.st_ino != metadata.st_ino
            ):
                if not entry.exists() and not entry.is_symlink():
                    removal.rename(entry)
                raise WorkspaceBoundaryError(
                    f"worktree runtime entry changed before removal: {entry}"
                )
            try:
                _remove_owned_tree_snapshot(removal, snapshot, relative)
            except (OSError, WorkspaceBoundaryError):
                if removal.exists() and not entry.exists():
                    removal.rename(entry)
                raise
            continue
        removal = entry.with_name(
            f".{entry.name}.remove-{os.getpid()}-{os.urandom(8).hex()}"
        )
        entry.rename(removal)
        moved = removal.lstat()
        if (
            stat.S_ISLNK(moved.st_mode)
            or not stat.S_ISREG(moved.st_mode)
            or moved.st_dev != metadata.st_dev
            or moved.st_ino != metadata.st_ino
        ):
            if not entry.exists() and not entry.is_symlink():
                removal.rename(entry)
            raise WorkspaceBoundaryError(
                f"worktree runtime entry changed before removal: {entry}"
            )
        removal.unlink()
    root.rmdir()


def _remove_completed_node_state_atomic(
    state_path: Path,
    metadata: os.stat_result,
    expected: dict[str, object],
) -> None:
    quarantine = state_path.with_name(
        f".{state_path.name}.cleanup-{os.getpid()}-{os.urandom(8).hex()}"
    )
    state_path.rename(quarantine)
    moved = quarantine.lstat()
    try:
        current = json.loads(quarantine.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if not state_path.exists():
            quarantine.rename(state_path)
        raise WorkspaceBoundaryError("worktree run state changed during cleanup") from exc
    if (
        moved.st_dev != metadata.st_dev
        or moved.st_ino != metadata.st_ino
        or current != expected
    ):
        if not state_path.exists():
            quarantine.rename(state_path)
        raise WorkspaceBoundaryError("worktree run state changed during cleanup")
    quarantine.unlink()


def _git_dirty(root: Path) -> bool:
    result = _run_git(root, "status", "--porcelain")
    dirty_lines = [
        line
        for line in result.stdout.splitlines()
        if not _is_agent_flow_status_line(line)
    ]
    return bool(dirty_lines)


def _worktree_manifest_path(*, root: Path, name: str) -> Path:
    return worktree_runtime_root(root=root, name=name) / "manifest.json"


def _legacy_worktree_manifest_path(*, root: Path, name: str) -> Path:
    return root / ".agent-flow" / "worktrees" / _feature_worktree_name(name) / "manifest.json"


def _agent_flow_git_dir(root: Path) -> Path:
    result = run_safe_command(("git", "rev-parse", "--git-common-dir"), cwd=root)
    if not result.ok:
        return root / ".agent-flow"
    git_common = Path(result.stdout.strip())
    if not git_common.is_absolute():
        git_common = root / git_common
    return git_common / "agent-flow"


def _is_agent_flow_status_line(line: str) -> bool:
    path = line[3:] if len(line) > 3 else ""
    return path == ".agent-flow" or path.startswith(".agent-flow/")


def _run_git(
    root: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    # worktree add/remove는 큰 저장소나 느린 디스크에서 30초를 넘을 수 있어 별도 여유를 둔다.
    if env_extra is None:
        result = run_safe_command(
            ("git", *args),
            cwd=root,
            timeout_s=GIT_WORKTREE_TIMEOUT_S,
        )
    else:
        result = run_safe_command(
            ("git", *args),
            cwd=root,
            env_extra=env_extra,
            timeout_s=GIT_WORKTREE_TIMEOUT_S,
        )
    if not result.ok:
        # 호출자는 기존 subprocess 예외 경로로 처리하므로 형태를 유지한다.
        raise subprocess.CalledProcessError(
            result.returncode or 1,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )
    return subprocess.CompletedProcess(result.args, result.returncode or 0, result.stdout, result.stderr)


@contextmanager
def _expected_delete_reference_transaction_hook(
    *,
    root: Path,
    branch_ref: str,
    expected_tip: str,
):
    original_hook = _reference_transaction_hook(root)
    with tempfile.TemporaryDirectory(prefix="agent-flow-ref-delete-") as temporary:
        hook_dir = Path(temporary)
        hook_path = hook_dir / "reference-transaction"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(hook_path, flags, 0o700)
        try:
            payload = _EXPECTED_DELETE_HOOK.encode("utf-8")
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("reference transaction hook write failed")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        yield hook_dir, {
            "AGENT_FLOW_GIT_EXECUTABLE": _git_executable_for_hook(),
            "AGENT_FLOW_EXPECTED_OLD_OID": expected_tip,
            "AGENT_FLOW_EXPECTED_REF": branch_ref,
            "AGENT_FLOW_ORIGINAL_REFERENCE_TRANSACTION_HOOK": (
                str(original_hook) if original_hook is not None else ""
            ),
        }


def _git_executable_for_hook() -> str:
    configured = os.environ.get("AGENT_FLOW_GIT_EXECUTABLE")
    if configured and Path(configured).is_absolute():
        return configured
    return shutil.which("git") or "/usr/bin/git"


def _reference_transaction_hook(root: Path) -> Path | None:
    configured = run_safe_command(
        ("git", "config", "--path", "--get", "core.hooksPath"),
        cwd=root,
    )
    if configured.returncode not in {0, 1} or configured.error is not None:
        raise RuntimeError("failed to resolve Git hooks path")
    if configured.stdout.strip():
        hooks_dir = Path(configured.stdout.strip())
        if not hooks_dir.is_absolute():
            hooks_dir = root / hooks_dir
        hook = hooks_dir / "reference-transaction"
    else:
        hook_path = _run_git(root, "rev-parse", "--git-path", "hooks/reference-transaction")
        hook = Path(hook_path.stdout.strip())
        if not hook.is_absolute():
            hook = root / hook
    if not hook.exists():
        return None
    if hook.is_symlink() or not hook.is_file() or not os.access(hook, os.X_OK):
        raise RuntimeError(f"Git reference transaction hook is not executable: {hook}")
    return hook.resolve(strict=True)


@contextmanager
def worktree_lifecycle_lock(*, root: Path):
    workspace_identity = capture_workspace_identity(root)
    leader_root = leader_root_for_identity(workspace_identity)
    leader_identity = capture_workspace_identity(leader_root)
    lock_key = leader_identity.git_common_dir
    held = _HELD_WORKTREE_LIFECYCLE_LOCKS.get()
    if lock_key in held:
        yield
        return
    deadline = time.monotonic() + GIT_WORKTREE_TIMEOUT_S
    while True:
        try:
            claim = acquire_workspace_start_claim(
                leader_identity,
                run_id="worktree-lifecycle",
            )
        except WorkspaceBoundaryError as exc:
            if "workspace start is already in progress" not in str(exc):
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for worktree lifecycle lock")
            time.sleep(0.01)
            continue
        break
    context_token = _HELD_WORKTREE_LIFECYCLE_LOCKS.set(held | {lock_key})
    try:
        yield
    finally:
        _HELD_WORKTREE_LIFECYCLE_LOCKS.reset(context_token)
        release_workspace_start_claim(claim)


def _owned_branch_for_live_worktree(*, root: Path, status: WorktreeStatus) -> str | None:
    planned_branch = plan_worktree(root=root, name=status.name).branch
    if not status.branch_created_by_agent_flow or status.branch != planned_branch:
        return None
    result = run_safe_command(("git", "-C", str(status.path), "branch", "--show-current"), cwd=root)
    if not result.ok:
        return None
    current_branch = result.stdout.strip()
    return current_branch if current_branch == planned_branch else None


def _safe_component(value: str) -> str:
    lowered = value.strip().lower()
    safe = re.sub(r"[^a-z0-9._-]+", "-", lowered).strip("-")
    if not safe or safe.startswith(".") or ".." in safe:
        if not any(char.isalnum() for char in lowered):
            raise ValueError(f"worktree name must contain at least one safe character: {value}")
        # 한글 등 비ASCII task도 기본 worktree 이름으로 쓸 수 있게 안정적인 fallback을 둔다.
        digest = hashlib.sha1(lowered.encode("utf-8")).hexdigest()[:8]
        safe = f"task-{digest}"
    return safe


def _feature_worktree_name(value: str) -> str:
    # worktree 디렉터리는 slash를 못 쓰므로 feat/<slug> 브랜치와 feat-<slug> 디렉터리를 짝지어 둔다.
    safe = _safe_component(value)
    return safe if safe.startswith("feat-") else f"feat-{safe}"


def _validate_branch(value: str) -> None:
    invalid_chars = set(" ~^:?*[\\")
    invalid = (
        not value
        or value.startswith("-")
        or value.startswith(".")
        or value.startswith("/")
        or value.endswith("/")
        or value.endswith(".")
        or value.endswith(".lock")
        or ".." in value
        or "//" in value
        or "@{" in value
        or any(ord(ch) < 32 or ch == "\x7f" or ch in invalid_chars for ch in value)
    )
    if invalid:
        raise ValueError(f"unsafe worktree branch: {value}")
