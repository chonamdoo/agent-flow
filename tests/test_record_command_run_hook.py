"""실행 관측 hook을 **프로세스로** 구동한다.

함수만 부르는 테스트는 hook이 import 단계에서 죽어도 통과한다. `record-skill-read.py`가
실제로 그렇게 상수 하나를 잃고 모든 Read에서 죽었는데 전체 스위트가 통과했다.

관측 hook의 두 계약을 여기서 못박는다: **항상 exit 0**이고 **PostToolUse에만
등록된다.** 어기면 관측자가 판정자로 승격되어 사용자 도구를 통째로 막는다.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "scripts" / "hooks" / "record-command-run.py"
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.command_evidence import read_command_evidence


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".agent-flow").mkdir(parents=True)
    (root / ".agent-flow" / "kit.json").write_text("{}\n", encoding="utf-8")
    return root


def _invoke(root: Path, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(root),
        timeout=30,
    )


def _bash(root: Path, command: str, *, tool: str = "Bash", **extra) -> subprocess.CompletedProcess:
    payload = {"tool_name": tool, "cwd": str(root), "tool_input": {"command": command}}
    payload.update(extra)
    return _invoke(root, payload)


def _log(root: Path) -> list[dict]:
    path = root / ".agent-flow" / "commands-run.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_hook_records_a_command(tmp_path):
    """불변: hook이 실제로 실행돼 기록을 남긴다."""
    root = _project(tmp_path)
    result = _bash(root, "pytest -q tests")
    assert result.returncode == 0, result.stderr
    assert [entry["command"] for entry in _log(root)] == ["pytest -q tests"]


def test_hook_records_the_exit_code_when_the_host_reports_one(tmp_path):
    root = _project(tmp_path)
    _bash(root, "pytest -q", exit_code=1)
    assert _log(root)[0]["exit_code"] == 1


def test_missing_exit_code_is_null_not_zero(tmp_path):
    """반증: 없는 것과 0을 섞으면 '실패를 못 봤다'가 '성공했다'로 둔갑한다."""
    root = _project(tmp_path)
    _bash(root, "pytest -q")
    assert _log(root)[0]["exit_code"] is None


def test_boolean_is_not_an_exit_code(tmp_path):
    root = _project(tmp_path)
    _bash(root, "pytest -q", exit_code=True)
    assert _log(root)[0]["exit_code"] is None


@pytest.mark.parametrize("tool", ["bash", "shell", "run_terminal_cmd", "local_shell"])
def test_hook_accepts_host_specific_command_tool_names(tmp_path, tool):
    root = _project(tmp_path)
    _bash(root, "go test ./...", tool=tool)
    assert len(_log(root)) == 1


def test_hook_ignores_non_command_tools(tmp_path):
    root = _project(tmp_path)
    _bash(root, "pytest -q", tool="Read")
    assert _log(root) == []


def test_hook_ignores_a_payload_without_a_command(tmp_path):
    root = _project(tmp_path)
    _invoke(root, {"tool_name": "Bash", "cwd": str(root), "tool_input": {}})
    assert _log(root) == []


def test_hook_records_nothing_outside_an_agent_flow_project(tmp_path):
    outside = tmp_path / "plain"
    outside.mkdir()
    result = _invoke(
        outside, {"tool_name": "Bash", "cwd": str(outside), "tool_input": {"command": "ls"}}
    )
    assert result.returncode == 0
    assert not (outside / ".agent-flow" / "commands-run.jsonl").exists()


def test_hook_never_blocks_on_garbage_input(tmp_path):
    """불변: 관측 전용이다. 어떤 입력에도 exit 0이고 stderr를 더럽히지 않는다.

    이게 깨지면 PostToolUse에서도 host가 오류로 읽어 tool 결과를 오염시킨다.
    """
    root = _project(tmp_path)
    for payload in ["", "not json", "[]", "null", '{"tool_name": 3}', '{"tool_input": "x"}']:
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=payload,
            capture_output=True,
            text=True,
            cwd=str(root),
            timeout=30,
        )
        assert result.returncode == 0, (payload, result.stderr)
        assert result.stderr == "", (payload, result.stderr)


def test_hook_appends_instead_of_overwriting(tmp_path):
    """O_APPEND 계약. 병렬 agent가 서로의 기록을 덮으면 증거가 사라진다."""
    root = _project(tmp_path)
    _bash(root, "pytest -q")
    _bash(root, "npm test")
    assert [entry["command"] for entry in _log(root)] == ["pytest -q", "npm test"]


def test_evidence_reader_matches_on_token_boundaries(tmp_path):
    root = _project(tmp_path)
    _bash(root, "mypytest --all")
    evidence = read_command_evidence(root)
    assert evidence.available is True
    assert evidence.ran("pytest") is False
    assert evidence.ran("mypytest") is True


def test_evidence_reader_degrades_when_the_hook_is_not_installed(tmp_path):
    """불변: hook 미지원 host에서 관측 불가는 위반이 아니다. L2와 같은 계약이다."""
    root = _project(tmp_path)
    evidence = read_command_evidence(root)
    assert evidence.available is False
    assert evidence.runs == ()


def test_evidence_reader_windows_by_phase_entry_time(tmp_path):
    """이전 phase의 실행이 이번 phase의 증거가 되면 관측이 무의미해진다."""
    root = _project(tmp_path)
    _bash(root, "pytest -q")
    stale = read_command_evidence(root).runs[0].at
    assert read_command_evidence(root, since=stale + 1).runs == ()
    assert read_command_evidence(root, since=stale).runs != ()


def test_evidence_reader_reports_observed_failures(tmp_path):
    root = _project(tmp_path)
    _bash(root, "pytest -q", exit_code=1)
    evidence = read_command_evidence(root)
    assert evidence.failed("pytest") is True
    assert evidence.failed("gradlew") is False


def test_evidence_reader_does_not_call_a_missing_exit_code_a_failure(tmp_path):
    root = _project(tmp_path)
    _bash(root, "pytest -q")
    assert read_command_evidence(root).failed("pytest") is False
