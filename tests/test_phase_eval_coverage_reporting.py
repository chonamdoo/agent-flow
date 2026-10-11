from __future__ import annotations

import copy
import hashlib
import json

import pytest

from tests.test_phase_eval_compare import CASE, META, THRESHOLDS, gate, rows_for, write_run


@pytest.fixture
def pending_matrix(gate):
    """The release matrix has 48 reviews per kit, four intentionally unscored."""
    meta = copy.deepcopy(META)
    for stack in ("app", "backend"):
        case = copy.deepcopy(CASE)
        case["review"]["team"]["clean"] = {
            "expect": None,
            "note": f"{stack} pending architecture permits a decision request; approve is not fixed.",
        }
        body = json.dumps(case)
        (gate.DEFAULT_CASES / stack / "case.json").write_text(body, encoding="utf-8")
        meta["case_files"][f"{stack}/case.json"] = hashlib.sha256(body.encode()).hexdigest()
    rows = []
    for kit in ("before", "after"):
        for stack in ("app", "backend", "web", "rn"):
            for mode in ("clean", "local", "team"):
                units = rows_for(kit, stack=stack, mode=mode)
                for row in units:
                    if row["scenario"] == "review" and row["trial"] == 3:
                        continue
                    if row.get("variant") == "clean" and stack in ("app", "backend") and mode == "team":
                        row.update(expected=None, correct=None, false_request_changes=None)
                        # Both verdicts are permitted without inventing a fixed accuracy answer.
                        row["overall"] = "request-changes" if row["trial"] == 1 else "approve"
                    if row.get("variant") == "clean" and stack == "web" and mode == "team" and row["trial"] == 1:
                        row.update(overall="request-changes", correct=False, false_request_changes=1)
                    rows.append(row)
    thresholds = copy.deepcopy(THRESHOLDS)
    thresholds["matrix"].update(stacks=["app", "backend", "web", "rn"], modes=["clean", "local", "team"])
    return rows, meta, thresholds


def test_report_distinguishes_completed_valid_scored_and_pending_units(gate, tmp_path, pending_matrix):
    rows, meta, thresholds = pending_matrix
    original = copy.deepcopy(rows)
    run = write_run(tmp_path / "run", rows, meta=meta)
    lines, failures = gate.compare([run], "before", "after", thresholds)
    report = "\n".join(lines)
    assert failures == []
    assert "168 rows." in report
    assert "|review completed|48|48|" in report
    assert "|review valid|48|48|" in report
    assert "|review invalid|0|0|" in report
    assert "|review accuracy scored|44|44|" in report
    assert "|review accuracy unscored (expected=null)|4|4|" in report
    assert "|review correct / accuracy scored|43/44|43/44|" in report
    assert "|review_correct_rate|0.977|0.977|" in report
    for kit in ("before", "after"):
        for stack in ("app", "backend"):
            assert f"|{kit}|{stack}/team/clean|2|{stack} pending architecture" in report
    assert rows == original


def test_invalid_pending_trial_is_not_reported_as_valid_unscored(gate, tmp_path, pending_matrix):
    rows, meta, thresholds = pending_matrix
    trial = next(row for row in rows if row["kit"] == "after" and row["stack"] == "app"
                 and row["mode"] == "team" and row.get("variant") == "clean")
    trial.update(valid=False, error="provider failed", overall="incomplete")
    run = write_run(tmp_path / "run", rows, meta=meta)
    lines, failures = gate.compare([run], "before", "after", thresholds)
    report = "\n".join(lines)
    assert failures == []
    assert "|review completed|48|48|" in report
    assert "|review valid|48|47|" in report
    assert "|review invalid|0|1|" in report
    assert "|review accuracy unscored (expected=null)|4|3|" in report
    assert "|review correct / accuracy scored|43/44|43/44|" in report


def test_unscored_reason_requires_measured_case_fingerprint(gate, tmp_path, pending_matrix):
    rows, meta, thresholds = pending_matrix
    path = gate.DEFAULT_CASES / "app" / "case.json"
    case = json.loads(path.read_text(encoding="utf-8"))
    case["review"]["team"]["clean"]["note"] = "Unmeasured new claim about pending architecture."
    path.write_text(json.dumps(case), encoding="utf-8")
    run = write_run(tmp_path / "run", rows, meta=meta)
    lines, failures = gate.compare([run], "before", "after", thresholds, export=tmp_path / "record")
    report = "\n".join(lines)
    assert any("case contract hash does not match measurement: app/case.json" in item for item in failures)
    assert "Reason unavailable: case contract does not match measurement." in report
    assert "Unmeasured new claim" not in report
    assert not (tmp_path / "record").exists()


def test_compact_record_retains_pending_coverage_without_reviewer_text(gate, tmp_path, pending_matrix):
    rows, meta, thresholds = pending_matrix
    run = write_run(tmp_path / "run", rows, meta=meta)
    original, failures = gate.compare([run], "before", "after", thresholds, export=tmp_path / "record")
    assert failures == []
    compact, failures = gate.compare([tmp_path / "record"], "before", "after", thresholds)
    assert failures == []
    assert compact == original
    loaded, failures, _, _ = gate.load([tmp_path / "record"])
    assert failures == []
    pending = [row for row in loaded if row["scenario"] == "review" and row["expected"] is None]
    assert len(pending) == 8
    assert all(row["valid"] is True and row["correct"] is None and "overall" not in row for row in pending)


def test_unscored_case_without_note_reports_explicit_unknown_reason(gate, tmp_path, pending_matrix):
    rows, meta, thresholds = pending_matrix
    path = gate.DEFAULT_CASES / "app" / "case.json"
    case = json.loads(path.read_text(encoding="utf-8"))
    del case["review"]["team"]["clean"]["note"]
    body = json.dumps(case)
    path.write_text(body, encoding="utf-8")
    meta["case_files"]["app/case.json"] = hashlib.sha256(body.encode()).hexdigest()
    run = write_run(tmp_path / "run", rows, meta=meta)
    lines, failures = gate.compare([run], "before", "after", thresholds)
    assert failures == []
    assert "Case explicitly declares expect=null; no reason recorded." in "\n".join(lines)
