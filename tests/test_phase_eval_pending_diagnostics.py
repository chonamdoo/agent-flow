from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from tests.test_phase_eval_hardening import ROOT, evaluator


@pytest.mark.parametrize("stack", ["app", "backend"])
@pytest.mark.parametrize("mode,expected", [("team", "pending"), ("team-approved", "local")])
def test_review_preparation_keeps_pending_separate_from_approved_contract(evaluator, tmp_path, stack, mode, expected):
    if stack == "app" and any(shutil.which(tool) is None for tool in ("dart", "flutter")):
        pytest.skip("actual app review preparation requires Dart and Flutter")
    project = tmp_path / "project"
    case_dir = ROOT / "evals/phase-cases" / stack
    evaluator._prepare_subprocess(ROOT, case_dir, mode, "review", "clean", project)
    architecture = yaml.safe_load((project / ".agent-flow/project.yaml").read_text())["architecture"]
    assert architecture["mode"] == expected


@pytest.mark.parametrize("stack", ["app", "backend"])
def test_approved_diagnostic_mode_rejects_author_before_creating_project(evaluator, tmp_path, stack):
    project = tmp_path / "project"
    with pytest.raises(ValueError, match="review.only"):
        evaluator.prepare(ROOT, ROOT / "evals/phase-cases" / stack, "team-approved", "author", "", project)
    assert not project.exists()


def _review_output(evaluator, monkeypatch, tmp_path: Path, stack: str, mode: str, variant: str, verdict: str):
    monkeypatch.setattr(evaluator, "_prepare_subprocess", lambda *args: {
        "jobs": [{"angle": "contract", "provider": "codex", "prompt": "Review the installed contract."}],
    })
    findings = {
        "app": "orders_repository.dart catch silently drops errors without calling reportTeamError.\n"
               "orders_list_notifier.dart _formatCents drops two decimal trailing zero padding; $12.5 is wrong.",
        "backend": "models.py OrderRowDTO dataclass violates orders-team-conventions; must use NamedTuple.\n"
                   "list_customer_orders in listing.py ignores customer_id without filtering, leaking all customers.",
    }
    body = findings[stack] if variant == "defect" else "A new module needs the user's architecture decision in pending mode."
    text = "## Reviewer\nreviewer-source: sub-agent\nverdict: " + verdict
    if verdict == "request-changes":
        text += "\n\n## Must-fix\n" + body
    monkeypatch.setattr(evaluator, "_codex", lambda *args, **kwargs: {
        "rc": 0, "text": text, "seconds": 0, "usage": {
            "input": 100, "uncached_input": 50, "cached_input": 50, "output": 10,
        },
    })
    return evaluator.run_review("after", ROOT, ROOT / "evals/phase-cases" / stack,
                                mode, variant, 1, tmp_path, 10)


@pytest.mark.parametrize("stack", ["app", "backend"])
@pytest.mark.parametrize("verdict", ["approve", "request-changes"])
def test_pending_clean_verdict_remains_valid_without_fixed_accuracy(evaluator, monkeypatch, tmp_path, stack, verdict):
    result = _review_output(evaluator, monkeypatch, tmp_path, stack, "team", "clean", verdict)
    assert result["valid"] is True
    assert result["overall"] == verdict
    assert result["expected"] is None and result["correct"] is None


@pytest.mark.parametrize("stack", ["app", "backend"])
@pytest.mark.parametrize("variant,verdict,correct", [
    ("clean", "approve", True),
    ("clean", "request-changes", False),
    ("defect", "request-changes", True),
    ("defect", "approve", False),
])
def test_approved_counterpart_scores_clean_and_seeded_defect(evaluator, monkeypatch, tmp_path, stack, variant, verdict, correct):
    result = _review_output(evaluator, monkeypatch, tmp_path, stack, "team-approved", variant, verdict)
    assert result["valid"] is True
    assert result["correct"] is correct
    if variant == "defect":
        assert result["defects"] == {"mode-rule": verdict == "request-changes", "bug": verdict == "request-changes"}


@pytest.mark.parametrize("stack", ["app", "backend"])
@pytest.mark.parametrize("variant,expected", [("clean", True), ("defect", False)])
def test_approved_diagnostic_oracle_checks_its_fixture_norm(tmp_path, stack, variant, expected):
    case_dir = ROOT / "evals/phase-cases" / stack
    project = tmp_path / "project"
    shutil.copytree(case_dir / "review/team" / variant, project)
    if stack == "app":
        shutil.copytree(case_dir / "modes/team/overlay", project, dirs_exist_ok=True)
    result = subprocess.run(
        [sys.executable, str(case_dir / "oracle.py"), "norm",
         "--project", str(project), "--mode", "team-approved"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["norm"] is expected


@pytest.mark.parametrize("stack", ["app", "backend"])
def test_approved_diagnostic_oracle_rejects_author_without_creating_project(tmp_path, stack):
    case_dir = ROOT / "evals/phase-cases" / stack
    project = tmp_path / "not-created"
    result = subprocess.run(
        [sys.executable, str(case_dir / "oracle.py"), "author",
         "--project", str(project), "--mode", "team-approved"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert not project.exists()
