from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProviderCommand:
    name: str
    argv: tuple[str, ...]
    prompt_via_stdin: bool = True
    timeout_s: int = 900


@dataclass(frozen=True)
class ProviderResult:
    name: str
    exit_code: int | None
    stdout: str
    stderr: str
    failed: bool


def run_provider(command: ProviderCommand, *, prompt: str, cwd: Path, env: dict | None = None) -> ProviderResult:
    try:
        completed = subprocess.run(
            command.argv,
            cwd=cwd,
            env=env,
            input=prompt if command.prompt_via_stdin else "",
            text=True,
            capture_output=True,
            timeout=command.timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return ProviderResult(
            name=command.name,
            exit_code=None,
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr),
            failed=True,
        )
    except OSError as exc:
        return ProviderResult(
            name=command.name,
            exit_code=None,
            stdout="",
            stderr=str(exc),
            failed=True,
        )
    return ProviderResult(
        name=command.name,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        failed=completed.returncode != 0,
    )


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value

