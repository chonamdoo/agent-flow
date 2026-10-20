#!/usr/bin/env python3
"""Autoresearch loop for Agent Flow: propose one edit, score it, keep it or reset it.

Modelled on karpathy/autoresearch: one editable surface, a fixed evaluator the proposer cannot
see or change, a single score, git as the ratchet (keep = commit stays, discard = reset to the
best commit), a simplicity tie-break, and `results.tsv` as the record.

    python tools/autoresearch/loop.py --track routing --tag routing-1 --iterations 20 --budget-usd 5
    python tools/autoresearch/loop.py --track model --tag model-1 --skills react-clean-presentation-architecture \
        --stacks commerce --modes clean --trials 3 --iterations 10 --budget-usd 60

The proposer is a fresh `claude -p` session confined to a separate worktree: Read/Grep/Glob/Edit
only, no shell, the evaluation corpus removed from its checkout and denied by path. The evaluator
runs from this repository against the worktree as the kit under test.

routing: score = development cases passing in `evals/skill_routing.py`; no case that passed in the
best commit may fail; held-out cases are recorded for every iteration but never decide.
model:   score = author runs passing behavior, plan and norm in `evals/phase_eval.py` (Claude author
only), then plan passes, then fewer uncached input tokens; the observed required-read rate may not
drop. Model runs are noisy: keep decisions need a strictly higher pass count.

Not shipped: like tools/skill-noop, this tree is outside `scripts/` and the kit digest roots.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PROGRAM = HERE / "program.md"
TSV_HEADER = "iteration\tcommit\tstatus\tscore\tdev\theldout\textra\tcost_usd\tdescription\n"
HIDDEN_FROM_PROPOSER = ("evals", "tests", "maintainer", "tools")
ROUTING_LINE = re.compile(r"^\s*(taskTerms|task_terms):")
CONTAMINATION_GUARD = "tests/test_eval_case_isolation.py"
ROUTING_GUARDS = (
    "tests/test_profile_routing.py::test_tanstack_form_capability_routes_only_for_react_web",
    "tests/test_profile_routing.py::test_react_capabilities_route_by_intent_not_all_tsx",
    "tests/test_profile_routing.py::test_localized_aliases_keep_platform_boundaries_in_full_resolver",
    "tests/test_profile_routing.py::test_unrelated_profiles_never_route_android_vocabulary",
    "tests/test_profile_routing.py::test_multi_profile_merge_keeps_both_vocabularies",
)


def run(argv: list[str], cwd: Path, timeout: int = 3600, env: dict | None = None,
        stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, input=stdin,
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "AGENT_FLOW_NO_UPDATE_CHECK": "1",
                               **(env or {})})


def git(cwd: Path, *args: str) -> str:
    process = run(["git", *args], cwd)
    if process.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {process.stderr.strip()}")
    return process.stdout.strip()


# --------------------------------------------------------------------------- worktree

def prepare_worktree(root: Path, tag: str) -> Path:
    tree = root / tag / "kit"
    if tree.exists():
        return tree
    tree.parent.mkdir(parents=True, exist_ok=True)
    git(REPO, "worktree", "add", "-q", "-b", f"autoresearch/{tag}", str(tree), "HEAD")
    # The proposer must not read the cases it is scored on; sparse checkout keeps them in the
    # index (commits leave them untouched) but out of the working copy.
    git(tree, "sparse-checkout", "set", "--no-cone", "/*", *(f"!/{name}/" for name in HIDDEN_FROM_PROPOSER))
    return tree


def changed_files(tree: Path) -> list[str]:
    # Untrimmed and NUL-separated: porcelain lines start with a status column that may be a space.
    output = run(["git", "status", "--porcelain", "-z", "--untracked-files=all"], tree).stdout
    return [entry[3:] for entry in output.split("\0") if entry]


def surface_violations(tree: Path, track: str, skills: list[str]) -> list[str]:
    problems = []
    for path in changed_files(tree):
        if track == "routing":
            if not (fnmatch.fnmatch(path, "skills/*/SKILL.md") or fnmatch.fnmatch(path, "src/agent_flow/profiles/*.yaml")):
                problems.append(f"{path}: outside the routing surface")
                continue
            diff = git(tree, "diff", "-U0", "--", path)
            for line in diff.splitlines():
                if line.startswith(("+++", "---", "@@")) or not line.startswith(("+", "-")):
                    continue
                if not ROUTING_LINE.match(line[1:]):
                    problems.append(f"{path}: changed a non-vocabulary line: {line[:80]}")
        else:
            allowed = [f"skills/{name}/SKILL.md" for name in skills] + [f"skills/{name}/references/*.md" for name in skills]
            if not any(fnmatch.fnmatch(path, pattern) for pattern in allowed):
                problems.append(f"{path}: outside the model surface")
                continue
            if path.endswith("SKILL.md"):
                before = git(tree, "show", f"HEAD:{path}").split("---", 2)[1]
                after = (tree / path).read_text(encoding="utf-8").split("---", 2)[1]
                if before != after:
                    problems.append(f"{path}: frontmatter is frozen in the model track")
    return problems


def surface_bytes(tree: Path, track: str, skills: list[str]) -> int:
    if track == "routing":
        paths = list(tree.glob("skills/*/SKILL.md")) + list(tree.glob("src/agent_flow/profiles/*.yaml"))
    else:
        paths = [path for name in skills for path in (tree / "skills" / name).rglob("*.md")]
    return sum(path.stat().st_size for path in paths)


# --------------------------------------------------------------------------- evaluation

def guards(tree: Path) -> list[str]:
    """Contamination and routing ledger guards, run inside the candidate checkout.

    The proposer is not running now, so the hidden trees may be checked out for the run and
    hidden again before its next turn.
    """
    git(tree, "sparse-checkout", "disable")
    try:
        process = run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=",
                       CONTAMINATION_GUARD, *ROUTING_GUARDS], tree)
    finally:
        git(tree, "sparse-checkout", "set", "--no-cone", "/*", *(f"!/{name}/" for name in HIDDEN_FROM_PROPOSER))
    if process.returncode == 0:
        return []
    return [process.stdout.strip().splitlines()[-1] if process.stdout.strip() else process.stderr[-200:]]


def score_routing(tree: Path, work: Path) -> dict:
    output = work / "routing.json"
    process = run([sys.executable, str(REPO / "evals" / "skill_routing.py"), "--kit", str(tree), "--output", str(output)],
                  REPO, timeout=1800)
    if process.returncode != 0:
        return {"error": process.stderr[-600:]}
    payload = json.loads(output.read_text(encoding="utf-8"))
    corpus = json.loads((REPO / "evals" / "routing-cases.json").read_text(encoding="utf-8"))
    split = {case["id"]: case["split"] for case in corpus["cases"]}
    texts = {case["id"]: case["task"] for case in corpus["cases"]}
    cases = {cid: result for entry in payload["profiles"] for cid, result in entry.get("cases", {}).items()}
    dev = {cid: bool(r.get("passed")) for cid, r in cases.items() if split[cid] == "dev"}
    heldout = {cid: bool(r.get("passed")) for cid, r in cases.items() if split[cid] == "heldout"}
    failing = [f"- {texts[cid]!r}: missing {r.get('missing')}, unexpected {r.get('unexpected')}"
               for cid, r in cases.items() if split[cid] == "dev" and not r.get("passed")]
    return {"score": (sum(dev.values()),), "dev": sum(dev.values()), "dev_total": len(dev),
            "heldout": sum(heldout.values()), "heldout_total": len(heldout), "passing": dev,
            "feedback": "Failing development cases:\n" + ("\n".join(failing) or "- none")}


def score_model(tree: Path, work: Path, args: argparse.Namespace) -> dict:
    output = work / f"eval-{int(time.time())}"
    process = run([sys.executable, str(REPO / "evals" / "phase_eval.py"), "--kit", f"after={tree}",
                   "--stacks", args.stacks, "--modes", args.modes, "--scenario", "author",
                   "--trials", str(args.trials), "--author-provider", "claude", "--review-providers", "claude",
                   *(["--claude-model", args.model] if args.model else []),
                   "--concurrency", str(args.concurrency), "--output", str(output)], REPO, timeout=6 * 3600)
    results = output / "results.jsonl"
    if not results.is_file():
        return {"error": (process.stderr or process.stdout)[-600:]}
    rows = [json.loads(line) for line in results.read_text(encoding="utf-8").splitlines() if line.strip()]
    valid = [row for row in rows if not row.get("error") and isinstance(row.get("oracle"), dict)]
    passed = sum(all(row["oracle"].get(axis) for axis in ("behavior", "plan", "norm")) for row in valid)
    plan = sum(bool(row["oracle"].get("plan")) for row in valid)
    tokens = sum((row.get("usage") or {}).get("uncached_input") or 0 for row in valid)
    rates = [row["required_read_rate"] for row in valid if row.get("required_read_rate") is not None]
    checks = [f"- {check['id']}: {'ok' if check['ok'] else 'FAILED'}"
              for row in valid for check in row["oracle"].get("checks", []) if not check["ok"]]
    return {"score": (passed, plan, -tokens), "dev": passed, "dev_total": len(rows), "heldout": "-",
            "heldout_total": "-", "read_rate": min(rates) if rates else None,
            "cost": sum(row.get("cost_usd") or 0 for row in rows),
            "feedback": f"{passed}/{len(rows)} runs passed every axis; failed checks:\n" + ("\n".join(checks) or "- none")}


def evaluate(tree: Path, work: Path, args: argparse.Namespace) -> dict:
    return score_routing(tree, work) if args.track == "routing" else score_model(tree, work, args)


def better(candidate: dict, best: dict, track: str, candidate_bytes: int, best_bytes: int) -> tuple[bool, str]:
    if "error" in candidate:
        return False, "evaluation error"
    if track == "routing":
        lost = [cid for cid, ok in best["passing"].items() if ok and not candidate["passing"].get(cid)]
        if lost:
            return False, f"regressed {lost}"
        if candidate["score"] > best["score"]:
            return True, "improved"
        if candidate["score"] == best["score"] and candidate_bytes < best_bytes:
            return True, "same score, shorter"
        return False, "no improvement"
    if best.get("read_rate") is not None and (candidate.get("read_rate") or 0) < best["read_rate"]:
        return False, "required-read rate dropped"
    passed, plan, _ = candidate["score"]
    best_passed, best_plan, _ = best["score"]
    # Model runs are noisy; a token change alone is not evidence. Only more passing runs keep an edit.
    # No "same passes, shorter" tie-break here: the score covers a few cases, so it cannot see what a
    # deleted rule protects elsewhere (the first fixloop run kept four deletions of unrelated guidance).
    if passed > best_passed:
        return True, "improved"
    return False, "no improvement"


# --------------------------------------------------------------------------- proposer

def propose(tree: Path, args: argparse.Namespace, state: str) -> tuple[str, float]:
    prompt = PROGRAM.read_text(encoding="utf-8") + "\n\n## Current state\n\n" + state
    # Absolute paths in permission rules take a leading `//`.
    settings = {"permissions": {"deny": [f"Read(/{REPO}/**)", f"Edit(/{REPO}/**)", f"Grep(/{REPO}/**)"]}}
    process = run(["claude", "-p", "--output-format", "json", "--permission-mode", "acceptEdits",
                   "--settings", json.dumps(settings), "--max-budget-usd", str(args.proposer_budget_usd),
                   *(["--model", args.proposer_model] if args.proposer_model else []),
                   "--allowedTools", "Read,Grep,Glob,Edit", "--disallowedTools", "Bash,Write,WebFetch,WebSearch,Agent"],
                  tree, timeout=1800, stdin=prompt)
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError:
        return f"proposer failed: {(process.stderr or process.stdout)[-200:]}", 0.0
    text = payload.get("result") or ""
    match = re.search(r"DESCRIPTION:\s*(.+)", text)
    return (match.group(1).strip() if match else text.strip().splitlines()[-1:][0] if text.strip() else "no description"), \
        float(payload.get("total_cost_usd") or 0)


def state_text(args: argparse.Namespace, best: dict, results: Path) -> str:
    history = results.read_text(encoding="utf-8").splitlines()[1:][-args.history:]
    lines = [f"Track: {args.track}", f"Best score: {best['dev']}/{best['dev_total']} (higher is better)"]
    if args.track == "model":
        lines.append(f"Editable skills: {', '.join(args.skills)}")
    lines += ["", best["feedback"], "", "Recent attempts (iteration, status, description):"]
    lines += [f"- {row.split(chr(9))[0]} {row.split(chr(9))[2]}: {row.split(chr(9))[-1]}" for row in history] or ["- none"]
    return "\n".join(lines)


def record(results: Path, iteration: str, commit: str, status: str, outcome: dict, extra: str, cost: float,
           description: str) -> None:
    score = outcome.get("score", "")
    with results.open("a", encoding="utf-8") as handle:
        handle.write("\t".join(map(str, [iteration, commit[:12], status, score, outcome.get("dev", ""),
                                          outcome.get("heldout", ""), extra, f"{cost:.4f}",
                                          description.replace("\t", " ").replace("\n", " ")])) + "\n")


# --------------------------------------------------------------------------- loop

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--track", choices=("routing", "model"), required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--root", type=Path, default=Path.home() / ".cache" / "agent-flow-autoresearch")
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--patience", type=int, default=6, help="stop after this many attempts without a keep")
    parser.add_argument("--budget-usd", type=float, required=True, help="stop once proposer + model cost reaches this")
    parser.add_argument("--proposer-budget-usd", type=float, default=1.0)
    parser.add_argument("--proposer-model")
    parser.add_argument("--history", type=int, default=12)
    parser.add_argument("--skills", type=lambda value: value.split(","), default=[])
    parser.add_argument("--stacks", default="commerce")
    parser.add_argument("--modes", default="clean")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--model", help="Claude model for evaluated author runs")
    args = parser.parse_args(argv)
    if args.track == "model" and not args.skills:
        parser.error("--skills is required for the model track")

    tree = prepare_worktree(args.root, args.tag)
    work = args.root / args.tag
    results = work / "results.tsv"
    if not results.exists():
        results.write_text(TSV_HEADER, encoding="utf-8")
    spent = 0.0
    best = evaluate(tree, work, args)
    if "error" in best:
        print(f"baseline evaluation failed: {best['error']}", file=sys.stderr)
        return 1
    spent += best.get("cost", 0.0)
    best_commit = git(tree, "rev-parse", "HEAD")
    best_bytes = surface_bytes(tree, args.track, args.skills)
    record(results, "baseline", best_commit, "keep", best, f"bytes={best_bytes}", best.get("cost", 0.0), "baseline")
    print(f"baseline {best['dev']}/{best['dev_total']} heldout {best['heldout']}/{best['heldout_total']}", flush=True)

    idle = 0
    for iteration in range(1, args.iterations + 1):
        if spent >= args.budget_usd or idle >= args.patience:
            break
        description, cost = propose(tree, args, state_text(args, best, results))
        spent += cost
        problems = surface_violations(tree, args.track, args.skills)
        if not changed_files(tree):
            problems = problems or ["no edit"]
        if problems:
            git(tree, "reset", "-q", "--hard", best_commit)
            git(tree, "clean", "-qfd")
            record(results, str(iteration), best_commit, "invalid", {}, "; ".join(problems)[:200], cost, description)
            idle += 1
            continue
        git(tree, "add", "-A")
        git(tree, "-c", "user.name=autoresearch", "-c", "user.email=autoresearch@localhost",
            "commit", "-qm", f"autoresearch {args.tag} #{iteration}: {description}")
        commit = git(tree, "rev-parse", "HEAD")
        failed_guards = guards(tree)
        outcome = {} if failed_guards else evaluate(tree, work, args)
        spent += outcome.get("cost", 0.0)
        candidate_bytes = surface_bytes(tree, args.track, args.skills)
        keep, reason = (False, f"guard: {failed_guards[0]}") if failed_guards else \
            better(outcome, best, args.track, candidate_bytes, best_bytes)
        record(results, str(iteration), commit, "keep" if keep else "discard", outcome,
               f"{reason}; bytes={candidate_bytes}", cost + outcome.get("cost", 0.0), description)
        print(f"#{iteration} {'keep' if keep else 'discard'} ({reason}) {outcome.get('dev', '-')}/"
              f"{outcome.get('dev_total', '-')} heldout {outcome.get('heldout', '-')} spent ${spent:.2f}: {description}",
              flush=True)
        if keep:
            best, best_commit, best_bytes, idle = outcome, commit, candidate_bytes, 0
        else:
            git(tree, "reset", "-q", "--hard", best_commit)
            idle += 1
    print(f"best {best['dev']}/{best['dev_total']} heldout {best['heldout']}/{best['heldout_total']} at {best_commit[:12]} "
          f"on branch autoresearch/{args.tag}; spent ${spent:.2f}; log {results}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
