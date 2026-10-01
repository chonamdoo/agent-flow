from __future__ import annotations

import copy
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS = {
    "min_trials": 3,
    "floors": {"author_required_read_rate": 0.95, "review_correct_rate": 0.95, "defect_detection_rate": 1.0},
    "ceilings": {"invalid_rate": 0.10},
    "no_drop": ["author_required_read_rate", "author_pass_rate", "review_correct_rate",
                "defect_detection_rate", "reviewer_read_rate"],
    "no_rise": ["false_request_changes_rate"],
    "token_ratio_max": {"uncached_input": 1.05, "output": 1.10},
    "matrix": {"stacks": ["web"], "modes": ["clean"], "variants": {"author": [""], "review": ["defect", "clean"]}},
}
CASE = {"review": {mode: {
    "defect": {"expect": "request-changes", "defects": [{"id": "bug"}]},
    "clean": {"expect": "approve"},
} for mode in ("clean", "local", "team")}}
CASE_BODY = json.dumps(CASE)
META = {
    "evaluator_sha256": "e" * 64,
    "requested_providers": {"author": "claude", "review": ["claude", "codex"]},
    "requested_models": {"claude": "c", "codex": "x"},
    "review_report_language": "en",
    "case_files": {f"{stack}/case.json": hashlib.sha256(CASE_BODY.encode()).hexdigest()
                   for stack in ("web", "rn", "app", "backend")},
    "kit_digests": {"before": "b" * 64, "after": "a" * 64},
}


@pytest.fixture
def gate(tmp_path_factory, monkeypatch):
    spec = importlib.util.spec_from_file_location("phase_eval_compare", ROOT / "evals/phase_eval_compare.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = tmp_path_factory.mktemp("case-contracts")
    for stack in ("web", "rn", "app", "backend"):
        (cases / stack).mkdir()
        (cases / stack / "case.json").write_text(CASE_BODY, encoding="utf-8")
    monkeypatch.setattr(module, "DEFAULT_CASES", cases, raising=False)
    return module


def usage(uncached=100, output=10):
    return {"input": uncached * 5, "uncached_input": uncached, "cached_input": uncached * 4, "output": output}


def document(observed, inline=False):
    return {"path": "/p/SKILL.md", "sha256": "s", "bytes": 1, "inline_in_prompt": inline, "read_path_observed": observed}


def rows_for(kit, trials=3, stack="web", mode="clean", author="claude", reviewers=("claude", "codex")):
    rows = []
    for trial in range(1, trials + 1):
        rows.append({
            "scenario": "author", "stack": stack, "mode": mode, "kit": kit, "trial": trial, "provider": author,
            "required_read": ["a", "b"], "required_unread": [], "error": None,
            "oracle": {"behavior": True, "plan": True, "norm": True}, "usage": usage(),
        })
        for variant, expect in (("defect", "request-changes"), ("clean", "approve")):
            rows.append({
                "scenario": "review", "stack": stack, "mode": mode, "variant": variant, "kit": kit, "trial": trial,
                "valid": True, "expected": expect, "overall": expect, "correct": True,
                "defects": {"bug": True} if variant == "defect" else {},
                "false_request_changes": 0 if variant == "clean" else None, "subprocesses": len(reviewers),
                "usage": usage(),
                "angles": [
                    {"provider": provider, "documents": (
                        [document(True), document(None, inline=True)] if provider == "claude"
                        else [document(None), document(None)]
                    )}
                    for provider in reviewers
                ],
            })
    return rows


def write_run(directory: Path, rows, meta=None, units=None):
    directory.mkdir(parents=True)
    meta = {**(meta or META), "units": len(rows) if units is None else units}
    (directory / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (directory / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return directory


def run_gate(gate, tmp_path, before, after, thresholds=THRESHOLDS):
    run = write_run(tmp_path / "run", before + after)
    _, failures = gate.compare([run], "before", "after", thresholds)
    return failures


def test_equal_kits_meeting_thresholds_pass(gate, tmp_path):
    assert run_gate(gate, tmp_path, rows_for("before"), rows_for("after")) == []


def test_after_kit_below_absolute_floor_fails_even_without_a_drop(gate, tmp_path):
    before, after = rows_for("before"), rows_for("after")
    for rows in (before, after):
        rows[0]["required_read"], rows[0]["required_unread"] = ["a"], ["b"]
    failures = run_gate(gate, tmp_path, before, after)
    assert any("author_required_read_rate" in failure and "0.95" in failure for failure in failures)


def test_drop_against_previous_kit_fails_without_a_floor(gate, tmp_path):
    after = rows_for("after")
    for row in after:
        for angle in row.get("angles", []):
            if angle["provider"] == "claude":
                angle["documents"][0]["read_path_observed"] = False
    failures = run_gate(gate, tmp_path, rows_for("before"), after)
    assert any("reviewer_read_rate" in failure for failure in failures)


def test_unobserved_codex_reads_and_inline_bodies_do_not_count_as_unread(gate, tmp_path):
    run = write_run(tmp_path / "run", rows_for("after"))
    rows = [json.loads(line) for line in (run / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert gate.kit_metrics(rows, "after")["reviewer_read_rate"] == 1.0


@pytest.mark.parametrize("uncached, output, allowed", [
    (105, 11, True),
    (106, 10, False),
    (100, 12, False),
])
def test_token_ratio_limits(gate, tmp_path, uncached, output, allowed):
    after = rows_for("after")
    for row in after:
        row["usage"] = usage(uncached, output)
    failures = run_gate(gate, tmp_path, rows_for("before"), after)
    assert (not any("token" in failure for failure in failures)) is allowed


@pytest.mark.parametrize("before_invalid", [False, True], ids=["counterpart-valid", "counterpart-invalid"])
def test_valid_unit_without_usage_fails_even_when_its_counterpart_is_invalid(gate, tmp_path, before_invalid):
    before, after = rows_for("before"), rows_for("after")
    if before_invalid:
        before[1].update(valid=False, correct=None, error="provider failed")
    after[1]["usage"] = {"input": None, "uncached_input": None, "cached_input": None, "output": None}
    failures = run_gate(gate, tmp_path, before, after)
    assert any("usage" in failure for failure in failures)


def test_fewer_trials_than_required_fails(gate, tmp_path):
    failures = run_gate(gate, tmp_path, rows_for("before"), rows_for("after", trials=2))
    assert any("trials" in failure for failure in failures)


@pytest.mark.parametrize("invalid_kit", ["before", "after"])
def test_invalid_rows_above_ceiling_fail_and_are_excluded_from_accuracy(gate, tmp_path, invalid_kit):
    before, after = rows_for("before"), rows_for("after")
    rows = before if invalid_kit == "before" else after
    rows[1].update(valid=False, correct=None, error="provider failed")
    rows[2].update(valid=False, correct=None, error="provider failed")
    failures = run_gate(gate, tmp_path, before, after)
    assert any(f"invalid_rate on {invalid_kit}" in failure for failure in failures)
    assert not any("review_correct_rate" in failure for failure in failures)


@pytest.mark.parametrize("invalid_kit", ["before", "after"])
@pytest.mark.parametrize("invalid_count, allowed", [(3, True), (4, False)], ids=["at-ceiling", "above-ceiling"])
def test_invalid_rate_ceiling_controls_export_for_each_kit(gate, tmp_path, invalid_kit, invalid_count, allowed):
    before, after = rows_for("before", trials=10), rows_for("after", trials=10)
    rows = before if invalid_kit == "before" else after
    for row in rows[:invalid_count]:
        row["error"] = "provider failed"
        if row["scenario"] == "review":
            row.update(valid=False, correct=None)
    run = write_run(tmp_path / "run", before + after)
    destination = tmp_path / "record"
    _, failures = gate.compare([run], "before", "after", THRESHOLDS, export=destination)
    if allowed:
        assert failures == []
        assert (destination / "results.jsonl.gz").is_file()
    else:
        assert any(f"invalid_rate on {invalid_kit}" in failure for failure in failures)
        assert not destination.exists()


def test_baseline_quality_can_improve_under_after_only_ceilings(gate, tmp_path):
    before = rows_for("before")
    before[2].update(overall="request-changes", correct=False, false_request_changes=1)
    thresholds = {**THRESHOLDS, "ceilings": {**THRESHOLDS["ceilings"], "false_request_changes_rate": 0.0}}
    assert run_gate(gate, tmp_path, before, rows_for("after"), thresholds) == []


def test_runs_measured_under_different_conditions_are_not_compared(gate, tmp_path):
    before = write_run(tmp_path / "before", rows_for("before"))
    other = copy.deepcopy(META)
    other["evaluator_sha256"] = "d" * 64
    after = write_run(tmp_path / "after", rows_for("after"), meta=other)
    _, failures = gate.compare([before, after], "before", "after", THRESHOLDS)
    assert any("evaluator_sha256" in failure for failure in failures)


def test_missing_scenario_on_after_kit_is_unmeasured(gate, tmp_path):
    after = [row for row in rows_for("after") if row["scenario"] == "review"]
    failures = run_gate(gate, tmp_path, rows_for("before"), after)
    assert any("author_required_read_rate" in failure and "unmeasured" in failure for failure in failures)


def test_shipped_thresholds_require_the_full_release_matrix(gate, tmp_path):
    thresholds = json.loads((ROOT / "evals/phase-eval-thresholds.json").read_text(encoding="utf-8"))
    matrix = thresholds["matrix"]
    full = {kit: [row for stack in matrix["stacks"] for mode in matrix["modes"]
                  for row in rows_for(kit, stack=stack, mode=mode)] for kit in ("before", "after")}
    assert run_gate(gate, tmp_path / "full", full["before"], full["after"], thresholds) == []
    failures = run_gate(gate, tmp_path / "web", rows_for("before"), rows_for("after"), thresholds)
    assert any("backend" in failure and "missing" in failure for failure in failures)


def test_trials_are_required_per_combination_not_per_scenario(gate, tmp_path):
    before = rows_for("before") + rows_for("before", stack="backend")
    after = rows_for("after") + rows_for("after", trials=1, stack="backend")
    failures = run_gate(gate, tmp_path, before, after)
    assert any("backend" in failure and "1 trials" in failure for failure in failures)


def test_combination_missing_on_one_kit_fails(gate, tmp_path):
    before = rows_for("before") + rows_for("before", stack="backend")
    failures = run_gate(gate, tmp_path, before, rows_for("after"))
    assert any("backend" in failure for failure in failures)


def test_results_shorter_than_meta_units_fail_as_incomplete(gate, tmp_path):
    run = write_run(tmp_path / "run", rows_for("before") + rows_for("after"), units=100)
    _, failures = gate.compare([run], "before", "after", THRESHOLDS)
    assert any("incomplete" in failure for failure in failures)


def test_truncated_results_line_is_a_failure_not_a_crash(gate, tmp_path):
    run = write_run(tmp_path / "run", rows_for("before") + rows_for("after"))
    with (run / "results.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"scenario": "review", "kit": "af')
    _, failures = gate.compare([run], "before", "after", THRESHOLDS)
    assert any("unparseable" in failure for failure in failures)


def test_duplicate_unit_fails(gate, tmp_path):
    after = rows_for("after")
    failures = run_gate(gate, tmp_path, rows_for("before"), after + [copy.deepcopy(after[0])])
    assert any("duplicate" in failure for failure in failures)


@pytest.mark.parametrize("missing", ["evaluator_sha256", "requested_providers", "requested_models", "case_files"])
def test_single_run_without_measurement_provenance_fails(gate, tmp_path, missing):
    meta = {key: value for key, value in META.items() if key != missing}
    run = write_run(tmp_path / "run", rows_for("before") + rows_for("after"), meta=meta)
    original = {name: (run / name).read_bytes() for name in ("meta.json", "results.jsonl")}
    destination = tmp_path / "record"
    _, failures = gate.compare([run], "before", "after", THRESHOLDS, export=destination)
    assert any(missing in failure for failure in failures)
    assert not destination.exists()
    assert {name: (run / name).read_bytes() for name in original} == original


def test_incomplete_oracle_counts_as_invalid_instead_of_leaving_the_denominator(gate, tmp_path):
    after = rows_for("after")
    after[0]["oracle"] = {"behavior": True, "plan": None, "norm": True}
    failures = run_gate(gate, tmp_path, rows_for("before"), after)
    assert any("invalid_rate" in failure for failure in failures)


def test_missing_false_request_changes_is_unmeasured_not_zero(gate, tmp_path):
    after = rows_for("after")
    for row in after:
        if row.get("expected") == "approve":
            row.pop("false_request_changes")
    failures = run_gate(gate, tmp_path, rows_for("before"), after)
    assert any("false_request_changes_rate" in failure and "unmeasured" in failure for failure in failures)


def test_unknown_threshold_metric_is_reported_not_raised(gate, tmp_path):
    thresholds = {**THRESHOLDS, "floors": {**THRESHOLDS["floors"], "author_read_rate": 0.9}}
    failures = run_gate(gate, tmp_path, rows_for("before"), rows_for("after"), thresholds)
    assert any("author_read_rate" in failure and "unknown" in failure for failure in failures)


def kit_tree(root: Path, body: str) -> Path:
    (root / "skills" / "guide").mkdir(parents=True)
    (root / "skills" / "guide" / "SKILL.md").write_text(body, encoding="utf-8")
    return root


def digest_meta(gate, before_kit: Path, after_kit: Path) -> dict:
    return {**META, "kit_digests": {"before": gate.kit_digest(before_kit), "after": gate.kit_digest(after_kit)}}


def test_export_is_a_recomputable_record_without_local_paths_or_reviewer_text(gate, tmp_path):
    before_kit, after_kit = kit_tree(tmp_path / "old", "old"), kit_tree(tmp_path / "new", "new")
    rows = rows_for("before") + rows_for("after")
    for row in rows:
        row["execution"] = {"cwd": "/Users/someone/private"}
        for angle in row.get("angles", []):
            angle["text"] = "reviewer output"
    run = write_run(tmp_path / "run", rows, meta={**digest_meta(gate, before_kit, after_kit), "kits": {"after": "/Users/someone"}})
    record = tmp_path / "record"
    original, failures = gate.compare([run], "before", "after", THRESHOLDS, export=record)
    assert failures == []
    raw = (record / "meta.json").read_text(encoding="utf-8") + gzip.decompress((record / "results.jsonl.gz").read_bytes()).decode()
    assert "/Users/someone" not in raw and "reviewer output" not in raw
    recomputed, failures = gate.compare([record], "before", "after", THRESHOLDS,
                                        require={"after": after_kit, "before": before_kit})
    assert failures == []
    assert recomputed[4:] == original[4:]


def test_export_is_refused_when_the_gate_fails(gate, tmp_path):
    run = write_run(tmp_path / "run", rows_for("before") + rows_for("after", trials=2))
    _, failures = gate.compare([run], "before", "after", THRESHOLDS, export=tmp_path / "record")
    assert failures and not (tmp_path / "record").exists()


@pytest.mark.parametrize("kit", ["after", "before"])
def test_record_for_a_different_kit_than_the_tag_fails(gate, tmp_path, kit):
    measured = {"before": kit_tree(tmp_path / "old", "old"), "after": kit_tree(tmp_path / "new", "new")}
    run = write_run(tmp_path / "run", rows_for("before") + rows_for("after"),
                    meta=digest_meta(gate, measured["before"], measured["after"]))
    tagged = {**measured, kit: kit_tree(tmp_path / "tag", "edited after measurement")}
    _, failures = gate.compare([run], "before", "after", THRESHOLDS, require=tagged)
    assert any("kit digest" in failure and kit in failure for failure in failures)


def test_required_kit_without_recorded_digest_fails(gate, tmp_path):
    run = write_run(tmp_path / "run", rows_for("before") + rows_for("after"))
    _, failures = gate.compare([run], "before", "after", THRESHOLDS, require={"after": kit_tree(tmp_path / "k", "x")})
    assert any("kit digest" in failure for failure in failures)


def _drop(row, key):
    row.pop(key)


@pytest.mark.parametrize("index, damage", [
    (0, lambda row: _drop(row, "required_read")),
    (1, lambda row: _drop(row, "correct")),
    (1, lambda row: row.update(correct=float("nan"))),
    (1, lambda row: _drop(row, "defects")),
    (1, lambda row: row["angles"][0]["documents"][0].update(read_path_observed="yes")),
    (2, lambda row: row.update(usage={**usage(), "uncached_input": -5})),
    (1, lambda row: _drop(row, "expected")),
    (0, lambda row: row.update(usage="n/a")),
], ids=["author-reads", "review-correct", "review-correct-nan", "defects", "read-observation", "negative-usage",
        "missing-expected", "usage-not-a-dict"])
def test_malformed_scoring_fields_fail_instead_of_shrinking_the_denominator(gate, tmp_path, index, damage):
    after = rows_for("after")
    damage(after[index])
    failures = run_gate(gate, tmp_path, rows_for("before"), after)
    assert any("malformed" in failure for failure in failures)


@pytest.mark.parametrize("line", ["[]", "1", '{"kit": "after"}'])
def test_result_line_that_is_not_a_result_row_is_a_failure_not_a_crash(gate, tmp_path, line):
    run = write_run(tmp_path / "run", rows_for("before") + rows_for("after"))
    with (run / "results.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    _, failures = gate.compare([run], "before", "after", THRESHOLDS)
    assert any("not a result row" in failure for failure in failures)


def test_interrupted_export_leaves_no_partial_record(gate, tmp_path, monkeypatch):
    run = write_run(tmp_path / "run", rows_for("before") + rows_for("after"))

    def disk_full(*args, **kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(gate.gzip, "compress", disk_full)
    with pytest.raises(OSError):
        gate.compare([run], "before", "after", THRESHOLDS, export=tmp_path / "record")
    assert not (tmp_path / "record").exists()
    assert list(tmp_path.iterdir()) == [tmp_path / "run"]


def evaluation_tree(root: Path, evaluator: str, case: str) -> Path:
    (root / "evals" / "phase-cases" / "web").mkdir(parents=True)
    (root / "evals" / "phase_eval.py").write_text(evaluator, encoding="utf-8")
    body = json.dumps({**CASE, "note": case})
    (root / "evals" / "phase-cases" / "web" / "case.json").write_text(body, encoding="utf-8")
    return root


@pytest.mark.parametrize("patch", [
    {"token_ratio_max": {"uncached_input": 1.05, "output": float("nan")}},
    {"token_ratio_max": {"uncached_input": 0, "output": 1.1}},
    {"floors": {"review_correct_rate": 1.5}},
    {"ceilings": {"invalid_rate": "0.1"}},
    {"min_trials": 0},
    {"min_trials": 2.5},
    {"floors": {"review_correct_rate": 10**400}},
    {"floors": []},
    {"matrix": []},
    {"no_drop": {"review_correct_rate": 1}},
    {"matrix": {**THRESHOLDS["matrix"], "stacks": "web"}},
], ids=["nan-limit", "zero-limit", "rate-above-one", "string-rate", "zero-trials", "fractional-trials",
        "huge-integer-rate", "floors-not-object", "matrix-not-object", "no-drop-not-list", "stacks-not-list"])
def test_invalid_threshold_values_are_reported_not_silently_disabling_a_check(gate, tmp_path, patch):
    failures = run_gate(gate, tmp_path, rows_for("before"), rows_for("after"), {**THRESHOLDS, **patch})
    assert any(failure.startswith("thresholds") for failure in failures)


def test_thresholds_that_are_not_an_object_are_reported(gate, tmp_path):
    run = write_run(tmp_path / "run", rows_for("before") + rows_for("after"))
    _, failures = gate.compare([run], "before", "after", [])
    assert any(failure.startswith("thresholds") for failure in failures)


@pytest.mark.parametrize("damage", ["meta-not-json", "meta-not-object", "corrupt-gzip-header", "corrupt-gzip-body"])
def test_unreadable_input_is_a_failure_not_a_crash(gate, tmp_path, damage):
    run = write_run(tmp_path / "run", rows_for("before") + rows_for("after"))
    if damage == "meta-not-json":
        (run / "meta.json").write_text("{", encoding="utf-8")
    elif damage == "meta-not-object":
        (run / "meta.json").write_text("[]", encoding="utf-8")
    else:
        body = gzip.compress((run / "results.jsonl").read_bytes(), mtime=0)
        if damage == "corrupt-gzip-header":
            body = b"not gzip"
        else:
            damaged = bytearray(body)
            for index in range(20, 60):
                damaged[index] ^= 0xFF
            body = bytes(damaged)
        (run / "results.jsonl").unlink()
        (run / "results.jsonl.gz").write_bytes(body)
    _, failures = gate.compare([run], "before", "after", THRESHOLDS)
    assert any("unreadable" in failure for failure in failures)


def test_row_with_unhashable_unit_fields_is_not_a_result_row(gate, tmp_path):
    run = write_run(tmp_path / "run", rows_for("before") + rows_for("after"))
    with (run / "results.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"scenario": "author", "stack": "web", "mode": "clean", "kit": ["after"], "trial": [1]}) + "\n")
    _, failures = gate.compare([run], "before", "after", THRESHOLDS)
    assert any("not a result row" in failure for failure in failures)


@pytest.mark.parametrize("changed, key", [("evaluator", "evaluator_sha256"), ("case", "case_files")])
def test_record_measured_with_another_evaluator_or_case_set_fails(gate, tmp_path, changed, key):
    measured = evaluation_tree(tmp_path / "measured", "evaluator", "case")
    run = write_run(tmp_path / "run", rows_for("before") + rows_for("after"),
                    meta={**META, **gate.evaluation_fingerprint(measured)})
    _, failures = gate.compare([run], "before", "after", THRESHOLDS, evaluator_tree=measured)
    assert failures == []
    tagged = evaluation_tree(tmp_path / "tagged", *(("changed", "case") if changed == "evaluator" else ("evaluator", "changed")))
    _, failures = gate.compare([run], "before", "after", THRESHOLDS, evaluator_tree=tagged)
    assert any(key in failure for failure in failures)


@pytest.mark.parametrize("damage", [
    lambda row: row.update(defects={}),
    lambda row: row.update(expected=None, correct=None, defects={}),
    lambda row: row.update(defects={"invented": True}),
], ids=["missing-defects", "removed-expectation", "substituted-defect"])
def test_review_denominators_cannot_be_changed_by_recorded_scoring_fields(gate, tmp_path, damage):
    after = rows_for("after")
    damage(after[1])
    failures = run_gate(gate, tmp_path, rows_for("before"), after)
    assert failures


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("damage", ["conflict", "missing", "malformed"])
def test_each_run_must_bind_its_rows_to_consistent_kit_digests(gate, tmp_path, reverse, damage):
    measured = kit_tree(tmp_path / "kit", "measured")
    actual = gate.kit_digest(measured)
    first_meta = {**META, "kit_digests": {"before": "b" * 64, "after": actual}}
    first_meta["kit_digests"] = dict(first_meta["kit_digests"])
    if damage == "missing":
        first_meta["kit_digests"].pop("after")
    else:
        first_meta["kit_digests"]["after"] = "f" * 64 if damage == "conflict" else 42
    second_meta = {**META, "kit_digests": {"after": actual}}
    after = rows_for("after")
    first = write_run(tmp_path / "first", rows_for("before") + after[:3], meta=first_meta)
    second = write_run(tmp_path / "second", after[3:], meta=second_meta)
    directories = [second, first] if reverse else [first, second]
    _, failures = gate.compare(directories, "before", "after", THRESHOLDS, require={"after": measured},
                               export=tmp_path / "record")
    assert failures
    assert not (tmp_path / "record").exists()


@pytest.mark.parametrize("invalid", [False, True])
def test_relabeling_a_trial_cannot_remove_it_from_paired_token_comparison(gate, tmp_path, invalid):
    after = rows_for("after")
    after[7].update(trial=4, usage=usage(10000, 1000))
    if invalid:
        after[7].update(valid=False, error="provider failed")
    thresholds = {**THRESHOLDS, "ceilings": {"invalid_rate": 0.2}}
    assert run_gate(gate, tmp_path, rows_for("before"), after, thresholds)


@pytest.mark.parametrize("models", [
    {"claude": "c"},
    {"codex": "x"},
    {"claude": "c", "codex": 1},
    {"claude": True, "codex": "x"},
    {"claude": "c", "codex": "  "},
])
def test_enabled_provider_model_names_are_required_to_compare_a_measurement(gate, tmp_path, models):
    run = write_run(tmp_path / "run", rows_for("before") + rows_for("after"),
                    meta={**META, "requested_models": models})
    _, failures = gate.compare([run], "before", "after", THRESHOLDS, export=tmp_path / "record")
    assert failures
    assert not (tmp_path / "record").exists()


def test_explicitly_unscored_case_remains_valid_without_weakening_other_cases(gate, tmp_path):
    case = copy.deepcopy(CASE)
    case["review"]["local"]["clean"]["expect"] = None
    body = json.dumps(case)
    (gate.DEFAULT_CASES / "web" / "case.json").write_text(body, encoding="utf-8")
    meta = copy.deepcopy(META)
    meta["case_files"]["web/case.json"] = hashlib.sha256(body.encode()).hexdigest()
    rows = []
    for kit in ("before", "after"):
        rows.extend(rows_for(kit))
        local = rows_for(kit, mode="local")
        for row in local:
            if row.get("variant") == "clean":
                row.update(expected=None, correct=None)
        rows.extend(local)
    run = write_run(tmp_path / "run", rows, meta=meta)
    _, failures = gate.compare([run], "before", "after", THRESHOLDS)
    assert failures == []


def test_case_changed_after_measurement_cannot_redefine_the_review_denominator(gate, tmp_path):
    case = copy.deepcopy(CASE)
    case["review"]["clean"]["defect"]["defects"] = []
    (gate.DEFAULT_CASES / "web" / "case.json").write_text(json.dumps(case), encoding="utf-8")
    assert run_gate(gate, tmp_path, rows_for("before"), rows_for("after"))


def test_invalid_trial_with_the_same_identity_keeps_the_existing_pair_exclusion_policy(gate, tmp_path):
    after = rows_for("after")
    after[7].update(valid=False, error="provider failed", usage=usage(10000, 1000))
    thresholds = {**THRESHOLDS, "ceilings": {"invalid_rate": 0.2}}
    assert run_gate(gate, tmp_path, rows_for("before"), after, thresholds) == []


def test_separate_runs_with_matching_models_cases_and_kit_digests_can_pass(gate, tmp_path):
    before = write_run(tmp_path / "before", rows_for("before"),
                       meta={**META, "kit_digests": {"before": "b" * 64}})
    after = write_run(tmp_path / "after", rows_for("after"),
                      meta={**META, "kit_digests": {"after": "a" * 64}})
    _, failures = gate.compare([before, after], "before", "after", THRESHOLDS)
    assert failures == []


@pytest.mark.parametrize("roles", [
    None,
    {},
    {"author": "codex"},
    {"author": "unknown", "review": ["codex"]},
    {"author": ["codex"], "review": ["codex"]},
    {"author": "codex", "review": []},
    {"author": "codex", "review": "codex"},
    {"author": "codex", "review": ["unknown"]},
    {"author": "codex", "review": ["codex", "codex"]},
    {"author": "codex", "review": [{}]},
])
def test_unknown_provider_roles_refuse_comparison_and_export(gate, tmp_path, roles):
    run = write_run(tmp_path / "run", rows_for("before") + rows_for("after"),
                    meta={**META, "requested_providers": roles})
    destination = tmp_path / "record"
    _, failures = gate.compare([run], "before", "after", THRESHOLDS, export=destination)
    assert any("measurement condition unknown" in failure and "requested_providers" in failure
               for failure in failures)
    assert not destination.exists()


@pytest.mark.parametrize("role", ["author", "review"])
def test_provider_roles_cannot_change_between_measured_kits(gate, tmp_path, role):
    before = write_run(tmp_path / "before", rows_for("before"))
    meta = copy.deepcopy(META)
    meta["requested_providers"][role] = "codex" if role == "author" else ["codex"]
    after_rows = rows_for("after", author="codex" if role == "author" else "claude",
                          reviewers=("codex",) if role == "review" else ("claude", "codex"))
    after = write_run(tmp_path / "after", after_rows, meta=meta)
    destination = tmp_path / "record"
    _, failures = gate.compare([before, after], "before", "after", THRESHOLDS, export=destination)
    assert any("measurement condition differs" in failure and "requested_providers" in failure
               for failure in failures)
    assert not destination.exists()


@pytest.mark.parametrize("index, damage", [
    (0, lambda row: row.pop("provider")),
    (0, lambda row: row.update(provider="codex")),
    (1, lambda row: row["angles"][0].update(provider="unknown")),
    (1, lambda row: row["angles"][0].update(provider="codex")),
    (1, lambda row: row.update(angles=[], subprocesses=1)),
    (1, lambda row: row.update(subprocesses=3)),
    (1, lambda row: row.pop("subprocesses")),
    (1, lambda row: row.update(angles=row["angles"] + [copy.deepcopy(row["angles"][0])], subprocesses=3)),
], ids=["missing-author", "changed-author", "unknown-reviewer", "missing-reviewer",
        "empty-reviewers", "changed-subprocesses", "missing-subprocesses", "unbalanced-reviewers"])
def test_scored_provider_mismatch_cannot_export(gate, tmp_path, index, damage):
    after = rows_for("after")
    damage(after[index])
    run = write_run(tmp_path / "run", rows_for("before") + after)
    destination = tmp_path / "record"
    _, failures = gate.compare([run], "before", "after", THRESHOLDS, export=destination)
    assert any("provider configuration mismatch" in failure for failure in failures)
    assert not destination.exists()


@pytest.mark.parametrize("model", [None, "", "  ", 1, True])
def test_codex_only_requires_an_explicit_codex_model(gate, tmp_path, model):
    meta = {**META, "requested_providers": {"author": "codex", "review": ["codex"]},
            "requested_models": {"claude": None, "codex": model}}
    rows = [row for kit in ("before", "after") for row in rows_for(kit, author="codex", reviewers=("codex",))]
    run = write_run(tmp_path / "run", rows, meta=meta)
    destination = tmp_path / "record"
    _, failures = gate.compare([run], "before", "after", THRESHOLDS, export=destination)
    assert any("measurement condition unknown" in failure and "requested_models" in failure for failure in failures)
    assert not destination.exists()


def test_codex_only_full_matrix_exports_a_recomputable_private_record(gate, tmp_path):
    thresholds = json.loads((ROOT / "evals/phase-eval-thresholds.json").read_text(encoding="utf-8"))
    matrix = thresholds["matrix"]
    roles = {"author": "codex", "review": ["codex"]}
    models = {"claude": None, "codex": "gpt-6-astra"}
    rows = [
        row for kit in ("before", "after") for stack in matrix["stacks"] for mode in matrix["modes"]
        for row in rows_for(kit, stack=stack, mode=mode, author="codex", reviewers=("codex",))
    ]
    for row in rows:
        row["execution"] = {"cwd": "/private/eval", "argv": ["secret-cli"]}
        for angle in row.get("angles", []):
            angle["text"] = "private reviewer response"
            angle["documents"] = [document(True), document(None), document(False, inline=True)]
    meta = {**META, "requested_providers": roles, "requested_models": models}
    run = write_run(tmp_path / "run", rows, meta=meta)
    destination = tmp_path / "record"
    original, failures = gate.compare([run], "before", "after", thresholds, export=destination)
    assert failures == []
    exported_meta = json.loads((destination / "meta.json").read_text(encoding="utf-8"))
    assert exported_meta["requested_providers"] == roles
    assert exported_meta["requested_models"] == models
    body = gzip.decompress((destination / "results.jsonl.gz").read_bytes()).decode()
    assert all(secret not in body for secret in ("/private/eval", "/p/SKILL.md", "secret-cli", "private reviewer response"))
    exported = [json.loads(line) for line in body.splitlines()]
    assert {row["provider"] for row in exported if row["scenario"] == "author"} == {"codex"}
    recomputed, failures = gate.compare([destination], "before", "after", thresholds)
    assert failures == []
    assert recomputed[4:] == original[4:]


@pytest.mark.parametrize("observed", [False, None])
def test_codex_only_false_or_unknown_reads_cannot_pass_the_strict_gate(gate, tmp_path, observed):
    roles = {"author": "codex", "review": ["codex"]}
    rows = [row for kit in ("before", "after") for row in rows_for(kit, author="codex", reviewers=("codex",))]
    for row in rows:
        for angle in row.get("angles", []):
            angle["documents"] = [document(observed)]
    run = write_run(tmp_path / "run", rows, meta={
        **META, "requested_providers": roles, "requested_models": {"claude": None, "codex": "gpt-6-astra"},
    })
    destination = tmp_path / "record"
    _, failures = gate.compare([run], "before", "after", THRESHOLDS, export=destination)
    assert any("reviewer_read_rate" in failure for failure in failures)
    assert not destination.exists()


def test_provider_failure_rows_remain_invalid_and_are_preserved_in_export(gate, tmp_path):
    rows = [row for kit in ("before", "after") for row in rows_for(kit, trials=10)]
    rows[0].update(error="author unavailable")
    rows[0].pop("provider")
    rows[1].update(valid=False, error="review unavailable", angles=[], subprocesses=0)
    run = write_run(tmp_path / "run", rows)
    destination = tmp_path / "record"
    _, failures = gate.compare([run], "before", "after", THRESHOLDS, export=destination)
    assert failures == []
    exported, failures, _, _ = gate.load([destination])
    assert failures == []
    assert gate.kit_metrics(exported, "before")["invalid_rate"] == 2 / 30
    assert exported[0]["error"] is True
    assert exported[1]["valid"] is False and exported[1]["error"] is True
