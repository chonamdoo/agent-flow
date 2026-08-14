from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

DEFAULT_COMMAND_TIMEOUT_S = 30
_COMMAND_REAP_TIMEOUT_S = 1.0
_COMMAND_OUTPUT_LIMIT_MESSAGE = "command output exceeded {limit} bytes"
_COMMAND_OUTPUT_POLL_S = 0.05


@dataclass(frozen=True)
class SafeCommandResult:
    args: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    output_truncated: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.returncode == 0
            and not self.timed_out
            and not self.output_truncated
            and self.error is None
        )


def run_safe_command(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
    env: Mapping[str, str] | None = None,
    pass_fds: tuple[int, ...] = (),
    cwd_fd: int | None = None,
    max_output_bytes: int | None = None,
) -> SafeCommandResult:
    # Timeout은 직접 자식만이 아니라 그 자식이 띄운 CLI까지 함께 종료한다. 그렇지
    # 않으면 호출자는 fail-closed로 돌아와도 뒤에 남은 프로세스가 쓰기를 계속한다.
    command = tuple(str(arg) for arg in args)
    if max_output_bytes is not None and max_output_bytes < 1:
        raise ValueError("max_output_bytes must be positive")
    if cwd_fd is not None:
        if os.name == "nt":
            return SafeCommandResult(
                args=command,
                returncode=None,
                stdout="",
                stderr="directory-fd cwd is unavailable on Windows",
                error="directory-fd cwd is unavailable on Windows",
            )
        pass_fds = tuple(dict.fromkeys((*pass_fds, cwd_fd)))
    stdout_capture: BinaryIO | None = None
    stderr_capture: BinaryIO | None = None
    try:
        try:
            if max_output_bytes is not None:
                stdout_capture = tempfile.TemporaryFile()  # noqa: SIM115
                stderr_capture = tempfile.TemporaryFile()  # noqa: SIM115
            process = subprocess.Popen(
                command,
                cwd=None if cwd_fd is not None else cwd,
                env=env,
                stdin=subprocess.PIPE if input_text is not None else None,
                stdout=stdout_capture or subprocess.PIPE,
                stderr=stderr_capture or subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=os.name != "nt",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
                close_fds=True,
                pass_fds=pass_fds,
                # Python has no cwd-fd argument. fchdir is a single async-signal-safe
                # syscall, and avoids reopening a verified directory by path.
                preexec_fn=(  # noqa: PLW1509
                    (lambda: os.fchdir(cwd_fd))
                    if cwd_fd is not None and os.name != "nt"
                    else None
                ),
            )
            if stdout_capture is not None and stderr_capture is not None:
                stdout, stderr, timed_out = _communicate_with_output_limit(
                    process,
                    input_text=input_text,
                    timeout_s=timeout_s,
                    stdout_capture=stdout_capture,
                    stderr_capture=stderr_capture,
                    max_output_bytes=max_output_bytes,
                )
            else:
                timed_out = False
                try:
                    stdout, stderr = process.communicate(
                        input=input_text,
                        timeout=timeout_s,
                    )
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_process_tree(process)
                    stdout, stderr = _communicate_after_kill(process)
                else:
                    _kill_process_tree(process)
            output_truncated = False
            if stdout_capture is not None and stderr_capture is not None:
                stdout, stderr, output_truncated = _read_capped_output(
                    stdout_capture,
                    stderr_capture,
                    max_output_bytes,
                )
            if timed_out:
                return SafeCommandResult(
                    args=command,
                    returncode=None,
                    stdout=_text(stdout),
                    stderr=_text(stderr) or f"command timed out after {timeout_s}s",
                    timed_out=True,
                    output_truncated=output_truncated,
                )
            return SafeCommandResult(
                args=command,
                returncode=process.returncode,
                stdout=_text(stdout),
                stderr=_text(stderr),
                output_truncated=output_truncated,
            )
        except OSError as exc:
            return SafeCommandResult(
                args=command,
                returncode=None,
                stdout="",
                stderr=str(exc),
                error=str(exc),
            )
    finally:
        if stdout_capture is not None:
            stdout_capture.close()
        if stderr_capture is not None:
            stderr_capture.close()


def _communicate_with_output_limit(
    process: subprocess.Popen[str],
    *,
    input_text: str | None,
    timeout_s: float,
    stdout_capture: BinaryIO,
    stderr_capture: BinaryIO,
    max_output_bytes: int | None,
) -> tuple[str | bytes | None, str | bytes | None, bool]:
    assert max_output_bytes is not None
    deadline = time.monotonic() + max(timeout_s, 0)
    pending_input = input_text
    while True:
        if _captured_output_exceeded(
            stdout_capture,
            stderr_capture,
            max_output_bytes,
        ):
            _kill_process_tree(process)
            stdout, stderr = _communicate_after_kill(process)
            return stdout, stderr, False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_process_tree(process)
            stdout, stderr = _communicate_after_kill(process)
            return stdout, stderr, True
        try:
            stdout, stderr = process.communicate(
                input=pending_input,
                timeout=min(_COMMAND_OUTPUT_POLL_S, remaining),
            )
            _kill_process_tree(process)
            return stdout, stderr, False
        except subprocess.TimeoutExpired:
            pending_input = None


def _captured_output_exceeded(
    stdout: BinaryIO,
    stderr: BinaryIO,
    limit: int,
) -> bool:
    return (
        os.fstat(stdout.fileno()).st_size
        + os.fstat(stderr.fileno()).st_size
        > limit
    )


def _read_capped_output(
    stdout: BinaryIO,
    stderr: BinaryIO,
    limit: int | None,
) -> tuple[str, str, bool]:
    assert limit is not None
    stdout_size = os.fstat(stdout.fileno()).st_size
    stderr_size = os.fstat(stderr.fileno()).st_size
    stdout.seek(0)
    stdout_bytes = stdout.read(min(stdout_size, limit))
    remaining = max(0, limit - len(stdout_bytes))
    stderr.seek(0)
    stderr_bytes = stderr.read(min(stderr_size, remaining))
    truncated = stdout_size + stderr_size > limit
    stderr_text = _text(stderr_bytes)
    if truncated:
        marker = _COMMAND_OUTPUT_LIMIT_MESSAGE.format(limit=limit)
        stderr_text = f"{stderr_text.rstrip()}\n{marker}".lstrip()
    return _text(stdout_bytes), stderr_text, truncated


def _communicate_after_kill(
    process: subprocess.Popen[str],
) -> tuple[str | bytes | None, str | bytes | None]:
    try:
        return process.communicate(timeout=_COMMAND_REAP_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        try:
            process.wait(timeout=_COMMAND_REAP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=_COMMAND_REAP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                pass
        return exc.output, exc.stderr



def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        if process.poll() is not None:
            return
        try:
            subprocess.run(
                ("taskkill", "/PID", str(process.pid), "/T", "/F"),
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        if process.poll() is None:
            process.kill()


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
