from __future__ import annotations

import contextlib
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Sequence

from agent_flow.core.worktree_isolation import (
    ProviderLease,
    WorktreeIsolationError,
    assert_cwd_bound,
    assert_provider_lease,
    git_common_dir,
    git_dir,
    leader_root_for,
    managed_worktree_root,
    provider_lease,
    list_registered_worktrees,
    real_path,
    sanitized_worker_env,
    verify_linked_worktree,
)

_MACOS_SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
_HOST_STATE_DIR_NAMES = (".claude", ".Codex", ".codex", ".omp", ".agents")
MAX_PROVIDER_OUTPUT_BYTES = 8 * 1024 * 1024
_PROVIDER_POLL_S = 0.05
_PROVIDER_REAP_TIMEOUT_S = 5.0
_PROVIDER_OUTPUT_LIMIT_MESSAGE = (
    f"provider output exceeded {MAX_PROVIDER_OUTPUT_BYTES} bytes"
)


@dataclass(frozen=True)
class ProviderCommand:
    name: str
    argv: tuple[str, ...]
    prompt_via_stdin: bool = True
    timeout_s: int = 900
    allow_workspace_writes: bool = True


@dataclass(frozen=True)
class ProviderResult:
    name: str
    exit_code: int | None
    stdout: str
    stderr: str
    failed: bool


@dataclass(frozen=True)
class ConfinedProviderLaunch:
    argv: tuple[str, ...]
    cwd: Path
    env: dict
    lease: ProviderLease
    scratch: Path


@contextlib.contextmanager
def confined_provider_launch(
    *,
    argv: Sequence[str],
    cwd: Path,
    env: dict | None = None,
    lease: ProviderLease | None = None,
    allow_workspace_writes: bool = True,
) -> Iterator[ConfinedProviderLaunch]:
    """Verify cwd/backend and hold a repository slot for the process tree."""

    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise WorktreeIsolationError("provider argv must contain non-empty strings")
    # backend 검증이 먼저다. 이 OS에 write-sandbox가 없으면 어떤 관측도 하기 전에
    # 멈춰야 한다 — git을 먼저 부르면 막을 수 없는 플랫폼에서 일을 시작한 셈이다.
    backend = verify_provider_sandbox_backend()
    verified = _verified_provider_worktree(cwd)
    provider_kind = _provider_kind(argv[0])
    bound_argv = _bind_provider_cli_cwd(
        argv,
        verified,
        provider_kind=provider_kind,
    )
    provider_argv = _verified_provider_argv(bound_argv, verified, env)
    with tempfile.TemporaryDirectory(prefix="agent-flow-provider-") as temp_dir:
        scratch = real_path(Path(temp_dir))
        host_home = Path(
            str((env if env is not None else os.environ).get("HOME") or Path.home())
        ).expanduser()
        profile = _macos_sandbox_profile(
            verified,
            allow_workspace_writes=allow_workspace_writes,
            scratch=scratch,
            host_home=host_home,
        )
        sandboxed_argv = (str(backend), "-p", profile, *provider_argv)
        worker_env = sanitized_worker_env(base_env=env)
        worker_env.update(
            {
                "PWD": str(verified),
                "TMPDIR": str(scratch),
                "TMP": str(scratch),
                "TEMP": str(scratch),
                "XDG_CACHE_HOME": str(scratch / "cache"),
            }
        )
        _prepare_provider_state(
            argv=provider_argv,
            env=worker_env,
            scratch=scratch,
            provider_kind=provider_kind,
        )
        if lease is not None:
            assert_provider_lease(lease, root=verified)
            yield ConfinedProviderLaunch(
                argv=sandboxed_argv,
                cwd=verified,
                env=worker_env,
                lease=lease,
                scratch=scratch,
            )
            return
        with provider_lease(verified) as acquired:
            yield ConfinedProviderLaunch(
                argv=sandboxed_argv,
                cwd=verified,
                env=worker_env,
                lease=acquired,
                scratch=scratch,
            )


def _verified_provider_argv(
    argv: Sequence[str],
    verified_worktree: Path,
    env: dict | None,
) -> tuple[str, ...]:
    source_env = env if env is not None else os.environ
    candidate = shutil.which(argv[0], path=source_env.get("PATH"))
    if candidate is None:
        raise WorktreeIsolationError(
            f"provider executable is unavailable: {argv[0]}"
        )
    executable = real_path(candidate)
    try:
        identity = executable.stat()
    except OSError as exc:
        raise WorktreeIsolationError(
            f"provider executable identity cannot be verified: {executable}"
        ) from exc
    leader = leader_root_for(verified_worktree)
    if leader is None:
        raise WorktreeIsolationError(
            "provider executable verification requires a linked worktree"
        )
    repository_roots = (
        entry.path for entry in list_registered_worktrees(leader)
    )
    inside_repository = any(
        _path_is_within(executable, root) for root in repository_roots
    )
    if (
        not stat.S_ISREG(identity.st_mode)
        or identity.st_uid not in (0, os.getuid())
        or identity.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not os.access(executable, os.X_OK)
        or inside_repository
    ):
        raise WorktreeIsolationError(
            f"provider executable is not a trusted external regular file: {executable}"
        )
    return (str(executable), *argv[1:])


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(real_path(root))
        return True
    except ValueError:
        return False


def _provider_kind(command: str) -> str | None:
    binary = Path(command).name.lower()
    if binary in {"codex", "codex.js"} or binary.startswith("codex-"):
        return "codex"
    if binary == "omp":
        return "omp"
    return None


def _provider_cwd_options(provider_kind: str | None) -> tuple[str, ...]:
    if provider_kind == "codex":
        return ("--cd", "-C")
    if provider_kind == "omp":
        return ("--cwd",)
    return ()


def _bind_provider_cli_cwd(
    argv: Sequence[str],
    verified: Path,
    *,
    provider_kind: str | None,
) -> tuple[str, ...]:
    if _assert_provider_cli_cwd(
        argv,
        verified,
        provider_kind=provider_kind,
    ):
        return tuple(argv)
    option = "--cd" if provider_kind == "codex" else "--cwd"
    return (argv[0], option, str(verified), *argv[1:])


def _assert_provider_cli_cwd(
    argv: Sequence[str],
    verified: Path,
    *,
    provider_kind: str | None = None,
) -> bool:
    binary = Path(argv[0]).name.lower()
    kind = provider_kind if provider_kind is not None else _provider_kind(argv[0])
    options = _provider_cwd_options(kind)
    if not options:
        return True
    values: list[tuple[str, str]] = []
    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument in options:
            if index + 1 >= len(argv):
                raise WorktreeIsolationError(
                    f"{binary} {argument} requires a worktree path"
                )
            values.append((argument, argv[index + 1]))
            index += 2
            continue
        matching = next(
            (
                option
                for option in options
                if option.startswith("--") and argument.startswith(f"{option}=")
            ),
            None,
        )
        if matching is not None:
            values.append((matching, argument.split("=", 1)[1]))
        index += 1
    for option, value in values:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = verified / candidate
        if real_path(candidate) != verified:
            raise WorktreeIsolationError(
                f"{binary} {option} path does not match the verified worktree: "
                f"{value!r} != {str(verified)!r}"
            )
    return bool(values)


def run_provider(
    command: ProviderCommand,
    *,
    prompt: str,
    cwd: Path,
    env: dict | None = None,
    lease: ProviderLease | None = None,
) -> ProviderResult:
    proc: subprocess.Popen[bytes] | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    outcome = "failed"
    try:
        with confined_provider_launch(
            argv=command.argv,
            cwd=cwd,
            env=env,
            lease=lease,
            allow_workspace_writes=command.allow_workspace_writes,
        ) as launch:
            stdout_path = launch.scratch / "stdout"
            stderr_path = launch.scratch / "stderr"
            prompt_path = launch.scratch / "prompt"
            prompt_file = None
            if command.prompt_via_stdin:
                prompt_path.write_text(prompt, encoding="utf-8")
                prompt_file = prompt_path.open("rb")
            try:
                with stdout_path.open("xb") as stdout_file, stderr_path.open(
                    "xb"
                ) as stderr_file:
                    launch_identity = _provider_worktree_identity(launch.cwd)
                    proc = subprocess.Popen(
                        launch.argv,
                        cwd=launch.cwd,
                        env=launch.env,
                        stdin=prompt_file if prompt_file is not None else subprocess.DEVNULL,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        start_new_session=True,
                        close_fds=True,
                        pass_fds=launch.lease.process_lifetime_fds,
                    )
                    if _provider_worktree_identity(launch.cwd) != launch_identity:
                        raise WorktreeIsolationError(
                            "verified provider worktree identity changed during launch"
                        )
                    outcome = _wait_for_provider(
                        proc,
                        timeout_s=command.timeout_s,
                        stdout_path=stdout_path,
                        stderr_path=stderr_path,
                    )
                    if outcome != "completed":
                        _kill_process_tree(proc)
                    _bounded_reap(proc)
                    if _provider_output_exceeded(stdout_path, stderr_path):
                        outcome = "output-limit"
            finally:
                if prompt_file is not None:
                    prompt_file.close()
                if proc is not None:
                    _kill_process_tree(proc)
                    _bounded_reap(proc)

            stdout, stderr = _read_provider_outputs(stdout_path, stderr_path)
            if outcome == "output-limit":
                stderr = _append_error(stderr, _PROVIDER_OUTPUT_LIMIT_MESSAGE)
            return ProviderResult(
                name=command.name,
                exit_code=proc.returncode if outcome == "completed" else None,
                stdout=stdout,
                stderr=stderr,
                failed=outcome != "completed" or proc.returncode != 0,
            )
    except (OSError, WorktreeIsolationError) as exc:
        stdout, stderr = _read_provider_outputs(stdout_path, stderr_path)
        return ProviderResult(
            name=command.name,
            exit_code=None,
            stdout=stdout,
            stderr=_append_error(stderr, str(exc)),
            failed=True,
        )


def _verified_provider_worktree(cwd: Path) -> Path:
    candidate = real_path(cwd)
    leader = leader_root_for(candidate)
    if leader is None:
        raise WorktreeIsolationError(
            "external providers require a verified linked worktree cwd"
        )
    managed_root = managed_worktree_root(root=leader, path=candidate)
    verified = verify_linked_worktree(
        root=leader,
        path=candidate,
        managed_root=managed_root,
    )
    assert_cwd_bound(worktree_path=verified, cwd=candidate)
    return verified
def _provider_worktree_identity(worktree: Path) -> tuple[object, ...]:
    verified = _verified_provider_worktree(worktree)
    checkout_identity = verified.stat()
    pointer = verified / ".git"
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(pointer, flags)
    except OSError as exc:
        raise WorktreeIsolationError(
            "verified provider worktree pointer could not be pinned"
        ) from exc
    try:
        pointer_identity = os.fstat(fd)
        pointer_content = os.read(fd, 4097)
    finally:
        os.close(fd)
    if (
        not stat.S_ISREG(pointer_identity.st_mode)
        or pointer_identity.st_nlink != 1
        or len(pointer_content) > 4096
    ):
        raise WorktreeIsolationError(
            "verified provider worktree pointer identity is not trustworthy"
        )
    return (
        checkout_identity.st_dev,
        checkout_identity.st_ino,
        pointer_identity.st_dev,
        pointer_identity.st_ino,
        pointer_identity.st_uid,
        pointer_identity.st_mode,
        pointer_identity.st_nlink,
        pointer_identity.st_size,
        pointer_identity.st_ctime_ns,
        pointer_content,
    )




def verify_provider_sandbox_backend() -> Path:
    """Return the single supported write-confinement backend or fail closed."""

    if sys.platform != "darwin":
        raise WorktreeIsolationError(
            "no verified provider write-sandbox backend for this operating system"
        )
    expected = Path("/usr/bin/sandbox-exec")
    candidate = _MACOS_SANDBOX_EXEC
    backend = real_path(candidate)
    if (
        candidate != expected
        or backend != expected
        or not _is_trusted_system_executable(backend)
    ):
        raise WorktreeIsolationError(
            "the verified macOS sandbox-exec backend is unavailable"
        )
    return backend


def _prepare_provider_state(
    *,
    argv: Sequence[str],
    env: dict[str, str],
    scratch: Path,
    provider_kind: str | None = None,
) -> None:
    binary = provider_kind or _provider_kind(argv[0])
    if binary == "codex":
        state_home = scratch / "codex-home"
        state_home.mkdir(mode=0o700)
        configured_home = env.get("CODEX_HOME")
        source_home = (
            Path(configured_home).expanduser()
            if configured_home
            else Path(env.get("HOME", str(Path.home()))) / ".codex"
        )
        _copy_private_auth_file(source_home / "auth.json", state_home / "auth.json")
        env["CODEX_HOME"] = str(state_home)
    elif binary == "omp":
        source_agent_home = Path(
            env.get(
                "PI_CODING_AGENT_DIR",
                str(Path(env.get("HOME", str(Path.home()))) / ".omp" / "agent"),
            )
        ).expanduser()
        session_home = scratch / "omp-agent"
        session_home.mkdir(mode=0o700)
        _snapshot_omp_state(source_agent_home, session_home)
        isolated_home = scratch / "home"
        isolated_home.mkdir(mode=0o700)
        env["HOME"] = str(isolated_home)
        env["PI_CODING_AGENT_DIR"] = str(session_home)


def _snapshot_omp_state(source_home: Path, target_home: Path) -> None:
    config = source_home / "config.yml"
    _copy_owned_regular_file(
        config,
        target_home / config.name,
        max_size=1024 * 1024,
        require_private=True,
        missing_ok=True,
    )
    for name in ("models.db", "agent.db"):
        source = source_home / name
        snapshot_path = target_home / f".{name}.snapshot"
        copied = _copy_owned_regular_file(
            source,
            snapshot_path,
            max_size=128 * 1024 * 1024,
            require_private=False,
            missing_ok=True,
        )
        if not copied:
            continue
        target_path = target_home / name
        source_connection: sqlite3.Connection | None = None
        target_connection: sqlite3.Connection | None = None
        try:
            source_connection = sqlite3.connect(
                f"{snapshot_path.resolve().as_uri()}?mode=ro", uri=True
            )
            target_connection = sqlite3.connect(target_path)
            source_connection.backup(target_connection)
        except Exception:
            target_path.unlink(missing_ok=True)
            raise
        finally:
            if target_connection is not None:
                target_connection.close()
            if source_connection is not None:
                source_connection.close()
            snapshot_path.unlink(missing_ok=True)
        target_path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _open_owned_regular_file(
    source: Path,
    *,
    max_size: int,
    require_private: bool,
    missing_ok: bool,
) -> BinaryIO | None:
    if not hasattr(os, "O_NOFOLLOW"):
        raise WorktreeIsolationError(
            "provider state cannot be opened without no-follow support"
        )
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(source, flags)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise WorktreeIsolationError(f"provider state seed disappeared: {source}")
    except OSError as exc:
        raise WorktreeIsolationError(
            f"provider state seed could not be opened safely: {source}"
        ) from exc
    try:
        identity = os.fstat(fd)
        disallowed_mode = (
            stat.S_IRWXG | stat.S_IRWXO
            if require_private
            else stat.S_IWGRP | stat.S_IWOTH
        )
        if (
            not stat.S_ISREG(identity.st_mode)
            or identity.st_uid != os.getuid()
            or identity.st_size > max_size
            or identity.st_mode & disallowed_mode
        ):
            raise WorktreeIsolationError(
                f"provider state seed is not an owned regular file: {source}"
            )
        return os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise


def _copy_owned_regular_file(
    source: Path,
    target: Path,
    *,
    max_size: int,
    require_private: bool,
    missing_ok: bool = False,
) -> bool:
    input_file = _open_owned_regular_file(
        source,
        max_size=max_size,
        require_private=require_private,
        missing_ok=missing_ok,
    )
    if input_file is None:
        return False
    try:
        with input_file, target.open("xb") as output_file:
            copied = 0
            while True:
                chunk = input_file.read(min(1024 * 1024, max_size - copied + 1))
                if not chunk:
                    break
                copied += len(chunk)
                if copied > max_size:
                    raise WorktreeIsolationError(
                        f"provider state seed exceeded its copy limit: {source}"
                    )
                output_file.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return True


def _copy_private_auth_file(source: Path, target: Path) -> None:
    _copy_owned_regular_file(
        source,
        target,
        max_size=1024 * 1024,
        require_private=True,
        missing_ok=True,
    )


def _is_trusted_system_executable(path: Path) -> bool:
    try:
        identity = path.stat()
    except OSError:
        return False
    return (
        stat.S_ISREG(identity.st_mode)
        and identity.st_uid == 0
        and (identity.st_mode & (stat.S_IWGRP | stat.S_IWOTH)) == 0
        and os.access(path, os.X_OK)
    )




def _macos_sandbox_profile(
    worktree: Path,
    *,
    allow_workspace_writes: bool = True,
    scratch: Path | None = None,
    host_home: Path | None = None,
) -> str:
    canonical = real_path(worktree)
    leader = leader_root_for(canonical)
    if leader is None:
        raise WorktreeIsolationError(
            "provider sandbox requires a verified linked worktree"
        )
    protected_parents = {
        real_path(Path.home()),
        real_path(leader),
        canonical,
    }
    if host_home is not None:
        protected_parents.add(real_path(host_home))
    protected = sorted(
        {
            real_path(parent / name)
            for parent in protected_parents
            for name in _HOST_STATE_DIR_NAMES
        },
        key=str,
    )
    protected_rules = tuple(
        f'(deny file-write* (subpath "{_sandbox_path(root)}"))'
        for root in protected
    )
    workspace_rules = (
        (f'(allow file-write* (subpath "{_sandbox_path(canonical)}"))',)
        if allow_workspace_writes
        else ()
    )
    git_control_rules = (
        f'(deny file-write* (literal "{_sandbox_path(canonical / ".git")}"))',
    )
    # 공유 gitdir 쓰기는 **커밋할 프로세스에만** 준다. read-only 실행(reviewer)은
    # 커밋하지 않으므로 이 규칙이 필요 없고, 주면 workspace가 잠긴 프로세스가
    # `<common>/refs/heads/<protected>`를 직접 덮어써 hook 기반 commit/push 보호를
    # 우회할 수 있다. 그쪽은 파일시스템 권한이 유일한 방어다.
    git_shared_rules = (
        _git_shared_write_rules(canonical) if allow_workspace_writes else ()
    )
    scratch_rules = (
        (f'(allow file-write* (subpath "{_sandbox_path(real_path(scratch))}"))',)
        if scratch is not None
        else ()
    )
    return "\n".join(
        (
            "(version 1)",
            "(allow default)",
            "(deny file-write*)",
            *workspace_rules,
            *scratch_rules,
            *git_shared_rules,
            *git_control_rules,
            '(allow file-write-data (literal "/dev/null"))',
            *protected_rules,
        )
    )


def _git_shared_write_rules(worktree: Path) -> tuple[str, ...]:
    """linked worktree가 자기 작업을 커밋하는 데 필요한 공유 gitdir 자리만 연다.

    linked worktree의 index·HEAD·objects·refs는 checkout 안이 아니라 leader의 git
    common dir 아래에 있다. 그 자리를 통째로 막으면 provider의 `git add` 한 줄이
    `Unable to create '<leader>/.git/worktrees/<name>/index.lock': Operation not
    permitted`로 죽는다 — worktree 안에서 아무것도 커밋할 수 없다는 뜻이고,
    leader가 사용자 홈의 보호 폴더에 있으면 그 폴더가 잠긴 것처럼 보인다.

    그래서 커밋 경로만 열고, 다음 git 실행 때 코드를 실행시킬 수 있는 자리는 계속
    막는다: `hooks/`와 `config`는 애초에 열지 않고, worktree 전용 gitdir 안에서도
    `config.worktree`(`core.hooksPath` 재지정)와 `commondir`(저장소 재지정)는 명시적으로
    되막는다. sandbox 규칙은 마지막에 일치한 것이 이기므로 deny를 뒤에 둔다.
    """
    common = git_common_dir(worktree)
    gitdir = git_dir(worktree)
    if common is None or gitdir is None:
        raise WorktreeIsolationError(
            f"provider sandbox cannot resolve the git directories for: {worktree}"
        )
    allowed_subpaths = (gitdir, common / "objects", common / "refs", common / "logs")
    allowed_literals = (common / "packed-refs", common / "packed-refs.lock")
    denied_literals = (gitdir / "config.worktree", gitdir / "commondir")
    return (
        *(
            f'(allow file-write* (subpath "{_sandbox_path(path)}"))'
            for path in allowed_subpaths
        ),
        *(
            f'(allow file-write* (literal "{_sandbox_path(path)}"))'
            for path in allowed_literals
        ),
        *(
            f'(deny file-write* (literal "{_sandbox_path(path)}"))'
            for path in denied_literals
        ),
    )


def _sandbox_path(path: Path) -> str:
    escaped = str(path).replace("\\", "\\\\").replace('"', '\\"')
    if any(character in escaped for character in ("\x00", "\n", "\r")):
        raise WorktreeIsolationError(
            "worktree path cannot be represented by the sandbox backend"
        )
    return escaped


def _wait_for_provider(
    proc: subprocess.Popen[bytes],
    *,
    timeout_s: float,
    stdout_path: Path,
    stderr_path: Path,
) -> str:
    deadline = time.monotonic() + max(timeout_s, 0)
    while True:
        if _provider_output_exceeded(stdout_path, stderr_path):
            return "output-limit"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout"
        try:
            proc.wait(timeout=min(_PROVIDER_POLL_S, remaining))
        except subprocess.TimeoutExpired:
            continue
        return "completed"


def _bounded_reap(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.wait(timeout=_PROVIDER_REAP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            proc.wait(timeout=_PROVIDER_REAP_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise WorktreeIsolationError(
                "provider process could not be reaped within the bounded deadline"
            ) from exc


def _provider_output_exceeded(stdout_path: Path, stderr_path: Path) -> bool:
    total = 0
    for path in (stdout_path, stderr_path):
        try:
            total += path.stat().st_size
        except FileNotFoundError:
            continue
        if total > MAX_PROVIDER_OUTPUT_BYTES:
            return True
    return False


def _read_provider_outputs(
    stdout_path: Path | None,
    stderr_path: Path | None,
) -> tuple[str, str]:
    stdout_bytes = _read_provider_output_bytes(
        stdout_path, MAX_PROVIDER_OUTPUT_BYTES
    )
    stderr_bytes = _read_provider_output_bytes(
        stderr_path,
        MAX_PROVIDER_OUTPUT_BYTES - len(stdout_bytes),
    )
    return (
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


def _read_provider_output_bytes(path: Path | None, limit: int) -> bytes:
    if path is None or limit <= 0 or not path.is_file():
        return b""
    with path.open("rb") as stream:
        return stream.read(limit)


def _append_error(stderr: str, message: str) -> str:
    return f"{stderr.rstrip()}\n{message}\n" if stderr else f"{message}\n"


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return
    except OSError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value

