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
CI_PYTHON_PROFILE = {
    "gates": [
        {"id": "test", "command": ["pytest", "-q"], "required": True,
         "execution": "ci", "ci_check": "pytest"}
    ]
}
ANDROID_PROFILE = {"gates": [{"id": "test", "command": ["./gradlew", "test"]}]}

GATE = "## Completion Gate\n\nregression-test: tests/test_x.py::test_bug\nred-observed: 1\n"


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".agent-flow").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _observe(root: Path, command: str, exit_code=None, at=None, code_baseline="") -> None:
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
                    "code_baseline": code_baseline,
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


def test_a_sibling_checkout_log_does_not_trap_an_unobserved_session(tmp_path):
    """반증: 로그 파일 존재만으로 관측 가능으로 보면 탈출구가 0이 된다.

    로그는 저장소가 공유한다. 형제 worktree의 세션이 파일을 만들어 두면 hook이
    실리지 않은 세션도 `available=True`가 되어, 테스트를 실제로 돌려도 기록이 없고
    `unavailable`로 신고해도 그 가지에 닿지 못해 phase가 영구히 막혔다.
    """
    root = _project(tmp_path)
    sibling = tmp_path.parent / "sibling"
    sibling.mkdir(exist_ok=True)
    path = root / COMMANDS_RUN_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"command": "pytest -q", "exit_code": 1, "cwd": str(sibling), "at": time.time()}
        )
        + "\n",
        encoding="utf-8",
    )

    assert missing_test_evidence_markers(
        root, "red", GATE, profile=PYTHON_PROFILE, cwd_root=root
    ) == [TEST_RUN_EVIDENCE_MARKER]
    assert (
        missing_test_evidence_markers(
            root,
            "red",
            GATE + "test-run-evidence: unavailable\n",
            profile=PYTHON_PROFILE,
            cwd_root=root,
        )
        == []
    )


def test_an_observed_checkout_still_blocks_a_phase_with_no_test_run(tmp_path):
    """불변: hook이 살아 있는 checkout에서 이전 phase의 실행을 재활용할 수는 없다.

    탈출구는 "이 checkout이 한 번도 관측되지 않았다"에만 열린다. 기록이 있는데
    이번 창에만 없으면 hook은 살아 있는 것이므로 그대로 막힌다.
    """
    root = _project(tmp_path)
    _observe(root, "pytest -q", exit_code=1, at=100.0)

    missing = missing_test_evidence_markers(
        root,
        "red",
        GATE + "test-run-evidence: unavailable\n",
        profile=PYTHON_PROFILE,
        since=200.0,
        cwd_root=root,
    )

    assert any("no test command was observed" in item for item in missing)


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


@pytest.mark.parametrize("phase_id", ("red", "implement-fix", "implement", "fix-loop"))
@pytest.mark.parametrize("self_report", ("verified", "unavailable"))
def test_red_requires_a_reported_exit_code(tmp_path, phase_id, self_report):
    root = _project(tmp_path)
    _observe(root, "pytest -q")
    missing = missing_test_evidence_markers(
        root,
        phase_id,
        GATE + f"test-run-evidence: {self_report}\n",
        profile=PYTHON_PROFILE,
    )
    assert any(item.startswith("red-observed:") for item in missing)
    assert any("run-command" in item for item in missing)


def test_runner_result_satisfies_red_beside_unknown_host_records(tmp_path):
    """반증: exit code 없는 host 기록만으로 red가 통과하면 자기신고 RED가 남고, runner가
    직접 관측한 실패가 같은 창에 있어도 막히면 Claude·Codex 세션의 탈출구가 없다.
    """
    from agent_flow.core.command_runner import run_observed_command

    root = _project(tmp_path)
    _observe(root, "pytest tests/test_x.py::test_bug")
    _observe(root, "agent-flow run-command -- pytest tests/test_x.py::test_bug")
    unknown_only = missing_test_evidence_markers(
        root, "red", GATE, profile=PYTHON_PROFILE, cwd_root=root,
    )
    assert any(item.startswith("red-observed:") for item in unknown_only)

    fake_pytest = root / "bin" / "pytest"
    fake_pytest.parent.mkdir()
    fake_pytest.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_pytest.chmod(0o755)
    assert run_observed_command(
        [str(fake_pytest), "tests/test_x.py::test_bug"],
        project_root=root, checkout_root=root, cwd=root,
    ) == 1

    assert missing_test_evidence_markers(
        root, "red", GATE, profile=PYTHON_PROFILE, cwd_root=root,
    ) == []


def test_unknown_only_red_reference_asks_for_an_observed_green_rerun(tmp_path):
    """반증: RED가 이미 참조로 게시됐는데 새 실패를 요구하면 고친 코드로는 낼 수 없는
    증거를 요구하게 된다. 남은 것은 같은 회귀가 초록으로 끝나는 관측뿐이다.
    """
    root = _project(tmp_path)
    _observe(root, "pytest tests/test_x.py::test_bug")
    missing = missing_test_evidence_markers(
        root, "implement", GATE + f"red-reference: {'0' * 64}\n", profile=PYTHON_PROFILE,
    )
    assert len(missing) == 1
    assert missing[0].startswith("test-run-evidence: verified")
    assert "run-command" in missing[0] and "ending green" in missing[0]


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
    _observe(root, "./gradlew assembleDebug", exit_code=0)
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


def test_behavior_preserving_requires_reason_and_related_green(tmp_path):
    root = _project(tmp_path)
    selector = "tests/test_x.py::test_bug"
    text = GATE + "change-kind: behavior-preserving\nbehavior-preserving-reason: only the adapter boundary moves\n"
    _observe(root, "pytest tests/test_other.py::test_other", 0, at=1)
    assert missing_test_evidence_markers(root, "implement", text, profile=PYTHON_PROFILE) == [
        "test-run-evidence: verified (the related test must end green)"
    ]
    _observe(root, f"pytest {selector}", 0, at=2)
    assert missing_test_evidence_markers(root, "implement", text, profile=PYTHON_PROFILE) == []
    assert missing_test_evidence_markers(
        root, "implement", GATE + "change-kind: behavior-preserving\n", profile=PYTHON_PROFILE
    ) == ["behavior-preserving-reason: <unchanged behavior and affected boundary>"]
    _observe(root, f"pytest {selector}", 1, at=3)
    assert missing_test_evidence_markers(root, "implement", text, profile=PYTHON_PROFILE) == [
        "test-run-evidence: verified (the related test must end green)"
    ]


@pytest.mark.parametrize("prefix", [
    "uv run pytest",
    "uv run --locked pytest",
    "uv run --locked python -m pytest",
    "uv run --frozen python -m pytest",
])
def test_uv_run_pytest_counts_as_the_related_test_run(tmp_path, prefix):
    """반증: `uv run`을 벗기지 못하면 uv 사용자가 정상으로 돌린 테스트가
    "테스트 미실행"으로 막히는데, 이를 단언하는 테스트가 없어 전체 스위트가 green이었다(#285).
    """
    root = _project(tmp_path)
    text = GATE + "change-kind: behavior-preserving\nbehavior-preserving-reason: only the adapter boundary moves\n"
    _observe(root, f"{prefix} tests/test_x.py::test_bug", 0)

    assert missing_test_evidence_markers(root, "implement", text, profile=PYTHON_PROFILE) == []


def test_behavior_preserving_ci_only_test_is_deferred_without_command_evidence(tmp_path):
    root = _project(tmp_path)
    text = (
        GATE
        + "change-kind: behavior-preserving\n"
        + "behavior-preserving-reason: only the adapter boundary moves\n"
    )

    assert missing_test_evidence_markers(
        root, "implement", text + "test-run-evidence: unavailable\n",
        profile=CI_PYTHON_PROFILE,
    ) == []
    assert missing_test_evidence_markers(
        root, "implement", text + "test-run-evidence: verified\n",
        profile=CI_PYTHON_PROFILE,
    ) == ["test-run-evidence: unavailable (CI-only test deferred to PR check)"]
    assert missing_test_evidence_markers(
        root, "implement", text + "test-run-evidence: unavailable\n",
        profile=PYTHON_PROFILE,
    ) == ["test-run-evidence: verified (the related test must end green)"]


def test_ci_deferral_respects_omitted_required_default_but_not_optional_gate(tmp_path):
    root = _project(tmp_path)
    _observe(root, "git status", 0)
    text = (
        GATE
        + "change-kind: behavior-preserving\n"
        + "behavior-preserving-reason: only the adapter boundary moves\n"
        + "test-run-evidence: unavailable\n"
    )
    gate = {"id": "test", "command": ["pytest"], "execution": "ci", "ci_check": "pytest"}

    assert missing_test_evidence_markers(
        root, "implement", text, profile={"gates": [gate]},
    ) == []
    assert missing_test_evidence_markers(
        root, "implement", text, profile={"gates": [{**gate, "required": False}]},
    ) == ["test-run-evidence: verified (the related test must end green)"]


def test_behavior_preserving_ci_deferral_requires_concrete_regression_and_required_check(tmp_path):
    root = _project(tmp_path)
    text = (
        "change-kind: behavior-preserving\n"
        "behavior-preserving-reason: only the adapter boundary moves\n"
        "test-run-evidence: unavailable\n"
    )
    assert missing_test_evidence_markers(
        root, "implement", "## Completion Gate\n" + text, profile=CI_PYTHON_PROFILE,
    ) == ["regression-test: <concrete test id for the unchanged contract>"]
    for gates in (
        [{"id": "test", "command": ["pytest"], "required": True, "execution": "ci"}],
        [*CI_PYTHON_PROFILE["gates"], *PYTHON_PROFILE["gates"]],
    ):
        assert missing_test_evidence_markers(
            root, "implement", GATE + text,
            profile={"gates": gates},
        ) == ["test-run-evidence: verified (the related test must end green)"]


def test_behavior_preserving_ci_deferral_does_not_apply_to_other_phases_or_failed_run(tmp_path):
    root = _project(tmp_path)
    text = (
        GATE
        + "change-kind: behavior-preserving\n"
        + "behavior-preserving-reason: only the adapter boundary moves\n"
        + "test-run-evidence: unavailable\n"
    )
    assert missing_test_evidence_markers(
        root, "implement-fix", text, profile=CI_PYTHON_PROFILE,
    ) == ["test-run-evidence: verified (the related test must end green)"]
    _observe(root, "pytest tests/test_x.py::test_bug", 1)
    assert missing_test_evidence_markers(
        root, "implement", text, profile=CI_PYTHON_PROFILE,
    ) == ["test-run-evidence: verified (the related test must end green)"]


def test_undeclared_and_feature_changes_still_require_red(tmp_path):
    root = _project(tmp_path)
    _observe(root, "pytest tests/test_x.py::test_bug", 0)
    for change in ("", "change-kind: feature\n", "change-kind: bugfix\n"):
        missing = missing_test_evidence_markers(root, "implement", GATE + change, profile=PYTHON_PROFILE)
        assert any(item.startswith("red-observed:") for item in missing)


def test_red_reference_requires_same_regression_and_entry_code_baseline(tmp_path):
    import subprocess
    from agent_flow.core.command_evidence import test_code_baseline
    from agent_flow.runner import publish_red_reference

    root = _project(tmp_path)
    (root / ".agent-flow" / "kit.json").write_text("{}\n", encoding="utf-8")
    (root / ".gitignore").write_text(".agent-flow/\n", encoding="utf-8")
    code = root / "logic.py"
    code.write_text("value = 0\n", encoding="utf-8")
    for args in (
        ("init", "-b", "main"), ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"), ("add", "."), ("commit", "-m", "baseline"),
    ):
        subprocess.run(("git", *args), cwd=root, check=True, capture_output=True)
    run_dir = root / ".agent-flow" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    baseline = test_code_baseline(root)
    _observe(root, "pytest tests/test_x.py::test_bug", 1, at=1)
    assert publish_red_reference(
        root, run_dir, evidence_root=root, text=GATE, profile=PYTHON_PROFILE,
    ) is None
    subprocess.run(
        (sys.executable, str(REPO / "scripts" / "hooks" / "record-command-run.py")),
        input=json.dumps({
            "tool_name": "Bash", "cwd": str(root),
            "tool_input": {"command": "pytest tests/test_x.py::test_bug"}, "exit_code": 1,
        }),
        cwd=root, text=True, capture_output=True, check=True, timeout=30,
    )
    reference = publish_red_reference(
        root, run_dir, evidence_root=root, text=GATE, profile=PYTHON_PROFILE, since=1,
    )
    phase_entered_at = time.time() + 1
    code.write_text("value = 1\n", encoding="utf-8")
    _observe(root, "pytest tests/test_x.py::test_bug", 0, at=phase_entered_at + 1)
    text = GATE + f"red-reference: {reference}\n"
    assert missing_test_evidence_markers(
        root, "implement", text, profile=PYTHON_PROFILE, since=phase_entered_at,
        run_dir=run_dir, code_baseline=baseline, cwd_root=root,
    ) == []
    assert missing_test_evidence_markers(
        root, "implement", text, profile=PYTHON_PROFILE, since=phase_entered_at,
        run_dir=run_dir, code_baseline=test_code_baseline(root), cwd_root=root,
    ) == ["red-reference: <recorded RED for the same test and code baseline>"]
    other_test = text.replace("test_bug", "test_other")
    assert missing_test_evidence_markers(
        root, "implement", other_test, profile=PYTHON_PROFILE, since=phase_entered_at,
        run_dir=run_dir, code_baseline=baseline, cwd_root=root,
    ) == ["test-run-evidence: verified (the referenced regression must end green)"]


@pytest.mark.parametrize(
    ("marker_class", "observed_class", "exit_code", "accepted"),
    (
        ("CatalogTest", "CatalogTest", 0, True),
        ("CatalogTest", "catalogtest", 0, False),
        ("catalogtest", "CatalogTest", 0, False),
        ("CatalogTest", "CatalogTest", 1, False),
    ),
)
def test_behavior_preserving_case_sensitive_selector(
    tmp_path, marker_class, observed_class, exit_code, accepted,
):
    root = _project(tmp_path)
    text = (
        "## Completion Gate\n"
        f"regression-test: example.{marker_class}\n"
        "change-kind: Behavior-Preserving\n"
        "behavior-preserving-reason: unchanged catalog boundary\n"
        "test-run-evidence: VERIFIED\n"
    )
    _observe(root, f"./gradlew test --tests example.{observed_class}", exit_code)

    missing = missing_test_evidence_markers(
        root, "implement", text, profile=ANDROID_PROFILE, cwd_root=root,
    )

    assert (missing == []) is accepted


def test_ci_selector_preserves_case_and_normalized_evidence(tmp_path):
    root = _project(tmp_path)
    text = (
        "## Completion Gate\n"
        "regression-test: example.CatalogTest\n"
        "change-kind: Behavior-Preserving\n"
        "behavior-preserving-reason: unchanged catalog boundary\n"
        "test-run-evidence: VERIFIED\n"
    )
    profile = {"gates": [{
        **ANDROID_PROFILE["gates"][0],
        "required": True, "execution": "ci", "ci_check": "jvm",
    }]}
    _observe(root, "./gradlew test --tests example.CatalogTest", 0)

    assert missing_test_evidence_markers(
        root, "implement", text, profile=profile, cwd_root=root,
    ) == []


def test_case_sensitive_red_reference_round_trip(tmp_path):
    import subprocess
    from agent_flow.core.command_evidence import read_red_reference, test_code_baseline
    from agent_flow.runner import publish_red_reference

    root = _project(tmp_path)
    (root / ".agent-flow" / "kit.json").write_text("{}\n", encoding="utf-8")
    (root / ".gitignore").write_text(".agent-flow/\n", encoding="utf-8")
    code = root / "logic.py"
    code.write_text("value = 0\n", encoding="utf-8")
    for args in (
        ("init", "-b", "main"), ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"), ("add", "."), ("commit", "-m", "baseline"),
    ):
        subprocess.run(("git", *args), cwd=root, check=True, capture_output=True)
    run_dir = root / ".agent-flow" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    baseline = test_code_baseline(root)
    text = (
        "## Completion Gate\n"
        "regression-test: example.CatalogTest\n"
        "change-kind: feature\n"
        "test-run-evidence: verified\n"
    )
    _observe(
        root, "./gradlew test --tests example.CatalogTest",
        1, at=1, code_baseline=baseline,
    )
    reference = publish_red_reference(
        root, run_dir, evidence_root=root, text=text, profile=ANDROID_PROFILE,
    )
    code.write_text("value = 1\n", encoding="utf-8")
    _observe(root, "./gradlew test --tests example.CatalogTest", 0, at=3)
    green = text + f"red-reference: {reference}\n"

    assert missing_test_evidence_markers(
        root, "implement", green, profile=ANDROID_PROFILE, since=2,
        run_dir=run_dir, code_baseline=baseline, cwd_root=root,
    ) == []

    wrong_case = green.replace("CatalogTest", "catalogtest")
    _observe(root, "./gradlew test --tests example.catalogtest", 0, at=4)
    assert missing_test_evidence_markers(
        root, "implement", wrong_case, profile=ANDROID_PROFILE, since=2,
        run_dir=run_dir, code_baseline=baseline, cwd_root=root,
    ) != []
    assert read_red_reference(
        root, text=wrong_case, profile=ANDROID_PROFILE,
        code_baseline=baseline, cwd_root=root,
    ) is None


STRICT_PROFILE = {**PYTHON_PROFILE, "execution": {"observation": "strict"}}


@pytest.mark.parametrize("self_report", ("verified", "unavailable"))
def test_strict_observation_does_not_accept_a_self_report_without_observation(tmp_path, self_report):
    """반증: hook이 실리지 않은 세션은 `unavailable` 한 줄로 red를 통과했다(#414)."""
    root = _project(tmp_path)
    text = GATE + f"test-run-evidence: {self_report}\n"
    missing = missing_test_evidence_markers(root, "red", text, profile=STRICT_PROFILE)
    assert len(missing) == 1 and "agent-flow run-command" in missing[0]


def test_strict_observation_passes_once_the_run_command_result_is_recorded(tmp_path):
    root = _project(tmp_path)
    text = GATE + "test-run-evidence: verified\n"
    _observe(root, "pytest tests/test_x.py::test_bug", exit_code=1)
    assert missing_test_evidence_markers(root, "red", text, profile=STRICT_PROFILE) == \
        missing_test_evidence_markers(root, "red", text, profile=PYTHON_PROFILE)
    assert not any("strict observation" in marker
                   for marker in missing_test_evidence_markers(root, "red", text, profile=STRICT_PROFILE))


def test_observation_mode_rejects_an_unknown_value(tmp_path):
    root = _project(tmp_path)
    with pytest.raises(ValueError, match="execution.observation"):
        missing_test_evidence_markers(root, "red", GATE + "test-run-evidence: unavailable\n",
                                      profile={**PYTHON_PROFILE, "execution": {"observation": "strcit"}})


def test_project_override_can_turn_strict_observation_on(tmp_path):
    from agent_flow.core.profiles import load_profile_payload, observation_mode

    profiles = tmp_path / ".agent-flow" / "profiles"
    profiles.mkdir(parents=True)
    override = profiles / "generic.local.yaml"
    override.write_text("execution:\n  observation: strict\n", encoding="utf-8")
    assert observation_mode(load_profile_payload("generic", tmp_path)) == "strict"
    override.write_text("execution:\n  observation: off\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid profile override execution"):
        load_profile_payload("generic", tmp_path)
    assert observation_mode(load_profile_payload("generic", tmp_path / "elsewhere")) == "self-report"


def test_a_base_profile_with_an_unknown_observation_value_fails_when_loaded(tmp_path):
    from agent_flow.core.profiles import load_profile_payload

    profiles = tmp_path / ".agent-flow" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "custom.yaml").write_text("id: custom\nexecution:\n  observation: strcit\n", encoding="utf-8")
    with pytest.raises(ValueError, match="execution.observation"):
        load_profile_payload("custom", tmp_path)
    (profiles / "custom.yaml").write_text("id: custom\nexecution: strict\n", encoding="utf-8")
    with pytest.raises(ValueError, match="execution must be a mapping"):
        load_profile_payload("custom", tmp_path)


def test_one_strict_profile_makes_a_multi_profile_run_strict(tmp_path):
    """반증: union이 execution을 옮기지 않아 profile을 하나 더 켜면 strict가 꺼졌다."""
    from agent_flow.core.phase_workflow import find_kit_root
    from agent_flow.core.profile_resolution import load_profile_union
    from agent_flow.core.profiles import observation_mode

    profiles = tmp_path / ".agent-flow" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "python.local.yaml").write_text("execution:\n  observation: strict\n", encoding="utf-8")
    _, union = load_profile_union(find_kit_root(), ["python", "nextjs"], explicit_fallback=False, project_root=tmp_path)
    assert observation_mode(union) == "strict"
    _, plain = load_profile_union(find_kit_root(), ["python", "nextjs"], explicit_fallback=False, project_root=tmp_path / "x")
    assert observation_mode(plain) == "self-report"
