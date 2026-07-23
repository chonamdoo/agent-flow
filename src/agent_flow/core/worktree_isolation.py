"""Fail-closed worktree isolation primitives.

Multi-agent runs fan workers out into linked git worktrees. Isolation must never
fail open: when it cannot prove a worker is confined to its own worktree, the run
stops instead of silently falling back to the leader checkout. Every
worker-spawning path shares one audited definition of "isolated" so a single
missed check cannot leak writes into main.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Optional, Sequence, TypeVar

from agent_flow.core.commands import run_safe_command


class WorktreeIsolationError(RuntimeError):
    """An isolation guarantee could not be proven; the caller must fail closed."""


# git honors these env vars over cwd-based discovery. A worker that inherits any
# of them can reach the leader repo regardless of its bound cwd, so they are
# stripped and git is forced to rediscover the repo from the worktree cwd.
LEAKY_GIT_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_CEILING_DIRECTORIES",
)

DEFAULT_MAX_WORKERS = 8
_LOCK_TIMEOUT_S = 120
_LOCK_POLL_S = 0.05
_GIT_LOCK_RETRY_ATTEMPTS = 8
_GIT_LOCK_RETRY_BASE_DELAY_S = 0.1

T = TypeVar("T")


def real_path(value) -> Path:
    """Canonical absolute path with symlinks resolved.

    All boundary comparisons run on realpath output so that symlink and `..`
    tricks cannot make an out-of-tree path masquerade as the worktree.
    """
    return Path(os.path.realpath(str(value)))


def max_worker_capacity() -> int:
    raw = os.environ.get("AGENT_FLOW_MAX_WORKERS")
    if raw is None:
        return DEFAULT_MAX_WORKERS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_WORKERS
    return value if value > 0 else DEFAULT_MAX_WORKERS


def sanitized_worker_env(*, base_env: Optional[dict] = None) -> dict:
    env = dict(os.environ if base_env is None else base_env)
    for name in LEAKY_GIT_ENV_VARS:
        env.pop(name, None)
    return env


def assert_cwd_bound(*, worktree_path, cwd) -> None:
    want = real_path(worktree_path)
    got = real_path(cwd)
    if want != got:
        raise WorktreeIsolationError(
            f"worker cwd {got} is not bound to worktree {want}"
        )


def verify_linked_worktree(
    *,
    root,
    path,
    expected_branch: Optional[str] = None,
    managed_root=None,
) -> Path:
    """Prove ``path`` is a linked worktree of this repo, or raise.

    Returns the verified canonical worktree path. Trust is granted only to this
    exact path; a mismatch anywhere is a hard failure, never a downgrade.
    """
    target = real_path(path)
    if not target.is_dir():
        raise WorktreeIsolationError(f"worktree path is not a directory: {target}")

    leader = _git_toplevel(root)
    if leader is not None and target == leader:
        raise WorktreeIsolationError(
            f"refusing to treat leader checkout as a worktree: {target}"
        )

    # Creation-path pattern: managed worktrees always live under this directory.
    # realpath on both sides means a symlinked target cannot slip past it.
    expected_parent = (
        real_path(managed_root)
        if managed_root is not None
        else real_path(Path(root) / ".agent-flow" / "worktrees")
    )
    if target == expected_parent or not _is_within(target, expected_parent):
        raise WorktreeIsolationError(
            f"worktree path escapes managed root {expected_parent}: {target}"
        )

    # A linked worktree has a `.git` file (a `gitdir:` pointer); the leader and
    # standalone clones have a `.git` directory.
    dot_git = target / ".git"
    if not dot_git.is_file():
        raise WorktreeIsolationError(
            f"not a linked worktree (.git is not a gitdir pointer file): {target}"
        )
    try:
        gitdir_line = dot_git.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        raise WorktreeIsolationError(f"cannot read worktree .git pointer: {target}") from exc
    if not gitdir_line.startswith("gitdir:"):
        raise WorktreeIsolationError(f"malformed linked worktree .git pointer: {target}")

    # Same repository: the git common dir seen from the leader and from the
    # worktree must be identical, or the target belongs to another repo.
    leader_common = _git_common_dir(root)
    target_common = _git_common_dir(target)
    if leader_common is None or target_common is None or leader_common != target_common:
        raise WorktreeIsolationError(
            f"worktree does not share this repo's git common dir: {target}"
        )

    # Authoritative registration check: git itself must list this worktree.
    if target not in _registered_worktree_paths(root):
        raise WorktreeIsolationError(f"worktree is not registered with git: {target}")

    if expected_branch is not None:
        branch = _current_branch(target)
        if branch != expected_branch:
            raise WorktreeIsolationError(
                f"worktree {target} is on branch {branch!r}, expected {expected_branch!r}"
            )

    return target


def assert_worktree_mergeable(*, root, path) -> None:
    """Refuse cleanup unless the worktree's work is already in the leader.

    Removal is destructive, so it is gated on proof: no uncommitted changes and
    the worktree tip is reachable from the leader HEAD. Otherwise raise and let
    the caller preserve the worktree instead of losing unmerged work.
    """
    target = real_path(path)

    status = git_safe("-C", str(target), "status", "--porcelain", cwd=target)
    if not status.ok:
        raise WorktreeIsolationError(f"cannot inspect worktree status before cleanup: {target}")
    if status.stdout.strip():
        raise WorktreeIsolationError(
            f"worktree has uncommitted changes; refusing to remove: {target}"
        )

    tip = git_safe("-C", str(target), "rev-parse", "HEAD", cwd=target)
    if not tip.ok or not tip.stdout.strip():
        raise WorktreeIsolationError(f"cannot resolve worktree HEAD before cleanup: {target}")
    tip_sha = tip.stdout.strip()

    leader_head = _leader_head_sha(root)
    if leader_head is None:
        raise WorktreeIsolationError("cannot resolve leader HEAD for merge proof")

    ancestor = git_safe("merge-base", "--is-ancestor", tip_sha, leader_head, cwd=root)
    if not ancestor.ok:
        raise WorktreeIsolationError(
            f"worktree commit {tip_sha[:12]} is not merged into leader "
            f"{leader_head[:12]}; refusing to remove (unmerged work would be lost)"
        )


@dataclass(frozen=True)
class WorkerScope:
    worker: str
    paths: tuple
    worktree_isolated: bool


def assert_scopes_isolated(scopes: Sequence[WorkerScope]) -> None:
    """Overlapping write scopes are allowed only when both workers declare
    worktree isolation. Undeclared or undeterminable (glob) scopes fail closed.
    """
    items = list(scopes)
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            if a.worker == b.worker:
                continue
            if _scopes_overlap(a, b) and not (a.worktree_isolated and b.worktree_isolated):
                raise WorktreeIsolationError(
                    f"overlapping write scope between {a.worker!r} and {b.worker!r} "
                    "requires declared worktree isolation on both"
                )


def git_repo_state(root) -> str:
    """Classify ``root`` as 'repo', 'non-repo', or 'unknown'.

    'unknown' means the git call itself failed (timeout/OSError). It must not be
    downgraded to non-git: the caller fails closed rather than silently running a
    worker unisolated in the leader checkout.
    """
    result = git_safe("rev-parse", "--git-dir", cwd=root)
    if result.ok:
        return "repo"
    if result.timed_out or result.error is not None:
        return "unknown"
    return "non-repo"


@contextlib.contextmanager
def worktree_creation_lock(root, *, timeout_s: int = _LOCK_TIMEOUT_S) -> Iterator[None]:
    """Serialize concurrent ``git worktree add`` across processes via flock.

    Concurrent creation races on the shared index/ref locks and can leave a
    half-registered worktree; a cross-process file lock makes creation the sole
    writer for the critical section.
    """
    common = _git_common_dir(root) or real_path(Path(root) / ".git")
    lock_dir = common / "agent-flow"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "worktree-create.lock"
    deadline = time.monotonic() + timeout_s
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise WorktreeIsolationError(
                        f"timed out acquiring worktree creation lock: {lock_path}"
                    )
                time.sleep(_LOCK_POLL_S)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def is_git_lock_contention(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    markers = (
        "index.lock",
        "config.lock",
        "packed-refs.lock",
        "unable to create",
        "cannot lock ref",
        "another git process seems to be running",
        "file exists",
    )
    return any(marker in lowered for marker in markers)


def default_git_lock_retryable(exc: BaseException) -> bool:
    if isinstance(exc, subprocess.CalledProcessError):
        blob = f"{getattr(exc, 'stderr', '') or ''}{getattr(exc, 'output', '') or ''}"
        return is_git_lock_contention(blob)
    return False


def with_git_lock_retry(
    fn: Callable[[], T],
    *,
    is_retryable: Optional[Callable[[BaseException], bool]] = None,
    attempts: int = _GIT_LOCK_RETRY_ATTEMPTS,
    base_delay_s: float = _GIT_LOCK_RETRY_BASE_DELAY_S,
) -> T:
    """Run ``fn`` with bounded exponential backoff while ``is_retryable`` holds.

    Bounded so contention that never clears fails closed instead of spinning.
    """
    check = is_retryable or default_git_lock_retryable
    last: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if attempt < attempts - 1 and check(exc):
                time.sleep(base_delay_s * (2 ** attempt))
                continue
            raise
    assert last is not None
    raise last


def git_safe(*args, cwd, timeout_s: Optional[int] = None):
    """Run git with git-discovery env vars stripped so cwd stays authoritative.

    A poisoned ambient GIT_DIR/GIT_WORK_TREE must never redirect our own git
    operations away from the requested cwd.
    """
    env = sanitized_worker_env()
    command = ("git",) + tuple(str(a) for a in args)
    if timeout_s is None:
        return run_safe_command(command, cwd=cwd, env=env)
    return run_safe_command(command, cwd=cwd, env=env, timeout_s=timeout_s)


def _git_toplevel(path) -> Optional[Path]:
    result = git_safe("rev-parse", "--show-toplevel", cwd=path)
    if not result.ok:
        return None
    raw = result.stdout.strip()
    return real_path(raw) if raw else None


def _git_common_dir(path) -> Optional[Path]:
    result = git_safe("rev-parse", "--git-common-dir", cwd=path)
    if not result.ok:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path(path) / candidate
    return real_path(candidate)


def _current_branch(path) -> Optional[str]:
    result = git_safe("rev-parse", "--abbrev-ref", "HEAD", cwd=path)
    if not result.ok:
        return None
    return result.stdout.strip() or None


def _leader_head_sha(root) -> Optional[str]:
    result = git_safe("rev-parse", "HEAD", cwd=root)
    if not result.ok:
        return None
    return result.stdout.strip() or None


def _registered_worktree_paths(root) -> set:
    result = git_safe("worktree", "list", "--porcelain", cwd=root)
    if not result.ok:
        return set()
    paths = set()
    prefix = "worktree "
    for line in result.stdout.splitlines():
        if line.startswith(prefix):
            paths.add(real_path(line[len(prefix):].strip()))
    return paths


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _scopes_overlap(a: WorkerScope, b: WorkerScope) -> bool:
    for pa in a.paths:
        for pb in b.paths:
            if _paths_conflict(str(pa), str(pb)):
                return True
    return False


def _paths_conflict(pa: str, pb: str) -> bool:
    # Undeterminable (glob) scopes cannot be proven disjoint, so treat them as
    # conflicting and defer to the isolation requirement.
    if _is_glob(pa) or _is_glob(pb):
        return True
    na, nb = _norm_path(pa), _norm_path(pb)
    if na == nb:
        return True
    return _is_path_prefix(na, nb) or _is_path_prefix(nb, na)


def _is_glob(value: str) -> bool:
    return any(ch in value for ch in "*?[]")


def _norm_path(value: str) -> PurePosixPath:
    cleaned = value.strip().replace("\\", "/").strip("/")
    parts = [part for part in cleaned.split("/") if part not in ("", ".")]
    return PurePosixPath(*parts) if parts else PurePosixPath(".")


def _is_path_prefix(ancestor: PurePosixPath, descendant: PurePosixPath) -> bool:
    a_parts = ancestor.parts
    d_parts = descendant.parts
    if len(a_parts) > len(d_parts):
        return False
    return d_parts[: len(a_parts)] == a_parts
