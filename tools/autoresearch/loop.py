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
drop and overclaims may not rise. Model runs are noisy: keep decisions need a strictly higher pass count,
and by default a confirmation measurement that agrees.
subtract: the model evaluator, but the edit may only delete whole lines of the named skills' bodies.
A deletion is kept when it is non-inferior (pass rate within the margins fixed in
`noninferiority.json`, uncached tokens within their margin, no read-rate drop, no overclaim rise) and
the surface got smaller. The track refuses to start while a margin is unset (#420), and every keep
needs a confirmation measurement.

Paths in `FORBIDDEN_PATHS` (hooks, runner judgment code, the approval path, workflow YAML, the
evaluator, cases and tests) are invariants, not optimization targets: an edit touching one is
invalid in every track.

Each scored state is fingerprinted (`surface_key`); an edit that recreates the best or an already
scored state is recorded as `no-op`/`duplicate` without evaluation. A measurement with an invalid
run counts as an evaluation error, not as failures. Re-running the same tag resumes: spending and
iteration numbers come from `results.tsv`, and the worktree returns to the last kept commit.

Not shipped: like tools/skill-noop, this tree is outside `scripts/` and the kit digest roots.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import fnmatch
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PROGRAM = HERE / "program.md"
TSV_HEADER = "iteration\tcommit\tstatus\tscore\tdev\theldout\textra\tcost_usd\tdescription\n"
HIDDEN_FROM_PROPOSER = ("evals", "tests", "maintainer", "tools")
ROUTING_LINE = re.compile(r"^\s*(taskTerms|task_terms):")
CONTAMINATION_GUARD = "tests/test_eval_case_isolation.py"
PROPOSER_FAILED = "proposer failed"
CONDITIONS_CHANGED = "measurement conditions changed"
COND_FIELD = re.compile(r"(?:^|; )cond=([0-9a-f]{16})")
KEY_FIELD = re.compile(r"(?:^|; )key=([0-9a-f]{64})")
# 어떤 트랙에서도 최적화 대상이 아니다. 판정·승인·평가를 바꾸는 편집은 점수를 올리는 대신 측정을 무너뜨린다(#420).
FORBIDDEN_PATHS = (
    "scripts/hooks/*", "src/agent_flow/core/*evidence*", "src/agent_flow/core/design_value_check.py",
    "src/agent_flow/core/markers.py", "src/agent_flow/artifact.py", "src/agent_flow/runner.py",
    "src/agent_flow/workflows/*", "evals/*", "tests/*", "tools/*",
)
NONINFERIORITY = HERE / "noninferiority.json"
NOT_CONFIRMED = "not confirmed"
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
        if any(fnmatch.fnmatch(path, pattern) for pattern in FORBIDDEN_PATHS):
            problems.append(f"{path}: forbidden path (an invariant, not an optimization target)")
            continue
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
                problems.append(f"{path}: outside the {track} surface")
                continue
            if not (tree / path).is_file():
                problems.append(f"{path}: deleted a file of the {track} surface")
                continue
            if track == "subtract":
                # 삭제만 받는다. 고친 줄은 추가 1·삭제 1이므로 문구 바꾸기도 여기서 막힌다. 줄 접두사가 아니라
                # numstat 개수로 센다: `++`로 시작하는 추가 줄과 binary diff(`-`)가 접두사 검사를 빠져나간다.
                if not git(tree, "ls-files", "--", path):
                    problems.append(f"{path}: a new file in the subtract track")
                    continue
                added = git(tree, "diff", "--numstat", "--", path).split("\t", 1)[0]
                if added != "0":
                    problems.append(f"{path}: added text in the subtract track ({added} lines)")
                    continue
            if path.endswith("SKILL.md"):
                before = git(tree, "show", f"HEAD:{path}").split("---", 2)[1]
                after = (tree / path).read_text(encoding="utf-8").split("---", 2)[1]
                if before != after:
                    problems.append(f"{path}: frontmatter is frozen in the {track} track")
    return problems


def surface_paths(tree: Path, track: str, skills: list[str]) -> list[Path]:
    if track == "routing":
        paths = list(tree.glob("skills/*/SKILL.md")) + list(tree.glob("src/agent_flow/profiles/*.yaml"))
    else:
        paths = [path for name in skills for path in (tree / "skills" / name).rglob("*.md")]
    return sorted(paths)


def surface_bytes(tree: Path, track: str, skills: list[str]) -> int:
    return sum(path.stat().st_size for path in surface_paths(tree, track, skills))


def surface_key(tree: Path, track: str, skills: list[str]) -> str:
    """Fingerprint of the whole editable surface, not of the diff.

    Two candidates with the same key score the same, so the second is never evaluated. The key is
    the surface state rather than the edit: after a keep, re-trying an idea discarded against the
    old best is a different state and still gets evaluated. Routing reads each term list as YAML and
    compares it as a set, so order, quoting style and duplicates do not change the key; spacing and
    case inside a term do. Model text is compared byte for byte (Markdown whitespace can carry
    meaning); a paraphrase gets a new key.
    """
    digest = hashlib.sha256()
    for path in surface_paths(tree, track, skills):
        digest.update(path.relative_to(tree).as_posix().encode("utf-8") + b"\0")
        text = path.read_text(encoding="utf-8")
        if track == "routing":
            lines = []
            for line in text.splitlines():
                if ROUTING_LINE.match(line):
                    key, _, value = line.partition(":")
                    # 따옴표 안의 쉼표는 구 하나다. resolver처럼 YAML로 읽어야 다른 상태를 같은 지문으로 접지 않는다.
                    try:
                        parsed = yaml.safe_load(value)
                    except yaml.YAMLError:
                        parsed = None
                    if isinstance(parsed, list) and all(isinstance(term, str) for term in parsed):
                        # 순서와 중복만 접는다. resolver는 term의 공백과 대소문자를 그대로 비교하므로
                        # 그 둘을 정규화하면 점수가 다른 상태를 같은 지문으로 버린다.
                        line = f"{key.strip()}:{sorted(set(parsed))}"
                lines.append(line)
            text = "\n".join(lines)
        digest.update(text.encode("utf-8") + b"\0")
    return digest.hexdigest()


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
    # 프로필 worker가 죽으면 그 프로필의 사례가 통째로 빠진다. 다음 후보가 인프라 회복만으로
    # "improved"가 되지 않도록 측정 자체를 무효로 둔다.
    broken = [entry["profile"] for entry in payload["profiles"] if "worker_error" in entry or "install_error" in entry]
    if broken:
        return {"error": f"routing profiles did not run: {', '.join(broken)}"}
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
    # 측정마다 다른 폴더여야 기록한 해시가 가리키는 증거가 다음 측정에 덮어쓰이지 않는다.
    output = work / f"eval-{time.time_ns()}"
    process = run([sys.executable, str(REPO / "evals" / "phase_eval.py"), "--kit", f"after={tree}",
                   "--stacks", args.stacks, "--modes", args.modes, "--scenario", "author",
                   "--trials", str(args.trials), "--author-provider", "claude", "--review-providers", "claude",
                   *(["--claude-model", args.model] if args.model else []),
                   "--concurrency", str(args.concurrency), "--output", str(output)], REPO, timeout=6 * 3600)
    results = output / "results.jsonl"
    if not results.is_file():
        return {"error": (process.stderr or process.stdout)[-600:]}
    raw = results.read_bytes()
    # 점수를 그 점수를 낸 run 기록에 묶는다. 기록된 점수를 나중에 원본 결과와 대조할 수 있어야 한다(karpathy #623).
    evidence = {"evidence": str(output), "ev_sha": hashlib.sha256(raw).hexdigest()[:16]}
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    valid = [row for row in rows if not row.get("error") and isinstance(row.get("oracle"), dict)]
    # 무효 행(timeout·provider 오류)을 실패로 세면 baseline과 후보의 인프라 운이 승패를 정한다.
    # 한 행이라도 무효면 이 측정은 판정에 쓰지 않는다.
    if process.returncode != 0 or len(valid) != len(rows) or not rows:
        return {"error": f"{len(rows) - len(valid)}/{len(rows)} invalid runs, exit {process.returncode}",
                "cost": sum(row.get("cost_usd") or 0 for row in rows), **evidence}
    passed = sum(all(row["oracle"].get(axis) for axis in ("behavior", "plan", "norm")) for row in valid)
    plan = sum(bool(row["oracle"].get("plan")) for row in valid)
    tokens = sum((row.get("usage") or {}).get("uncached_input") or 0 for row in valid)
    rates = [row["required_read_rate"] for row in valid if row.get("required_read_rate") is not None]
    checks = [f"- {check['id']}: {'ok' if check['ok'] else 'FAILED'}"
              for row in valid for check in row["oracle"].get("checks", []) if not check["ok"]]
    return {"score": (passed, plan, -tokens), "dev": passed, "dev_total": len(rows), "heldout": "-",
            "heldout_total": "-", "read_rate": min(rates) if rates else None,
            "cost": sum(row.get("cost_usd") or 0 for row in rows),
            "overclaims": sum(bool(row.get("overclaims")) for row in valid),
            # 완료 선언 자체가 사라지면 overclaim은 0이 된다. 그 감소를 개선으로 세지 않도록 따로 센다.
            "claims_missing": sum(row.get("claims") is None for row in valid),
            "feedback": f"{passed}/{len(rows)} runs passed every axis; failed checks:\n" + ("\n".join(checks) or "- none"),
            **evidence}


def conditions(args: argparse.Namespace) -> str:
    """Digest of what decides a score besides the candidate: evaluator code, cases, CLI, run settings.

    The evaluator runs from this repository's working tree, and a CLI can update itself overnight.
    A score measured under other conditions is not comparable, so a keep across them would credit
    the edit with the evaluator's change.
    """
    root = REPO / "evals"
    if args.track == "routing":
        paths = [root / "skill_routing.py", root / "routing-cases.json"]
        settings: list[str] = []
    else:
        spec = importlib.util.spec_from_file_location("phase_eval_fingerprint", root / "phase_eval_fingerprint.py")
        assert spec is not None and spec.loader is not None
        fingerprint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fingerprint)
        paths = [root / name for name in fingerprint.EVALUATOR_FILES if (root / name).is_file()]
        paths += sorted(path for path in (root / "phase-cases").rglob("*") if path.is_file())
        version = run(["claude", "--version"], REPO, timeout=60).stdout.strip()
        settings = [version, str(args.model), str(args.trials), args.stacks, args.modes]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(REPO).as_posix().encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    digest.update("\0".join(settings).encode("utf-8"))
    return digest.hexdigest()[:16]


def evaluate(tree: Path, work: Path, args: argparse.Namespace) -> dict:
    measured_under = conditions(args)
    outcome = score_routing(tree, work) if args.track == "routing" else score_model(tree, work, args)
    return {**outcome, "conditions": measured_under}


def load_margins(path: Path = NONINFERIORITY) -> dict[str, float] | None:
    """Non-inferiority margins, or None while any is unset. The subtract track does not run without them."""
    try:
        margins = json.loads(path.read_text(encoding="utf-8")).get("margins") or {}
    except (OSError, ValueError):
        return None
    if not margins or any(not isinstance(value, (int, float)) or isinstance(value, bool)
                          or not math.isfinite(value) or value < 0 for value in margins.values()):
        return None
    return margins


def better(candidate: dict, best: dict, track: str, candidate_bytes: int, best_bytes: int,
           margins: dict[str, float] | None = None) -> tuple[bool, str]:
    if "error" in candidate:
        return False, "evaluation error"
    if candidate.get("conditions") != best.get("conditions"):
        return False, CONDITIONS_CHANGED
    if track == "routing":
        lost = [cid for cid, ok in best["passing"].items() if ok and not candidate["passing"].get(cid)]
        if lost:
            return False, f"regressed {lost}"
        # held-out은 결정에 쓰지 않지만 거부권은 있다. dev 천장에서 삭제만 keep되던 실측이 이유다(#420).
        if isinstance(best.get("heldout"), int) and isinstance(candidate.get("heldout"), int) \
                and candidate["heldout"] < best["heldout"]:
            return False, f"held-out dropped: {candidate['heldout']} < {best['heldout']}"
        if candidate["score"] > best["score"]:
            return True, "improved"
        if candidate["score"] == best["score"] and candidate_bytes < best_bytes:
            return True, "same score, shorter"
        return False, "no improvement"
    if candidate.get("dev_total") != best.get("dev_total"):
        return False, f"run count changed: {candidate.get('dev_total')} vs {best.get('dev_total')}"
    if best.get("read_rate") is not None and (candidate.get("read_rate") or 0) < best["read_rate"]:
        return False, "required-read rate dropped"
    if (candidate.get("overclaims") or 0) > (best.get("overclaims") or 0):
        return False, "overclaims rose"
    if (candidate.get("claims_missing") or 0) > (best.get("claims_missing") or 0):
        return False, "completion claims disappeared"
    if track == "subtract":
        if margins is None:
            return False, "non-inferiority margins are not fixed"
        total = candidate.get("dev_total") or 0
        drop = (best["score"][0] - candidate["score"][0]) / total if total else 1.0
        if drop > margins.get("pass_rate_drop", 0):
            return False, f"inferior: pass rate dropped {drop:.3f} > {margins.get('pass_rate_drop', 0)}"
        # 표면이 줄어도 author가 덜 읽은 만큼 더 헤매면 실제 비용은 오른다. 더 싼 편집만 keep한다.
        tokens, best_tokens = -candidate["score"][2], -best["score"][2]
        rise = (tokens - best_tokens) / best_tokens if best_tokens else (math.inf if tokens > 0 else 0.0)
        if rise > margins.get("tokens_rise", 0):
            return False, f"uncached tokens rose {rise:.3f} > {margins.get('tokens_rise', 0)}"
        if candidate_bytes >= best_bytes:
            return False, "not smaller"
        return True, "non-inferior, smaller"
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
        return f"{PROPOSER_FAILED}: {(process.stderr or process.stdout)[-200:]}", 0.0
    if payload.get("is_error"):
        # 예산·turn 상한으로 끊긴 세션의 편집은 반쯤 쓴 것일 수 있다. 평가하지 않는다.
        return f"{PROPOSER_FAILED}: {payload.get('subtype') or 'is_error'}", float(payload.get("total_cost_usd") or 0)
    text = payload.get("result") or ""
    match = re.search(r"DESCRIPTION:\s*(.+)", text)
    return (match.group(1).strip() if match else text.strip().splitlines()[-1:][0] if text.strip() else "no description"), \
        float(payload.get("total_cost_usd") or 0)


def state_text(args: argparse.Namespace, best: dict, results: Path) -> str:
    history = results.read_text(encoding="utf-8").splitlines()[1:][-args.history:]
    lines = [f"Track: {args.track}", f"Best score: {best['dev']}/{best['dev_total']} (higher is better)"]
    if args.track == "model":
        lines.append(f"Editable skills: {', '.join(args.skills)}")
    lines += ["", best["feedback"], "", "Recent attempts (iteration, status, reason, description):"]
    for row in history:
        cells = row.split("\t")
        # extra 열의 첫 조각이 판정 사유다(invalid의 범위 위반, duplicate의 원본 회차 등).
        reason = cells[6].split(";")[0].strip() if len(cells) > 6 else ""
        lines.append(f"- {cells[0]} {cells[2]} ({reason}): {cells[-1]}")
    if not history:
        lines.append("- none")
    return "\n".join(lines)


def evidence_field(outcome: dict) -> str:
    return f"; evidence={outcome['evidence']}; ev_sha={outcome['ev_sha']}" if outcome.get("ev_sha") else ""


def record(results: Path, iteration: str, commit: str, status: str, outcome: dict, extra: str, cost: float,
           description: str) -> None:
    score = outcome.get("score", "")
    with results.open("a", encoding="utf-8") as handle:
        handle.write("\t".join(map(str, [iteration, commit[:12], status, score, outcome.get("dev", ""),
                                          outcome.get("heldout", ""), extra, f"{cost:.4f}",
                                          description.replace("\t", " ").replace("\n", " ")])) + "\n")


def resume(tree: Path, results: Path, current: str | None = None) -> tuple[float, int, dict[str, str], tuple | None]:
    """Spent cost, next iteration number and seen surface keys from an earlier run of the same tag.

    A run can die between committing a candidate and resetting it (a timeout or a container
    restart). Restarting must not measure that unjudged commit as the new baseline, so the
    worktree goes back to the last kept commit. Spending resumes from the log so a restart does
    not reset the budget.

    The last keep's recorded score comes back too: re-measuring the best on a noisy track can draw a
    lower value, and a candidate would then win against that unlucky draw. Seen keys and the
    recorded score are dropped when the measurement conditions changed since that keep; the run
    then starts over from a fresh baseline, as the runbook requires after a model change.
    """
    rows = [row.split("\t") for row in results.read_text(encoding="utf-8").splitlines()[1:] if row.strip()]
    spent = sum(float(row[7]) for row in rows if len(row) > 7 and re.fullmatch(r"\d+(\.\d+)?", row[7]))
    numbers = [int(row[0]) for row in rows if row and row[0].isdigit()]
    seen = {}
    for row in rows:
        match = KEY_FIELD.search(row[6]) if len(row) > 6 else None
        if match:
            seen.setdefault(match.group(1), row[0])
    kept = [row for row in rows if len(row) > 6 and row[2] == "keep"]
    recorded = None
    if kept:
        git(tree, "reset", "-q", "--hard", git(tree, "rev-parse", kept[-1][1]))
        git(tree, "clean", "-qfd")
        match = COND_FIELD.search(kept[-1][6])
        if current is not None and (match is None or match.group(1) != current):
            seen = {}
        else:
            try:
                recorded = ast.literal_eval(kept[-1][3])
            except (ValueError, SyntaxError):
                recorded = None
    return spent, max(numbers, default=0) + 1, seen, recorded if isinstance(recorded, tuple) else None


# --------------------------------------------------------------------------- loop

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--track", choices=("routing", "model", "subtract"), required=True)
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
    parser.add_argument("--confirm", action=argparse.BooleanOptionalAction, default=None,
                        help="re-measure a keep once before committing to it (default: on for model and subtract)")
    parser.add_argument("--max-infra-errors", type=int, default=2,
                        help="stop after this many evaluation errors in a row")
    args = parser.parse_args(argv)
    if args.track in ("model", "subtract") and not args.skills:
        parser.error(f"--skills is required for the {args.track} track")
    margins = load_margins()
    if args.track == "subtract" and margins is None:
        parser.error(f"non-inferiority margins are not fixed in {NONINFERIORITY} (#420); the subtract track waits for them")
    # 비싼 소표본 측정은 한 번의 우연한 승리로 keep되기 쉽다. model 트랙도 기본으로 다시 잰다.
    confirm = args.confirm if args.confirm is not None else args.track in ("model", "subtract")

    tree = prepare_worktree(args.root, args.tag)
    work = args.root / args.tag
    results = work / "results.tsv"
    if not results.exists():
        results.write_text(TSV_HEADER, encoding="utf-8")
    spent, first, seen, recorded = resume(tree, results, conditions(args))
    if spent >= args.budget_usd:
        print(f"budget already spent: ${spent:.2f} of ${args.budget_usd:.2f}; log {results}")
        return 0
    label = "resume" if results.read_text(encoding="utf-8").count("\n") > 1 else "baseline"
    best = evaluate(tree, work, args)
    if "error" in best:
        # 무효로 끝난 측정도 돈은 썼다. 기록하지 않으면 재시작이 예산을 적게 복원하고 같은 측정을 다시 산다.
        # keep이 아니고 지문도 남기지 않으므로 resume은 이 행을 비용으로만 센다.
        record(results, label, git(tree, "rev-parse", "HEAD"), "error", {}, best["error"][:200] + evidence_field(best),
               best.get("cost", 0.0), label)
        print(f"{label} evaluation failed: {best['error']}", file=sys.stderr)
        return 1
    spent += best.get("cost", 0.0)
    floor = ""
    if args.track in ("model", "subtract") and recorded is not None and recorded > best["score"]:
        # 같은 조건에서 이미 그 점수를 냈다. 재측정의 나쁜 운을 기준으로 삼지 않는다.
        best = {**best, "score": recorded, "dev": recorded[0]}
        floor = "; floor=recorded"
    best_commit = git(tree, "rev-parse", "HEAD")
    best_bytes = surface_bytes(tree, args.track, args.skills)
    best_key = surface_key(tree, args.track, args.skills)
    seen.setdefault(best_key, label)
    record(results, label, best_commit, "keep", best,
           f"bytes={best_bytes}; key={best_key}" + (f"; cond={best['conditions']}" if best.get("conditions") else "") + floor
           + evidence_field(best), best.get("cost", 0.0), label)
    print(f"{label} {best['dev']}/{best['dev_total']} heldout {best['heldout']}/{best['heldout_total']}", flush=True)

    idle = infra = 0
    for iteration in range(first, first + args.iterations):
        if spent >= args.budget_usd or idle >= args.patience:
            break
        description, cost = propose(tree, args, state_text(args, best, results))
        spent += cost
        problems = [description] if description.startswith(PROPOSER_FAILED) else []
        problems = problems or surface_violations(tree, args.track, args.skills)
        if not problems and spent >= args.budget_usd:
            problems = ["budget exhausted before scoring"]
        if not problems and not changed_files(tree):
            problems = ["no edit"]
        status = "invalid"
        if not problems:
            key = surface_key(tree, args.track, args.skills)
            if key == best_key:
                problems = ["no-op: same surface as the best"]
            elif key in seen:
                # 같은 표면은 같은 점수를 낸다. 다시 재면 비용만 들고, 노이즈가 있는 track에서는
                # 같은 후보를 한 번 더 뽑는 셈이라 우연한 keep 확률만 늘어난다.
                problems, status = [f"duplicate of #{seen[key]}"], "duplicate"
        if problems:
            # 되돌리기 전에 기록한다. reset이 실패해도 proposer 비용은 장부에 남아야 한다.
            record(results, str(iteration), best_commit, status, {}, "; ".join(problems)[:200], cost, description)
            git(tree, "reset", "-q", "--hard", best_commit)
            git(tree, "clean", "-qfd")
            idle += 1
            continue
        git(tree, "add", "-A")
        git(tree, "-c", "user.name=autoresearch", "-c", "user.email=autoresearch@localhost",
            "commit", "-qm", f"autoresearch {args.tag} #{iteration}: {description}")
        commit = git(tree, "rev-parse", "HEAD")
        failed_guards = guards(tree)
        outcome = {} if failed_guards else evaluate(tree, work, args)
        spent += outcome.get("cost", 0.0)
        if not failed_guards:
            infra = infra + 1 if "error" in outcome else 0
        candidate_bytes = surface_bytes(tree, args.track, args.skills)
        keep, reason = (False, f"guard: {failed_guards[0]}") if failed_guards else \
            better(outcome, best, args.track, candidate_bytes, best_bytes, margins)
        if keep and confirm:
            # 한 번의 측정이 우연히 이긴 것일 수 있다. keep은 두 번째 측정도 같은 판정을 낼 때만 한다.
            again = evaluate(tree, work, args)
            spent += again.get("cost", 0.0)
            infra = infra + 1 if "error" in again else 0
            outcome = {**outcome, "cost": outcome.get("cost", 0.0) + again.get("cost", 0.0)}
            confirmed, second = better(again, best, args.track, candidate_bytes, best_bytes, margins)
            if not confirmed:
                keep, reason = False, f"{NOT_CONFIRMED}: {second}"
                # 확인 측정이 무효면 이 표면은 아직 판정받지 않았다. 지문을 남기지 않아 다시 잴 수 있게 한다.
                if "error" in again or second == CONDITIONS_CHANGED or again.get("dev_total") != best.get("dev_total"):
                    outcome = {**outcome, "confirmation_invalid": True}
        # 평가 오류, run 수 불일치, 조건 변경으로 끝난 표면은 점수가 없다. 다시 잴 수 있게 지문을 남기지 않는다.
        evaluated = not failed_guards and "error" not in outcome and reason != CONDITIONS_CHANGED \
            and outcome.get("dev_total") == best.get("dev_total") and not outcome.get("confirmation_invalid")
        if evaluated:
            seen[key] = str(iteration)
        record(results, str(iteration), commit, "keep" if keep else "discard", outcome,
               f"{reason}; bytes={candidate_bytes}" + (f"; key={key}" if evaluated else "")
               + (f"; cond={outcome['conditions']}" if outcome.get("conditions") else "") + evidence_field(outcome),
               cost + outcome.get("cost", 0.0), description)
        print(f"#{iteration} {'keep' if keep else 'discard'} ({reason}) {outcome.get('dev', '-')}/"
              f"{outcome.get('dev_total', '-')} heldout {outcome.get('heldout', '-')} spent ${spent:.2f}: {description}",
              flush=True)
        if keep:
            best, best_commit, best_bytes, best_key, idle = outcome, commit, candidate_bytes, key, 0
        else:
            git(tree, "reset", "-q", "--hard", best_commit)
            idle += 1
        if reason == CONDITIONS_CHANGED:
            print("measurement conditions changed; re-run the same tag to measure a fresh baseline", flush=True)
            break
        if infra >= args.max_infra_errors:
            # provider·timeout 장애가 이어지면 다음 후보도 무효 측정에 돈만 쓴다. 비용은 이미 장부에 있다.
            print(f"stopping: infra, {infra} evaluation errors in a row; re-run the same tag once it recovers",
                  flush=True)
            break
    print(f"best {best['dev']}/{best['dev_total']} heldout {best['heldout']}/{best['heldout_total']} at {best_commit[:12]} "
          f"on branch autoresearch/{args.tag}; spent ${spent:.2f}; log {results}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
