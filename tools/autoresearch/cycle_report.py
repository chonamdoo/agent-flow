"""Tables for a release improvement-cycle record (maintainer/improvement-history/).

Reads phase_eval run directories (or results.jsonl files) and prints Markdown:

- author rows per case × kit: passes on all axes, each axis, mean turns, uncached input and output tokens,
  cost, recorded overclaims and how many rows could have shown one, plus a one-sided Fisher exact p-value
  against the baseline kit and, with --evidence, how many plan-related tool calls each run made;
- review rows per kit × reviewer provider: request-changes on defect variants, false request-changes on clean
  variants, unparsed verdicts and their rate, tokens and cost; then per kit the valid rows, correct final
  verdicts, seeded defects found and, with more than one provider, how often providers disagree on an angle.

    python tools/autoresearch/cycle_report.py <run dir|results.jsonl> [...] [--baseline before]

Tool calls and plan lookups come from a run directory's `evidence/` (phase_eval keeps each model call's prompt
and stream there; Claude stream-json and Codex `exec --json` are both read) or, for an archived run, its
`evidence-stats.json` (archive_runs.py). Codex reports no turns or dollar cost; those cells read n/a. `overclaim-eligible` counts rows where claim_audit could find an overclaim at all: an unread
required skill, or a failed behavior or norm oracle. Not shipped: outside `scripts/` and the kit digest roots.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import defaultdict
from math import comb
from pathlib import Path

PROJECT = re.compile(r"author-(?P<stack>[\w-]+?)-(?P<mode>clean|local|team|pending|stack)-(?P<kit>[\w.-]+?)-(?P<trial>\d+)\b")
PLAN_TERMS = re.compile(r"slice|plan", re.IGNORECASE)


def fisher_greater(success_a: int, total_a: int, success_b: int, total_b: int) -> float:
    """One-sided Fisher exact p: chance of B doing at least this much better than A if both rates were equal."""
    successes, total = success_a + success_b, total_a + total_b
    return sum(comb(successes, k) * comb(total - successes, total_b - k)
               for k in range(success_b, min(successes, total_b) + 1)) / comb(total, total_b)


def load(paths: list[str]) -> list[dict]:
    """Rows of every run. Author rows get `_evidence` (tool calls, plan lookups) from the same run's evidence/,
    or from an archived run's evidence-stats.json; runs are kept apart because trial numbers repeat."""
    rows = []
    for raw in paths:
        path = Path(raw)
        results = path / "results.jsonl" if path.is_dir() else path
        run_rows = [json.loads(line) for line in results.read_text(encoding="utf-8").splitlines() if line.strip()]
        stats: dict[tuple[str, str, int], dict[str, int]] = {}
        if (results.parent / "evidence").is_dir():
            stats = evidence_stats([results.parent / "evidence"])
        elif (results.parent / "evidence-stats.json").is_file():
            for item in json.loads((results.parent / "evidence-stats.json").read_text(encoding="utf-8")):
                stats[(item["stack"], item["kit"], item["trial"])] = {
                    "tool_calls": item["tool_calls"], "plan_searches": item["plan_searches"]}
        for row in run_rows:
            if row.get("scenario") == "author" and (row["stack"], row["kit"], row["trial"]) in stats:
                row["_evidence"] = stats[(row["stack"], row["kit"], row["trial"])]
        rows += run_rows
    return rows


def _tool_inputs(stream: Path) -> list[str]:
    """Tool-call inputs from a Claude stream-json or a Codex `exec --json` stream, as text."""
    inputs = []
    for line in stream.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        message, item = event.get("message"), event.get("item")
        if isinstance(message, dict):  # Claude
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    inputs.append((block.get("name") or "") + " " + json.dumps(block.get("input")))
        elif event.get("type") == "item.completed" and isinstance(item, dict) and \
                item.get("type") in ("command_execution", "file_change"):  # Codex
            inputs.append(item["type"] + " " + json.dumps(item.get("command") or item.get("changes")))
    return inputs


def evidence_stats(evidence_dirs: list[Path]) -> dict[tuple[str, str, int], dict[str, int]]:
    """(stack, kit, trial) -> tool calls and plan-related lookups of each author run."""
    stats: dict[tuple[str, str, int], dict[str, int]] = {}
    for root in evidence_dirs:
        for call in sorted(path for path in root.iterdir() if path.is_dir()):
            prompt, stream = call / "prompt.txt", call / "stdout.raw"
            match = PROJECT.search(prompt.read_text(encoding="utf-8", errors="replace")) if prompt.is_file() else None
            if not match or not stream.is_file():
                continue
            inputs = _tool_inputs(stream)
            # The project directory name carries the case name, which may itself say "plan". Edits do not count.
            lookups = [text for text in inputs if not text.startswith(("Edit ", "Write ", "file_change "))
                       and PLAN_TERMS.search(PROJECT.sub("", text))]
            stats[(match["stack"], match["kit"], int(match["trial"]))] = {
                "tool_calls": len(inputs), "plan_searches": len(lookups)}
    return stats


def _passed(row: dict) -> bool:
    oracle = row.get("oracle") if isinstance(row.get("oracle"), dict) else {}
    return all(oracle.get(axis) for axis in ("behavior", "plan", "norm"))


def _mean(values: list[float]) -> str:
    return f"{statistics.mean(values):,.1f}" if values else "n/a"


def author_table(rows: list[dict], baseline: str = "before") -> list[str]:
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("scenario") == "author":
            groups[(row["stack"], row.get("provider") or "claude", row["kit"])].append(row)
    lines = ["| case | provider | kit | pass | behavior | plan | norm | p vs baseline | turns | tool calls | uncached in | output "
             "| cost | overclaims (eligible) | plan lookups |",
             "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|"]
    for (stack, provider, kit), group in sorted(groups.items()):
        oracle = [row.get("oracle") if isinstance(row.get("oracle"), dict) else {} for row in group]
        usage = [row.get("usage") or {} for row in group]
        passed = sum(map(_passed, group))
        base = groups.get((stack, provider, baseline))
        p_value = (f"{fisher_greater(sum(map(_passed, base)), len(base), passed, len(group)):.3f}"
                   if base and kit != baseline else "—")
        eligible = sum(bool(row.get("required_unread")) or o.get("behavior") is False or o.get("norm") is False
                       for row, o in zip(group, oracle))
        runs = [row["_evidence"] for row in group if "_evidence" in row]
        lookups = [run["plan_searches"] for run in runs]
        costs = [row["cost_usd"] for row in group if row.get("cost_usd") is not None]
        lines.append(
            f"| {stack} | {provider} | {kit} | {passed}/{len(group)} | {sum(bool(o.get('behavior')) for o in oracle)} "
            f"| {sum(bool(o.get('plan')) for o in oracle)} | {sum(bool(o.get('norm')) for o in oracle)} | {p_value} "
            f"| {_mean([row['turns'] for row in group if row.get('turns') is not None])} "
            f"| {_mean([run['tool_calls'] for run in runs])} "
            f"| {statistics.mean(u.get('uncached_input') or 0 for u in usage):,.0f} "
            f"| {statistics.mean(u.get('output') or 0 for u in usage):,.0f} "
            f"| {f'${sum(costs):.2f}' if costs else 'n/a'} "
            f"| {sum(bool(row.get('overclaims')) for row in group)} ({eligible}) "
            f"| {f'{sum(lookups)} in {sum(1 for n in lookups if n)}/{len(lookups)} runs' if lookups else '—'} |")
    return lines


def review_table(rows: list[dict]) -> list[str]:
    """Reviewer-level verdicts per kit × reviewer provider, then per-kit final verdicts and provider disagreement."""
    totals: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    finals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        if row.get("scenario") != "review":
            continue
        by_angle: dict[str, dict[str, str]] = defaultdict(dict)
        for angle in row.get("angles", []):
            bucket = totals[(row["kit"], angle.get("provider") or "claude")]
            verdict = angle.get("verdict")
            bucket["cost"] += angle.get("cost_usd") or 0
            bucket["uncached"] += (angle.get("usage") or {}).get("uncached_input") or 0
            bucket["calls"] += 1
            if verdict is None:
                bucket["unparsed"] += 1
                continue
            by_angle[angle.get("angle", "?")][angle.get("provider") or "claude"] = verdict
            if row["variant"] == "clean":
                bucket["clean"] += 1
                bucket["false_request_changes"] += verdict == "request-changes"
            else:
                bucket["defect"] += 1
                bucket["request_changes"] += verdict == "request-changes"
        final = finals[row["kit"]]
        final["rows"] += 1
        final["valid"] += bool(row.get("valid"))
        final["correct"] += bool(row.get("valid") and row.get("correct"))
        if row["variant"] != "clean":
            for found in (row.get("defects") or {}).values():
                final["seeded"] += 1
                final["found"] += bool(found)
        for verdicts in by_angle.values():
            if len(verdicts) > 1:
                final["paired"] += 1
                final["disagree"] += len(set(verdicts.values())) > 1
    if not totals:
        return []
    lines = ["| kit | reviewer | request-changes on defect variants | false request-changes on clean "
             "| unparsed verdicts | uncached in | cost |", "|---|---|---:|---:|---:|---:|---:|"]
    for (kit, provider), b in sorted(totals.items()):
        cost = f"${b['cost']:.2f}" if b["cost"] else "n/a"
        lines.append(f"| {kit} | {provider} | {b['request_changes']:.0f}/{b['defect']:.0f} "
                     f"| {b['false_request_changes']:.0f}/{b['clean']:.0f} "
                     f"| {b['unparsed']:.0f}/{b['calls']:.0f} ({b['unparsed'] / b['calls']:.0%}) "
                     f"| {b['uncached']:,.0f} | {cost} |")
    lines += ["", "| kit | valid rows | correct final verdicts (of valid) | seeded defects found | provider disagreement |",
              "|---|---:|---:|---:|---:|"]
    for kit, f in sorted(finals.items()):
        disagreement = f"{f['disagree']:.0f}/{f['paired']:.0f}" if f["paired"] else "—"
        lines.append(f"| {kit} | {f['valid']:.0f}/{f['rows']:.0f} | {f['correct']:.0f}/{f['valid']:.0f} "
                     f"| {f['found']:.0f}/{f['seeded']:.0f} | {disagreement} |")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+", help="phase_eval output directories or results.jsonl files")
    parser.add_argument("--baseline", default="before", help="kit name the p-values compare against")
    args = parser.parse_args(argv)
    rows = load(args.runs)
    print("\n".join(author_table(rows, args.baseline)))
    review = review_table(rows)
    if review:
        print("\n" + "\n".join(review))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
