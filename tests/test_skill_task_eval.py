from __future__ import annotations

import hashlib
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
        "id": "tenant-update",
        "expected_verdict": "request-changes",
        "expected_findings": [{
            "file": "handler.py", "start_line": 7, "end_line": 9,
            "invariant": "The trusted tenant must constrain updates; the handler permits cross-tenant writes.",
        }],
        "required_references": [],
        "forbidden_skills": [],
    }


def adjudicate(evaluator, case, response, *, cause=True, contract=True):
    return {
        "review_sha256": evaluator.review_fingerprint(case, response),
        "findings": [{"finding_index": 0, "expected_finding_index": 0,
                      "cause_correct": cause, "contract_correct": contract}],
    }


def test_invalid_model_verdict_is_a_failed_score_not_a_crash(evaluator, defect_case):
    result = evaluator.score_review(defect_case, {"verdict": [], "findings": []},
                                    host_ok=True, reads=set(), config="skill-index")
    assert result["passed"] is False


def test_right_location_does_not_prove_an_unreviewed_reason(evaluator, defect_case):
    response = {"verdict": "request-changes", "findings": [
        {"file": "handler.py", "line": 8, "reason": "SQL concatenation allows an injected query."}
    ]}
    result = evaluator.score_review(defect_case, response, host_ok=True, reads=set(), config="baseline")
    assert result["passed"] is False


def test_failed_host_cannot_receive_credit_for_a_valid_answer(evaluator, defect_case):
    response = {"verdict": "request-changes", "findings": [
        {"file": "handler.py", "line": 8, "reason": "A denied caller can mutate another tenant's record."}
    ]}
    result = evaluator.score_review(defect_case, response, host_ok=False, reads=set(), config="baseline",
                                    adjudication=adjudicate(evaluator, defect_case, response))
    assert result["passed"] is False


def test_finding_outside_the_defect_does_not_satisfy_the_oracle(evaluator, defect_case):
    response = {"verdict": "request-changes", "findings": [
        {"file": "handler.py", "line": 1, "reason": "The module name is too broad."}
    ]}
    result = evaluator.score_review(defect_case, response, host_ok=True, reads=set(), config="baseline",
                                    adjudication=adjudicate(evaluator, defect_case, response))
    assert result["defects_found"] is False
    assert result["no_false_positives"] is False


def test_alternative_evidence_satisfies_only_its_own_defect(evaluator, defect_case):
    defect_case["expected_findings"][0]["alternate_locations"] = [
        {"file": "sender.py", "start_line": 20, "end_line": 24}
    ]
    defect_case["expected_findings"][0]["invariant"] = "Failed delivery must remain durably replayable."
    response = {"verdict": "request-changes", "findings": [
        {"file": "sender.py", "line": 22, "reason": "A failed delivery leaves no durable retry record."}
    ]}
    result = evaluator.score_review(defect_case, response, host_ok=True, reads=set(), config="baseline",
                                    adjudication=adjudicate(evaluator, defect_case, response))
    assert result["passed"] is True

    defect_case["expected_findings"].append({
        "file": "auth.py", "start_line": 3, "end_line": 5, "invariant": "Reject unauthorized callers.",
    })
    result = evaluator.score_review(defect_case, response, host_ok=True, reads=set(), config="baseline",
                                    adjudication=adjudicate(evaluator, defect_case, response))
    assert result["defects_found"] is False
    assert result["no_false_positives"] is True


@pytest.mark.parametrize(("reason", "cause", "contract"), [
    ("SQL concatenation permits crossing the authenticated tenant boundary.", False, True),
    ("The tenant filter is omitted, violating the requirement to name handlers after tables.", True, False),
])
def test_wrong_cause_or_contract_cannot_receive_defect_credit(evaluator, defect_case, reason, cause, contract):
    response = {"verdict": "request-changes", "findings": [
        {"file": "handler.py", "line": 8, "reason": reason}
    ]}
    result = evaluator.score_review(
        defect_case, response, host_ok=True, reads=set(), config="baseline",
        adjudication=adjudicate(evaluator, defect_case, response, cause=cause, contract=contract),
    )
    assert result["reasons_reviewed"] is True
    assert result["defects_found"] is False
    assert result["no_false_positives"] is False
    assert result["passed"] is False


def test_correct_paraphrase_is_accepted_without_keyword_matching(evaluator, defect_case):
    response = {"verdict": "request-changes", "findings": [
        {"file": "handler.py", "line": 8,
         "reason": "변경 조건에 로그인한 사용자의 소속이 빠져 있어 다른 조직의 자료도 바뀝니다."}
    ]}
    result = evaluator.score_review(
        defect_case, response, host_ok=True, reads=set(), config="baseline",
        adjudication=adjudicate(evaluator, defect_case, response),
    )
    assert result["passed"] is True


@pytest.mark.parametrize("changed", ["response", "case"])
def test_semantic_credit_is_bound_to_exact_response_and_contract(evaluator, defect_case, changed):
    response = {"verdict": "request-changes", "findings": [
        {"file": "handler.py", "line": 8, "reason": "The update ignores the authenticated tenant."}
    ]}
    decision = adjudicate(evaluator, defect_case, response)
    if changed == "response":
        response["findings"][0]["reason"] = "Only the variable name should change."
    else:
        defect_case["expected_findings"][0]["invariant"] = "Reads are public; updates are out of scope."
    result = evaluator.score_review(defect_case, response, host_ok=True, reads=set(), config="baseline",
                                    adjudication=decision)
    assert result["reasons_reviewed"] is False
    assert result["passed"] is False


def test_model_cannot_self_adjudicate_its_reason(evaluator, defect_case):
    response = {"verdict": "request-changes", "findings": [
        {"file": "handler.py", "line": 8, "reason": "The update ignores the authenticated tenant."}
    ]}
    response["adjudication"] = adjudicate(evaluator, defect_case, response)
    result = evaluator.score_review(defect_case, response, host_ok=True, reads=set(), config="baseline")
    assert result["reasons_reviewed"] is False
    assert result["passed"] is False


def test_normal_review_without_findings_needs_no_reason_adjudication(evaluator, defect_case):
    defect_case.update(expected_verdict="approve", expected_findings=[])
    result = evaluator.score_review(defect_case, {"verdict": "approve", "findings": []},
                                    host_ok=True, reads=set(), config="baseline")
    assert result["passed"] is True


@pytest.mark.parametrize("findings", [17, True, {"file": "handler.py"}])
def test_malformed_findings_fail_without_crashing(evaluator, defect_case, findings):
    result = evaluator.score_review(defect_case, {"verdict": "request-changes", "findings": findings},
                                    host_ok=True, reads=set(), config="baseline")
    assert result["response_valid"] is False
    assert result["passed"] is False


def test_rescoring_cannot_reuse_credit_after_a_reason_changes(evaluator, defect_case, tmp_path):
    response = {"verdict": "request-changes", "findings": [
        {"file": "handler.py", "line": 8, "reason": "The update ignores the authenticated tenant filter."}
    ]}
    source = tmp_path / "recorded"
    case_path = source / "tenant-update-baseline-0" / "case.json"
    case_path.parent.mkdir(parents=True)
    case_path.write_text(json.dumps(defect_case), encoding="utf-8")
    report = {"metadata": {}, "results": [{
        "case": defect_case["id"], "config": "baseline", "trial": 0,
        "case_sha256": hashlib.sha256(json.dumps(defect_case, sort_keys=True).encode()).hexdigest(),
        "response": response, "host_ok": True, "observed_skill_reads": [],
    }]}
    report_path = source / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    labels = tmp_path / "judgments.json"
    labels.write_text(json.dumps({"reviews": [adjudicate(evaluator, defect_case, response)]}), encoding="utf-8")
    reviewed = tmp_path / "reviewed"
    result = evaluator.rescore_report(report_path, labels, reviewed)
    assert result["results"][0]["score"]["passed"] is True

    result["results"][0]["response"]["findings"][0]["reason"] = "The module needs a shorter name."
    (reviewed / "report.json").write_text(json.dumps(result), encoding="utf-8")
    rechecked = evaluator.rescore_report(reviewed / "report.json", labels, tmp_path / "rechecked")
    assert rechecked["results"][0]["score"]["reasons_reviewed"] is False
    assert rechecked["results"][0]["score"]["passed"] is False


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
