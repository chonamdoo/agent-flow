from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def evaluator(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "evals"))
    import skill_tasks

    return skill_tasks


@pytest.fixture
def defect_case():
    return {
        "expected_verdict": "request-changes",
        "expected_findings": [{"file": "handler.py", "start_line": 7, "end_line": 9}],
        "required_references": [],
        "forbidden_skills": [],
    }


def test_invalid_model_verdict_is_a_failed_score_not_a_crash(evaluator, defect_case):
    result = evaluator.score_review(defect_case, {"verdict": [], "findings": []},
                                    host_ok=True, reads=set(), config="skill-index")
    assert result["passed"] is False


def test_failed_host_cannot_receive_credit_for_a_valid_answer(evaluator, defect_case):
    response = {"verdict": "request-changes", "findings": [
        {"file": "handler.py", "line": 8, "reason": "A denied caller can mutate another tenant's record."}
    ]}
    result = evaluator.score_review(defect_case, response, host_ok=False, reads=set(), config="baseline")
    assert result["passed"] is False


def test_finding_outside_the_defect_does_not_satisfy_the_oracle(evaluator, defect_case):
    response = {"verdict": "request-changes", "findings": [
        {"file": "handler.py", "line": 1, "reason": "The module name is too broad."}
    ]}
    result = evaluator.score_review(defect_case, response, host_ok=True, reads=set(), config="baseline")
    assert result["defects_found"] is False
    assert result["no_false_positives"] is False


def test_alternative_evidence_satisfies_only_its_own_defect(evaluator, defect_case):
    defect_case["expected_findings"][0]["alternate_locations"] = [
        {"file": "sender.py", "start_line": 20, "end_line": 24}
    ]
    response = {"verdict": "request-changes", "findings": [
        {"file": "sender.py", "line": 22, "reason": "A failed delivery leaves no durable retry record."}
    ]}
    result = evaluator.score_review(defect_case, response, host_ok=True, reads=set(), config="baseline")
    assert result["passed"] is True

    defect_case["expected_findings"].append({"file": "auth.py", "start_line": 3, "end_line": 5})
    result = evaluator.score_review(defect_case, response, host_ok=True, reads=set(), config="baseline")
    assert result["defects_found"] is False
    assert result["no_false_positives"] is True


def test_reference_self_report_is_not_an_executed_read(evaluator, tmp_path, monkeypatch):
    reference = tmp_path / "skills/demo/references/policy.md"
    reference.parent.mkdir(parents=True)
    reference.write_text("# Policy\nAuthorization uses the trusted execution principal.\n", encoding="utf-8")
    monkeypatch.setattr(evaluator, "KIT_ROOT", tmp_path)
    events = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Read demo/references/policy.md"}},
        {"type": "item.completed", "item": {
            "type": "command_execution", "exit_code": 0,
            "command": "echo 'cat .agent-flow/skills/demo/references/policy.md'",
            "aggregated_output": "cat .agent-flow/skills/demo/references/policy.md",
        }},
    ]
    assert evaluator.observed_skill_reads("\n".join(json.dumps(event) for event in events)) == set()


def test_executed_reference_read_is_observed(evaluator, tmp_path, monkeypatch):
    reference = tmp_path / "skills/demo/references/policy.md"
    reference.parent.mkdir(parents=True)
    content = "# Policy\nAuthorization uses the trusted execution principal.\n"
    reference.write_text(content, encoding="utf-8")
    monkeypatch.setattr(evaluator, "KIT_ROOT", tmp_path)
    event = {"type": "item.completed", "item": {
        "type": "command_execution", "exit_code": 0,
        "command": "/bin/zsh -lc 'cat .agent-flow/skills/demo/references/policy.md'",
        "aggregated_output": content,
    }}
    assert evaluator.observed_skill_reads(json.dumps(event)) == {"demo/references/policy.md"}


def test_conditional_reference_is_available_in_the_evaluation_project(evaluator, tmp_path, monkeypatch):
    import configs

    source = tmp_path / "source"
    skill = source / "skills/demo"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Transaction recovery review.\n---\n"
        "[Recovery](references/recovery.md)\n", encoding="utf-8",
    )
    content = "After an ambiguous write, reconcile the durable operation before retrying.\n"
    (skill / "references/recovery.md").write_text(content, encoding="utf-8")
    monkeypatch.setattr(configs, "KIT_ROOT", source)
    project = tmp_path / "project"
    evaluator.prepare_skill_index(project)
    index = json.loads((project / ".agent-flow/skills/index.json").read_text(encoding="utf-8"))
    entry = project / index["skills"][0]["path"]
    assert (entry.parent / "references/recovery.md").read_text(encoding="utf-8") == content
