"""재현 테스트 관측 게이트.

이 층이 잡는 것은 **"아예 안 돌렸다"** 하나뿐이다. `pytest -k trivial`도 exit 0이고
`assert False` 한 줄도 빨간 테스트라, 가짜 테스트는 관측으로 갈 수 없다. 그래서
테스트도 그 하나만 반증한다 — 실행 기록이 없으면 red/fix phase가 막히는가.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.command_evidence import (
    COMMANDS_RUN_LOG,
    FALLBACK_TEST_TOKENS,
    TEST_RUN_EVIDENCE_MARKER,
    CommandRun,
    CommandRunEvidence,
    agent_run_spec_approvals,
    missing_test_evidence_markers,
    resolve_test_command_tokens,
)

PYTHON_PROFILE = {"gates": [{"id": "test", "command": ["pytest", "-q"]}]}
ANDROID_PROFILE = {"gates": [{"id": "test", "command": ["./gradlew", "test"]}]}

GATE = "## Completion Gate\n\nregression-test: tests/test_x.py::test_bug\nred-observed: 1\n"


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".agent-flow").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _observe(root: Path, command: str, exit_code=None, at=None) -> None:
    path = root / COMMANDS_RUN_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "command": command,
                    "exit_code": exit_code,
                    "cwd": str(root),
                    "at": time.time() if at is None else at,
                }
            )
            + "\n"
        )


def test_no_observation_log_degrades_to_self_report(tmp_path):
    """불변: hook 미지원 host에서 관측 불가는 위반이 아니다."""
    root = _project(tmp_path)
    assert missing_test_evidence_markers(root, "red", GATE, profile=PYTHON_PROFILE) == [
        TEST_RUN_EVIDENCE_MARKER
    ]
    assert (
        missing_test_evidence_markers(
            root, "red", GATE + "test-run-evidence: unavailable\n", profile=PYTHON_PROFILE
        )
        == []
    )


def test_observed_but_no_test_command_is_blocked(tmp_path):
    """반증: 마커만 채우고 테스트를 안 돌린 red가 통과하면 F1이 그대로 남는다."""
    root = _project(tmp_path)
    _observe(root, "git status", exit_code=0)
    missing = missing_test_evidence_markers(root, "red", GATE, profile=PYTHON_PROFILE)
    assert any("no test command was observed" in item for item in missing)


def test_self_report_cannot_override_an_available_observation(tmp_path):
    """불변: 관측이 가능한데 `unavailable`이라고 쓰는 것은 아무 효력이 없다."""
    root = _project(tmp_path)
    _observe(root, "git status", exit_code=0)
    text = GATE + "test-run-evidence: unavailable\n"
    assert missing_test_evidence_markers(root, "red", text, profile=PYTHON_PROFILE) != []


def test_observed_failing_test_command_passes(tmp_path):
    root = _project(tmp_path)
    _observe(root, "pytest -q tests/test_x.py", exit_code=1)
    assert missing_test_evidence_markers(root, "red", GATE, profile=PYTHON_PROFILE) == []


def test_red_with_only_passing_test_runs_is_blocked(tmp_path):
    """반증: 처음부터 통과하는 테스트는 red가 아니다."""
    root = _project(tmp_path)
    _observe(root, "pytest -q", exit_code=0)
    missing = missing_test_evidence_markers(root, "red", GATE, profile=PYTHON_PROFILE)
    assert any(item.startswith("red-observed:") for item in missing)


def test_red_passes_when_the_host_reports_no_exit_codes(tmp_path):
    """반증: exit code를 안 실어 보내는 host를 위반으로 들면 red가 통째로 막힌다."""
    root = _project(tmp_path)
    _observe(root, "pytest -q")
    assert missing_test_evidence_markers(root, "red", GATE, profile=PYTHON_PROFILE) == []


def test_implement_fix_with_only_passing_test_runs_is_blocked(tmp_path):
    """반증: 고친 뒤 한 번만 돌리면 회귀 테스트가 그 버그를 잡는지 아무도 안 봤다."""
    root = _project(tmp_path)
    _observe(root, "pytest -q", exit_code=0)
    missing = missing_test_evidence_markers(root, "implement-fix", GATE, profile=PYTHON_PROFILE)
    assert any(item.startswith("red-observed:") for item in missing)


def test_implement_fix_passes_with_a_failing_run_before_the_fix(tmp_path):
    root = _project(tmp_path)
    _observe(root, "pytest -q tests/test_bug.py", exit_code=1, at=100.0)
    _observe(root, "pytest -q tests/test_bug.py", exit_code=0, at=101.0)
    assert (
        missing_test_evidence_markers(root, "implement-fix", GATE, profile=PYTHON_PROFILE) == []
    )


def test_evidence_is_windowed_to_the_phase(tmp_path):
    """반증: 지난 phase의 실행이 이번 phase의 증거가 되면 관측이 무의미하다."""
    root = _project(tmp_path)
    _observe(root, "pytest -q", exit_code=1, at=100.0)
    missing = missing_test_evidence_markers(root, "red", GATE, profile=PYTHON_PROFILE, since=200.0)
    assert any("no test command was observed" in item for item in missing)


def test_other_phases_are_not_checked(tmp_path):
    root = _project(tmp_path)
    _observe(root, "git status", exit_code=0)
    assert missing_test_evidence_markers(root, "green", GATE, profile=PYTHON_PROFILE) == []


def test_gradle_gate_requires_every_token(tmp_path):
    """반증: `./gradlew assembleDebug`가 test gate 증거로 인정되면 안 된다."""
    root = _project(tmp_path)
    _observe(root, "./gradlew assembleDevDebug", exit_code=0)
    missing = missing_test_evidence_markers(root, "red", GATE, profile=ANDROID_PROFILE)
    assert any("no test command was observed" in item for item in missing)
    _observe(root, "./gradlew test --tests X", exit_code=1)
    assert missing_test_evidence_markers(root, "red", GATE, profile=ANDROID_PROFILE) == []


def test_profile_without_a_test_gate_falls_back(tmp_path):
    """`generic`에는 test gate가 0개다. 그래도 '아무것도 안 돌렸다'는 잡아야 한다."""
    assert resolve_test_command_tokens({"gates": []}) == tuple((t,) for t in FALLBACK_TEST_TOKENS)
    root = _project(tmp_path)
    _observe(root, "echo hi", exit_code=0)
    assert missing_test_evidence_markers(root, "red", GATE, profile={"gates": []}) != []
    _observe(root, "cargo test", exit_code=1)
    assert missing_test_evidence_markers(root, "red", GATE, profile={"gates": []}) == []


@pytest.mark.parametrize(
    "workflow,phase_id",
    [("full-feature", "red"), ("bugfix", "implement-fix")],
)
@pytest.mark.parametrize("copy", ["workflows", "src/agent_flow/workflows"])
def test_workflows_require_the_regression_markers(workflow, phase_id, copy):
    """`bugfix.yaml`에는 red phase도 required_markers도 0개였다."""
    data = yaml.safe_load((REPO / copy / f"{workflow}.yaml").read_text(encoding="utf-8"))
    phase = next(item for item in data["phases"] if item["id"] == phase_id)
    markers = phase.get("required_markers") or []
    assert "regression-test:" in markers
    assert "red-observed:" in markers
    assert TEST_RUN_EVIDENCE_MARKER in markers


def _spec_evidence(command: str) -> CommandRunEvidence:
    return CommandRunEvidence(
        available=True,
        runs=(CommandRun(command=command, exit_code=0, at=1.0, cwd=""),),
    )


@pytest.mark.parametrize(
    "command",
    [
        "agent-flow spec confirm --help",
        "agent-flow spec approve -h",
        "python3 -m agent_flow.cli spec confirm --help",
        # 출력을 돌리거나 종료 코드를 무시하는 확인 명령. 셸 연산자가 보인다고
        # 판단을 포기하면 이런 한 번으로 런이 영구히 막힌다.
        "agent-flow spec confirm --help 2>&1",
        "agent-flow spec confirm --help | cat",
        "agent-flow spec confirm --help || true",
        "agent-flow spec confirm --help > /tmp/spec-help.txt 2>&1",
        # 래퍼로 부른 도움말도 승인 기록을 만들지 않는다.
        "agent-flow-kit spec confirm --help",
        "node bin/agent-flow-kit.mjs spec confirm --help",
    ],
)
def test_help_invocation_is_not_an_agent_run_approval(command: str):
    """반증: 도움말만 봐도 위반이면 명령 형태를 확인한 것만으로 그 런이 끝난다.

    증거 창은 런 시작 시각부터라 한 번 기록되면 같은 런에서는 풀 방법이 없다.
    실제로 사용자가 런을 새로 파야 했다.
    """
    assert agent_run_spec_approvals(_spec_evidence(command)) == ()


@pytest.mark.parametrize(
    "command",
    [
        "agent-flow spec confirm --run-dir .agent-flow/runs/default/r1 --artifact design.md",
        "python3 -m agent_flow.cli spec approve --run-dir .agent-flow/runs/default/r1 --spec-id SPEC-1",
    ],
)
def test_real_approval_in_the_agent_shell_is_still_caught(command: str):
    """짝 테스트. 도움말 예외가 승인 자체까지 눈감으면 안 된다."""
    assert len(agent_run_spec_approvals(_spec_evidence(command))) == 1


@pytest.mark.parametrize(
    "command",
    [
        "agent-flow-kit spec confirm --run-dir .agent-flow/runs/default/r1 --artifact design.md",
        "node bin/agent-flow-kit.mjs spec confirm --run-dir r --artifact design.md",
        "node /opt/kit/bin/agent-flow-kit.mjs spec approve --run-dir r --spec-id SPEC-1",
        "npx agent-flow-kit spec confirm --run-dir r --artifact design.md",
        "agent-flow-kit spec confirm --run-dir r --artifact design.md | cat",
    ],
)
def test_wrapper_approval_in_the_agent_shell_is_caught(command: str):
    """반증: 래퍼는 stdin을 물려주므로 실제 승인 파일을 만든다.

    `agent-flow`와 `agent_flow.cli`만 훑으면 agent가 만든 승인이 사용자 승인으로
    통과한다.
    """
    assert len(agent_run_spec_approvals(_spec_evidence(command))) == 1


@pytest.mark.parametrize(
    "command",
    [
        # `--help`가 셸의 $0로 먹히고 따옴표 안의 승인은 그대로 실행된다.
        "sh -c 'agent-flow spec confirm --run-dir r --artifact design.md' --help",
        # 첫 줄이 승인을 실행하고 둘째 줄은 장식이다. 로그에는 원문이 그대로 남는다.
        "agent-flow spec confirm --run-dir r --artifact design.md\nagent-flow spec confirm --help",
        "agent-flow spec confirm --run-dir r --artifact design.md; echo --help",
        # 도움말 단위를 앞에 붙여도 뒤 단위가 승인을 실행한다.
        "agent-flow spec confirm --help && agent-flow spec confirm --run-dir r --artifact design.md",
        "echo $(agent-flow spec confirm --run-dir r --artifact design.md) --help",
        "`agent-flow spec confirm --run-dir r --artifact design.md` --help",
    ],
)
def test_inert_flag_cannot_launder_a_real_approval(command: str):
    """반증: 토큰만 훑으면 승인을 실제로 실행하는 명령까지 면제된다."""
    assert len(agent_run_spec_approvals(_spec_evidence(command))) == 1
