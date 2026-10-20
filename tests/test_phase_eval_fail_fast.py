"""릴리스 측정은 판정이 이미 확정되면 멈춘다. 확정되지 않은 실패로는 멈추지 않는다."""

from __future__ import annotations

import copy
import gzip
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "evals"))

from phase_eval_fail_fast import FailFast  # noqa: E402

THRESHOLDS = {"floors": {"defect_detection_rate": 1.0}, "ceilings": {"invalid_rate": 0.10}}


def _recorded_defect_review() -> dict:
    """A comparator-valid row from a committed release record, so validity is the comparator's own."""
    path = REPO / "evals" / "release-gates" / "v0.3.19" / "results.jsonl.gz"
    rows = (json.loads(line) for line in gzip.open(path, "rt", encoding="utf-8"))
    return next(row for row in rows if row["scenario"] == "review" and row.get("expected") == "request-changes"
                and row["kit"] == "after" and all(row["defects"].values()))


def _review(found: bool, kit: str = "after") -> dict:
    row = copy.deepcopy(_recorded_defect_review())
    row["kit"] = kit
    first = sorted(row["defects"])[0]
    row["defects"][first] = found
    return row


def test_a_missed_seeded_defect_on_the_new_kit_stops_the_run():
    guard = FailFast("after", kit_units=84, thresholds=THRESHOLDS)
    assert guard.observe(_review(found=True)) is None
    reason = guard.observe(_review(found=False))
    assert reason is not None and "missed" in reason and "floor is 1.0" in reason


def test_the_previous_kit_missing_a_defect_does_not_stop_the_run():
    guard = FailFast("after", kit_units=84, thresholds=THRESHOLDS)
    assert guard.observe(_review(found=False, kit="before")) is None


def test_invalid_units_stop_only_past_the_ceiling():
    guard = FailFast("after", kit_units=20, thresholds=THRESHOLDS)
    error = {"scenario": "author", "stack": "web", "mode": "clean", "kit": "after", "trial": 1, "error": "timeout"}
    assert guard.observe(dict(error)) is None
    assert guard.observe(dict(error)) is None
    assert "exceed the ceiling of 2" in guard.observe(dict(error))


def test_a_lower_defect_floor_never_stops_on_one_miss():
    guard = FailFast("after", kit_units=84, thresholds={"floors": {"defect_detection_rate": 0.9}, "ceilings": {}})
    assert guard.observe(_review(found=False)) is None


def test_main_stops_scheduling_after_a_certain_failure_and_exits_nonzero(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("phase_eval_fail_fast_main", REPO / "evals/phase_eval.py")
    evaluator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluator)
    calls = []

    def run_review(kit_name, kit, case_dir, mode, variant, trial, work, timeout, evidence=None):
        calls.append((kit_name, variant, trial))
        row = _review(found=False, kit=kit_name)
        row.update(stack=case_dir.name, mode=mode, variant=variant, trial=trial)
        return row

    monkeypatch.setattr(evaluator, "run_review", run_review)
    monkeypatch.setattr(evaluator.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "x", ""))
    output = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", [
        "phase_eval.py", "--kit", f"after={REPO}", "--scenario", "review", "--stacks", "web", "--modes", "clean",
        "--review-trials", "3", "--concurrency", "1", "--review-providers", "codex", "--author-provider", "codex",
        "--codex-model", "m", "--fail-fast", "--output", str(output),
    ])
    assert evaluator.main() == 1
    assert json.loads((output / "fail-fast.json").read_text())["reason"]
    assert len(calls) < 6, "units after the certain failure must not run"
