from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GateCommand:
    gate_id: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class GateResult:
    gate_id: str
    command: tuple[str, ...]
    passed: bool
    exit_code: int | None
    stdout: str
    stderr: str


def run_gate(command: GateCommand, *, cwd: Path, timeout_s: int = 600) -> GateResult:
    try:
        completed = subprocess.run(
            command.command,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return GateResult(
            gate_id=command.gate_id,
            command=command.command,
            passed=False,
            exit_code=None,
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr),
        )
    except OSError as exc:
        return GateResult(
            gate_id=command.gate_id,
            command=command.command,
            passed=False,
            exit_code=None,
            stdout="",
            stderr=str(exc),
        )
    return GateResult(
        gate_id=command.gate_id,
        command=command.command,
        passed=completed.returncode == 0,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value

