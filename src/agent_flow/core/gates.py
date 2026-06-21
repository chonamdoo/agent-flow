from __future__ import annotations

import subprocess
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GateCommand:
    gate_id: str
    command: tuple[str, ...]
    required: bool = True


@dataclass(frozen=True)
class GateResult:
    gate_id: str
    command: tuple[str, ...]
    passed: bool
    exit_code: int | None
    stdout: str
    stderr: str
    required: bool = True


def run_gate(command: GateCommand, *, cwd: Path, timeout_s: int = 600) -> GateResult:
    try:
        completed = subprocess.run(
            command.command,
            cwd=cwd,
            env=_gate_environment(cwd),
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
            required=command.required,
        )
    except OSError as exc:
        return GateResult(
            gate_id=command.gate_id,
            command=command.command,
            passed=False,
            exit_code=None,
            stdout="",
            stderr=str(exc),
            required=command.required,
        )
    return GateResult(
        gate_id=command.gate_id,
        command=command.command,
        passed=completed.returncode == 0,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        required=command.required,
    )


def run_gates(commands: list[GateCommand], *, cwd: Path, timeout_s: int = 600) -> list[GateResult]:
    return [run_gate(command, cwd=cwd, timeout_s=timeout_s) for command in commands]


def _gate_environment(cwd: Path) -> dict[str, str]:
    env = os.environ.copy()
    src_path = cwd / "src"
    if src_path.is_dir():
        current = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(src_path) if not current else f"{src_path}{os.pathsep}{current}"
    return env


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
