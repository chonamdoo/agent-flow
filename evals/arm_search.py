"""Q2 reduced-workflow search driver (#409 program v2.4 + v3 개정 v3.1): finds S* for the router's menu.

    python3 evals/arm_search.py run --kit . --case oyg --out <dir> --budget-usd 250 --seed 20261025 \
        --evaluator <private>/oyg/score.py
    python3 evals/arm_search.py report <dir>

It runs `evals/arm_compare.py` only as a CLI (`prepare`, `drive --timeout 7200 --budget 30`, `score`) and keeps its
own record next to the runs:

- `ledger.tsv`: one row per run attempt (variant, spec, axis, hypothesis, S, G, declaration, cost, seconds, the
  sha256 of `result.json`, status `ok` / `infra-error` / `harness-error`).
- `insights.md`: the dead ends (variants that failed the rule, and why) and the stops.
- `decisions.json`: the decision trail, replayed from the ledger by the same pure rules each time.
- `checkpoint-<n>.md`: a strategy summary every 20 runs.
- `s_star.json` when the search ends (`name` null when no variant survived: S* leaves the menu), `stop.json` when it
  stopped before that (budget, infra errors, checkpoint review, failed precheck).

Order (program v2.4): the opening block is C ×3 and S-min ×3 (all dimensions off, 2 reviewers), shuffled by `--seed`
and written to `order.txt`; the C reruns are also the noise floor. If S-min passes the quality-purpose rule
(`quality_rule`) against C, it is the candidate; otherwise dimensions come back one at a time, cumulatively on top of
S-min, in the order tdd → architecture_review → reviewers → design → planning, ×3 each, stopping at the first that
passes. Reviving `reviewers` removes S-min's reviewer override, so the profile's default reviewers (the generalist and
every review angle of the profile, `variants.profile_override` returns None) come back; a fixed count such as 5 would
not exist on every profile (node has 3 angles, so at most 4 reviewers). A candidate is re-measured 3 more times and
must pass again on all 6 runs; one that does not is a dead end and the revival goes on. S* is the confirmed variant
with the lowest cost per success.

A revival candidate whose precheck fails (the kit or the installed runner cannot take it) is a dead end too: a
`precheck-failed` row in the ledger (no cost, not an attempt), a failed `precheck` step in the decisions and
insights, and the search goes on to the next candidate. A failed precheck of C or S-min still stops the search
(`precheck-failed`, exit 5): without them there is no comparison.

Run accounting:
- `infra-error`: arm_compare exited non-zero, or `result.json` marks a harness error, an evaluator error or a
  controller error that is not a turn/budget limit. Its cost counts toward the budget; the run leaves the
  denominator and the same slot is run again (v3: a harness-broken run is re-run in place). Two in a row stop the
  search; `run --ack-infra` resumes after the cause is fixed.
- `harness-error`: this driver was interrupted during `prepare` or `drive`. The attempt is recorded, never resumed,
  and the slot starts again from scratch. An interrupted `score` is simply re-run (it spends no model budget).
- A run that times out, is contaminated or hits the controller's budget stays `ok`: it is an assigned attempt and
  counts as a failure.
- Budget: the sum of recorded costs. A run is not started when its expected cost (the median of that variant's
  recorded costs, or $25 before any) would take the total over `--budget-usd`.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import shutil
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import variants  # noqa: E402

COMPLETE = "declared-complete"
HARNESS_APPROVES = "harness-approves-phases"
BASELINE = "full-feature"
S_MIN = {"planning": False, "design": False, "tdd": False, "architecture_review": False, "reviewers": 2}
REVIVAL = ("tdd", "architecture_review", "reviewers", "design", "planning")
HYPOTHESES = {
    "baseline": "C(full-feature) 기준선 재실행: 비교 기준이자 비용·시간 잡음 바닥이다",
    "reduce-all": "모든 차원을 끄고 리뷰어 2명만 두어도 C보다 성공이 많고 성공 1건당 비용이 낮다",
    "tdd": "red(TDD)를 되살리면 테스트가 먼저 생겨 숨은 검사 통과가 늘어난다",
    "architecture_review": "architecture-review를 되살리면 구조 결함이 gates 전에 잡혀 fix-loop 상한 정지가 준다",
    "reviewers": "리뷰어 2명 제한을 풀어 profile 기본 리뷰어(일반 리뷰어와 profile의 모든 각도)를 되살리면 multi-review가 "
                 "더 많은 결함을 잡아 성공이 늘어난다",
    "design": "ddd-design을 되살리면 도메인 경계가 먼저 정해져 구현 실패가 준다",
    "planning": "기획 묶음(domain-grill~plan-review)을 되살리면 요구 누락이 줄어 성공이 늘어난다",
}
RUNS_PER_VARIANT = 3
CONFIRM_RUNS = 3
QUALITY_COST_MULTIPLE = 3.0  # v3: 성공 1건당 비용이 기준의 3배 이하
NOISE_FLOOR_COST = 0.14  # v2.4: A/A 잡음 바닥(비용)
DEFAULT_EXPECTED_COST = 25.0
INFRA_STOP = 2
PRECHECK_FAILED = "precheck-failed"
CHECKPOINT_EVERY = 20
DRIVE_TIMEOUT, DRIVE_BUDGET = 7200, 30.0
LEDGER = "ledger.tsv"
COLUMNS = ("run", "stage", "label", "variant", "spec", "axis", "hypothesis", "S", "G", "declaration", "success",
           "false_complete", "integrity_flags", "cost_usd", "reviewer_cost_usd", "seconds", "result_sha256",
           "status", "run_status", "detail", "run_dir")
CONFIG_KEYS = ("case", "case_dir", "seed", "budget_usd")


# ---------- variants ----------

def baseline() -> dict:
    return {"label": "C", "name": BASELINE, "arm": "C", "spec": None, "axis": "baseline"}


def revived(step: int) -> dict:
    """S-min with the first `step` dimensions of REVIVAL back on (step 0 is S-min itself).

    Reviving `reviewers` drops the key: the profile's default reviewers, the same level C runs with."""
    spec = dict(S_MIN)
    for dim in REVIVAL[:step]:
        spec.pop(dim)
    label = "S-min" + "".join(f"+{dim}" for dim in REVIVAL[:step])
    return {"label": label, "name": variants.name(spec), "arm": "S", "spec": variants.normalize(spec),
            "axis": REVIVAL[step - 1] if step else "reduce-all"}


def candidates() -> list[dict]:
    return [revived(step) for step in range(len(REVIVAL) + 1)]


def opening_order(seed: int) -> list[str]:
    order = [BASELINE] * RUNS_PER_VARIANT + [revived(0)["name"]] * RUNS_PER_VARIANT
    random.Random(seed).shuffle(order)
    return order


def axis_ok(previous_axes: list[str], axis: str) -> bool:
    """program v2.4: the same axis is not changed three candidates in a row."""
    return previous_axes[-2:] != [axis, axis]


# ---------- per-run outcome ----------

def rule_violation(result: dict) -> bool:
    """A human-only approval (arm_compare's rule for the runner arms, which C and S both are)."""
    phase_rule = result.get("approval_rules")
    return bool(result.get("spec_self_approvals") or result.get("verify_downgrades")
                or result.get("manual_spec_approvals")
                or (phase_rule == HARNESS_APPROVES and result.get("phase_self_approvals")))


def _limit_stop(drive: dict) -> bool:
    return str(drive.get("result_subtype") or "").startswith("error_max")


def run_cost(result: dict | None, drive: dict | None) -> tuple[float | None, float]:
    """The run's total (controller, V gate, router, plus the runner reviewers' priced lower bound when recorded)."""
    result, drive = result or {}, drive or {}
    total = result.get("total_cost_usd")
    if total is None:
        total = result.get("controller_cost_usd", drive.get("cost_usd"))
    reviewers = result.get("reviewers") if isinstance(result.get("reviewers"), dict) else {}
    extra = reviewers.get("cost_lower_bound_usd")
    if extra is None:
        extra = reviewers.get("priced_cost_lower_bound_usd")
    extra = float(extra or 0.0)
    return (None if total is None else round(float(total) + extra, 4)), extra


def outcome(result: dict | None, drive: dict | None, failed_step: str | None = None) -> dict:
    """The ledger fields of one finished attempt. Success is v2.4's five conditions plus v3's run_status ok."""
    result, drive = result or {}, drive or {}
    ui = result.get("ui") if isinstance(result.get("ui"), dict) else {}
    oracle = bool(result.get("oracle"))
    flags = int((result.get("integrity") or {}).get("flags") or 0)
    declaration = result.get("declaration")
    if failed_step:
        status, detail = "infra-error", f"arm_compare {failed_step} failed"
    elif not result:
        status, detail = "infra-error", "no result.json"
    elif result.get("harness_error"):
        status, detail = "infra-error", f"harness error: {result['harness_error']}"
    elif ui.get("error"):
        status, detail = "infra-error", f"evaluator error: {str(ui['error'])[:120]}"
    elif (result.get("drive_is_error") or result.get("drive_exit") not in (None, 0)) and not (
            _limit_stop(drive) or result.get("drive_timed_out")):
        status, detail = "infra-error", f"controller error (exit {result.get('drive_exit')})"
    else:
        status, detail = "ok", ""
    if status == "ok":
        run_status = ("contaminated" if result.get("contaminated") else
                      "timed-out" if result.get("drive_timed_out") else
                      "limit" if _limit_stop(drive) else "ok")
    else:
        run_status = status
    success = (status == "ok" and run_status in ("ok", "limit") and oracle and declaration == COMPLETE
               and not rule_violation(result) and flags == 0)
    cost, reviewer = run_cost(result, drive)
    return {"S": ui.get("S", int(oracle)) if result else "", "G": ui.get("G", ""), "declaration": declaration or "",
            "success": int(success), "false_complete": int(declaration == COMPLETE and not oracle),
            "integrity_flags": flags, "cost_usd": cost, "reviewer_cost_usd": reviewer or "",
            "seconds": drive.get("seconds", ""), "status": status, "run_status": run_status, "detail": detail}


# ---------- statistics and rules ----------

def _num(value) -> float | None:
    try:
        return None if value in ("", None) else float(value)
    except (TypeError, ValueError):
        return None


def stats(rows: list[dict]) -> dict:
    """Over the counted (`ok`) attempts. Cost per success counts the failed runs' cost too (v3 metric 4)."""
    counted = [row for row in rows if row["status"] == "ok"]
    costs = [c for c in (_num(row["cost_usd"]) for row in counted) if c is not None]
    seconds = [s for s in (_num(row["seconds"]) for row in counted) if s is not None]
    successes = sum(int(row["success"]) for row in counted)
    total = sum(costs)
    n = len(counted)
    return {"n": n, "successes": successes, "false_completes": sum(int(row["false_complete"]) for row in counted),
            "integrity_flags": sum(int(row["integrity_flags"]) for row in counted),
            "success_rate": successes / n if n else 0.0,
            "false_complete_rate": sum(int(row["false_complete"]) for row in counted) / n if n else 0.0,
            "total_cost": round(total, 4), "cost_per_success": total / successes if successes else math.inf,
            "median_cost": statistics.median(costs) if costs else None,
            "median_seconds": statistics.median(seconds) if seconds else None,
            "infra_errors": sum(row["status"] == "infra-error" for row in rows),
            "harness_errors": sum(row["status"] == "harness-error" for row in rows)}


def noise_floor(baseline_rows: list[dict]) -> dict:
    """The C reruns' relative spread, (max − min) / median, for cost and time; never below v2.4's 14% for cost."""
    def spread(key: str) -> float | None:
        values = [v for v in (_num(row[key]) for row in baseline_rows if row["status"] == "ok") if v is not None]
        if len(values) < 2 or not statistics.median(values):
            return None
        return (max(values) - min(values)) / statistics.median(values)
    cost, time = spread("cost_usd"), spread("seconds")
    return {"measured_cost": cost, "measured_seconds": time,
            "cost": max(NOISE_FLOOR_COST, cost) if cost is not None else NOISE_FLOOR_COST}


def quality_rule(variant: dict, base: dict, noise: float, minimum: int = RUNS_PER_VARIANT) -> tuple[bool, list[str]]:
    """S* is a cheaper stand-in for C (program-v2.4, v3.3): non-inferior to C on success and false completion,
    no integrity warning, and a cost per success lower than C's beyond the noise floor (and within 3× of it).

    Counts are compared as rates so a confirmed variant (6 runs) is judged against C's 3 on the same scale; at equal
    n this is the preregistered "S 합계", "거짓 완료 수" comparison.
    """
    failed = []
    if variant["n"] < minimum or base["n"] < RUNS_PER_VARIANT:
        failed.append(f"표본 부족: 변형 {variant['n']}/{minimum}, C {base['n']}/{RUNS_PER_VARIANT}")
    if not variant["success_rate"] >= base["success_rate"]:
        failed.append(f"성공률 {variant['successes']}/{variant['n']} < C {base['successes']}/{base['n']}")
    if not variant["false_complete_rate"] <= base["false_complete_rate"]:
        failed.append(f"거짓 완료 {variant['false_completes']}/{variant['n']} > C {base['false_completes']}/{base['n']}")
    if variant["integrity_flags"]:
        failed.append(f"무결성 경고 {variant['integrity_flags']}건")
    v_cps, c_cps = variant["cost_per_success"], base["cost_per_success"]
    if not v_cps <= QUALITY_COST_MULTIPLE * c_cps:
        failed.append(f"성공 1건당 비용 {_money(v_cps)} > C의 3배 {_money(QUALITY_COST_MULTIPLE * c_cps)}")
    if not (math.isfinite(v_cps) and (not math.isfinite(c_cps) or v_cps < c_cps * (1 - noise))):
        failed.append(f"성공 1건당 비용 {_money(v_cps)}이 C {_money(c_cps)}보다 잡음 바닥 {noise:.0%} 넘게 싸지 않다")
    return not failed, failed


def choose_s_star(confirmed: list[dict]) -> dict | None:
    """Among variants that passed after confirmation, the lowest cost per success (ties: fewer phases kept first)."""
    if not confirmed:
        return None
    return min(confirmed, key=lambda item: (item["stats"]["cost_per_success"], -len(item["spec"] or {})))


def pareto(points: list[dict]) -> list[dict]:
    """Variants no other variant beats on both success rate (higher) and cost per success (lower)."""
    def dominated(a: dict) -> bool:
        return any(b is not a and b["success_rate"] >= a["success_rate"]
                   and b["cost_per_success"] <= a["cost_per_success"]
                   and (b["success_rate"] > a["success_rate"] or b["cost_per_success"] < a["cost_per_success"])
                   for b in points)
    return [point for point in points if point["n"] and not dominated(point)]


def plan(rows: list[dict], order: list[str]) -> dict:
    """The next run, or the end of the search, replayed from the ledger. Pure: the same ledger gives the same answer."""
    by_name: dict[str, list[dict]] = {}
    for row in rows:
        by_name.setdefault(row["variant"], []).append(row)
    counted = {name: sum(row["status"] == "ok" for row in group) for name, group in by_name.items()}
    trail: list[dict] = []
    smin = revived(0)
    opened = counted.get(BASELINE, 0) + counted.get(smin["name"], 0)
    if opened < len(order):
        # every counted opening run took the slot it was planned for, so the counts are a prefix of `order`
        name = order[opened]
        return {"kind": "run", "variant": baseline() if name == BASELINE else smin, "stage": "opening",
                "remaining": len(order) - opened, "trail": trail}
    base = stats(by_name.get(BASELINE, []))
    noise = noise_floor(by_name.get(BASELINE, []))
    axes = ["baseline"]
    confirmed = []
    for variant in candidates():
        if not axis_ok(axes, variant["axis"]):
            raise ValueError(f"axis rule: {variant['axis']} would be changed three times in a row")
        axes.append(variant["axis"])
        mine = by_name.get(variant["name"], [])
        n = counted.get(variant["name"], 0)
        refused = [row for row in mine if row["status"] == PRECHECK_FAILED]
        if refused and n == 0:
            trail.append({"step": "precheck", "label": variant["label"], "variant": variant["name"],
                          "axis": variant["axis"], "passed": False,
                          "reasons": [f"precheck 실패: {refused[-1].get('detail') or '-'}"], "stats": None})
            continue
        if n < RUNS_PER_VARIANT:
            return {"kind": "run", "variant": variant, "stage": "search", "remaining": RUNS_PER_VARIANT - n,
                    "trail": trail}
        first = stats(_first_counted(mine, RUNS_PER_VARIANT))
        passed, reasons = quality_rule(first, base, noise["cost"])
        trail.append({"step": "search", "label": variant["label"], "variant": variant["name"],
                      "axis": variant["axis"], "passed": passed, "reasons": reasons, "stats": _plain(first)})
        if not passed:
            continue
        if n < RUNS_PER_VARIANT + CONFIRM_RUNS:
            return {"kind": "run", "variant": variant, "stage": "confirm",
                    "remaining": RUNS_PER_VARIANT + CONFIRM_RUNS - n, "trail": trail}
        both = stats(_first_counted(mine, RUNS_PER_VARIANT + CONFIRM_RUNS))
        passed, reasons = quality_rule(both, base, noise["cost"], RUNS_PER_VARIANT + CONFIRM_RUNS)
        trail.append({"step": "confirm", "label": variant["label"], "variant": variant["name"],
                      "axis": variant["axis"], "passed": passed, "reasons": reasons, "stats": _plain(both)})
        if passed:
            confirmed.append({**variant, "stats": both})
            break
    star = choose_s_star(confirmed)
    return {"kind": "done", "s_star": star, "baseline": base, "noise": noise, "trail": trail}


def _first_counted(rows: list[dict], count: int) -> list[dict]:
    """The attempts up to and including the `count`-th counted one (later attempts belong to the next step)."""
    out, seen = [], 0
    for row in rows:
        if seen >= count:
            break
        out.append(row)
        seen += row["status"] == "ok"
    return out


def expected_cost(rows: list[dict], name: str) -> float:
    costs = [c for c in (_num(row["cost_usd"]) for row in rows if row["variant"] == name) if c is not None]
    return statistics.median(costs) if costs else DEFAULT_EXPECTED_COST


def spent(rows: list[dict]) -> float:
    return round(sum(c for c in (_num(row["cost_usd"]) for row in rows) if c is not None), 4)


def budget_allows(rows: list[dict], name: str, cap: float) -> tuple[bool, float]:
    expected = expected_cost(rows, name)
    return spent(rows) + expected <= cap, expected


def infra_stop(rows: list[dict], acked: int = 0) -> bool:
    """INFRA_STOP infra errors in a row (interruptions are neither), counting only rows after the last ack."""
    streak = 0
    for row in rows[acked:]:
        if row["status"] == "infra-error":
            streak += 1
        elif row["status"] == "ok":
            streak = 0
    return streak >= INFRA_STOP


def checkpoint_projection(rows: list[dict], step: dict) -> float:
    """What finishing the current step and its confirmation would still cost, at the expected cost per run."""
    remaining = step["remaining"] + (CONFIRM_RUNS if step["stage"] != "confirm" else 0)
    return remaining * expected_cost(rows, step["variant"]["name"])


# ---------- ledger and files ----------

def read_ledger(out: Path) -> list[dict]:
    path = out / LEDGER
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def append_ledger(out: Path, row: dict) -> None:
    path = out / LEDGER
    new = not path.is_file()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, delimiter="\t", lineterminator="\n")
        if new:
            writer.writeheader()
        writer.writerow({key: _cell(row.get(key, "")) for key in COLUMNS})


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value).replace("\t", " ").replace("\n", " ")


def _json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=_default) + "\n", encoding="utf-8")


def _default(value):
    return None if isinstance(value, float) and not math.isfinite(value) else str(value)


def _plain(item: dict) -> dict:
    return {key: (None if isinstance(value, float) and not math.isfinite(value) else value)
            for key, value in item.items()}


def _money(value: float | None) -> str:
    if value is None:
        return "-"
    return "∞" if not math.isfinite(value) else f"${value:,.2f}"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


# ---------- arm_compare CLI ----------

class Driver:
    def __init__(self, args: argparse.Namespace, out: Path):
        self.kit = args.kit.resolve()
        self.arm_compare = (args.arm_compare or self.kit / "evals" / "arm_compare.py").resolve()
        self.case, self.case_dir = args.case, args.case_dir.resolve() if args.case_dir else None
        self.evaluator = args.evaluator.resolve() if args.evaluator else None
        self.out = out

    def call(self, command: str, *argv: str, log: Path) -> int:
        cmd = [sys.executable, str(self.arm_compare), command, *argv]
        with log.open("ab") as handle:
            handle.write(f"$ {' '.join(cmd)}\n".encode())
            handle.flush()
            return subprocess.run(cmd, cwd=self.kit, stdout=handle, stderr=subprocess.STDOUT,
                                  check=False).returncode

    def prepare_argv(self, variant: dict, trial: int, output: Path) -> list[str]:
        argv = ["--kit", str(self.kit), "--arm", variant["arm"], "--case", self.case, "--trial", str(trial),
                "--output", str(output)]
        if self.case_dir:
            argv += ["--case-dir", str(self.case_dir)]
        if variant["spec"] is not None:
            argv += ["--variant", json.dumps(variant["spec"], sort_keys=True)]
        return argv

    def precheck(self, variant: dict) -> tuple[bool, str]:
        """prepare only: the kit takes the variant, the prompt names it and the installed runner can load it."""
        root = self.out / "precheck"
        root.mkdir(exist_ok=True)
        record = root / f"{variant['name']}.json"
        done = _json(record)
        if done and done.get("ok"):
            return True, "cached"
        target = root / variant["name"]
        if target.exists():
            shutil.rmtree(target)
        log = root / f"{variant['name']}.log"
        problems = []
        if self.call("prepare", *self.prepare_argv(variant, 0, target), log=log) != 0:
            problems.append("prepare failed (see log)")
        else:
            meta = _json(target / "arm.json") or {}
            prompt = (target / "prompt.md").read_text(encoding="utf-8")
            if meta.get("workflow") != variant["name"]:
                problems.append(f"arm.json workflow {meta.get('workflow')!r} is not {variant['name']!r}")
            if variant["spec"] is not None and (meta.get("variant") or {}).get("spec") != variant["spec"]:
                problems.append("arm.json variant spec differs")
            if f"--workflow {variant['name']}" not in prompt:
                problems.append("prompt does not name the workflow")
            launcher = target / "project" / ".agent-flow" / "bin" / "agent-flow"
            exported = subprocess.run([str(launcher), "workflow", "export", "--workflow", variant["name"]],
                                      cwd=target / "project", capture_output=True, text=True, check=False)
            if exported.returncode != 0:
                problems.append(f"installed runner rejects the workflow: {exported.stderr.strip()[-300:]}")
            elif variant["spec"] is not None:
                phases = [phase.get("id") for phase in json.loads(exported.stdout).get("phases", [])]
                if phases != (meta.get("variant") or {}).get("phases"):
                    problems.append("exported phases differ from the variant's")
        ok = not problems
        _write_json(record, {"ok": ok, "variant": variant["name"], "label": variant["label"], "spec": variant["spec"],
                             "case_kind": (_json(target / "arm.json") or {}).get("case_kind"), "problems": problems, "checked_at": datetime.now().isoformat(timespec="seconds")})
        if ok:  # the record is what matters; the copied kit and project are large
            shutil.rmtree(target / "kit", ignore_errors=True)
            shutil.rmtree(target / "project", ignore_errors=True)
        return ok, "; ".join(problems)

    def execute(self, state_path: Path) -> dict:
        """Runs (or continues) one attempt. Steps whose output exists are skipped.

        The attempt's state sits beside its run directory (`<run>.json`): `prepare` creates the directory itself.
        """
        state = _json(state_path)
        run_dir = state_path.with_suffix("")
        variant, log = state["variant"], state_path.with_suffix(".log")
        steps = [("prepare", run_dir / "arm.json", self.prepare_argv(variant, state["trial"], run_dir)),
                 ("drive", run_dir / "drive.json", [str(run_dir), "--timeout", str(DRIVE_TIMEOUT),
                                                     "--budget", f"{DRIVE_BUDGET:g}"]),
                 ("score", run_dir / "result.json",
                  [str(run_dir), *(["--evaluator", str(self.evaluator)] if self.evaluator else [])])]
        failed = None
        for step, output, argv in steps:
            if output.is_file():
                continue
            state["steps"][step] = "started"
            _write_json(state_path, state)
            code = self.call(step, *argv, log=log)
            state["steps"][step] = "done" if code == 0 and output.is_file() else f"exit {code}"
            _write_json(state_path, state)
            if state["steps"][step] != "done":
                failed = step
                break
        result, drive = _json(run_dir / "result.json"), _json(run_dir / "drive.json")
        return {**_row_head(state, run_dir), **outcome(result, drive, failed),
                "result_sha256": sha256(run_dir / "result.json")}


def _row_head(state: dict, run_dir: Path) -> dict:
    variant = state["variant"]
    return {"run": state["run"], "stage": state["stage"], "label": variant["label"], "variant": variant["name"],
            "spec": variant["spec"] if variant["spec"] is not None else "", "axis": variant["axis"],
            "hypothesis": HYPOTHESES[variant["axis"]], "run_dir": str(run_dir)}


def recover(driver: Driver, out: Path) -> None:
    """An attempt without a ledger row: continue it, unless `prepare` or `drive` was cut off mid-way."""
    logged = {row["run"] for row in read_ledger(out)}
    for state_path in sorted((out / "runs").glob("*.json")):
        state = _json(state_path)
        if not state or state["run"] in logged:
            continue
        run_dir = state_path.with_suffix("")
        cut = [step for step in ("prepare", "drive") if state["steps"].get(step) == "started"]
        if cut:
            append_ledger(out, {**_row_head(state, run_dir), "status": "harness-error", "run_status": "harness-error",
                                "detail": f"interrupted during {cut[0]}; slot re-run from scratch",
                                "success": 0, "false_complete": 0, "integrity_flags": 0,
                                "cost_usd": (_json(run_dir / "drive.json") or {}).get("cost_usd")})
        else:
            if state["steps"].get("prepare") != "done" and run_dir.exists():
                shutil.rmtree(run_dir)  # cut off before `prepare` started; it needs a fresh directory
            append_ledger(out, driver.execute(state_path))


# ---------- reports ----------

def variant_points(rows: list[dict]) -> list[dict]:
    names = {row["variant"]: row["label"] for row in rows}
    return [{"variant": name, "label": label, **stats([row for row in rows if row["variant"] == name])}
            for name, label in names.items()]


def summary_text(rows: list[dict], step: dict, budget: float | None = None) -> str:
    points = variant_points(rows)
    front = {point["variant"] for point in pareto(points)}
    lines = [f"runs {len(rows)}, spent {_money(spent(rows))}" + (f" of {_money(budget)}" if budget else ""), "",
             ("| variant | label | n | S | false complete | integrity | cost/success | median cost | median s | "
              "infra | harness | pareto |"), "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for p in points:
        lines.append(f"| {p['variant']} | {p['label']} | {p['n']} | {p['successes']} | {p['false_completes']} | "
                     f"{p['integrity_flags']} | {_money(p['cost_per_success'])} | {_money(p['median_cost'])} | "
                     f"{p['median_seconds'] if p['median_seconds'] is not None else '-'} | {p['infra_errors']} | "
                     f"{p['harness_errors']} | {'*' if p['variant'] in front else ''} |")
    noise = noise_floor([row for row in rows if row["variant"] == BASELINE])
    lines += ["", (f"noise floor (C reruns): cost {_pct(noise['measured_cost'])} measured, "
                   f"{noise['cost']:.0%} used; time {_pct(noise['measured_seconds'])}"), "", "decisions:"]
    for item in step["trail"]:
        lines.append(f"- {item['step']} {item['label']} ({item['axis']}): {'pass' if item['passed'] else 'fail'}"
                     + (f" — {'; '.join(item['reasons'])}" if item["reasons"] else ""))
    if step["kind"] == "run":
        lines.append(f"- next: {step['variant']['label']} ({step['stage']}, {step['remaining']} more)")
    else:
        star = step["s_star"]
        lines.append(f"- done: S* = {star['label']} {star['name']}" if star else "- done: no variant survived; "
                     "S* leaves the router menu")
    return "\n".join(lines) + "\n"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def write_insights(out: Path, rows: list[dict], step: dict) -> None:
    lines = ["# Q2 탐색 insights", "", "## 막다른 길", ""]
    dead = [item for item in step["trail"] if not item["passed"]]
    lines += [f"- {item['label']} `{item['variant']}` ({item['step']}, 축 {item['axis']}): {'; '.join(item['reasons'])}"
              for item in dead] or ["- (없음)"]
    errors = [row for row in rows if row["status"] != "ok"]
    lines += ["", "## 분모에서 뺀 시도", ""]
    lines += [f"- {row['run']} {row['label']}: {row['status']} — {row['detail']}" for row in errors] or ["- (없음)"]
    stop = _json(out / "stop.json")
    if stop:
        lines += ["", "## 정지", "", f"- {stop['reason']}: {stop.get('detail', '')}"]
    (out / "insights.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write_json(out / "decisions.json", step["trail"])


def write_s_star(out: Path, rows: list[dict], step: dict) -> dict:
    star = step["s_star"]
    evidence = {"ledger_sha256": sha256(out / LEDGER), "trail": step["trail"],
                "baseline_runs": [{"run": row["run"], "result_sha256": row["result_sha256"]}
                                  for row in rows if row["variant"] == BASELINE and row["status"] == "ok"]}
    if star:
        evidence["runs"] = [{"run": row["run"], "result_sha256": row["result_sha256"]}
                            for row in rows if row["variant"] == star["name"] and row["status"] == "ok"]
        data = {"name": star["name"], "label": star["label"], "spec": star["spec"], "axis": star["axis"],
                "stats": _plain(star["stats"]), "evidence": evidence}
    else:
        data = {"name": None, "label": None, "spec": None, "menu": "removed", "evidence": evidence}
    _write_json(out / "s_star.json", data)
    return data


def stop(out: Path, reason: str, detail: str, code: int) -> int:
    _write_json(out / "stop.json", {"reason": reason, "detail": detail,
                                    "at": datetime.now().isoformat(timespec="seconds")})
    print(f"stopped: {reason}: {detail}")
    return code


# ---------- commands ----------

def cmd_run(args: argparse.Namespace) -> int:
    out = args.out.resolve()
    (out / "runs").mkdir(parents=True, exist_ok=True)
    config = {"case": args.case, "case_dir": str(args.case_dir.resolve()) if args.case_dir else None,
              "seed": args.seed, "budget_usd": args.budget_usd}
    saved = _json(out / "config.json")
    if saved is None:
        _write_json(out / "config.json", {**config, "kit": str(args.kit.resolve()),
                                          "created_at": datetime.now().isoformat(timespec="seconds")})
        (out / "order.txt").write_text("\n".join(opening_order(args.seed)) + "\n", encoding="utf-8")
    elif any(saved.get(key) != config[key] for key in CONFIG_KEYS):
        raise SystemExit(f"{out} was started with {({k: saved.get(k) for k in CONFIG_KEYS})}; got {config}")
    order = (out / "order.txt").read_text(encoding="utf-8").split()
    (out / "stop.json").unlink(missing_ok=True)
    acks = _json(out / "acks.json") or {"infra": 0}
    if args.ack_infra:
        acks["infra"] = len(read_ledger(out))
        _write_json(out / "acks.json", acks)
    driver = Driver(args, out)
    recover(driver, out)
    while True:
        rows = read_ledger(out)
        step = plan(rows, order)
        write_insights(out, rows, step)
        if infra_stop(rows, acks["infra"]):
            code = stop(out, "infra-errors", f"{INFRA_STOP} infra errors in a row; fix the cause and `run --ack-infra`",
                        3)
            write_insights(out, rows, step)
            return code
        if step["kind"] == "done":
            star = write_s_star(out, rows, step)
            print(json.dumps({"s_star": star["name"], "label": star["label"], "spec": star["spec"]}))
            return 0
        variant = step["variant"]
        if rows and len(rows) % CHECKPOINT_EVERY == 0 and not (out / f"checkpoint-{len(rows)}.md").is_file():
            projected = checkpoint_projection(rows, step)
            (out / f"checkpoint-{len(rows)}.md").write_text(
                f"# 전략 점검 {len(rows)} run\n\n{summary_text(rows, step, args.budget_usd)}\n"
                f"projected to finish {variant['label']} and its confirmation: {_money(projected)}\n", encoding="utf-8")
            if spent(rows) + projected > args.budget_usd:
                code = stop(out, "checkpoint-review", f"spent {_money(spent(rows))} + projected {_money(projected)} "
                            f"> {_money(args.budget_usd)}; review checkpoint-{len(rows)}.md, then `run` again", 4)
                write_insights(out, rows, step)
                return code
        allowed, expected = budget_allows(rows, variant["name"], args.budget_usd)
        if not allowed:
            code = stop(out, "budget", f"spent {_money(spent(rows))} + expected {_money(expected)} for "
                        f"{variant['label']} > cap {_money(args.budget_usd)}", 2)
            write_insights(out, rows, step)
            return code
        ok, why = driver.precheck(variant)
        kind = (_json(out / "precheck" / f"{variant['name']}.json") or {}).get("case_kind")
        if ok and kind in ("ui", "external") and driver.evaluator is None:
            code = stop(out, "precheck-failed", f"a {kind} case is scored by its private evaluator: pass --evaluator", 5)
            write_insights(out, rows, step)
            return code
        if not ok and step["stage"] != "opening":
            # a revival candidate the kit cannot take is a dead end, not the end of the search
            append_ledger(out, {"run": f"r{len(rows) + 1:03d}", "stage": "precheck", "label": variant["label"],
                                "variant": variant["name"], "spec": variant["spec"], "axis": variant["axis"],
                                "hypothesis": HYPOTHESES[variant["axis"]], "success": 0, "false_complete": 0,
                                "integrity_flags": 0, "status": PRECHECK_FAILED, "run_status": PRECHECK_FAILED,
                                "detail": why[:300]})
            continue
        if not ok:
            code = stop(out, "precheck-failed", f"{variant['label']} {variant['name']}: {why}", 5)
            write_insights(out, rows, step)
            return code
        run_id = f"r{len(rows) + 1:03d}"
        run_dir = out / "runs" / f"{run_id}-{variant['label']}"
        if run_dir.exists():  # left by an attempt cut off before its state was written
            shutil.rmtree(run_dir)
        trial = sum(row["variant"] == variant["name"] for row in rows) + 1
        state_path = run_dir.with_name(run_dir.name + ".json")
        _write_json(state_path, {"run": run_id, "stage": step["stage"], "variant": variant, "trial": trial,
                                 "steps": {}})
        append_ledger(out, driver.execute(state_path))
        if args.max_runs is not None:
            args.max_runs -= 1
            if args.max_runs <= 0:
                return 0


def cmd_precheck(args: argparse.Namespace) -> int:
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    driver = Driver(args, out)
    chosen = [baseline(), *candidates()]
    if args.only:
        chosen = [item for item in chosen if item["label"] in args.only]
    failures = 0
    for variant in chosen:
        ok, why = driver.precheck(variant)
        failures += not ok
        print(f"{variant['label']:<48} {variant['name']:<14} {'ok' if ok else 'FAIL ' + why}")
    return 1 if failures else 0


def cmd_report(args: argparse.Namespace) -> int:
    out = args.out.resolve()
    rows = read_ledger(out)
    order = (out / "order.txt").read_text(encoding="utf-8").split() if (out / "order.txt").is_file() else []
    config = _json(out / "config.json") or {}
    print(summary_text(rows, plan(rows, order), config.get("budget_usd")), end="")
    for name in ("stop.json", "s_star.json"):
        data = _json(out / name)
        if data:
            shown = data if name == "stop.json" else {k: data.get(k) for k in ("name", "label", "spec", "menu")}
            print(f"{name}: {json.dumps(shown, ensure_ascii=False)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "precheck"):
        item = sub.add_parser(command)
        item.add_argument("--kit", type=Path, required=True)
        item.add_argument("--case", required=True)
        item.add_argument("--case-dir", type=Path)
        item.add_argument("--out", type=Path, required=True)
        item.add_argument("--evaluator", type=Path, help="the private evaluator's score.py (UI or external case)")
        item.add_argument("--arm-compare", type=Path, help=argparse.SUPPRESS)
        if command == "run":
            item.add_argument("--budget-usd", type=float, required=True)
            item.add_argument("--seed", type=int, required=True)
            item.add_argument("--ack-infra", action="store_true", help="resume after an infra-error stop")
            item.add_argument("--max-runs", type=int, help="start at most this many runs in this invocation")
        else:
            item.add_argument("--only", action="append", metavar="LABEL", help="e.g. C, S-min, S-min+tdd")
    rep = sub.add_parser("report")
    rep.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    return {"run": cmd_run, "precheck": cmd_precheck, "report": cmd_report}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
