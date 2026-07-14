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
    recorded_command = _recorded_gate_command(command.command, cwd)
    try:
        executable_command = _resolve_gate_command(command.command, cwd)
        recorded_command = _recorded_gate_command(executable_command, cwd)
        completed = subprocess.run(
            executable_command,
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
            command=recorded_command,
            passed=False,
            exit_code=None,
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr),
            required=command.required,
        )
    except OSError as exc:
        return GateResult(
            gate_id=command.gate_id,
            command=recorded_command,
            passed=False,
            exit_code=None,
            stdout="",
            stderr=str(exc),
            required=command.required,
        )
    return GateResult(
        gate_id=command.gate_id,
        command=recorded_command,
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
    python_paths: list[Path] = []
    runtime_path = _installed_python_runtime_path(cwd)
    if runtime_path is not None:
        python_paths.append(runtime_path)
    src_path = cwd / "src"
    if src_path.is_dir():
        python_paths.append(src_path)
    kit_path = Path(__file__).resolve().parents[2]
    python_paths.append(kit_path)
    current = env.get("PYTHONPATH")
    if current:
        python_paths.extend(Path(item) for item in current.split(os.pathsep) if item)
    env["PYTHONPATH"] = os.pathsep.join(str(path) for path in dict.fromkeys(python_paths))
    return env


def _installed_python_runtime_path(cwd: Path) -> Path | None:
    for root in _candidate_agent_flow_roots(cwd):
        runtime_path = root / ".agent-flow" / "runtime" / "python"
        if (runtime_path / "agent_flow" / "__init__.py").is_file():
            return runtime_path
    return None


def _resolve_gate_command(command: tuple[str, ...], cwd: Path) -> tuple[str, ...]:
    if command and command[0] == "agent-flow":
        launcher = os.environ.get("AGENT_FLOW_PROJECT_LAUNCHER")
        if not launcher or not Path(launcher).is_absolute():
            raise OSError("project-local agent-flow launcher is not pinned")
        return (launcher, *command[1:])
    return command


def _recorded_gate_command(command: tuple[str, ...], cwd: Path) -> tuple[str, ...]:
    recorded: list[str] = []
    for part in command:
        if Path(part).is_absolute():
            recorded.append(os.path.relpath(part, cwd.resolve()))
        else:
            recorded.append(part)
    return tuple(recorded)


def _candidate_agent_flow_roots(cwd: Path) -> list[Path]:
    resolved = cwd.resolve()
    roots: list[Path] = []
    if (resolved / ".agent-flow").is_dir():
        roots.append(resolved)
    parts = resolved.parts
    if ".agent-flow" in parts:
        marker_index = parts.index(".agent-flow")
        roots.append(Path(*parts[:marker_index]) if marker_index else Path("/"))
    roots.extend(resolved.parents)
    return list(dict.fromkeys(roots))


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
