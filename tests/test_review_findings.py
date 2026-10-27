import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import review_findings as rf  # noqa: E402

REVIEW = """# claude-generalist

- status: OK
- provider: claude

## review output

```text
## Reviewer
reviewer-source: sub-agent

### Must-fix
- <severity:high> [server/app.js:12] The refund endpoint returns 200 but never marks the transaction refunded.
- <severity:med> [server/app.js:40-44] The admin CSV export route required by the contract is not implemented.
  It returns 501 for every request.

### Should-fix
- None found.

### Notes
- <severity:low> Architecture is `pending`; select it before structural work.
- Plain observation without a tag or location.
- verdict: approve

verdict: request-changes
```

## stderr

```text
noise - <severity:high> [x.js:1] not a finding
```
"""

APPROVE = REVIEW.replace("verdict: request-changes", "verdict: approve").replace(
    "never marks the transaction refunded", "marks it refunded twice")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _results(artifact: str, text: str, verdict: str) -> dict:
    return {"outcomes": [{"artifact": artifact, "artifact_sha256": _sha(text), "job_id": "claude-generalist",
                          "provider": "claude", "verdict": verdict}], "phase_id": "review"}


def _run(tmp_path: Path, name: str = "run-B-1", archive: bool = True) -> Path:
    run = tmp_path / name
    flow = run / "project" / ".git" / "agent-flow" / "worktrees" / "feat" / ".agent-flow" / "runs" / "20261025-000000"
    flow.mkdir(parents=True)
    (flow / "review-generalist-claude.md").write_text(APPROVE, encoding="utf-8")
    final = json.dumps(_results("review-generalist-claude.md", APPROVE, "approve"))
    (flow / "review-review-results.json").write_text(final, encoding="utf-8")
    if archive:
        directory = run / "review-archive"
        directory.mkdir()
        first = json.dumps(_results("review-generalist-claude.md", REVIEW, "request-changes"))
        entries = [("review-generalist-claude.md", REVIEW), ("review-review-results.json", first),
                   ("review-generalist-claude.md", APPROVE), ("review-review-results.json", final)]
        with (directory / "index.jsonl").open("w", encoding="utf-8") as index:
            for seq, (file, text) in enumerate(entries):
                (directory / f"{seq:04d}-20261025-000000-{file}").write_text(text, encoding="utf-8")
                index.write(json.dumps({"sha": _sha(text), "seq": seq, "file": file, "run_id": "20261025-000000"})
                            + "\n")
    result = {"arm": "B", "case": "demo", "declaration": "declared-complete",
              "phases_entered": ["implement", "review", "fix-loop", "review"],
              "ui": {"S": 0, "G": 0.9, "failed_checks": ["API01", "UI02@desktop", "UI02@mobile", "API03"]}}
    (run / "result.json").write_text(json.dumps(result), encoding="utf-8")
    score = {"install": {"exit": 0}, "api_failures": {"API01": "AssertionError: refund status {'status': 'PAID'}",
                                                      "API03": "AssertionError: "},
             "ui_failures": {"UI02@desktop": "export-button visible (not visible)",
                             "UI02@mobile": "locator.click: Timeout 4000ms exceeded."}}
    (run / "ui-score.json").write_text(json.dumps(score), encoding="utf-8")
    return run


def test_parse_findings_keeps_tagged_items_inside_review_output():
    items = rf.parse_findings(REVIEW)
    assert [item["section"] for item in items] == ["must-fix", "must-fix", "notes"]
    assert items[0]["severity"] == "high" and items[0]["location"] == "server/app.js:12"
    assert items[1]["location"] == "server/app.js:40-44" and "returns 501" in items[1]["text"]
    assert all("not a finding" not in item["text"] for item in items)
    assert rf.artifact_meta(REVIEW) == {"job_id": "claude-generalist", "provider": "claude",
                                        "verdict": "request-changes"}


def test_rounds_from_archive_and_final_files(tmp_path):
    rounds = rf.load_rounds(_run(tmp_path))
    assert [(r["phase"], r["seq"], r["verdict"], r["final"]) for r in rounds] == [
        ("review", 1, "request-changes", False), ("review", 2, "approve", True)]
    rows = rf.flat_findings(rounds)
    assert {row["job_id"] for row in rows} == {"claude-generalist"} and len(rows) == 6
    assert sum(row["final"] for row in rows) == 3


def test_final_only_run_numbers_round_from_phases_entered(tmp_path):
    rounds = rf.load_rounds(_run(tmp_path, archive=False))
    assert len(rounds) == 1 and rounds[0]["seq"] == 2 and rounds[0]["earlier_rounds_lost"]


def test_rule_kind():
    assert rf.rule_kind("GET /api/x: 404") == "missing"
    assert rf.rule_kind("locator.selectOption: Timeout 4000ms exceeded.") == "missing"
    assert rf.rule_kind("toast visible (not visible)") == "missing"
    assert rf.rule_kind("row count (count 0)") == "missing"
    assert rf.rule_kind("row count (count 3)") == "wrong"
    assert rf.rule_kind('title text (got "Tools")') == "wrong"
    assert rf.rule_kind("button disabled (enabled)") == "wrong"
    assert rf.rule_kind("AssertionError: {'status': 'ACTIVE'}") == "wrong"
    assert rf.rule_kind("KeyError: 'status'") == "wrong"
    assert rf.rule_kind('GET /api/x/1: status = undefined, want "EXPIRED"') == "wrong"
    assert rf.rule_kind("connect ECONNREFUSED 127.0.0.1:9") == "unclear"
    assert rf.rule_kind("AssertionError: ") == "unclear"
    assert rf.rule_kind(None) == "unclear"


def test_hidden_failures_merge_viewports_and_no_build(tmp_path):
    hidden = rf.hidden_failures(_run(tmp_path))
    rows = {row["id"]: row for row in hidden["failed"]}
    assert set(rows) == {"API01", "UI02", "API03"}
    assert rows["UI02"]["rule"] == "missing" and len(rows["UI02"]["variants"]) == 2
    assert rows["API01"]["rule"] == "wrong" and rows["API03"]["rule"] == "unclear"
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "result.json").write_text(json.dumps({"ui": {"S": 0, "G": 0.0, "failed_checks": ["A", "B@x"]}}))
    (broken / "ui-score.json").write_text(json.dumps({"install": {"exit": 1}}))
    hidden = rf.hidden_failures(broken)
    assert hidden["no_build"] and {row["rule"] for row in hidden["failed"]} == {"missing"}


def test_check_specs_from_json_and_python(tmp_path):
    browser = tmp_path / "browser.json"
    browser.write_text(json.dumps({"checks": [{"id": "UI02", "spec": "PRD 9", "title": "export button",
                                               "steps": [{"click": "export-button"}]}]}))
    module = tmp_path / "api_checks.py"
    module.write_text('def api01(s):\n    """PRD 3: refund marks the transaction."""\n\n'
                      'def api03(s):\n    return 1\n\nCHECKS = {"API01": api01, "API03": api03}\n')
    specs = rf.load_check_specs([str(browser), str(module)])
    assert specs["UI02"].startswith("PRD 9 export button") and "export-button" in specs["UI02"]
    assert specs["API01"] == "PRD 3: refund marks the transaction." and "return 1" in specs["API03"]


class FakeRunner:
    """Answers like the classifier would, from the prompt it gets; counts calls."""

    def __init__(self):
        self.calls = []

    def __call__(self, prompt: str, system: str) -> dict:
        self.calls.append(system)
        if system == rf.LABEL_PROMPT:
            items = json.loads(prompt)
            labels = []
            for item in items:
                text = item["text"]
                label = "incomplete" if "not implemented" in text else "procedure" if "Architecture" in text \
                    else "bug"
                labels.append({"id": item["id"], "label": label, "why": "fake"})
            return {"text": "```json\n" + json.dumps({"labels": labels}) + "\n```", "cost": 0.01}
        body = json.loads(prompt.split("</prd_and_contract>", 1)[1])
        refund = [f["id"] for f in body["findings"] if "refund" in f["text"]]
        csv = [f["id"] for f in body["findings"] if "CSV" in f["text"]]
        checks = [{"id": c["id"], "kind": "missing" if c["rule"] == "unclear" else c["rule"],
                   "findings": refund if c["id"] == "API01" else csv if c["id"] in ("API03", "UI02") else []}
                  for c in body["failed_checks"]]
        prd = [{"id": i, "verdict": "supported"} for i in body["judge_prd"]]
        return {"text": json.dumps({"checks": checks, "prd": prd}), "cost": 0.02}


def test_analyze_run_end_to_end_with_cache(tmp_path):
    run = _run(tmp_path)
    runner = FakeRunner()
    classifier = rf.Classifier(tmp_path / "cache.json", runner=runner, budget=1.0)
    row = rf.analyze_run(run, {}, "PRD text", classifier, total_checks=10)
    assert row["findings_final"] == {"incomplete": 1, "bug": 1, "architecture": 0, "style": 0, "design": 0,
                                     "procedure": 1, "other": 0}
    assert row["findings_all"]["bug"] == 2
    assert (row["missing"], row["wrong"], row["unclear"], row["rule_unclear"]) == (2, 1, 0, 1)
    assert row["recall"]["final_missing"] == {"hit": 2, "of": 2, "rate": 1.0}
    assert row["recall"]["any_wrong"]["hit"] == 1
    # the final round's refund finding ("marks it refunded twice") is a different text; round 1's matched API01
    assert row["recall"]["final_wrong"]["hit"] == 1
    assert row["precision"]["final_claims"] == 2 and row["precision"]["final_hidden_or_prd"] == 1.0
    assert row["false_completion"] == "false-complete-missing" and not row["complete"]
    assert row["missing_ratio"] == 0.2 and row["wrong_ratio_of_present"] == 0.125
    calls = len(runner.calls)
    again = rf.Classifier(tmp_path / "cache.json", runner=runner, budget=1.0)
    rf.analyze_run(run, {}, "PRD text", again, total_checks=10)
    assert len(runner.calls) == calls and again.spent == 0.0


def test_budget_stops_calls(tmp_path):
    classifier = rf.Classifier(None, runner=FakeRunner(), budget=0.0)
    item = {"sha": "x", "text": "a bug", "section": "must-fix", "severity": "high"}
    try:
        classifier.label([item])
    except RuntimeError as error:
        assert "budget" in str(error)
    else:
        raise AssertionError("budget not enforced")


def test_report_tables(tmp_path):
    run = _run(tmp_path)
    row = rf.analyze_run(run, {}, "", rf.Classifier(None, runner=FakeRunner()), total_checks=10)
    bare = dict(row, run="run-A-1", arm="A", findings=[], findings_total=0, review_rounds=0)
    arms = rf.summarize_arms([row, bare])
    assert arms["B"]["missing"] == 2 and arms["A"]["findings_total"] == 0
    assert arms["B"]["recall"]["final_missing"]["rate"] == 1.0
    table = rf.markdown([row, bare], arms)
    assert "| run-B-1 | B |" in table and "| A | 1 |" in table


def test_prompts_are_frozen():
    assert rf.prompt_sha(rf.LABEL_PROMPT) == rf.LABEL_PROMPT_SHA256


def test_a_field_absent_from_an_answered_response_is_wrong():
    assert rf.rule_kind("AssertionError: no price for PLAN_A: {'items': []}") == "wrong"
    assert rf.rule_kind("AssertionError: no price for PLAN_A") == "unclear"


def test_the_v318_labels_and_the_fix_rule_sets():
    assert rf.LABELS == ("incomplete", "bug", "architecture", "style", "design", "procedure", "other")
    assert rf.BLOCKING == ("incomplete", "bug", "architecture") and rf.FIXABLE == ("bug", "architecture")
    for label in rf.LABELS:
        assert f"- {label}:" in rf.LABEL_PROMPT
    assert "architecture mode or selection" not in rf.LABEL_PROMPT  # procedure is the agent-flow process only


def test_label_flow_counts_raised_fixed_at_the_next_review_and_remaining(tmp_path):
    def item(phase, order, sha, label):
        return {"phase": phase, "order": order, "sha": sha, "label": label, "final": False}
    rounds = [{"phase": "multi-review", "order": 1, "final": False}, {"phase": "multi-review", "order": 2, "final": True},
              {"phase": "architecture-review", "order": 0, "final": True}]
    findings = [item("multi-review", 1, "a", "bug"), item("multi-review", 1, "b", "style"),
                item("multi-review", 1, "c", "architecture"),
                item("multi-review", 2, "b", "style"), item("multi-review", 2, "d", "bug"),
                item("architecture-review", 0, "e", "architecture")]
    gate = [{"text": "1. x", "label": "incomplete"}, {"text": "2. y", "label": "style"}]
    flow = rf.label_flow(rounds, findings, gate)
    assert flow["bug"] == {"raised": 2, "fixed": 1, "remaining": 1}
    assert flow["style"] == {"raised": 2, "fixed": 0, "remaining": 1}
    assert flow["architecture"] == {"raised": 2, "fixed": 1, "remaining": 1}
    assert flow["incomplete"] == {"raised": 1, "fixed": 0, "remaining": 0}
    (tmp_path / "verify-gate.jsonl").write_text(json.dumps({"step": "review", "labels": gate}) + "\n" +
                                                json.dumps({"step": "done"}) + "\n", encoding="utf-8")
    assert rf.gate_findings(tmp_path) == gate
    arms = rf.summarize_arms([{"arm": "C", "label_flow": flow}, {"arm": "C", "label_flow": flow}])
    assert arms["C"]["label_flow"]["bug"] == {"raised": 4, "fixed": 2, "remaining": 2}
    table = rf.markdown([], arms)
    assert "Findings by label" in table and "| C | 2/0/0 | 4/2/2 | 4/2/2 | 4/0/2 |" in table


def test_android_chain_report_is_not_a_no_build(tmp_path):
    """The Android chain evaluator reports `B` (its build verdict), `H` and `failure_messages`; a built project with one
    failing check is not a no-build, and its messages decide the rule (#409 app chain, 2026-10-27)."""
    score = {"B": True, "H": 0.992, "failed_checks": ["C1-09"],
             "failure_messages": {"C1-09": "java.lang.AssertionError: expected:<1> but was:<0>"}}
    out = rf.failures_from(score, {})
    assert out["no_build"] is False and out["failed_count"] == 1
    assert out["failed"][0]["messages"] and out["failed"][0].get("reason") != "no-build"
    broken = rf.failures_from({"B": False, "H": 0.0, "failed_checks": ["C1-09"]}, {})
    assert broken["no_build"] is True and broken["missing"] == 1
