#!/usr/bin/env python3
"""Why a run declared `blocked`, set against what the hidden evaluator found (#409 v3.9).

A workflow arm can stop for its own procedure (an architecture decision, a guard hook, the fix-loop round cap) while
the code is done, or because the code is not done. The declaration alone does not tell these apart, so each blocked
run gets a stated reason, from its final message and `result.json`, and a verdict from the hidden score:

- `false-block`: the evaluator passed the build (S=1). The harness cost a success.
- `blocked-with-defects`: the build fails hidden checks; `G` and the failed-check count say how far it was.
- `no-build`: nothing to grade (G=0 and every check failed).

Reasons, first match wins: `guard` (the worktree guard hook refused commands), `architecture` (a pending or drifted
architecture selection), `review-cap` (the fix loop hit its round limit), `review-findings` (reviewers kept asking
for changes), `other`.

    python3 evals/block_reasons.py <run-dir>...
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REASONS = (
    ("guard", re.compile(r"guard|hook.*(bound|bind|worktree)|not bound|EnterWorktree", re.I)),
    ("architecture", re.compile(r"architecture", re.I)),
    ("review-cap", re.compile(r"round limit|exceeded \d+ rounds|fix-loop .*(cap|limit)", re.I)),
    ("review-findings", re.compile(r"request[- ]changes|requests? changes", re.I)),
)


def stated_reason(result: dict) -> str:
    message = result.get("final_message") or ""
    status = [line for line in message.splitlines() if "EVAL-STATUS" in line]
    text = status[-1] if status else message[-600:]
    for name, pattern in REASONS:
        if pattern.search(text):
            return name
    return "review-cap" if result.get("fix_loop_cap_stop") else "other"


def classify(run: Path) -> dict | None:
    result = json.loads((run / "result.json").read_text(encoding="utf-8"))
    if result.get("declaration") != "declared-blocked":
        return None
    ui = result.get("ui") or {}
    score, grade = ui.get("S"), ui.get("G")
    failed = len(ui.get("failed_checks") or [*(ui.get("failed_browser") or []), *(ui.get("failed_api") or [])])
    verdict = "false-block" if score == 1 else "no-build" if not grade else "blocked-with-defects"
    return {"run": run.name, "arm": result.get("arm"), "reason": stated_reason(result), "verdict": verdict,
            "S": score, "G": grade, "failed": failed, "cap_stop": bool(result.get("fix_loop_cap_stop")),
            "reviews": (result.get("phases_entered") or []).count("review")}


def main(argv: list[str]) -> int:
    rows = [row for row in (classify(Path(path)) for path in argv) if row]
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
