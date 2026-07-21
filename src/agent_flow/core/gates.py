from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
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
    executed_at: str | None = None
    reused: bool = False
    reused_at: str | None = None


def run_gate(command: GateCommand, *, cwd: Path, timeout_s: int = 600) -> GateResult:
    executed_at = datetime.now(timezone.utc).isoformat()
    recorded_command = _recorded_gate_command(command.command, cwd)
    try:
        executable_command = _resolve_gate_command(command.command, cwd)
        environment = _gate_environment(cwd)
        executable_command, environment = _apply_gradle_gate_policy(
            executable_command, cwd, environment
        )
        recorded_command = _recorded_gate_command(executable_command, cwd)
        completed = subprocess.run(
            executable_command,
            cwd=cwd,
            env=environment,
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
            executed_at=executed_at,
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
            executed_at=executed_at,
        )
    return GateResult(
        gate_id=command.gate_id,
        command=recorded_command,
        passed=completed.returncode == 0,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        required=command.required,
        executed_at=executed_at,
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


_GRADLE_EXECUTABLES = {"gradle", "gradlew", "gradlew.bat"}
_SHELL_EXECUTABLES = {"sh", "bash", "dash", "zsh", "ksh"}


def _argv_is_gradle(command: tuple[str, ...]) -> bool:
    return bool(command) and Path(command[0]).name.lower() in _GRADLE_EXECUTABLES


def _shell_gradle_script_index(command: tuple[str, ...]) -> int | None:
    if len(command) < 3 or Path(command[0]).name.lower() not in _SHELL_EXECUTABLES:
        return None
    for index in range(1, len(command) - 1):
        if command[index] != "-c":
            continue
        tokens = _script_tokens(command[index + 1])
        if tokens and any(Path(token).name.lower() in _GRADLE_EXECUTABLES for token in tokens):
            return index + 1
    return None


def _script_tokens(script: str) -> list[str] | None:
    try:
        return shlex.split(script)
    except ValueError:
        return None


def _apply_gradle_gate_policy(
    command: tuple[str, ...],
    cwd: Path,
    env: dict[str, str],
) -> tuple[tuple[str, ...], dict[str, str]]:
    argv_gradle = _argv_is_gradle(command)
    script_index = None if argv_gradle else _shell_gradle_script_index(command)
    if not argv_gradle and script_index is None:
        return command, env
    # Gradle gates must never touch the developer's shared caches or leave a
    # daemon alive, so pin GRADLE_USER_HOME / the Kotlin daemon under the pinned
    # workspace and force --no-daemon — matching the Node sandboxed-gate seam.
    gate_runtime = cwd / ".agent-flow" / "gate-runtime"
    gradle_home = gate_runtime / "gradle-home"
    kotlin_daemon = gate_runtime / "kotlin-daemon"
    gradle_home.mkdir(parents=True, exist_ok=True)
    kotlin_daemon.mkdir(parents=True, exist_ok=True)
    managed = dict(env)
    managed.pop("GRADLE_OPTS", None)
    managed["GRADLE_USER_HOME"] = str(gradle_home)
    managed["KOTLIN_DAEMON_RUNFILES_PATH"] = str(kotlin_daemon)
    if argv_gradle:
        if "--daemon" in command:
            raise OSError("blocked: managed Gradle gates do not allow --daemon")
        if "--no-daemon" not in command:
            command = (*command, "--no-daemon")
    else:
        script = command[script_index]
        tokens = _script_tokens(script) or []
        if "--daemon" in tokens:
            raise OSError("blocked: managed Gradle gates do not allow --daemon")
        if "--no-daemon" not in tokens:
            script = f"{script} --no-daemon"
        command = (*command[:script_index], script, *command[script_index + 1:])
    return command, managed


def _installed_python_runtime_path(cwd: Path) -> Path | None:
    for root in _candidate_agent_flow_roots(cwd):
        runtime_path = root / ".agent-flow" / "runtime" / "python"
        if (runtime_path / "agent_flow" / "__init__.py").is_file():
            return runtime_path
    return None


def _resolve_gate_command(command: tuple[str, ...], cwd: Path) -> tuple[str, ...]:
    launcher = os.environ.get("AGENT_FLOW_PROJECT_LAUNCHER")
    if command and command[0] == "agent-flow":
        if not launcher or not Path(launcher).is_absolute():
            raise OSError("project-local agent-flow launcher is not pinned")
        return (launcher, *command[1:])
    if (
        len(command) >= 3
        and command[1:3] == ("-m", "agent_flow.core.architecture_lint")
        and launcher
        and Path(launcher).is_absolute()
    ):
        return (launcher, "architecture-lint", *command[3:])
    if command and launcher and Path(launcher).is_absolute():
        return (launcher, "gate", "--", *command)
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
