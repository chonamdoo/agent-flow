"""Reuse a previous release's phase_eval rows for cells whose model inputs did not change.

A release re-measures 168 units even when the release changed nothing a model sees. This
tool fingerprints, per release-matrix cell (scenario × stack × mode × review variant),
everything the evaluation feeds a model and the kit code it runs on the way:

- the rendered prompt(s), with machine paths replaced by placeholders;
- the installed project files (skills, templates, rules, scripts, profiles, agent files),
  excluding the runtime copy, launcher and run state, which are covered by the next item;
- for review cells, the source of the kit modules the evaluator uses to score a review.

Kit code that runs before the model is fingerprinted by what it produced, not by its source:
a runtime-only change that leaves every prompt and file identical leaves the cell reusable.
`architecture-lint` status is reported, not gated, so reused rows keep the previous result;
the anchor run records a fresh one. Two kits with equal fingerprints hand the model the same
input and judge its output with the same code, so a cell measured on the previous release
stands for the new kit. Dependency
installation and fixture test runs depend on the case and the machine, not on the kit, so
they are replaced by a constant here; drift in models, CLIs and toolchains since the previous
record is what the fresh anchor run (see `verify`) is for.

    python evals/release_reuse.py plan --previous <prev tree> --kit <new tree> --output plan.json
    python evals/release_reuse.py assemble --plan plan.json --previous-record <dir> \
        [--run <phase_eval output>] --anchors <phase_eval output> --export evals/release-gates/v<version>
    python evals/release_reuse.py verify --record <dir> --previous-record <dir> \
        --previous <prev tree> --kit <new tree>
"""
from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

THRESHOLDS = HERE / "phase-eval-thresholds.json"
FINGERPRINT_VERSION = "agent-flow release reuse fingerprint v1"
# The run directory holds what the model is told to read (prd, plans, review input) and is
# included. The managed runtime copy and launcher are code the evaluation does not ask the
# model to run; kit.json and the asset receipt carry install timestamps and the whole-kit digest.
EXCLUDED_PARTS = {".git", "node_modules", "__pycache__", ".dart_tool", ".venv", ".pytest_cache", "build"}
EXCLUDED_PREFIXES = (
    ".agent-flow/state/", ".agent-flow/handoffs/", ".agent-flow/runtime/",
    ".agent-flow/bin/", ".agent-flow/kit.json", ".agent-flow/kit-assets.json",
)
# The evaluator writes the fixture's own test run here; it depends on the case and the
# toolchain, not the kit, and carries run timings.
KIT_INDEPENDENT_FILES = {"test-evidence.md"}
SCORING_MODULES = ("agent_flow.core.review_evidence", "agent_flow.multi_review", "agent_flow.subprocess_pool")
ANCHOR_SCENARIOS = (("author", ""), ("review", "defect"), ("review", "clean"))
# Distinct trials each anchor needs. One sample per combination misses intermittent failures:
# at a 5% per-unit miss rate, 36 anchors catch one with ~84% probability, 72 with ~97.5%.
ANCHOR_TRIALS = 2
# Reusing the previous kit's rows as a changed cell's `before` compares a fresh `after` with an
# older measurement. Fresh previous-kit anchors must show the same outcomes, and their token use
# must stay within this factor of what the previous record measured for the same cells.
TOKEN_DRIFT_MAX = 1.25


# --------------------------------------------------------------------------- cells

def matrix_cells(thresholds: dict) -> list[tuple[str, str, str, str]]:
    matrix = thresholds["matrix"]
    cells = []
    for stack in matrix["stacks"]:
        for mode in matrix["modes"]:
            for variant in matrix["variants"]["author"]:
                cells.append(("author", stack, mode, variant))
            for variant in matrix["variants"]["review"]:
                cells.append(("review", stack, mode, variant))
    return cells


def cell_id(scenario: str, stack: str, mode: str, variant: str) -> str:
    return f"{scenario}/{stack}/{mode}/{variant or '-'}"


def row_cell(row: dict) -> str:
    return cell_id(row["scenario"], row["stack"], row["mode"], row.get("variant") or "")


# --------------------------------------------------------------------------- kit code

def scoring_sources(kit: Path) -> dict[str, str]:
    """The kit modules the evaluator calls to judge a review after the model ran.

    Everything the kit does before the model runs is visible in what it produced (prompts and
    files), so it is fingerprinted by those outputs instead of by its code.
    """
    sources = {}
    for name in SCORING_MODULES:
        path = kit / "src" / Path(*name.split(".")).with_suffix(".py")
        sources[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
    return sources


# --------------------------------------------------------------------------- one cell

def _normalise(text: str, replacements: dict[str, str]) -> str:
    for old, new in sorted(replacements.items(), key=lambda item: -len(item[0])):
        text = text.replace(old, new)
    text = re.sub(r"/tmp/[^\s'\"`]*?af-[A-Za-z0-9_.-]+", "<tmp>", text)
    # Commit ids and digests name content that is fingerprinted on its own (the install commit
    # carries kit.json's install time and kit digest, so its id differs on every install).
    text = re.sub(r"\b[0-9a-f]{64}\b", "<sha256>", text)
    text = re.sub(r"\b[0-9a-f]{40}\b", "<oid>", text)
    # Run metadata records when it started and draws fresh gate/review nonces.
    text = re.sub(r"\b[0-9a-f]{32}\b", "<nonce>", text)
    return re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?", "<time>", text)


def _assets(project: Path, replacements: dict[str, str]) -> dict[str, str]:
    assets: dict[str, str] = {}
    for path in sorted(project.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(project).as_posix()
        if EXCLUDED_PARTS & set(path.relative_to(project).parts) or relative.startswith(EXCLUDED_PREFIXES):
            continue
        if path.name in KIT_INDEPENDENT_FILES:
            continue
        data = path.read_bytes()
        try:
            data = _normalise(data.decode("utf-8"), replacements).encode("utf-8")
        except UnicodeDecodeError:
            pass
        assets[relative] = hashlib.sha256(data).hexdigest()
    return assets


def fingerprint_cell(kit: Path, scenario: str, stack: str, mode: str, variant: str, project: Path,
                     providers: dict) -> dict:
    import phase_eval

    # Fixture commits feed the review-input snapshot digest that reviewer prompts quote.
    # Pinned dates make those commit ids, and so the prompt, independent of when this runs.
    for key in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        os.environ[key] = "2000-01-01T00:00:00+0000"
    kit = kit.resolve()
    tooling = {"test_command": ["true"], "tools": {"python3": sys.executable}, "launcher": "<launcher>"}
    phase_eval._fixture_tooling = lambda *args, **kwargs: tooling
    phase_eval.prepare_fixture_environment = lambda *args, **kwargs: {"stub": "kit-independent"}
    phase_eval.verify_web_review = lambda *args, **kwargs: []
    prepared = phase_eval.prepare(
        kit, phase_eval.CASES / stack, mode, scenario, variant, project,
        author_provider=providers["author"], review_providers=tuple(providers["review"]),
    )
    replacements = {str(project.resolve()): "<project>", str(project): "<project>", str(kit): "<kit>",
                    sys.executable: "<python>"}
    if scenario == "author":
        prompts = [prepared["prompt"]]
    else:
        prompts = [f"{job['angle']}\0{job['provider']}\0{job['prompt']}" for job in prepared["jobs"]]
    return {
        "prompts": [hashlib.sha256(_normalise(p, replacements).encode("utf-8")).hexdigest() for p in prompts],
        "assets": _assets(project, replacements),
        "code": scoring_sources(kit) if scenario == "review" else {},
    }


def combine(parts: dict) -> str:
    digest = hashlib.sha256(FINGERPRINT_VERSION.encode("utf-8") + b"\0")
    digest.update(json.dumps(parts, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()


def _fingerprint_worker(kit: Path, cell: tuple[str, str, str, str], providers: dict) -> dict:
    root = Path(tempfile.mkdtemp(prefix="af-reuse-"))
    try:
        process = subprocess.run(
            [sys.executable, __file__, "fingerprint", "--kit", str(kit), "--cell", json.dumps(cell),
             "--providers", json.dumps(providers), "--project", str(root / "project")],
            capture_output=True, text=True, timeout=1800,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "AGENT_FLOW_NO_UPDATE_CHECK": "1"},
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if process.returncode != 0:
        return {"error": process.stderr[-1200:]}
    return json.loads(process.stdout)


# --------------------------------------------------------------------------- plan

def evaluation_conditions(tree: Path) -> dict:
    from phase_eval_compare import evaluation_fingerprint

    return evaluation_fingerprint(tree)


def plan(previous: Path, kit: Path, providers: dict, jobs: int) -> dict:
    thresholds = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
    cells = matrix_cells(thresholds)
    tasks = [(name, tree, cell) for cell in cells for name, tree in (("previous", previous), ("kit", kit))]
    with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
        results = list(pool.map(lambda task: _fingerprint_worker(task[1], task[2], providers), tasks))
    by_cell: dict[str, dict] = {}
    for (name, _, cell), result in zip(tasks, results):
        entry = by_cell.setdefault(cell_id(*cell), {})
        entry[name] = {"error": result["error"]} if "error" in result else {
            "fingerprint": combine(result), "parts": result,
        }
    for entry in by_cell.values():
        fingerprints = [entry[name].get("fingerprint") for name in ("previous", "kit")]
        entry["unchanged"] = None not in fingerprints and fingerprints[0] == fingerprints[1]
    return {
        "version": FINGERPRINT_VERSION,
        "providers": providers,
        "conditions": {"previous": evaluation_conditions(previous), "kit": evaluation_conditions(kit)},
        "cells": by_cell,
    }


def reusable_cells(plan_payload: dict) -> list[str]:
    """Cells whose rows may be reused: unchanged inputs under the same evaluator and cases."""
    conditions = plan_payload["conditions"]
    if conditions["previous"] != conditions["kit"]:
        return []
    return sorted(cell for cell, entry in plan_payload["cells"].items() if entry["unchanged"])


# --------------------------------------------------------------------------- record assembly

def _read_rows(directory: Path) -> list[dict]:
    plain, packed = directory / "results.jsonl", directory / "results.jsonl.gz"
    if plain.is_file():
        text = plain.read_text(encoding="utf-8")
    else:
        with gzip.open(packed, "rt", encoding="utf-8") as handle:
            text = handle.read()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _read_meta(directory: Path) -> dict:
    return json.loads((directory / "meta.json").read_text(encoding="utf-8"))


def _condition(meta: dict) -> dict:
    from phase_eval_compare import CONDITION_KEYS

    return {key: meta.get(key) for key in CONDITION_KEYS}


def _previous_after_rows(previous_record: Path) -> list[dict]:
    return [row for row in _read_rows(previous_record) if row.get("kit") == "after"]


def _reuse_blockers(plan_payload: dict, previous_meta: dict, anchor_meta: dict, run_meta: dict | None) -> list[str]:
    """Why no cell may be reused: every measurement must share the previous record's condition."""
    blockers = []
    previous = _condition(previous_meta)
    kit_conditions = plan_payload["conditions"]["kit"]
    for key in ("evaluator_sha256", "case_files"):
        if previous.get(key) != kit_conditions.get(key):
            blockers.append(f"the previous record's {key} differs from this tree's")
    if plan_payload.get("providers") != previous.get("requested_providers"):
        blockers.append(f"the plan rendered prompts for {plan_payload.get('providers')}, "
                        f"the previous record measured {previous.get('requested_providers')}")
    if _condition(anchor_meta) != previous:
        blockers.append("the anchor run's condition (evaluator, cases, providers, models, language) "
                        "differs from the previous record's")
    if run_meta is not None and _condition(run_meta) != previous:
        blockers.append("the fresh run's condition differs from the previous record's")
    return blockers


def assemble(plan_payload: dict, previous_record: Path, run: Path | None, anchors: Path,
             export: Path, *, reuse_before: bool = False) -> tuple[list[str], list[str]]:
    """Build the record, judge it with the release comparator, and export it only if it passes.

    With `reuse_before`, a changed cell measured only on the new kit takes its `before` rows
    from the previous record, provided fresh previous-kit anchors show no drift.
    """
    from phase_eval_compare import _record_row, compare

    previous_meta = _read_meta(previous_record)
    anchor_meta = _read_meta(anchors)
    run_meta = _read_meta(run) if run is not None else None
    blockers = _reuse_blockers(plan_payload, previous_meta, anchor_meta, run_meta)
    reuse = [] if blockers else reusable_cells(plan_payload)
    fresh_rows = _read_rows(run) if run is not None else []
    fresh_cells = {row_cell(row) for row in fresh_rows}
    failures = list(blockers)
    overlap = sorted(fresh_cells & set(reuse))
    if overlap:
        failures.append(f"cells both reused and measured: {overlap}")
    missing = sorted(set(plan_payload["cells"]) - set(reuse) - fresh_cells)
    if missing:
        failures.append(f"cells neither reused nor measured: {missing}")
    anchor_rows = _read_rows(anchors)
    failures += anchor_failures(anchor_rows, reuse, anchor_meta.get("kit_digests", {}).get("after"), anchor_meta)
    measured_before = {row_cell(row) for row in fresh_rows if row.get("kit") == "before"}
    before_reused = sorted(fresh_cells - measured_before) if reuse_before else []
    if fresh_cells - measured_before and not reuse_before:
        failures.append(f"cells measured without `before`: {sorted(fresh_cells - measured_before)}")
    previous_rows = _previous_after_rows(previous_record)
    if before_reused:
        failures += drift_failures(anchor_rows, previous_rows, before_reused,
                                   previous_meta["kit_digests"]["after"], anchor_meta)
    if failures:
        return [], failures

    rows = list(fresh_rows)
    # The previous release's kit is this release's `before`; an unchanged cell behaves the same
    # on the new kit, so the same recorded rows stand for both sides.
    for row in previous_rows:
        if row_cell(row) in reuse:
            rows.append({**row, "kit": "before"})
            rows.append({**row, "kit": "after"})
        elif row_cell(row) in before_reused:
            rows.append({**row, "kit": "before"})
    digests = {"before": previous_meta["kit_digests"]["after"], "after": anchor_meta["kit_digests"]["after"]}
    if run_meta is not None and run_meta.get("kit_digests") != digests:
        return [], [f"the fresh run measured kits {run_meta.get('kit_digests')}, not {digests}"]
    thresholds = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="af-reuse-record-") as scratch:
        staging = Path(scratch)
        (staging / "meta.json").write_text(
            json.dumps({**_condition(previous_meta), "kit_digests": digests, "units": len(rows)}), encoding="utf-8")
        (staging / "results.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        lines, failures = compare([staging], "before", "after", thresholds, export=export)
    if failures:
        return lines, failures
    (export / "anchors.jsonl.gz").write_bytes(gzip.compress(
        "".join(json.dumps(_record_row(row), sort_keys=True) + "\n" for row in anchor_rows).encode("utf-8"), mtime=0))
    (export / "anchor-meta.json").write_text(
        json.dumps({**_condition(anchor_meta), "kit_digests": anchor_meta["kit_digests"]}, indent=1, sort_keys=True)
        + "\n", encoding="utf-8")
    reuse_record = {
        "version": FINGERPRINT_VERSION,
        "previous_record_meta_sha256": hashlib.sha256((previous_record / "meta.json").read_bytes()).hexdigest(),
        "reused_cells": reuse,
        "reused_before_cells": before_reused,
        "fingerprints": {cell: plan_payload["cells"][cell]["kit"]["fingerprint"] for cell in reuse},
        "providers": plan_payload["providers"],
    }
    (export / "reuse.json").write_text(json.dumps(reuse_record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return lines + [f"Reused {len(reuse)} of {len(plan_payload['cells'])} cells from the previous release"
                    f" and the `before` side of {len(before_reused)} more."], []


# --------------------------------------------------------------------------- verification

def _outcome_failures(rows: list[dict], combos: list[tuple[str, str]], kit: str, label: str) -> list[str]:
    """Every stack × mode needs a valid, correct `kit` row for each anchor scenario, and every
    skill the phase required must have been observed read: reuse stands in for re-measuring
    whether skills load, so the fresh sample has to show they still do."""
    from phase_eval_compare import AXES, _invalid

    failures = []
    for stack, mode in combos:
        for scenario, variant in ANCHOR_SCENARIOS:
            matching = [row for row in rows if row.get("kit") == kit and row["scenario"] == scenario
                        and row["stack"] == stack and row["mode"] == mode and (row.get("variant") or "") == variant]
            name = f"{scenario}/{stack}/{mode}/{variant or '-'}"
            trials = {row.get("trial") for row in matching}
            if len(trials) < ANCHOR_TRIALS:
                failures.append(f"{label} for {name} has {len(trials)} of {ANCHOR_TRIALS} trials")
            for row in matching:
                where = f"{label} {name} t{row['trial']}"
                if _invalid(row):
                    failures.append(f"{where} is invalid")
                elif scenario == "author" and not all(row["oracle"][axis] for axis in AXES):
                    failures.append(f"{where} failed the oracle")
                elif scenario == "author" and (row.get("required_unread") or row.get("required_unobserved")):
                    failures.append(f"{where} did not observably read "
                                    f"{sorted({*(row.get('required_unread') or []), *(row.get('required_unobserved') or [])})}")
                elif scenario == "review" and row.get("expected") is not None and not row.get("correct"):
                    failures.append(f"{where} gave the wrong verdict")
                elif scenario == "review" and row.get("expected") == "request-changes" and not all(
                        row["defects"].values()):
                    failures.append(f"{where} missed a seeded defect")
                elif scenario == "review" and not all(
                        document.get("read_path_observed") is not False
                        for angle in row.get("angles") or [] for document in angle.get("documents") or []):
                    failures.append(f"{where} has a reviewer that did not read a required document")
    return failures


def _combos(cells: list[str]) -> list[tuple[str, str]]:
    return sorted({(cell.split("/")[1], cell.split("/")[2]) for cell in cells})


def anchor_failures(anchor_rows: list[dict], reused_cells: list[str], new_digest: str | None,
                    anchor_meta: dict) -> list[str]:
    """Each stack × mode with a reused cell needs fresh, passing new-kit evidence for every scenario."""
    if not reused_cells:
        return []
    failures = []
    if new_digest is None or anchor_meta.get("kit_digests", {}).get("after") != new_digest:
        failures.append("the anchor run did not measure this kit as `after`")
    return failures + _outcome_failures(anchor_rows, _combos(reused_cells), "after", "anchor")


def drift_failures(anchor_rows: list[dict], previous_rows: list[dict], cells: list[str],
                   previous_digest: str, anchor_meta: dict) -> list[str]:
    """Fresh previous-kit anchors must reproduce the previous record's outcomes and token use."""
    failures = []
    if anchor_meta.get("kit_digests", {}).get("before") != previous_digest:
        failures.append("the anchor run did not measure the previous kit as `before`")
    failures += _outcome_failures(anchor_rows, _combos(cells), "before", "previous-kit anchor")
    from phase_eval_compare import TOKEN_AXES, _invalid

    for axis in TOKEN_AXES:
        fresh = recorded = 0
        for row in anchor_rows:
            if row.get("kit") != "before" or _invalid(row):
                continue
            history = [old["usage"][axis] for old in previous_rows
                       if row_cell(old) == row_cell(row) and not _invalid(old)]
            if history:
                fresh += row["usage"][axis]
                recorded += sum(history) / len(history)
        if recorded and not (1 / TOKEN_DRIFT_MAX <= fresh / recorded <= TOKEN_DRIFT_MAX):
            failures.append(f"previous-kit {axis} tokens drifted {fresh / recorded:.2f}x from the previous record")
    return failures


def verify(record: Path, previous_record: Path, previous: Path, kit: Path, jobs: int,
           new_digest: str, planner=None) -> list[str]:
    """Re-derive every reuse claim of a committed record from the two trees."""
    reuse_record = json.loads((record / "reuse.json").read_text(encoding="utf-8"))
    if reuse_record.get("version") != FINGERPRINT_VERSION:
        return [f"reuse record version {reuse_record.get('version')!r} is not {FINGERPRINT_VERSION!r}"]
    failures: list[str] = []
    if hashlib.sha256((previous_record / "meta.json").read_bytes()).hexdigest() != \
            reuse_record["previous_record_meta_sha256"]:
        failures.append("the previous release record changed since its rows were reused")
    reused = reuse_record["reused_cells"]
    before_reused = reuse_record.get("reused_before_cells", [])
    if not reused and not before_reused:
        return failures
    anchor_meta = json.loads((record / "anchor-meta.json").read_text(encoding="utf-8"))
    previous_meta = _read_meta(previous_record)
    fresh_plan = (planner or plan)(previous, kit, reuse_record["providers"], jobs)
    failures += _reuse_blockers(fresh_plan, previous_meta, anchor_meta, None)
    if _condition(_read_meta(record)) != _condition(previous_meta):
        failures.append("the record's condition differs from the previous record's")
    allowed = set(reusable_cells(fresh_plan))
    for cell in reused:
        if cell not in allowed:
            failures.append(f"{cell}: model inputs changed; it cannot be reused")
        elif fresh_plan["cells"][cell]["kit"]["fingerprint"] != reuse_record["fingerprints"].get(cell):
            failures.append(f"{cell}: recorded fingerprint does not match this kit")
    previous_rows: dict[str, list[str]] = {}
    for row in _previous_after_rows(previous_record):
        if row_cell(row) in reused or row_cell(row) in before_reused:
            previous_rows.setdefault(row_cell(row), []).append(json.dumps(row, sort_keys=True))
    recorded: dict[tuple[str, str], list[str]] = {}
    for row in _read_rows(record):
        if row_cell(row) in reused or row_cell(row) in before_reused:
            recorded.setdefault((row_cell(row), row.get("kit")), []).append(
                json.dumps({**row, "kit": "after"}, sort_keys=True))
    for cell in reused:
        for side in ("before", "after"):
            if sorted(recorded.get((cell, side), [])) != sorted(previous_rows.get(cell, [])):
                failures.append(f"{cell}/{side}: rows are not the previous release's rows")
    for cell in before_reused:
        if sorted(recorded.get((cell, "before"), [])) != sorted(previous_rows.get(cell, [])):
            failures.append(f"{cell}/before: rows are not the previous release's rows")
    anchor_rows = _read_rows_named(record, "anchors")
    failures += anchor_failures(anchor_rows, reused, new_digest, anchor_meta)
    if before_reused:
        failures += drift_failures(anchor_rows, _previous_after_rows(previous_record), before_reused,
                                   previous_meta["kit_digests"]["after"], anchor_meta)
    return failures


def _read_rows_named(directory: Path, stem: str) -> list[dict]:
    with gzip.open(directory / f"{stem}.jsonl.gz", "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle.read().splitlines() if line.strip()]


# --------------------------------------------------------------------------- cli

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("fingerprint")
    one.add_argument("--kit", type=Path, required=True)
    one.add_argument("--cell", required=True)
    one.add_argument("--providers", required=True)
    one.add_argument("--project", type=Path, required=True)
    planning = sub.add_parser("plan")
    planning.add_argument("--previous", type=Path, required=True)
    planning.add_argument("--kit", type=Path, required=True)
    planning.add_argument("--author-provider", default="codex")
    planning.add_argument("--review-providers", default="codex")
    planning.add_argument("--jobs", type=int, default=4)
    planning.add_argument("--output", type=Path, required=True)
    assembling = sub.add_parser("assemble")
    assembling.add_argument("--plan", type=Path, required=True)
    assembling.add_argument("--previous-record", type=Path, required=True)
    assembling.add_argument("--run", type=Path)
    assembling.add_argument("--anchors", type=Path, required=True)
    assembling.add_argument("--export", type=Path, required=True, help="release record directory to create")
    assembling.add_argument("--reuse-before", action="store_true",
                            help="take `before` rows of new-kit-only cells from the previous record")
    checking = sub.add_parser("verify")
    checking.add_argument("--record", type=Path, required=True)
    checking.add_argument("--previous-record", type=Path, required=True)
    checking.add_argument("--previous", type=Path, required=True)
    checking.add_argument("--kit", type=Path, required=True)
    checking.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args(argv)

    if args.command == "fingerprint":
        scenario, stack, mode, variant = json.loads(args.cell)
        sys.path.insert(0, str(args.kit.resolve() / "src"))
        os.environ["AGENT_FLOW_HOST"] = json.loads(args.providers)["author"]
        print(json.dumps(fingerprint_cell(args.kit, scenario, stack, mode, variant, args.project,
                                          json.loads(args.providers))))
        return 0
    if args.command == "plan":
        providers = {"author": args.author_provider, "review": args.review_providers.split(",")}
        payload = plan(args.previous.resolve(), args.kit.resolve(), providers, args.jobs)
        args.output.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        errors = sorted(cell for cell, entry in payload["cells"].items()
                        if any("error" in entry[name] for name in ("previous", "kit")))
        reusable = reusable_cells(payload)
        print(f"{len(reusable)}/{len(payload['cells'])} cells reusable; "
              f"{len(payload['cells']) - len(reusable)} must be measured")
        if errors:
            print("fingerprint errors (measure these): " + ", ".join(errors), file=sys.stderr)
        return 0
    if args.command == "assemble":
        payload = json.loads(args.plan.read_text(encoding="utf-8"))
        lines, failures = assemble(payload, args.previous_record, args.run, args.anchors, args.export,
                                   reuse_before=args.reuse_before)
        print("\n".join(lines))
        for failure in failures:
            print(f"FAIL {failure}")
        return 1 if failures else 0
    sys.path.insert(0, str(HERE.parent / "src"))
    from agent_flow.core.kit_digest import kit_source_digest

    failures = verify(args.record, args.previous_record, args.previous.resolve(), args.kit.resolve(), args.jobs,
                      new_digest=kit_source_digest(args.kit.resolve()))
    for failure in failures:
        print(f"FAIL {failure}")
    print("reuse verified" if not failures else "reuse rejected")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
