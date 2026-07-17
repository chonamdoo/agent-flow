from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


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
    env_extra: Mapping[str, str] | None = None,
    input_text: str | None = None,
    timeout_s: int = DEFAULT_COMMAND_TIMEOUT_S,
) -> SafeCommandResult:
    # 외부 CLI는 자동 릴레이를 멈추지 않도록 항상 timeout과 오류 캡처를 적용한다.
    command = tuple(str(arg) for arg in args)
    configured_git = os.environ.get("AGENT_FLOW_GIT_EXECUTABLE")
    if command and command[0] == "git" and configured_git and Path(configured_git).is_absolute():
        command = (configured_git, *command[1:])
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env={**os.environ, **env_extra} if env_extra is not None else None,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return SafeCommandResult(
            args=command,
            returncode=None,
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr) or f"command timed out after {timeout_s}s",
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
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
