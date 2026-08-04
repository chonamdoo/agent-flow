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
try:
    import fcntl
except ModuleNotFoundError:
    class _UnavailableFcntl:
        LOCK_SH = LOCK_EX = LOCK_NB = LOCK_UN = 0

        @staticmethod
        def flock(_fd: int, _operation: int) -> None:
            raise OSError(errno.ENOSYS, "fcntl.flock is unavailable")

    fcntl = _UnavailableFcntl()  # type: ignore[assignment]
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Literal, Optional, Sequence, TypeVar

from agent_flow.core.commands import run_safe_command


class WorktreeIsolationError(RuntimeError):
    """An isolation guarantee could not be proven; the caller must fail closed."""

class FileLeaseUnavailable(WorktreeIsolationError):
    """A kernel-backed file lease cannot be acquired safely."""



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

# git이 사람에게 보여주는 메시지는 로케일에 따라 번역된다. 우리가 그 문자열로
# 분기하는 자리가 남아 있는 한(`is_git_lock_contention`) 로케일을 고정하지 않으면
# 한국어 환경에서 그 분기가 통째로 죽는다 — lock 경합 재시도가 아예 발동하지
# 않는다. 사용자에게 보이는 문구는 agent-flow가 직접 만드는 것이라 영향이 없다.
_GIT_STABLE_LOCALE = {"LC_ALL": "C", "LANG": "C", "LANGUAGE": ""}

DEFAULT_MAX_WORKERS = 8
_LOCK_TIMEOUT_S = 120
_LOCK_POLL_S = 0.05
_LOCK_OPEN_ATTEMPTS = 8
_GIT_LOCK_RETRY_ATTEMPTS = 8
_GIT_LOCK_RETRY_BASE_DELAY_S = 0.1
_MANAGED_WORKTREE_MARKERS = (".agent-flow", ".codex", ".Codex", ".omp")

T = TypeVar("T")


ProviderLeaseState = Literal["inactive", "active", "unknown"]

_PROVIDER_LEASE_VERSION = 1
_MAX_PROVIDER_CAPACITY = 1024
_PROVIDER_LEASE_DIR = Path("agent-flow") / "provider-leases"
_CLEANUP_LEASE_DIR = Path("agent-flow") / "cleanup-leases"
_PROVIDER_REGISTRY_LOCK = "registry.lock"
_PROVIDER_REGISTRY_STATE = "registry.json"
_CLEANUP_REPOSITORY_LOCK = "repository.lock"
_PROVIDER_REGISTRY_TIMEOUT_S = 5.0
_IN_PROCESS_PROVIDER_LEASES: dict[tuple[str, int], str] = {}
_IN_PROCESS_PROVIDER_LEASES_LOCK = threading.Lock()


class ProviderLeaseUnavailable(WorktreeIsolationError):
    """The repository-wide provider capacity is currently occupied."""


@dataclass
class ProviderLease:
    """Live capability for one repository-global provider slot."""

    common_dir: Path
    capacity: int
    slot: int
    capability: str = field(repr=False)
    _slot_fd: int = field(repr=False)
    _active: bool = field(default=True, repr=False)

    @property
    def active(self) -> bool:
        return self._active
    @property
    def process_lifetime_fds(self) -> tuple[int]:
        if not self._active or self._slot_fd < 0:
            raise ProviderLeaseUnavailable(
                "provider lease capability no longer owns a live slot"
            )
        return (self._slot_fd,)


    def release(self) -> None:
        _release_provider_lease(self)


@contextlib.contextmanager
def provider_lease(
    root, *, capacity: Optional[int] = None
) -> Iterator[ProviderLease]:
    """Hold one canonical repository slot until the provider tree is finished."""

    lease = acquire_provider_lease(root=root, capacity=capacity)
    try:
        yield lease
    finally:
        lease.release()


def acquire_provider_lease(
    *, root, capacity: Optional[int] = None
) -> ProviderLease:
    """Acquire a non-stale repository-global provider capability.

    Slot ownership is a kernel ``flock`` held by this process. A crash releases
    it in the kernel; no PID file or age heuristic ever reclaims an unknown
    owner. Cleanup does not require repository-wide provider idleness, so this
    lease deliberately holds nothing outside its own slot.
    """

    requested = _strict_provider_capacity(capacity)
    common = _required_git_common_dir(root)
    slot_fd: Optional[int] = None
    slot = -1
    slot_key: Optional[tuple[str, int]] = None
    capability = os.urandom(32).hex()
    try:
        registry_dir = _ensure_lock_directory(common, _PROVIDER_LEASE_DIR)
        registry_path = registry_dir / _PROVIDER_REGISTRY_LOCK
        registry_fd = _open_lock_file(registry_path, create=True)
        try:
            _lock_registry(registry_fd)
            _assert_lock_file_binding(registry_path, registry_fd)
            _prepare_provider_registry(registry_dir, requested)
            registry_key = str(registry_dir)
            for candidate in range(requested):
                candidate_key = (registry_key, candidate)
                with _IN_PROCESS_PROVIDER_LEASES_LOCK:
                    if candidate_key in _IN_PROCESS_PROVIDER_LEASES:
                        continue
                candidate_path = _provider_slot_path(registry_dir, candidate)
                candidate_fd = _open_lock_file(candidate_path, create=True)
                try:
                    fcntl.flock(
                        candidate_fd, fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                    _assert_lock_file_binding(candidate_path, candidate_fd)
                except WorktreeIsolationError:
                    os.close(candidate_fd)
                    raise
                except OSError as exc:
                    os.close(candidate_fd)
                    if _is_lock_busy(exc):
                        continue
                    raise WorktreeIsolationError(
                        f"cannot acquire provider lease slot {candidate}"
                    ) from exc
                with _IN_PROCESS_PROVIDER_LEASES_LOCK:
                    if candidate_key in _IN_PROCESS_PROVIDER_LEASES:
                        fcntl.flock(candidate_fd, fcntl.LOCK_UN)
                        os.close(candidate_fd)
                        continue
                    _IN_PROCESS_PROVIDER_LEASES[candidate_key] = capability
                slot = candidate
                slot_fd = candidate_fd
                slot_key = candidate_key
                break
        finally:
            try:
                fcntl.flock(registry_fd, fcntl.LOCK_UN)
            finally:
                os.close(registry_fd)

        if slot_fd is None:
            raise ProviderLeaseUnavailable(
                f"repository provider capacity reached ({requested}/{requested})"
            )
        return ProviderLease(
            common_dir=common,
            capacity=requested,
            slot=slot,
            capability=capability,
            _slot_fd=slot_fd,
        )
    except BaseException:
        if slot_fd is not None:
            try:
                fcntl.flock(slot_fd, fcntl.LOCK_UN)
            finally:
                os.close(slot_fd)
        if slot_key is not None:
            with _IN_PROCESS_PROVIDER_LEASES_LOCK:
                if _IN_PROCESS_PROVIDER_LEASES.get(slot_key) == capability:
                    _IN_PROCESS_PROVIDER_LEASES.pop(slot_key, None)
        raise


def assert_provider_lease(lease: ProviderLease, *, root) -> None:
    """Require a live capability for the same canonical repository."""

    if (
        not isinstance(lease, ProviderLease)
        or not lease.active
        or not lease.capability
    ):
        raise ProviderLeaseUnavailable(
            "a live provider lease capability is required"
        )
    key = (
        str(lease.common_dir / _PROVIDER_LEASE_DIR),
        lease.slot,
    )
    with _IN_PROCESS_PROVIDER_LEASES_LOCK:
        registered = (
            _IN_PROCESS_PROVIDER_LEASES.get(key) == lease.capability
        )
    if not registered:
        raise ProviderLeaseUnavailable(
            "provider lease capability is not owned by this process"
        )
    try:
        os.fstat(lease._slot_fd)
    except OSError as exc:
        raise ProviderLeaseUnavailable(
            "provider lease capability no longer owns a live lock file"
        ) from exc
    common = _required_git_common_dir(root)
    if common != lease.common_dir:
        raise ProviderLeaseUnavailable(
            "provider lease belongs to a different repository"
        )


def probe_provider_leases(root) -> ProviderLeaseState:
    """Return active/inactive/unknown without reclaiming any owner.

    The probe serializes with acquisitions through ``registry.lock`` and then
    nonblockingly locks every configured slot. Any malformed or unreadable
    registry is ``unknown`` so destructive cleanup must preserve it.
    """

    common = _git_common_dir(root)
    if common is None:
        return "unknown"
    registry_dir = common / _PROVIDER_LEASE_DIR
    if not os.path.lexists(registry_dir):
        return "inactive"
    if not _is_secure_lock_directory(common, registry_dir):
        return "unknown"
    registry_path = registry_dir / _PROVIDER_REGISTRY_LOCK
    state_path = registry_dir / _PROVIDER_REGISTRY_STATE
    if not os.path.lexists(registry_path) and not os.path.lexists(state_path):
        return "inactive"
    if not os.path.lexists(registry_path) or not os.path.lexists(state_path):
        return "unknown"
    try:
        registry_fd = _open_lock_file(registry_path, create=False)
    except (OSError, WorktreeIsolationError):
        return "unknown"
    try:
        try:
            fcntl.flock(registry_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _assert_lock_file_binding(registry_path, registry_fd)
        except WorktreeIsolationError:
            return "unknown"
        except OSError as exc:
            return "active" if _is_lock_busy(exc) else "unknown"
        try:
            capacity = _read_provider_registry_capacity(state_path)
            return _probe_provider_slots(registry_dir, capacity)
        except (OSError, ValueError, WorktreeIsolationError):
            return "unknown"
        finally:
            fcntl.flock(registry_fd, fcntl.LOCK_UN)
    finally:
        os.close(registry_fd)


def _release_provider_lease(lease: ProviderLease) -> None:
    if not lease._active:
        return
    capability = lease.capability
    lease._active = False
    lease.capability = ""
    registry_dir = lease.common_dir / _PROVIDER_LEASE_DIR
    key = (str(registry_dir), lease.slot)
    inherited_holder = False
    release_error: OSError | WorktreeIsolationError | None = None
    registry_fd: int | None = None
    probe_fd: int | None = None
    try:
        registry_fd = _open_lock_file(
            registry_dir / _PROVIDER_REGISTRY_LOCK,
            create=False,
        )
        _lock_registry(registry_fd)
        _assert_lock_file_binding(
            registry_dir / _PROVIDER_REGISTRY_LOCK,
            registry_fd,
        )
        os.close(lease._slot_fd)
        lease._slot_fd = -1
        probe_fd = _open_lock_file(
            _provider_slot_path(registry_dir, lease.slot),
            create=False,
        )
        try:
            fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _assert_lock_file_binding(
                _provider_slot_path(registry_dir, lease.slot),
                probe_fd,
            )
        except OSError as exc:
            if _is_lock_busy(exc):
                inherited_holder = True
            else:
                release_error = exc
        else:
            fcntl.flock(probe_fd, fcntl.LOCK_UN)
    except (OSError, WorktreeIsolationError) as exc:
        release_error = exc
        if lease._slot_fd >= 0:
            os.close(lease._slot_fd)
            lease._slot_fd = -1
    finally:
        if probe_fd is not None:
            os.close(probe_fd)
        if registry_fd is not None:
            try:
                fcntl.flock(registry_fd, fcntl.LOCK_UN)
            finally:
                os.close(registry_fd)
        with _IN_PROCESS_PROVIDER_LEASES_LOCK:
            if _IN_PROCESS_PROVIDER_LEASES.get(key) == capability:
                _IN_PROCESS_PROVIDER_LEASES.pop(key, None)
    if release_error is not None:
        raise WorktreeIsolationError(
            "provider process-tree lease release could not be verified"
        ) from release_error
    if inherited_holder:
        raise WorktreeIsolationError(
            "provider descendant remains active after the provider exited; "
            "the inherited repository lease remains locked"
        )


def _strict_provider_capacity(value: Optional[int]) -> int:
    if value is None:
        raw = os.environ.get("AGENT_FLOW_MAX_WORKERS")
        if raw is None:
            capacity = DEFAULT_MAX_WORKERS
        else:
            try:
                capacity = int(raw)
            except ValueError as exc:
                raise WorktreeIsolationError(
                    "AGENT_FLOW_MAX_WORKERS must be an integer"
                ) from exc
    else:
        if isinstance(value, bool) or not isinstance(value, int):
            raise WorktreeIsolationError(
                "provider capacity must be an integer"
            )
        capacity = value
    if capacity < 1 or capacity > _MAX_PROVIDER_CAPACITY:
        raise WorktreeIsolationError(
            "provider capacity must be between "
            f"1 and {_MAX_PROVIDER_CAPACITY}"
        )
    return capacity


def _required_git_common_dir(root) -> Path:
    common = _git_common_dir(root)
    if common is None:
        raise WorktreeIsolationError(
            "cannot prove the canonical git common dir for provider isolation"
        )
    return common


def _ensure_lock_directory(common: Path, relative: Path) -> Path:
    current = common
    for component in relative.parts:
        current = current / component
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise WorktreeIsolationError(
                f"cannot create repository lock directory: {current}"
            ) from exc
        if not _is_secure_lock_directory(common, current):
            raise WorktreeIsolationError(
                f"repository lock directory is not trustworthy: {current}"
            )
    return current


def _is_secure_lock_directory(common: Path, path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and real_path(path) == path
        and _is_within(path, common)
    )


def _open_lock_file(path: Path, *, create: bool) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or cloexec is None or directory is None:
        raise WorktreeIsolationError(
            "secure repository lease file opening is unsupported"
        )
    parent_fd = -1
    fd = -1
    try:
        parent_fd = os.open(
            path.parent,
            os.O_RDONLY | directory | nofollow | cloexec,
        )
        opened_parent = os.fstat(parent_fd)
        visible_parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(opened_parent.st_mode)
            or stat.S_ISLNK(visible_parent.st_mode)
            or (opened_parent.st_dev, opened_parent.st_ino)
            != (visible_parent.st_dev, visible_parent.st_ino)
        ):
            raise WorktreeIsolationError(
                f"repository lease directory identity changed: {path.parent}"
            )
        flags = os.O_RDWR | nofollow | cloexec
        if create:
            flags |= os.O_CREAT
        # 생성은 반드시 검증된 부모 fd를 거친다. 절대경로로 열면 부모가 교체된
        # 순간 공격자 디렉터리에 파일이 만들어진다. Darwin은 경합 중
        # `openat(dir_fd, name, O_CREAT)`가 부모가 살아 있는데도 ENOENT를 내므로
        # 그 한 가지 조건만 좁게 다시 시도한다.
        for attempt in range(_LOCK_OPEN_ATTEMPTS):
            try:
                fd = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
                break
            except FileNotFoundError:
                if not create or attempt == _LOCK_OPEN_ATTEMPTS - 1:
                    raise
                time.sleep(_LOCK_POLL_S)
        _assert_lock_file_binding(path, fd)
        return fd
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def _assert_lock_file_binding(path: Path, fd: int) -> None:
    opened = os.fstat(fd)
    visible = path.lstat()
    visible_parent = path.parent.lstat()
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(visible.st_mode)
        or stat.S_ISLNK(visible.st_mode)
        or not stat.S_ISDIR(visible_parent.st_mode)
        or stat.S_ISLNK(visible_parent.st_mode)
        or (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino)
        or opened.st_uid != os.getuid()
        or opened.st_nlink != 1
    ):
        raise WorktreeIsolationError(
            f"repository lease path no longer names the opened lock file: {path}"
        )

def _ensure_lease_parent(path: Path) -> None:
    """Create the lease directory chain through the one private-dir creator."""
    target = path.parent
    base = target
    while not base.is_dir():
        parent = base.parent
        if parent == base:
            break
        base = parent
    _ensure_lock_directory(real_path(base), target.relative_to(base))


@contextlib.contextmanager
def exclusive_file_lease(path: Path) -> Iterator[None]:
    """Hold a crash-released exclusive lease on one regular file."""
    try:
        _ensure_lease_parent(path)
        fd = _open_lock_file(path, create=True)
    except (OSError, WorktreeIsolationError) as exc:
        raise FileLeaseUnavailable(
            f"cannot establish file lease at {path}"
        ) from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _assert_lock_file_binding(path, fd)
        except WorktreeIsolationError as exc:
            raise FileLeaseUnavailable(
                f"file lease path changed while acquiring {path}"
            ) from exc
        except OSError as exc:
            if _is_lock_busy(exc):
                raise FileLeaseUnavailable(
                    f"file lease is already held at {path}"
                ) from exc
            raise FileLeaseUnavailable(
                f"cannot acquire file lease at {path}"
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)


@contextlib.contextmanager
def shared_file_lease(path: Path) -> Iterator[None]:
    """Hold a crash-released shared lease on one regular file."""
    try:
        _ensure_lease_parent(path)
        fd = _open_lock_file(path, create=True)
    except (OSError, WorktreeIsolationError) as exc:
        raise FileLeaseUnavailable(
            f"cannot establish file lease at {path}"
        ) from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            _assert_lock_file_binding(path, fd)
        except WorktreeIsolationError as exc:
            raise FileLeaseUnavailable(
                f"file lease path changed while acquiring {path}"
            ) from exc
        except OSError as exc:
            if _is_lock_busy(exc):
                raise FileLeaseUnavailable(
                    f"file lease is already held exclusively at {path}"
                ) from exc
            raise FileLeaseUnavailable(
                f"cannot acquire file lease at {path}"
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)



def _lock_registry(fd: int) -> None:
    deadline = time.monotonic() + _PROVIDER_REGISTRY_TIMEOUT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if not _is_lock_busy(exc):
                raise WorktreeIsolationError(
                    "cannot lock the provider lease registry"
                ) from exc
            if time.monotonic() >= deadline:
                raise ProviderLeaseUnavailable(
                    "provider lease registry is busy"
                ) from exc
            time.sleep(_LOCK_POLL_S)


def _prepare_provider_registry(
    registry_dir: Path, requested_capacity: int
) -> None:
    state_path = registry_dir / _PROVIDER_REGISTRY_STATE
    if os.path.lexists(state_path):
        current_capacity = _read_provider_registry_capacity(state_path)
        if current_capacity == requested_capacity:
            return
        state = _probe_provider_slots(registry_dir, current_capacity)
        if state == "active":
            raise ProviderLeaseUnavailable(
                "provider capacity cannot change while a lease is active "
                f"({current_capacity} -> {requested_capacity})"
            )
        if state == "unknown":
            raise WorktreeIsolationError(
                "cannot prove provider slots inactive for a capacity change"
            )
    else:
        unexpected_slots = tuple(registry_dir.glob("slot-*.lock"))
        if unexpected_slots:
            raise WorktreeIsolationError(
                "provider lease slots exist without registry metadata"
            )
    _write_provider_registry_capacity(state_path, requested_capacity)


def _read_provider_registry_capacity(path: Path) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    if nofollow is None or cloexec is None:
        raise WorktreeIsolationError(
            "secure provider registry reads are unsupported"
        )
    fd = os.open(path, os.O_RDONLY | nofollow | cloexec)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise WorktreeIsolationError(
                "provider lease registry metadata is not a regular file"
            )
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            payload = json.load(stream)
    finally:
        if fd >= 0:
            os.close(fd)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "capacity"}
        or payload.get("version") != _PROVIDER_LEASE_VERSION
    ):
        raise WorktreeIsolationError(
            "provider lease registry metadata is malformed"
        )
    capacity = payload.get("capacity")
    if (
        isinstance(capacity, bool)
        or not isinstance(capacity, int)
        or capacity < 1
        or capacity > _MAX_PROVIDER_CAPACITY
    ):
        raise WorktreeIsolationError(
            "provider lease registry capacity is invalid"
        )
    return capacity


def _write_provider_registry_capacity(path: Path, capacity: int) -> None:
    payload = {
        "version": _PROVIDER_LEASE_VERSION,
        "capacity": capacity,
    }
    fd, raw_temp = tempfile.mkstemp(
        prefix=".registry-", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(raw_temp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(payload, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if fd >= 0:
            os.close(fd)
        if os.path.lexists(temp_path):
            temp_path.unlink()


def _provider_slot_path(registry_dir: Path, slot: int) -> Path:
    return registry_dir / f"slot-{slot:04d}.lock"


def _probe_provider_slots(
    registry_dir: Path, capacity: int
) -> ProviderLeaseState:
    registry_key = str(registry_dir)
    with _IN_PROCESS_PROVIDER_LEASES_LOCK:
        if any(
            key[0] == registry_key
            for key in _IN_PROCESS_PROVIDER_LEASES
        ):
            return "active"
    locked: list[int] = []
    try:
        for slot in range(capacity):
            path = _provider_slot_path(registry_dir, slot)
            if not os.path.lexists(path):
                continue
            try:
                fd = _open_lock_file(path, create=False)
            except (OSError, WorktreeIsolationError):
                return "unknown"
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                _assert_lock_file_binding(path, fd)
            except WorktreeIsolationError:
                os.close(fd)
                return "unknown"
            except OSError as exc:
                os.close(fd)
                if _is_lock_busy(exc):
                    return "active"
                return "unknown"
            locked.append(fd)
        return "inactive"
    finally:
        for fd in locked:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _is_lock_busy(exc: OSError) -> bool:
    return exc.errno in {errno.EACCES, errno.EAGAIN}


def real_path(value) -> Path:
    """Canonical absolute path with symlinks resolved.

    All boundary comparisons run on realpath output so that symlink and `..`
    tricks cannot make an out-of-tree path masquerade as the worktree.
    """
    return Path(os.path.realpath(str(value)))


def max_worker_capacity() -> int:
    """선언된 동시성. 잘못된 env를 조용히 기본값으로 접으면 provider slot은
    거부하고 pool은 계속 도는 상태가 되어 선언값과 실제가 갈린다."""
    return _strict_provider_capacity(None)


def sanitized_worker_env(*, base_env: Optional[dict] = None) -> dict:
    """워커에게 넘길 env에서 git discovery 변수를 제거한다.

    **보장 범위는 git 서브프로세스뿐이다.** git은 이 변수들을 cwd보다 우선하므로,
    제거하면 git 경로에 한해 cwd가 권위가 된다. 파일시스템 쓰기는 여기에 해당하지
    않는다 — 절대경로 쓰기는 cwd를 무시하며 이 함수로 막을 수 없다.

    그래서 이건 확률 저감이지 격리 보장이 아니다. 오염 판정은 오직
    ``capture_leader_snapshot``/``assert_leader_unchanged`` tripwire가 한다. 이
    함수를 "격리"로 읽으면 tripwire 없이도 안전하다는 오독이 생긴다.
    """
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

def managed_worktree_root(*, root, path) -> Path:
    """Return the exact recognized managed root containing ``path``."""
    target = real_path(path)
    for marker in _MANAGED_WORKTREE_MARKERS:
        candidate = real_path(Path(root) / marker / "worktrees")
        if target.parent == candidate:
            return candidate
    adopted = adopted_worktree_parent(root=root, path=path)
    if adopted is not None:
        return adopted
    raise WorktreeIsolationError(
        f"worktree path is not a direct child of a managed root: {target}"
    )


def adopted_worktree_parent(*, root, path) -> Optional[Path]:
    """채택 기록이 ``path``를 지목할 때 그 checkout의 부모.

    git 등록만으로는 인가가 되지 않는다. 워커도 `git worktree add`를 실행할 수 있고
    그 호출은 host write boundary의 write 명령 집합에 없어서 경계를 그대로 통과한다.
    그래서 등록 지문(위조 방지)에 더해 **채택 기록**을 요구한다.

    기록을 worktree manifest에 두면 안 된다. manifest는 `<git_common>/agent-flow/
    worktrees/<name>/`, 즉 바인딩된 워커의 쓰기 허용 구역 안이라 워커가 자기 이름의
    manifest에서 `path`만 바꿔 관리 루트 밖 임의 디렉터리를 자기 checkout으로
    승격시킬 수 있다. 그래서 기록은 그 구역 밖의 `adopted/`에 둔다.

    기록은 경로만으로 참이 되지 않는다. 채택 시점의 등록 지문을 함께 적고 현재
    지문과 대조한다 — 그러지 않으면 채택했던 자리를 지우고 다른 목적으로 다시 만든
    checkout이 사람의 인가 없이 그 기록을 물려받는다.

    관리 루트 밖 checkout은 공유 부모가 없으므로 자기 부모를 돌려준다. 그 값이
    ``verify_linked_worktree``의 containment 인자로 쓰이면 그 검사는 항진식이 된다 —
    관리 루트 안에서도 호출자들이 이미 같은 값을 넘기고 있고, 실질 증명은 gitdir
    왕복 검증과 등록 지문이 한다.
    """
    target = real_path(path)
    payload = _adopted_record(root=root, name=target.name)
    if payload is None:
        return None
    if _recorded_checkout(root=root, payload=payload) != target:
        return None
    expected = payload.get("registration_identity")
    if not isinstance(expected, str) or not expected:
        return None
    registered = registered_worktree_at(root, target)
    if registered is None or registered.registration_identity != expected:
        return None
    return target.parent


def adopted_checkout_path(*, root, name) -> Optional[Path]:
    """``name`` 기록이 지목하는 checkout의 실경로. 기록이 없으면 ``None``.

    후보를 만드는 용도다. 지문 대조는 ``adopted_worktree_parent``가 한다.
    """
    payload = _adopted_record(root=root, name=name)
    if payload is None:
        return None
    return _recorded_checkout(root=root, payload=payload)


def trusted_checkout_paths(*, root, name) -> tuple[Path, ...]:
    """런타임 상태 키 ``name``이 가리킬 수 있는 checkout 경로들.

    두 근거만 인정한다: 생성 규약이 정한 관리 루트 아래 자리, 그리고 채택 기록.
    둘 다 워커가 쓸 수 없는 곳에서 나온다 — 이 목록으로 소유자를 고르는 자리가
    host write boundary이므로, 워커가 쓸 수 있는 값이 섞이면 경계가 무의미해진다.
    """
    candidates: list[Path] = []
    adopted = adopted_checkout_path(root=root, name=name)
    if adopted is not None:
        candidates.append(adopted)
    for marker in _MANAGED_WORKTREE_MARKERS:
        candidates.append(real_path(Path(root) / marker / "worktrees" / name))
    return tuple(candidates)


def record_adopted_checkout(*, root, name, path, registration_identity) -> Path:
    """채택 기록을 남긴다. 채택 명령만 호출한다."""
    record = adopted_record_path(root=root, name=name)
    if record is None:
        raise WorktreeIsolationError(
            f"cannot record adoption without a git common directory: {path}"
        )
    if not registration_identity:
        raise WorktreeIsolationError(
            f"cannot record adoption without a registration identity: {path}"
        )
    record.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "path": str(real_path(path)),
            "registration_identity": registration_identity,
        },
        sort_keys=True,
    )
    # 기존 자리를 열어 자르면 안 된다. `O_NOFOLLOW`는 symlink만 막고 hard link는
    # 통과시키므로, 기록 자리가 남의 파일과 연결돼 있으면 `O_TRUNC`가 그 파일을 먼저
    # 비운다. 배타 생성한 임시 파일에 쓴 뒤 atomic rename으로 갈아 끼운다.
    staging = record.with_name(f"{record.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    handle = os.open(staging, flags, 0o600)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(f"{payload}\n")
        os.replace(staging, record)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    return record


def forget_adopted_checkout(*, root, name) -> None:
    record = adopted_record_path(root=root, name=name)
    if record is None:
        return
    try:
        record.unlink()
    except OSError:
        # 기록이 없거나 지울 수 없어도 제거 자체를 막지 않는다. 남은 기록은 경로가
        # 다시 일치할 때만 의미가 있고, 그때는 채택이 여전히 참이다.
        pass


def adopted_record_path(*, root, name) -> Optional[Path]:
    # 워커의 쓰기 허용 구역(`agent-flow/worktrees/<name>`) 밖이어야 한다.
    common = _git_common_dir(root)
    if common is None or common.name != ".git":
        return None
    if not name or Path(name).name != name or name in {".", ".."}:
        raise WorktreeIsolationError(f"invalid adopted checkout key: {name!r}")
    return common / "agent-flow" / "adopted" / f"{name}.json"


def _adopted_record(*, root, name) -> Optional[dict]:
    record = adopted_record_path(root=root, name=name)
    if record is None:
        return None
    try:
        info = record.lstat()
    except OSError:
        return None
    # 인가 상태는 정규 파일 하나여야 한다. symlink나 hardlink는 기록을 저장소 밖에서
    # 갈아치울 수 있는 자리다(host 바인딩 파일에 이미 걸린 계약과 같다).
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return None
    try:
        payload = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _recorded_checkout(*, root, payload: dict) -> Optional[Path]:
    recorded = payload.get("path")
    if not isinstance(recorded, str) or not recorded:
        return None
    candidate = Path(recorded)
    if not candidate.is_absolute():
        candidate = Path(root) / candidate
    return real_path(candidate)


def git_common_dir(path) -> Optional[Path]:
    """sanitize된 git으로 얻은 common dir. anchor 유도는 이 함수만 쓴다.

    ambient `GIT_DIR`/`GIT_COMMON_DIR`을 상속한 채 물으면 대답이 다른 저장소를
    가리키고, 그 값의 부모가 그대로 config/state root가 된다.
    """
    return _git_common_dir(path)


def git_dir(path) -> Optional[Path]:
    """sanitize된 git으로 얻은 이 checkout 전용 git dir. 같은 이유로 이 함수만 쓴다.

    linked worktree면 `<common>/worktrees/<name>`이고, 그 자리가 index·HEAD처럼
    checkout 하나에만 속한 상태를 담는다.
    """
    return _git_dir(path)


def git_toplevel(path) -> Optional[Path]:
    """sanitize된 git으로 얻은 checkout 최상위. 같은 이유로 이 함수만 쓴다."""
    return _git_toplevel(path)



def _read_stable_regular_text(
    path: Path,
    *,
    observed: os.stat_result,
    label: str,
) -> str:
    if not hasattr(os, "O_NOFOLLOW"):
        raise WorktreeIsolationError(f"{label} cannot be opened without no-follow support")
    try:
        fd = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise WorktreeIsolationError(f"cannot read {label}: {path}") from exc
    try:
        opened = os.fstat(fd)
        content = os.read(fd, 4097)
    finally:
        os.close(fd)
    if (
        opened.st_dev != observed.st_dev
        or opened.st_ino != observed.st_ino
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != os.getuid()
        or opened.st_nlink != 1
        or len(content) > 4096
    ):
        raise WorktreeIsolationError(f"{label} identity changed or is not trusted: {path}")
    return content.decode("utf-8", errors="replace").strip()


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

    # 담을 그릇을 호출자가 주지 않으면 **인식된** 관리 루트로 판정한다. 여기에
    # layout 리터럴을 두면 기본 자리를 옮길 때 이 함수가 유효한 checkout을 거절한다 —
    # 신뢰의 근거는 경로 모양이 아니라 marker layout 또는 채택 기록이다.
    # realpath on both sides means a symlinked target cannot slip past it.
    expected_parent = (
        real_path(managed_root)
        if managed_root is not None
        else managed_worktree_root(root=root, path=path)
    )
    if target == expected_parent or not _is_within(target, expected_parent):
        raise WorktreeIsolationError(
            f"worktree path escapes managed root {expected_parent}: {target}"
        )

    # A linked worktree has a `.git` file (a `gitdir:` pointer); the leader and
    # standalone clones have a `.git` directory.
    dot_git = target / ".git"
    try:
        dot_git_identity = dot_git.lstat()
    except OSError as exc:
        raise WorktreeIsolationError(
            f"not a linked worktree (.git is not a gitdir pointer file): {target}"
        ) from exc
    if (
        dot_git.is_symlink()
        or not stat.S_ISREG(dot_git_identity.st_mode)
        or dot_git_identity.st_nlink != 1
    ):
        raise WorktreeIsolationError(
            f"not a linked worktree (.git is not a trusted pointer file): {target}"
        )
    gitdir_line = _read_stable_regular_text(
        dot_git,
        observed=dot_git_identity,
        label="worktree .git pointer",
    )
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
    gitdir_value = gitdir_line.partition(":")[2].strip()
    gitdir = Path(gitdir_value).expanduser()
    if not gitdir.is_absolute():
        gitdir = target / gitdir
    gitdir = real_path(gitdir)
    if gitdir.parent != leader_common / "worktrees":
        raise WorktreeIsolationError(
            f"worktree .git pointer escapes the repository worktree registry: {target}"
        )
    backlink = gitdir / "gitdir"
    try:
        backlink_identity = backlink.lstat()
    except OSError as exc:
        raise WorktreeIsolationError(
            f"cannot verify worktree .git backlink: {target}"
        ) from exc
    backlink_value = _read_stable_regular_text(
        backlink,
        observed=backlink_identity,
        label="worktree .git backlink",
    )
    backlink_path = Path(backlink_value).expanduser()
    if not backlink_path.is_absolute():
        backlink_path = gitdir / backlink_path
    if (
        backlink.is_symlink()
        or not stat.S_ISREG(backlink_identity.st_mode)
        or real_path(backlink_path) != dot_git
    ):
        raise WorktreeIsolationError(
            f"worktree .git pointer does not point back to this checkout: {target}"
        )

    # Authoritative registration check: git itself must list this worktree.
    # 조회가 실패하면 raise한다. 빈 집합으로 강등하면 "등록 안 됨"과 "물어보지
    # 못했다"가 같은 값이 되어, 진단이 실제 원인과 무관해진다.
    if registered_worktree_at(root, target) is None:
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

    status = git_safe(
        "-C", str(target), "status", "--porcelain", cwd=target, optional_locks=False
    )
    if not status.ok:
        raise _tripwire_git_error(
            f"cannot inspect worktree status before cleanup: {target}", status
        )
    if status.stdout.strip():
        raise WorktreeIsolationError(
            f"worktree has uncommitted changes; refusing to remove: {target}"
        )

    tip = git_safe(
        "-C", str(target), "rev-parse", "HEAD", cwd=target, optional_locks=False
    )
    if not tip.ok or not tip.stdout.strip():
        raise _tripwire_git_error(f"cannot resolve worktree HEAD before cleanup: {target}", tip)
    tip_sha = tip.stdout.strip()

    leader_head = _leader_head_sha(root)
    if leader_head is None:
        raise WorktreeIsolationError("cannot resolve leader HEAD for merge proof")

    ancestor = git_safe(
        "merge-base", "--is-ancestor", tip_sha, leader_head, cwd=root, optional_locks=False
    )
    if ancestor.returncode == 1 and not ancestor.stderr.strip():
        # exit 1은 git이 정상 실행되어 "조상이 아니다"라고 답한 것이다. 그 외의
        # 실패를 같은 문구로 보고하면 사용자가 고장을 미머지로 오독한다.
        raise WorktreeIsolationError(
            f"worktree commit {tip_sha[:12]} is not merged into leader "
            f"{leader_head[:12]}; refusing to remove (unmerged work would be lost)"
        )
    if not ancestor.ok:
        raise _tripwire_git_error(
            f"cannot prove worktree commit {tip_sha[:12]} is merged into leader "
            f"{leader_head[:12]}; refusing to remove",
            ancestor,
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

    'unknown' means git could not answer. It must never be downgraded to
    non-git: the caller fails closed rather than running a worker unisolated in
    the leader checkout, or planting agent-flow state inside it.

    non-zero exit alone does not prove "not a repository". ``rev-parse`` exits
    128 for every fatal — dubious ownership (``safe.directory``), an unreadable
    ``.git``, a corrupt config. Those are exactly the states where the tripwire
    must stay armed, yet the old code folded all of them into 'non-repo', which
    made ``capture_leader_snapshot`` return a disarmed snapshot and silently
    drop the only contamination verdict there is.

    So a failure is downgraded only when the filesystem *proves* it. That oracle
    spawns no process, so it cannot be defeated by the locale, the git version,
    or the contention that caused the failure in the first place.
    """
    result = git_safe("rev-parse", "--git-dir", cwd=root, optional_locks=False)
    if result.ok:
        return "repo"
    return "non-repo" if _no_git_dir_above(root) else "unknown"


def _no_git_dir_above(root) -> bool:
    """``root``와 그 조상 어디에도 ``.git``이 없음을 증명했는가.

    존재만 본다. 읽지 못하는 ``.git``도 "있다"로 센다 — 판정 불가를 non-repo로
    접지 않기 위해서다. ``Path.exists()``는 권한 오류를 False로 삼키므로 쓰지
    않고 ``lstat``의 예외 종류로 구분한다.
    """
    current = real_path(root)
    for candidate in (current, *current.parents):
        try:
            os.lstat(candidate / ".git")
        except FileNotFoundError:
            continue
        except OSError:
            return False
        return False
    return True


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
# Finder가 checkout을 열어 둔 것만으로 비결정적으로 갱신한다. 프로젝트 동작이나
# 실행 표면을 바꾸지 않는 이 basename만 status와 tracked digest 양쪽에서 뺀다.
_AMBIENT_METADATA_BASENAMES = (".DS_Store",)
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
_TRIPWIRE_TIMEOUT_S = 120
# porcelain v1 상태 문자. 레코드 앞 두 글자가 전부 여기 속할 때만 경로 접두어로 본다.
_STATUS_CODES = frozenset(" MADRCUT?!")
_STAMP_CHUNK_BYTES = 64 * 1024



@dataclass(frozen=True)
class LeaderSnapshot:
    head: str              # git rev-parse HEAD (unborn HEAD fails closed)
    branch: str            # git rev-parse --abbrev-ref HEAD ("HEAD" when detached)
    status: str            # normalized git status records, newline joined
    armed: bool = True     # False면 지킬 leader가 없다(비-git 프로젝트)

    def records(self) -> tuple[str, ...]:
        return tuple(self.status.split("\n")) if self.status else ()


def capture_leader_snapshot(
    leader_root: Path,
    *,
    include_ignored: bool = True,
) -> LeaderSnapshot:
    """phase 진입 시점 leader 상태를 찍는다.

    git이 대답하지 못하면(unknown) 비교 기준을 세울 수 없으므로 fail-closed로
    raise한다. 비-git 프로젝트는 지킬 leader가 없으니 disarmed 스냅샷을 준다.

    재시도 단위는 개별 git 호출이 아니라 **스냅샷 전체**다. 호출마다 재시도하면
    경합이 걷히는 도중에 찍힌 조각들이 한 스냅샷 안에 섞여, 어느 시점과도
    일치하지 않는 기준선이 만들어진다. 그런 기준선은 다음 비교에서 반드시 오탐을
    내고, 오탐은 완료된 워커 산출물을 failed로 되돌린다.
    """
    state = git_repo_state(leader_root)
    if state == "non-repo":
        return LeaderSnapshot(head="", branch="", status="", armed=False)
    if state == "unknown":
        raise WorktreeIsolationError(
            f"cannot read leader git state for the isolation tripwire: {leader_root}"
        )
    # 재시도 단위가 개별 git 호출이 아니라 **관측 전체**인 이유: HEAD를 attempt
    # 1에서, status를 attempt 2에서 주워 담으면 lock이 풀리는 순간에 걸친 반쪽
    # 상태가 기준선으로 굳어 다음 비교에서 있지도 않은 diff를 만든다.
    git_fact = _git_fact
    leader_status = _leader_status
    snapshot_type = LeaderSnapshot
    retry = with_git_lock_retry
    retryable = tripwire_git_lock_retryable
    return retry(
        lambda: snapshot_type(
            head=git_fact(leader_root, "rev-parse", "HEAD"),
            branch=git_fact(
                leader_root,
                "rev-parse",
                "--abbrev-ref",
                "HEAD",
            ),
            status=leader_status(
                leader_root,
                include_ignored=include_ignored,
            ),
        ),
        is_retryable=retryable,
    )


HOST_PHASE_LEADER_BASELINE_KEY = "host_phase_leader_baseline"
# 스냅샷 관측 방식이 바뀔 때 올린다. 구버전 스냅샷은 현재 관측과 비교할 수
# 없으므로, 값이 다르면 비교하지 않고 거부해야 한다.
HOST_PHASE_LEADER_BASELINE_VERSION = 2
_EXTERNAL_HEAD_DRIFT = "external-head"
_DRIFT_GUIDANCE = {
    "status": (
        "The leader working tree no longer matches the baseline. Restore those "
        "paths - commit, stash, or clean them - then re-run."
    ),
    "branch": (
        "The leader is on a different branch than the baseline, so its working tree "
        "may now hold a different commit's content. Switch it back before continuing."
    ),
    "non-fast-forward": (
        "The leader HEAD did not move forward from the baseline (rebase, reset, or "
        "force-push), so commits this run was built on may be gone. Investigate "
        "before continuing."
    ),
    _EXTERNAL_HEAD_DRIFT: (
        "The leader HEAD moved without the leader checkout recording it, so the "
        "move came from somewhere else - another worktree writing the shared refs, "
        "or a rewrite that dropped the reflog. Investigate before continuing."
    ),
}


@dataclass(frozen=True)
class LeaderDrift:
    """기준선과 현재 leader의 차이. **무엇이** 달라졌는지만 담는다.

    귀속(누가 썼는가)은 스냅샷 차이로 알 수 없다. 대신 종류를 나눈다 — 종류마다
    사용자가 할 수 있는 다음 수가 다르기 때문이다.
    """

    kind: str
    reasons: tuple[str, ...]
    before: LeaderSnapshot
    after: LeaderSnapshot




class LeaderDriftError(WorktreeIsolationError):
    """tripwire가 **실제 leader 변경**을 봤다. 관측 실패와 구분해야 한다.

    호출자는 복구 안내를 종류별로 갈라야 하고, 그 판단 근거는 예외 문자열이
    아니라 이 ``drift``다.
    """

    def __init__(self, message: str, drift: LeaderDrift) -> None:
        super().__init__(message)
        self.drift = drift


def classify_leader_drift(
    leader_root: Path,
    before: LeaderSnapshot,
    *,
    include_ignored: bool = True,
    after: LeaderSnapshot | None = None,
) -> LeaderDrift | None:
    """기준선과의 차이를 종류로 나눈다. 차이가 없으면 ``None``.

    축이 둘이고 비교 방식이 다르다. working tree는 등호로 본다 — 워커가 흘린
    쓰기가 여기 남는다. HEAD는 등호로 보지 않는다. 사람이 leader에서 평범한
    `git pull`을 하는 것은 정상이고, 그것까지 오염으로 접으면 런이 남의 정상
    작업에 멈춘 뒤 그 정지를 푸는 수단이 다시 필요해진다.

    HEAD 축의 판정 조건은 ``_head_drift_kind``에 있다.
    """
    if not before.armed:
        return None
    observed = (
        after
        if after is not None
        else capture_leader_snapshot(leader_root, include_ignored=include_ignored)
    )
    if not observed.armed:
        raise WorktreeIsolationError(
            f"leader stopped being a git repository during the phase: {leader_root}"
        )
    head_drift = _head_drift_kind(leader_root, before, observed)
    reasons = _snapshot_diff(before, observed, include_head=head_drift is not None)
    if not reasons:
        return None
    # HEAD 축이 어긋났으면 그쪽을 우선한다. 두 축이 함께 어긋난 경우 status 안내
    # ("commit, stash, or clean")는 HEAD 문제에 아무 소용이 없다.
    kind = head_drift or "status"
    return LeaderDrift(
        kind=kind,
        reasons=tuple(reasons),
        before=before,
        after=observed,
    )



def _head_drift_kind(
    leader_root: Path, before: LeaderSnapshot, observed: LeaderSnapshot
) -> str | None:
    """HEAD 축이 어긋났는가, 어긋났으면 어느 조건에서인가. 정상 전진이면 ``None``.

    완화는 셋을 **모두** 만족할 때만 준다. 같은 브랜치, 이전 HEAD가 새 HEAD의
    조상, 그 이동이 leader 자신의 reflog에 기록됨. 하나만 봤을 때 각각 무엇을
    놓치는지 실측으로 확인했다 — reflog만 보면 `git -C <leader> reset --hard
    HEAD~1`이 통과하고(leader에서 도는 명령이라 reflog에 남는다), 조상 관계만
    보면 형제 worktree가 공유 ref에 밀어 넣은 커밋이 통과한다.

    어느 조건에서 탈락했는지 그대로 돌려주는 이유는 안내문 때문이다. "leader가
    기록하지 않았다"는 문구를 leader가 분명히 기록한 전환·reset에 붙이면 사용자가
    엉뚱한 곳을 조사한다.
    """
    if before.branch != observed.branch:
        return "branch"
    if before.head == observed.head:
        return None
    if not _is_ancestor(leader_root, before.head, observed.head):
        return "non-fast-forward"
    if not _head_moved_inside_leader(leader_root, observed.head):
        return _EXTERNAL_HEAD_DRIFT
    return None


def leader_drift_message(
    drift: LeaderDrift,
    *,
    worker_root: Path | None = None,
) -> str:
    """차단 사유 + 그 종류에 **실제로 통하는** 다음 수."""
    guidance = _DRIFT_GUIDANCE.get(
        drift.kind, "Investigate the leader before continuing."
    )
    resume = (
        f" Resume only from the worker checkout: {real_path(worker_root)}."
        if worker_root is not None
        else ""
    )
    return (
        "leader checkout changed during the phase: "
        + "; ".join(drift.reasons)
        + ". Nothing was modified or reverted - the leader is exactly as you left it. "
        + guidance
        + resume
    )


def assert_leader_unchanged(
    leader_root: Path,
    before: LeaderSnapshot,
    *,
    run_id: str = "",
    worker_root: Path | None = None,
    include_ignored: bool = True,
) -> None:
    """워커가 leader를 건드리지 않았는지 검증한다. **아무것도 바꾸지 않는다.**

    불변식은 "leader가 비트 단위로 그대로"가 아니라 "이 런이 leader를 건드리지
    않았다"이다. 그래서 working tree는 등호로 보고, HEAD 이동은 leader 자신의
    reflog에 기록됐는지로 본다. 사람의 `pull`/`checkout`은 통과하고, 공유 ref를
    밖에서 밀어 넣은 이동은 걸린다.

    되돌리지 않는 이유: working tree 변경의 출처는 스냅샷 차이만으로 알 수 없다.
    worker가 흘린 것일 수도, 사람이 같은 시각에 leader를 편집한 것일 수도 있다.
    귀속이 불가능한 신호로 ``reset --hard``를 돌리면 사람의 미커밋 작업을 파괴한다.
    탐지해서 멈추는 것만으로 "조용히 넘어가지 않는다"는 목적은 달성된다.
    """
    drift = classify_leader_drift(
        leader_root,
        before,
        include_ignored=include_ignored,
    )
    if drift is None:
        return
    raise LeaderDriftError(
        leader_drift_message(drift, worker_root=worker_root),
        drift,
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


def _snapshot_diff(
    before: LeaderSnapshot, after: LeaderSnapshot, *, include_head: bool = True
) -> list[str]:
    reasons: list[str] = []
    if include_head:
        if before.head != after.head:
            reasons.append(f"HEAD moved {before.head[:12]} -> {after.head[:12]}")
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


def _leader_status(leader_root, *, include_ignored: bool = True) -> str:
    """Return a content-sensitive snapshot of every non-runtime leader path."""
    status_args = (
        ("-uall", "--ignored=traditional")
        if include_ignored
        else ("-uall",)
    )
    entries = dict(_status_records(leader_root, status_args))
    kept = sorted(
        (record, path)
        for record, path in entries.items()
        if not _is_excluded_path(path)
    )
    lines = [record for record, _ in kept]
    lines.append(f"tracked-content {_tracked_content_digest(leader_root)}")
    seen = {path for _, path in kept}
    for _, path in kept:
        stamp = _path_content_stamp(leader_root, path)
        if stamp:
            lines.append(stamp)
    # status와 diff가 **둘 다** 침묵하는 경로들. 여기서 직접 해시하지 않으면 그
    # 파일에 대한 쓰기는 어느 신호에도 남지 않는다(실측: 두 출력 모두 빈 문자열).
    # 전수 해시한다. 상한을 두면 그 뒤로 밀린 파일의 내용 변경이 영구히 안 보이고,
    # 그것은 탐지 실패 중 최악인 조용한 미탐지다. 비용 근거도 없다 — 실측으로
    # tracked 3000개 전수 sha256이 58ms로, 이미 매 스냅샷마다 도는 `git status`
    # 한 번(60ms)보다 싸다. 표시가 없는 저장소에서는 이 순회가 0회다.
    unwatched = [path for path in _index_flagged_paths(leader_root) if path not in seen]
    for path in unwatched:
        if not _path_is_present(leader_root, path):
            # sparse checkout은 제외 파일 전부를 표시한다. 디스크에 없는 경로는
            # leader의 내용이 아니므로 스냅샷에 넣지 않는다.
            continue
        stamp = _path_content_stamp(leader_root, path)
        if stamp:
            lines.append(f"unwatched {stamp}")
    return "\n".join(lines)


def _index_flagged_paths(leader_root) -> tuple[str, ...]:
    """`assume-unchanged`/`skip-worktree`로 표시된 tracked 경로.

    이 표시는 git에게 "이 파일의 변경을 보지 말라"는 지시다. 그래서 `status`도
    `diff HEAD`도 그 파일의 쓰기를 보고하지 않는다. 사전 차단을 두 규칙으로 줄인
    뒤 tripwire가 주 기제가 됐으므로, 그 침묵은 그냥 구멍이다 — 표시 여부와
    무관하게 "phase 도중 leader는 그대로"라는 계약은 같다.

    sparse checkout은 제외된 파일 **전부**를 `S`로 표시한다. 그 파일들은 디스크에
    없으므로 호출자가 존재 여부로 걸러낸다 — 없는 파일은 leader의 내용이 아니다.
    """
    result = git_safe(
        "ls-files",
        "-v",
        "-z",
        cwd=leader_root,
        timeout_s=_TRIPWIRE_TIMEOUT_S,
        optional_locks=False,
    )
    if not result.ok:
        raise _tripwire_git_error(
            f"cannot read leader index flags in {leader_root}", result
        )
    flagged: list[str] = []
    for record in result.stdout.split("\0"):
        if len(record) < 3 or record[1] != " ":
            continue
        tag, path = record[0], record[2:]
        # 소문자 = assume-unchanged, `S` = skip-worktree. 정상(`H`)은 대상이 아니다.
        if not (tag.islower() or tag == "S"):
            continue
        if _is_excluded_path(path):
            continue
        flagged.append(path)
    return tuple(sorted(flagged))


def _path_is_present(leader_root, relative: str) -> bool:
    try:
        (Path(leader_root) / relative).lstat()
    except OSError:
        return False
    return True



def _tracked_content_digest(leader_root) -> str:
    """tracked 파일의 실제 내용 변화. 상태 문자만으로는 안 보이는 재수정을 잡는다.

    읽지 못하면 sentinel을 남기지 않고 raise한다. sentinel은 다음 스냅샷과
    무조건 어긋나 오탐이 되고, 오탐은 완료된 산출물을 버리게 만든다.

    진단용 필터는 전부 끈다. `.gitattributes`가 지목하는 diff driver의 `textconv`와
    `command`는 저장소가 커밋해 두는 값이므로, 켜 두면 **관측 대상이 관측 방법을
    고른다**. 결과는 두 가지다. 양쪽 출력이 같아지는 필터에서는 diff가 0바이트가 되어
    이 digest가 상수로 굳고(재수정 신호 상실), git이 그 프로그램을 스냅샷마다 실제로
    실행한다(경계 코드가 저장소 설정에 코드 실행을 허용).

    내용 자체는 싣지 않는다. `--full-index`가 내는 `index <40hex>..<40hex>` 헤더의
    뒤쪽 sha는 git이 워킹트리 blob을 직접 해싱해 채우므로 1바이트 변경도 그 줄에
    반영된다. `--binary`로 patch 본문까지 받으면 탐지력은 같은데 비용만 내용 크기에
    비례한다 — 실측(32MB 바이너리 1바이트 수정) 43.2MB/0.63s 대 179B/0.06s.
    `--no-renames`는 무관한 파일 추가가 diff 표현을 흔드는 잡음을 없앤다.
    """
    # pathspec이 없으면 agent-flow 자신의 상태 쓰기가 digest를 바꾼다. `.agent-flow/`가
    # git-tracked인 프로젝트에서 `claim_task` 한 번에 완료된 task가 failed로 되돌아갔다.
    result = git_safe(
        "diff",
        "HEAD",
        "--no-color",
        "--full-index",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        "--",
        ".",
        *(f":(exclude){path}" for path in _RUNTIME_WRITE_PATHS),
        *(
            f":(glob,exclude){basename}"
            for basename in _AMBIENT_METADATA_BASENAMES
        ),
        *(
            f":(glob,exclude)**/{basename}"
            for basename in _AMBIENT_METADATA_BASENAMES
        ),
        cwd=leader_root,
        timeout_s=_TRIPWIRE_TIMEOUT_S,
        optional_locks=False,
    )
    if not result.ok:
        raise _tripwire_git_error(
            f"cannot read leader tracked content for the isolation tripwire in {leader_root}",
            result,
        )
    return hashlib.sha256(result.stdout.encode("utf-8", "replace")).hexdigest()[:16]


def _path_content_stamp(leader_root, relative: str) -> str:
    """untracked/ignored 파일의 내용 지표. 디렉터리 레코드는 건너뛴다.

    mtime이 아니라 내용을 해시한다. ``lstat``을 쓰는 이유는 파일을 같은 내용의
    symlink로 바꿔치기하는 걸 잡기 위해서다 — ``stat``은 대상을 따라가 구분을
    잃는다. 크기와 무관하게 전체를 스트리밍 해시한다. 큰 파일의 가운데를 같은
    크기로 바꾼 누출도 놓치지 않아야 하므로 크기 기반 표본화는 하지 않는다.
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
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_STAMP_CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError:
        # ""를 돌려주면 이 파일의 stamp 줄이 통째로 빠져 내용 변화가 영구히 안
        # 보인다. sentinel은 결정적이라 unreadable이 유지되는 동안 오탐이 없고,
        # readable -> unreadable 전이만 정확히 잡는다.
        return f"stamp {relative} unreadable"
    return f"stamp {relative} {info.st_size} {digest.hexdigest()[:16]}"


def _git_fact(leader_root, *args: str) -> str:
    """tripwire 기준선이 되는 git 사실 하나. 성패는 오직 return code로 가른다.

    빈 stdout은 실패가 아니다 — detached HEAD의 ``branch --show-current``는
    정상적으로 빈 문자열을 준다. 반대로 non-zero exit은 stdout이 무엇이든
    사실이 아니다. 예전엔 exit code를 안 봐서 unborn HEAD의 ``rev-parse HEAD``가
    stdout으로 뱉는 ``HEAD`` 문자열을 그대로 기준선으로 굳혔다.

    stderr 원문을 메시지에 싣는다. ``tripwire_git_lock_retryable``이 일시적
    lock 경합인지 판정하는 근거가 이 문자열밖에 없다.
    """
    result = git_safe(
        *args, cwd=leader_root, timeout_s=_TRIPWIRE_TIMEOUT_S, optional_locks=False
    )
    if result.ok:
        return result.stdout.strip()
    if result.timed_out:
        cause = "timed out"
    elif result.error is not None:
        cause = "could not run git"
    else:
        cause = f"git exited {result.returncode}"
    detail = result.stderr.strip() or result.error or ""
    purpose = " ".join(("git",) + tuple(str(arg) for arg in args))
    raise WorktreeIsolationError(
        f"cannot read `{purpose}` for the isolation tripwire in {leader_root}: "
        + cause
        + (f": {detail}" if detail else "")
    )


def _tripwire_git_error(message: str, result) -> WorktreeIsolationError:
    """관측 실패를 예외로 만든다. 실패 원인과 stderr 원문을 함께 싣는다.

    ``tripwire_git_lock_retryable``이 일시적 lock 경합인지 판정하는 근거가 이
    문자열밖에 없다. 원문을 빼면 재시도가 조용히 죽는다.
    """
    if result.timed_out:
        cause = "timed out"
    elif result.error is not None:
        cause = "could not run git"
    else:
        cause = f"git exited {result.returncode}"
    detail = result.stderr.strip() or result.error or ""
    return WorktreeIsolationError(f"{message}: {cause}" + (f": {detail}" if detail else ""))


def _status_records(leader_root, extra_args: Sequence[str]) -> list[tuple[str, str]]:
    """``--porcelain=v1 -z`` 스트림을 (레코드, 경로) 쌍으로 읽는다.

    경로를 레코드 문자열에서 되짚어 추측하지 않는다. rename/copy(``R``/``C``)는
    바로 다음 원소가 원본 경로라는 것이 형식의 약속이므로, 그 약속대로 소비한다.
    문자열을 잘라 맞히려 들면 ``db backup.sql``이나 ``MD file.txt`` 같은 경로에서
    반드시 틀린다.
    """
    result = git_safe(
        "status",
        "--porcelain=v1",
        "-z",
        *extra_args,
        cwd=leader_root,
        timeout_s=_TRIPWIRE_TIMEOUT_S,
        optional_locks=False,
    )
    if not result.ok:
        raise _tripwire_git_error(
            f"cannot read leader working tree status in {leader_root}", result
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

    네 종류다. (1) OS가 자동 갱신하는 ambient metadata. (2) agent-flow가 스스로
    쓰는 상태 디렉터리. (3) 접힌 ``.agent-flow/`` 디렉터리 레코드 — 심층 스캔이
    안을 파일 단위로 이미 보고하므로 정보가 없고, 디렉터리가 생기고 사라지는
    것만으로 변화가 생긴다. (4) ``.agent-flow/runtime`` 아래에서 실행으로
    생기는 bytecode(``__pycache__``).
    """
    trimmed = path.rstrip("/")
    if trimmed.rsplit("/", 1)[-1] in _AMBIENT_METADATA_BASENAMES:
        return True
    if trimmed == _AGENT_FLOW_PREFIX:
        return True
    parts = trimmed.split("/")
    if "__pycache__" in parts:
        return parts[:2] == [_AGENT_FLOW_PREFIX, "runtime"]
    return any(
        trimmed == path_prefix or trimmed.startswith(f"{path_prefix}/")
        for path_prefix in _RUNTIME_WRITE_PATHS
    )


@contextlib.contextmanager
def worktree_creation_lock(root, *, timeout_s: int = _LOCK_TIMEOUT_S) -> Iterator[None]:
    """Serialize repository-global ``git worktree add`` mutations.

    Worktree metadata and refs share a common git directory, so concurrent
    mutation can contend even when paths and branches are unique. This lock is
    preventive hardening; contention alone does not explain writes to leader.
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
    common = _git_common_dir(root)
    if common is None:
        # 경로를 추측하면 프로세스마다 다른 lock 파일을 잡아 상호배제가 조용히
        # 사라진다. git이 대답하지 못할 확률은 경합이 심할 때 가장 높으므로,
        # 가드가 필요한 바로 그 순간에 꺼지는 fail-open이다. 증명된 non-git만
        # 예외로 두고, 그때는 지킬 leader가 없으므로 `.agent-flow`를 쓴다.
        if git_repo_state(root) != "non-repo":
            raise WorktreeIsolationError(
                f"cannot resolve the git common dir for the cross-process lock: {root}"
            )
        lock_dir = real_path(Path(root) / _AGENT_FLOW_PREFIX)
    else:
        lock_dir = common / "agent-flow"
    _ensure_lease_parent(lock_dir / name)
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


def tripwire_git_lock_retryable(exc: BaseException) -> bool:
    """tripwire **관측**만 재시도한다. 관측 결과(실제 leader diff)는 대상이 아니다.

    `default_git_lock_retryable`은 `CalledProcessError`만 본다. tripwire의 git
    호출은 `git_safe`를 거쳐 `WorktreeIsolationError`로 올라오므로, 그 메시지에
    실려 온 stderr 원문으로만 판정한다. lock 경합이 아닌 git 오류는 재시도해도
    같은 답이 나오므로 즉시 fail-closed다.
    """
    return isinstance(exc, WorktreeIsolationError) and is_git_lock_contention(str(exc))


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


def git_safe(
    *args,
    cwd,
    timeout_s: Optional[int] = None,
    optional_locks: bool = True,
    input_text: str | None = None,
    pass_fds: tuple[int, ...] = (),
    cwd_fd: int | None = None,
):
    """Run git with git-discovery env vars stripped so cwd stays authoritative.

    A poisoned ambient GIT_DIR/GIT_WORK_TREE must never redirect our own git
    operations away from the requested cwd.

    ``optional_locks=False`` for read-only observation. ``status`` and ``diff``
    refresh the index by default and take ``index.lock`` to do it, so the
    tripwire would manufacture the very contention it then has to retry. Never
    pass it on a mutation — it tells git to skip work a writer may actually need.

    The locale is pinned so every message we branch on stays deterministic. It is
    applied to this command only and never leaks into the worker env.
    """
    env = sanitized_worker_env()
    env.update(_GIT_STABLE_LOCALE)
    if not optional_locks:
        env["GIT_OPTIONAL_LOCKS"] = "0"
    command = ("git",) + tuple(str(a) for a in args)
    options = {
        "cwd": cwd,
        "env": env,
        "input_text": input_text,
    }
    if pass_fds:
        options["pass_fds"] = pass_fds
    if cwd_fd is not None:
        options["cwd_fd"] = cwd_fd
    if timeout_s is not None:
        options["timeout_s"] = timeout_s
    return run_safe_command(command, **options)


def _git_toplevel(path) -> Optional[Path]:
    result = git_safe("rev-parse", "--show-toplevel", cwd=path, optional_locks=False)
    if not result.ok:
        return None
    raw = result.stdout.strip()
    return real_path(raw) if raw else None


def _git_common_dir(path) -> Optional[Path]:
    result = git_safe("rev-parse", "--git-common-dir", cwd=path, optional_locks=False)
    if not result.ok:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path(path) / candidate
    return real_path(candidate)


def _git_dir(path) -> Optional[Path]:
    result = git_safe("rev-parse", "--absolute-git-dir", cwd=path, optional_locks=False)
    if not result.ok:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    return real_path(Path(raw))


def _current_branch(path) -> Optional[str]:
    result = git_safe("rev-parse", "--abbrev-ref", "HEAD", cwd=path, optional_locks=False)
    if not result.ok:
        return None
    return result.stdout.strip() or None


def _leader_head_sha(root) -> Optional[str]:
    result = git_safe("rev-parse", "HEAD", cwd=root, optional_locks=False)
    if not result.ok:
        return None
    return result.stdout.strip() or None


def _is_ancestor(leader_root, old: str, new: str) -> bool:
    """``old``가 ``new``의 조상인가. 판정 불가는 **아니다**로 접는다.

    이 답은 "HEAD 이동을 정상 전진으로 볼 수 있는가"의 근거다. 모르는 것을
    "그렇다"로 접으면 `reset --hard`나 force-push로 사라진 커밋 위에서 런이
    계속된다. exit 1(조상 아님)과 exit 128(객체 없음)은 둘 다 "아니다"다.
    """
    if not old or not new:
        return False
    result = git_safe(
        "merge-base",
        "--is-ancestor",
        old,
        new,
        cwd=leader_root,
        timeout_s=_TRIPWIRE_TIMEOUT_S,
        optional_locks=False,
    )
    if result.timed_out or result.error is not None:
        return False
    return result.returncode == 0


# reflog 마지막 줄만 필요하다. 오래된 저장소의 `logs/HEAD`는 수 MB까지 자라므로
# 끝에서 이만큼만 읽는다. 한 줄은 sha 두 개 + 서명 + 메시지라 넉넉하다.
_REFLOG_TAIL_BYTES = 8192


def _head_moved_inside_leader(leader_root, observed_head: str) -> bool:
    """지금 HEAD가 leader 자신의 reflog가 마지막으로 적은 값인가.

    git은 HEAD reflog를 checkout마다 따로 쓴다 — leader는 `.git/logs/HEAD`,
    linked worktree는 `.git/worktrees/<name>/logs/HEAD`. 그래서 leader에서 실행한
    명령만 leader의 reflog에 남는다. tip이 지금 HEAD와 같으면 그 이동은 leader
    안에서 일어난 것이고, 사람의 `pull`/`checkout`/`commit`이 여기 속한다.

    이것은 사고와 정상 워크플로를 가르는 판별기이지 적대적 보증이 아니다.
    `.git`이 공유되므로 워커가 `GIT_DIR`을 leader로 겨눠 직접 append하면 이
    신호를 위조할 수 있다. 그 경로는 사전 차단이 막아야 한다.

    판정 불가는 "밖에서 움직였다"로 접는다. reflog가 없거나 읽히지 않으면
    이동을 정당하다고 부를 근거가 없다.
    """
    if not observed_head:
        return False
    git_directory = _git_dir(leader_root)
    if git_directory is None:
        return False
    log = git_directory / "logs" / "HEAD"
    try:
        size = log.stat().st_size
        with log.open("rb") as handle:
            if size > _REFLOG_TAIL_BYTES:
                handle.seek(size - _REFLOG_TAIL_BYTES)
            tail = handle.read()
    except OSError:
        return False
    lines = [line for line in tail.split(b"\n") if line.strip()]
    if not lines:
        return False
    # `<old-sha> <new-sha> <name> <email> <ts> <tz>\t<message>`
    fields = lines[-1].split(b" ", 2)
    if len(fields) < 2:
        return False
    try:
        tip = fields[1].decode("ascii")
    except UnicodeDecodeError:
        return False
    return tip == observed_head


def _read_registration_file(path: Path) -> tuple[os.stat_result, bytes] | None:
    if not hasattr(os, "O_NOFOLLOW"):
        return None
    try:
        observed = path.lstat()
    except OSError:
        return None
    if (
        path.is_symlink()
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.getuid()
        or observed.st_nlink != 1
    ):
        return None
    try:
        fd = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        content = os.read(fd, 4097)
    finally:
        os.close(fd)
    if (
        opened.st_dev != observed.st_dev
        or opened.st_ino != observed.st_ino
        or len(content) > 4096
    ):
        return None
    return opened, content


def _registration_admin_for_path(
    path: Path,
    *,
    common_dir: Path | None,
) -> Path | None:
    dot_git = path / ".git"
    pointer = _read_registration_file(dot_git)
    if pointer is not None:
        try:
            pointer_line = pointer[1].decode("utf-8").strip()
        except UnicodeDecodeError:
            return None
        if not pointer_line.startswith("gitdir:"):
            return None
        admin = Path(pointer_line.partition(":")[2].strip()).expanduser()
        if not admin.is_absolute():
            admin = path / admin
        return real_path(admin)
    try:
        dot_git_identity = dot_git.lstat()
    except OSError:
        dot_git_identity = None
    if (
        dot_git_identity is not None
        and stat.S_ISDIR(dot_git_identity.st_mode)
        and not dot_git.is_symlink()
        and dot_git_identity.st_uid == os.getuid()
    ):
        return real_path(dot_git)
    if common_dir is None:
        return None

    registry = real_path(common_dir) / "worktrees"
    try:
        candidates = tuple(registry.iterdir())
    except OSError:
        return None
    expected_backlink = real_path(dot_git)
    matches: list[Path] = []
    for candidate in candidates:
        backlink = _read_registration_file(candidate / "gitdir")
        if backlink is None:
            continue
        backlink_path = Path(
            backlink[1].decode("utf-8", errors="replace").strip()
        ).expanduser()
        if not backlink_path.is_absolute():
            backlink_path = candidate / backlink_path
        if real_path(backlink_path) == expected_backlink:
            matches.append(real_path(candidate))
    return matches[0] if len(matches) == 1 else None


def _registered_worktree_identity(
    path: Path,
    *,
    common_dir: Path | None = None,
) -> Optional[str]:
    admin_path = _registration_admin_for_path(path, common_dir=common_dir)
    if admin_path is None:
        return None
    try:
        admin_identity = admin_path.lstat()
    except OSError:
        return None
    if (
        admin_path.is_symlink()
        or not stat.S_ISDIR(admin_identity.st_mode)
        or admin_identity.st_uid != os.getuid()
    ):
        return None

    backlink_identity: os.stat_result | None = None
    backlink_content = b""
    if admin_path != real_path(path / ".git"):
        backlink = _read_registration_file(admin_path / "gitdir")
        if backlink is None:
            return None
        backlink_identity, backlink_content = backlink
        backlink_path = Path(
            backlink_content.decode("utf-8", errors="replace").strip()
        ).expanduser()
        if not backlink_path.is_absolute():
            backlink_path = admin_path / backlink_path
        if real_path(backlink_path) != real_path(path / ".git"):
            return None

    payload = json.dumps(
        (
            str(real_path(path)),
            str(admin_path),
            admin_identity.st_dev,
            admin_identity.st_ino,
            admin_identity.st_uid,
            admin_identity.st_mode,
            None if backlink_identity is None else backlink_identity.st_dev,
            None if backlink_identity is None else backlink_identity.st_ino,
            None if backlink_identity is None else backlink_identity.st_uid,
            None if backlink_identity is None else backlink_identity.st_mode,
            None if backlink_identity is None else backlink_identity.st_nlink,
            None if backlink_identity is None else backlink_identity.st_ctime_ns,
            backlink_content.hex(),
        ),
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class RegisteredWorktree:
    """``git worktree list --porcelain``이 보고한 한 행.

    제거·정리의 진실 원천은 manifest 이름이 아니라 이 값이다. 이름 정규화 규칙을
    바꾸면 조회가 깨지지만, git이 보고한 경로는 규칙과 무관하게 항상 맞다.
    """

    path: Path                    # realpath된 절대경로
    branch: Optional[str]         # 'refs/heads/x' -> 'x'; detached면 None
    head: Optional[str] = None
    bare: bool = False
    locked: bool = False
    prunable: bool = False
    registration_identity: Optional[str] = None


def list_registered_worktrees(root) -> list:
    """git이 등록한 worktree 전부. **실패하면 raise한다 (fail-closed).**

    빈 목록을 실패의 대체값으로 쓰면 안 된다. 조회 실패와 "등록된 worktree가
    없다"가 같은 값이 되는 순간, 제거 경로는 지울 게 없다고 판단하고 소유권
    증명은 통과할 근거 없이 통과한다. 표시용 강등이 필요하면 호출부에서
    ``WorktreeIsolationError``를 잡아 그 자리에서 명시적으로 하라 — 이 함수가
    대신 삼켜 주지 않는다.
    """
    return _parse_worktree_list(
        _registered_worktree_payload(root),
        common_dir=_git_common_dir(root),
    )


def registered_worktree_at(root, path) -> Optional[RegisteredWorktree]:
    """대상 경로 한 행만 돌려준다. 같은 fail-closed 계약이다.

    등록 행마다 지문을 계산하면 등록 수 N에 비례하는 lstat/open/read가 매
    경계에 얹혀 lifecycle 연산당 O(N²)로 증폭된다. 필요한 것은 대상 1행이므로
    파싱 루프 안에서 걸러 그 행만 지문을 만든다.

    지문은 **스냅샷 시점에 즉시** 계산해야 한다. 접근 시점으로 미루면
    ``_same_registration``의 before/after 비교가 항상 같은 값을 읽어 ABA 탐지가
    조용히 사라진다.
    """
    entries = _parse_worktree_list(
        _registered_worktree_payload(root),
        common_dir=_git_common_dir(root),
        only_path=path,
    )
    return entries[0] if entries else None


def _registered_worktree_payload(root) -> str:
    result = git_safe("worktree", "list", "--porcelain", cwd=root, optional_locks=False)
    if not result.ok:
        raise _tripwire_git_error(f"cannot list registered worktrees for {root}", result)
    return result.stdout


def _parse_worktree_list(
    payload: str,
    *,
    common_dir: Path | None = None,
    only_path=None,
) -> list:
    entries: list = []
    current: dict = {}
    wanted = worktree_path_key(only_path) if only_path is not None else None

    def flush() -> None:
        raw = current.get("worktree")
        if raw:
            path = real_path(raw)
            if wanted is None or worktree_path_key(path) == wanted:
                branch = current.get("branch") or None
                entries.append(
                    RegisteredWorktree(
                        path=path,
                        branch=branch.removeprefix("refs/heads/") if branch else None,
                        head=current.get("HEAD") or None,
                        bare="bare" in current,
                        locked="locked" in current,
                        prunable="prunable" in current,
                        registration_identity=_registered_worktree_identity(
                            path,
                            common_dir=common_dir,
                        ),
                    )
                )
        current.clear()

    for line in payload.splitlines():
        if not line.strip():
            flush()
            continue
        key, _, value = line.partition(" ")
        current[key] = value.strip()
    flush()
    return entries


def worktree_path_key(path) -> str:
    """경로 비교용 정규화 키. 이름이 아니라 이걸로 대상을 찾는다.

    ``realpath``가 symlink와 macOS의 ``/private`` 별칭을, ``normcase``가 대소문자를
    구분하지 않는 파일시스템을 흡수한다. 존재하지 않는 경로도 정규화되므로 비교를
    건너뛰는 분기가 없다 — 정규화 실패 시 가드를 스킵하면 그게 곧 fail-open이다.
    """
    return os.path.normcase(str(real_path(path)))


def same_worktree_path(left, right) -> bool:
    return worktree_path_key(left) == worktree_path_key(right)


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
