"""Behavior (B) and discovery checks for a bundled skill, following work-skills validation.

B: a fresh Claude session gets only the task request, the case fixture and the skill's own files,
does the work in a scratch copy, and is judged by evaluator-only criteria it never sees. The same
request runs without the skill as the baseline, because an improvement claim needs one.
Discovery: with competing bundled skills installed, record which skill the host actually loads.

    python evals/skill_behavior.py --skill qa --scenario behavior --config skill,baseline --output <new dir>
    python evals/skill_behavior.py --skill qa --scenario discovery --output <new dir>
    python evals/skill_behavior.py --skill qa --scenario behavior --rejudge --output <that dir>

Needs an authenticated `claude` CLI and costs model usage. Each criterion is pass, fail, unverified
or not-applicable; a required fail fails the case, a required unverified blocks a verified claim.
Only mechanical criteria are judged here; anything else stays unverified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
ARTIFACT = "qa.md"
PRODUCED_BY_PROJECT = {"e2e-report.json"}
# Tolerates markdown around the value ("**Status: green**", "Status: `blocked`").
STATUS = re.compile(r"status\W{0,6}(green|request-changes|blocked|error)\b", re.IGNORECASE)


def _digest_tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not ({".git", "skill", "__pycache__"} & set(path.relative_to(root).parts))
    }


def _status(text: str) -> str | None:
    # Two different statuses give the runner contradictory routing: that is no status, not the last one.
    found = {value.lower() for value in STATUS.findall(text)}
    return found.pop() if len(found) == 1 else None


def _port_free(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def judge(criterion: dict, before: dict, after: dict, artifact: str | None) -> tuple[str, str]:
    """Return (verdict, evidence) for one mechanical criterion."""
    method = criterion["method"]
    if not method.startswith("mechanical:"):
        return "unverified", "not mechanically judged"
    kind = method.split(":", 1)[1]
    if kind == "unchanged":
        changed = sorted(
            path for path in set(before) | set(after)
            if before.get(path) != after.get(path) and path != ARTIFACT and path not in PRODUCED_BY_PROJECT
        )
        return ("fail", f"changed {changed}") if changed else ("pass", "fixture unchanged")
    if artifact is None:
        return "fail", f"{ARTIFACT} missing"
    status = _status(artifact)
    if kind == "status":
        return ("pass", f"status {status}") if status else ("fail", "no status line")
    if kind.startswith("status="):
        return ("pass" if status == kind[7:] else "fail"), f"status {status}"
    if kind.startswith("status!="):
        return ("pass" if status and status != kind[8:] else "fail"), f"status {status}"
    if kind.startswith("mentions="):
        words = kind[9:].split("|")
        hit = [word for word in words if word.lower() in artifact.lower()]
        return ("pass", f"mentions {hit}") if hit else ("unverified", "none of the expected words; judge manually")
    if kind.startswith("mentions-all="):
        missing = [word for word in kind[13:].split("|") if word.lower() not in artifact.lower()]
        return ("fail", f"missing {missing}") if missing else ("pass", "all mentioned")
    if kind.startswith("port-free="):
        port = int(kind[10:])
        return ("pass" if _port_free(port) else "fail"), f"port {port}"
    return "unverified", f"unknown method {kind}"


def _settle(record: dict) -> None:
    required = [v for v in record["criteria"] if v["required"]]
    record["passed"] = all(v["verdict"] != "fail" for v in required)
    record["verified"] = record["passed"] and all(v["verdict"] in ("pass", "not-applicable") for v in required)


def rejudge(output: Path, criteria: list[dict]) -> list[dict]:
    """Re-judge saved artifacts after a judge fix. Workspace checks (unchanged, port) keep their
    recorded verdict because the workspace is gone."""
    records = json.loads((output / "report.json").read_text(encoding="utf-8"))
    by_id = {criterion["id"]: criterion for criterion in criteria}
    for record in records:
        path = output / record["config"] / record["case"] / ARTIFACT
        artifact = path.read_text(encoding="utf-8") if path.is_file() else None
        record["status"] = _status(artifact or "")
        for verdict in record["criteria"]:
            criterion = by_id[verdict["id"]]
            if verdict["verdict"] == "not-applicable" or criterion["method"] in (
                    "mechanical:unchanged",) or criterion["method"].startswith("mechanical:port-free="):
                continue
            verdict["verdict"], verdict["evidence"] = judge(criterion, {}, {}, artifact)
        _settle(record)
    return records


def run_claude(prompt: str, cwd: Path, *, model: str | None, budget: float, timeout: int,
               stream: bool = False, max_turns: int | None = None) -> subprocess.CompletedProcess:
    argv = ["claude", "-p", "--output-format", "stream-json" if stream else "json",
            "--permission-mode", "acceptEdits", "--max-budget-usd", str(budget),
            "--allowedTools", "Read,Glob,Grep,Write,Edit,Bash,Skill",
            "--disallowedTools", "WebFetch,WebSearch,Agent"]
    if stream:
        argv.append("--verbose")
    if max_turns:
        argv += ["--max-turns", str(max_turns)]
    if model:
        argv += ["--model", model]
    # Own session, and the whole group is killed afterwards: a server the executor left running would
    # otherwise answer the next case's requests on the same port.
    process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(prompt, timeout=timeout)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def behavior(skill: str, config: str, task: dict, criteria: list[dict], args: argparse.Namespace,
             evals: Path, output: Path) -> dict:
    work = Path(tempfile.mkdtemp(prefix=f"af-skill-{task['id']}-"))
    try:
        shutil.copytree(evals / "fixtures" / task["fixture"], work, dirs_exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=work, check=True)
        prompt = task["request"]
        if config == "skill":
            shutil.copytree(REPO / "skills" / skill, work / "skill")
            prompt = f"Use the skill at ./skill/SKILL.md (and its references as it directs).\n\n{prompt}"
        before = _digest_tree(work)
        process = run_claude(prompt, work, model=args.model, budget=args.budget_usd, timeout=args.timeout)
        after = _digest_tree(work)
        artifact_path = work / ARTIFACT
        artifact = artifact_path.read_text(encoding="utf-8", errors="replace") if artifact_path.is_file() else None
        try:
            payload = json.loads(process.stdout)
        except json.JSONDecodeError:
            payload = {}
        verdicts = []
        for criterion in criteria:
            applies = criterion["case"] == "*" or task["id"] in criterion["case"].split(",")
            verdict, evidence = judge(criterion, before, after, artifact) if applies else ("not-applicable", "other case")
            verdicts.append({"id": criterion["id"], "required": criterion["required"], "verdict": verdict,
                             "evidence": evidence})
        record = {"case": task["id"], "split": task["split"], "config": config, "rc": process.returncode,
                  "cost_usd": payload.get("total_cost_usd"), "status": _status(artifact or ""),
                  "criteria": verdicts}
        _settle(record)
        case_dir = output / config / task["id"]
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "stdout.json").write_text(process.stdout, encoding="utf-8")
        if artifact is not None:
            (case_dir / ARTIFACT).write_text(artifact, encoding="utf-8")
        return record
    finally:
        shutil.rmtree(work, ignore_errors=True)


def discovery(skill: str, case: dict, competitors: list[str], args: argparse.Namespace, output: Path) -> dict:
    work = Path(tempfile.mkdtemp(prefix=f"af-discovery-{case['id']}-"))
    try:
        subprocess.run(["git", "init", "-q"], cwd=work, check=True)
        for name in [skill, *competitors]:
            shutil.copytree(REPO / "skills" / name, work / ".claude" / "skills" / name)
        # The request goes in as a user would write it; a "plan only" suffix suppressed every skill load.
        # A few turns are enough to see the first skill choice.
        process = run_claude(case["request"], work, model=args.model, budget=args.budget_usd, timeout=args.timeout,
                             stream=True, max_turns=4)
        loaded = []
        for line in process.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Skill":
                    loaded.append(str((block.get("input") or {}).get("skill", "")).rsplit(":", 1)[-1])
        selected = skill in loaded
        correct = selected if case["expect"] == skill else not selected
        (output / "discovery").mkdir(parents=True, exist_ok=True)
        (output / "discovery" / f"{case['id']}.jsonl").write_text(process.stdout, encoding="utf-8")
        return {"case": case["id"], "split": case["split"], "expect": case["expect"], "loaded": loaded,
                "correct": correct}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill", required=True)
    parser.add_argument("--scenario", choices=("behavior", "discovery"), required=True)
    parser.add_argument("--config", default="skill,baseline")
    parser.add_argument("--split", choices=("dev", "heldout", "all"), default="all")
    parser.add_argument("--case", action="append")
    parser.add_argument("--model")
    parser.add_argument("--budget-usd", type=float, default=1.0, help="per executor session")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rejudge", action="store_true", help="re-judge an existing behavior --output")
    args = parser.parse_args(argv)
    evals = HERE / "skill-behavior" / args.skill
    if not args.rejudge:
        args.output.mkdir(parents=True, exist_ok=False)
    keep = lambda item: (args.split == "all" or item["split"] == args.split) and (  # noqa: E731
        not args.case or item["id"] in args.case)
    if args.rejudge:
        criteria = json.loads((evals / "expectations.json").read_text(encoding="utf-8"))["criteria"]
        records = rejudge(args.output, criteria)
    elif args.scenario == "behavior":
        tasks = [task for task in json.loads((evals / "tasks.json").read_text(encoding="utf-8")) if keep(task)]
        criteria = json.loads((evals / "expectations.json").read_text(encoding="utf-8"))["criteria"]
        records = [behavior(args.skill, config, task, criteria, args, evals, args.output)
                   for config in args.config.split(",") for task in tasks]
    else:
        spec = json.loads((evals / "discovery.json").read_text(encoding="utf-8"))
        records = [discovery(args.skill, case, spec["competitors"], args, args.output)
                   for case in spec["cases"] if keep(case)]
    (args.output / "report.json").write_text(json.dumps(records, indent=1), encoding="utf-8")
    for record in records:
        if args.scenario == "behavior":
            failed = [v["id"] for v in record["criteria"] if v["required"] and v["verdict"] == "fail"]
            print(f"{record['config']:8} {record['case']:26} {record['split']:7} status={record['status']} "
                  f"{'PASS' if record['passed'] else 'FAIL'}{' (unverified)' if record['passed'] and not record['verified'] else ''}"
                  f"{' ' + str(failed) if failed else ''}")
        else:
            print(f"{record['case']} {record['split']:7} expect={record['expect']:6} loaded={record['loaded']} "
                  f"{'ok' if record['correct'] else 'WRONG'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
