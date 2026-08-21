from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.command_evidence import (
    COMMANDS_RUN_LOG,
    FEEDBACK_GREEN_EXIT_MARKER,
    FEEDBACK_RED_EXIT_MARKER,
    FEEDBACK_RUN_EVIDENCE_MARKER,
    REGRESSION_SEAM_MARKER,
    missing_feedback_evidence_markers,
    missing_test_evidence_markers,
)
from agent_flow.core.phase_workflow import load_phase_workflow_definition
from agent_flow.core.profile_routing import IMPLEMENTATION_PHASES
from agent_flow.core.skill_resolver import CODE_PHASES
from agent_flow.core.worktrees import plan_worktree, worktree_runtime_root
from agent_flow.runner import Phase, Runner
from tests.test_runner_smoke import _init_git_project, _run_cli


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".agent-flow").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _observe(
    root: Path,
    command: str,
    exit_code: int,
    *,
    at: float | None = None,
    cwd: Path | None = None,
) -> None:
    path = root / COMMANDS_RUN_LOG
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "command": command,
                    "exit_code": exit_code,
                    "cwd": str(cwd or root),
                    "at": time.time() if at is None else at,
                }
            )
            + "\n"
        )


def _feedback_gate(*, command: str, exit_marker: str, evidence: str = "verified") -> str:
    return (
        "## Completion Gate\n"
        f"feedback-command: {command}\n"
        f"{exit_marker}\n"
        f"feedback-run-evidence: {evidence}\n"
    )


def test_feedback_evidence_is_enabled_by_required_marker(tmp_path: Path) -> None:
    root = _project(tmp_path)
    assert missing_feedback_evidence_markers(root, "", required_markers=()) == []
    assert missing_feedback_evidence_markers(
        root,
        "",
        required_markers=(FEEDBACK_RUN_EVIDENCE_MARKER,),
    ) == ["feedback-command:"]


def test_successful_feedback_rejects_unavailable_command(tmp_path: Path) -> None:
    root = _project(tmp_path)
    text = (
        "status: green\n"
        + _feedback_gate(
            command="unavailable",
            exit_marker="feedback-red-exit: 1",
            evidence="unavailable",
        )
    )

    missing = missing_feedback_evidence_markers(
        root,
        text,
        required_markers=(
            FEEDBACK_RED_EXIT_MARKER,
            FEEDBACK_RUN_EVIDENCE_MARKER,
        ),
        cwd_root=root,
    )
    assert "feedback-command: <exact executed command>" in missing


def test_feedback_red_requires_the_reported_observed_failure(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _observe(root, "scripts/reproduce-session-loss.sh", 7)
    required = (FEEDBACK_RED_EXIT_MARKER, FEEDBACK_RUN_EVIDENCE_MARKER)
    matching = _feedback_gate(
        command="scripts/reproduce-session-loss.sh",
        exit_marker="feedback-red-exit: 7",
    )
    assert missing_feedback_evidence_markers(
        root, matching, required_markers=required, cwd_root=root
    ) == []

    wrong = _feedback_gate(
        command="scripts/reproduce-session-loss.sh",
        exit_marker="feedback-red-exit: 1",
    )
    missing = missing_feedback_evidence_markers(
        root, wrong, required_markers=required, cwd_root=root
    )
    assert any("reported 1" in marker for marker in missing)


def test_feedback_command_match_is_exact_and_case_sensitive(tmp_path: Path) -> None:
    required = (FEEDBACK_RED_EXIT_MARKER, FEEDBACK_RUN_EVIDENCE_MARKER)
    gate = _feedback_gate(
        command="./Scripts/ReproBug.sh",
        exit_marker="feedback-red-exit: 1",
    )

    wrapped_root = _project(tmp_path / "wrapped")
    _observe(wrapped_root, "./Scripts/ReproBug.sh || true", 1)
    missing = missing_feedback_evidence_markers(
        wrapped_root,
        gate,
        required_markers=required,
        cwd_root=wrapped_root,
    )
    assert any("was not observed" in marker for marker in missing)

    exact_root = _project(tmp_path / "exact")
    _observe(exact_root, "./Scripts/ReproBug.sh", 1)
    assert missing_feedback_evidence_markers(
        exact_root,
        gate,
        required_markers=required,
        cwd_root=exact_root,
    ) == []


def test_feedback_enum_values_remain_case_insensitive(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _observe(root, "./Scripts/ReproBug.sh", 1)
    gate = _feedback_gate(
        command="./Scripts/ReproBug.sh",
        exit_marker="feedback-red-exit: 1",
        evidence="Verified",
    )

    assert missing_feedback_evidence_markers(
        root,
        gate,
        required_markers=(
            FEEDBACK_RED_EXIT_MARKER,
            FEEDBACK_RUN_EVIDENCE_MARKER,
        ),
        cwd_root=root,
    ) == []


def test_feedback_green_uses_the_original_command_and_zero_exit(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _observe(root, "scripts/reproduce-session-loss.sh", 0)
    required = (FEEDBACK_GREEN_EXIT_MARKER, FEEDBACK_RUN_EVIDENCE_MARKER)
    gate = _feedback_gate(
        command="scripts/reproduce-session-loss.sh",
        exit_marker="feedback-green-exit: 0",
    )
    assert missing_feedback_evidence_markers(
        root,
        gate,
        required_markers=required,
        expected_command="scripts/reproduce-session-loss.sh",
        cwd_root=root,
    ) == []

    changed = _feedback_gate(
        command="scripts/different-reproduction.sh",
        exit_marker="feedback-green-exit: 0",
    )
    missing = missing_feedback_evidence_markers(
        root,
        changed,
        required_markers=required,
        expected_command="scripts/reproduce-session-loss.sh",
        cwd_root=root,
    )
    assert any("exact feedback-loop command" in marker for marker in missing)


def test_feedback_self_report_only_degrades_when_checkout_is_unobserved(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    required = (FEEDBACK_RED_EXIT_MARKER, FEEDBACK_RUN_EVIDENCE_MARKER)
    unavailable = _feedback_gate(
        command="scripts/reproduce.sh",
        exit_marker="feedback-red-exit: 1",
        evidence="unavailable",
    )
    assert missing_feedback_evidence_markers(
        root, unavailable, required_markers=required, cwd_root=root
    ) == []

    _observe(root, "git status", 0)
    missing = missing_feedback_evidence_markers(
        root, unavailable, required_markers=required, cwd_root=root
    )
    assert any(marker.startswith("feedback-run-evidence: verified") for marker in missing)


def test_feedback_only_seam_bypasses_test_gate_only_with_feedback_enforcement(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    _observe(root, "git status", 0)
    text = (
        "## Completion Gate\n"
        "regression-seam: feedback-only\n"
        "test-run-evidence: unavailable\n"
    )
    required = (REGRESSION_SEAM_MARKER, FEEDBACK_RUN_EVIDENCE_MARKER)
    assert missing_test_evidence_markers(
        root,
        "implement-fix",
        text,
        required_markers=required,
        cwd_root=root,
    ) == []
    assert missing_test_evidence_markers(
        root,
        "implement-fix",
        text,
        required_markers=(REGRESSION_SEAM_MARKER,),
        cwd_root=root,
    ) != []


def test_diagnosing_bugs_workflow_preserves_the_feedback_contract() -> None:
    definition = load_phase_workflow_definition(REPO, "diagnosing-bugs")
    phases = {phase.id: phase for phase in definition.phases}
    assert tuple(phases) == (
        "feedback-loop",
        "reproduce-minimise",
        "hypotheses",
        "instrument",
        "implement-fix",
        "diagnosis-cleanup",
        "review",
        "qa",
        "handoff",
    )
    assert phases["feedback-loop"].routes == {
        "green": "reproduce-minimise",
        "blocked": "block",
        "error": "block",
        "default": "block",
    }
    assert phases["review"].routes == {
        "approve": "qa",
        "request-changes": "hypotheses",
    }
    assert FEEDBACK_RED_EXIT_MARKER in phases["feedback-loop"].required_markers
    assert FEEDBACK_GREEN_EXIT_MARKER in phases["implement-fix"].required_markers
    assert FEEDBACK_GREEN_EXIT_MARKER in phases["qa"].required_markers
    assert phases["diagnosis-cleanup"].artifact == "cleanup.md"


def test_diagnosis_cleanup_does_not_reclassify_generic_cleanup() -> None:
    assert "cleanup" not in CODE_PHASES
    assert "cleanup" not in IMPLEMENTATION_PHASES
    assert "diagnosis-cleanup" in CODE_PHASES
    assert "diagnosis-cleanup" in IMPLEMENTATION_PHASES


def test_non_green_status_does_not_require_green_feedback_evidence(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    _observe(root, "git status", 0)
    text = (
        "status: request-changes\n"
        "## Completion Gate\n"
        "feedback-command: unavailable\n"
        "feedback-green-exit: unavailable\n"
        "feedback-run-evidence: unavailable\n"
    )

    assert missing_feedback_evidence_markers(
        root,
        text,
        required_markers=(
            FEEDBACK_GREEN_EXIT_MARKER,
            FEEDBACK_RUN_EVIDENCE_MARKER,
        ),
        expected_command="scripts/reproduce-session-loss.sh",
        recoverable_statuses=("request-changes",),
        cwd_root=root,
    ) == []


def test_route_less_non_green_status_still_requires_feedback_evidence(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    _observe(root, "git status", 0)
    text = (
        "status: request-changes\n"
        "## Completion Gate\n"
        "feedback-command: unavailable\n"
        "feedback-green-exit: unavailable\n"
        "feedback-run-evidence: unavailable\n"
    )

    assert missing_feedback_evidence_markers(
        root,
        text,
        required_markers=(
            FEEDBACK_GREEN_EXIT_MARKER,
            FEEDBACK_RUN_EVIDENCE_MARKER,
        ),
        expected_command="scripts/reproduce-session-loss.sh",
        cwd_root=root,
    ) != []


def test_unobserved_green_still_requires_zero_exit(tmp_path: Path) -> None:
    root = _project(tmp_path)
    text = _feedback_gate(
        command="scripts/reproduce-session-loss.sh",
        exit_marker="feedback-green-exit: unavailable",
        evidence="unavailable",
    )

    missing = missing_feedback_evidence_markers(
        root,
        text,
        required_markers=(
            FEEDBACK_GREEN_EXIT_MARKER,
            FEEDBACK_RUN_EVIDENCE_MARKER,
        ),
        expected_command="scripts/reproduce-session-loss.sh",
        cwd_root=root,
    )
    assert "feedback-green-exit: 0" in missing


def test_green_qa_rejects_failed_regression_status(tmp_path: Path) -> None:
    root = _project(tmp_path)
    text = (
        "status: green\n"
        "## Completion Gate\n"
        "feedback-command: scripts/reproduce-session-loss.sh\n"
        "feedback-green-exit: 0\n"
        "feedback-run-evidence: unavailable\n"
        "regression-test-status: failed\n"
    )

    missing = missing_feedback_evidence_markers(
        root,
        text,
        required_markers=(
            FEEDBACK_GREEN_EXIT_MARKER,
            FEEDBACK_RUN_EVIDENCE_MARKER,
        ),
        expected_command="scripts/reproduce-session-loss.sh",
        cwd_root=root,
    )
    assert "regression-test-status: pass|feedback-only" in missing


def test_runner_derives_green_command_from_prior_red_marker(tmp_path: Path) -> None:
    runner = object.__new__(Runner)
    runner.run_dir = tmp_path
    red = Phase(
        id="custom-red",
        description="",
        required_markers=(FEEDBACK_RED_EXIT_MARKER,),
    )
    green = Phase(
        id="custom-green",
        description="",
        required_markers=(FEEDBACK_GREEN_EXIT_MARKER,),
    )
    runner.phases = [red, green]
    (tmp_path / "custom-red.md").write_text(
        "## Completion Gate\n"
        "feedback-command: scripts/reproduce-session-loss.sh\n"
        "feedback-red-exit: 1\n",
        encoding="utf-8",
    )

    assert (
        runner._expected_feedback_command(green)
        == "scripts/reproduce-session-loss.sh"
    )


def test_runner_exempts_only_blocking_or_backward_feedback_routes() -> None:
    runner = object.__new__(Runner)
    hypotheses = Phase(id="hypotheses", description="")
    implement = Phase(id="implement-fix", description="")
    qa = Phase(
        id="qa",
        description="",
        routes={
            "green": "handoff",
            "request-changes": "hypotheses",
            "blocked": "block",
            "error": "block",
        },
    )
    forward_only = Phase(
        id="forward-only",
        description="",
        routes={"error": "handoff"},
    )
    handoff = Phase(id="handoff", description="")
    runner.phases = [hypotheses, implement, qa, forward_only, handoff]

    assert runner._feedback_recoverable_statuses(implement) == ()
    assert runner._feedback_recoverable_statuses(qa) == (
        "request-changes",
        "blocked",
        "error",
    )
    assert runner._feedback_recoverable_statuses(forward_only) == ()


def test_diagnosing_bugs_run_creates_worktree_and_stops_at_feedback_loop(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    project = tmp_path / "project"
    project.mkdir()
    _init_git_project(project)

    result = _run_cli(
        ["run", "intermittent session loss", "--workflow", "diagnosing-bugs"],
        project,
    )

    assert result.returncode == 0, result.stderr
    plan = plan_worktree(root=project, name="intermittent session loss")
    assert plan.path.is_dir()
    runs_root = (
        worktree_runtime_root(root=project, name=plan.name)
        / ".agent-flow"
        / "runs"
    )
    runs = tuple(path for path in runs_root.iterdir() if path.is_dir())
    assert len(runs) == 1
    meta = json.loads((runs[0] / "meta.json").read_text(encoding="utf-8"))
    assert meta["current_phase"] == "feedback-loop"
    assert "phase 'feedback-loop' is blocked" in result.stdout
    _observe(project, "git status", 0, cwd=plan.path)
    (runs[0] / "feedback-loop.md").write_text(
        "status: blocked\n"
        "## Completion Gate\n"
        "feedback-command: unavailable\n"
        "feedback-red-exit: unavailable\n"
        "feedback-run-evidence: unavailable\n",
        encoding="utf-8",
    )
    continued = _run_cli(["continue", "--worktree", plan.name], project)
    assert continued.returncode == 0, continued.stderr
    assert '"reason": "route_blocked"' in continued.stdout
    assert "missing completion markers" not in continued.stdout
