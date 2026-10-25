"""Arm V's completion gate (#409): verification blocks, one review round, caps and `stop_hook_active`."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
import verify_gate  # noqa: E402


def _config(tmp_path: Path, verify_command=("make", "test")) -> dict:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return {"project": str(project), "run_dir": str(tmp_path), "task": "Add a discount.", "documents": ["prd.md"],
            "verify_command": list(verify_command) if verify_command else None, "model": "claude-opus-5-5",
            "env": {"keys": ["PATH", "HOME"], "set": {"CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1"}}}


class Stubs:
    def __init__(self, passes, reviews):
        self.passes, self.reviews = list(passes), list(reviews)
        self.verify_calls, self.review_calls = [], []

    def verify(self, argv, project, env):
        self.verify_calls.append((argv, env))
        ok = self.passes.pop(0)
        return {"exit": 0 if ok else 1, "timed_out": False, "passed": ok, "seconds": 0.1,
                "tail": "" if ok else "FAILED test_discount"}

    def review(self, prompt, model, project, env):
        self.review_calls.append((prompt, model, env))
        text = self.reviews.pop(0)
        return {"exit": 0, "timed_out": False, "seconds": 5, "is_error": False, "text": text, "cost_usd": 0.4,
                "usage": {"output_tokens": 7}, "num_turns": 2, "error": None}


def _stop(config, stubs, active=False):
    output, event = verify_gate.decide(config, {"stop_hook_active": active, "session_id": "s"}, verify=stubs.verify,
                                       review=stubs.review,
                                       environ={"PATH": "/bin", "HOME": "/h", "CLAUDE_CODE_SESSION_ID": "leak"})
    verify_gate.append_event(config, event)
    return output, event


def test_a_passing_run_gets_one_review_and_stops_on_no_findings(tmp_path):
    config, stubs = _config(tmp_path), Stubs([True], ["NO_FINDINGS"])
    output, event = _stop(config, stubs)
    assert output == {} and event["step"] == "review" and event["findings"] == 0
    prompt, model, env = stubs.review_calls[0]
    assert "Add a discount." in prompt and "`prd.md`" in prompt and "NO_FINDINGS" in prompt and model == "claude-opus-5-5"
    assert env == {"PATH": "/bin", "HOME": "/h", "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1"}
    assert stubs.verify_calls[0][1] == env
    assert verify_gate.summary(tmp_path / verify_gate.LOG)["gate_cap_reached"] is None


def test_failing_verification_blocks_at_most_three_times_then_lets_the_stop_through(tmp_path):
    config, stubs = _config(tmp_path), Stubs([False] * 4, [])
    outputs = [_stop(config, stubs, active=index > 0) for index in range(4)]
    for output, event in outputs[:3]:
        assert output["decision"] == "block" and "`make test` exited 1" in output["reason"]
        assert "FAILED test_discount" in output["reason"] and event["step"] == "verify"
    output, event = outputs[3]
    assert output == {} and event["gate_cap_reached"] == "verify"
    assert stubs.review_calls == []  # no review while verification fails
    summary = verify_gate.summary(tmp_path / verify_gate.LOG)
    assert summary["verify_blocks"] == 3 and summary["blocks"] == 3 and summary["gate_cap_reached"] == "verify"
    assert summary["reviewer_cost_usd"] == 0.0


def test_findings_block_once_and_the_review_is_not_repeated(tmp_path):
    config, stubs = _config(tmp_path), Stubs([False, True, True], ["Notes:\n1. No discount field.\n2) Total wrong."])
    assert _stop(config, stubs)[0]["decision"] == "block"  # verify fails
    output, event = _stop(config, stubs, active=True)
    assert output["decision"] == "block" and "1. No discount field." in output["reason"]
    assert "2) Total wrong." in output["reason"] and event["findings"] == 2
    output, event = _stop(config, stubs, active=True)
    assert output == {} and event["gate_cap_reached"] == "review" and len(stubs.review_calls) == 1
    summary = verify_gate.summary(tmp_path / verify_gate.LOG)
    assert (summary["blocks"], summary["review_rounds"], summary["review_findings"]) == (2, 1, 2)
    assert summary["reviewer_cost_usd"] == 0.4 and summary["reviewer_usage"] == {"output_tokens": 7}


def test_stop_hook_active_without_a_block_of_ours_is_let_through_untouched(tmp_path):
    config, stubs = _config(tmp_path), Stubs([], [])
    output, event = _stop(config, stubs, active=True)
    assert output == {} and event["step"] == "foreign-block" and stubs.verify_calls == []


def test_no_verify_command_skips_to_the_review_and_records_null(tmp_path):
    config, stubs = _config(tmp_path, verify_command=None), Stubs([], ["NO_FINDINGS"])
    output, event = _stop(config, stubs)
    assert output == {} and event["verify_command"] is None and event["verify"] is None
    assert len(stubs.review_calls) == 1


def test_a_node_project_falls_back_to_its_test_script(tmp_path):
    config = _config(tmp_path, verify_command=None)
    project = Path(config["project"])
    (project / "package.json").write_text(json.dumps({"scripts": {"test": "echo \"Error: no test specified\" && exit 1"}}),
                                          encoding="utf-8")
    assert verify_gate.verify_command(config, project) is None
    (project / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8")
    assert verify_gate.verify_command(config, project) == ["npm", "test", "--silent"]


def test_a_failed_or_unparseable_review_is_a_recorded_gate_error_not_a_pass(tmp_path):
    assert verify_gate.parse_findings("NO_FINDINGS") == [] and verify_gate.parse_findings("`NO_FINDINGS`\n") == []
    assert verify_gate.parse_findings("Looks fine overall.") is None and verify_gate.parse_findings(None) is None
    config, stubs = _config(tmp_path), Stubs([True], ["I could not tell."])
    output, event = _stop(config, stubs)
    assert output == {} and event["findings"] is None
    assert event["decision"] == "gate-error" and event["gate_error"] == "review-unparsed"
    summary = verify_gate.summary(tmp_path / verify_gate.LOG)
    assert summary["review_unparsed"] == 1 and summary["gate_errors"] == 1 and summary["blocks"] == 0
    assert summary["gate_error_reasons"] == ["review-unparsed"]


def test_a_review_call_that_errors_is_a_gate_error(tmp_path):
    config = _config(tmp_path)
    stubs = Stubs([True], [None])
    stubs.review = lambda prompt, model, project, env: {"exit": 1, "timed_out": False, "seconds": 1, "is_error": True,
                                                        "text": None, "cost_usd": None, "usage": None,
                                                        "num_turns": None, "error": "boom"}
    _, event = _stop(config, stubs)
    assert event["decision"] == "gate-error" and event["gate_error"] == "review-error"


def test_the_reviewer_runs_on_the_configured_model_and_effort(tmp_path):
    argv = verify_gate.reviewer_argv("p", "claude-opus-5-5", "low")
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5" and argv[argv.index("--effort") + 1] == "low"
    config = {**_config(tmp_path), "effort": "low"}
    seen = []
    stubs = Stubs([True], ["NO_FINDINGS"])
    stubs.review = lambda prompt, model, project, env, effort=None: seen.append((model, effort)) or {
        "exit": 0, "timed_out": False, "seconds": 1, "is_error": False, "text": "NO_FINDINGS", "cost_usd": 0.1,
        "usage": None, "num_turns": 1, "error": None}
    _, event = _stop(config, stubs)
    assert seen == [("claude-opus-5-5", "low")] and event["model"] == "claude-opus-5-5" and event["effort"] == "low"
    assert verify_gate.summary(tmp_path / verify_gate.LOG)["review_models"] == ["claude-opus-5-5/low"]


def test_the_real_verify_runner_reports_exit_and_tail(tmp_path):
    passed = verify_gate.run_verify(["sh", "-c", "echo ok"], tmp_path, {"PATH": "/usr/bin:/bin"})
    failed = verify_gate.run_verify(["sh", "-c", "echo broken; exit 3"], tmp_path, {"PATH": "/usr/bin:/bin"})
    assert passed["passed"] and not failed["passed"] and failed["exit"] == 3 and "broken" in failed["tail"]


# ─── v3.18: the gate's findings follow the kit arms' review→fix rule ───

def _labels(*names, cost=0.03):
    def label(config, findings):
        assert len(findings) == len(names)
        return {"labels": [{"label": name, "why": "w"} for name in names], "cost_usd": cost}
    return label


def _review_stop(config, stubs, label):
    output, event = verify_gate.decide(config, {"session_id": "s"}, verify=stubs.verify, review=stubs.review,
                                       label=label, environ={"PATH": "/bin"})
    verify_gate.append_event(config, event)
    return output, event


def test_the_gate_blocks_only_for_incomplete_bug_and_architecture_findings_each_tagged(tmp_path):
    config = {**_config(tmp_path), "blocking_labels": ["incomplete", "bug", "architecture"]}
    stubs = Stubs([True], ["1. The refund screen is missing.\n2. Rename `x` to `total`.\n3. API imports web code."])
    output, event = _review_stop(config, stubs, _labels("incomplete", "style", "architecture"))
    assert output["decision"] == "block"
    assert "[incomplete] 1. The refund screen is missing." in output["reason"]
    assert "[architecture] 3. API imports web code." in output["reason"] and "Rename" not in output["reason"]
    assert event["label_counts"] == {"incomplete": 1, "style": 1, "architecture": 1}
    assert event["blocking_findings"] == 2 and [item["label"] for item in event["labels"]] == [
        "incomplete", "style", "architecture"]
    summary = verify_gate.summary(tmp_path / verify_gate.LOG)
    assert summary["label_cost_usd"] == 0.03 and summary["label_counts"]["style"] == 1


def test_style_only_feedback_does_not_block_the_stop(tmp_path):
    config, stubs = _config(tmp_path), Stubs([True], ["1. Rename `x`.\n2. The module is large."])
    output, event = _review_stop(config, stubs, _labels("style", "design"))
    assert output == {} and event["decision"] == "allow" and event["non_blocking"] is True
    summary = verify_gate.summary(tmp_path / verify_gate.LOG)
    assert summary["non_blocking_stops"] == 1 and summary["blocks"] == 0 and summary["gate_errors"] == 0


def test_without_labels_the_gate_blocks_on_every_finding_as_before(tmp_path):
    config, stubs = _config(tmp_path), Stubs([True], ["1. Rename `x`."])
    output, event = _review_stop(config, stubs, verify_gate.label_findings)  # no broker: no labels.json
    assert output["decision"] == "block" and "1. Rename `x`." in output["reason"] and "[" not in output["reason"]
    assert event["label_error"] and event["label_cost_usd"] == 0.0
    assert verify_gate.summary(tmp_path / verify_gate.LOG)["label_errors"] == 1


def test_the_label_broker_answers_the_hook_and_refuses_a_wrong_token(tmp_path):
    (tmp_path / "gate").mkdir()
    seen = []

    def labeler(texts):
        seen.append(texts)
        return {"labels": [{"label": "bug" if "crash" in text else "style", "why": "w"} for text in texts],
                "cost_usd": 0.02}

    config = _config(tmp_path)
    with verify_gate.LabelBroker(tmp_path / "gate", labeler) as broker:
        got = verify_gate.label_findings(config, ["1. It can crash.", "2. Bad name."])
        assert [item["label"] for item in got["labels"]] == ["bug", "style"] and got["cost_usd"] == 0.02
        client = json.loads((tmp_path / "gate" / verify_gate.LABELS_CLIENT).read_text(encoding="utf-8"))
        forged = tmp_path / "forged.json"
        forged.write_text(json.dumps({**client, "token": "0" * 32}), encoding="utf-8")
        assert "error" in verify_gate.label_findings({**config, "labels_client": str(forged)}, ["x"])
    assert seen == [["1. It can crash.", "2. Bad name."]] and broker.calls == 1 and broker.cost_usd == 0.02
    assert not (tmp_path / "gate" / verify_gate.LABELS_CLIENT).exists()
