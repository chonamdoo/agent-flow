"""#418: 모델이 흡수한 skill과 아무것도 재지 않는 skill을 기록에서 고른다. 모델을 부르지 않는다."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def retirement(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "evals"))
    spec = importlib.util.spec_from_file_location("skill_retirement", ROOT / "evals/skill_retirement.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_file(path: Path, summary: list[dict], rows: list[dict] | None = None) -> Path:
    path.write_text(json.dumps({"host": "claude", "rows": rows or [], "summary": summary}), encoding="utf-8")
    return path


CASES = {"easy": ("alpha", "shared"), "hard": ("shared",), "gain": ("beta",)}


def test_a_skill_is_a_candidate_only_when_every_case_naming_it_is_at_the_ceiling(retirement, tmp_path):
    entries, skipped = retirement.run_measurements([_run_file(tmp_path / "r.json", [
        {"case": "easy", "config": "baseline", "n": 5, "resolution": True, "both": 1.0},
        {"case": "easy", "config": "agents-index", "n": 5, "resolution": True, "both": 1.0},
        {"case": "hard", "config": "baseline", "n": 5, "resolution": True, "both": 0.4},
        {"case": "hard", "config": "agents-index", "n": 5, "resolution": True, "both": 0.4},
        {"case": "gain", "config": "baseline", "n": 5, "resolution": True, "both": 0.2},
        {"case": "gain", "config": "agents-index", "n": 5, "resolution": True, "both": 0.8},
    ])])
    table = retirement.classify(["alpha", "beta", "shared", "unmeasured"], CASES, entries)
    assert skipped == 0
    assert {name: item["status"] for name, item in table.items()} == {
        "alpha": "retire-candidate", "beta": "keep", "shared": "inconclusive", "unmeasured": "unverified"}


def test_too_few_trials_or_an_unresolved_row_is_not_a_ceiling(retirement, tmp_path):
    entries, skipped = retirement.run_measurements([_run_file(tmp_path / "r.json", [
        {"case": "easy", "config": "baseline", "n": 2, "resolution": True, "both": 1.0},
        {"case": "gain", "config": "baseline", "n": 5, "resolution": False, "both": None},
    ])])
    table = retirement.classify(["alpha", "beta"], CASES, entries)
    assert skipped == 1 and table["alpha"]["status"] == "inconclusive" and table["beta"]["status"] == "inconclusive"


def test_an_early_summary_is_attributed_to_the_files_single_case(retirement, tmp_path):
    rows = [{"case": "easy", "config": "baseline", "host_ok": True}]
    entries, skipped = retirement.run_measurements([_run_file(tmp_path / "r.json",
                                                              [{"config": "baseline", "n": 5, "both": 1.0}], rows)])
    assert skipped == 0 and entries[0]["case"] == "easy"
    failed = [{"case": "easy", "config": "baseline", "host_ok": False}]
    _, skipped = retirement.run_measurements([_run_file(tmp_path / "f.json",
                                                        [{"config": "baseline", "n": 5, "both": 1.0}], failed)])
    assert skipped == 1


def test_the_latest_baseline_decides_and_the_trend_keeps_every_point(retirement, tmp_path):
    old = _run_file(tmp_path / "20260101.json", [{"case": "easy", "config": "baseline", "n": 5, "resolution": True, "both": 0.6}])
    new = _run_file(tmp_path / "20260201.json", [{"case": "easy", "config": "baseline", "n": 5, "resolution": True, "both": 1.0}])
    entries, _ = retirement.run_measurements([new, old])
    item = retirement.classify(["alpha"], CASES, entries)["alpha"]
    assert item["status"] == "retire-candidate"
    assert item["trend"]["easy"] == ["20260101:0.60", "20260201:1.00"]


def test_skill_task_reports_count_passed_host_runs_per_config(retirement, tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"metadata": {"started_at": "2026-10-01"}, "results": [
        {"case": "gain", "config": "baseline", "host_ok": True, "score": {"passed": False}},
        {"case": "gain", "config": "skill-index", "host_ok": True, "score": {"passed": True}},
        {"case": "gain", "config": "skill-index", "host_ok": False, "score": {"passed": False}},
    ]}), encoding="utf-8")
    entries = retirement.skill_task_measurements([report])
    assert {(entry["config"], entry["score"], entry["n"]) for entry in entries} == {("baseline", 0.0, 1), ("skill-index", 1.0, 1)}
    assert retirement.classify(["beta"], CASES, entries)["beta"]["status"] == "keep"


def test_plan_lists_a_baseline_command_for_every_recorded_case(retirement):
    commands = retirement.plan("claude", "model-x", 5)
    assert all("--config baseline" in command for command in commands[:-1])
    assert commands[-1].startswith("python3 evals/skill_tasks.py --model model-x")
