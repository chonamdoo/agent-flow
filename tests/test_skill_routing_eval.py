"""`evals/skill_routing.py`는 PR CI에서 라우팅 회귀를 판정한다. 놓치면 required skill이 조용히 빠진다."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "evals"))
_SPEC = importlib.util.spec_from_file_location("skill_routing", REPO / "evals" / "skill_routing.py")
assert _SPEC is not None and _SPEC.loader is not None
routing = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(routing)

ALIASES = {"presentation": "react-native-clean-presentation-architecture", "shell": "react-native-app-shell-error-handling"}


def _payload(**cases: dict) -> dict:
    return {"profiles": [{"profile": "react-native", "cases": cases}]}


def _passed() -> dict:
    return {"passed": True, "missing": [], "unexpected": []}


def _failed(missing=(), unexpected=()) -> dict:
    return {"passed": False, "missing": list(missing), "unexpected": list(unexpected)}


def test_shipped_corpus_is_valid_and_covers_both_splits_and_negatives():
    corpus = routing.load_corpus()
    splits = {case["split"] for case in corpus["cases"]}
    kinds = {case["kind"] for case in corpus["cases"]}
    assert splits == {"dev", "heldout"}
    assert {"exact", "paraphrase", "negative"} <= kinds


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        (lambda c: c["cases"].append(copy.deepcopy(c["cases"][0])), "duplicate case id"),
        (lambda c: c["cases"][0].update(profile="cobol"), "unknown profile"),
        (lambda c: c["cases"][0].update(split="train"), "split must be"),
        (lambda c: c["cases"][0].update(required=["no-such-alias"]), "unknown skill aliases"),
        (lambda c: (c["cases"][0].pop("required", None), c["cases"][0].pop("absent", None)), "must name required or absent"),
        (lambda c: c["neutral_files"].pop(c["cases"][0]["profile"]), "no neutral file"),
    ],
)
def test_malformed_corpus_is_rejected(tmp_path, patch, message):
    corpus = json.loads(routing.CASES.read_text(encoding="utf-8"))
    patch(corpus)
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(corpus), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        routing.load_corpus(path)


def test_a_case_passes_only_when_required_skills_are_present_and_absent_skills_are_not():
    case = {"required": ["presentation"], "absent": ["shell"]}
    assert routing.score_case(case, ALIASES, {ALIASES["presentation"]})["passed"]
    missing = routing.score_case(case, ALIASES, set())
    assert missing == {"passed": False, "missing": ["presentation"], "unexpected": []}
    unexpected = routing.score_case(case, ALIASES, {ALIASES["presentation"], ALIASES["shell"]})
    assert unexpected == {"passed": False, "missing": [], "unexpected": ["shell"]}


def test_a_case_that_passed_before_and_fails_after_is_a_regression():
    failures, _ = routing.compare(_payload(a=_passed()), _payload(a=_failed(missing=["presentation"])))
    assert failures == ["a: regressed (missing ['presentation'], unexpected [])"]


def test_a_case_that_failed_before_may_keep_failing_and_a_fix_is_reported():
    failures, notes = routing.compare(
        _payload(a=_failed(missing=["presentation"]), b=_failed(missing=["shell"])),
        _payload(a=_failed(missing=["presentation"]), b=_passed()),
    )
    assert failures == []
    assert notes == ["b: now passes"]


def test_a_case_measured_before_but_not_after_fails_instead_of_looking_unchanged():
    failures, _ = routing.compare(
        _payload(a=_passed()),
        _payload(a={"error": "RuntimeError: boom"}),
    )
    assert failures == ["a: measured before but not after"]


def test_no_measured_baseline_fails():
    failures, _ = routing.compare(_payload(a={"error": "boom"}), _payload(a=_passed()))
    assert "before measured no case" in failures[0]


def test_a_profile_that_could_not_install_after_fails():
    after = {"profiles": [{"profile": "react-native", "install_error": "install failed"}]}
    failures, _ = routing.compare(_payload(a=_passed()), after)
    assert "a: measured before but not after" in failures
    assert "react-native: install failed" in failures


def test_summary_counts_cases_by_split_kind_and_language():
    corpus = {"cases": [
        {"id": "a", "split": "dev", "kind": "exact", "lang": "ko"},
        {"id": "b", "split": "heldout", "kind": "paraphrase", "lang": "en"},
    ]}
    totals = routing.summarize(corpus, _payload(a=_passed(), b=_failed(missing=["shell"]))["profiles"])
    assert totals["all"] == {"passed": 1, "total": 2}
    assert totals["split:heldout"] == {"passed": 0, "total": 1}
    assert totals["kind:exact"] == {"passed": 1, "total": 1}
