"""Grill experiment: case layers, the simulated product owner (`user_sim.py`) and its `ask-user` channel."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
import arm_compare  # noqa: E402
import user_sim  # noqa: E402

FULL = "# PRD\n\n- The cart holds at most 5 items.\n- Coupons are not supported.\n"
UNDER = "# PRD\n\nBuild a cart.\n"
# HEAD 이전(user_sim 도입 전) 코드로 만든 프롬프트의 sha256. user_sim이 없으면 한 바이트도 달라지지 않아야 한다.
PINNED = {"devflow-A": "e6cd3009825aa3b09f9f1e91d342f0eff79644f249c06c5b93ac73483b001206",
          "ext-A": "f9100087baedac60aa8514bb55ceed9620facae44ac4d9dda3b18fd6f3d3c7ac",
          "ext-C": "f066ecc387c8c988859e3c689992845852f5304f47a5b00dc1dc7bf55aefe522"}


def _git_init(path: Path) -> None:
    path.mkdir(parents=True)
    for args in (["init", "-q", "-b", "main"], ["config", "user.name", "t"], ["config", "user.email", "t@e"],
                 ["config", "commit.gpgsign", "false"]):
        arm_compare._git(args, path)


def _case(tmp_path: Path, **overrides) -> Path:
    seed = tmp_path / "seed-repo"
    _git_init(seed)
    (seed / "app.txt").write_text("seed app\n", encoding="utf-8")
    arm_compare._git(["add", "app.txt"], seed)
    arm_compare._git(["commit", "-qm", "app"], seed)
    case_dir = tmp_path / "private" / "cart"
    (case_dir / "task").mkdir(parents=True)
    (case_dir / "task" / "prd.md").write_text(FULL, encoding="utf-8")
    (case_dir / "task" / "prd-under.md").write_text(UNDER, encoding="utf-8")
    (case_dir / "score.py").write_text(
        "import json, sys\nargs = sys.argv[1:]\nout = args[args.index('--output') + 1]\n"
        "json.dump({'S': 1, 'contamination': []}, open(out, 'w'))\n", encoding="utf-8")
    case = {"name": "cart", "seed": {"repository": str(seed), "commit": arm_compare._git(["rev-parse", "HEAD"], seed)},
            "task": "Build the cart.", "bare_note": "Run ./gradlew test.", "profile": "android", "install": [],
            "score_keys": ["S"],
            "layers": {"complete": {"prd.md": "task/prd.md"},
                       "underspec": {"files": {"prd.md": "task/prd-under.md"}, "user_sim": {"oracle": "task/prd.md"}}},
            **overrides}
    (case_dir / "case.json").write_text(json.dumps(case), encoding="utf-8")
    return case_dir


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ─── prompts ───

def test_prompts_without_a_simulated_user_are_byte_identical_to_before(tmp_path):
    meta = arm_compare.prepare(ROOT, "A", "devflow", 1, tmp_path / "A-1")
    assert meta["prompt_sha256"] == PINNED["devflow-A"] and "user_sim" not in meta and "layer" not in meta
    case_dir = _case(tmp_path, files={"prd.md": "task/prd.md"})
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    del case["layers"]
    (case_dir / "case.json").write_text(json.dumps(case), encoding="utf-8")
    assert arm_compare.prepare(ROOT, "A", "x", 1, tmp_path / "ext-A", case_dir=case_dir)["prompt_sha256"] == PINNED["ext-A"]
    assert arm_compare.driver_prompt("Build the cart.", "full-feature") == arm_compare.driver_prompt(
        "Build the cart.", "full-feature", ask_user=False)
    assert _sha(arm_compare.driver_prompt("Build the cart.", "full-feature")) == PINNED["ext-C"]
    assert arm_compare.with_ask_user("Build the cart.", None) == "Build the cart."


def test_the_complete_layer_has_no_ask_user_line(tmp_path):
    case_dir = _case(tmp_path)
    run = tmp_path / "A-complete"
    meta = arm_compare.prepare(ROOT, "A", "x", 1, run, case_dir=case_dir, layer="complete")
    prompt = (run / "prompt.md").read_text(encoding="utf-8")
    assert meta["layer"] == "complete" and "user_sim" not in meta and "ask-user" not in prompt
    assert _sha(prompt) == PINNED["ext-A"]
    assert (run / "project" / "prd.md").read_text(encoding="utf-8") == FULL and not (run / user_sim.DIR).exists()


# ─── layers and the hidden oracle ───

def test_the_underspec_layer_seeds_its_files_and_keeps_the_oracle_out_of_the_project(tmp_path):
    case_dir = _case(tmp_path)
    run = tmp_path / "A-under"
    meta = arm_compare.prepare(ROOT, "A", "x", 1, run, case_dir=case_dir, layer="underspec")
    project = run / "project"
    assert (project / "prd.md").read_text(encoding="utf-8") == UNDER
    assert meta["layer"] == "underspec" and meta["user_sim"]["oracle_sha256"] == _sha(FULL)
    oracle = run / user_sim.DIR / user_sim.ORACLE
    assert oracle.read_text(encoding="utf-8") == FULL and not oracle.resolve().is_relative_to(project.resolve())
    tracked = arm_compare._git(["ls-files"], project).splitlines()
    assert all(FULL not in (project / name).read_text(encoding="utf-8", errors="replace") for name in tracked)
    assert all(path.read_bytes() != FULL.encode() for path in project.rglob("*") if path.is_file())
    recorded = (run / "arm.json").read_text(encoding="utf-8")
    assert str(case_dir) not in recorded and str(case_dir) not in (run / user_sim.DIR / user_sim.CONFIG).read_text()
    ask = Path(meta["user_sim"]["ask_user"])
    assert ask.is_file() and os.access(ask, os.X_OK) and not ask.resolve().is_relative_to(project.resolve())
    prompt = (run / "prompt.md").read_text(encoding="utf-8")
    line = arm_compare.ASK_USER_LINE.format(script=ask)
    assert line in prompt and f'`{ask} "<question>"`' in prompt
    assert prompt == arm_compare.external_bare_prompt({"task": f"Build the cart.\n\n{line}",
                                                       "bare_note": "Run ./gradlew test."})


def test_a_layered_case_needs_a_known_layer_and_a_plain_case_takes_none(tmp_path):
    case_dir = _case(tmp_path)
    with pytest.raises(SystemExit, match="needs --layer"):
        arm_compare.prepare(ROOT, "A", "x", 1, tmp_path / "r1", case_dir=case_dir)
    with pytest.raises(SystemExit, match="needs --layer"):
        arm_compare.prepare(ROOT, "A", "x", 1, tmp_path / "r2", case_dir=case_dir, layer="other")
    with pytest.raises(ValueError, match="--layer applies"):
        arm_compare.prepare(ROOT, "A", "devflow", 1, tmp_path / "r3", layer="complete")


def test_an_oracle_that_is_also_a_project_file_is_refused(tmp_path):
    case_dir = _case(tmp_path, layers={"leaky": {"files": {"prd.md": "task/prd.md"},
                                                 "user_sim": {"oracle": "task/prd.md"}}})
    with pytest.raises(SystemExit, match="also copied into the project"):
        arm_compare.prepare(ROOT, "A", "x", 1, tmp_path / "r", case_dir=case_dir, layer="leaky")
    case_dir = _case(tmp_path / "b", layers={"out": {"files": {"prd.md": "task/prd-under.md"},
                                                     "user_sim": {"oracle": "../outside.md"}}})
    (case_dir.parent / "outside.md").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit, match="not a file in the case directory"):
        arm_compare.prepare(ROOT, "A", "x", 1, tmp_path / "r2", case_dir=case_dir, layer="out")


def test_a_workflow_arm_routes_phase_questions_to_the_product_owner(tmp_path):
    case_dir = _case(tmp_path)
    run = tmp_path / "C-under"
    meta = arm_compare.prepare(ROOT, "C", "x", 1, run, case_dir=case_dir, layer="underspec")
    prompt = (run / "prompt.md").read_text(encoding="utf-8")
    line = arm_compare.ASK_USER_LINE.format(script=meta["user_sim"]["ask_user"])
    assert line in prompt and arm_compare.ANSWER_FROM_OWNER in prompt
    assert arm_compare.ANSWER_FROM_TASK not in prompt
    assert prompt == arm_compare.driver_prompt(f"Build the cart.\n\n{line}", "full-feature", ask_user=True)


# ─── the simulator and its ask-user script ───

def _fake_claude(bin_dir: Path, seen: Path, answer: str = "최대 5개입니다.", selection: str = '{"fact_ids": ["F01"]}') -> None:
    """A `claude` that answers the select call (its system prompt asks for fact_ids) and the answer call."""
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "stdin = sys.stdin.read()\n"
        "system = sys.argv[sys.argv.index('--system-prompt') + 1]\n"
        f"open({str(seen)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'stdin': stdin, 'cwd': os.getcwd(),\n"
        "                                         'env': dict(os.environ)}) + '\\n')\n"
        f"result = {selection!r} if 'fact_ids' in system else {answer!r}\n"
        "print(json.dumps({'result': result, 'total_cost_usd': 0.02, 'is_error': False,\n"
        "                  'modelUsage': {'claude-opus-5-5': {}}, 'usage': {'input_tokens': 50, 'output_tokens': 5}}))\n",
        encoding="utf-8")
    fake.chmod(0o755)


def test_the_ask_user_script_selects_facts_then_answers_from_only_those_and_logs_them(tmp_path):
    case_dir = _case(tmp_path)
    run = tmp_path / "A-under"
    meta = arm_compare.prepare(ROOT, "A", "x", 1, run, case_dir=case_dir, layer="underspec")
    user_sim.set_model(run, "claude-test-model")
    seen = tmp_path / "seen.jsonl"
    _fake_claude(tmp_path / "bin", seen)
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "CLAUDE_CODE_SESSION_ID": "controller",
           "CLAUDE_CODE_EFFORT_LEVEL": "high"}
    done = subprocess.run([meta["user_sim"]["ask_user"], "장바구니에 최대 몇 개까지 담나요?"], capture_output=True,
                          text=True, env=env, cwd=run / "project", timeout=60)
    assert done.returncode == 0 and done.stdout.strip() == "최대 5개입니다."
    select, answer = [json.loads(line) for line in seen.read_text(encoding="utf-8").splitlines()]
    for call in (select, answer):
        argv = call["argv"]
        assert argv[argv.index("--model") + 1] == "claude-test-model" and argv[argv.index("--tools") + 1] == ""
        assert argv[argv.index("--max-budget-usd") + 1] == "1" and argv[argv.index("--output-format") + 1] == "json"
        assert "CLAUDE_CODE_SESSION_ID" not in call["env"] and call["env"]["CLAUDE_CODE_EFFORT_LEVEL"] == "low"
        assert not Path(call["cwd"]).resolve().is_relative_to((run / "project").resolve())
    selecting = select["argv"][select["argv"].index("--system-prompt") + 1]
    assert '<fact id="F01">The cart holds at most 5 items.</fact>' in selecting
    assert '<fact id="F02">Coupons are not supported.</fact>' in selecting and "prd.md" not in selecting
    assert "장바구니에 최대 몇 개까지 담나요?" in select["stdin"]
    answering = answer["argv"][answer["argv"].index("--system-prompt") + 1]
    assert "The cart holds at most 5 items." in answering and "Coupons" not in answering
    assert user_sim.UNDECIDED in answering and "Answer in Korean" in answering
    assert answer["stdin"] == "장바구니에 최대 몇 개까지 담나요?"
    (row,) = user_sim.read_log(run / user_sim.LOG)
    assert row["question"] == "장바구니에 최대 몇 개까지 담나요?" and row["answer"] == "최대 5개입니다."
    assert row["disclosed_fact_ids"] == ["F01"] and row["language"] == "ko" and row["effort"] == "low"
    assert row["cost_usd"] == 0.04 and row["questions"] == 1 and row["ok"] and row["oracle_sha256"] == _sha(FULL)
    assert [call["stage"] for call in row["calls"]] == ["select", "answer"]
    assert row["models_used"] == ["claude-opus-5-5"]


FACTS = {"facts": [
    {"id": "U01.1", "rule": "U01", "topic": "장바구니 수량", "ko": "장바구니에는 최대 5개까지 담는다."},
    {"id": "U01.2", "rule": "U01", "topic": "장바구니 수량", "ko": "6번째를 담으면 담지 않고 안내 문구를 보인다."},
    {"id": "U05.1", "rule": "U05", "topic": "배송비", "ko": "배송비는 주문당 3,000원이다.",
     "en": "Shipping costs 3,000 won per order."}]}


def _staged(selection: str, answer: str = "답", cost: float | None = 0.01, seen: list | None = None):
    """A `call` stub: the select call returns `selection`, the answer call `answer`; every call is kept in `seen`."""
    def stub(argv, stdin, cwd, env, timeout):
        system = argv[argv.index("--system-prompt") + 1]
        if seen is not None:
            seen.append({"system": system, "stdin": stdin, "env": env})
        result = selection if "fact_ids" in system else answer
        return 0, json.dumps({"result": result, "total_cost_usd": cost}), "", False
    return stub


def _config(tmp_path: Path, oracle_text: str) -> dict:
    oracle = tmp_path / "oracle"
    oracle.write_text(oracle_text, encoding="utf-8")
    return {"run_dir": str(tmp_path), "oracle": str(oracle), "model": "m", "env": {"keys": ["PATH"], "set": {}}}


def test_a_compound_question_discloses_exactly_the_selected_facts(tmp_path):
    config = _config(tmp_path, json.dumps(FACTS, ensure_ascii=False))
    seen: list = []
    row = user_sim.ask("장바구니에 몇 개까지 담나요? 배송비는 얼마인가요?", config,
                       call=_staged('{"fact_ids": ["U05.1", "U01.1", "U99.9"]}', "5개, 3,000원입니다.", seen=seen),
                       environ={})
    assert row["ok"] and row["disclosed_fact_ids"] == ["U01.1", "U05.1"] and row["invalid_fact_ids"] == ["U99.9"]
    assert row["disclosed_rules"] == ["U01", "U05"] and row["questions"] == 2 and row["cost_usd"] == 0.02
    answering = seen[1]["system"]
    assert "최대 5개" in answering and "3,000원" in answering and "안내 문구" not in answering
    assert "U01.1" not in answering and "(English: Shipping costs 3,000 won per order.)" in answering
    assert seen[0]["env"]["CLAUDE_CODE_EFFORT_LEVEL"] == "low"


def test_an_english_question_is_answered_in_english_and_unknowns_get_the_english_undecided_sentence(tmp_path):
    config = _config(tmp_path, json.dumps(FACTS, ensure_ascii=False))
    seen: list = []
    row = user_sim.ask("How much is shipping?", config,
                       call=_staged('{"fact_ids": ["U05.1"]}', "3,000 won per order.", seen=seen), environ={})
    assert row["language"] == "en" and row["answer_language"] == "en" and row["disclosed_fact_ids"] == ["U05.1"]
    assert "Answer in English" in seen[1]["system"] and user_sim.UNDECIDED_BY_LANGUAGE["en"] in seen[1]["system"]
    seen.clear()
    unknown = user_sim.ask("What colour is the logo?", config, call=_staged('{"fact_ids": []}', seen=seen), environ={})
    assert unknown["ok"] and unknown["undecided"] and unknown["answer"] == user_sim.UNDECIDED_BY_LANGUAGE["en"]
    assert unknown["disclosed_fact_ids"] == [] and len(seen) == 1 and unknown["cost_usd"] == 0.01
    korean = user_sim.ask("로고 색은요?", config, call=_staged('{"fact_ids": []}'), environ={})
    assert korean["answer"] == user_sim.UNDECIDED and korean["language"] == "ko"


def test_every_question_and_answer_is_logged_with_its_cost_and_failures_count(tmp_path):
    config = _config(tmp_path, FULL)
    replies = iter([(0, json.dumps({"result": '{"fact_ids": []}', "total_cost_usd": 0.01}), "", False),
                    (1, "", "boom", False),
                    (0, json.dumps({"result": "I think F01", "total_cost_usd": 0.01}), "", False),
                    (0, json.dumps({"result": '{"fact_ids": ["F02"]}', "total_cost_usd": 0.01}), "", False),
                    (0, json.dumps({"result": "쿠폰은 없습니다."}), "", False)])
    stub = lambda argv, stdin, cwd, env, timeout: next(replies)  # noqa: E731
    first = user_sim.ask("1. 쿠폰은요? 2. 배송비는?", config, call=stub, environ={})
    second = user_sim.ask("색상은?", config, call=stub, environ={})
    third = user_sim.ask("포장은?", config, call=stub, environ={})
    fourth = user_sim.ask("쿠폰은요?", config, call=stub, environ={})
    assert first["ok"] and first["undecided"] and first["questions"] == 2 and first["answer"] == user_sim.UNDECIDED
    assert not second["ok"] and second["answer"] is None and "boom" in second["error"]
    assert not third["ok"] and "fact_ids" in third["error"] and third["disclosed_fact_ids"] == []
    assert fourth["ok"] and fourth["disclosed_fact_ids"] == ["F02"] and fourth["cost_usd"] is None
    rows = user_sim.read_log(tmp_path / user_sim.LOG)
    assert [row["question"] for row in rows] == ["1. 쿠폰은요? 2. 배송비는?", "색상은?", "포장은?", "쿠폰은요?"]
    assert user_sim.summary(tmp_path / user_sim.LOG) == {
        "ask_user_calls": 4, "ask_user_questions": 5, "asked_any": True, "ask_user_failed": 2,
        "ask_user_undecided": 1, "ask_user_disclosed_fact_ids": ["F02"], "user_sim_cost_usd": None}
    assert user_sim.summary(tmp_path / "none.jsonl")["user_sim_cost_usd"] == 0.0
    assert user_sim.count_questions("") == 0 and user_sim.count_questions("Tell me the limit") == 1


def test_markdown_oracles_split_into_one_fact_per_bullet_with_their_indented_lines():
    facts = user_sim.facts_from_markdown("<!-- c -->\n# T\n\n## 상품\n- 첫째.\n  - 예: 1개.\n- 둘째.\n\n## 결제\n- 셋째.\n")
    assert facts == [{"id": "F01", "ko": "첫째.\n- 예: 1개.", "topic": "상품"}, {"id": "F02", "ko": "둘째.", "topic": "상품"},
                     {"id": "F03", "ko": "셋째.", "topic": "결제"}]
    assert [fact["id"] for fact in user_sim.load_facts(json.dumps(FACTS))] == ["U01.1", "U01.2", "U05.1"]
    with pytest.raises(ValueError):
        user_sim.load_facts('{"facts": []}')
    assert [user_sim.language(t) for t in ("몇 개?", "How many?", "いくつ?", "1?")] == ["ko", "en", "ja", "en"]
    assert user_sim.parse_selection("```json\n{\"fact_ids\": [\"b\", \"a\"]}\n```", ["a", "b"]) == (["a", "b"], [])
    assert user_sim.parse_selection("none", ["a"]) == (None, [])


# ─── score and summarize ───

def test_score_counts_ask_user_calls_adds_the_simulator_cost_and_flags_oracle_reads(tmp_path):
    case_dir = _case(tmp_path)
    run = tmp_path / "A-under"
    arm_compare.prepare(ROOT, "A", "x", 1, run, case_dir=case_dir, layer="underspec")
    config = json.loads((run / user_sim.DIR / user_sim.CONFIG).read_text(encoding="utf-8"))
    stub = _staged('{"fact_ids": ["F01"]}', "5개", cost=0.05)
    user_sim.ask("최대 수량은? 쿠폰은?", config, call=stub, environ={})
    user_sim.ask("배송비는?", config, call=stub, environ={})
    (run / "drive.json").write_text(json.dumps({"cost_usd": 2.0, "exit": 0}), encoding="utf-8")
    result = arm_compare.score(run, None, case_dir / "score.py")
    assert result["ask_user_calls"] == 2 and result["ask_user_questions"] == 3 and result["asked_any"] is True
    assert result["user_sim_cost_usd"] == 0.2 and result["total_cost_usd"] == 2.2
    assert result["layer"] == "underspec" and result["oracle_reads"] == 0 and result["contaminated"] is False
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": f"cat {run / user_sim.DIR / user_sim.ORACLE}"}}]}})
        + "\n", encoding="utf-8")
    peeked = arm_compare.score(run, transcript, case_dir / "score.py")
    assert peeked["oracle_reads"] == 1 and peeked["contaminated"] is True


def test_a_run_without_a_simulated_user_records_no_ask_fields(tmp_path):
    case_dir = _case(tmp_path)
    run = tmp_path / "A-complete"
    arm_compare.prepare(ROOT, "A", "x", 1, run, case_dir=case_dir, layer="complete")
    result = arm_compare.score(run, None, case_dir / "score.py")
    assert "ask_user_calls" not in result and "asked_any" not in result and result["layer"] == "complete"


def _row(arm, layer, *, oracle=True, **extra):
    return {"arm": arm, "task_kind": "normal", "oracle": oracle, "contaminated": False,
            "final_message": "EVAL-STATUS: complete", "declaration": "declared-complete",
            "usage": {"input": 1, "uncached_input": 1, "cached_input": 0, "output": 10}, "seconds": 5,
            "layer": layer, **extra}


def test_the_summary_groups_by_layer_and_reports_questions_and_simulator_cost():
    asked = {"ask_user_calls": 3, "ask_user_questions": 5, "asked_any": True, "user_sim_cost_usd": 0.3}
    silent = {"ask_user_calls": 0, "ask_user_questions": 0, "asked_any": False, "user_sim_cost_usd": 0.0}
    rows = [_row("A", "complete", total_cost_usd=1.0), _row("A", "complete", total_cost_usd=3.0),
            _row("A", "underspec", oracle=False, total_cost_usd=1.0, **silent),
            _row("A", "underspec", total_cost_usd=2.3, **asked),
            _row("C", "underspec", total_cost_usd=5.3, **asked)]
    table = arm_compare.summarize(rows)
    lines = table.splitlines()
    assert "By layer (assigned runs):" in lines
    assert "| A | complete | 2 | 2/2 | - | $2.00 |" in lines
    assert "| A | underspec | 2 | 1/2 | 1/2 / 1.5 / 2.5 / $0.15 | $1.65 |" in lines
    assert "| C | underspec | 1 | 1/1 | 1/1 / 3.0 / 5.0 / $0.30 | $5.30 |" in lines
    plain = arm_compare.summarize([{key: value for key, value in row.items() if key != "layer"}
                                   for row in rows[:2]])
    assert "By layer" not in plain


# ─── domain-grill: user rounds must come through ask-user ───

GRILL = """# Domain grill

## Decision Sources
- cart limit is 5: user round 1
- coupons out of scope: user round 2
- currency: task

## Completion Gate
domain-grill: complete
"""


def test_the_grill_check_compares_user_rounds_with_ask_user_calls(tmp_path, monkeypatch):
    artifact = tmp_path / "domain-grill.md"
    artifact.write_text(GRILL, encoding="utf-8")
    monkeypatch.setattr(arm_compare, "phase_artifact", lambda worktree, status, phase: artifact)
    log = tmp_path / user_sim.LOG
    check = arm_compare.grill_check(tmp_path, {}, log)
    assert check["decisions"] == 3 and check["user_round_decisions"] == 2 and check["user_rounds"] == 2
    assert check["ask_user_calls"] == 0 and check["passed"] is False
    log.write_text("".join(json.dumps({"question": q, "ok": True, "cost_usd": 0.01}) + "\n" for q in "ab"),
                   encoding="utf-8")
    assert arm_compare.grill_check(tmp_path, {}, log)["passed"] is True
    artifact.write_text(GRILL.replace("user round 1", "task").replace("user round 2", "prd.md"), encoding="utf-8")
    assert arm_compare.grill_check(tmp_path, {}, tmp_path / "none.jsonl")["passed"] is True
    monkeypatch.setattr(arm_compare, "phase_artifact", lambda worktree, status, phase: None)
    assert arm_compare.grill_check(tmp_path, {}, log)["passed"] is None


def test_drive_sends_a_self_answered_grill_back_once_then_approves_and_records_it(tmp_path, monkeypatch):
    run = tmp_path / "C-1"
    (run / "project").mkdir(parents=True)
    (run / user_sim.DIR).mkdir()
    (run / user_sim.DIR / user_sim.CONFIG).write_text(json.dumps({"model": "old"}), encoding="utf-8")
    (run / "arm.json").write_text(json.dumps({"arm": "C", "project": str(run / "project"),
                                              "user_sim": {"ask_user": "/r/user-sim/ask-user"}}), encoding="utf-8")
    (run / "prompt.md").write_text("task", encoding="utf-8")
    artifact = tmp_path / "domain-grill.md"
    artifact.write_text(GRILL, encoding="utf-8")
    messages = []

    def controller_once(argv, project, out, err, timeout):
        messages.append(argv[2])
        out.write((json.dumps({"type": "system", "subtype": "init", "session_id": "s-1"}) + "\n" +
                   json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 1.0,
                               "result": "Summary for approval."}) + "\n").encode())
        return 0, False

    grill = {"reason": "phase_approval_required", "current_phase": "domain-grill", "worktree": str(tmp_path),
             "run": "full-feature/1", "approval_command": "agent-flow continue --approve x"}
    queue = [grill, grill]
    approved = []
    monkeypatch.setattr(arm_compare, "_controller_once", controller_once)
    monkeypatch.setattr(arm_compare, "runner_status", lambda project: queue.pop(0) if queue else None)
    monkeypatch.setattr(arm_compare, "phase_artifact", lambda worktree, status, phase: artifact)
    monkeypatch.setattr(arm_compare, "approve_phase", lambda project, status: approved.append(status) or True)
    monkeypatch.setattr(arm_compare.c_prime, "_sh", lambda argv, cwd, **_:
                        type("R", (), {"returncode": 0, "stdout": "2.1.308", "stderr": ""})())
    record = arm_compare.drive(run, "claude-opus-5-5", 600, 30.0)
    assert len(messages) == 3 and len(approved) == 1
    assert messages[1] == arm_compare.RESUME_AFTER_GRILL_CHECK.format(rounds=2, calls=0, script="/r/user-sim/ask-user")
    assert messages[2] == arm_compare.RESUME_AFTER_APPROVAL.format(phase="domain-grill")
    assert [check["passed"] for check in record["grill_checks"]] == [False, False]
    assert record["harness_approvals"] == ["domain-grill"]
    assert json.loads((run / user_sim.DIR / user_sim.CONFIG).read_text(encoding="utf-8"))["model"] == "claude-opus-5-5"
