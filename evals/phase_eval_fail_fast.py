"""Stop a release measurement once its gate can no longer pass.

A full release matrix takes hours. Two outcomes are already certain before it ends: a
seeded defect the new kit's reviewers missed (the defect floor is 1.0), and more invalid
units on the new kit than the invalid-rate ceiling allows. Measuring the rest cannot turn
either into a pass, so `phase_eval.py --fail-fast` stops scheduling units and records why.
An early stop never produces a usable record: the comparator rejects the missing units.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

THRESHOLDS = Path(__file__).resolve().parent / "phase-eval-thresholds.json"


class FailFast:
    def __init__(self, kit: str, kit_units: int, thresholds: dict | None = None) -> None:
        thresholds = thresholds or json.loads(THRESHOLDS.read_text(encoding="utf-8"))
        self.kit = kit
        self.defect_floor = thresholds.get("floors", {}).get("defect_detection_rate")
        ceiling = thresholds.get("ceilings", {}).get("invalid_rate")
        # The comparator fails when invalid / units exceeds the ceiling.
        self.invalid_allowed = math.floor(ceiling * kit_units) if ceiling is not None else None
        self.invalid = 0

    def observe(self, row: dict) -> str | None:
        """A reason the gate is already lost, or None while it can still pass."""
        if row.get("kit") != self.kit:
            return None
        from phase_eval_compare import _invalid  # the comparator owns what counts as invalid

        if _invalid(row):
            self.invalid += 1
            if self.invalid_allowed is not None and self.invalid > self.invalid_allowed:
                return f"{self.invalid} invalid {self.kit} units exceed the ceiling of {self.invalid_allowed}"
            return None
        if self.defect_floor == 1.0 and row.get("scenario") == "review" and row.get("expected") == "request-changes":
            missed = sorted(defect for defect, found in (row.get("defects") or {}).items() if not found)
            if missed:
                return (f"{self.kit} review {row['stack']}/{row['mode']} t{row['trial']} missed {', '.join(missed)}; "
                        "the defect detection floor is 1.0")
        return None
