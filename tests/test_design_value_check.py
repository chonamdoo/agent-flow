"""수치 대조 gate.

관측자는 git이다. 원장은 agent가 쓰지만 diff는 아니다. 그래서 테스트도
"원장에 16dp라고 쓰고 12dp를 구현했다"를 반증한다.

토큰 경유(`Spacing.m`)를 위반으로 들면 정상 구현이 fix-loop에 갇힌다. 그래서
토큰은 금지가 아니라 명시를 요구하고, 명시한 이름이 diff에 있는지는 다시 git이
판정한다 — 그 경계도 함께 반증한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.test_hook_integrity import _install as _install_managed_hooks

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.cli import main
from agent_flow.artifact import _missing_completion_markers, create_run, find_active_run, read_meta, write_meta
from agent_flow.core.command_evidence import COMMANDS_RUN_LOG
from agent_flow.core.design_ledger import (
    capture_design_ledger,
    confirm_current_spec_changes,
    manual_spec_approval_statement,
    read_manual_spec_approvals,
    record_manual_spec_approval,
)
from agent_flow.core.design_value_check import (
    declared_tokens,
    missing_design_value_implementations,
    missing_spec_item_evidence,
)
from agent_flow.core.phase_workflow import load_phase_workflow_definition, parse_phase_workflow_definition
from agent_flow.core.workflow_pin import workflow_pin_metadata
from agent_flow.runner import Phase, ResumeMode, Runner


LEDGER_SOURCE = """## Design Values

horizontal-padding: 16dp
brand-primary: #FF6B00
"""

GATE = "## Completion Gate\n\nverdict: approve\n"


def _git(*args, cwd):
    return subprocess.run(("git", *args), cwd=str(cwd), capture_output=True, text=True, check=True)


@pytest.fixture()
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    for key in tuple(os.environ):
        if key.startswith("AGENT_FLOW_") or key in {"CLAUDECODE", "CLAUDE_CLI", "CODEX_CLI"}:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_FLOW_NO_UPDATE_CHECK", "1")
    monkeypatch.chdir(root)
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    _install_managed_hooks(root)
    (root / ".gitignore").write_text(".agent-flow/\n")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "init", cwd=root)
    return root


@pytest.fixture()
def run_dir(tmp_path):
    path = tmp_path / "run"
    path.mkdir()
    (path / "prd.md").write_text(LEDGER_SOURCE, encoding="utf-8")
    capture_design_ledger(path, "prd", LEDGER_SOURCE)
    return path


def _write_code(project: Path, body: str) -> None:
    (project / "Screen.kt").write_text(body, encoding="utf-8")

def _capture_spec_ledger(
    run_dir: Path,
    verification: str,
    requirement: str = "Empty search results show the empty state.",
    *,
    design_values: str = "",
    due: str = "review",
) -> None:
    artifact = (
        "## Spec Items\n\n"
        f"SPEC-1: {requirement}\n"
        f"verify: {verification}\n\n"
        f"due: {due}\n"
        "## Design Values\n"
        f"{design_values}"
    )
    (run_dir / "design.md").write_text(artifact, encoding="utf-8")
    capture_design_ledger(run_dir, "design", artifact)
    confirm_current_spec_changes(run_dir)


def _observe(
    project: Path,
    command: str,
    exit_code: int,
    *,
    cwd: Path | None = None,
    at: float = 100.0,
) -> None:
    path = project / COMMANDS_RUN_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "command": command,
                "exit_code": exit_code,
                "cwd": str(cwd or project),
                "at": at,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_test_spec_requires_observed_passing_named_test(project, run_dir):
    test_name = "test_empty_search_results_show_the_empty_state"
    _capture_spec_ledger(run_dir, f"test:{test_name}")
    review_claim = GATE + f"spec-evidence: test:{test_name}\n"
    expected = [
        f"SPEC-1: test:{test_name} "
        "(no passing observed test command includes the test name)"
    ]

    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review_claim,
    ) == expected

    _observe(project, f"pytest -q tests/test_search.py::{test_name}", exit_code=1)
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review_claim,
    ) == expected

    _observe(project, f"echo {test_name}", exit_code=0)
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review_claim,
    ) == expected

    _observe(project, f"pytest -q {test_name}", exit_code=0)
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review_claim,
    ) == expected

    _observe(
        project,
        f"pytest -q tests/test_search.py::{test_name} || true",
        exit_code=0,
    )
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review_claim,
    ) == expected

    other_checkout = project.parent / "other"
    other_checkout.mkdir()
    _observe(
        project,
        f"pytest -q tests/test_search.py::{test_name}",
        exit_code=0,
        cwd=other_checkout,
    )
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review_claim,
    ) == expected
    _observe(project, f"pytest -q tests/test_search.py::{test_name}", exit_code=0)
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review_claim,
    ) == []


def test_test_spec_evidence_is_scoped_to_the_run_not_review_entry(project, run_dir):
    definition = load_phase_workflow_definition(REPO, "default")
    test_name = "test_empty_search_results_show_the_empty_state"
    _capture_spec_ledger(run_dir, f"test:{test_name}")
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "workflow": "default",
                "current_phase": "final-review",
                "phase_index": next(i for i, phase in enumerate(definition.phases) if phase.id == "final-review"),
                **workflow_pin_metadata(definition, workflow="default"),
                "task": "Show an empty state.",
                "started_at": "1970-01-01T00:01:40+00:00",
                "phase_entered_at": "1970-01-01T00:03:20+00:00",
            }
        ),
        encoding="utf-8",
    )
    _observe(
        project,
        f"pytest -q tests/test_search.py::{test_name}",
        exit_code=0,
        at=150.0,
    )
    (run_dir / "final-review.md").write_text(GATE, encoding="utf-8")
    runner = Runner(project, run_dir=run_dir)

    runner_missing = runner._missing_required_markers(
        Phase(id="final-review", description="")
    )
    status_missing = _missing_completion_markers(
        run_dir,
        "default",
        "final-review",
        config_root=project,
        project_root=project,
    )

    assert not any(item.startswith("SPEC-1: test:") for item in runner_missing)
    assert not any(item.startswith("SPEC-1: test:") for item in status_missing)

def test_symbol_spec_scopes_value_to_changed_symbol_file(project, run_dir):
    _capture_spec_ledger(run_dir, "symbol:SearchResults=No results")
    symbol_file = project / "SearchResults.kt"
    symbol_file.write_text("class SearchResults\n", encoding="utf-8")
    _git("add", ".", cwd=project)
    _git("commit", "-m", "add symbol", cwd=project)

    symbol_file.write_text(
        'class SearchResults\nval emptyCopy = "Nothing here"\n',
        encoding="utf-8",
    )
    (project / "OtherCopy.kt").write_text(
        'val emptyCopy = "No results"\n',
        encoding="utf-8",
    )
    expected = [
        "SPEC-1: symbol:SearchResults=No results "
        "(value is not added in a changed file containing the symbol)"
    ]

    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        GATE,
    ) == expected

    symbol_file.write_text(
        'class SearchResults\nval emptyCopy = "No results"\n',
        encoding="utf-8",
    )
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        GATE,
    ) == []


def test_symbol_spec_does_not_accept_a_pure_rename(project, run_dir):
    _capture_spec_ledger(run_dir, "symbol:SearchResults=No results")
    original = project / "SearchResults.kt"
    original.write_text(
        'class SearchResults\nval emptyCopy = "No results"\n',
        encoding="utf-8",
    )
    _git("add", ".", cwd=project)
    _git("commit", "-m", "add implemented symbol", cwd=project)
    _git("mv", "SearchResults.kt", "RenamedSearchResults.kt", cwd=project)

    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        GATE,
    ) == [
        "SPEC-1: symbol:SearchResults=No results "
        "(value is not added in a changed file containing the symbol)"
    ]


def test_symbol_spec_ignores_untracked_symlinks(project, run_dir):
    _capture_spec_ledger(run_dir, "symbol:SearchResults=No results")
    outside = project.parent / "outside.kt"
    outside.write_text(
        'class SearchResults\nval emptyCopy = "No results"\n',
        encoding="utf-8",
    )
    (project / "SearchResults.kt").symlink_to(outside)

    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        GATE,
    ) == [
        "SPEC-1: symbol:SearchResults=No results "
        "(value is not added in a changed file containing the symbol)"
    ]



def test_manual_spec_requires_external_approval_record(project, run_dir):
    _capture_spec_ledger(run_dir, "manual")
    review_claim = GATE + "manual-approved: SPEC-1\n"
    expected = ["SPEC-1: manual (no user approval record)"]

    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review_claim,
    ) == expected

    (run_dir / "spec-manual-approvals.json").write_text(
        json.dumps({"approved_spec_ids": ["SPEC-1"]}),
        encoding="utf-8",
    )
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review_claim,
    ) == expected

def test_cli_records_manual_spec_after_chat_confirmation(project, run_dir):
    _capture_spec_ledger(run_dir, "manual")

    exit_code = main(
        [
            "spec",
            "approve",
            "SPEC-1",
            "--run-dir",
            str(run_dir),
        ]
    )

    assert exit_code == 0
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        GATE,
    ) == []

    _capture_spec_ledger(
        run_dir,
        "manual",
        requirement="Confirm a changed rendered copy.",
    )
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        GATE,
    ) == ["SPEC-1: manual (no user approval record)"]


def test_premerge_due_defers_only_explicit_items(project, run_dir):
    text = (
        "## Spec Items\nSPEC-1: Confirm implementation.\nverify: manual\n"
        "SPEC-2: Confirm published CI.\nverify: manual\ndue: pre-merge\n"
    )
    (run_dir / "design.md").write_text(text)
    capture_design_ledger(run_dir, "design", text)
    confirm_current_spec_changes(run_dir)
    for phase in ("final-review", "commit", "push-pr"):
        missing = missing_spec_item_evidence(project, run_dir, phase, GATE)
        assert missing == ["SPEC-1: manual (no user approval record)"]
    record_manual_spec_approval(
        run_dir, "SPEC-1", manual_spec_approval_statement(run_dir, "SPEC-1"),
    )
    assert missing_spec_item_evidence(project, run_dir, "final-review", GATE) == []
    assert "SPEC-2: manual (no user approval record)" in missing_spec_item_evidence(
        project, run_dir, "merge", GATE, review_rejected=True,
    )


def test_premerge_manual_approval_expires_when_head_changes(project, run_dir, monkeypatch):
    import agent_flow.core.design_ledger as ledger_module

    _capture_spec_ledger(run_dir, "manual", due="pre-merge")
    statement = manual_spec_approval_statement(run_dir, "SPEC-1", project_root=project)
    with pytest.raises(ValueError, match="publication evidence is missing"):
        record_manual_spec_approval(run_dir, "SPEC-1", statement, project_root=project)
    monkeypatch.setattr(ledger_module, "missing_spec_publication_evidence", lambda *a, **k: [])
    record_manual_spec_approval(run_dir, "SPEC-1", statement, project_root=project)
    assert read_manual_spec_approvals(run_dir, project_root=project) == {"SPEC-1"}
    _git("commit", "--allow-empty", "-m", "fix: new publication", cwd=project)
    assert read_manual_spec_approvals(run_dir, project_root=project) == set()
    with pytest.raises(ValueError, match="approval statement must be"):
        record_manual_spec_approval(run_dir, "SPEC-1", statement, project_root=project)
    fresh = manual_spec_approval_statement(run_dir, "SPEC-1", project_root=project)
    record_manual_spec_approval(run_dir, "SPEC-1", fresh, project_root=project)
    assert read_manual_spec_approvals(run_dir, project_root=project) == {"SPEC-1"}


@pytest.mark.parametrize("phase_id", ["merge", "merge-approval", "handoff", "terminal"])
@pytest.mark.parametrize("completed_artifact", [False, True])
def test_premerge_guard_blocks_before_adapter_or_existing_artifact(
    project, monkeypatch, capsys, phase_id, completed_artifact,
):
    import agent_flow.runner as runner_module
    from agent_flow.adapters.generic import GenericAdapter

    path = create_run(project, "full-feature", "Confirm delivery.")
    text = "## Spec Items\nSPEC-1: Confirm delivery.\nverify: manual\ndue: pre-merge\n"
    (path / "prd.md").write_text(text)
    capture_design_ledger(path, "prd", text)
    runner = Runner(project, run_dir=path)
    index = len(runner.phases) if phase_id == "terminal" else next(
        i for i, phase in enumerate(runner.phases) if phase.id == phase_id
    )
    meta = read_meta(path)
    meta.update(
        phase_index=index, current_phase=None if phase_id == "terminal" else phase_id,
        phase_entered_at="2026-08-21T00:00:00+00:00",
    )
    write_meta(path, meta)
    if completed_artifact and phase_id != "terminal":
        artifact = runner._artifact_path(runner.phases[index])
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("status: complete\nverdict: approve\n")
    active = find_active_run(project)
    active.print_status(config_root=project, project_root=project)
    status = capsys.readouterr().out
    assert "SPEC-1: manual (no user approval record)" in status
    assert "status: blocked" in status

    def forbidden(*args, **kwargs):
        pytest.fail("missing SPEC evidence reached adapter execution or rendering")

    adapter = GenericAdapter()
    monkeypatch.setattr(adapter, "execute", forbidden)
    monkeypatch.setattr(adapter, "render_envelope", forbidden)
    monkeypatch.setattr(runner_module, "detect_adapter", lambda: adapter)
    monkeypatch.setattr(runner_module, "detect_available_clis", lambda: [])
    runner.run(ResumeMode.RESUME)
    output = capsys.readouterr().out
    assert "SPEC-1: manual (no user approval record)" in output
    assert read_meta(path)["phase_index"] == index
    assert (path / "active").exists()
    if completed_artifact and phase_id != "terminal":
        assert artifact.read_text() == "status: complete\nverdict: approve\n"


@pytest.mark.parametrize(
    "ci_state",
    ["stale-head", "missing", "pending", "failed", "profile-required", "stub-artifact", "merged"],
)
def test_premerge_ci_and_merge_completion_checkpoints(project, monkeypatch, capsys, ci_state):
    import agent_flow.core.design_ledger as ledger_module
    import agent_flow.pr_watch as pr_watch
    import agent_flow.runner as runner_module
    from agent_flow.adapters.generic import GenericAdapter
    from agent_flow.core.gates import GateCommand

    path = create_run(project, "full-feature", "Confirm published CI.")
    text = "## Spec Items\nSPEC-1: Confirm published CI.\nverify: manual\ndue: pre-merge\n"
    (path / "prd.md").write_text(text)
    capture_design_ledger(path, "prd", text)
    head = _git("rev-parse", "HEAD", cwd=project).stdout.strip()
    (path / "push-pr.md").write_text(f"remote-oid: {head}\npr-url: https://github.com/example/repo/pull/1\n")
    gates = path / "artifacts" / "gate-results.json"
    gates.parent.mkdir(exist_ok=True)
    gates.write_text(json.dumps({
        "produced_by": {"gate_phase": "all", "gate_execution": "local"},
        "deferred_ci_checks": [] if ci_state == "profile-required" else ["required-ci"],
    }))
    if ci_state == "profile-required":
        monkeypatch.setattr(
            ledger_module,
            "profile_gate_commands",
            lambda *a, **k: [GateCommand("ci", ("true",), ci_check="required-ci")],
        )
    payload = {
        "url": "https://github.com/example/repo/pull/1", "state": "OPEN",
        "headRefOid": head, "reviewDecision": "APPROVED",
        "statusCheckRollup": [{"name": "required-ci", "status": "COMPLETED", "conclusion": "SUCCESS"}],
    }
    monkeypatch.setattr(ledger_module, "missing_delivery_evidence", lambda *a, **k: [])
    monkeypatch.setattr(pr_watch, "_fetch_pr_data", lambda *a, **k: payload)
    monkeypatch.setattr(pr_watch, "_fetch_review_threads", lambda *a, **k: [])
    record_manual_spec_approval(
        path, "SPEC-1", manual_spec_approval_statement(path, "SPEC-1", project_root=project),
        project_root=project,
    )
    runner = Runner(project, run_dir=path)
    index = next(i for i, phase in enumerate(runner.phases) if phase.id == "merge")
    meta = read_meta(path)
    meta.update(phase_index=index, current_phase="merge", phase_entered_at="2026-08-21T00:00:00+00:00")
    write_meta(path, meta)
    assert runner._missing_entry_spec_evidence("merge") == []
    if ci_state == "stale-head":
        payload["headRefOid"] = "0" * 40
    elif ci_state == "missing":
        payload["statusCheckRollup"] = []
    elif ci_state == "pending":
        payload["statusCheckRollup"][0].update(status="IN_PROGRESS", conclusion=None)
    elif ci_state == "failed":
        payload["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    elif ci_state == "profile-required":
        payload["statusCheckRollup"][0]["name"] = "unrelated-green-check"
    elif ci_state == "stub-artifact":
        monkeypatch.setenv("AGENT_FLOW_GENERIC_MODE", "stub")
        artifact = runner._artifact_path(runner.phases[index])
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("_stub artifact written by GenericAdapter (stub mode)._\n")

    calls = []

    def execute(phase, **kwargs):
        if ci_state != "merged":
            pytest.fail("stale or failing CI reached merge adapter")
        calls.append(phase.id)
        if phase.id != "merge":
            return False
        payload["state"] = "MERGED"
        artifact = runner._artifact_path(phase)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("status: complete\nverdict: approve\n")
        return True

    adapter = GenericAdapter()
    monkeypatch.setattr(adapter, "execute", execute)
    monkeypatch.setattr(runner_module, "detect_adapter", lambda: adapter)
    monkeypatch.setattr(runner_module, "detect_available_clis", lambda: [])
    runner.run(ResumeMode.RESUME)
    output = capsys.readouterr().out
    if ci_state == "merged":
        assert read_meta(path)["current_phase"] == "handoff"
        assert calls == ["merge", "handoff"]
    else:
        assert "status: blocked" in output
        assert read_meta(path)["current_phase"] == "merge"
        assert calls == []
        if ci_state == "stub-artifact":
            assert "generic_stub_artifact" in output
    assert read_manual_spec_approvals(path, project_root=project) == {"SPEC-1"}
    assert not (path / "pr-feedback.json").exists()


@pytest.mark.parametrize("target", ["merge", "cleanup", "terminal"])
@pytest.mark.parametrize("replay", [False, True])
def test_premerge_guard_rejects_forward_and_replayed_bypass(project, run_dir, target, replay):
    from agent_flow.core.worktree_isolation import WorktreeIsolationError

    _capture_spec_ledger(run_dir, "manual", due="pre-merge")
    phases = [
        {"id": "review", "routes": {"default": target}},
        {"id": "merge"},
        {"id": "cleanup"},
    ] if target != "terminal" else [{"id": "review"}]
    definition = parse_phase_workflow_definition(
        json.dumps({"name": "default", "phases": phases}).encode(),
        source=project / "guard.yaml", name="default",
    )
    (run_dir / "review.md").write_text("status: done\n")
    write_meta(run_dir, {
        "run_id": "guard", "workflow": "default",
        "task": "",
        **workflow_pin_metadata(definition, workflow="default"),
        "phase_index": 0, "current_phase": "review",
        "phase_entered_at": "2026-08-21T00:00:00+00:00",
    })
    runner = Runner(project, run_dir=run_dir)
    transition = runner._plan_transition(0, runner.phases[0])
    if replay:
        runner._append_transition_journal(transition)
    with pytest.raises(WorktreeIsolationError, match="SPEC-1: manual"):
        if replay:
            runner._resume_pending_transition()
        else:
            runner._commit_transition(transition)
    assert read_meta(run_dir)["phase_index"] == 0
    assert not (run_dir / "cleanup.md").exists()
    assert not (run_dir / "merge.md").exists()


@pytest.mark.parametrize("phase_id", ["final-review", "merge"])
@pytest.mark.parametrize("missing_ledger", [False, True])
def test_all_completion_paths_share_spec_evidence_check(
    project, capsys, phase_id, missing_ledger,
):
    run_dir = create_run(project, "default", "Check spec evidence")
    (run_dir / "prd.md").write_text(LEDGER_SOURCE, encoding="utf-8")
    capture_design_ledger(run_dir, "prd", LEDGER_SOURCE)
    _capture_spec_ledger(run_dir, "manual")
    if missing_ledger:
        for name in ("design-spec.md", "prd.md", "design.md"):
            (run_dir / name).unlink()
    artifact_path = run_dir / f"{phase_id}.md"
    artifact_path.write_text(GATE, encoding="utf-8")
    runner = Runner(project, run_dir=run_dir)

    runner_missing = runner._missing_required_markers(
        Phase(id=phase_id, description="")
    )
    status_missing = _missing_completion_markers(
        run_dir,
        "default",
        phase_id,
        config_root=project,
        project_root=project,
    )

    expected = (
        "spec-ledger: design-spec.md is missing"
        if missing_ledger else "SPEC-1: manual (no user approval record)"
    )
    assert expected in runner_missing
    assert expected in status_missing
    assert main([
        "spec", "markers", "--root", str(project), "--project-root", str(project),
        "--run-dir", str(run_dir), "--phase", phase_id, "--artifact", str(artifact_path),
    ]) == 0
    assert expected in json.loads(capsys.readouterr().out)


def test_spec_markers_uses_the_supplied_run_context(
    project, run_dir, capsys
):
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "task": "",
                "started_at": "1970-01-01T00:01:40+00:00",
            }
        ),
        encoding="utf-8",
    )
    other_run = project / ".agent-flow" / "runs" / "999"
    other_run.mkdir(parents=True)
    (other_run / "active").touch()
    (other_run / "meta.json").write_text(
        json.dumps(
            {
                "task": "Unrelated active task.",
                "started_at": "1970-01-01T00:03:20+00:00",
                "phase_entered_at": "1970-01-01T00:03:20+00:00",
            }
        ),
        encoding="utf-8",
    )
    artifact = run_dir / "final-review.md"
    artifact.write_text(GATE, encoding="utf-8")

    exit_code = main(
        [
            "spec",
            "markers",
            "--root",
            str(project),
            "--run-dir",
            str(run_dir),
            "--project-root",
            str(project),
            "--phase",
            "final-review",
            "--artifact",
            str(artifact),
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == []


def test_request_changes_routes_even_when_spec_evidence_is_missing(
    project,
    run_dir,
    capsys: pytest.CaptureFixture[str],
):
    definition = load_phase_workflow_definition(REPO, "default")
    _capture_spec_ledger(run_dir, "manual", design_values="request-timeout: 30s\n")
    _write_code(project, "val requestTimeout = 10\n")
    review = "## Overall\nverdict: request-changes\n"
    (run_dir / "final-review.md").write_text(review, encoding="utf-8")
    reviewer = run_dir / "final-review-fixture.md"
    reviewer.write_text(
        "## Reviewer\nreviewer-source: sub-agent\nverdict: request-changes\n",
        encoding="utf-8",
    )
    nonce = "c" * 32
    entered_at = "2026-08-21T00:00:00+00:00"
    payload = {
        "schema_version": 1,
        "phase_id": "final-review",
        "produced_by": {
            "run_id": "r1",
            "nonce": nonce,
            "phase_entered_at": entered_at,
        },
        "outcomes": [
            {
                "job_id": "claude-generalist",
                "provider": "claude",
                "model": "test-model",
                "effort": "xhigh",
                "status": "ok",
                "verdict": "request-changes",
                "required": True,
                "artifact": reviewer.name,
                "artifact_sha256": hashlib.sha256(
                    reviewer.read_bytes()
                ).hexdigest(),
                "prompt_digest": "a" * 16,
                "argv_digest": "b" * 16,
            }
        ],
    }
    serialized = json.dumps(payload)
    results = run_dir / "final-review-review-results.json"
    results.write_text(serialized, encoding="utf-8")
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "run_id": "r1",
                "workflow": "default",
                "current_phase": "final-review",
                "phase_index": next(i for i, phase in enumerate(definition.phases) if phase.id == "final-review"),
                **workflow_pin_metadata(definition, workflow="default"),
                "task": "",
                "review_nonce": nonce,
                "phase_entered_at": entered_at,
                "review_evidence": {
                    "final-review": {
                        "schema_version": 1,
                        "nonce": nonce,
                        "results_sha256": hashlib.sha256(
                            serialized.encode("utf-8")
                        ).hexdigest(),
                        "phase_entered_at": entered_at,
                        "observed_job_ids": ["claude-generalist"],
                        "blocking_job_ids": ["claude-generalist"],
                        "accept_any_provider": False,
                        "expected_job_ids_by_provider": {
                            "claude": ["claude-generalist"]
                        },
                        "complete_providers": ["claude"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    runner = Runner(project, run_dir=run_dir)
    missing_values = missing_design_value_implementations(
        project, run_dir, "final-review", GATE
    )
    assert any("request-timeout=30s" in item for item in missing_values)
    assert missing_design_value_implementations(
        project, run_dir, "final-review", review, review_rejected=True
    ) == []

    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review,
    ) == ["SPEC-1: manual (no user approval record)"]
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review,
        review_rejected=True,
    ) == []
    assert runner._missing_required_markers(
        Phase(id="final-review", description="", multi_review=True)
    ) == []
    exit_code = main(
        [
            "spec",
            "markers",
            "--root",
            str(project),
            "--run-dir",
            str(run_dir),
            "--project-root",
            str(project),
            "--phase",
            "final-review",
            "--artifact",
            str(run_dir / "final-review.md"),
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == []

    forward = parse_phase_workflow_definition(
        definition.source_bytes.replace(b"request-changes: fix-loop", b"request-changes: commit"),
        source=Path(definition.source), name="default", kit_owned=definition.kit_owned,
    )
    meta = read_meta(run_dir)
    meta.update(workflow_pin_metadata(forward, workflow="default"))
    write_meta(run_dir, meta)
    assert "SPEC-1: manual (no user approval record)" in runner._missing_required_markers(
        Phase(id="final-review", description="", multi_review=True),
    )


def test_missing_canonical_ledger_fails_closed(project, run_dir):
    (run_dir / "design-spec.md").unlink()

    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        GATE,
        task_text="Show an empty state.",
    ) == ["spec-ledger: design-spec.md is missing"]

def test_literal_values_in_the_diff_pass(project, run_dir):
    _write_code(project, "val pad = 16.dp // 16dp\nval brand = Color(0xFF6B00) // #FF6B00\n")
    assert missing_design_value_implementations(project, run_dir, "final-review", GATE) == []


def test_wrong_value_is_reported(project, run_dir):
    """반증: 16dp를 보고 12dp를 쓰면 아무도 안 잡던 자리다."""
    _write_code(project, "val pad = 12.dp\nval brand = Color(0xFF6B00) // #FF6B00\n")
    missing = missing_design_value_implementations(project, run_dir, "final-review", GATE)
    assert missing and "horizontal-padding=16dp" in missing[0]
    assert "brand-primary" not in missing[0]


def test_hex_color_case_is_ignored(project, run_dir):
    _write_code(project, "val pad = 16dp\nval brand = 0xff6b00 // #ff6b00\n")
    assert missing_design_value_implementations(project, run_dir, "final-review", GATE) == []


def test_declared_token_present_in_the_diff_passes(project, run_dir):
    """`Spacing.m`(=16dp)을 위반으로 들면 정상 구현이 fix-loop에 갇힌다."""
    _write_code(project, "val pad = Spacing.m\nval brand = BrandColors.primary\n")
    text = GATE + "design-values-implemented: horizontal-padding=Spacing.m, brand-primary=BrandColors.primary\n"
    assert missing_design_value_implementations(project, run_dir, "final-review", text) == []


def test_declared_token_absent_from_the_diff_is_reported(project, run_dir):
    """반증: 토큰 이름을 대는 것만으로 통과하면 그건 다시 자기신고다."""
    _write_code(project, "val pad = 4.dp\nval brand = 0xFF6B00\n")
    text = GATE + "design-values-implemented: horizontal-padding=Spacing.m\n"
    missing = missing_design_value_implementations(project, run_dir, "final-review", text)
    assert missing and "declared token is not in the diff" in missing[0]


def test_untouched_code_does_not_count_as_evidence(project, run_dir):
    """반증: 원래부터 있던 값이 증거가 되면 코드 0줄로도 통과한다."""
    (project / "Theme.kt").write_text("val pad = 16dp\nval brand = #FF6B00\n", encoding="utf-8")
    _git("add", ".", cwd=project)
    _git("commit", "-m", "pre-existing", cwd=project)
    _write_code(project, "val nothing = 1\n")
    missing = missing_design_value_implementations(project, run_dir, "final-review", GATE)
    assert missing and "horizontal-padding=16dp" in missing[0]


def test_committed_work_on_a_branch_still_counts(project, run_dir):
    """작업이 이미 커밋됐다고 증거가 사라지면 안 된다. merge-base부터 본다."""
    _git("checkout", "-b", "feat/x", cwd=project)
    _write_code(project, "val pad = 16dp\nval brand = #FF6B00\n")
    _git("add", ".", cwd=project)
    _git("commit", "-m", "impl", cwd=project)
    assert missing_design_value_implementations(project, run_dir, "final-review", GATE) == []


def test_other_phases_are_not_checked(project, run_dir):
    _write_code(project, "val nothing = 1\n")
    assert missing_design_value_implementations(project, run_dir, "green", GATE) == []


def test_empty_ledger_checks_nothing(project, tmp_path):
    empty = tmp_path / "empty-run"
    empty.mkdir()
    capture_design_ledger(empty, "prd", "## Design Values\n\n")
    _write_code(project, "val nothing = 1\n")
    assert missing_design_value_implementations(project, empty, "final-review", GATE) == []


def test_non_git_project_degrades(tmp_path, run_dir):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert missing_design_value_implementations(plain, run_dir, "final-review", GATE) == []


def test_declared_tokens_parsing():
    text = "## Completion Gate\n\ndesign-values-implemented: a=Spacing.m, b=Brand.primary\n"
    assert declared_tokens(text) == {"a": "Spacing.m", "b": "Brand.primary"}
    assert declared_tokens("## Completion Gate\n\ndesign-values-implemented: none\n") == {}
