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
