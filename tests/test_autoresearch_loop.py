"""autoresearch 루프의 유지/되돌림 판정과 편집 범위. 틀리면 루프가 회귀나 범위 밖 편집을 남긴다."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("autoresearch_loop", REPO / "tools" / "autoresearch" / "loop.py")
assert _SPEC is not None and _SPEC.loader is not None
loop = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(loop)


def _routing(passing: dict[str, bool]) -> dict:
    return {"score": (sum(passing.values()),), "passing": passing}


def test_routing_keeps_a_higher_score_and_rejects_any_lost_case():
    best = _routing({"a": True, "b": False, "c": False})
    assert loop.better(_routing({"a": True, "b": True, "c": False}), best, "routing", 10, 10) == (True, "improved")
    keep, reason = loop.better(_routing({"a": False, "b": True, "c": True}), best, "routing", 10, 10)
    assert not keep and "regressed ['a']" in reason


def test_routing_keeps_an_equal_score_only_when_shorter():
    best = _routing({"a": True, "b": False})
    assert loop.better(_routing({"a": True, "b": False}), best, "routing", 9, 10) == (True, "same score, shorter")
    assert loop.better(_routing({"a": True, "b": False}), best, "routing", 10, 10) == (False, "no improvement")


def test_model_track_ignores_token_only_gains_and_read_rate_drops():
    best = {"score": (2, 3, -1000), "read_rate": 1.0}
    assert loop.better({"score": (2, 3, -10), "read_rate": 1.0}, best, "model", 10, 10)[0] is False
    assert loop.better({"score": (3, 3, -2000), "read_rate": 1.0}, best, "model", 10, 10) == (True, "improved")
    assert loop.better({"score": (3, 3, -1), "read_rate": 0.9}, best, "model", 10, 10) == (
        False, "required-read rate dropped")


def test_model_track_never_keeps_a_deletion_for_being_shorter():
    best = {"score": (3, 3, -1000), "read_rate": 1.0}
    assert loop.better({"score": (3, 3, -900), "read_rate": 1.0}, best, "model", 5, 10) == (
        False, "no improvement")


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "kit"
    (root / "skills" / "demo").mkdir(parents=True)
    (root / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: d\ntaskTerms: [alpha]\n---\n\nBody.\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed"], cwd=root, check=True)
    return root


def test_routing_surface_accepts_only_vocabulary_lines(tmp_path):
    root = _repo(tmp_path)
    skill = root / "skills" / "demo" / "SKILL.md"
    skill.write_text(skill.read_text().replace("[alpha]", "[alpha, beta]"), encoding="utf-8")
    assert loop.surface_violations(root, "routing", []) == []
    skill.write_text(skill.read_text().replace("Body.", "Body changed."), encoding="utf-8")
    assert any("non-vocabulary line" in problem for problem in loop.surface_violations(root, "routing", []))
    (root / "README.md").write_text("x", encoding="utf-8")
    assert any("README.md: outside" in problem for problem in loop.surface_violations(root, "routing", []))


def test_model_surface_freezes_frontmatter_and_other_skills(tmp_path):
    root = _repo(tmp_path)
    skill = root / "skills" / "demo" / "SKILL.md"
    skill.write_text(skill.read_text().replace("Body.", "Read the slice plan first."), encoding="utf-8")
    assert loop.surface_violations(root, "model", ["demo"]) == []
    assert any("outside the model surface" in p for p in loop.surface_violations(root, "model", ["other"]))
    skill.write_text(skill.read_text().replace("description: d", "description: e"), encoding="utf-8")
    assert any("frontmatter is frozen" in p for p in loop.surface_violations(root, "model", ["demo"]))


def _set_terms(root: Path, terms: str) -> None:
    skill = root / "skills" / "demo" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    skill.write_text(text.replace(text.split("taskTerms: ")[1].split("\n")[0], terms), encoding="utf-8")


def _commit(root: Path, message: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message], cwd=root, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


def test_routing_key_ignores_term_order_quotes_and_repeats_only(tmp_path):
    root = _repo(tmp_path)
    _set_terms(root, "[alpha, beta]")
    key = loop.surface_key(root, "routing", [])
    _set_terms(root, "['beta',alpha, beta]")
    assert loop.surface_key(root, "routing", []) == key
    _set_terms(root, "[alpha]")
    assert loop.surface_key(root, "routing", []) != key
    # resolver는 term을 그대로 비교한다: 대소문자나 안쪽 공백이 다르면 다른 상태다.
    _set_terms(root, "[Alpha, beta]")
    assert loop.surface_key(root, "routing", []) != key
    _set_terms(root, '[" alpha ", beta]')
    assert loop.surface_key(root, "routing", []) != key


def test_model_key_is_byte_exact(tmp_path):
    root = _repo(tmp_path)
    key = loop.surface_key(root, "model", ["demo"])
    skill = root / "skills" / "demo" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8").replace("Body.", "Body. "), encoding="utf-8")
    assert loop.surface_key(root, "model", ["demo"]) != key


def _fake_routing():
    calls = []

    def evaluate(tree, work, args):
        text = (tree / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8")
        passing = {"a": "beta" in text.lower()}
        calls.append(text)
        return {"score": (sum(passing.values()),), "dev": sum(passing.values()), "dev_total": 1,
                "heldout": 0, "heldout_total": 0, "passing": passing, "feedback": ""}
    return calls, evaluate


def test_a_recreated_state_is_never_re_evaluated(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    calls, evaluate = _fake_routing()
    edits = iter(["[alpha, beta]", "[alpha]", "[beta, alpha, alpha]"])

    def propose(tree, args, state):
        _set_terms(tree, next(edits))
        return "edit", 0.0

    monkeypatch.setattr(loop, "prepare_worktree", lambda base, tag: (base / tag).mkdir(parents=True) or root)
    monkeypatch.setattr(loop, "evaluate", evaluate)
    monkeypatch.setattr(loop, "propose", propose)
    monkeypatch.setattr(loop, "guards", lambda tree: [])

    assert loop.main(["--track", "routing", "--tag", "t", "--root", str(tmp_path / "work"),
                      "--iterations", "3", "--budget-usd", "10"]) == 0

    assert len(calls) == 2  # baseline와 첫 편집만 잰다
    rows = [line.split("\t") for line in (tmp_path / "work" / "t" / "results.tsv").read_text().splitlines()[1:]]
    assert [row[2] for row in rows] == ["keep", "keep", "duplicate", "invalid"]
    assert rows[2][6] == "duplicate of #baseline"
    assert rows[3][6].startswith("no-op")


def test_restart_resets_to_the_last_keep_and_keeps_the_spend(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    kept = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()
    _set_terms(root, "[alpha, beta]")
    _commit(root, "unjudged candidate")  # 평가 전에 죽은 회차가 남긴 커밋
    work = tmp_path / "work" / "t"
    work.mkdir(parents=True)
    (work / "results.tsv").write_text(
        loop.TSV_HEADER
        + f"baseline\t{kept[:12]}\tkeep\t(0,)\t0\t0\tbytes=1\t3.0000\tbaseline\n"
        + f"1\t{kept[:12]}\tdiscard\t(0,)\t0\t0\tno improvement; bytes=1\t2.5000\tx\n", encoding="utf-8")
    calls, evaluate = _fake_routing()
    proposed = []
    monkeypatch.setattr(loop, "prepare_worktree", lambda base, tag: root)
    monkeypatch.setattr(loop, "evaluate", evaluate)
    monkeypatch.setattr(loop, "propose", lambda *a: proposed.append(a) or ("edit", 0.0))

    assert loop.main(["--track", "routing", "--tag", "t", "--root", str(tmp_path / "work"),
                      "--iterations", "3", "--budget-usd", "5"]) == 0

    # 지난 실행이 쓴 $5.5가 예산을 이미 넘었으므로 다시 재지도 제안하지도 않는다.
    assert proposed == [] and calls == []
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True)
    assert head.stdout.strip() == kept

    assert loop.main(["--track", "routing", "--tag", "t", "--root", str(tmp_path / "work"),
                      "--iterations", "1", "--budget-usd", "50"]) == 0
    assert "beta" not in calls[0]  # 재개 측정은 평가되지 않은 후보가 아니라 마지막 keep을 잰다
    rows = [line.split("\t") for line in (work / "results.tsv").read_text().splitlines()[1:]]
    assert rows[2][:3] == ["resume", kept[:12], "keep"]
    assert rows[3][0] == "2"


def test_model_score_with_an_invalid_run_is_an_evaluation_error(tmp_path, monkeypatch):
    rows = [{"oracle": {"behavior": True, "plan": True, "norm": True}, "cost_usd": 1.0},
            {"oracle": {"behavior": True, "plan": True, "norm": True}, "cost_usd": 1.0},
            {"error": "timeout", "cost_usd": 0.5}]

    def run(argv, cwd, **kwargs):
        output = Path(argv[argv.index("--output") + 1])
        output.mkdir(parents=True)
        (output / "results.jsonl").write_text("".join(__import__("json").dumps(r) + "\n" for r in rows))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(loop, "run", run)
    args = loop.argparse.Namespace(stacks="commerce", modes="clean", trials=3, model=None, concurrency=1)
    outcome = loop.score_model(tmp_path, tmp_path, args)
    assert "error" in outcome and outcome["cost"] == 2.5
    assert loop.better(outcome, {"score": (1, 1, 0), "dev_total": 3}, "model", 1, 1) == (False, "evaluation error")


def test_model_track_rejects_a_changed_run_count():
    best = {"score": (2, 3, 0), "dev_total": 3, "read_rate": 1.0}
    assert loop.better({"score": (3, 3, 0), "dev_total": 4, "read_rate": 1.0}, best, "model", 1, 1)[0] is False


def test_failed_proposer_edit_is_not_scored(tmp_path, monkeypatch):
    payload = '{"is_error": true, "subtype": "error_max_budget_usd", "total_cost_usd": 1.2}'
    monkeypatch.setattr(loop, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, payload, ""))
    args = loop.argparse.Namespace(proposer_budget_usd=1.0, proposer_model=None)
    description, cost = loop.propose(tmp_path, args, "state")
    assert description.startswith(loop.PROPOSER_FAILED) and cost == 1.2


def test_proposer_sees_why_an_attempt_was_rejected(tmp_path):
    results = tmp_path / "results.tsv"
    results.write_text(loop.TSV_HEADER + "3\tabc\tinvalid\t\t\t\tREADME.md: outside the routing surface\t0.1000\tx\n")
    args = loop.argparse.Namespace(track="routing", history=12)
    state = loop.state_text(args, {"dev": 1, "dev_total": 2, "feedback": ""}, results)
    assert "- 3 invalid (README.md: outside the routing surface): x" in state


def test_a_state_whose_evaluation_failed_can_be_scored_again(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    outcomes = iter([
        {"score": (0,), "dev": 0, "dev_total": 1, "heldout": 0, "heldout_total": 0, "passing": {"a": False},
         "feedback": ""},
        {"error": "worker died"},
        {"score": (1,), "dev": 1, "dev_total": 1, "heldout": 0, "heldout_total": 0, "passing": {"a": True},
         "feedback": ""},
    ])
    edits = iter(["[alpha, beta]", "[alpha, beta]"])

    def propose(tree, args, state):
        _set_terms(tree, next(edits))
        return "edit", 0.0

    monkeypatch.setattr(loop, "prepare_worktree", lambda base, tag: (base / tag).mkdir(parents=True) or root)
    monkeypatch.setattr(loop, "evaluate", lambda *a: next(outcomes))
    monkeypatch.setattr(loop, "propose", propose)
    monkeypatch.setattr(loop, "guards", lambda tree: [])

    loop.main(["--track", "routing", "--tag", "t", "--root", str(tmp_path / "work"), "--iterations", "2",
               "--budget-usd", "10"])

    rows = [line.split("\t") for line in (tmp_path / "work" / "t" / "results.tsv").read_text().splitlines()[1:]]
    assert [row[2] for row in rows] == ["keep", "discard", "keep"]


def test_routing_key_keeps_a_quoted_phrase_with_a_comma_apart(tmp_path):
    root = _repo(tmp_path)
    _set_terms(root, '["alpha, beta"]')
    phrase = loop.surface_key(root, "routing", [])
    _set_terms(root, '["alpha", "beta"]')
    assert loop.surface_key(root, "routing", []) != phrase


def test_a_failed_baseline_records_its_cost_for_the_next_restart(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setattr(loop, "prepare_worktree", lambda base, tag: (base / tag).mkdir(parents=True, exist_ok=True)
                        or root)
    monkeypatch.setattr(loop, "evaluate", lambda *a: {"error": "1/3 invalid runs", "cost": 4.0})
    argv = ["--track", "routing", "--tag", "t", "--root", str(tmp_path / "work"), "--budget-usd", "5"]

    assert loop.main(argv) == 1
    rows = [line.split("\t") for line in (tmp_path / "work" / "t" / "results.tsv").read_text().splitlines()[1:]]
    assert rows[0][2] == "error" and rows[0][7] == "4.0000"
    assert loop.resume(root, tmp_path / "work" / "t" / "results.tsv") == (4.0, 1, {}, None, None)


def test_a_score_from_other_measurement_conditions_is_never_kept():
    best = {"score": (1, 3, 0), "dev_total": 3, "read_rate": None, "conditions": "a" * 16}
    candidate = {"score": (3, 3, 0), "dev_total": 3, "read_rate": None, "conditions": "b" * 16}
    assert loop.better(candidate, best, "model", 1, 1) == (False, loop.CONDITIONS_CHANGED)


def _model(dev: int, total: int = 3, cond: str = "c" * 16) -> dict:
    return {"score": (dev, dev, 0), "dev": dev, "dev_total": total, "heldout": "-", "heldout_total": "-",
            "read_rate": None, "feedback": "", "conditions": cond}


def _body(tree: Path, text: str) -> None:
    skill = tree / "skills" / "demo" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8").split("\n\n")[0] + f"\n\n{text}\n", encoding="utf-8")


def _model_loop(tmp_path, monkeypatch, root, outcomes, edits, conditions="c" * 16):
    monkeypatch.setattr(loop, "prepare_worktree", lambda base, tag: (base / tag).mkdir(parents=True, exist_ok=True)
                        or root)
    monkeypatch.setattr(loop, "conditions", lambda args: conditions)
    monkeypatch.setattr(loop, "evaluate", lambda *a: next(outcomes))
    monkeypatch.setattr(loop, "guards", lambda tree: [])

    def propose(tree, args, state):
        _body(tree, next(edits))
        return "edit", 0.0
    monkeypatch.setattr(loop, "propose", propose)
    return ["--track", "model", "--skills", "demo", "--tag", "t", "--root", str(tmp_path / "work"),
            "--budget-usd", "10"]


def _rows(tmp_path: Path) -> list[list[str]]:
    return [line.split("\t") for line in (tmp_path / "work" / "t" / "results.tsv").read_text().splitlines()[1:]]


def test_a_measurement_with_a_different_run_count_leaves_the_state_scoreable(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    argv = _model_loop(tmp_path, monkeypatch, root, iter([_model(1), _model(2, total=2), _model(2), _model(2)]),
                       iter(["Read the plan.", "Read the plan."]))
    loop.main([*argv, "--iterations", "2"])
    assert [row[2] for row in _rows(tmp_path)] == ["keep", "discard", "keep"]


def test_restart_does_not_lower_the_bar_to_an_unlucky_re_measurement(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    kept = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()
    work = tmp_path / "work" / "t"
    work.mkdir(parents=True)
    (work / "results.tsv").write_text(
        loop.TSV_HEADER + f"baseline\t{kept[:12]}\tkeep\t(3, 3, 0)\t3\t-\tbytes=1; cond={'c' * 16}\t1.0000\tbaseline\n",
        encoding="utf-8")
    argv = _model_loop(tmp_path, monkeypatch, root, iter([_model(1), _model(2)]), iter(["Read the plan."]))
    loop.main([*argv, "--iterations", "1"])
    rows = _rows(tmp_path)
    assert rows[1][0] == "resume" and "floor=recorded" in rows[1][6]
    assert rows[2][2] == "discard"  # 2/3은 기록된 3/3을 넘지 못한다


def test_restart_under_new_conditions_forgets_scores_and_fingerprints(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    kept = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()
    _body(root, "Read the plan.")
    key = loop.surface_key(root, "model", ["demo"])
    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=root, check=True)
    work = tmp_path / "work" / "t"
    work.mkdir(parents=True)
    (work / "results.tsv").write_text(
        loop.TSV_HEADER
        + f"baseline\t{kept[:12]}\tkeep\t(3, 3, 0)\t3\t-\tbytes=1; cond={'c' * 16}\t1.0000\tbaseline\n"
        + f"1\t{kept[:12]}\tdiscard\t(1, 1, 0)\t1\t-\tno improvement; key={key}; cond={'c' * 16}\t1.0000\tx\n",
        encoding="utf-8")
    argv = _model_loop(tmp_path, monkeypatch, root, iter([_model(1, cond="d" * 16), _model(2, cond="d" * 16), _model(2, cond="d" * 16)]),
                       iter(["Read the plan."]), conditions="d" * 16)
    loop.main([*argv, "--iterations", "1"])
    rows = _rows(tmp_path)
    assert "floor" not in rows[2][6]
    assert rows[3][2] == "keep"  # 예전 조건에서 잰 같은 상태도 새 조건에서는 다시 잰다


def test_a_rejected_attempt_is_recorded_before_the_reset(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    argv = _model_loop(tmp_path, monkeypatch, root, iter([_model(1)]), iter([]))

    def outside(tree, args, state):
        (tree / "README.md").write_text("x", encoding="utf-8")
        return "edit", 0.25
    monkeypatch.setattr(loop, "propose", outside)
    real_git = loop.git

    def git(cwd, *args):
        if args[:1] == ("reset",):
            raise RuntimeError("reset failed")
        return real_git(cwd, *args)
    monkeypatch.setattr(loop, "git", git)
    try:
        loop.main([*argv, "--iterations", "1"])
    except RuntimeError:
        pass
    assert _rows(tmp_path)[-1][2] == "invalid" and _rows(tmp_path)[-1][7] == "0.2500"


def test_a_proposal_that_exhausts_the_budget_is_not_scored(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    outcomes = iter([_model(1)])
    argv = _model_loop(tmp_path, monkeypatch, root, outcomes, iter([]))

    def costly(tree, args, state):
        _body(tree, "Read the plan.")
        return "edit", 20.0
    monkeypatch.setattr(loop, "propose", costly)
    loop.main([*argv, "--iterations", "1"])
    assert _rows(tmp_path)[-1][6] == "budget exhausted before scoring"


# --------------------------------------------------------------------------- #420 subtract track and guards

def test_forbidden_paths_are_invalid_in_every_track(tmp_path):
    root = _repo(tmp_path)
    for relative in ("scripts/hooks/guard.sh", "src/agent_flow/runner.py", "src/agent_flow/workflows/full-feature.yaml",
                     "evals/phase_eval.py", "src/agent_flow/core/command_evidence.py"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
    for track in ("routing", "model", "subtract"):
        problems = loop.surface_violations(root, track, ["demo"])
        assert len(problems) == 5 and all("forbidden path" in problem for problem in problems)


def _subtract_repo(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    skill = root / "skills" / "demo" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "Line two.\nLine three.\n", encoding="utf-8")
    _commit(root, "body")
    return root


def test_subtract_surface_accepts_only_deleted_lines(tmp_path):
    root = _subtract_repo(tmp_path)
    skill = root / "skills" / "demo" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8").replace("Line two.\n", ""), encoding="utf-8")
    assert loop.surface_violations(root, "subtract", ["demo"]) == []

    skill.write_text(skill.read_text(encoding="utf-8").replace("Line three.", "Line 3."), encoding="utf-8")
    assert any("added text" in problem for problem in loop.surface_violations(root, "subtract", ["demo"]))

    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=root, check=True)
    (root / "skills" / "demo" / "references").mkdir()
    (root / "skills" / "demo" / "references" / "new.md").write_text("new\n", encoding="utf-8")
    assert any("new file" in problem for problem in loop.surface_violations(root, "subtract", ["demo"]))


def _scored(passed: int, total: int = 10, overclaims: int = 0, read_rate: float = 1.0) -> dict:
    return {"score": (passed, passed, -1000), "dev": passed, "dev_total": total, "overclaims": overclaims,
            "read_rate": read_rate, "conditions": "c" * 16}


def test_subtract_keeps_a_smaller_surface_only_within_the_margin():
    best, margins = _scored(9), {"pass_rate_drop": 0.1}
    assert loop.better(_scored(8), best, "subtract", 90, 100, margins) == (True, "non-inferior, smaller")
    keep, reason = loop.better(_scored(7), best, "subtract", 90, 100, margins)
    assert not keep and reason.startswith("inferior")
    assert loop.better(_scored(9), best, "subtract", 100, 100, margins) == (False, "not smaller")
    assert loop.better(_scored(9), best, "subtract", 90, 100, None) == (False, "non-inferiority margins are not fixed")


def test_overclaims_rising_blocks_a_keep_in_model_and_subtract():
    best = _scored(5)
    assert loop.better(_scored(6, overclaims=1), best, "model", 10, 10) == (False, "overclaims rose")
    assert loop.better(_scored(5, overclaims=1), best, "subtract", 9, 10, {"pass_rate_drop": 0.1}) == (
        False, "overclaims rose")


def test_routing_held_out_drop_vetoes_a_dev_gain():
    best = {**_routing({"a": True, "b": False}), "heldout": 10}
    keep, reason = loop.better({**_routing({"a": True, "b": True}), "heldout": 9}, best, "routing", 10, 10)
    assert not keep and reason.startswith("held-out dropped")


def test_unset_margins_keep_the_subtract_track_from_starting(tmp_path):
    unset = tmp_path / "n.json"
    unset.write_text('{"margins": {"pass_rate_drop": null}}', encoding="utf-8")
    assert loop.load_margins(unset) is None
    unset.write_text('{"margins": {"pass_rate_drop": 0.05}}', encoding="utf-8")
    assert loop.load_margins(unset) == {"pass_rate_drop": 0.05}
    assert loop.load_margins() is None  # 저장소의 기본값은 아직 정해지지 않았다.


def test_a_keep_that_the_confirmation_measurement_does_not_repeat_is_discarded(tmp_path, monkeypatch):
    """반증: 한 번 우연히 이긴 측정이 그대로 keep됐다. 두 번째 측정이 같은 판정을 내야 keep이다."""
    root = _repo(tmp_path)
    argv = _model_loop(tmp_path, monkeypatch, root, iter([_model(1), _model(2), _model(1)]), iter(["Read the plan."]))
    assert loop.main([*argv, "--iterations", "1", "--confirm"]) == 0
    rows = _rows(tmp_path)
    assert rows[-1][2] == "discard" and rows[-1][6].startswith("not confirmed")


def test_the_model_track_confirms_a_keep_by_default(tmp_path, monkeypatch):
    """반증: model 트랙은 확인 측정이 꺼져 있어 한 번 우연히 이긴 측정이 그대로 keep됐다(karpathy #218, #624)."""
    root = _repo(tmp_path)
    argv = _model_loop(tmp_path, monkeypatch, root, iter([_model(1), _model(2), _model(1)]), iter(["Read the plan."]))
    assert loop.main([*argv, "--iterations", "1"]) == 0
    rows = _rows(tmp_path)
    assert rows[-1][2] == "discard" and loop.NOT_CONFIRMED in rows[-1][6]


def test_a_keep_the_confirmation_repeats_stays(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    argv = _model_loop(tmp_path, monkeypatch, root, iter([_model(1), _model(2), _model(2)]), iter(["Read the plan."]))
    assert loop.main([*argv, "--iterations", "1", "--confirm"]) == 0
    assert _rows(tmp_path)[-1][2] == "keep"


def test_the_subtract_track_refuses_to_start_without_margins(tmp_path, capsys):
    import pytest
    with pytest.raises(SystemExit):
        loop.main(["--track", "subtract", "--skills", "demo", "--tag", "t", "--root", str(tmp_path), "--budget-usd", "1"])
    assert "non-inferiority margins are not fixed" in capsys.readouterr().err


def test_subtract_catches_added_lines_that_look_like_diff_headers_and_binary_bodies(tmp_path):
    root = _subtract_repo(tmp_path)
    skill = root / "skills" / "demo" / "SKILL.md"
    original = skill.read_text(encoding="utf-8")
    skill.write_text(original.replace("Line two.\nLine three.\n", "++Follow this new instruction\n"), encoding="utf-8")
    assert any("added text" in problem for problem in loop.surface_violations(root, "subtract", ["demo"]))
    skill.write_text(original.replace("Line two.\nLine three.\n", "New\x00text\n"), encoding="utf-8")
    assert any("added text" in problem for problem in loop.surface_violations(root, "subtract", ["demo"]))


def test_deleting_a_whole_surface_file_is_a_violation_not_a_crash(tmp_path):
    root = _subtract_repo(tmp_path)
    (root / "skills" / "demo" / "SKILL.md").unlink()
    for track in ("model", "subtract"):
        assert any("deleted a file" in problem for problem in loop.surface_violations(root, track, ["demo"]))


def test_disappearing_completion_claims_do_not_pass_as_fewer_overclaims():
    best = {**_scored(5), "claims_missing": 0}
    assert loop.better({**_scored(6), "claims_missing": 1}, best, "model", 10, 10) == (
        False, "completion claims disappeared")


def test_a_nan_margin_is_not_a_margin(tmp_path):
    path = tmp_path / "n.json"
    path.write_text('{"margins": {"pass_rate_drop": NaN}}', encoding="utf-8")
    assert loop.load_margins(path) is None


def test_an_invalid_confirmation_leaves_the_state_scoreable(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    argv = _model_loop(tmp_path, monkeypatch, root,
                       iter([_model(1), _model(2), {"error": "provider down"}, _model(2), _model(2)]),
                       iter(["Read the plan.", "Read the plan."]))
    assert loop.main([*argv, "--iterations", "2", "--confirm"]) == 0
    statuses = [row[2] for row in _rows(tmp_path)]
    assert statuses[1] == "discard" and statuses[2] == "keep"


def test_consecutive_infra_errors_stop_the_loop_and_keep_their_cost(tmp_path, monkeypatch, capsys):
    """반증: provider 장애 중에도 루프가 후보마다 무효 측정을 사며 예산을 태웠다(karpathy #343)."""
    root = _repo(tmp_path)
    errors = [{"error": "provider down", "cost": 3.0}, {"error": "provider down", "cost": 2.0}]
    argv = _model_loop(tmp_path, monkeypatch, root, iter([_model(1), *errors]),
                       iter(["Read the plan.", "Read the slice plan.", "Unused."]))
    assert loop.main([*argv, "--iterations", "3", "--budget-usd", "100"]) == 0
    rows = _rows(tmp_path)
    assert [row[0] for row in rows] == ["baseline", "1", "2"]
    assert [row[7] for row in rows[1:]] == ["3.0000", "2.0000"]
    assert "stopping: infra" in capsys.readouterr().out


def test_a_valid_measurement_resets_the_infra_error_count(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    outcomes = iter([_model(1), {"error": "provider down"}, _model(1), {"error": "provider down"}, _model(1)])
    argv = _model_loop(tmp_path, monkeypatch, root, outcomes,
                       iter(["Read the plan.", "Read the slice plan.", "Check the plan.", "Apply the plan."]))
    assert loop.main([*argv, "--iterations", "4", "--budget-usd", "100"]) == 0
    assert [row[2] for row in _rows(tmp_path)] == ["keep", "discard", "discard", "discard", "discard"]


def test_recorded_scores_name_the_results_file_that_produced_them(tmp_path, monkeypatch):
    """반증: TSV의 점수가 어떤 run 기록에서 나왔는지 대조할 수 없었다(karpathy #623)."""
    import hashlib
    import json
    root = _repo(tmp_path)
    passes = iter([1, 2])
    real_run = loop.run

    def run(argv, cwd, **kwargs):
        if "--output" not in argv:
            return real_run(argv, cwd, **kwargs)
        output = Path(argv[argv.index("--output") + 1])
        output.mkdir(parents=True)
        ok = next(passes)
        rows = [{"oracle": {"behavior": i < ok, "plan": True, "norm": True}, "cost_usd": 1.0} for i in range(3)]
        (output / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        return subprocess.CompletedProcess(argv, 0, "", "")

    argv = _model_loop(tmp_path, monkeypatch, root, iter([]), iter(["Read the plan."]))
    monkeypatch.setattr(loop, "evaluate", lambda tree, work, args: {**loop.score_model(tree, work, args),
                                                                    "conditions": "c" * 16})
    monkeypatch.setattr(loop, "run", run)
    loop.main([*argv, "--iterations", "1", "--no-confirm"])
    rows = _rows(tmp_path)
    assert [row[2] for row in rows] == ["keep", "keep"]
    for row in rows:
        fields = dict(part.split("=", 1) for part in row[6].split("; ") if "=" in part)
        digest = hashlib.sha256((Path(fields["evidence"]) / "results.jsonl").read_bytes()).hexdigest()[:16]
        assert fields["ev_sha"] == digest
    assert rows[0][6] != rows[1][6]


def test_subtract_rejects_a_smaller_surface_that_costs_more_tokens():
    """반증: 표면은 줄었지만 author가 더 헤매 uncached 토큰이 늘어난 삭제가 "더 싸다"며 keep됐다."""
    best, margins = _scored(9), {"pass_rate_drop": 0.1, "tokens_rise": 0.0}
    costlier = {**_scored(9), "score": (9, 9, -1001)}
    keep, reason = loop.better(costlier, best, "subtract", 90, 100, margins)
    assert not keep and reason.startswith("uncached tokens rose")
    cheaper = {**_scored(9), "score": (9, 9, -900)}
    assert loop.better(cheaper, best, "subtract", 90, 100, margins) == (True, "non-inferior, smaller")
    assert loop.better(costlier, best, "subtract", 90, 100, {**margins, "tokens_rise": 0.01}) == (
        True, "non-inferior, smaller")



def test_a_model_keep_must_beat_the_measured_noise_floor():
    best = {**_scored(5), "noise": 1}
    assert loop.better(_scored(6), best, "model", 10, 10) == (False, "no improvement")
    assert loop.better(_scored(7), best, "model", 10, 10) == (True, "improved")
    assert loop.better(_scored(6), _scored(5), "model", 10, 10) == (True, "improved")


def test_resume_restores_the_noise_floor_of_the_last_keep(tmp_path):
    root = _repo(tmp_path)
    kept = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()
    results = tmp_path / "results.tsv"
    results.write_text(
        loop.TSV_HEADER
        + f"baseline\t{kept[:12]}\tkeep\t(3, 3, 0)\t3\t-\tbytes=1; cond={'c' * 16}; noise=2\t1.0000\tbaseline\n"
        + f"1\t{kept[:12]}\tdiscard\t(4, 4, 0)\t4\t-\tno improvement; cond={'c' * 16}; noise=2\t1.0000\tx\n",
        encoding="utf-8")
    assert loop.resume(root, results, "c" * 16)[3:] == ((3, 3, 0), 2)
    # 조건이 바뀌면 그 노이즈도 다시 잰다.
    assert loop.resume(root, results, "d" * 16)[3:] == (None, None)


def test_noise_floor_measures_the_baseline_twice_and_raises_the_bar(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    argv = _model_loop(tmp_path, monkeypatch, root, iter([_model(1), _model(2), _model(3), _model(3)]),
                       iter(["Read the plan."]))
    assert loop.main([*argv, "--iterations", "1", "--noise-floor"]) == 0
    rows = _rows(tmp_path)
    assert "noise=1" in rows[0][6]
    # 3 - 1 = 2 > noise 1이므로 keep이고, 확인 측정도 같은 판정을 낸다.
    assert rows[1][2] == "keep" and "noise=1" in rows[1][6]
