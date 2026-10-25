"""Arm R (router): the model picks the workflow from a fixed menu, the pick is frozen, then that arm is prepared."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
import arm_compare  # noqa: E402
import router  # noqa: E402
import variants  # noqa: E402

NAMES = ["none", "verify-only", "verify-skills", "development", "default", "full-feature", "compose"]


@pytest.fixture
def fake_router(tmp_path, monkeypatch):
    """A `claude` on PATH that answers each call with the next prepared answer and records what it was given."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    answers, calls = tmp_path / "answers.json", tmp_path / "calls.jsonl"
    fake = bin_dir / "claude"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"calls = {str(calls)!r}\n"
        "n = sum(1 for _ in open(calls)) if os.path.exists(calls) else 0\n"
        "open(calls, 'a').write(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ), 'cwd': os.getcwd()}) + '\\n')\n"
        f"answer = json.load(open({str(answers)!r}))[n]\n"
        "if answer is None:\n"
        "    sys.exit(3)\n"
        "print(json.dumps({'result': answer, 'total_cost_usd': 0.25, 'num_turns': 4, 'is_error': False,\n"
        "                  'usage': {'input_tokens': 300, 'output_tokens': 40}}))\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{arm_compare.os.environ['PATH']}")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "operator-session")

    def setup(*replies):
        answers.write_text(json.dumps(list(replies)), encoding="utf-8")
        return lambda: [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    return setup


def _answer(choice, reason="The change is small and well covered by tests."):
    return f"I read the cart module.\n\n{json.dumps({'choice': choice, 'reason': reason})}"


def test_parsing_takes_only_an_exact_json_last_line_naming_a_menu_option():
    good, error = router.parse_choice(_answer("development"), NAMES)
    assert good == {"choice": "development", "reason": "The change is small and well covered by tests."} and not error
    for text in ('{"choice": "development", "reason": "x"}\nThanks!',            # not the last line
                 '```json\n{"choice": "development", "reason": "x"}\n```',      # fenced
                 '{"choice": "fast", "reason": "x"}',                            # not on the menu
                 '{"choice": "none"}',                                           # no reason
                 '{"choice": "none", "reason": "x", "confidence": 1}',           # extra key
                 '{"choice": "none", "reason": ""}', "", None):
        assert router.parse_choice(text, NAMES)[0] is None, text


def test_the_menu_is_fixed_text_and_variants_join_it_under_their_names():
    items = router.menu()
    assert [item["name"] for item in items] == NAMES
    assert [item["arm"] for item in items] == ["A", "V", "VK", "B", "D", "C", "S"]
    assert router.menu_sha256(items) == router.menu_sha256(router.menu())
    spec = {"planning": False, "reviewers": 2}
    with_variant = router.menu([spec])
    assert with_variant[-1] == {"name": variants.name(spec), "arm": "S", "variant": variants.normalize(spec),
                                "description": router.variant_description(spec)}
    assert "domain-grill" in with_variant[-1]["description"] and "2 reviewers" in with_variant[-1]["description"]
    assert router.menu_sha256(with_variant) != router.menu_sha256(items)
    with pytest.raises(ValueError):
        router.menu([spec, {"reviewers": 2, "planning": False}])


def test_the_router_is_one_read_only_call_in_the_seeded_project(tmp_path, fake_router):
    calls = fake_router(_answer("verify-only"))
    run = tmp_path / "R-1"
    meta = arm_compare.prepare(ROOT, "R", "devflow", 1, run)
    (call,) = calls()
    argv = call["argv"]
    assert Path(call["cwd"]).resolve() == (run / "project").resolve()
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5" and argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep"
    assert {"Bash", "Edit", "Write"} <= set(argv[argv.index("--disallowedTools") + 1].split(","))
    assert argv[argv.index("--max-budget-usd") + 1] == "2" and "--no-session-persistence" in argv
    assert "CLAUDE_CODE_SESSION_ID" not in call["env"] and call["env"]["CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"] == "1"
    text = argv[argv.index("-p") + 1]
    case = json.loads((ROOT / "evals" / "phase-cases" / "devflow" / "case.json").read_text(encoding="utf-8"))
    assert case["task"] in text and router.menu_text(router.menu()) in text and '"choice"' in text
    for marker in arm_compare.HIDDEN_MARKERS:
        assert marker not in text
    assert meta["router"]["prompt_sha256"] == router.hashlib.sha256(text.encode()).hexdigest()


def test_the_decision_is_frozen_and_verify_only_prepares_arm_vs_gate(tmp_path, fake_router):
    fake_router(_answer("verify-only"))
    run = tmp_path / "R-1"
    meta = arm_compare.prepare(ROOT, "R", "devflow", 1, run)
    stored = json.loads((run / "arm.json").read_text(encoding="utf-8"))
    assert stored == meta and stored["arm"] == "R" and stored["effective_arm"] == "V"
    decision = stored["router"]
    assert decision["choice"] == "verify-only" and decision["fallback"] is False and decision["attempts"] == 1
    assert decision["reason"] == "The change is small and well covered by tests."
    assert decision["cost_usd"] == 0.25 and decision["usage"] == {"input_tokens": 300, "output_tokens": 40}
    assert decision["seconds"] is not None and decision["menu"] == NAMES
    assert decision["menu_sha256"] == router.menu_sha256(router.menu()) and decision["model"] == "claude-opus-5-5"
    (raw,) = decision["raw_outputs"]
    assert Path(raw).is_file() and (run / "router").resolve() in Path(raw).parents
    assert stored["router_frozen_at"] and not (run / "drive.json").exists()
    # arm V's preparation in the same run directory, with A's prompt word for word
    assert (run / "gate" / "verify_gate.py").is_file() and (run / "project" / ".claude" / "settings.json").is_file()
    assert meta["verify_gate"]["verify_command"] == json.loads(
        (ROOT / "evals" / "phase-cases" / "devflow" / "case.json").read_text(encoding="utf-8"))["test_command"]
    arm_compare.prepare(ROOT, "A", "devflow", 1, tmp_path / "A-1")
    assert (run / "prompt.md").read_text(encoding="utf-8") == (tmp_path / "A-1" / "prompt.md").read_text(encoding="utf-8")
    assert arm_compare._git(["log", "--format=%s", "-1"], run / "project") == "verify gate"
    assert not (run / "project" / "router").exists()


def test_the_frozen_record_is_written_before_the_chosen_arm_is_prepared(tmp_path, fake_router, monkeypatch):
    fake_router(_answer("development"))
    seen = {}

    def install(kit, project, case, *, hooks):
        seen["arm.json"] = json.loads((project.parent / "arm.json").read_text(encoding="utf-8"))
        raise SystemExit("stop here")

    monkeypatch.setattr(arm_compare, "_install", install)
    with pytest.raises(SystemExit):
        arm_compare.prepare(ROOT, "R", "devflow", 1, tmp_path / "R-1")
    assert seen["arm.json"]["router"]["choice"] == "development" and seen["arm.json"]["effective_arm"] == "B"


def test_development_installs_the_kit_and_drives_the_development_workflow(tmp_path, fake_router):
    fake_router(_answer("development"))
    run = tmp_path / "R-1"
    meta = arm_compare.prepare(ROOT, "R", "devflow", 1, run)
    assert meta["effective_arm"] == "B" and meta["workflow"] == "development"
    assert (run / "project" / ".agent-flow" / "bin" / "agent-flow").exists()
    assert "--workflow development" in (run / "prompt.md").read_text(encoding="utf-8")
    assert meta["approval_rules"] == arm_compare.HARNESS_APPROVES and meta["verify_gate"] is None
    assert arm_compare.rule_violation({**meta, "spec_self_approvals": 1})


def test_a_menu_variant_can_be_chosen_and_is_prepared_as_arm_s(tmp_path, fake_router):
    spec = {"planning": False, "reviewers": 2}
    name = variants.name(spec)
    calls = fake_router(_answer(name))
    run = tmp_path / "R-1"
    meta = arm_compare.prepare(ROOT, "R", "devflow", 1, run, menu_variants=[spec])
    assert name in calls()[0]["argv"][calls()[0]["argv"].index("-p") + 1]
    assert meta["effective_arm"] == "S" and meta["router"]["menu"] == [*NAMES, name]
    assert meta["router"]["variant"] == variants.normalize(spec) and meta["variant"]["name"] == name
    assert (run / "project" / ".agent-flow" / "workflows" / f"{name}.yaml").is_file()
    assert f"--workflow {name}" in (run / "prompt.md").read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        arm_compare.prepare(ROOT, "C", "devflow", 1, tmp_path / "C-1", menu_variants=[spec])


def test_a_bad_answer_is_retried_once(tmp_path, fake_router):
    calls = fake_router("I would pick development.", _answer("none"))
    meta = arm_compare.prepare(ROOT, "R", "devflow", 1, tmp_path / "R-1")
    assert len(calls()) == 2
    decision = meta["router"]
    assert decision["choice"] == "none" and decision["fallback"] is False and decision["attempts"] == 2
    assert decision["errors"] == ["last line is not JSON"] and decision["cost_usd"] == 0.5
    assert len(decision["raw_outputs"]) == 2 and meta["effective_arm"] == "A"


def test_two_bad_answers_fall_back_to_none(tmp_path, fake_router):
    calls = fake_router(_answer("fast"), None)  # an option not on the menu, then a failed call
    run = tmp_path / "R-1"
    meta = arm_compare.prepare(ROOT, "R", "devflow", 1, run)
    assert len(calls()) == 2
    decision = meta["router"]
    assert decision["choice"] == "none" and decision["fallback"] is True and decision["reason"] is None
    assert decision["errors"] == ["`choice` 'fast' is not on the menu", "call failed (exit 3)"]
    assert decision["cost_usd"] is None  # the failed call reported no cost
    assert meta["effective_arm"] == "A" and not (run / "project" / ".agent-flow").exists()
    assert arm_compare._git(["branch", "--show-current"], run / "project") == arm_compare.BRANCH


def test_an_external_case_prepares_arm_r(tmp_path, fake_router):
    from tests.test_arm_compare import _external_case
    case_dir, _ = _external_case(tmp_path)
    calls = fake_router(_answer("verify-only"))
    meta = arm_compare.prepare(ROOT, "R", "ignored", 1, tmp_path / "runs" / "R-1", case_dir=case_dir)
    assert "Build the cart." in calls()[0]["argv"][calls()[0]["argv"].index("-p") + 1]
    assert meta["effective_arm"] == "V" and meta["verify_gate"]["documents"] == ["prd.md"]
    assert meta["case_kind"] == "external"


def test_drive_runs_the_chosen_arm_with_a_fresh_controller_on_the_routers_model(tmp_path, fake_router, monkeypatch):
    fake_router(_answer("verify-only"))
    run = tmp_path / "R-1"
    arm_compare.prepare(ROOT, "R", "devflow", 1, run)
    started = []

    def controller_once(argv, project, out, err, timeout):
        started.append(argv)
        out.write((json.dumps({"type": "system", "subtype": "init", "session_id": "s-1"}) + "\n" +
                   json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 2.0,
                               "result": "EVAL-STATUS: complete"}) + "\n").encode())
        return 0, False

    monkeypatch.setattr(arm_compare, "_controller_once", controller_once)
    with pytest.raises(SystemExit):
        arm_compare.drive(run, "claude-other", 600, 30.0)
    record = arm_compare.drive(run, "claude-opus-5-5", 600, 30.0)
    (argv,) = started
    assert "--resume" not in argv and argv[argv.index("-p") + 1] == (run / "prompt.md").read_text(encoding="utf-8")
    assert "verify_gate" in record  # the gate's summary, as for arm V


def test_score_adds_the_router_cost_and_the_summary_shows_the_choices(tmp_path, fake_router):
    fake_router(_answer("verify-only"))
    run = tmp_path / "R-1"
    arm_compare.prepare(ROOT, "R", "devflow", 1, run)
    (run / "drive.json").write_text(json.dumps({"cost_usd": 2.0, "exit": 0}), encoding="utf-8")
    (run / "verify-gate.jsonl").write_text(
        json.dumps({"step": "review", "decision": "allow", "findings": 0,
                    "review": {"cost_usd": 0.5, "seconds": 10}}) + "\n", encoding="utf-8")
    result = arm_compare.score(run, None)
    assert result["arm"] == "R" and result["effective_arm"] == "V" and "wiring_intact" in result["verify_gate"]
    assert result["router_cost_usd"] == 0.25 and result["total_cost_usd"] == 2.75

    from tests.test_arm_compare import _summary_row

    def routed(choice, arm, cost, **extra):
        return _summary_row("R", effective_arm=arm, controller_cost_usd=2.0, total_cost_usd=None if cost is None else 2.0 + cost,
                            router={"choice": choice, "menu": NAMES, "cost_usd": cost, "fallback": False}, **extra)
    rows = [_summary_row("A", controller_cost_usd=1.0, total_cost_usd=1.0),
            routed("development", "B", 0.25), routed("development", "B", 0.25, oracle=False),
            routed("verify-only", "V", 0.5, verify_gate={"blocks": 1, "review_findings": 0, "reviewer_cost_usd": 0.0}),
            {**routed("none", "A", None), "router": {"choice": "none", "menu": NAMES, "cost_usd": None,
                                                    "fallback": True}, "total_cost_usd": None}]
    table = arm_compare.summarize(rows)
    lines = table.splitlines()
    r_line = next(line for line in lines if line.startswith("| R |"))
    assert "| R | 4 | 3/4 |" in r_line and "| $2.00 / $2.33 |" in r_line
    dev = next(line for line in lines if line.startswith("| R: development |"))
    assert "| R: development | 2 | 1/2 | - |" in dev and "| $2.00 / $2.25 | - |" in dev
    gate = next(line for line in lines if line.startswith("| R: verify-only |"))
    assert "| 1.0 / 0.0 / $0.00 |" in gate
    assert "Router choices (arm R, assigned runs):" in table
    choices = lines[lines.index("| option | effective arm | runs | success | router fallbacks | router cost (mean) |"):]
    assert "| none | A | 1 | 1/1 | 1 | - |" in choices
    assert "| verify-only | V | 1 | 1/1 | 0 | $0.50 |" in choices
    assert "| development | B | 2 | 1/2 | 0 | $0.25 |" in choices
    assert "| default | D | 0 | 0/0 | 0 | - |" in choices and "| full-feature | C | 0 | 0/0 | 0 | - |" in choices
    assert "| verify-skills | VK | 0 | 0/0 | 0 | - |" in choices and "| compose | S | 0 | 0/0 | 0 | - |" in choices


def test_the_cli_and_preflight_take_arm_r(tmp_path, fake_router, monkeypatch):
    spec = {"tdd": False}
    calls = fake_router(_answer(variants.name(spec)))
    run = tmp_path / "R-cli"
    assert arm_compare.main(["prepare", "--kit", str(ROOT), "--arm", "R", "--case", "devflow", "--output", str(run),
                             "--menu-variant", json.dumps(spec), "--model", "claude-opus-5-5"]) == 0
    assert json.loads((run / "arm.json").read_text(encoding="utf-8"))["effective_arm"] == "S"
    assert len(calls()) == 1
    # the router runs on the controller's model, so the controller's preflight entry covers it
    monkeypatch.setattr(arm_compare.subprocess, "run", lambda argv, **_: arm_compare.subprocess.CompletedProcess(
        argv, 0, "OK\n", ""))
    assert arm_compare.main(["preflight", "--model", "claude:claude-opus-5-5"]) == 0


# ─── compose: the router assembles a reduced full-feature from variants' space ───

def _compose(spec, reason="Small change; planning adds nothing."):
    return f"I read the cart module.\n\n{json.dumps({'choice': 'compose', 'reason': reason, 'spec': spec})}"


def test_the_compose_option_describes_variants_space_in_neutral_words():
    (item,) = [item for item in router.menu() if item["name"] == "compose"]
    assert item["arm"] == "S" and "variant" not in item
    text = item["description"]
    for key in (*variants.DROPS, "reviewers"):
        assert f"`{key}`" in text
    for phases in variants.DROPS.values():
        assert all(phase in text for phase in phases)
    assert all(str(count) in text for count in variants.REVIEWER_COUNTS) and '"spec"' in text
    assert "$" not in text and "minute" not in text and "token" not in text
    assert '"spec"' in router.prompt("Do the task.", router.menu())


def test_compose_needs_a_spec_that_variants_accepts_and_no_other_choice_takes_one():
    good, error = router.parse_choice(_compose({"planning": False, "reviewers": 2}), NAMES)
    assert not error and good["spec"] == {"planning": False, "reviewers": 2}
    assert router.parse_choice(_compose({}), NAMES)[0] is not None  # every key absent: full-feature's values
    for text, reason in (
            ('{"choice": "compose", "reason": "x"}', "`compose` needs exactly `choice`, `reason` and `spec`"),
            (_compose({"planning": "no"}), "`spec` is invalid: planning must be true or false"),
            (_compose({"reviewers": 3}), "`spec` is invalid: reviewers must be one of (2, 5)"),
            (_compose({"qa": False}), "`spec` is invalid: unknown variant keys: ['qa']"),
            (_compose([False]), "`spec` is not an object"),
            ('{"choice": "none", "reason": "x", "spec": {}}',
             "last line is not an object with exactly `choice` and `reason`")):
        assert router.parse_choice(text, NAMES) == (None, reason), text


def test_a_composed_spec_is_prepared_as_arm_s_with_that_variant(tmp_path, fake_router):
    spec = {"planning": False, "tdd": True, "architecture_review": False, "reviewers": 2}
    name = variants.name(spec)
    calls = fake_router(_compose(spec))
    run = tmp_path / "R-1"
    meta = arm_compare.prepare(ROOT, "R", "devflow", 1, run)
    assert len(calls()) == 1
    decision = meta["router"]
    assert decision["choice"] == "compose" and decision["fallback"] is False and decision["spec"] == spec
    assert decision["variant"] == variants.normalize(spec) == {"planning": False, "architecture_review": False,
                                                                "reviewers": 2}
    assert decision["variant_name"] == name and meta["effective_arm"] == "S"
    assert meta["workflow"] == name and meta["variant"]["name"] == name
    assert meta["variant"]["spec"] == variants.normalize(spec)
    phases = meta["variant"]["phases"]
    assert "domain-grill" not in phases and "architecture-review" not in phases and "red" in phases
    assert (run / "project" / ".agent-flow" / "workflows" / f"{name}.yaml").is_file()
    assert f"--workflow {name}" in (run / "prompt.md").read_text(encoding="utf-8")
    assert meta["approval_rules"] == arm_compare.HARNESS_APPROVES
    stored = json.loads((run / "arm.json").read_text(encoding="utf-8"))
    assert stored["router"]["spec"] == spec and stored["router"]["variant_name"] == name


def test_an_invalid_spec_is_retried_once_then_falls_back_to_none(tmp_path, fake_router):
    calls = fake_router(_compose({"reviewers": 3}), _compose({"planning": False}))
    meta = arm_compare.prepare(ROOT, "R", "devflow", 1, tmp_path / "R-1")
    assert len(calls()) == 2 and meta["effective_arm"] == "S" and meta["router"]["attempts"] == 2
    assert meta["router"]["errors"] == ["`spec` is invalid: reviewers must be one of (2, 5)"]


def test_a_spec_the_runs_profile_cannot_build_counts_as_invalid(tmp_path, fake_router):
    # devflow's python profile has 3 review angles: 5 reviewers is a valid point of the space but not of this run
    calls = fake_router(_compose({"reviewers": 5}), _compose({"design": "off"}))
    run = tmp_path / "R-2"
    meta = arm_compare.prepare(ROOT, "R", "devflow", 2, run)
    decision = meta["router"]
    assert len(calls()) == 2 and decision["choice"] == "none" and decision["fallback"] is True
    assert decision["errors"] == ["`spec` is invalid: profile python has 3 angles; 5 reviewers need 4",
                                  "`spec` is invalid: design must be true or false"]
    assert "spec" not in decision and "variant" not in decision and "variant_name" not in decision
    assert meta["effective_arm"] == "A" and not (run / "project" / ".agent-flow").exists()


def test_verify_skills_prepares_arm_vk(tmp_path, fake_router):
    fake_router(_answer("verify-skills"))
    run = tmp_path / "R-1"
    meta = arm_compare.prepare(ROOT, "R", "devflow", 1, run)
    assert meta["effective_arm"] == "VK" and meta["verify_gate"] is not None
    assert meta["skills"]["names"] and (run / "project" / ".claude" / "skills" / "tdd" / "SKILL.md").is_file()
    assert not (run / "project" / ".agent-flow").exists()
