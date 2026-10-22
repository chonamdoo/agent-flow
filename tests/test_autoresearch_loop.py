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


def test_routing_key_ignores_term_order_case_and_quotes(tmp_path):
    root = _repo(tmp_path)
    _set_terms(root, "[alpha, beta]")
    key = loop.surface_key(root, "routing", [])
    _set_terms(root, "['Beta',alpha, beta]")
    assert loop.surface_key(root, "routing", []) == key
    _set_terms(root, "[alpha]")
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
    edits = iter(["[alpha, beta]", "[alpha]", "[Beta, alpha]"])

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
    assert loop.resume(root, tmp_path / "work" / "t" / "results.tsv") == (4.0, 1, {})
