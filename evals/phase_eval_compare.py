"""Release gate: compare two kits' `phase_eval.py` results against fixed thresholds.

    python evals/phase_eval_compare.py <output dir> [<output dir>] \
        [--before before] [--after after] [--thresholds evals/phase-eval-thresholds.json] [--summary out.md]

Pass one `phase_eval.py` output directory that measured both kits, or one directory per kit.
Every directory must record `evaluator_sha256`, `requested_models`, `case_files` and `units`,
and the directories must agree on the first three; results measured under different or
unknown conditions are not compared.

Failure conditions come from the thresholds file:
- a results file is incomplete (fewer rows than `units`, unparseable lines) or has duplicate units;
- a stack · mode · variant combination is missing on one kit or has fewer than `min_trials` trials;
- an after-kit metric is below its floor or above its ceiling;
- either kit's `invalid_rate` is above its ceiling;
- an after-kit metric dropped (`no_drop`) or rose (`no_rise`) against the before kit;
- summed uncached input or output tokens of units valid in both kits grew beyond the ratio;
- a metric or token sum cannot be measured, including a valid unit without usage;
- the thresholds file names an unknown metric or token axis.

`--export DIR` writes a passing result as a release record: the condition keys, `units`,
`kit_digests` and only the row fields the metrics read (no paths, argv or reviewer text).
`--require-kit` / `--require-before-kit` fail unless the recorded kit digests equal the given
kit trees, and `--require-evaluator` fails unless the recorded evaluator and case hashes equal
that tree's `evals/phase_eval.py` and `evals/phase-cases`. The release workflow runs this on the
tag's record against the tag and the previous tag. It catches a release without a matching
measurement; it does not stop someone with commit access from fabricating rows.

Real model runs stay manual; this gate blocks a release by procedure and in the release workflow.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import shutil
import sys
import tempfile
import zlib
from pathlib import Path
from typing import IO

DEFAULT_THRESHOLDS = Path(__file__).resolve().parent / "phase-eval-thresholds.json"
DEFAULT_CASES = Path(__file__).resolve().parent / "phase-cases"
CONDITION_KEYS = ("evaluator_sha256", "requested_models", "case_files")
SCENARIOS = ("author", "review")
UNIT_FIELDS = ("scenario", "stack", "mode", "kit", "trial")
AXES = ("behavior", "plan", "norm")
TOKEN_AXES = ("uncached_input", "output")
METRICS = (
    "author_required_read_rate", "author_pass_rate", "review_correct_rate", "defect_detection_rate",
    "false_request_changes_rate", "reviewer_read_rate", "invalid_rate",
)
ROW_FIELDS = (
    "scenario", "stack", "mode", "variant", "kit", "trial", "valid", "expected", "correct", "defects",
    "false_request_changes", "subprocesses", "required_read", "required_unread", "usage",
)


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _unscored(row: dict) -> bool:
    """The run itself failed: a provider error, an invalid review, or an oracle without all three axes."""
    if row.get("error"):
        return True
    if row["scenario"] == "review":
        return row.get("valid") is not True
    oracle = row.get("oracle")
    return not isinstance(oracle, dict) or "error" in oracle or any(type(oracle.get(axis)) is not bool for axis in AXES)


def _count(value: object, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def malformed_fields(row: dict) -> list[str]:
    """Scoring fields of a scored row that are missing or of the wrong type or range."""
    usage = row.get("usage")
    # A scored unit without complete usage cannot be checked for token efficiency, so it is malformed.
    problems = [f"usage.{axis}" for axis in TOKEN_AXES if not (isinstance(usage, dict) and _count(usage.get(axis)))]
    if row["scenario"] == "author":
        return problems + [key for key in ("required_read", "required_unread")
                           if not (isinstance(row.get(key), list) and all(isinstance(name, str) for name in row[key]))]
    if "expected" not in row:
        problems.append("expected")
    expected = row.get("expected")
    if expected not in (None, "approve", "request-changes"):
        problems.append("expected")
    if expected is not None and type(row.get("correct")) is not bool:
        problems.append("correct")
    defects = row.get("defects")
    if expected == "request-changes" and not (isinstance(defects, dict) and all(type(v) is bool for v in defects.values())):
        problems.append("defects")
    if expected == "approve" and not (_count(row.get("false_request_changes")) and _count(row.get("subprocesses"), 1)):
        problems.append("false_request_changes/subprocesses")
    angles = row.get("angles")
    if not isinstance(angles, list) or not all(
        isinstance(angle, dict) and isinstance(angle.get("documents"), list) and all(
            isinstance(document, dict) and type(document.get("inline_in_prompt")) is bool
            and type(document.get("read_path_observed")) in (type(None), bool)
            for document in angle["documents"])
        for angle in angles
    ):
        problems.append("angles")
    return problems


def _invalid(row: dict) -> bool:
    return _unscored(row) or bool(malformed_fields(row))


def kit_digest(kit: Path) -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from agent_flow.core.kit_digest import kit_source_digest

    return kit_source_digest(kit)


def evaluation_fingerprint(tree: Path) -> dict:
    """The evaluator and case hashes `phase_eval.py` records, computed for another tree."""
    cases = tree / "evals" / "phase-cases"
    return {
        "evaluator_sha256": hashlib.sha256((tree / "evals" / "phase_eval.py").read_bytes()).hexdigest(),
        "case_files": {
            path.relative_to(cases).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(cases.rglob("*")) if path.is_file() and "__pycache__" not in path.parts
        },
    }


def _review_case_failures(rows: list[dict], condition: dict, cases: Path) -> list[str]:
    reviews = [row for row in rows if row["scenario"] == "review" and not _unscored(row)]
    failures: list[str] = []
    for stack in sorted({row["stack"] for row in reviews}):
        relative = f"{stack}/case.json"
        try:
            body = (cases / relative).read_bytes()
            recorded = condition.get("case_files")
            if not isinstance(recorded, dict) or recorded.get(relative) != hashlib.sha256(body).hexdigest():
                failures.append(f"case contract hash does not match measurement: {relative}")
                continue
            specs = json.loads(body)["review"]
        except (OSError, ValueError, KeyError, TypeError):
            failures.append(f"case contract unreadable: {relative}")
            continue
        for row in reviews:
            if row["stack"] != stack:
                continue
            label = "/".join(map(str, _unit_key(row)))
            try:
                spec = specs[row["mode"]][row.get("variant")]
                expected = spec["expect"]
                defect_ids = {defect["id"] for defect in spec.get("defects", [])}
            except (KeyError, TypeError):
                failures.append(f"case contract missing for {label}")
                continue
            defects = row.get("defects")
            if (row.get("expected") != expected or not isinstance(defects, dict)
                    or set(defects) != defect_ids or any(type(value) is not bool for value in defects.values())):
                failures.append(f"case contract scoring fields differ in {row['kit']} {label}")
    return failures


def _condition_known(key: str, value: object) -> bool:
    if key == "requested_models":
        return isinstance(value, dict) and all(
            isinstance(value.get(provider), str) and bool(value[provider].strip())
            for provider in ("claude", "codex")
        )
    return bool(value)


def _open_results(directory: Path) -> IO[str]:
    plain = directory / "results.jsonl"
    return plain.open(encoding="utf-8") if plain.exists() else gzip.open(directory / "results.jsonl.gz", "rt", encoding="utf-8")


def load(directories: list[Path]) -> tuple[list[dict], list[str], dict, dict[str, str]]:
    """Return parsed rows, reasons the input cannot be trusted, its condition and recorded kit digests."""
    rows: list[dict] = []
    failures: list[str] = []
    conditions: list[tuple[Path, dict]] = []
    digests: dict[str, str] = {}
    for directory in directories:
        try:
            meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = None
        if not isinstance(meta, dict):
            failures.append(f"unreadable meta.json in {directory}")
            continue
        condition = {key: meta.get(key) for key in CONDITION_KEYS}
        missing = [key for key, value in condition.items() if not _condition_known(key, value)]
        if missing:
            failures.append(f"measurement condition unknown in {directory}: {', '.join(missing)}")
        conditions.append((directory, condition))
        recorded = meta.get("kit_digests")
        recorded = recorded if isinstance(recorded, dict) else {}
        for kit, digest in recorded.items():
            if not (isinstance(digest, str) and len(digest) == 64
                    and all(char in "0123456789abcdef" for char in digest)):
                failures.append(f"{kit} kit digest malformed in {directory}")
            elif kit in digests and digests[kit] != digest:
                failures.append(f"{kit} kit digest conflicts in {directory}")
            else:
                digests[kit] = digest
        start = len(rows)
        parsed = 0
        try:
            parsed = _read_rows(directory, rows, failures)
        except (OSError, EOFError, ValueError, zlib.error):
            failures.append(f"unreadable results in {directory}")
        failures.extend(f"{kit} kit digest not recorded in {directory}"
                        for kit in {row["kit"] for row in rows[start:]} if kit not in recorded)
        units = meta.get("units")
        if type(units) is not int or parsed != units:
            failures.append(f"results incomplete in {directory}: {parsed} rows, meta.json units {units}")
    if not conditions:
        return rows, failures, {}, digests
    reference_dir, reference = conditions[0]
    failures.extend(
        f"measurement condition differs: `{key}` in {directory} vs {reference_dir}"
        for directory, condition in conditions[1:]
        for key in CONDITION_KEYS if condition[key] != reference[key]
    )
    return rows, failures, reference, digests


def _read_rows(directory: Path, rows: list[dict], failures: list[str]) -> int:
    parsed = 0
    with _open_results(directory) as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                failures.append(f"{directory} results line {number} is unparseable")
                continue
            if not _is_result_row(row):
                failures.append(f"{directory} results line {number} is not a result row")
                continue
            rows.append(row)
            parsed += 1
    return parsed


def _record_row(row: dict) -> dict:
    record = {key: row[key] for key in ROW_FIELDS if key in row}
    if row.get("error"):
        record["error"] = True
    oracle = row.get("oracle")
    if isinstance(oracle, dict):
        record["oracle"] = {axis: oracle.get(axis) for axis in AXES} | ({"error": True} if "error" in oracle else {})
    elif "oracle" in row:
        record["oracle"] = None
    if "angles" in row:
        record["angles"] = [
            {"provider": angle.get("provider"), "documents": [
                {"inline_in_prompt": document.get("inline_in_prompt"),
                 "read_path_observed": document.get("read_path_observed")}
                for document in angle.get("documents") or []
            ]}
            for angle in row["angles"]
        ]
    return record


def export_record(destination: Path, rows: list[dict], condition: dict, digests: dict[str, str]) -> None:
    """Write the record into a sibling temp directory and rename it, so a failure leaves nothing behind."""
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        meta = {**condition, "units": len(rows), "kit_digests": digests}
        (staging / "meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        body = "".join(json.dumps(_record_row(row), sort_keys=True) + "\n" for row in rows)
        (staging / "results.jsonl.gz").write_bytes(gzip.compress(body.encode("utf-8"), mtime=0))
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def kit_metrics(rows: list[dict], kit: str) -> dict[str, float | None]:
    own = [row for row in rows if row.get("kit") == kit]
    authors = [row for row in own if row["scenario"] == "author" and not _invalid(row)]
    reviews = [row for row in own if row["scenario"] == "review" and not _invalid(row)]

    read = sum(len(row["required_read"]) for row in authors)
    unread = sum(len(row["required_unread"]) for row in authors)
    scored = [row["oracle"] for row in authors]
    judged = [row["correct"] for row in reviews if row.get("expected") is not None]
    seeded = [found for row in reviews if row.get("expected") == "request-changes" for found in row["defects"].values()]
    approve_rows = [row for row in reviews if row.get("expected") == "approve"]
    # Codex read observations are null and inline bodies need no read; neither counts as unread.
    observed = [
        document["read_path_observed"]
        for row in reviews for angle in row.get("angles") or [] if angle.get("provider") == "claude"
        for document in angle.get("documents") or []
        if not document.get("inline_in_prompt") and document.get("read_path_observed") is not None
    ]
    return {
        "author_required_read_rate": _rate(read, read + unread),
        "author_pass_rate": _rate(sum(all(oracle[axis] for axis in AXES) for oracle in scored), len(scored)),
        "review_correct_rate": _rate(sum(judged), len(judged)),
        "defect_detection_rate": _rate(sum(bool(found) for found in seeded), len(seeded)),
        "false_request_changes_rate": _rate(
            sum(row["false_request_changes"] for row in approve_rows), sum(row["subprocesses"] for row in approve_rows),
        ),
        "reviewer_read_rate": _rate(sum(observed), len(observed)),
        "invalid_rate": _rate(sum(_invalid(row) for row in own), len(own)),
    }


def _unit_key(row: dict) -> tuple:
    return row["scenario"], row["stack"], row["mode"], row.get("variant") or "", row["trial"]


def token_ratios(rows: list[dict], before: str, after: str) -> dict[str, dict[str, float | None]]:
    """Per scenario after/before ratio of summed tokens over units valid in both kits."""
    valid = {
        kit: {_unit_key(row): row for row in rows if row.get("kit") == kit and not _invalid(row)}
        for kit in (before, after)
    }
    ratios: dict[str, dict[str, float | None]] = {}
    for scenario in SCENARIOS:
        keys = [key for key in valid[before].keys() & valid[after].keys() if key[0] == scenario]
        ratios[scenario] = {}
        for axis in TOKEN_AXES:
            totals = {kit: sum(valid[kit][key]["usage"][axis] for key in keys) for kit in (before, after)}
            ratios[scenario][axis] = totals[after] / totals[before] if keys and totals[before] else None
    return ratios


def _coverage_failures(rows: list[dict], before: str, after: str, thresholds: dict) -> list[str]:
    """The release matrix and any measured combination must exist on both kits with enough distinct trials."""
    trials: dict[str, dict[tuple, list[int]]] = {before: {}, after: {}}
    for row in rows:
        if row.get("kit") in trials:
            key = _unit_key(row)
            trials[row["kit"]].setdefault(key[:-1], []).append(key[-1])
    matrix = thresholds["matrix"]
    required = {
        (scenario, stack, mode, variant)
        for scenario, variants in matrix["variants"].items() for variant in variants
        for stack in matrix["stacks"] for mode in matrix["modes"]
    }
    min_trials = thresholds["min_trials"]
    failures: list[str] = []
    combinations = required | trials[before].keys() | trials[after].keys()
    for kit, by_combination in trials.items():
        for combination in sorted(combinations):
            seen = by_combination.get(combination, [])
            label = f"{kit} {'/'.join(part for part in combination if part)}"
            if not seen:
                failures.append(f"{label}: missing on this kit")
            elif len(set(seen)) < min_trials:
                failures.append(f"{label}: {len(set(seen))} trials, need {min_trials}")
            if len(seen) != len(set(seen)):
                failures.append(f"{label}: duplicate trial rows")
    for combination in sorted(combinations):
        old = set(trials[before].get(combination, []))
        new = set(trials[after].get(combination, []))
        if old != new:
            label = "/".join(part for part in combination if part)
            failures.append(f"{label}: trial sets differ: {before} {sorted(old)} vs {after} {sorted(new)}")
    return failures


def _is_result_row(row: object) -> bool:
    return (
        isinstance(row, dict)
        and row.get("scenario") in SCENARIOS
        and all(isinstance(row.get(key), str) for key in ("stack", "mode", "kit"))
        and type(row.get("trial")) is int
        and (row.get("variant") is None or isinstance(row["variant"], str))
    )


def _finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _names(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _shape_failures(thresholds: object) -> list[str]:
    """Container shapes the value checks rely on."""
    if not isinstance(thresholds, dict):
        return ["thresholds must be a JSON object"]
    failures = [f"thresholds missing `{key}`" for key in
                ("min_trials", "floors", "ceilings", "no_drop", "no_rise", "token_ratio_max", "matrix")
                if key not in thresholds]
    failures.extend(f"thresholds `{key}` must be an object" for key in ("floors", "ceilings", "token_ratio_max", "matrix")
                    if key in thresholds and not isinstance(thresholds[key], dict))
    failures.extend(f"thresholds `{key}` must be a list of names" for key in ("no_drop", "no_rise")
                    if key in thresholds and not _names(thresholds[key]))
    matrix = thresholds.get("matrix")
    if isinstance(matrix, dict):
        failures.extend(f"thresholds matrix `{key}` must be a non-empty list of names" for key in ("stacks", "modes")
                        if not (_names(matrix.get(key)) and matrix[key]))
        variants = matrix.get("variants")
        if not (isinstance(variants, dict) and variants and all(_names(value) for value in variants.values())):
            failures.append("thresholds matrix `variants` must map scenarios to lists of names")
    return failures


def _threshold_failures(thresholds: dict) -> list[str]:
    """Shapes, unknown names and non-finite or out-of-range values; NaN would otherwise disable a check."""
    failures = _shape_failures(thresholds)
    if failures:
        return failures
    failures.extend(f"thresholds matrix names unknown scenario `{scenario}`"
                    for scenario in thresholds["matrix"]["variants"] if scenario not in SCENARIOS)
    named = [*thresholds["floors"], *thresholds["ceilings"], *thresholds["no_drop"], *thresholds["no_rise"]]
    failures.extend(f"thresholds name unknown metric `{name}`" for name in named if name not in METRICS)
    failures.extend(f"thresholds name unknown token axis `{axis}`"
                    for axis in thresholds["token_ratio_max"] if axis not in TOKEN_AXES)
    if not _count(thresholds["min_trials"], 1):
        failures.append("thresholds `min_trials` must be a positive integer")
    failures.extend(f"thresholds `{name}` must be a rate between 0 and 1"
                    for section in ("floors", "ceilings") for name, value in thresholds[section].items()
                    if not (_finite_number(value) and 0 <= value <= 1))
    failures.extend(f"thresholds token limit `{axis}` must be a positive finite number"
                    for axis, value in thresholds["token_ratio_max"].items()
                    if not (_finite_number(value) and value > 0))
    return failures


def _fmt(value: float | None) -> str:
    return "unmeasured" if value is None else f"{value:.3f}"


def compare(directories: list[Path], before: str, after: str, thresholds: dict, *,
            export: Path | None = None, require: dict[str, Path] | None = None,
            evaluator_tree: Path | None = None) -> tuple[list[str], list[str]]:
    """Return (report lines, failure reasons). Export a record only when nothing failed."""
    rows, failures, condition, digests = load(directories)
    threshold_failures = _threshold_failures(thresholds)
    if threshold_failures:
        return ["## phase_eval gate", "", "### Failures", *(f"- {reason}" for reason in threshold_failures)], (
            failures + threshold_failures
        )
    for kit, tree in (require or {}).items():
        recorded, actual = digests.get(kit), kit_digest(tree)
        if recorded != actual:
            failures.append(f"{kit} kit digest {recorded or 'not recorded'} does not match {tree} ({actual})")
    if evaluator_tree is not None:
        expected = evaluation_fingerprint(evaluator_tree)
        failures.extend(f"record `{key}` does not match {evaluator_tree}"
                        for key, value in expected.items() if condition.get(key) != value)
    cases = evaluator_tree / "evals" / "phase-cases" if evaluator_tree is not None else DEFAULT_CASES
    failures.extend(_review_case_failures(rows, condition, cases))
    failures.extend(
        f"malformed scoring fields in {row.get('kit')} {'/'.join(map(str, _unit_key(row)))}: {', '.join(problems)}"
        for row in rows if not _unscored(row) for problems in [malformed_fields(row)] if problems
    )
    metrics = {kit: kit_metrics(rows, kit) for kit in (before, after)}
    failures.extend(_coverage_failures(rows, before, after, thresholds))

    new = metrics[after]
    for metric, floor in thresholds["floors"].items():
        if new[metric] is None:
            failures.append(f"{metric} unmeasured on {after}; floor {floor}")
        elif new[metric] < floor:
            failures.append(f"{metric} {_fmt(new[metric])} is below floor {floor}")
    for metric, ceiling in thresholds["ceilings"].items():
        for kit in ((before, after) if metric == "invalid_rate" else (after,)):
            value = metrics[kit][metric]
            if value is None:
                failures.append(f"{metric} unmeasured on {kit}; ceiling {ceiling}")
            elif value > ceiling:
                failures.append(f"{metric} on {kit} {_fmt(value)} is above ceiling {ceiling}")
    for metric in thresholds["no_drop"]:
        old, value = metrics[before][metric], new[metric]
        if old is None or value is None:
            failures.append(f"{metric} unmeasured, cannot compare: {_fmt(old)} → {_fmt(value)}")
        elif value < old:
            failures.append(f"{metric} dropped: {_fmt(old)} → {_fmt(value)}")
    for metric in thresholds["no_rise"]:
        old, value = metrics[before][metric], new[metric]
        if old is None or value is None:
            failures.append(f"{metric} unmeasured, cannot compare: {_fmt(old)} → {_fmt(value)}")
        elif value > old:
            failures.append(f"{metric} rose: {_fmt(old)} → {_fmt(value)}")

    ratios = token_ratios(rows, before, after)
    for scenario, by_axis in ratios.items():
        for axis, limit in thresholds["token_ratio_max"].items():
            ratio = by_axis[axis]
            if ratio is None:
                failures.append(f"{scenario} {axis} token ratio unmeasured (no unit valid in both kits)")
            elif ratio > limit:
                failures.append(f"{scenario} {axis} token ratio {_fmt(ratio)} exceeds {limit}")

    lines = ["## phase_eval gate", "", f"`{before}` → `{after}`, {len(rows)} rows.", "",
             "|metric|before|after|", "|---|---:|---:|"]
    lines.extend(f"|{metric}|{_fmt(metrics[before][metric])}|{_fmt(new[metric])}|" for metric in new)
    lines.extend(f"|{scenario} {axis} token ratio||{_fmt(ratio)}|"
                 for scenario, by_axis in ratios.items() for axis, ratio in by_axis.items())
    lines.append("")
    lines.append("### Failures" if failures else "All thresholds met.")
    lines.extend(f"- {reason}" for reason in failures)
    if export is not None and not failures:
        export_record(export, rows, condition, digests)
    return lines, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directories", type=Path, nargs="+", help="phase_eval.py output directories")
    parser.add_argument("--before", default="before")
    parser.add_argument("--after", default="after")
    parser.add_argument("--thresholds", type=Path, default=DEFAULT_THRESHOLDS)
    parser.add_argument("--summary", type=Path, help="append the markdown report here")
    parser.add_argument("--export", type=Path, help="write a passing result as a release record here")
    parser.add_argument("--require-kit", type=Path, help="kit tree the after kit digest must match")
    parser.add_argument("--require-before-kit", type=Path, help="kit tree the before kit digest must match")
    parser.add_argument("--require-evaluator", type=Path, help="tree whose evaluator and cases the record must match")
    args = parser.parse_args()
    try:
        thresholds = json.loads(args.thresholds.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        thresholds = None
    require = {kit: tree for kit, tree in ((args.after, args.require_kit), (args.before, args.require_before_kit)) if tree}
    lines, failures = compare(args.directories, args.before, args.after, thresholds, export=args.export,
                              require=require, evaluator_tree=args.require_evaluator)
    report = "\n".join(lines) + "\n"
    print(report)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write(report)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
