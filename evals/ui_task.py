"""Score a UI build against a UI task's browser acceptance checks.

    python evals/ui_task.py validate evals/ui-tasks/<task>
    python evals/ui_task.py run evals/ui-tasks/<task> (--static DIR | --url URL) \
        [--spec SPEC-n ...] [--only CHECK_ID] [--repeat N] [--json out.json] [--summary out.md]

A UI task directory holds `prd.md` (Spec Items `SPEC-n` and a test-id contract table), `acceptance.json`
(browser checks, each tied to one Spec Item) and `fixtures/` (the only data source a build may read).

`validate` checks the task itself without a browser: unique check ids, every check names a Spec Item
from the PRD and every Spec Item has a check, every step kind is one the runner knows, and every
data-testid a check uses is in the PRD's contract.

`run` scores a build. `--static DIR` serves a built directory on 127.0.0.1 with SPA fallback (a path
that is not a file returns `index.html`) and serves the task's fixtures at `/fixtures/`; `--url`
scores an app that is already running. Each check runs in a fresh browser context through
`evals/ui_task_runner.mjs` (Node + Playwright). `--spec` limits the run to those Spec Items' checks, for a
round that builds one slice of the PRD. With `--repeat N` a check passes only if it passed
in every run, so a flaky build does not score as passing. The exit status is 0 only when every check
passed.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import re
import subprocess
import sys
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path

RUNNER = Path(__file__).resolve().parent / "ui_task_runner.mjs"
# Mirrors the `case` labels in ui_task_runner.mjs; tests/test_ui_task.py keeps the two in step.
STEP_KINDS = frozenset({
    "goto", "pause", "tick", "click", "focus", "fill", "type", "press", "select", "check", "uncheck", "reload",
    "expectVisible", "expectHidden", "expectCount", "expectText", "expectMoney", "expectDisabled", "expectEnabled",
    "expectAttr", "expectSelectedLabel", "expectValue", "expectUrl", "expectSorted",
})
STEP_OPTIONS = frozenset({"nth", "value", "key", "label", "equals", "contains", "matches", "notContains", "eq", "min",
                          "max", "name", "order", "delay", "hasText", "last"})
TARGETLESS = frozenset({"goto", "pause", "tick", "reload", "expectUrl"})
SPEC_RE = re.compile(r"^(SPEC-\d+):", re.MULTILINE)


def load_task(task_dir: Path) -> tuple[str, dict]:
    return (task_dir / "prd.md").read_text(encoding="utf-8"), json.loads(
        (task_dir / "acceptance.json").read_text(encoding="utf-8"))


def contract_patterns(prd: str) -> list[re.Pattern[str]]:
    """Test ids from the PRD's contract table; a placeholder such as `filter-<categoryId>` matches any suffix."""
    section = prd.split("## Test-id contract", 1)[-1].split("\n## ", 1)[0]
    ids = {token for line in section.splitlines() if line.startswith("|")
           for token in re.findall(r"`([^`]+)`", line.split("|")[2] if line.count("|") >= 3 else "")}
    return [re.compile("^" + re.sub(r"<[^>]+>", ".+", re.escape(i)) + "$")
            for i in sorted(ids)]


def step_testids(target) -> list[str]:
    if isinstance(target, str):
        return [target]
    if isinstance(target, dict):
        return [target.get("testid", "")] + (step_testids(target["within"]) if "within" in target else [])
    return []


def validate(prd: str, acceptance: dict) -> list[str]:
    problems = []
    specs = SPEC_RE.findall(prd)
    patterns = contract_patterns(prd)
    if not specs:
        problems.append("prd.md has no Spec Items (lines starting with `SPEC-n:`)")
    if not patterns:
        problems.append("prd.md has no test-id contract table")
    seen, covered = set(), set()
    for check in acceptance.get("checks", []):
        cid = check.get("id", "?")
        if cid in seen:
            problems.append(f"{cid}: duplicate check id")
        seen.add(cid)
        if check.get("spec") not in specs:
            problems.append(f"{cid}: spec {check.get('spec')!r} is not a Spec Item in prd.md")
        covered.add(check.get("spec"))
        if not check.get("steps"):
            problems.append(f"{cid}: no steps")
        for index, step in enumerate(check.get("steps", [])):
            kinds = [key for key in step if key not in STEP_OPTIONS]
            if len(kinds) != 1 or kinds[0] not in STEP_KINDS:
                problems.append(f"{cid} step {index}: unknown or ambiguous step {sorted(step)}")
                continue
            if kinds[0] in TARGETLESS:
                continue
            for testid in step_testids(step[kinds[0]]):
                if not any(p.match(testid) for p in patterns):
                    problems.append(f"{cid} step {index}: test id {testid!r} is not in the prd.md contract")
    problems += [f"{spec}: no acceptance check" for spec in specs if spec not in covered]
    return problems


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, root: Path, fixtures: Path, **kwargs):
        self._root, self._fixtures = root, fixtures
        super().__init__(*args, directory=str(root), **kwargs)

    def translate_path(self, path: str) -> str:
        clean = path.split("?", 1)[0].split("#", 1)[0]
        if clean.startswith("/fixtures/"):
            candidate = (self._fixtures / clean[len("/fixtures/"):]).resolve()
            if candidate.is_relative_to(self._fixtures) and candidate.is_file():
                return str(candidate)
        resolved = Path(super().translate_path(path))
        if resolved.exists():
            return str(resolved)
        return str(self._root / "index.html")

    def log_message(self, format, *args):  # noqa: A002 - signature from the base class
        pass


def serve_static(root: Path, fixtures: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = functools.partial(_Handler, root=root.resolve(), fixtures=fixtures.resolve())
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def run_checks(acceptance_path: Path, base_url: str, only: str | None) -> dict:
    command = ["node", str(RUNNER), str(acceptance_path), base_url] + (["--only", only] if only else [])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError(f"runner failed ({completed.returncode}): {completed.stderr.strip()[-2000:]}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


def merge_runs(runs: list[dict]) -> list[dict]:
    """One row per check; it passes only if it passed in every run, and keeps the first failure seen."""
    merged: OrderedDict[str, dict] = OrderedDict()
    for run in runs:
        for result in run["results"]:
            row = merged.setdefault(result["id"], {**result, "runs": 0, "passes": 0})
            row["runs"] += 1
            row["passes"] += bool(result["passed"])
            if not result["passed"] and row["passed"]:
                row.update(passed=False, failure=result["failure"], page_errors=result["page_errors"])
    return list(merged.values())


def score(results: list[dict], specs: list[str]) -> dict:
    by_spec = OrderedDict((spec, {"passed": 0, "total": 0}) for spec in specs)
    for row in results:
        bucket = by_spec.setdefault(row["spec"], {"passed": 0, "total": 0})
        bucket["total"] += 1
        bucket["passed"] += bool(row["passed"])
    passed = sum(bool(row["passed"]) for row in results)
    return {"checks_passed": passed, "checks_total": len(results),
            "specs_passed": sum(1 for b in by_spec.values() if b["total"] and b["passed"] == b["total"]),
            "specs_total": len(by_spec), "by_spec": by_spec}


def summary_markdown(task: str, base: str, totals: dict, results: list[dict]) -> str:
    lines = [f"# UI task `{task}`", "", f"- Build: `{base}`",
             f"- Checks: {totals['checks_passed']}/{totals['checks_total']}",
             f"- Spec Items fully passing: {totals['specs_passed']}/{totals['specs_total']}", "",
             "| Spec Item | Checks passed |", "|---|---|"]
    lines += [f"| {spec} | {b['passed']}/{b['total']} |" for spec, b in totals["by_spec"].items()]
    failures = [row for row in results if not row["passed"]]
    if failures:
        lines += ["", "## Failures", ""]
        for row in failures:
            failure = row.get("failure") or {}
            lines.append(f"- `{row['id']}` ({row['spec']}, {row.get('passes', 0)}/{row.get('runs', 1)} runs): step "
                         f"{failure.get('step')} `{json.dumps(failure.get('action'))}`: {failure.get('message')}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("validate", help="check the task's PRD and acceptance checks without a browser")
    check.add_argument("task", type=Path)
    run = sub.add_parser("run", help="score a build")
    run.add_argument("task", type=Path)
    where = run.add_mutually_exclusive_group(required=True)
    where.add_argument("--static", type=Path, help="built directory to serve with SPA fallback")
    where.add_argument("--url", help="base URL of a running app")
    run.add_argument("--spec", action="append", default=[], help="run only this Spec Item's checks (repeatable)")
    run.add_argument("--only", help="run one check id")
    run.add_argument("--repeat", type=int, default=1, help="runs per check; a check must pass in all of them")
    run.add_argument("--json", type=Path, help="write the scored result as JSON")
    run.add_argument("--summary", type=Path, help="write a Markdown summary")
    args = parser.parse_args(argv)

    prd, acceptance = load_task(args.task)
    problems = validate(prd, acceptance)
    if args.command == "validate" or problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        if args.command == "validate" and not problems:
            print(f"{args.task}: {len(acceptance['checks'])} checks over {len(SPEC_RE.findall(prd))} Spec Items OK")
        return 1 if problems else 0

    specs = SPEC_RE.findall(prd)
    unknown = sorted(set(args.spec) - set(specs))
    if unknown:
        parser.error(f"unknown Spec Item(s): {', '.join(unknown)}")
    acceptance_path = args.task / "acceptance.json"
    scratch = tempfile.TemporaryDirectory()
    if args.spec:
        specs = [spec for spec in specs if spec in args.spec]
        acceptance_path = Path(scratch.name) / "acceptance.json"
        acceptance_path.write_text(json.dumps({**acceptance, "checks": [
            check for check in acceptance["checks"] if check["spec"] in args.spec]}), encoding="utf-8")

    server = None
    if args.static:
        if not (args.static / "index.html").is_file():
            parser.error(f"{args.static} has no index.html")
        server, base = serve_static(args.static, args.task / "fixtures")
    else:
        base = args.url
    try:
        runs = [run_checks(acceptance_path, base, args.only) for _ in range(max(1, args.repeat))]
    finally:
        if server:
            server.shutdown()
        scratch.cleanup()
    results = merge_runs(runs)
    totals = score(results, specs if not args.only else sorted({r["spec"] for r in results}))
    label = str(args.static or args.url)
    report = {"task": acceptance["task"], "version": acceptance["version"], "build": label, "repeat": args.repeat,
              **totals, "results": results}
    if args.json:
        args.json.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    text = summary_markdown(acceptance["task"], label, totals, results)
    if args.summary:
        args.summary.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if totals["checks_passed"] == totals["checks_total"] and results else 1


if __name__ == "__main__":
    sys.exit(main())
