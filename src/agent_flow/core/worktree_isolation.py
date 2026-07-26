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
import hashlib
import os
import stat
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


# --- leader tripwire ---------------------------------------------------------
#
# 워커가 자기 worktree 밖으로 새어 나갔는지는 명령 문자열이 아니라 파일시스템
# 사실로만 판정한다. phase 진입 시 leader의 HEAD/브랜치/working tree 상태를
# 찍어 두고 phase 종료 시 같은지 본다. 다르면 그 차이 자체가 오염이다.

_AGENT_FLOW_PREFIX = ".agent-flow"

# agent-flow가 스스로 쓰는 **상태** 디렉터리. `artifacts.init_project`가 이걸
# 그대로 만든다. tripwire 비교에서 빼는 유일한 자리이므로 둘이 갈라지면 정상
# 명령이 오탐을 낸다.
AGENT_FLOW_STATE_DIRS = (
    "runs",
    "state",
    "handoffs",
    "team",
    "archive",
    "context",
    "memory",
    "worktrees",
)
# 워커가 worktree 안에서 돌아도 leader 쪽에 정당하게 쓰는 자리들. 여기만 뺀다.
# `scripts/`도 `runtime/`도 절대 넣지 마라 — 둘 다 host와 gate가 **실행하는**
# 코드다. runtime에서 정당하게 생기는 것은 bytecode뿐이라 그것만 따로 뺀다.
_RUNTIME_WRITE_PATHS = tuple(
    f"{_AGENT_FLOW_PREFIX}/{name}" for name in AGENT_FLOW_STATE_DIRS
) + (
    f"{_AGENT_FLOW_PREFIX}/skills-read.jsonl",
    f"{_AGENT_FLOW_PREFIX}/commands-run.jsonl",
)
# 심층 스캔에 함께 넣는 **실행 표면**. 다음 phase가 실제로 실행하는 바이너리와
# host의 hook 등록 파일이다. `--ignored=matching`은 ignore된 디렉터리를 한 줄로
# 접어서 그 안쪽 교체가 통째로 안 보인다. 실측 BLIND였던 자리를 그대로 편다:
# `.venv/bin/agent-flow` 덮어쓰기, `node_modules/.bin/*`, `.claude/hooks/pre.sh`.
#
# 전면 확대가 아니라 화이트리스트인 이유는 성능이다. `node_modules` 전체를
# phase당 두 번 해시하면 tripwire가 런보다 비싸진다.
#
# host hook 등록 파일을 빼면 안 된다 — 워커가 leader의 `.claude/settings.json`을
# 갈아끼워 **다음 phase의 권한 판정 자리 자체**를 무탐지로 바꿀 수 있다.
# 런 *시작 전*의 오염은 이걸로 안 보인다. 그건 `hook_integrity`가 본다.
_EXEC_SURFACE_PATHS = (
    ".venv/bin",
    "node_modules/.bin",
    ".claude/settings.json",
    ".claude/hooks",
    ".Codex/hooks.json",
    ".codex/hooks.json",
    ".omp/extensions",
)
_GENERATED_PATH_SEGMENTS = ("__pycache__",)
_TRIPWIRE_TIMEOUT_S = 120
# porcelain v1 상태 문자. 레코드 앞 두 글자가 전부 여기 속할 때만 경로 접두어로 본다.
_STATUS_CODES = frozenset(" MADRCUT?!")
# 내용 해시 한도. 넘으면 크기만 찍는다 — mtime을 섞으면 touch만으로 오탐이 난다.
_STAMP_FULL_READ_BYTES = 64 * 1024 * 1024
_STAMP_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class LeaderSnapshot:
    head: str              # git rev-parse HEAD ("" on an unborn branch)
    branch: str            # git rev-parse --abbrev-ref HEAD ("HEAD" when detached)
    status: str            # normalized git status records, newline joined
    armed: bool = True     # False면 지킬 leader가 없다(비-git 프로젝트)

    def records(self) -> tuple[str, ...]:
        return tuple(self.status.split("\n")) if self.status else ()


def capture_leader_snapshot(leader_root: Path) -> LeaderSnapshot:
    """phase 진입 시점 leader 상태를 찍는다.

    git이 대답하지 못하면(unknown) 비교 기준을 세울 수 없으므로 fail-closed로
    raise한다. 비-git 프로젝트는 지킬 leader가 없으니 disarmed 스냅샷을 준다.
    """
    state = git_repo_state(leader_root)
    if state == "non-repo":
        return LeaderSnapshot(head="", branch="", status="", armed=False)
    if state == "unknown":
        raise WorktreeIsolationError(
            f"cannot read leader git state for the isolation tripwire: {leader_root}"
        )
    return LeaderSnapshot(
        head=_git_fact(leader_root, "rev-parse", "HEAD"),
        branch=_git_fact(leader_root, "rev-parse", "--abbrev-ref", "HEAD"),
        status=_leader_status(leader_root),
    )


def assert_leader_unchanged(
    leader_root: Path, before: LeaderSnapshot, *, run_id: str = ""
) -> None:
    """leader가 phase 진입 시점과 동일한지 검증한다. **아무것도 바꾸지 않는다.**

    불변식은 하나뿐이다: HEAD + 현재 브랜치 + working tree 상태가 ``before``와
    같다. 어긋나면 그 사실만 보고하고 멈춘다.

    되돌리지 않는 이유: 변경의 출처를 스냅샷 차이만으로는 알 수 없다. worker가
    흘린 것일 수도, 사람이 같은 시각에 leader를 편집한 것일 수도 있다. 귀속이
    불가능한 신호로 ``reset --hard``를 돌리면 사람의 미커밋 작업을 파괴한다.
    탐지해서 멈추는 것만으로 "조용히 넘어가지 않는다"는 목적은 달성된다.
    """
    if not before.armed:
        return
    after = capture_leader_snapshot(leader_root)
    if not after.armed:
        raise WorktreeIsolationError(
            f"leader stopped being a git repository during the phase: {leader_root}"
        )
    reasons = _snapshot_diff(before, after)
    if not reasons:
        return
    raise WorktreeIsolationError(
        "leader checkout changed during the phase: "
        + "; ".join(reasons)
        + ". Nothing was modified or reverted - the leader is exactly as you left it. "
        "If a worker leaked these writes, move them into the worktree and clean the "
        "leader. If they are your own edits, commit or stash them before re-running "
        "so the tripwire has a stable baseline."
    )


def leader_root_for(worker_root) -> Optional[Path]:
    """``worker_root``를 지탱하는 leader 체크아웃. worker_root가 곧 leader면 None.

    linked worktree의 git common dir은 leader의 ``.git``이므로 그 부모가 leader다.
    격리가 없는 실행에서는 지킬 대상이 없어 tripwire를 무장하지 않는다.
    """
    common = _git_common_dir(worker_root)
    if common is None or common.name != ".git":
        return None
    leader = common.parent
    top = _git_toplevel(worker_root)
    if top is None or top == leader:
        return None
    return leader


def _snapshot_diff(before: LeaderSnapshot, after: LeaderSnapshot) -> list[str]:
    reasons: list[str] = []
    if before.head != after.head:
        reasons.append(
            f"HEAD moved {before.head[:12] or '(unborn)'} -> {after.head[:12] or '(unborn)'}"
        )
    if before.branch != after.branch:
        reasons.append(f"branch switched {before.branch!r} -> {after.branch!r}")
    known = set(before.records())
    seen = set(after.records())
    gained = sorted(_status_record_path(r) for r in seen - known)
    lost = sorted(_status_record_path(r) for r in known - seen)
    if gained:
        reasons.append("working tree gained " + ", ".join(gained[:8]))
    if lost:
        reasons.append("working tree lost " + ", ".join(lost[:8]))
    return reasons


def _leader_status(leader_root) -> str:
    """leader working tree 상태를 정규화된 레코드 문자열로 만든다.

    status를 두 번 부르는 이유가 있다. ``--ignored=matching``은 ignore 패턴에
    직접 걸린 디렉터리(``.agent-flow/``)를 한 줄로 접어 버려서 그 안쪽 쓰기가
    통째로 안 보인다. 그래서 ``.agent-flow/``만 파일 단위로 다시 훑는다.

    다만 ``.agent-flow/`` 전체를 감시하면 정상 동작이 100% 오탐이 된다. read
    hook은 worktree 안에서 돌아도 leader의 ``skills-read.jsonl``에 append하고,
    runner는 ``runs/``, JS는 ``state/``를 쓴다. 그래서 **그 경로들만** 빼고
    나머지는 계속 본다. 특히 ``scripts/hooks/``는 host가 매 tool call마다
    실행하는 표면이라 여기를 놓치면 워커가 leader에 hook을 심을 수 있다.

    레코드 문자(`` M``/``!!``)만으로는 **이미 더러운 파일의 추가 수정**과
    **ignored 파일의 내용 교체**가 보이지 않는다. 그래서 tracked 변경은
    ``diff HEAD``의 내용 해시로, untracked/ignored 파일은 내용 해시로 함께
    찍는다. mtime은 쓰지 않는다 — 같은 바이트로 다시 저장하기만 해도 바뀐다.

    ``.agent-flow/``만으로는 부족하다. 워커가 다음 phase가 **실행할** 것을 바꾸는
    자리가 밖에도 있다 — `.venv/bin`, `node_modules/.bin`, 그리고 host의 hook
    등록 파일. 그래서 `_EXEC_SURFACE_PATHS`를 같은 심층 스캔에 함께 넣는다.
    """
    entries = dict(_status_records(leader_root, ("--ignored=matching",)))
    entries.update(
        _status_records(
            leader_root,
            ("-uall", "--ignored=traditional", "--", _AGENT_FLOW_PREFIX)
            + _EXEC_SURFACE_PATHS
            + tuple(f":(exclude){path}" for path in _RUNTIME_WRITE_PATHS),
        )
    )
    kept = sorted(
        (record, path)
        for record, path in entries.items()
        if not _is_excluded_path(path)
    )
    lines = [record for record, _ in kept]
    lines.append(f"tracked-content {_tracked_content_digest(leader_root)}")
    for _, path in kept:
        stamp = _path_content_stamp(leader_root, path)
        if stamp:
            lines.append(stamp)
    return "\n".join(lines)


def _tracked_content_digest(leader_root) -> str:
    """tracked 파일의 실제 내용 변화. 상태 문자만으로는 안 보이는 재수정을 잡는다.

    읽지 못하면 sentinel을 남기지 않고 raise한다. sentinel은 다음 스냅샷과
    무조건 어긋나 오탐이 되고, 오탐은 완료된 산출물을 버리게 만든다.
    """
    # pathspec이 없으면 agent-flow 자신의 상태 쓰기가 digest를 바꾼다. `.agent-flow/`가
    # git-tracked인 프로젝트에서 `claim_task` 한 번에 완료된 task가 failed로 되돌아갔다.
    result = git_safe(
        "diff",
        "HEAD",
        "--no-color",
        "--",
        ".",
        *(f":(exclude){path}" for path in _RUNTIME_WRITE_PATHS),
        cwd=leader_root,
        timeout_s=_TRIPWIRE_TIMEOUT_S,
    )
    if not result.ok:
        raise WorktreeIsolationError(
            f"cannot read leader tracked content for the isolation tripwire in "
            f"{leader_root}: {result.stderr.strip() or result.error or 'git did not answer'}"
        )
    return hashlib.sha256(result.stdout.encode("utf-8", "replace")).hexdigest()[:16]


def _path_content_stamp(leader_root, relative: str) -> str:
    """untracked/ignored 파일의 내용 지표. 디렉터리 레코드는 건너뛴다.

    mtime이 아니라 내용을 해시한다. ``lstat``을 쓰는 이유는 파일을 같은 내용의
    symlink로 바꿔치기하는 걸 잡기 위해서다 — ``stat``은 대상을 따라가 구분을
    잃는다. 한도를 넘는 파일은 앞뒤 조각만 읽으므로 가운데만 바뀌면 놓친다.
    그 구간만 mtime을 함께 찍어 메운다.
    """
    if not relative or relative.endswith("/"):
        return ""
    target = Path(leader_root) / relative
    try:
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode):
            return f"stamp {relative} symlink {os.readlink(target)}"
        if not stat.S_ISREG(info.st_mode):
            return ""
        if info.st_size > _STAMP_FULL_READ_BYTES:
            # 한도 초과는 크기만 본다. 앞뒤 조각 해시 + mtime을 섞어 봤더니
            # touch 한 번에 오탐이 나면서 정작 가운데 변조는 mtime 위조로
            # 빠져나갔다. 오탐이 훨씬 비싸므로 여기서는 미탐을 택한다.
            return f"stamp {relative} {info.st_size} oversize"
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_STAMP_CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return f"stamp {relative} {info.st_size} {digest.hexdigest()[:16]}"


def _git_fact(leader_root, *args: str) -> str:
    result = git_safe(*args, cwd=leader_root, timeout_s=_TRIPWIRE_TIMEOUT_S)
    if result.timed_out or result.error is not None:
        raise WorktreeIsolationError(
            f"git did not answer for the isolation tripwire in {leader_root}: "
            f"{result.stderr.strip() or result.error}"
        )
    return result.stdout.strip()


def _status_records(leader_root, extra_args: Sequence[str]) -> list[tuple[str, str]]:
    """``--porcelain=v1 -z`` 스트림을 (레코드, 경로) 쌍으로 읽는다.

    경로를 레코드 문자열에서 되짚어 추측하지 않는다. rename/copy(``R``/``C``)는
    바로 다음 원소가 원본 경로라는 것이 형식의 약속이므로, 그 약속대로 소비한다.
    문자열을 잘라 맞히려 들면 ``db backup.sql``이나 ``MD file.txt`` 같은 경로에서
    반드시 틀린다.
    """
    result = git_safe(
        "status", "--porcelain=v1", "-z", *extra_args, cwd=leader_root, timeout_s=_TRIPWIRE_TIMEOUT_S
    )
    if not result.ok:
        raise WorktreeIsolationError(
            f"cannot read leader working tree status: {leader_root}: {result.stderr.strip()}"
        )
    fields = [field for field in result.stdout.split("\0") if field]
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) <= 3 or record[2] != " ":
            # 상태 접두어가 없는 조각은 형식상 나올 수 없다. 통째로 경로로 본다.
            entries.append((record, record))
            continue
        status, path = record[:2], record[3:]
        entries.append((record, path))
        if ("R" in status or "C" in status) and index < len(fields):
            source = fields[index]
            index += 1
            entries.append((f"{status} <- {source}", source))
    return entries


def _status_record_path(record: str) -> str:
    """진단 문자열용 경로 추정. **판정에는 쓰지 않는다.**

    판정 경로는 ``_status_records``가 위치 기반으로 돌려주는 값이다. 여기는
    스냅샷 문자열만 남은 diff 메시지를 사람이 읽게 다듬는 용도다.
    """
    if len(record) > 3 and record[2] == " " and _is_status_code(record[:2]):
        return record[3:]
    return record


def _is_status_code(prefix: str) -> bool:
    return all(char in _STATUS_CODES for char in prefix)


def _is_excluded_path(path: str) -> bool:
    """비교에서 뺄 경로인가.

    세 종류다. (1) agent-flow가 스스로 쓰는 상태 디렉터리. (2) 접힌
    ``.agent-flow/`` 디렉터리 레코드 — 심층 스캔이 안을 파일 단위로 이미
    보고하므로 정보가 없고, 디렉터리가 생기고 사라지는 것만으로 변화가 생긴다.
    (3) 실행으로 저절로 생기는 bytecode(``__pycache__``).
    """
    trimmed = path.rstrip("/")
    if trimmed == _AGENT_FLOW_PREFIX:
        return True
    parts = trimmed.split("/")
    if any(segment in _GENERATED_PATH_SEGMENTS for segment in parts):
        return True
    return any(
        trimmed == path_prefix or trimmed.startswith(f"{path_prefix}/")
        for path_prefix in _RUNTIME_WRITE_PATHS
    )


@contextlib.contextmanager
def worktree_creation_lock(root, *, timeout_s: int = _LOCK_TIMEOUT_S) -> Iterator[None]:
    """Serialize concurrent ``git worktree add`` across processes via flock.

    Concurrent creation races on the shared index/ref locks and can leave a
    half-registered worktree; a cross-process file lock makes creation the sole
    writer for the critical section.
    """
    with _cross_process_lock(root, "worktree-create.lock", timeout_s=timeout_s):
        yield


@contextlib.contextmanager
def worker_claim_lock(root, *, timeout_s: int = _LOCK_TIMEOUT_S) -> Iterator[None]:
    """Serialize "count running workers -> claim a task" across processes.

    Counting and claiming as two steps lets two workers walk through the same
    last capacity slot. One lock over both makes the pair atomic. It is a
    distinct lock file from creation so a caller can hold both without
    deadlocking against ``git worktree add``.
    """
    with _cross_process_lock(root, "worker-claim.lock", timeout_s=timeout_s):
        yield


@contextlib.contextmanager
def _cross_process_lock(root, name: str, *, timeout_s: int) -> Iterator[None]:
    common = _git_common_dir(root) or real_path(Path(root) / ".git")
    lock_dir = common / "agent-flow"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / name
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
                        f"timed out acquiring lock: {lock_path}"
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
