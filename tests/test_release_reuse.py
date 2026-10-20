"""릴리스 기록 재사용: 모델 입력이 같은 셀만, 같은 조건의 이전 기록 행을 그대로, 새 앵커 측정과 함께."""

from __future__ import annotations

import copy
import gzip
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "evals"))

import release_reuse as reuse  # noqa: E402

PREVIOUS = REPO / "evals" / "release-gates" / "v0.3.19"
NEW_DIGEST = "a" * 64


def _rows(directory: Path, name: str = "results.jsonl.gz") -> list[dict]:
    with gzip.open(directory / name, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _plan(previous_meta: dict, unchanged: bool = True) -> dict:
    thresholds = json.loads(reuse.THRESHOLDS.read_text(encoding="utf-8"))
    conditions = {"evaluator_sha256": previous_meta["evaluator_sha256"], "case_files": previous_meta["case_files"]}
    return {
        "version": reuse.FINGERPRINT_VERSION,
        "providers": previous_meta["requested_providers"],
        "conditions": {"previous": conditions, "kit": copy.deepcopy(conditions)},
        "cells": {
            reuse.cell_id(*cell): {"previous": {"fingerprint": "f"}, "kit": {"fingerprint": "f"}, "unchanged": unchanged}
            for cell in reuse.matrix_cells(thresholds)
        },
    }


def _anchors(tmp_path: Path, previous_meta: dict) -> Path:
    """One passing new-kit row per stack and scenario, taken from the previous record's rows."""
    chosen = []
    after = [row for row in _rows(PREVIOUS) if row["kit"] == "after"]
    for stack in ("web", "rn", "app", "backend"):
        for scenario, variant in reuse.ANCHOR_SCENARIOS:
            for row in after:
                if row["stack"] != stack or row["scenario"] != scenario or (row.get("variant") or "") != variant:
                    continue
                if scenario == "author" and not all(row["oracle"].values()):
                    continue
                if scenario == "review" and (row.get("expected") is None or not row.get("correct")):
                    continue
                chosen.append(row)
                break
    directory = tmp_path / "anchors"
    directory.mkdir()
    meta = {**reuse._condition(previous_meta), "kit_digests": {"after": NEW_DIGEST}}
    (directory / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (directory / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in chosen), encoding="utf-8")
    return directory


@pytest.fixture
def setting(tmp_path):
    previous = tmp_path / "previous"
    shutil.copytree(PREVIOUS, previous)
    previous_meta = json.loads((previous / "meta.json").read_text(encoding="utf-8"))
    return previous, previous_meta, _anchors(tmp_path, previous_meta)


def test_an_unchanged_release_reuses_every_cell_and_passes_the_comparator(tmp_path, setting):
    previous, meta, anchors = setting
    lines, failures = reuse.assemble(_plan(meta), previous, None, anchors, tmp_path / "record")
    assert failures == []
    assert lines[-1] == "Reused 36 of 36 cells from the previous release and the `before` side of 0 more."
    record = tmp_path / "record"
    assert {path.name for path in record.iterdir()} == {
        "meta.json", "results.jsonl.gz", "anchors.jsonl.gz", "anchor-meta.json", "reuse.json"}
    rows = _rows(record)
    assert len(rows) == 2 * len([row for row in _rows(previous) if row["kit"] == "after"])
    assert json.loads((record / "meta.json").read_text())["kit_digests"] == {
        "before": meta["kit_digests"]["after"], "after": NEW_DIGEST}


def test_verify_accepts_the_assembled_record(tmp_path, setting):
    previous, meta, anchors = setting
    plan = _plan(meta)
    reuse.assemble(plan, previous, None, anchors, tmp_path / "record")
    failures = reuse.verify(tmp_path / "record", previous, tmp_path, tmp_path, 1, NEW_DIGEST,
                            planner=lambda *args: plan)
    assert failures == []


def test_verify_rejects_a_reused_row_that_was_edited(tmp_path, setting):
    previous, meta, anchors = setting
    plan = _plan(meta)
    reuse.assemble(plan, previous, None, anchors, tmp_path / "record")
    path = tmp_path / "record" / "results.jsonl.gz"
    rows = _rows(tmp_path / "record")
    target = next(row for row in rows if row["kit"] == "after" and row["scenario"] == "review"
                  and row.get("expected") == "request-changes")
    target["defects"] = {key: True for key in target["defects"]} | {"extra": True}
    path.write_bytes(gzip.compress("".join(json.dumps(row) + "\n" for row in rows).encode()))
    failures = reuse.verify(tmp_path / "record", previous, tmp_path, tmp_path, 1, NEW_DIGEST,
                            planner=lambda *args: plan)
    assert any("rows are not the previous release's rows" in failure for failure in failures)


def test_verify_rejects_a_cell_whose_inputs_changed_since_assembly(tmp_path, setting):
    previous, meta, anchors = setting
    reuse.assemble(_plan(meta), previous, None, anchors, tmp_path / "record")
    changed = _plan(meta)
    changed["cells"]["author/web/clean/-"]["unchanged"] = False
    failures = reuse.verify(tmp_path / "record", previous, tmp_path, tmp_path, 1, NEW_DIGEST,
                            planner=lambda *args: changed)
    assert "author/web/clean/-: model inputs changed; it cannot be reused" in failures


def test_changed_cells_must_be_measured(tmp_path, setting):
    previous, meta, anchors = setting
    plan = _plan(meta)
    plan["cells"]["review/rn/local/defect"]["unchanged"] = False
    _, failures = reuse.assemble(plan, previous, None, anchors, tmp_path / "record")
    assert failures == ["cells neither reused nor measured: ['review/rn/local/defect']"]
    assert not (tmp_path / "record").exists()


def test_a_different_evaluator_or_model_blocks_all_reuse(tmp_path, setting):
    previous, meta, anchors = setting
    anchor_meta = json.loads((anchors / "meta.json").read_text())
    anchor_meta["requested_models"] = {**anchor_meta["requested_models"], "codex": "another-model"}
    (anchors / "meta.json").write_text(json.dumps(anchor_meta))
    _, failures = reuse.assemble(_plan(meta), previous, None, anchors, tmp_path / "record")
    assert any("anchor run's condition" in failure for failure in failures)
    plan = _plan(meta)
    plan["conditions"]["kit"]["evaluator_sha256"] = "0" * 64
    _, failures = reuse.assemble(plan, previous, None, anchors, tmp_path / "record2")
    assert any("evaluator_sha256 differs" in failure for failure in failures)


def test_a_missing_or_failing_anchor_blocks_reuse(tmp_path, setting):
    previous, meta, anchors = setting
    rows = [json.loads(line) for line in (anchors / "results.jsonl").read_text().splitlines()]
    kept = [row for row in rows if not (row["stack"] == "app" and row["scenario"] == "author")]
    defect = next(row for row in kept if row["stack"] == "web" and row.get("variant") == "defect")
    defect["defects"] = {key: False for key in defect["defects"]}
    (anchors / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in kept))
    _, failures = reuse.assemble(_plan(meta), previous, None, anchors, tmp_path / "record")
    assert "no anchor for author/app/-" in failures
    assert any("anchor review/web/defect" in failure and "missed a seeded defect" in failure for failure in failures)


def test_normalisation_hides_run_identity_but_not_content():
    replacements = {"/tmp/project": "<project>"}
    one = reuse._normalise(f"/tmp/project/x {'a' * 40} {'b' * 64} 2026-10-20T09:39:45.0+00:00 {'c' * 32}", replacements)
    two = reuse._normalise(f"/tmp/project/x {'d' * 40} {'e' * 64} 2027-01-01T00:00:00Z {'f' * 32}", replacements)
    assert one == two == "<project>/x <oid> <sha256> <time> <nonce>"
    assert reuse._normalise("Prefer small modules.", {}) != reuse._normalise("Prefer large modules.", {})


def _changed_cell_run(tmp_path: Path, meta: dict, cell: str, kits: tuple[str, ...]) -> Path:
    """A fresh measurement of one cell, built from the previous record's rows for that cell."""
    rows = [row for row in _rows(PREVIOUS) if row["kit"] == "after" and reuse.row_cell(row) == cell]
    directory = tmp_path / "run"
    directory.mkdir()
    digests = {"before": meta["kit_digests"]["after"], "after": NEW_DIGEST}
    (directory / "meta.json").write_text(json.dumps({**reuse._condition(meta), "kit_digests": digests}))
    body = "".join(json.dumps({**row, "kit": kit}) + "\n" for row in rows for kit in kits)
    (directory / "results.jsonl").write_text(body)
    return directory


def _with_previous_kit_anchors(anchors: Path, meta: dict, token_scale: float = 1.0) -> None:
    rows = [json.loads(line) for line in (anchors / "results.jsonl").read_text().splitlines()]
    before = []
    for row in rows:
        copy_row = copy.deepcopy(row)
        copy_row["kit"] = "before"
        copy_row["usage"] = {key: int(value * token_scale) for key, value in copy_row["usage"].items()}
        before.append(copy_row)
    (anchors / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows + before))
    anchor_meta = json.loads((anchors / "meta.json").read_text())
    anchor_meta["kit_digests"]["before"] = meta["kit_digests"]["after"]
    (anchors / "meta.json").write_text(json.dumps(anchor_meta))


def test_a_changed_cell_measured_on_both_kits_is_combined_with_reused_cells(tmp_path, setting):
    previous, meta, anchors = setting
    plan = _plan(meta)
    plan["cells"]["author/web/clean/-"]["unchanged"] = False
    run = _changed_cell_run(tmp_path, meta, "author/web/clean/-", ("before", "after"))
    lines, failures = reuse.assemble(plan, previous, run, anchors, tmp_path / "record")
    assert failures == []
    assert lines[-1].startswith("Reused 35 of 36 cells")


def test_a_new_kit_only_cell_needs_reuse_before_and_drift_free_previous_kit_anchors(tmp_path, setting):
    previous, meta, anchors = setting
    plan = _plan(meta)
    plan["cells"]["author/web/clean/-"]["unchanged"] = False
    run = _changed_cell_run(tmp_path, meta, "author/web/clean/-", ("after",))
    _, failures = reuse.assemble(plan, previous, run, anchors, tmp_path / "r1")
    assert failures == ["cells measured without `before`: ['author/web/clean/-']"]
    _, failures = reuse.assemble(plan, previous, run, anchors, tmp_path / "r2", reuse_before=True)
    assert "the anchor run did not measure the previous kit as `before`" in failures
    _with_previous_kit_anchors(anchors, meta)
    lines, failures = reuse.assemble(plan, previous, run, anchors, tmp_path / "r3", reuse_before=True)
    assert failures == []
    assert lines[-1].endswith("the `before` side of 1 more.")
    assert reuse.verify(tmp_path / "r3", previous, tmp_path, tmp_path, 1, NEW_DIGEST,
                        planner=lambda *args: plan) == []


def test_token_drift_on_the_previous_kit_blocks_before_reuse(tmp_path, setting):
    previous, meta, anchors = setting
    plan = _plan(meta)
    plan["cells"]["author/web/clean/-"]["unchanged"] = False
    run = _changed_cell_run(tmp_path, meta, "author/web/clean/-", ("after",))
    _with_previous_kit_anchors(anchors, meta, token_scale=1.6)
    _, failures = reuse.assemble(plan, previous, run, anchors, tmp_path / "record", reuse_before=True)
    assert any("tokens drifted" in failure for failure in failures)


def test_a_plan_for_other_providers_cannot_reuse(tmp_path, setting):
    previous, meta, anchors = setting
    plan = _plan(meta)
    plan["providers"] = {"author": "claude", "review": ["claude", "codex"]}
    _, failures = reuse.assemble(plan, previous, None, anchors, tmp_path / "record")
    assert any("the plan rendered prompts for" in failure for failure in failures)
