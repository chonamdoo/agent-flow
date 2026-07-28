from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


DEFAULT_COMMAND_TIMEOUT_S = 30


@dataclass(frozen=True)
class SafeCommandResult:
    args: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.error is None


def run_safe_command(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
    env: dict | None = None,
) -> SafeCommandResult:
    # Timeout은 직접 자식만이 아니라 그 자식이 띄운 CLI까지 함께 종료한다. 그렇지
    # 않으면 호출자는 fail-closed로 돌아와도 뒤에 남은 프로세스가 쓰기를 계속한다.
    command = tuple(str(arg) for arg in args)
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=os.name != "nt",
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
        try:
            stdout, stderr = process.communicate(input=input_text, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_process_tree(process)
            stdout, stderr = process.communicate()
            return SafeCommandResult(
                args=command,
                returncode=None,
                stdout=_text(stdout),
                stderr=_text(stderr) or f"command timed out after {timeout_s}s",
                timed_out=True,
            )
    except OSError as exc:
        return SafeCommandResult(
            args=command,
            returncode=None,
            stdout="",
            stderr=str(exc),
            error=str(exc),
        )
    return SafeCommandResult(
        args=command,
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
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
