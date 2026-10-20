"""Measure whether the runner makes the right skills required for a task, per profile.

Static, model-free. For every profile named in `routing-cases.json` this installs the
kit under test into a throwaway project, then asks that kit's resolver which skills a
phase requires for each case's task text and changed files. A case passes when every
skill it lists under `required` is required and none it lists under `absent` is.

It answers "does routing pick the skill", not "does the model read or apply it". The
corpus mixes exact vocabulary, paraphrases and look-alike negatives, and splits cases
into `dev` and `heldout` so a routing change tuned on one half can be checked on the
other.

    python evals/skill_routing.py --kit /path/to/kit --output routing.json
    python evals/skill_routing.py --compare before.json after.json
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import phase_budget  # noqa: E402  (shares the install fixture with the budget measurement)

CASES = HERE / "routing-cases.json"


def load_corpus(path: Path = CASES) -> dict:
    corpus = json.loads(path.read_text(encoding="utf-8"))
    aliases = corpus["skills"]
    seen: set[str] = set()
    for case in corpus["cases"]:
        if case["id"] in seen:
            raise ValueError(f"duplicate case id: {case['id']}")
        seen.add(case["id"])
        if case["profile"] not in phase_budget.PROFILES:
            raise ValueError(f"{case['id']}: unknown profile {case['profile']!r}")
        if case["profile"] not in corpus["neutral_files"]:
            raise ValueError(f"{case['id']}: no neutral file for profile {case['profile']!r}")
        if case.get("split") not in {"dev", "heldout"}:
            raise ValueError(f"{case['id']}: split must be dev or heldout")
        expected = [*case.get("required", ()), *case.get("absent", ())]
        if not expected:
            raise ValueError(f"{case['id']}: a case must name required or absent skills")
        unknown = [alias for alias in expected if alias not in aliases]
        if unknown:
            raise ValueError(f"{case['id']}: unknown skill aliases {unknown}")
    return corpus


def score_case(case: dict, aliases: dict[str, str], required: set[str]) -> dict:
    missing = [alias for alias in case.get("required", ()) if aliases[alias] not in required]
    unexpected = [alias for alias in case.get("absent", ()) if aliases[alias] in required]
    return {"passed": not missing and not unexpected, "missing": missing, "unexpected": unexpected}


def measure_profile(kit: Path, profile: str, cases: list[dict], aliases: dict[str, str],
                    phase_id: str, neutral_file: str, root: Path) -> dict:
    sys.path.insert(0, str(kit / "src"))
    os.environ["AGENT_FLOW_HOST"] = "claude"
    spec = phase_budget.PROFILES[profile]
    for rel, body in dict(spec["files"]).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    phase_budget._git(root, "init", "-q", "-b", "main")
    phase_budget._git(root, "add", "-A")
    phase_budget._git(root, "commit", "-qm", "seed")
    error = phase_budget._install(kit, root, profile, "clean")
    if error:
        return {"profile": profile, "install_error": error}

    from agent_flow.adapters.hosted import HostedAdapter
    from agent_flow.artifact import create_run
    from agent_flow.runner import Runner

    run_dir = create_run(root, "full-feature", "routing measurement", run_id="routing")
    runner = Runner(root, workflow="full-feature", run_dir=run_dir)
    phase = next(p for p in runner.phases if p.id == phase_id)
    adapter = HostedAdapter("claude")
    adapter._profile_id = runner.profile_id
    adapter._profile_snapshot = runner.profile
    adapter._config_root = runner.config_root
    # Every case changes the same file, chosen so that no path selector routes a measured
    # skill (a profile's baseline file can be an app-shell path); only the task text differs.
    adapter._changed_files = (neutral_file,)
    results = {}
    for case in cases:
        adapter._task_text = case["task"]
        try:
            resolution = adapter.phase_resolution(phase, root, skill_host="claude", run_dir=run_dir)
        except Exception as exc:  # A broken case is recorded, not allowed to hide the others.
            results[case["id"]] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            continue
        required = {skill.name for skill in resolution.required}
        results[case["id"]] = {
            **score_case(case, aliases, required),
            "required": sorted(required),
            "optional": sorted(skill.name for skill in resolution.optional),
        }
    return {"profile": profile, "cases": results}


def _worker(kit: str, profile: str, corpus_path: str) -> dict:
    root = Path(tempfile.mkdtemp(prefix=f"af-routing-{profile}-"))
    try:
        result = phase_budget._run_group(
            [sys.executable, __file__, "--kit", kit, "--one", profile,
             "--root", str(root), "--cases", corpus_path],
            timeout=1800,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if result.returncode != 0:
        return {"profile": profile, "worker_error": result.stderr[-800:]}
    return json.loads(result.stdout)


def summarize(corpus: dict, profiles: list[dict]) -> dict:
    by_id = {case["id"]: case for case in corpus["cases"]}
    totals: dict[str, dict[str, int]] = {}
    for entry in profiles:
        for case_id, result in entry.get("cases", {}).items():
            case = by_id[case_id]
            for key in ("all", f"split:{case['split']}", f"kind:{case['kind']}", f"lang:{case['lang']}"):
                bucket = totals.setdefault(key, {"passed": 0, "total": 0})
                bucket["total"] += 1
                bucket["passed"] += int(bool(result.get("passed")))
    return totals


def compare(before: dict, after: dict) -> tuple[list[str], list[str]]:
    """Return (failures, notes). A case that passed before must still pass."""
    failures: list[str] = []
    notes: list[str] = []
    before_cases = _flatten(before)
    after_cases = _flatten(after)
    measured = [case_id for case_id, result in before_cases.items() if "passed" in result]
    if before_cases and not measured:
        failures.append("before measured no case; there is no baseline to compare against")
    for case_id in measured:
        result = after_cases.get(case_id)
        if result is None or "passed" not in result:
            failures.append(f"{case_id}: measured before but not after")
        elif before_cases[case_id]["passed"] and not result["passed"]:
            failures.append(f"{case_id}: regressed (missing {result['missing']}, unexpected {result['unexpected']})")
        elif not before_cases[case_id]["passed"] and result["passed"]:
            notes.append(f"{case_id}: now passes")
    for entry in after.get("profiles", ()):
        if "install_error" in entry or "worker_error" in entry:
            failures.append(f"{entry['profile']}: {entry.get('install_error') or entry.get('worker_error')}")
    return failures, notes


def _flatten(payload: dict) -> dict[str, dict]:
    return {
        case_id: result
        for entry in payload.get("profiles", ())
        for case_id, result in entry.get("cases", {}).items()
    }


def render_report(before: dict | None, after: dict) -> str:
    lines = ["## skill routing", "", "|bucket|" + ("before|" if before else "") + "after|",
             "|---|" + ("---:|" if before else "") + "---:|"]
    after_totals = after["summary"]
    before_totals = before["summary"] if before else {}
    for key in sorted(after_totals, key=lambda k: (k != "all", k)):
        cell = lambda t: f"{t['passed']}/{t['total']}" if t else "-"  # noqa: E731
        row = f"|{key}|" + (f"{cell(before_totals.get(key))}|" if before else "") + f"{cell(after_totals[key])}|"
        lines.append(row)
    failing = [case_id for case_id, result in _flatten(after).items() if not result.get("passed")]
    if failing:
        lines += ["", "Failing after: " + ", ".join(sorted(failing))]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kit", type=Path)
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--one")
    parser.add_argument("--root", type=Path, help="temporary project for --one (owned by the parent)")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("BEFORE", "AFTER"))
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()

    if args.compare:
        before, after = (json.loads(path.read_text(encoding="utf-8")) for path in args.compare)
        failures, notes = compare(before, after)
        report = render_report(before, after)
        report += "".join(f"\n- {note}" for note in notes)
        report += "".join(f"\n- FAIL {failure}" for failure in failures)
        report += "\n\n" + ("Routing regression found.\n" if failures else "No routing regression found.\n")
        if args.summary:
            args.summary.write_text(report, encoding="utf-8")
        print(report)
        return 1 if failures else 0

    if args.kit is None:
        parser.error("--kit is required unless --compare is given")
    kit = args.kit.resolve()
    corpus = load_corpus(args.cases)
    aliases = corpus["skills"]
    if args.one:
        cases = [case for case in corpus["cases"] if case["profile"] == args.one]
        print(json.dumps(measure_profile(
            kit, args.one, cases, aliases, corpus["phase"], corpus["neutral_files"][args.one], args.root,
        )))
        return 0
    profiles = sorted({case["profile"] for case in corpus["cases"]})
    with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
        results = list(pool.map(lambda p: _worker(str(kit), p, str(args.cases.resolve())), profiles))
    payload = {"kit": str(kit), "phase": corpus["phase"], "profiles": results}
    payload["summary"] = summarize(corpus, results)
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
