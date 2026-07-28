from __future__ import annotations

import subprocess
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

# 여러 운영체제의 로컬 절대 경로를 같은 기준으로 가려 artifact에 개발자 경로가 남지 않게 한다.
_LOCAL_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![\w.-])"
    r"(?:/Users/|/home/|/private/var/|/workspace/|/tmp/|/var/|/opt/|/mnt/|[A-Za-z]:[\\/])"
    r"[^\s\"'<>|,;)\]}]*"
)


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
    timed_out: bool = False


def run_gate(command: GateCommand, *, cwd: Path, timeout_s: int = 600) -> GateResult:
    executable_command = command.command
    recorded_command = _recorded_gate_command(executable_command, cwd)
    try:
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
            stderr=_text(exc.stderr) or f"gate timed out after {timeout_s}s",
            required=command.required,
            timed_out=True,
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


def gate_results_timed_out(results: list[GateResult]) -> bool:
    return any(result.timed_out for result in results)


def run_gates(commands: list[GateCommand], *, cwd: Path, timeout_s: int = 600) -> list[GateResult]:
    return [run_gate(command, cwd=cwd, timeout_s=timeout_s) for command in commands]


def relativize_local_path(value: str, base: Path) -> str:
    """절대 경로 하나를 ``base`` 기준 상대 경로로 바꾼다.

    gate command와 gate 출력이 같은 규칙을 쓰도록 두 경로가 이 함수만 부른다.
    """
    if Path(value).is_absolute():
        try:
            return os.path.relpath(value, base.resolve())
        except ValueError:
            # Windows에서 checkout과 다른 드라이브면 상대 경로 자체가 없다.
            # 여기서 예외를 올리면 gate 실행 뒤 `write_gate_results`가 죽어
            # 결과 artifact가 남지 않고 run이 멈춘다.
            return _strip_path_anchor(value)
    if _foreign_absolute(value):
        # POSIX에서 만난 `D:\...`(또는 그 반대). 이 플랫폼에는 기준점이 없어
        # relpath가 무의미한 값을 만든다. 검사기가 잡는 절대 경로 표기만 없앤다.
        return _strip_path_anchor(value)
    return value


def _foreign_absolute(value: str) -> bool:
    return PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute()


def _strip_path_anchor(value: str) -> str:
    for flavour in (PureWindowsPath, PurePosixPath):
        anchor = flavour(value).anchor
        if anchor:
            return value[len(anchor) :] or value
    return value


def relativize_local_paths(text: str, base: Path) -> str:
    """자유 텍스트(gate stdout/stderr)에 실린 로컬 절대 경로를 상대화한다."""
    return _LOCAL_ABSOLUTE_PATH_RE.sub(lambda match: _relativized_match(match.group(0), base), text)


def _relativized_match(raw: str, base: Path) -> str:
    # 경로 뒤에 붙은 문장 부호(`...foo.py:`, `...tmpdir.`)는 경로의 일부가 아니다.
    trimmed = raw.rstrip(".:")
    return relativize_local_path(trimmed, base) + raw[len(trimmed) :]


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




def _recorded_gate_command(command: tuple[str, ...], cwd: Path) -> tuple[str, ...]:
    return tuple(relativize_local_path(part, cwd) for part in command)


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
