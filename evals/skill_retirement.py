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
- `retire-candidate`: on every case that names the skill, the latest baseline (no skill) is at
  the ceiling with at least `--min-n` trials, and the skill configuration did not score higher.
  A candidate is not removed automatically: the ablation in #417 decides.
- `keep`: on some case the skill configuration scored above the baseline.
- `inconclusive`: anything else (baseline below the ceiling without a measured gain, too few trials).

A `run.py` row counts only when its `resolution` is true (the trials are interpretable). Early
summaries carry neither `case` nor `resolution`: they are attributed to the file's single case and
count only when every host run succeeded. Rows that still cannot be attributed are skipped and counted.
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
    """One entry per (file, case, config) with an interpretable score; the count of skipped rows."""
    entries, skipped = [], 0
    for path in sorted(paths):
        payload = json.loads(path.read_text(encoding="utf-8"))
        trials = payload.get("rows", [])
        # 초기 요약에는 case도 resolution도 없다. 파일의 trial이 한 case뿐이면 그 case로 보고,
        # 모든 host 실행이 성공했을 때만 해석 가능한 결과로 센다.
        only_case = {trial.get("case") for trial in trials}
        fallback_case = next(iter(only_case)) if len(only_case) == 1 else None
        all_ok = bool(trials) and all(trial.get("host_ok") for trial in trials)
        for row in payload.get("summary", []):
            case = row.get("case", fallback_case)
            resolved = row.get("resolution", all_ok if "case" not in row else None) is True
            if case is None or not resolved or row.get("both") is None:
                skipped += 1
                continue
            entries.append({"source": path.stem, "host": payload.get("host"), "case": case,
                            "config": row["config"], "score": row["both"], "n": row.get("n", 0)})
    return entries, skipped


def skill_task_measurements(paths: list[Path]) -> list[dict]:
    entries = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        grouped: dict[tuple[str, str], list[bool]] = defaultdict(list)
        for row in payload.get("results", []):
            if row.get("host_ok"):
                grouped[(row["case"], row["config"])].append(bool((row.get("score") or {}).get("passed")))
        source = str((payload.get("metadata") or {}).get("started_at") or path.parent.name)
        host = (payload.get("metadata") or {}).get("host", "codex")
        entries.extend({"source": source, "host": host, "case": case, "config": config,
                        "score": sum(passed) / len(passed), "n": len(passed)}
                       for (case, config), passed in grouped.items())
    return entries


def classify(skills: list[str], case_skills: dict[str, tuple[str, ...]], entries: list[dict],
             *, min_n: int = 5) -> dict[str, dict]:
    latest: dict[tuple[str, str], dict] = {}
    for entry in sorted(entries, key=lambda item: item["source"]):
        latest[(entry["case"], entry["config"])] = entry
    trends: dict[str, list[str]] = defaultdict(list)
    for entry in sorted(entries, key=lambda item: item["source"]):
        if entry["config"] == "baseline":
            trends[entry["case"]].append(f"{entry['source']}:{entry['score']:.2f}")
    table = {}
    for skill in skills:
        cases = sorted(case for case, names in case_skills.items() if skill in names)
        if not cases:
            table[skill] = {"status": "unverified", "cases": []}
            continue
        verdicts = []
        for case in cases:
            base = latest.get((case, "baseline"))
            with_skill = [entry for (name, config), entry in latest.items() if name == case and config != "baseline"]
            best = max((entry["score"] for entry in with_skill), default=None)
            if base is None:
                verdicts.append("inconclusive")
            elif best is not None and best > base["score"]:
                verdicts.append("keep")
            elif base["score"] >= 1.0 and base["n"] >= min_n:
                verdicts.append("ceiling")
            else:
                verdicts.append("inconclusive")
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
