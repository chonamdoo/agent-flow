"""#425: 기록된 리뷰 row로 angle 부분집합과 k-of-N을 다시 센다. 모델을 부르지 않는다."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def subsets():
    spec = importlib.util.spec_from_file_location("review_angle_subsets", ROOT / "evals/review_angle_subsets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(expected, verdicts, detectors=None, uncached=10):
    return {
        "scenario": "review", "kit": "after", "expected": expected,
        "angles": [{"angle": name, "provider": "codex", "verdict": verdict, "usage": {"uncached_input": uncached}}
                   for name, verdict in verdicts.items()],
        **({"defect_detectors": detectors} if detectors is not None else {}),
    }


ROWS = [
    _row("request-changes", {"generalist": "request-changes", "types": "request-changes", "a11y": "approve"},
         {"bug": ["generalist/codex", "types/codex"], "mode-rule": ["a11y/codex"]}),
    _row("approve", {"generalist": "approve", "types": "request-changes", "a11y": "approve"}),
    _row("approve", {"generalist": "approve", "types": "approve", "a11y": "approve"}),
]


def test_a_subset_keeps_only_its_angles_detectors_flags_and_tokens(subsets):
    every = subsets.score(ROWS)
    assert (every["detection_rate"], every["clean_false_request_changes_rate"], every["uncached_input_per_row"]) == (
        1.0, 0.5, 30)
    alone = subsets.score(ROWS, subset=frozenset({"generalist"}))
    assert (alone["detection_rate"], alone["clean_false_request_changes_rate"], alone["uncached_input_per_row"]) == (
        0.5, 0.0, 10)


def test_k_of_n_needs_agreement_for_a_detection_and_a_flag(subsets):
    vote = subsets.score(ROWS, k=2)
    assert vote["detection_rate"] == 0.5 and vote["clean_false_request_changes_rate"] == 0.0


def test_rows_without_per_angle_results_are_skipped_and_counted(subsets, tmp_path):
    compact = {"scenario": "review", "kit": "after", "expected": "approve", "angles": [{"provider": "codex"}]}
    path = tmp_path / "results.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in [*ROWS, compact]), encoding="utf-8")
    text = subsets.report(subsets.read_rows([tmp_path]), kit="after", subsets=[frozenset({"types"})], ks=[2])
    assert text.startswith("3 review rows scored, 1 skipped")
    assert "| types | 0.500 | 2 | 0.500 | 2 | 10 |" in text
    assert "| 2-of-N | 0.500 | 2 | 0.000 | 2 | 30 |" in text


def test_one_angle_run_by_two_providers_is_one_vote(subsets):
    row = _row("request-changes", {"generalist": "request-changes", "types": "approve"},
               {"bug": ["generalist/claude", "generalist/codex"]})
    row["angles"].append({"angle": "generalist", "provider": "claude", "verdict": "request-changes",
                          "usage": {"uncached_input": 10}})
    clean = _row("approve", {"generalist": "request-changes", "types": "approve"})
    clean["angles"].append({"angle": "generalist", "provider": "claude", "verdict": "request-changes",
                            "usage": {"uncached_input": 10}})
    vote = subsets.score([row, clean], k=2)
    assert vote["detection_rate"] == 0.0 and vote["clean_false_request_changes_rate"] == 0.0


def test_angle_names_with_a_slash_keep_their_detections(subsets):
    row = _row("request-changes", {"security/auth": "request-changes"}, {"bug": ["security/auth/codex"]})
    assert subsets.score([row], subset=frozenset({"security/auth"}))["detection_rate"] == 1.0


@pytest.mark.parametrize("broken", ["invalid", "error", "incomplete-verdict", "missing-detector"])
def test_rows_that_cannot_be_scored_honestly_are_skipped(subsets, broken):
    row = _row("request-changes", {"generalist": "request-changes"}, {"bug": ["generalist/codex"]})
    row["defects"] = {"bug": True}
    if broken == "invalid":
        row["valid"] = False
    elif broken == "error":
        row["error"] = "provider failed"
    elif broken == "incomplete-verdict":
        row["angles"][0]["verdict"] = "incomplete"
    else:
        row["defects"]["missed"] = False
    assert not subsets._scorable(row)


def test_unmeasured_usage_is_not_a_saving(subsets):
    row = _row("approve", {"generalist": "approve"})
    row["angles"][0]["usage"] = {"uncached_input": None}
    assert subsets.score([row])["uncached_input_per_row"] is None
