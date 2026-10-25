"""Attempts: opaque run directories, the assignment ledger and the exclusion taxonomy (#409 v3.14 A, E).

A run directory's name is a random id (`new_run`), so neither its path nor the worktree the runner derives from it
names the case, the layer, the arm or the trial. The mapping is one line per attempt in `<runs root>/ledger.jsonl`,
outside every run directory (the sandbox hides a run's siblings and the ledger next to them). A run directory is never
moved or reused: `prepare` records it in `arm.json` and `score` refuses a run found elsewhere.

    python3 evals/attempts.py check --manifest assignment.json <result.json>...

`check` compares the pre-registered assignment (a JSON list of {case, layer, arm, trial, step}) with the results:
missing, duplicate and unassigned attempts. A run left out of the rates must carry one of `EXCLUSIONS` as its
`harness_error` code (`<code>: <detail>`), tied to its attempt id.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

LEDGER = "ledger.jsonl"
ID_PREFIX = "r-"
# Why an attempt leaves the denominator. Every one is the harness's fault, never the arm's.
EXCLUSIONS = {
    "sandbox": "the run sandbox could not be set up, or a process outlived its namespace",
    "model-pin": "a participating call ran on another model or effort than the pinned Opus 5.5 low",
    "cli-version": "the claude CLI was not the pinned version",
    "checkout": "the graded checkout could not be decided or was not recorded",
    "snapshot": "the graded snapshot is missing or changed after it was frozen",
    "gate-integrity": "arm V's gate script or config changed after prepare",
    "base-skills": "the CLI's base skill set differed from the pinned one",
    "moved": "the run directory was moved after prepare",
    "harness-bug": "a defect in the harness itself, fixed and re-run (name the commit)",
    "infra-outage": "the model service or proxy was down for the whole attempt",
}


def new_run(root: Path, *, case: str, layer: str | None, arm: str, trial: int, step: int = 1,
            extra: dict | None = None) -> Path:
    """Reserves `<root>/<random id>` for one attempt and appends its assignment to the ledger. The directory is not
    created (`prepare` creates it and refuses an existing one)."""
    root.mkdir(parents=True, exist_ok=True)
    for _ in range(20):
        run_id = ID_PREFIX + secrets.token_hex(6)
        if not (root / run_id).exists() and run_id not in {row["run_id"] for row in ledger(root)}:
            break
    else:  # pragma: no cover - 48 random bits
        raise RuntimeError("could not find a free run id")
    row = {"run_id": run_id, "case": case, "layer": layer, "arm": arm, "trial": trial, "step": step,
           "reserved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **(extra or {})}
    with (root / LEDGER).open("a", encoding="utf-8") as out:
        out.write(json.dumps(row, ensure_ascii=False) + "\n")
    return root / run_id


def ledger(root: Path) -> list[dict]:
    path = root / LEDGER
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def exclusion_code(reason: str | None) -> str | None:
    """The taxonomy code of a `harness_error` (`<code>` or `<code>: <detail>`); `unclassified` for free text."""
    if not reason:
        return None
    code = reason.split(":", 1)[0].strip()
    return code if code in EXCLUSIONS else "unclassified"


def check_reason(reason: str) -> str:
    """A `--harness-error` value, or ValueError naming the codes."""
    if exclusion_code(reason) == "unclassified":
        raise ValueError(f"harness error must start with one of {sorted(EXCLUSIONS)} (`<code>: <detail>`), "
                         f"got {reason!r}")
    return reason


# Contamination (#409 v3.14 note C). In a run whose sandbox was healthy (the host read-only inside, no hidden path
# showing anything, `sandbox_healthy`) nothing hidden was reachable, so only content proves contamination: a canary hit
# in a log or the evaluator's own `read` verdict (its gating `contamination` list). A hidden path named by a tool call,
# a read of the simulator's or an earlier step's files, a phase case's hidden markers, a changed tripwire are then
# diagnostics. A run without a healthy sandbox keeps the path-based rule: any of them is contamination.
CONTENT_EVIDENCE = ("canary_hits", "evaluator_read")
PATH_EVIDENCE = ("hidden_path_reads", "phase_hidden_markers", "oracle_reads", "chain_reads", "tripwire_changed")


def sandbox_healthy(sandbox: dict | None) -> bool:
    """A `drive.json` `sandbox` record of a required sandbox whose checks hold: host read-only, no hidden leaks."""
    if not isinstance(sandbox, dict) or sandbox.get("mode") != "required":
        return False
    checks = sandbox.get("checks")
    return (isinstance(checks, dict) and checks.get("host_read_only") is True and checks.get("hidden_leaks") == []
            and not sandbox.get("errors") and not sandbox.get("survivors"))


def contamination_rule(evidence: dict) -> str:
    return "content" if evidence.get("sandbox_healthy") else "path"


def contaminated(row: dict) -> bool:
    """The contamination verdict of a result (or of its `contamination_evidence`). A result scored before the
    evidence was recorded keeps its `contaminated` flag."""
    evidence = row.get("contamination_evidence") if "contamination_evidence" in row else row
    if not isinstance(evidence, dict) or "sandbox_healthy" not in evidence:
        return bool(row.get("contaminated"))
    keys = CONTENT_EVIDENCE if evidence["sandbox_healthy"] else (*CONTENT_EVIDENCE, *PATH_EVIDENCE)
    return any(bool(evidence.get(key)) for key in keys)


def attempt_key(row: dict) -> tuple:
    chain = row.get("chain") or {}
    return (row.get("case"), row.get("layer"), row.get("arm"), row.get("trial"),
            int(row.get("step") or chain.get("step") or 1))


def attempt_id(row: dict) -> str:
    return str(row.get("run_id") or f"{row.get('arm')}-{row.get('trial')}")


def check(assignment: list[dict], results: list[dict]) -> dict:
    """Missing, duplicate and unassigned attempts, by (case, layer, arm, trial, step)."""
    wanted = Counter(attempt_key(row) for row in assignment)
    got = Counter(attempt_key(row) for row in results)
    ids = Counter(attempt_id(row) for row in results)
    return {"assigned": sum(wanted.values()), "results": sum(got.values()),
            "missing": sorted([list(key) for key in wanted if got[key] < wanted[key]], key=str),
            "duplicate": sorted([list(key) for key in got if got[key] > max(wanted[key], 1)], key=str),
            "duplicate_ids": sorted(key for key, count in ids.items() if count > 1),
            "unassigned": sorted([list(key) for key in got if key not in wanted], key=str)}


def check_lines(assignment: list[dict], results: list[dict]) -> list[str]:
    found = check(assignment, results)
    lines = ["", f"Assignment check: {found['results']} results for {found['assigned']} assigned attempts", ""]
    for name in ("missing", "duplicate", "duplicate_ids", "unassigned"):
        items = found[name]
        lines.append(f"- {name}: {len(items)}" + (f" ({'; '.join(map(str, items[:20]))})" if items else ""))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    chk = sub.add_parser("check")
    chk.add_argument("--manifest", type=Path, required=True)
    chk.add_argument("results", type=Path, nargs="+")
    args = parser.parse_args(argv)
    assignment = json.loads(args.manifest.read_text(encoding="utf-8"))
    results = [json.loads(path.read_text(encoding="utf-8")) for path in args.results]
    found = check(assignment, results)
    print(json.dumps(found, indent=1))
    return 0 if not (found["missing"] or found["duplicate"] or found["duplicate_ids"] or found["unassigned"]) else 1


if __name__ == "__main__":
    sys.exit(main())
