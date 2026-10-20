"""Compare what an author phase claims in its Completion Gate with what was observed.

An agent saying "done" is a claim. The runner already observes some of it (skill reads,
test commands); the rest of the Completion Gate is self-report. This module scores the
self-report against evidence the author cannot write: observed SKILL.md reads and the
case oracle. A claim it cannot check is left alone, not counted as true or false.

    python evals/claim_audit.py <phase_eval output dir>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_GATE_HEADING = re.compile(r"(?m)^\s*#{2,3}\s*Completion Gate\s*$")
_MARKER = re.compile(r"^\s*([a-z][a-z0-9_-]*)\s*:\s*(.*?)\s*$")
_FENCE = re.compile(r"(?m)^\s*```")


def parse_completion_gate(text: str) -> dict[str, str] | None:
    """Markers of the last Completion Gate outside code fences, or None when there is none."""
    unfenced = []
    inside = False
    for line in text.splitlines():
        if _FENCE.match(line):
            inside = not inside
            continue
        if not inside:
            unfenced.append(line)
    body = "\n".join(unfenced)
    headings = list(_GATE_HEADING.finditer(body))
    if not headings:
        return None
    claims: dict[str, str] = {}
    for line in body[headings[-1].end():].splitlines():
        if re.match(r"^\s*#", line):
            break
        match = _MARKER.match(line)
        if match:
            claims[match.group(1)] = match.group(2).strip().strip("`").lower()
    return claims


def overclaims(claims: dict[str, str] | None, row: dict) -> list[str]:
    """Claims the evidence contradicts. Unobserved evidence contradicts nothing."""
    if claims is None:
        return []
    found = []
    unread = row.get("required_unread") or []
    claimed_read = (
        claims.get("skills_checked") == "true"
        or claims.get("required-profile-skills") == "checked"
        or claims.get("missing-required-profile-skills") == "none"
    )
    if claimed_read and unread:
        found.append(f"claimed required skills were read; observed unread: {', '.join(sorted(unread))}")
    oracle = row.get("oracle") if isinstance(row.get("oracle"), dict) else {}
    if oracle.get("norm") is False:
        for marker, value in (("architecture-contract", "applied"), ("presentation-state-review", "pass")):
            if claims.get(marker) == value:
                found.append(f"claimed {marker}: {value}; the norm oracle failed")
    if oracle.get("behavior") is False:
        found.append("claimed completion; the behavior oracle failed")
    return found


def author_artifact(project: Path, phase_id: str = "green") -> Path | None:
    """The author's phase artifact in the evaluation run, whatever extension it was given.

    full-feature writes under `artifacts/`; development and bugfix write at the run root.
    """
    matches = sorted(project.glob(f".agent-flow/runs/*/artifacts/{phase_id}.*")) or \
        sorted(project.glob(f".agent-flow/runs/*/{phase_id}.*"))
    return matches[0] if matches else None


def audit_rows(rows: list[dict]) -> dict[str, dict[str, int]]:
    """Per kit: author rows with a gate, rows with at least one overclaim, rows without a gate."""
    totals: dict[str, dict[str, int]] = {}
    for row in rows:
        if row.get("scenario") != "author" or row.get("error") or "claims" not in row:
            continue
        bucket = totals.setdefault(row.get("kit", "?"), {"with_gate": 0, "overclaimed": 0, "without_gate": 0})
        if row["claims"] is None:
            bucket["without_gate"] += 1
            continue
        bucket["with_gate"] += 1
        bucket["overclaimed"] += int(bool(row.get("overclaims")))
    return totals


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path, help="phase_eval output directory")
    args = parser.parse_args()
    results = args.output / "results.jsonl"
    rows = [json.loads(line) for line in results.read_text(encoding="utf-8").splitlines() if line.strip()]
    totals = audit_rows(rows)
    if not totals:
        print("no author rows carry claims; this output predates the claim audit", file=sys.stderr)
        return 1
    print("|kit|with gate|overclaimed|rate|without gate|\n|---|---:|---:|---:|---:|")
    for kit, bucket in sorted(totals.items()):
        rate = f"{bucket['overclaimed'] / bucket['with_gate']:.3f}" if bucket["with_gate"] else "-"
        print(f"|{kit}|{bucket['with_gate']}|{bucket['overclaimed']}|{rate}|{bucket['without_gate']}|")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
