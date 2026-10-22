"""#410 arm comparison harness: same seed per arm, no hidden tests in the agent's kit, honest scoring."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
import arm_compare  # noqa: E402


def _transcript(path: Path, *, tool_input: dict, final: str) -> Path:
    rows = [
        {"type": "assistant", "timestamp": "2026-10-22T07:00:00Z",
         "message": {"id": "m1", "model": "claude-x", "content": [{"type": "tool_use", "name": "Read", "input": tool_input}],
                     "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 100,
                               "output_tokens": 20}}},
        {"type": "assistant", "timestamp": "2026-10-22T07:01:40Z",
         "message": {"id": "m2", "model": "claude-x", "content": [{"type": "text", "text": final}],
                     "usage": {"input_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 50,
                               "output_tokens": 3}}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def bare(tmp_path_factory):
    run = tmp_path_factory.mktemp("arm") / "A-1"
    arm_compare.prepare(ROOT, "A", "devflow", 1, run)
    return run


def test_the_agent_kit_holds_only_installed_asset_trees(bare):
    kit = bare / "kit"
    assert (kit / "bin" / "agent-flow-kit.mjs").is_file() and (kit / "src" / "agent_flow").is_dir()
    for absent in ("evals", "tests", "maintainer", ".git", "tools"):
        assert not (kit / absent).exists(), absent


def test_bare_arm_gets_task_tests_and_declaration_only(bare):
    prompt = (bare / "prompt.md").read_text(encoding="utf-8")
    case = json.loads((ROOT / "evals" / "phase-cases" / "devflow" / "case.json").read_text(encoding="utf-8"))
    assert case["task"] in prompt and "EVAL-STATUS: complete" in prompt
    assert " ".join(case["test_command"]) in prompt
    for marker in (*arm_compare.HIDDEN_MARKERS, "agent-flow"):
        assert marker not in prompt
    project = bare / "project"
    assert not (project / ".agent-flow").exists()
    assert arm_compare._git(["branch", "--show-current"], project) == arm_compare.BRANCH
    assert not (project / "tests" / "hidden_fix").exists()


def test_workflow_arm_installs_the_kit_and_drives_the_runner(tmp_path):
    run = tmp_path / "C-1"
    meta = arm_compare.prepare(ROOT, "C", "devflow", 1, run)
    prompt = (run / "prompt.md").read_text(encoding="utf-8")
    assert "--workflow full-feature" in prompt and "EVAL-STATUS" in prompt
    assert (run / "project" / ".agent-flow" / "bin" / "agent-flow").exists()
    assert arm_compare._git(["branch", "--show-current"], run / "project") == "main"
    assert meta["prompt_sha256"] and meta["start_commit"]


def test_score_runs_hidden_tests_after_the_agent_and_flags_hidden_reads(bare, tmp_path):
    clean = _transcript(tmp_path / "clean.jsonl", tool_input={"file_path": "src/shop/cart/domain.py"},
                        final="Nothing done.\n\nEVAL-STATUS: complete")
    result = arm_compare.score(bare, clean)
    # 구현하지 않은 seed에서는 hidden test가 실패한다. 완료 선언은 그대로 읽는다(과대보고 후보).
    assert result["oracle"] is False and result["hidden_tests"]["fix"] is False
    assert result["declaration"] == "declared-complete" and result["contaminated"] is False
    assert result["usage"] == {"input": 166, "uncached_input": 16, "cached_input": 150, "output": 23}
    assert result["seconds"] == 100
    dirty = _transcript(tmp_path / "dirty.jsonl", tool_input={"file_path": "/x/evals/phase-cases/devflow/author/clean/hidden/t.py"},
                        final="EVAL-STATUS: complete")
    assert arm_compare.score(bare, dirty)["contaminated"] is True


def test_summary_leaves_contaminated_runs_out_and_compares_with_a():
    def row(arm, oracle, contaminated=False):
        return {"arm": arm, "task_kind": "normal", "oracle": oracle, "contaminated": contaminated,
                "final_message": "EVAL-STATUS: complete", "declaration": "declared-complete",
                "usage": {"input": 1, "uncached_input": 1, "cached_input": 0, "output": 10}, "seconds": 5}
    rows = [row("A", False), row("A", False), row("C", True), row("C", True), row("C", True, contaminated=True)]
    table = arm_compare.summarize(rows)
    c_line = next(line for line in table.splitlines() if line.startswith("| C |"))
    assert "| 2 | 2/2 |" in c_line and "| 1 |" in c_line
    assert "0.167" in c_line


def test_a_subagent_handback_is_the_final_message():
    rows = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "SubagentHandback", "input": {"message": "Done.\n\nEVAL-STATUS: complete"}}]}}]
    assert arm_compare.final_message(rows).endswith("EVAL-STATUS: complete")


def test_the_runner_command_quotes_the_task_for_the_shell():
    import shlex
    task = 'A subtotal of US$100.00 gets a "discount"'
    prompt = arm_compare.driver_prompt(task, "full-feature")
    command = next(line.strip() for line in prompt.splitlines() if line.strip().startswith(".agent-flow/bin/agent-flow run"))
    assert shlex.split(command)[2] == task


def test_missing_usage_is_null_not_zero():
    assert arm_compare._usage_totals([])["usage"] == {"input": None, "uncached_input": None, "cached_input": None, "output": None}


def test_the_driver_is_not_the_human_approver():
    prompt = arm_compare.driver_prompt("Add a discount", "full-feature")
    assert "do not run" in prompt and "spec approve" in prompt and "spec confirm" in prompt
    assert "approve, and run the printed approval" not in prompt


def test_human_only_approvals_fail_the_run_and_are_read_from_disk(tmp_path):
    project, checkout = tmp_path / "project", tmp_path / "wt"
    record = project / ".git" / "agent-flow" / "worktrees" / "w" / ".agent-flow" / "runs" / "r1"
    record.mkdir(parents=True)
    (record / arm_compare.MANUAL_APPROVALS).write_text(
        json.dumps({"approvals": [{"spec_id": "SPEC-1"}, {"spec_id": "SPEC-2"}]}), encoding="utf-8")
    assert arm_compare.manual_spec_approvals(project, checkout) == 2
    assert arm_compare.VERIFY_DOWNGRADE.search(json.dumps({"command": "sed -i 's/^verify: test:.*$/verify: manual/' prd.md"}))

    def row(arm, **extra):
        return {"arm": arm, "task_kind": "normal", "oracle": True, "contaminated": False,
                "final_message": "EVAL-STATUS: complete", "declaration": "declared-complete",
                "usage": {"input": 1, "uncached_input": 1, "cached_input": 0, "output": 1}, "seconds": 1, **extra}
    table = arm_compare.summarize([row("A"), row("C"), row("C", manual_spec_approvals=2), row("C", verify_downgrades=1)])
    c_line = next(line for line in table.splitlines() if line.startswith("| C |"))
    assert "| 1/3 |" in c_line and c_line.endswith("| 2 | instructed |")


def test_the_artifact_fingerprint_ignores_runner_state_and_tracks_edits(bare):
    project = bare / "project"
    before = arm_compare.artifact_sha256(project)
    runs = project / ".agent-flow" / "runs" / "r1"
    runs.mkdir(parents=True)
    (runs / "events.jsonl").write_text("{}\n", encoding="utf-8")
    assert arm_compare.artifact_sha256(project) == before
    (project / "NOTE.md").write_text("new\n", encoding="utf-8")
    assert arm_compare.artifact_sha256(project) != before
    (project / "NOTE.md").unlink()


def test_isolation_is_recorded_and_checked(tmp_path):
    meta = arm_compare.prepare(ROOT, "A", "devflow", 1, tmp_path / "A-9", isolation="enforced")
    assert meta["isolation"] == "enforced"
    with pytest.raises(ValueError):
        arm_compare.prepare(ROOT, "A", "devflow", 1, tmp_path / "A-10", isolation="trust-me")
