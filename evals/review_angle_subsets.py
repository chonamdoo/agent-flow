"""Re-score recorded review rows for angle subsets and k-of-N agreement, offline (#425).

    python evals/review_angle_subsets.py <phase_eval output dir or results file> [...] \
        [--kit after] [--subset generalist --subset generalist,types] [--k 1 --k 2]

It answers "what would fewer angles, or a vote among them, have caught and flagged on these same rows".
It reads what phase_eval already recorded, calls no model and does not change the runtime rule
(`review_evidence.py` still requests changes when any reviewer does).

For a subset S of angle names, on rows that recorded per-angle results (`angles[].angle` and
`.verdict`, plus `defect_detectors`):

- detection: seeded defects on request-changes rows whose detectors include an angle in S
- clean false request-changes: approve rows where an angle in S requested changes
- uncached input: summed `usage.uncached_input` of the angles in S, per row

`k-of-N` keeps every angle and counts a defect when at least k angles detected it, and a clean row as
flagged when at least k angles requested changes. Detectors are `angle/provider`; a subset matches the
angle part, so every provider's run of that angle counts.
Rows that are invalid (an angle without a verdict) or lack per-angle results are skipped and counted.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path


def read_rows(paths: list[Path]) -> list[dict]:
    rows = []
    for path in paths:
        if path.is_dir():
            path = next((candidate for candidate in (path / "results.jsonl", path / "results.jsonl.gz")
                         if candidate.is_file()), path / "results.jsonl")
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as handle:
            rows.extend(json.loads(line) for line in handle if line.strip())
    return rows


def _scorable(row: dict) -> bool:
    angles = row.get("angles")
    return (row.get("scenario") == "review" and row.get("expected") in ("approve", "request-changes")
            and isinstance(angles, list) and bool(angles)
            and all(isinstance(angle, dict) and angle.get("angle") and angle.get("verdict") for angle in angles)
            and (row["expected"] == "approve" or isinstance(row.get("defect_detectors"), dict)))


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def score(rows: list[dict], *, subset: frozenset[str] | None = None, k: int = 1) -> dict:
    """`subset=None` keeps every angle. `k` counts agreement among the kept angles."""
    found = seeded = flagged = clean = 0
    uncached: list[int] = []
    for row in rows:
        angles = [angle for angle in row["angles"] if subset is None or angle["angle"] in subset]
        if not angles:
            continue
        uncached.append(sum((angle.get("usage") or {}).get("uncached_input") or 0 for angle in angles))
        if row["expected"] == "approve":
            clean += 1
            flagged += sum(angle["verdict"] == "request-changes" for angle in angles) >= k
            continue
        for detectors in row["defect_detectors"].values():
            seeded += 1
            hits = {detector for detector in detectors if subset is None or detector.split("/")[0] in subset}
            found += len(hits) >= k
    return {
        "detection_rate": _rate(found, seeded), "seeded": seeded,
        "clean_false_request_changes_rate": _rate(flagged, clean), "clean_rows": clean,
        "uncached_input_per_row": _rate(sum(uncached), len(uncached)),
    }


def _cell(value: float | None, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def report(rows: list[dict], *, kit: str | None, subsets: list[frozenset[str]], ks: list[int]) -> str:
    own = [row for row in rows if kit is None or row.get("kit") == kit]
    usable = [row for row in own if _scorable(row)]
    skipped = sum(row.get("scenario") == "review" for row in own) - len(usable)
    angles = sorted({angle["angle"] for row in usable for angle in row["angles"]})
    lines = [f"{len(usable)} review rows scored, {skipped} skipped (invalid or no per-angle results). "
             f"Angles: {', '.join(angles) or 'none'}.", "",
             "| configuration | detection | seeded | clean false request-changes | clean rows | uncached input / row |",
             "|---|---:|---:|---:|---:|---:|"]
    configurations = [("all angles", None, 1)]
    configurations += [(", ".join(sorted(subset)), subset, 1) for subset in subsets]
    configurations += [(f"{k}-of-N", None, k) for k in ks if k > 1]
    for name, subset, k in configurations:
        item = score(usable, subset=subset, k=k)
        lines.append(f"| {name} | {_cell(item['detection_rate'])} | {item['seeded']} "
                     f"| {_cell(item['clean_false_request_changes_rate'])} | {item['clean_rows']} "
                     f"| {_cell(item['uncached_input_per_row'], 0)} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--kit", help="score only this kit's rows (default: all)")
    parser.add_argument("--subset", action="append", default=[],
                        help="comma-separated angle names; repeat for more (default: each single angle)")
    parser.add_argument("--k", type=int, action="append", default=[], help="k-of-N agreement; repeat for more")
    args = parser.parse_args(argv)
    rows = read_rows(args.inputs)
    subsets = [frozenset(name.strip() for name in subset.split(",") if name.strip()) for subset in args.subset]
    if not subsets:
        subsets = [frozenset({name}) for name in sorted(
            {angle.get("angle") for row in rows if _scorable(row) for angle in row["angles"]})]
    print(report(rows, kit=args.kit, subsets=subsets, ks=sorted(set(args.k))), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
