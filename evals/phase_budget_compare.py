"""Compare two `phase_budget.py` outputs and fail on skill-delivery regressions.

    python evals/phase_budget_compare.py before.json after.json [--summary out.md] [--accept]

Failure conditions, from `maintainer/verification-policy.md`:
- a skill required before is no longer required in some profile · mode · condition · phase;
- a non-Clean mode envelope carries the Clean `roles` table;
- the total of phase envelopes, controller sessions and reviewer subprocesses grew;
- a combination, condition or phase measured before can no longer be measured (a broken
  phase would otherwise count as zero bytes and read as a saving).

`--accept` reports the same findings but exits 0. CI passes it when the PR carries the
`phase-budget-accepted` label, i.e. a maintainer confirmed the reported change is intended
and the PR description states why.

Tokens are bytes/4. Required reads count only for phases whose envelope carries a read plan;
each reviewer subprocess counts its prompt plus its provider's required reads once.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CONTROLLER_PHASES = {"multi-review", "architecture-review"}
LAYER = "layer"


def _load(path: Path) -> tuple[dict, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    measured, failed = {}, {}
    for row in data["results"]:
        key = (row["profile"], row["mode"])
        if "conditions" in row:
            measured[key] = row
        else:
            failed[key] = (row.get("install_error") or row.get("worker_error") or "failed").strip()[-200:]
    return measured, failed


def _reviewer_bytes(phase: dict) -> int:
    prompts = phase.get("reviewer_prompt_bytes_by_provider") or {}
    reads = phase.get("reviewer_read_bytes_by_provider") or {}
    return sum(sum(sizes) + reads.get(provider, 0) * len(sizes) for provider, sizes in prompts.items())


def _sections(row: dict, condition: str = LAYER) -> tuple[int, int, int]:
    author = controller = reviewers = 0
    for phase_id, phase in row["conditions"][condition]["phases"].items():
        reads = phase.get("required_read_bytes", 0) if phase.get("read_plan_in_envelope", True) else 0
        own = phase.get("author_bytes", 0) + reads
        if phase_id in CONTROLLER_PHASES:
            controller += own
            reviewers += _reviewer_bytes(phase)
        else:
            author += own
    return author, controller, reviewers


def compare(before_path: Path, after_path: Path) -> tuple[list[str], list[str]]:
    """Return (report lines, failure reasons)."""
    before, before_failed = _load(before_path)
    after, after_failed = _load(after_path)
    common = sorted(set(before) & set(after))
    failures: list[str] = []

    lost_combos = sorted(key for key in before if key not in after)
    if lost_combos:
        failures.append(
            "measured before, not after: "
            + ", ".join(f"{p}:{m} ({after_failed.get((p, m), 'missing')})" for p, m in lost_combos)
        )

    # 측정이 깨진 phase는 바이트가 0으로 들어가 절감처럼 보인다. 비교할 수 없는 측정은 실패다.
    broken = []
    for key in sorted(set(before) & set(after)):
        for condition, cond_before in before[key]["conditions"].items():
            cond_after = after[key]["conditions"].get(condition)
            for phase_id in cond_before["phases"]:
                phase_after = (cond_after or {"phases": {}})["phases"].get(phase_id)
                if phase_after is None or "error" in phase_after:
                    broken.append(f"{key[0]}:{key[1]}/{condition}/{phase_id}")
    if broken:
        failures.append(f"head measurement incomplete: {', '.join(broken)}")

    totals = {"before": [0, 0, 0], "after": [0, 0, 0]}
    per_combo = []
    for key in common:
        b, a = _sections(before[key]), _sections(after[key])
        for i in range(3):
            totals["before"][i] += b[i]
            totals["after"][i] += a[i]
        per_combo.append((key, sum(b), sum(a)))

    lost: dict[tuple[str, str], int] = {}
    roles_leaks = []
    grown: dict[str, list[int]] = {}
    for key in common:
        for condition, cond_before in before[key]["conditions"].items():
            cond_after = after[key]["conditions"].get(condition, {"phases": {}})
            for phase_id, phase_before in cond_before["phases"].items():
                phase_after = cond_after["phases"].get(phase_id, {})
                for name in set(phase_before.get("required", [])) - set(phase_after.get("required", [])):
                    lost[(phase_id, name)] = lost.get((phase_id, name), 0) + 1
            if condition in after[key]["conditions"]:
                sums = grown.setdefault(condition, [0, 0])
                sums[0] += sum(_sections(before[key], condition))
                sums[1] += sum(_sections(after[key], condition))
        if key[1] != "clean":
            for condition, cond_after in after[key]["conditions"].items():
                for phase_id, phase in cond_after["phases"].items():
                    if phase.get("roles_in_envelope"):
                        roles_leaks.append(f"{key[0]}:{key[1]}/{condition}/{phase_id}")

    total_before, total_after = sum(totals["before"]), sum(totals["after"])
    if lost:
        failures.append(
            "required skills lost: "
            + ", ".join(f"{phase}/{name} ×{count}" for (phase, name), count in sorted(lost.items()))
        )
    if roles_leaks:
        failures.append(f"Clean roles in non-Clean envelopes: {', '.join(roles_leaks)}")
    # 표는 layer 조건을 보이지만, 증가는 조건마다 판정한다. 커밋 후 라우팅 같은 한 조건의 증가가
    # 다른 조건의 감소에 묻히지 않게 한다.
    for condition, (condition_before, condition_after) in sorted(grown.items()):
        if condition_after > condition_before:
            failures.append(f"total bytes grew in `{condition}`: {condition_before:,} → {condition_after:,}")

    def row(label: str, b: int, a: int) -> str:
        change = f"{(a - b) / b * 100:+.1f}%" if b else "n/a"
        return f"| {label} | {b // 4000:,}k | {a // 4000:,}k | {change} |"

    lines = [
        "## phase_budget",
        "",
        f"{len(common)} profile × mode combinations, `{LAYER}` condition, bytes/4.",
        "",
        "| section | before | after | change |",
        "|---|---:|---:|---:|",
        row("author phase envelopes + required reads", totals["before"][0], totals["after"][0]),
        row("multi-review · architecture-review controller", totals["before"][1], totals["after"][1]),
        row("reviewer subprocesses", totals["before"][2], totals["after"][2]),
        row("total", total_before, total_after),
        "",
    ]
    if per_combo:
        changes = sorted(((a - b) / b * 100 if b else 0.0, key) for key, b, a in per_combo)
        low, high = changes[0], changes[-1]
        lines.append(
            f"Per combination: {low[0]:+.1f}% ({low[1][0]}:{low[1][1]}) to {high[0]:+.1f}% ({high[1][0]}:{high[1][1]})."
        )
        lines.append("")
    if before_failed or after_failed:
        lines.append(
            "Not measured (install refused or failed): before "
            + (", ".join(f"{p}:{m}" for p, m in sorted(before_failed)) or "none")
            + "; after "
            + (", ".join(f"{p}:{m}" for p, m in sorted(after_failed)) or "none")
            + "."
        )
        lines.append("")
    lines.append("### Findings" if failures else "No skill-delivery regression found.")
    lines.extend(f"- {reason}" for reason in failures)
    return lines, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--summary", type=Path, help="append the markdown report here")
    parser.add_argument("--accept", action="store_true", help="report findings without failing")
    args = parser.parse_args()
    lines, failures = compare(args.before, args.after)
    text = "\n".join(lines) + "\n"
    print(text)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write(text)
    if failures and args.accept:
        print("findings accepted by the phase-budget-accepted label", file=sys.stderr)
        return 0
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
