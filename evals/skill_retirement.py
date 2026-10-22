"""Which skills a model may no longer need, and which skills nothing measures (#418).

    python evals/skill_retirement.py [--run-results 'evals/results/2026*.json'] \
        [--skill-task-report <dir>/report.json ...] [--min-n 5]
    python evals/skill_retirement.py --plan --host claude --model <model>

Reads results already recorded by `run.py` (case x config, oracle-scored) and `skill_tasks.py`
(`report.json`, baseline vs skill-index). Nothing here calls a model; `--plan` prints the commands
that re-measure every baseline after a model or host upgrade.

For each skill under `skills/`:

- `unverified`: no isolated behavior case names it. phase_eval delivers skills in bundles, so it
  does not count here.
- `retire-candidate`: on every case that names the skill, the latest run that measured both the
  baseline (no skill) and a skill configuration has the baseline at the ceiling, the skill
  configuration no higher, and both with at least `--min-n` valid trials.
  A candidate is not removed automatically: the ablation in #417 decides.
- `keep`: on some case the skill configuration scored above the baseline in that same run, both
  with at least `--min-n` valid trials.
- `inconclusive`: anything else (baseline below the ceiling without a measured gain, too few trials).

Baseline and skill scores are compared only within one result file or report (same host, model and
kit), never across runs. A `run.py` row counts unless its `resolution` is false; early summaries
without `case` are attributed to the file's single case. Pass `--skill-task-report` oldest first.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KIT = ROOT.parent
sys.path.insert(0, str(ROOT))
from configs import CASE_NORM_SKILLS  # noqa: E402


def skill_names(kit: Path = KIT) -> list[str]:
    return sorted(path.parent.name for path in (kit / "skills").glob("*/SKILL.md"))


def skill_task_cases(case_root: Path = ROOT / "skill-task-cases") -> dict[str, tuple[str, ...]]:
    cases: dict[str, tuple[str, ...]] = {}
    for path in sorted(case_root.glob("*.json")):
        for case in json.loads(path.read_text(encoding="utf-8")).get("cases", []):
            cases[case["id"]] = (case["skill"],)
    return cases


def run_measurements(paths: list[Path]) -> tuple[list[dict], int]:
    """One entry per (file, case, config) with an interpretable score; the count of skipped rows.

    `run.py` names its files by start time, so the file name orders them. It writes `resolution`
    only for cases that need a balanced config matrix; elsewhere `n` already counts valid trials.
    """
    entries, skipped = [], 0
    for path in sorted(paths, key=lambda item: item.name):
        payload = json.loads(path.read_text(encoding="utf-8"))
        # 초기 요약에는 case가 없다. 파일의 trial이 한 case뿐일 때만 그 case로 본다.
        cases = {trial.get("case") for trial in payload.get("rows", [])}
        fallback_case = next(iter(cases)) if len(cases) == 1 else None
        for row in payload.get("summary", []):
            case = row.get("case", fallback_case)
            if case is None or row.get("resolution", True) is not True or row.get("both") is None:
                skipped += 1
                continue
            entries.append({"source": path.stem, "order": ("run", path.name), "host": payload.get("host"),
                            "case": case, "config": row["config"], "score": row["both"], "n": row.get("n", 0)})
    return entries, skipped


def skill_task_measurements(paths: list[Path]) -> list[dict]:
    """`report.json` has no timestamp, so the order the reports are given is their chronology."""
    entries = []
    for index, path in enumerate(paths):
        payload = json.loads(path.read_text(encoding="utf-8"))
        grouped: dict[tuple[str, str], list[bool]] = defaultdict(list)
        for row in payload.get("results", []):
            if row.get("host_ok"):
                grouped[(row["case"], row["config"])].append(bool((row.get("score") or {}).get("passed")))
        entries.extend({"source": f"report{index + 1}", "order": ("skill_tasks", index), "host": "codex",
                        "case": case, "config": config, "score": sum(passed) / len(passed), "n": len(passed)}
                       for (case, config), passed in grouped.items())
    return entries


def _case_verdict(case_entries: list[dict], min_n: int) -> str:
    """Compare a baseline only with skill configurations from the same run: same host, model and kit."""
    by_source: dict[tuple, dict[str, dict]] = defaultdict(dict)
    for entry in case_entries:
        by_source[entry["order"]][entry["config"]] = entry
    paired = [configs for _, configs in sorted(by_source.items())
              if "baseline" in configs and len(configs) > 1]
    if not paired:
        return "inconclusive"
    configs = paired[-1]
    base = configs["baseline"]
    best = max((entry for config, entry in configs.items() if config != "baseline"), key=lambda entry: entry["score"])
    if base["n"] < min_n or best["n"] < min_n:
        return "inconclusive"
    if best["score"] > base["score"]:
        return "keep"
    return "ceiling" if base["score"] >= 1.0 else "inconclusive"


def classify(skills: list[str], case_skills: dict[str, tuple[str, ...]], entries: list[dict],
             *, min_n: int = 5) -> dict[str, dict]:
    by_case: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        by_case[entry["case"]].append(entry)
    trends = {
        case: [f"{entry['source']}:{entry['score']:.2f}"
               for entry in sorted(items, key=lambda item: item["order"]) if entry["config"] == "baseline"]
        for case, items in by_case.items()
    }
    table = {}
    for skill in skills:
        cases = sorted(case for case, names in case_skills.items() if skill in names)
        if not cases:
            table[skill] = {"status": "unverified", "cases": []}
            continue
        verdicts = [_case_verdict(by_case.get(case, []), min_n) for case in cases]
        status = ("keep" if "keep" in verdicts else
                  "retire-candidate" if all(verdict == "ceiling" for verdict in verdicts) else "inconclusive")
        table[skill] = {"status": status, "cases": cases,
                        "trend": {case: trends.get(case, []) for case in cases}}
    return table


def markdown(table: dict[str, dict], *, skipped: int) -> str:
    counts = defaultdict(int)
    for item in table.values():
        counts[item["status"]] += 1
    lines = [f"{len(table)} skills: " + ", ".join(f"{status} {counts[status]}" for status in
                                                   ("retire-candidate", "keep", "inconclusive", "unverified")),
             f"Skipped {skipped} recorded rows that cannot be attributed or interpreted.", "",
             "| skill | status | cases | baseline trend |", "|---|---|---|---|"]
    order = {"retire-candidate": 0, "keep": 1, "inconclusive": 2, "unverified": 3}
    for skill, item in sorted(table.items(), key=lambda pair: (order[pair[1]["status"]], pair[0])):
        trend = "; ".join(f"{case} {' → '.join(points) or '-'}" for case, points in item.get("trend", {}).items())
        lines.append(f"| {skill} | {item['status']} | {', '.join(item['cases']) or '-'} | {trend or '-'} |")
    return "\n".join(lines) + "\n"


def plan(host: str, model: str | None, trials: int) -> list[str]:
    model_flag = f" --model {model}" if model else ""
    commands = [f"python3 evals/run.py --host {host} --trials {trials} --config baseline --case {case}"
                for case in sorted(CASE_NORM_SKILLS)]
    commands.append(f"python3 evals/skill_tasks.py{model_flag} --trials {trials} --output <new dir>")
    return commands


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-results", default=str(ROOT / "results" / "2026*.json"),
                        help="glob of run.py result files")
    parser.add_argument("--skill-task-report", type=Path, action="append", default=[])
    parser.add_argument("--min-n", type=int, default=5)
    parser.add_argument("--plan", action="store_true", help="print the baseline re-measurement commands")
    parser.add_argument("--host", default="claude")
    parser.add_argument("--model")
    parser.add_argument("--trials", type=int, default=5)
    args = parser.parse_args(argv)
    if args.plan:
        print("\n".join(plan(args.host, args.model, args.trials)))
        return 0
    entries, skipped = run_measurements([Path(path) for path in glob.glob(args.run_results)])
    entries += skill_task_measurements(args.skill_task_report)
    case_skills = {**{case: tuple(names) for case, names in CASE_NORM_SKILLS.items()}, **skill_task_cases()}
    print(markdown(classify(skill_names(), case_skills, entries, min_n=args.min_n), skipped=skipped), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
