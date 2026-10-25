"""Q2 search driver (evals/arm_search.py): the preregistered rules, the search order and resume, on a stubbed arm_compare."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
import arm_search  # noqa: E402

# A stand-in for `arm_compare.py`: `prepare` writes arm.json/prompt.md and a launcher that exports the variant,
# `drive` and `score` write drive.json/result.json from the scenario's next outcome for that workflow.
FAKE = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ["EVALS"])
import variants
scenario = Path(os.environ["SCENARIO"])
data = json.loads(scenario.read_text())
cmd, rest = sys.argv[1], sys.argv[2:]
with open(scenario.parent / "calls.log", "a") as log:
    log.write(cmd + " " + (rest[0] if cmd != "prepare" else rest[rest.index("--output") + 1]) + "\n")
def opt(name):
    return rest[rest.index(name) + 1] if name in rest else None
if cmd == "prepare":
    out = Path(opt("--output"))
    out.mkdir(parents=True)
    spec = json.loads(opt("--variant")) if opt("--variant") else None
    name = variants.name(spec) if spec is not None else "full-feature"
    meta = {"arm": opt("--arm"), "workflow": name, "trial": int(opt("--trial")), "case_kind": os.environ.get("KIND")}
    if spec is not None:
        meta["variant"] = {"name": name, "spec": variants.normalize(spec), "phases": ["green", "gates"]}
    (out / "arm.json").write_text(json.dumps(meta))
    (out / "prompt.md").write_text(f"agent-flow run task --workflow {name}\n")
    launcher = out / "project" / ".agent-flow" / "bin" / "agent-flow"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\necho '" + json.dumps({"phases": [{"id": "green"}, {"id": "gates"}]}) + "'\n")
    launcher.chmod(0o755)
    sys.exit(0)
run = Path(rest[0])
meta = json.loads((run / "arm.json").read_text())
counters = scenario.parent / "counters.json"
count = json.loads(counters.read_text()) if counters.is_file() else {}
key = meta["workflow"]
if cmd == "drive":
    count[key] = count.get(key, 0) + 1
    counters.write_text(json.dumps(count))
outcomes = data.get(key, data.get("*"))
item = outcomes[min(count.get(key, 1), len(outcomes)) - 1]
if item.get("fail") == cmd:
    sys.exit(1)
if cmd == "drive":
    (run / "drive.json").write_text(json.dumps({"cost_usd": item.get("cost", 10.0), "seconds": item.get("seconds", 100),
                                                "exit": 0}))
else:
    result = {"oracle": item.get("ok", False), "declaration": item.get("declaration", "declared-complete"),
              "integrity": {"flags": item.get("flags", 0)}, "total_cost_usd": item.get("cost", 10.0),
              "harness_error": item.get("harness_error"), "drive_exit": 0, "drive_is_error": False,
              "approval_rules": "harness-approves-phases", "ui": {"S": int(item.get("ok", False)), "G": 0.5}}
    (run / "result.json").write_text(json.dumps(result))
'''

SMIN = arm_search.revived(0)["name"]
TDD = arm_search.revived(1)["name"]
ARCH = arm_search.revived(2)["name"]


def _row(variant: str, *, ok: bool = False, declaration: str = "declared-complete", cost: float = 10.0,
         status: str = "ok", flags: int = 0, seconds: float = 100) -> dict:
    return {"variant": variant, "label": variant, "status": status, "success": int(ok and status == "ok"),
            "false_complete": int(declaration == "declared-complete" and not ok), "integrity_flags": flags,
            "cost_usd": cost, "seconds": seconds, "run": "", "result_sha256": ""}


def _opening(c_ok: int = 0, s_ok: int = 0, *, c_cost: float = 20.0, s_cost: float = 8.0) -> list[dict]:
    return ([_row("full-feature", ok=i < c_ok, cost=c_cost) for i in range(3)]
            + [_row(SMIN, ok=i < s_ok, cost=s_cost) for i in range(3)])


def _order() -> list[str]:
    return ["full-feature"] * 3 + [SMIN] * 3


# ---------- rules ----------

def test_quality_rule_passes_a_variant_better_and_cheaper_than_c():
    base = arm_search.stats([_row("C", ok=True, cost=20), _row("C", cost=20), _row("C", cost=20)])
    better = arm_search.stats([_row("v", ok=True, cost=8), _row("v", ok=True, cost=8), _row("v", cost=8,
                                                                                            declaration="declared-blocked")])
    assert arm_search.quality_rule(better, base, 0.14) == (True, [])


@pytest.mark.parametrize("variant_rows, reason", [
    ([_row("v", cost=8, declaration="x")] * 3, "성공률"),
    ([_row("v", ok=True, cost=8), _row("v", cost=8), _row("v", cost=8)], "거짓 완료"),
    ([_row("v", ok=True, cost=8, flags=1), _row("v", ok=True, cost=8), _row("v", cost=8, declaration="x")], "무결성"),
    ([_row("v", ok=True, cost=150), _row("v", ok=True, cost=150), _row("v", cost=150, declaration="x")], "3배"),
    ([_row("v", ok=True, cost=37), _row("v", ok=True, cost=37), _row("v", cost=37, declaration="x")], "잡음"),
    ([_row("v", ok=True), _row("v", ok=True)], "표본"),
])
def test_quality_rule_names_each_failed_condition(variant_rows, reason):
    # C: 1/3 success, 1 false completion, $60 per success
    base = arm_search.stats([_row("C", ok=True, cost=20), _row("C", cost=20), _row("C", cost=20, declaration="x")])
    passed, reasons = arm_search.quality_rule(arm_search.stats(variant_rows), base, 0.14)
    assert not passed and any(reason in item for item in reasons)


def test_a_c_without_successes_has_infinite_cost_per_success_and_any_success_is_cheaper():
    base = arm_search.stats([_row("C", cost=20)] * 3)
    assert base["cost_per_success"] == math.inf
    variant = arm_search.stats([_row("v", ok=True, cost=30), _row("v", cost=30, declaration="x"),
                                _row("v", cost=30, declaration="x")])
    assert arm_search.quality_rule(variant, base, 0.14) == (True, [])


def test_success_needs_all_conditions_and_infra_marks_are_not_attempts():
    good = {"oracle": True, "declaration": "declared-complete", "integrity": {"flags": 0}, "total_cost_usd": 9,
            "reviewers": {"cost_lower_bound_usd": 1.5}, "ui": {"S": 1, "G": 1.0}}
    assert {"success": 1, "status": "ok", "cost_usd": 10.5}.items() <= arm_search.outcome(good, {"seconds": 5}).items()
    assert arm_search.outcome({**good, "spec_self_approvals": 1}, {})["success"] == 0
    assert arm_search.outcome({**good, "integrity": {"flags": 2}}, {})["success"] == 0
    assert arm_search.outcome({**good, "contaminated": True}, {})["status"] == "ok"
    assert arm_search.outcome({**good, "contaminated": True}, {})["success"] == 0
    assert arm_search.outcome({**good, "harness_error": "x"}, {})["status"] == "infra-error"
    assert arm_search.outcome({**good, "ui": {"S": 0, "error": "boom"}}, {})["status"] == "infra-error"
    assert arm_search.outcome({**good, "drive_is_error": True}, {})["status"] == "infra-error"
    limited = arm_search.outcome({**good, "drive_is_error": True}, {"result_subtype": "error_max_budget_usd"})
    assert limited["status"] == "ok"
    assert arm_search.outcome(None, None, "drive")["status"] == "infra-error"
    assert arm_search.outcome({**good, "oracle": False}, {})["false_complete"] == 1


def test_noise_floor_is_the_c_spread_but_never_below_the_preregistered_floor():
    narrow = arm_search.noise_floor([_row("C", cost=20), _row("C", cost=21), _row("C", cost=20)])
    assert narrow["measured_cost"] == pytest.approx(0.05) and narrow["cost"] == 0.14
    wide = arm_search.noise_floor([_row("C", cost=10), _row("C", cost=20), _row("C", cost=20)])
    assert wide["cost"] == pytest.approx(0.5)


def test_revival_is_cumulative_in_the_preregistered_order():
    labels = [item["label"] for item in arm_search.candidates()]
    assert labels == ["S-min", "S-min+tdd", "S-min+tdd+architecture_review",
                      "S-min+tdd+architecture_review+reviewers5", "S-min+tdd+architecture_review+reviewers5+design",
                      "S-min+tdd+architecture_review+reviewers5+design+planning"]
    specs = [item["spec"] for item in arm_search.candidates()]
    assert specs[0] == arm_search.S_MIN
    assert specs[1] == {"planning": False, "design": False, "architecture_review": False, "reviewers": 2}
    assert specs[3] == {"planning": False, "design": False, "reviewers": 5}
    assert specs[-1] == {"reviewers": 5}
    assert [item["axis"] for item in arm_search.candidates()] == ["reduce-all", *arm_search.REVIVAL]


def test_axis_rule_refuses_a_third_change_on_the_same_axis():
    assert arm_search.axis_ok(["tdd"], "tdd")
    assert not arm_search.axis_ok(["design", "tdd", "tdd"], "tdd")
    assert arm_search.axis_ok(["tdd", "design", "tdd"], "tdd")


def test_axis_rule_is_checked_when_planning(monkeypatch):
    smin = arm_search.revived(0)
    monkeypatch.setattr(arm_search, "candidates", lambda: [smin, {**smin, "name": "x", "axis": "reduce-all"},
                                                           {**smin, "name": "y", "axis": "reduce-all"}])
    rows = _opening() + [_row("x")] * 3
    with pytest.raises(ValueError, match="axis rule"):
        arm_search.plan(rows, _order())


def test_pareto_keeps_only_undominated_variants():
    points = [{"variant": "a", "n": 3, "success_rate": 2 / 3, "cost_per_success": 15},
              {"variant": "b", "n": 3, "success_rate": 1 / 3, "cost_per_success": 10},
              {"variant": "c", "n": 3, "success_rate": 1 / 3, "cost_per_success": 30},
              {"variant": "d", "n": 3, "success_rate": 0.0, "cost_per_success": math.inf}]
    assert [p["variant"] for p in arm_search.pareto(points)] == ["a", "b"]


def test_s_star_is_the_confirmed_variant_with_the_lowest_cost_per_success():
    pick = arm_search.choose_s_star([{"name": "a", "spec": {}, "stats": {"cost_per_success": 12}},
                                     {"name": "b", "spec": {"tdd": False}, "stats": {"cost_per_success": 9}}])
    assert pick["name"] == "b"
    assert arm_search.choose_s_star([]) is None


# ---------- order ----------

def test_opening_runs_follow_the_seeded_order_and_reruns_take_the_same_slot():
    order = arm_search.opening_order(7)
    assert sorted(order) == sorted(_order()) and order == arm_search.opening_order(7)
    assert arm_search.plan([], order)["variant"]["name"] == order[0]
    infra = [_row(order[0], status="infra-error")]
    assert arm_search.plan(infra, order)["variant"]["name"] == order[0]
    assert arm_search.plan([_row(order[0])], order)["variant"]["name"] == order[1]


def test_s_min_passing_goes_straight_to_confirmation():
    step = arm_search.plan(_opening(c_ok=0, s_ok=1), _order())
    assert step["kind"] == "run" and step["stage"] == "confirm" and step["variant"]["name"] == SMIN
    assert step["remaining"] == 3


def test_failing_s_min_revives_tdd_then_architecture_review():
    rows = _opening(c_ok=1, s_ok=0)  # S-min below C on successes: inferior
    step = arm_search.plan(rows, _order())
    assert step["variant"]["name"] == TDD and step["stage"] == "search"
    assert step["trail"][0]["passed"] is False
    rows += [_row(TDD, cost=10) for i in range(3)]
    assert arm_search.plan(rows, _order())["variant"]["name"] == ARCH


def test_confirmation_pass_ends_with_that_variant_and_a_failed_one_moves_on():
    rows = _opening(c_ok=0, s_ok=1) + [_row(SMIN, ok=True, cost=8)] + [_row(SMIN, cost=8)] * 2
    done = arm_search.plan(rows, _order())
    assert done["kind"] == "done" and done["s_star"]["name"] == SMIN
    assert [item["step"] for item in done["trail"]] == ["search", "confirm"]
    failed = _opening(c_ok=0, s_ok=1) + [_row(SMIN, cost=8, flags=1)] * 3
    step = arm_search.plan(failed, _order())
    assert step["variant"]["name"] == TDD and step["trail"][-1] == {**step["trail"][-1], "step": "confirm",
                                                                     "passed": False}


def test_no_survivor_ends_without_s_star():
    rows = _opening(c_ok=3, s_ok=0)
    for item in arm_search.candidates()[1:]:
        rows += [_row(item["name"], cost=5)] * 3
    done = arm_search.plan(rows, _order())
    assert done["kind"] == "done" and done["s_star"] is None and len(done["trail"]) == 6


def test_budget_refuses_a_run_whose_expected_cost_would_exceed_the_cap():
    rows = [_row("full-feature", cost=20)] * 3
    assert arm_search.budget_allows(rows, "full-feature", 80) == (True, 20)
    assert arm_search.budget_allows(rows, "full-feature", 79) == (False, 20)
    assert arm_search.budget_allows(rows, SMIN, 84) == (False, 25.0)


def test_infra_stop_needs_two_in_a_row_after_the_last_ack():
    infra, ok, cut = _row("C", status="infra-error"), _row("C"), _row("C", status="harness-error")
    assert not arm_search.infra_stop([infra, ok, infra])
    assert arm_search.infra_stop([ok, infra, cut, infra])
    assert not arm_search.infra_stop([infra, infra], acked=2)


# ---------- end to end on the stub ----------

def _setup(tmp_path: Path, scenario: dict, monkeypatch) -> tuple[Path, Path]:
    fake = tmp_path / "fake_arm_compare.py"
    fake.write_text(FAKE, encoding="utf-8")
    (tmp_path / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
    monkeypatch.setenv("SCENARIO", str(tmp_path / "scenario.json"))
    monkeypatch.setenv("EVALS", str(ROOT / "evals"))
    return fake, tmp_path / "out"


def _run(fake: Path, out: Path, *extra: str, budget: float = 250) -> int:
    return arm_search.main(["run", "--kit", str(ROOT), "--case", "oyg", "--out", str(out), "--budget-usd",
                            str(budget), "--seed", "1", "--arm-compare", str(fake), *extra])


def _calls(tmp_path: Path) -> list[str]:
    return (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()


def test_a_search_ends_with_s_star_and_a_hashed_ledger(tmp_path, monkeypatch, capsys):
    scenario = {"full-feature": [{"ok": False, "cost": 20}], SMIN: [{"ok": False, "cost": 8}],
                TDD: [{"ok": True, "cost": 9}, {"ok": False, "declaration": "declared-blocked", "cost": 9}]}
    fake, out = _setup(tmp_path, scenario, monkeypatch)
    assert _run(fake, out) == 0
    rows = arm_search.read_ledger(out)
    assert [row["label"] for row in rows[6:]] == ["S-min+tdd"] * 6
    assert {row["stage"] for row in rows[9:]} == {"confirm"}
    assert all(len(row["result_sha256"]) == 64 and row["status"] == "ok" for row in rows)
    assert all(row["hypothesis"] == arm_search.HYPOTHESES[row["axis"]] for row in rows)
    star = json.loads((out / "s_star.json").read_text())
    assert star["name"] == TDD and len(star["evidence"]["runs"]) == 6 and len(star["evidence"]["baseline_runs"]) == 3
    assert "S-min" in (out / "insights.md").read_text()
    prechecked = sorted(p.name for p in (out / "precheck").glob("*.json"))
    assert prechecked == sorted(f"{name}.json" for name in ("full-feature", SMIN, TDD))
    calls = _calls(tmp_path)
    assert sum(call.startswith("drive") for call in calls) == 12
    capsys.readouterr()
    arm_search.main(["report", str(out)])
    report = capsys.readouterr().out
    assert "done: S* = S-min+tdd" in report and "| * |" in report


def test_budget_cap_stops_before_a_run_would_exceed_it(tmp_path, monkeypatch):
    fake, out = _setup(tmp_path, {"*": [{"ok": False, "cost": 20}]}, monkeypatch)
    assert _run(fake, out, budget=70) == 2
    rows = arm_search.read_ledger(out)
    assert len(rows) == 3  # 0+25, 20+(20|25), 40+(20|25) fit; 60+20 does not
    assert json.loads((out / "stop.json").read_text())["reason"] == "budget"
    assert arm_search.spent(rows) + arm_search.expected_cost(rows, arm_search.plan(rows, (out / "order.txt")
                                                             .read_text().split())["variant"]["name"]) > 70


def test_two_infra_errors_in_a_row_stop_and_ack_resumes(tmp_path, monkeypatch):
    fake, out = _setup(tmp_path, {"*": [{"fail": "drive"}, {"harness_error": "evaluator crashed"},
                                        {"ok": False, "cost": 5}]}, monkeypatch)
    assert _run(fake, out) == 3
    rows = arm_search.read_ledger(out)
    assert [row["status"] for row in rows] == ["infra-error", "infra-error"]
    assert json.loads((out / "stop.json").read_text())["reason"] == "infra-errors"
    assert _run(fake, out) == 3  # not acknowledged: still stopped, nothing new run
    assert len(arm_search.read_ledger(out)) == 2
    assert _run(fake, out, "--ack-infra", "--max-runs", "1") == 0
    assert arm_search.read_ledger(out)[-1]["status"] == "ok"


def test_resume_skips_finished_steps_and_restarts_an_interrupted_drive(tmp_path, monkeypatch):
    fake, out = _setup(tmp_path, {"*": [{"ok": False, "cost": 5}]}, monkeypatch)
    assert _run(fake, out, "--max-runs", "1") == 0
    order = (out / "order.txt").read_text().split()
    second = arm_search.baseline() if order[1] == "full-feature" else arm_search.revived(0)
    third = arm_search.baseline() if order[2] == "full-feature" else arm_search.revived(0)
    driver = arm_search.Driver(arm_search.argparse.Namespace(kit=ROOT, arm_compare=fake, case="oyg", case_dir=None,
                                                             evaluator=None), out)

    def started(run_id: str, variant: dict, steps: dict, done: tuple[str, ...]) -> Path:
        run_dir = out / "runs" / f"{run_id}-{variant['label']}"
        if "prepare" in done:
            driver.call("prepare", *driver.prepare_argv(variant, 9, run_dir), log=out / "x.log")
        if "drive" in done:
            driver.call("drive", str(run_dir), log=out / "x.log")
        arm_search._write_json(run_dir.with_name(run_dir.name + ".json"),
                               {"run": run_id, "stage": "opening", "variant": variant, "trial": 9, "steps": steps})
        return run_dir

    started("r002", second, {"prepare": "done", "drive": "started"}, ("prepare",))
    (tmp_path / "calls.log").write_text("")
    assert _run(fake, out, "--max-runs", "1") == 0
    rows = arm_search.read_ledger(out)
    assert [row["status"] for row in rows] == ["ok", "harness-error", "ok"]
    assert rows[2]["variant"] == second["name"]  # the cut-off slot is run again from scratch
    assert any(call.startswith("prepare") and "r003" in call for call in _calls(tmp_path))

    started("r004", third, {"prepare": "done", "drive": "done", "score": "started"}, ("prepare", "drive"))
    (tmp_path / "calls.log").write_text("")
    assert _run(fake, out, "--max-runs", "1") == 0
    rows = arm_search.read_ledger(out)
    assert rows[3]["run"] == "r004" and rows[3]["status"] == "ok"
    calls = _calls(tmp_path)
    assert calls[0].startswith("score") and "r004" in calls[0]  # only the cut-off score ran again
    assert not any(call.startswith(("prepare", "drive")) and "r004" in call for call in calls)


@pytest.mark.parametrize("budget, first_exit", [(250, 0), (239, 4)])
def test_checkpoint_every_20_runs_stops_only_when_the_budget_would_be_exceeded(tmp_path, monkeypatch, budget,
                                                                               first_exit):
    # C always succeeds, so nothing beats it: 6 opening + 5 revivals x 3 = 21 runs at $10
    scenario = {"full-feature": [{"ok": True, "cost": 10}], "*": [{"ok": False, "cost": 10}]}
    fake, out = _setup(tmp_path, scenario, monkeypatch)
    assert _run(fake, out, budget=budget) == first_exit
    assert (out / "checkpoint-20.md").is_file()
    if first_exit:
        assert len(arm_search.read_ledger(out)) == 20  # spent $200 + 4 more runs projected > $239
        assert json.loads((out / "stop.json").read_text())["reason"] == "checkpoint-review"
        assert _run(fake, out, budget=budget) == 0  # reviewed: the next run still fits the cap
    assert len(arm_search.read_ledger(out)) == 21
    assert json.loads((out / "s_star.json").read_text())["menu"] == "removed"


def test_config_mismatch_is_refused_on_resume(tmp_path, monkeypatch):
    fake, out = _setup(tmp_path, {"*": [{"ok": False, "cost": 5}]}, monkeypatch)
    assert _run(fake, out, "--max-runs", "1") == 0
    with pytest.raises(SystemExit):
        _run(fake, out, "--max-runs", "1", budget=300)


def test_a_ui_case_without_its_evaluator_stops_before_any_run(tmp_path, monkeypatch):
    fake, out = _setup(tmp_path, {"*": [{"ok": False, "cost": 5}]}, monkeypatch)
    monkeypatch.setenv("KIND", "ui")
    assert _run(fake, out) == 5
    assert arm_search.read_ledger(out) == []
    assert "--evaluator" in json.loads((out / "stop.json").read_text())["detail"]
