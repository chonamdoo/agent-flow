"""Serialize kit mutation with run registration across repository worktrees."""
from __future__ import annotations

from contextlib import ExitStack
import os
import sys
from pathlib import Path

from agent_flow.core.run_storage import active_run_paths, worktree_runtime_directory
from agent_flow.core.worktree_isolation import (
    FileLeaseUnavailable,
    claim_inherited_file_lease,
    exclusive_file_lease,
    git_repo_state,
    git_safe,
)

INSTALL_LEASE_FD_ENV = "AGENT_FLOW_INSTALL_LEASE_FD"
INSTALL_SAFETY_EXIT = 75
_INSTALL_RECOVERY_MANIFEST = ".agent-flow/install-recovery/manifest.json"


def assert_install_complete(root: Path) -> None:
    """Raise ValueError/OSError when interrupted installation requires recovery."""
    if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY")):
        raise OSError("installation readiness requires no-follow directory opening")
    try:
        checkout = root.resolve(strict=True)
    except RuntimeError as exc:
        raise OSError(f"installation root cannot be accessed: {root}: {exc}") from exc
    with ExitStack() as opened:
        parent_fd = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened.callback(os.close, parent_fd)
        for segment in _INSTALL_RECOVERY_MANIFEST.split("/")[:-1]:
            try:
                parent_fd = os.open(
                    segment, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
                )
            except FileNotFoundError:
                return
            opened.callback(os.close, parent_fd)
        try:
            os.stat("manifest.json", dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise ValueError(
            f"architecture install recovery is required: {_INSTALL_RECOVERY_MANIFEST}; "
            "resume the installer before using architecture policy"
        )


def installation_lock_path(checkout_root: Path) -> Path:
    root = checkout_root.resolve()
    existing = root
    while not existing.exists():
        existing = existing.parent
    common = git_safe("rev-parse", "--git-common-dir", cwd=existing, optional_locks=False)
    if common.ok:
        git_dir = Path(common.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = existing / git_dir
        return git_dir.resolve() / "agent-flow" / "install.lock"
    if git_repo_state(existing) != "non-repo":
        raise FileLeaseUnavailable(f"cannot establish installation ownership for {root}")
    return root / ".agent-flow" / "install.lock"


def _assert_no_active_runs(root: Path) -> None:
    roots = [root]
    state = git_repo_state(root)
    if state == "repo":
        runtime_root = worktree_runtime_directory(root)
        if runtime_root.exists():
            roots.extend(path for path in runtime_root.iterdir() if path.is_dir())
    elif state != "non-repo":
        raise FileLeaseUnavailable(f"cannot inspect active runs for {root}")
    active = [run for state_root in roots for run in active_run_paths(state_root)]
    if active:
        raise FileLeaseUnavailable(
            "install blocked by active run: " + ", ".join(str(run) for run in active)
        )


def main(argv: list[str]) -> int:
    try:
        mode, root_text, *command = argv
        root = Path(root_text).resolve()
        lock = installation_lock_path(root)
        if mode == "check":
            claim_inherited_file_lease(lock, 3)
            _assert_no_active_runs(root)
            return 0
        if mode != "exec" or not command:
            raise ValueError("installation lease requires an installer command")
        with exclusive_file_lease(lock) as fd:
            _assert_no_active_runs(root)
            environment = {**os.environ, INSTALL_LEASE_FD_ENV: str(fd)}
            os.set_inheritable(fd, True)
            # exec keeps the lease with the actual writer even if its launcher dies.
            os.execvpe(command[0], command, environment)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"install safety check failed: {exc}", file=sys.stderr)
        return INSTALL_SAFETY_EXIT
