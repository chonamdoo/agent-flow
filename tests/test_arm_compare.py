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
    assert result["usage_components"] == {"fresh_input": 11, "cache_write": 5, "cache_write_5m": None,
                                          "cache_write_1h": None, "cache_read": 150, "output": 23}
    assert result["seconds"] == 100
    dirty = _transcript(tmp_path / "dirty.jsonl", tool_input={"file_path": "/x/evals/phase-cases/devflow/author/clean/hidden/t.py"},
                        final="EVAL-STATUS: complete")
    assert arm_compare.score(bare, dirty)["contaminated"] is True


def _summary_row(arm, oracle=True, *, declaration="declared-complete", final="EVAL-STATUS: complete", **extra):
    return {"arm": arm, "task_kind": "normal", "oracle": oracle, "contaminated": False, "final_message": final,
            "declaration": declaration, "usage": {"input": 1, "uncached_input": 1, "cached_input": 0, "output": 10},
            "seconds": 5, **extra}


def test_success_needs_a_completion_declaration_and_a_clean_run():
    # PREREGISTRATION.md: S=1 and a completion declaration and no rule violation and not contaminated.
    assert arm_compare._succeeded(_summary_row("C"))
    assert not arm_compare._succeeded(_summary_row("C", declaration="undeclared", final="Done."))
    assert not arm_compare._succeeded(_summary_row("C", declaration="unmeasured", final=None))
    assert not arm_compare._succeeded(_summary_row("C", contaminated=True))
    assert not arm_compare._succeeded(_summary_row("C", drive_timed_out=True))


def test_summary_keeps_every_assigned_run_in_the_denominator():
    rows = [_summary_row("A", False), _summary_row("A", False),
            _summary_row("C"), _summary_row("C"), _summary_row("C", contaminated=True),
            _summary_row("C", declaration="undeclared", final="Done."), _summary_row("C", drive_exit=1),
            _summary_row("C", harness_error="session env leaked into the controller", trial=9)]
    table = arm_compare.summarize(rows)
    c_line = next(line for line in table.splitlines() if line.startswith("| C |"))
    # 다섯 배정 run 중 성공 둘. 오염·인프라 오류·무선언은 분모에 남고 실패다. harness 오류만 따로 센다.
    assert "| C | 5 | 2/5 |" in c_line
    assert "| 1 / 1 / 0 |" in c_line and "| 1 / 1 / 0 | 1 |" in c_line
    assert "C-9: session env leaked into the controller" in table


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
    assert "| 1/3 |" in c_line and c_line.endswith("| 2 | instructed | 0 / 0 | 1 / 0 | 0 |")


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


def _uses(*inputs):
    return [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": name, "input": data} for name, data in inputs]}}]


def test_verify_downgrades_read_the_written_text_not_the_serialized_input():
    upgrade = ("Edit", {"file_path": "prd.md", "old_string": "verify: manual", "new_string": "verify: test:tests/t.py"})
    quoted = ("Edit", {"file_path": "prd.md", "old_string": "verify: test:x", "new_string": 'verify: "manual"'})
    sed = ("Bash", {"command": "sed -i 's/^verify: test:.*$/verify: manual/' prd.md"})
    multi = ("MultiEdit", {"file_path": "prd.md", "edits": [{"old_string": "verify: test:a", "new_string": "verify: manual"}]})
    read = ("Read", {"file_path": "verify: manual"})
    assert arm_compare.verify_downgrades(_uses(upgrade, read)) == 0
    assert arm_compare.verify_downgrades(_uses(quoted, sed, multi)) == 3


def test_scoring_twice_leaves_the_artifact_and_its_fingerprint_unchanged(tmp_path):
    run = tmp_path / "A-1"
    arm_compare.prepare(ROOT, "A", "devflow", 1, run)
    first = arm_compare.score(run, None)
    second = arm_compare.score(run, None)
    assert first["artifact_sha256"] == second["artifact_sha256"]
    assert not (run / "project" / "tests" / "hidden_fix").exists()


def test_the_ui_task_seeds_only_the_layer_and_the_pinned_fixtures(tmp_path):
    run = tmp_path / "A-oyg"
    meta = arm_compare.prepare(ROOT, "A", "oyg", 1, run)
    project = run / "project"
    names = sorted(str(path.relative_to(project)) for path in project.rglob("*")
                   if path.is_file() and ".git" not in path.parts)
    assert names == ["contract/interface.md", "contract/openapi.yaml", "fixtures/catalog.json",
                     "fixtures/commerce.json", "prd.md"]
    assert meta["case_kind"] == "ui" and meta["layer"] == "prd"
    prompt = (run / "prompt.md").read_text(encoding="utf-8")
    assert "prd.md" in prompt and "EVAL-STATUS" in prompt and "agent-flow" not in prompt
    with pytest.raises(SystemExit):
        arm_compare.score(run, None)


def test_every_arm_gets_the_same_headless_controller_without_a_bypass_flag():
    a = arm_compare.controller_argv("task A", "claude-opus-5-5", 30.0)
    c = arm_compare.controller_argv("task C", "claude-opus-5-5", 30.0)
    assert a[3:] == c[3:] and a[:2] == ["claude", "-p"]
    assert not any("dangerously" in item or item == "bypassPermissions" for item in a)
    assert a[a.index("--permission-mode") + 1] == "auto"
    assert a[a.index("--max-budget-usd") + 1] == "30"
    assert a[a.index("--add-dir") + 1] == str(arm_compare.AGENT_FLOW_WORKTREES)


def test_drive_reads_the_session_and_cost_from_the_stream(tmp_path):
    stream = tmp_path / "controller.jsonl"
    stream.write_text(json.dumps({"type": "system", "subtype": "init", "session_id": "s-1"}) + "\n" +
                      json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 1.25, "num_turns": 4,
                                  "is_error": False}) + "\n", encoding="utf-8")
    summary = arm_compare._stream_summary(stream)
    assert summary["session_id"] == "s-1" and summary["cost_usd"] == 1.25
    projects = tmp_path / "projects"
    (projects / "-x-project").mkdir(parents=True)
    (projects / "-x-project" / "s-1.jsonl").write_text("{}\n", encoding="utf-8")
    assert arm_compare.session_transcript("s-1", projects) == projects / "-x-project" / "s-1.jsonl"
    assert arm_compare.session_transcript(None, projects) is None


def test_the_controller_does_not_inherit_the_operators_session():
    env = arm_compare.controller_env({"PATH": "/bin", "HOME": "/root", "CLAUDE_CODE_SESSION_ID": "operator",
                                      "CLAUDE_CODE_REMOTE": "true", "CLAUDE_EFFORT": "low", "HTTPS_PROXY": "http://p"})
    assert env == {"PATH": "/bin", "HOME": "/root", "HTTPS_PROXY": "http://p", "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1"}


def test_running_the_runners_approval_command_is_self_approval():
    for command in (".agent-flow/bin/agent-flow continue --root /p --worktree w --approve abc123",
                    "agent-flow spec approve SPEC-1 --run-dir r"):
        assert arm_compare.SELF_APPROVAL.search(command)
    assert not arm_compare.SELF_APPROVAL.search(".agent-flow/bin/agent-flow continue --root /p --worktree w")
    prompt = arm_compare.driver_prompt("Add a discount", "full-feature")
    assert "evaluator approves" in prompt and "do not run its approval command" in prompt


def test_the_harness_runs_only_the_runners_own_approval_command(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(arm_compare.c_prime, "_sh", lambda argv, cwd, **_: calls.append(argv) or
                        type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    status = {"approval_command": "agent-flow continue --root /p --worktree w --approve abc", "worktree": str(tmp_path)}
    assert arm_compare.approve_phase(tmp_path, status)
    assert calls[-1] == [str(tmp_path / ".agent-flow" / "bin" / "agent-flow"), "continue", "--root", "/p",
                         "--worktree", "w", "--approve", "abc"]
    assert not arm_compare.approve_phase(tmp_path, {"approval_command": "rm -rf /", "worktree": str(tmp_path)})
    assert not arm_compare.approve_phase(tmp_path, {"approval_command": None, "worktree": str(tmp_path)})


def test_the_ui_task_installs_the_kit_with_a_declared_architecture(tmp_path):
    run = tmp_path / "C-oyg"
    arm_compare.prepare(ROOT, "C", "oyg", 1, run)
    kit = json.loads((run / "project" / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
    assert kit["architecture"]["mode"] == "clean"


def _session_with_subagent(tmp_path, sub_input):
    main = _transcript(tmp_path / "s1.jsonl", tool_input={"file_path": "src/shop/cart/domain.py"},
                       final="Done.\n\nEVAL-STATUS: complete")
    folder = tmp_path / "s1" / "subagents"
    folder.mkdir(parents=True)
    rows = [{"type": "assistant", "isSidechain": True, "timestamp": "2026-10-22T07:00:30Z",
             "message": {"id": "sub-1", "model": "claude-x", "content": [
                 {"type": "tool_use", "name": "Bash", "input": sub_input},
                 {"type": "text", "text": "Sub-agent done.\n\nEVAL-STATUS: blocked: not mine to say"}],
                 "usage": {"input_tokens": 7, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                           "output_tokens": 70}}}]
    (folder / "agent-abc.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return main


def test_subagent_tokens_count_and_its_last_words_are_not_the_final_message(bare, tmp_path):
    transcript = _session_with_subagent(tmp_path, {"command": "ls"})
    result = arm_compare.score(bare, transcript)
    assert result["usage"]["output"] == 23 + 70
    assert result["subagent_files"] == 1 and result["subagents"] == "read"
    assert result["declaration"] == "declared-complete"


def test_a_hidden_path_read_only_by_a_subagent_is_contamination(bare, tmp_path):
    transcript = _session_with_subagent(tmp_path, {"command": "cat /x/evals/phase-cases/devflow/author/clean/hidden/t.py"})
    assert arm_compare.score(bare, transcript)["contaminated"] is True


def test_an_unreadable_subagent_folder_leaves_usage_unmeasured(bare, tmp_path, monkeypatch):
    transcript = _session_with_subagent(tmp_path, {"command": "ls"})
    monkeypatch.setattr(arm_compare.run_usage, "subagent_transcripts", lambda path: ([], "unreadable"))
    result = arm_compare.score(bare, transcript)
    assert result["subagents"] == "unmeasured" and result["usage"]["output"] is None


def test_the_cost_estimate_uses_each_token_kind_at_its_own_price():
    parts = {"fresh_input": 1_000_000, "cache_write": 2_000_000, "cache_write_5m": 1_000_000,
             "cache_write_1h": 1_000_000, "cache_read": 1_000_000, "output": 1_000_000}
    assert arm_compare.message_cost("claude-opus-5-5", parts) == 4.0 + 5.0 + 8.0 + 0.20 + 20.0
    assert arm_compare.message_cost("claude-unknown", parts) is None
    assert arm_compare.message_cost("claude-opus-5-5", {**parts, "cache_write_5m": None}) is None


def _git_init(path):
    path.mkdir(parents=True)
    for args in (["init", "-q", "-b", "main"], ["config", "user.name", "t"], ["config", "user.email", "t@e"],
                 ["config", "commit.gpgsign", "false"], ["commit", "-q", "--allow-empty", "-m", "seed"]):
        arm_compare._git(args, path)


def test_the_runner_worktree_is_graded_even_when_a_newer_one_exists(tmp_path):
    import os, time
    project = tmp_path / "project"
    _git_init(project)
    worked = tmp_path / "wt" / "feat-x"
    arm_compare._git(["worktree", "add", "-q", "-b", "feat/x", str(worked)], project)
    (project / ".git" / "agent-flow" / "worktrees" / "feat-x" / ".agent-flow" / "runs" / "r1").mkdir(parents=True)
    (worked / "work.txt").write_text("done\n", encoding="utf-8")
    later = tmp_path / "wt" / "scratch"
    arm_compare._git(["worktree", "add", "-q", "-b", "scratch", str(later)], project)
    future = time.time() + 60
    os.utime(later, (future, future))
    chosen, info = arm_compare.select_checkout(project, "C")
    assert chosen == worked and info["checkout_selected_by"] == "runner-state" and info["linked_worktrees"] == 2
    assert arm_compare.select_checkout(project, "A")[0] == project


def test_an_undecidable_worktree_is_a_harness_error(tmp_path):
    project = tmp_path / "project"
    _git_init(project)
    arm_compare._git(["worktree", "add", "-q", "-b", "a", str(tmp_path / "wt" / "a")], project)
    chosen, info = arm_compare.select_checkout(project, "C")
    assert chosen == project and info["checkout_error"] == "0 runner worktrees among 1 linked worktrees"


def test_a_moved_run_is_graded_on_its_own_project(tmp_path):
    first = tmp_path / "A-1"
    arm_compare.prepare(ROOT, "A", "devflow", 1, first)
    kept = tmp_path / "harness-error-A-1"
    first.rename(kept)
    rerun = arm_compare.prepare(ROOT, "A", "devflow", 1, first)
    (first / "project" / "work.txt").write_text("rerun\n", encoding="utf-8")
    arm_compare._git(["add", "work.txt"], first / "project")
    arm_compare._git(["-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "rerun work"], first / "project")
    result = arm_compare.score(kept, None, harness_error="moved aside")
    assert result["project"] == str((kept / "project").resolve()) and result["project_recorded"] == rerun["project"]
    assert result["commits"] == 0
    assert "project_recorded" not in arm_compare.score(first, None)


def test_a_worktree_path_reused_by_a_later_project_is_not_graded(tmp_path):
    # An attempt moved aside and re-prepared at the same path: the rerun's runner recreates its worktree where the
    # attempt's stale registration still points.
    project, worktree = tmp_path / "C-1" / "project", tmp_path / "wt" / "feat-x"
    _git_init(project)
    arm_compare._git(["worktree", "add", "-q", "-b", "feat/x", str(worktree)], project)
    (project / ".git" / "agent-flow" / "worktrees" / "feat-x" / ".agent-flow" / "runs" / "r1").mkdir(parents=True)
    attempt = tmp_path / "harness-error-C-1"
    attempt.mkdir()
    (tmp_path / "C-1").rename(attempt / "C-1")
    import shutil
    shutil.rmtree(worktree)
    rerun = tmp_path / "C-1" / "project"
    _git_init(rerun)
    arm_compare._git(["worktree", "add", "-q", "-b", "feat/x", str(worktree)], rerun)
    (rerun / ".git" / "agent-flow" / "worktrees" / "feat-x" / ".agent-flow" / "runs" / "r1").mkdir(parents=True)
    chosen, info = arm_compare.select_checkout(attempt / "C-1" / "project", "C")
    assert chosen == attempt / "C-1" / "project" and info["linked_worktrees"] == 0
    assert arm_compare.select_checkout(rerun, "C")[0] == worktree


def test_a_gate_deferred_to_ci_is_recorded_as_never_run(tmp_path):
    project, checkout = tmp_path / "project", tmp_path / "wt"
    ledger = project / ".git" / "agent-flow" / "worktrees" / "w" / ".agent-flow" / "runs" / "r1" / "artifacts"
    ledger.mkdir(parents=True)
    (ledger / "gate-results.json").write_text(json.dumps({
        "status": "green", "execution": "local", "deferred_ci_checks": ["pytest"],
        "results": [{"gate_id": "architecture-lint", "required": True, "passed": True},
                    {"gate_id": "lint", "required": False, "passed": False}]}), encoding="utf-8")
    record = arm_compare.gate_record(project, checkout)
    assert record == {"gate_ledgers": 1, "local_gates": [["architecture-lint", True, True], ["lint", False, False]],
                      "deferred_ci_checks": ["pytest"], "required_local_gates_ran": 1}
    assert arm_compare.gate_record(tmp_path / "none", checkout)["local_gates"] is None
    row = _summary_row("C", gate_ledgers=1, deferred_ci_checks=["pytest"])
    c_line = next(line for line in arm_compare.summarize([row]).splitlines() if line.startswith("| C |"))
    assert c_line.endswith("| 1 / 1 | 1 / 0 | 0 |")


def test_a_rule_counts_only_where_the_runs_prompt_stated_it():
    # 1차 프롬프트는 에이전트에게 phase 승인을 맡겼다. 지금 규칙으로 그 run을 소급해 위반 처리하지 않는다.
    assert not arm_compare.rule_violation(_summary_row("C", phase_self_approvals=3))
    assert arm_compare.rule_violation(_summary_row("C", phase_self_approvals=1,
                                                   approval_rules=arm_compare.HARNESS_APPROVES))
    assert arm_compare.rule_violation(_summary_row("C", spec_self_approvals=1))
    # C′는 runner 없이 자기 SPEC을 쓴다. verify를 manual로 적는 것은 우회할 gate가 없는 편집이다.
    assert not arm_compare.rule_violation(_summary_row("Cp", verify_downgrades=2))


def test_new_workflow_runs_record_that_the_harness_approves(tmp_path):
    meta = arm_compare.prepare(ROOT, "C", "devflow", 1, tmp_path / "C-1")
    assert meta["approval_rules"] == arm_compare.HARNESS_APPROVES


CAP_STDOUT = """  [skip] multi-review
  [block] fix-loop exceeded 3 rounds routing multi-review -> fix-loop

status: blocked
status_json: {"current_phase": "multi-review", "reason": "route_blocked", "run": "full-feature/20261022-131409", "next_command": "agent-flow continue --root /p --worktree w"}
"""


def test_the_runner_status_reports_a_stop_at_the_fix_loop_limit(tmp_path):
    status = arm_compare.parse_runner_status(CAP_STDOUT, tmp_path)
    assert status["fix_loop_cap"] == {"rounds": 3, "phase": "multi-review", "target": "fix-loop"}
    approval = ('status_json: {"current_phase": "prd", "reason": "phase_approval_required", '
                '"approval_command": "agent-flow continue --root /p --worktree w --approve abc"}')
    assert arm_compare.parse_runner_status(approval, tmp_path)["fix_loop_cap"] is None
    # A blocked route for another reason (a gate) is not the fix-loop limit.
    other = 'status_json: {"current_phase": "gates", "reason": "route_blocked"}'
    assert arm_compare.parse_runner_status(other, tmp_path)["fix_loop_cap"] is None


def _drive_run(tmp_path, monkeypatch, statuses, finals):
    run = tmp_path / "C-1"
    (run / "project").mkdir(parents=True)
    (run / "arm.json").write_text(json.dumps({"arm": "C", "project": str(run / "project")}), encoding="utf-8")
    (run / "prompt.md").write_text("task", encoding="utf-8")
    messages = []

    def controller_once(argv, project, out, err, timeout):
        messages.append(argv[2])
        final = finals[min(len(messages), len(finals)) - 1]
        out.write((json.dumps({"type": "system", "subtype": "init", "session_id": "s-1"}) + "\n" +
                   json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 1.0 * len(messages),
                               "result": final}) + "\n").encode())
        return 0, False

    queue = list(statuses)
    monkeypatch.setattr(arm_compare, "_controller_once", controller_once)
    monkeypatch.setattr(arm_compare, "runner_status", lambda project: queue.pop(0) if queue else None)
    monkeypatch.setattr(arm_compare, "review_findings",
                        lambda worktree, status: {"artifact": "sha", "verdicts": {"request-changes": 2},
                                                  "findings": ["high: x"], "findings_total": 1})
    monkeypatch.setattr(arm_compare.c_prime, "_sh", lambda argv, cwd, **_:
                        type("R", (), {"returncode": 0, "stdout": "2.1.308", "stderr": ""})())
    record = arm_compare.drive(run, "claude-opus-5-5", 600, 30.0)
    return record, messages


def _cap_status(tmp_path):
    return {**arm_compare.parse_runner_status(CAP_STDOUT, tmp_path), "worktree": str(tmp_path)}


def test_the_harness_reapproves_a_fix_loop_stop_at_most_twice(tmp_path, monkeypatch):
    blocked = "Stopped.\n\nEVAL-STATUS: blocked: fix-loop limit"
    record, messages = _drive_run(tmp_path, monkeypatch, [_cap_status(tmp_path)] * 5, [blocked])
    assert len(messages) == 3
    assert all("fix-loop limit at phase `multi-review`" in message for message in messages[1:])
    assert "Do not change code" in messages[1]
    reapprovals = record["fix_loop_reapprovals"]
    assert len(reapprovals) == 2 and [item["passed"] for item in reapprovals] == [False, False]
    assert reapprovals[0]["findings"] == ["high: x"] and reapprovals[0]["phase"] == "multi-review"
    assert record["fix_loop_cap_stop"] is True


def test_a_reapproval_that_lets_the_run_finish_is_recorded_as_passed(tmp_path, monkeypatch):
    record, messages = _drive_run(tmp_path, monkeypatch, [_cap_status(tmp_path)],
                                  ["Stopped.\n\nEVAL-STATUS: blocked: fix-loop limit", "Done.\n\nEVAL-STATUS: complete"])
    assert len(messages) == 2
    assert [item["passed"] for item in record["fix_loop_reapprovals"]] == [True]
    assert record["fix_loop_cap_stop"] is False


def test_the_bare_arm_is_never_reapproved(tmp_path, monkeypatch):
    run = tmp_path / "C-1"
    _drive_run(tmp_path, monkeypatch, [], ["Done.\n\nEVAL-STATUS: complete"])
    (run / "arm.json").write_text(json.dumps({"arm": "A", "project": str(run / "project")}), encoding="utf-8")
    statuses = [_cap_status(tmp_path)] * 3
    monkeypatch.setattr(arm_compare, "runner_status", lambda project: statuses.pop(0))
    record = arm_compare.drive(run, "claude-opus-5-5", 600, 30.0)
    assert record["fix_loop_reapprovals"] == [] and len(statuses) == 3


def test_a_run_stopped_at_the_limit_is_read_from_runner_state(tmp_path):
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"phase_blocked_reason": "route_blocked", "fix_loop_rounds": {"fix-loop": 3}}),
                    encoding="utf-8")
    assert arm_compare.stopped_at_fix_loop_cap(meta)
    meta.write_text(json.dumps({"phase_blocked_reason": "route_blocked", "fix_loop_rounds": {"fix-loop": 1}}),
                    encoding="utf-8")
    assert not arm_compare.stopped_at_fix_loop_cap(meta)
    assert not arm_compare.stopped_at_fix_loop_cap(tmp_path / "missing.json")


def test_the_summary_separates_first_try_successes_from_reapproved_ones():
    rows = [_summary_row("A"), _summary_row("C"), _summary_row("C", fix_loop_reapprovals=1),
            _summary_row("C", oracle=False, declaration="declared-blocked", final="EVAL-STATUS: blocked: limit",
                         fix_loop_reapprovals=2, fix_loop_cap_stop=True)]
    c_line = next(line for line in arm_compare.summarize(rows).splitlines() if line.startswith("| C |"))
    assert c_line.endswith("| 1 / 1 | 1 |")
