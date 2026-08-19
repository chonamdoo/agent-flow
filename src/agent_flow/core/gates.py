from __future__ import annotations

import subprocess
from collections.abc import Callable
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


# gate 하나가 이만큼을 넘기면 판정 불가로 기록한다. profile이 `gates[].timeout_s`로
# 이 값을 올리고, 호출자가 `--timeout`으로 명시하면 그것이 둘 다를 이긴다.
DEFAULT_GATE_TIMEOUT_S = 600


@dataclass(frozen=True)
class GateCommand:
    gate_id: str
    command: tuple[str, ...]
    required: bool = True
    # profile이 선언한 이 gate만의 상한. `None`이면 `DEFAULT_GATE_TIMEOUT_S`.
    # 하나의 flat 기본값은 두 방향으로 틀린다 — gradle/xcodebuild는 600s에 걸려
    # timeout(=판정 불가)이 되고, ruff는 600s를 기다릴 이유가 없다. timeout은
    # 실패가 아니라 판정 불가로 기록되므로(`core/artifacts.py`), 짧은 기본값은
    # "빌드가 깨졌다"가 아니라 "검증이 끊겼다"를 만든다.
    timeout_s: int | None = None


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


def run_gate(command: GateCommand, *, cwd: Path, timeout_s: int | None = None) -> GateResult:
    # 우선순위: 호출자가 명시한 값 > profile 선언 > 기본값. `or`로 접으면 명시한
    # 상한이 선언에 먹힌다. 그러면 `--timeout`을 낮춰도 실제 상한은 그대로여서,
    # 그 플래그로 총예산을 계산하는 node wrapper의 예산이 실제보다 작아진다
    # (`bin/agent-flow-kit.mjs`의 `relayTimeoutForSubcommand`).
    if timeout_s is None:
        timeout_s = command.timeout_s or DEFAULT_GATE_TIMEOUT_S
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


def run_gates(
    commands: list[GateCommand],
    *,
    cwd: Path,
    timeout_s: int | None = None,
    on_start: Callable[[GateCommand, int, int], None] | None = None,
) -> list[GateResult]:
    """gate를 순차 실행한다. `on_start`는 각 gate 시작 직전에 (gate, 순번, 총수)로 불린다.

    출력을 여기서 찍지 않는 이유: gate 실행은 core이고 표시는 호출자의 몫이다.
    직접 print하면 테스트가 실행마다 출력을 뒤집어쓰고, CLI와 runner가 서로 다른
    형식을 쓸 수 없게 된다. `subprocess.run(capture_output=True)`이라 이 콜백이
    없으면 긴 gate가 도는 동안 관측 가능한 신호가 0이다.
    """
    results: list[GateResult] = []
    total = len(commands)
    for index, command in enumerate(commands, start=1):
        if on_start is not None:
            on_start(command, index, total)
        results.append(run_gate(command, cwd=cwd, timeout_s=timeout_s))
    return results


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
