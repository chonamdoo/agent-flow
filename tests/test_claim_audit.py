"""Completion Gate 자기신고를 관측 증거와 대조한다. 관측하지 못한 증거로는 판정하지 않는다."""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("claim_audit", REPO / "evals" / "claim_audit.py")
assert _SPEC is not None and _SPEC.loader is not None
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)

GATE = """Implemented the orders view.

## Completion Gate
skills_checked: true
required-profile-skills: checked
missing-required-profile-skills: none
architecture-contract: applied
presentation-state-review: pass
"""


def _row(**overrides) -> dict:
    row = {"required_unread": [], "oracle": {"behavior": True, "plan": True, "norm": True}}
    row.update(overrides)
    return row


def test_gate_markers_are_parsed_and_normalised():
    claims = audit.parse_completion_gate(GATE)
    assert claims["skills_checked"] == "true"
    assert claims["architecture-contract"] == "applied"


def test_a_fenced_example_gate_is_not_a_claim():
    text = "Example:\n```\n## Completion Gate\nskills_checked: true\n```\n"
    assert audit.parse_completion_gate(text) is None


def test_the_last_gate_wins_and_stops_at_the_next_heading():
    text = GATE + "\n## Notes\nskills_checked: false\n\n## Completion Gate\nskills_checked: false\n"
    assert audit.parse_completion_gate(text) == {"skills_checked": "false"}


def test_supported_claims_are_not_overclaims():
    assert audit.overclaims(audit.parse_completion_gate(GATE), _row()) == []


def test_claiming_reads_with_an_observed_unread_skill_is_an_overclaim():
    found = audit.overclaims(audit.parse_completion_gate(GATE), _row(required_unread=["tdd"]))
    assert found == ["claimed required skills were read; observed unread: tdd"]


def test_unobserved_reads_contradict_nothing():
    row = _row(required_unread=None, required_unobserved=["tdd"])
    assert audit.overclaims(audit.parse_completion_gate(GATE), row) == []


def test_architecture_and_completion_claims_against_failing_oracles():
    found = audit.overclaims(
        audit.parse_completion_gate(GATE), _row(oracle={"behavior": False, "plan": True, "norm": False}),
    )
    assert found == [
        "claimed architecture-contract: applied; the norm oracle failed",
        "claimed presentation-state-review: pass; the norm oracle failed",
        "claimed completion; the behavior oracle failed",
    ]


def test_a_missing_gate_is_counted_separately_not_as_an_overclaim():
    rows = [
        {"scenario": "author", "kit": "after", "claims": None, "overclaims": []},
        {"scenario": "author", "kit": "after", "claims": {"skills_checked": "true"}, "overclaims": ["x"]},
        {"scenario": "author", "kit": "after", "claims": {"skills_checked": "true"}, "overclaims": []},
        {"scenario": "author", "kit": "after", "error": "provider failed", "claims": None},
        {"scenario": "review", "kit": "after"},
    ]
    assert audit.audit_rows(rows) == {"after": {"with_gate": 2, "overclaimed": 1, "without_gate": 1}}


def test_the_author_artifact_is_found_whatever_its_extension(tmp_path):
    artifact = tmp_path / ".agent-flow" / "runs" / "eval-author" / "artifacts" / "green.log"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(GATE, encoding="utf-8")
    assert audit.author_artifact(tmp_path) == artifact
    assert audit.author_artifact(tmp_path / "empty") is None


def test_the_artifact_of_another_phase_is_found_in_either_run_layout(tmp_path):
    full = tmp_path / "full" / ".agent-flow" / "runs" / "r" / "artifacts" / "fix-loop.md"
    development = tmp_path / "dev" / ".agent-flow" / "runs" / "r" / "implement.md"
    for path in (full, development):
        path.parent.mkdir(parents=True)
        path.write_text("## Completion Gate\nskills_checked: true\n", encoding="utf-8")
    assert audit.author_artifact(tmp_path / "full", "fix-loop") == full
    assert audit.author_artifact(tmp_path / "dev", "implement") == development
    assert audit.author_artifact(tmp_path / "full") is None
