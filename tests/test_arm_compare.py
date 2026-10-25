"""#410 arm comparison harness: same seed per arm, no hidden tests in the agent's kit, honest scoring."""
from __future__ import annotations

import hashlib
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
    assert "| 1/3 |" in c_line and c_line.endswith("| 2 | instructed | 0 / 0 | 1 / 0 | 0 | - | - | - | - | - |")


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
    assert c_line.endswith("| 1 / 1 | 1 / 0 | 0 | - | - | - | - | - |")


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


def _external_case(tmp_path):
    seed = tmp_path / "seed-repo"
    _git_init(seed)
    (seed / "app.txt").write_text("seed app\n", encoding="utf-8")
    arm_compare._git(["add", "app.txt"], seed)
    arm_compare._git(["commit", "-qm", "app"], seed)
    case_dir = tmp_path / "private" / "mobile"
    (case_dir / "task").mkdir(parents=True)
    (case_dir / "task" / "prd.md").write_text("# PRD\n", encoding="utf-8")
    (case_dir / "checks").mkdir()
    (case_dir / "checks" / "Hidden.kt").write_text("secret check\n", encoding="utf-8")
    evaluator = case_dir / "score.py"
    evaluator.write_text(
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "assert '--repeat' not in args\n"
        "out = args[args.index('--output') + 1]\n"
        "json.dump({'S': 1, 'H': 1.0, 'R': True, 'B': True, 'contamination': [], 'extra': 'x'}, open(out, 'w'))\n",
        encoding="utf-8")
    (case_dir / "case.json").write_text(json.dumps({
        "name": "mobile", "seed": {"repository": str(seed), "commit": arm_compare._git(["rev-parse", "HEAD"], seed)},
        "files": {"prd.md": "task/prd.md"}, "task": "Build the cart.", "bare_note": "Run ./gradlew test.",
        "profile": "android", "install": [], "score_keys": ["S", "H", "R", "B"]}), encoding="utf-8")
    return case_dir, evaluator


def test_an_external_case_seeds_its_repository_and_task_files_only(tmp_path):
    case_dir, _ = _external_case(tmp_path)
    run = tmp_path / "runs" / "A-1"
    meta = arm_compare.prepare(ROOT, "A", "ignored", 1, run, case_dir=case_dir)
    project = run / "project"
    assert (project / "app.txt").is_file() and (project / "prd.md").read_text(encoding="utf-8") == "# PRD\n"
    assert not (project / "checks").exists() and not (project / "score.py").exists()
    assert arm_compare._git(["remote"], project) == ""
    prompt = (run / "prompt.md").read_text(encoding="utf-8")
    assert "Build the cart." in prompt and "Run ./gradlew test." in prompt and "EVAL-STATUS" in prompt
    recorded = (run / "arm.json").read_text(encoding="utf-8")
    assert meta["case"] == "mobile" and meta["case_kind"] == "external" and str(case_dir) not in recorded
    assert meta["case_sha256"] == hashlib.sha256((case_dir / "case.json").read_bytes()).hexdigest()
    leaks = [path for path in (project / ".git").rglob("*") if path.is_file()
             and str(tmp_path / "seed-repo").encode() in path.read_bytes()]
    assert leaks == []


def test_a_relative_seed_repository_is_the_case_directorys(tmp_path):
    case_dir, _ = _external_case(tmp_path)
    arm_compare._git(["bundle", "create", "-q", str(case_dir / "seed.bundle"), "HEAD", "main"], tmp_path / "seed-repo")
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    case["seed"]["repository"] = "seed.bundle"
    (case_dir / "case.json").write_text(json.dumps(case), encoding="utf-8")
    run = tmp_path / "elsewhere" / "runs" / "A-1"
    arm_compare.prepare(ROOT, "A", "ignored", 1, run, case_dir=case_dir)
    assert (run / "project" / "app.txt").read_text(encoding="utf-8") == "seed app\n"
    assert arm_compare._git(["remote"], run / "project") == ""
    for kept in ("https://example.com/seed.git", "git@example.com:org/seed.git", str(tmp_path / "seed-repo")):
        assert arm_compare.seed_repository(kept, case_dir) == kept
    assert arm_compare.seed_repository("../seed.bundle", case_dir) == str((case_dir.parent / "seed.bundle").resolve())


def test_an_external_case_is_graded_by_its_own_evaluator_and_keys(tmp_path):
    case_dir, evaluator = _external_case(tmp_path)
    run = tmp_path / "runs" / "A-1"
    arm_compare.prepare(ROOT, "A", "ignored", 1, run, case_dir=case_dir)
    result = arm_compare.score(run, None, evaluator)
    assert result["oracle"] is True and result["contaminated"] is False
    assert result["ui"] == {"S": 1, "H": 1.0, "R": True, "B": True, "error": None}


def test_an_external_case_can_start_from_patches_of_an_earlier_build(tmp_path):
    case_dir, _ = _external_case(tmp_path)
    seed = tmp_path / "seed-repo"
    (seed / "cart.txt").write_text("cart v0\n", encoding="utf-8")
    arm_compare._git(["add", "cart.txt"], seed)
    arm_compare._git(["commit", "-qm", "v0 cart"], seed)
    patch = arm_compare._git(["format-patch", "-1", "HEAD", "-o", str(case_dir / "reference")], seed)
    arm_compare._git(["reset", "-q", "--hard", "HEAD~1"], seed)
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    case["seed"]["patches"] = [str(Path(patch).relative_to(case_dir))]
    (case_dir / "case.json").write_text(json.dumps(case), encoding="utf-8")
    run = tmp_path / "runs" / "A-1"
    arm_compare.prepare(ROOT, "A", "ignored", 1, run, case_dir=case_dir)
    project = run / "project"
    assert (project / "cart.txt").read_text(encoding="utf-8") == "cart v0\n"
    assert arm_compare._git(["log", "--format=%s", "-3"], project).splitlines() == ["task", "v0 cart", "app"]
    assert [path for path in (project / ".git").rglob("*") if path.is_file()
            and str(case_dir).encode() in path.read_bytes()] == []


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


def _labelled_drive(tmp_path, monkeypatch, labels, finals, statuses=3, fail=False):
    findings = [{"job_id": "codex-generalist", "section": "must-fix", "severity": "high", "text": f"finding {n}",
                 "sha": f"sha{n}"} for n in range(len(labels))]
    monkeypatch.setattr(arm_compare, "open_findings", lambda worktree, status: findings)
    calls = []

    def run_claude(prompt, system, **_):
        calls.append(system)
        if fail:
            raise RuntimeError("claude -p gave no JSON")
        ids = [item["id"] for item in json.loads(prompt)]
        return {"text": json.dumps({"labels": [{"id": i, "label": label, "why": "w"}
                                               for i, label in zip(ids, labels)]}), "cost": 0.05}

    monkeypatch.setattr(arm_compare.finding_split, "run_claude", run_claude)
    record, messages = _drive_run(tmp_path, monkeypatch, [_cap_status(tmp_path)] * statuses, finals)
    return record, messages, calls


BLOCKED = "Stopped.\n\nEVAL-STATUS: blocked: fix-loop limit"


def test_an_incomplete_finding_at_the_cap_ends_the_run(tmp_path, monkeypatch):
    record, messages, calls = _labelled_drive(tmp_path, monkeypatch, ["bug", "incomplete"], [BLOCKED])
    assert len(messages) == 1 and calls == [arm_compare.finding_split.LABEL_PROMPT]
    stop = record["review_stop"]
    assert stop["verdict"] == "incomplete" and stop["incomplete"] == 1 and stop["phase"] == "multi-review"
    assert [item["label"] for item in stop["findings"]] == ["bug", "incomplete"]
    assert record["fix_extensions"] == [] and record["fix_loop_reapprovals"] == []
    assert record["review_classifier_cost_usd"] == 0.05 and record["fix_loop_cap_stop"] is True


def test_bug_findings_at_the_cap_get_at_most_two_fix_extensions(tmp_path, monkeypatch):
    record, messages, calls = _labelled_drive(tmp_path, monkeypatch, ["bug", "design"], [BLOCKED], statuses=5)
    assert len(messages) == 3 and len(calls) == 3
    assert all("Fix them in the code, run the tests" in message for message in messages[1:])
    assert "- [bug] finding 0" in messages[1] and "- [design] finding 1" in messages[1]
    extensions = record["fix_extensions"]
    assert [item["passed"] for item in extensions] == [False, False]
    assert extensions[0]["findings"][0]["label"] == "bug" and record["fix_loop_reapprovals"] == []
    assert record["review_stop"] is None and record["review_classifier_cost_usd"] == 0.15


def test_a_fix_extension_that_lets_the_run_finish_is_passed(tmp_path, monkeypatch):
    record, messages, _ = _labelled_drive(tmp_path, monkeypatch, ["procedure"], [BLOCKED, "Done.\n\nEVAL-STATUS: complete"],
                                          statuses=1)
    assert len(messages) == 2 and [item["passed"] for item in record["fix_extensions"]] == [True]
    assert record["fix_loop_cap_stop"] is False


def test_a_classifier_failure_falls_back_to_the_re_review(tmp_path, monkeypatch):
    record, messages, calls = _labelled_drive(tmp_path, monkeypatch, ["bug"], [BLOCKED], statuses=5, fail=True)
    assert len(messages) == 3 and "Do not change code" in messages[1]
    assert len(record["fix_loop_reapprovals"]) == 2 and record["fix_extensions"] == []
    failures = record["review_classifier_failures"]
    assert len(failures) == 3 and "no JSON" in failures[0]["error"]


def test_no_open_findings_falls_back_without_a_classifier_call(tmp_path, monkeypatch):
    record, messages = _drive_run(tmp_path, monkeypatch, [_cap_status(tmp_path)] * 5, [BLOCKED])
    assert record["review_classifier_failures"][0]["error"] == "no-open-findings"
    assert record["review_classifier_cost_usd"] == 0.0 and len(record["fix_loop_reapprovals"]) == 2


def test_open_findings_come_from_reviewers_that_requested_changes(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "20261022-131409"
    (run_dir / "artifacts").mkdir(parents=True)
    body = ("# codex-types\n\n## review output\n\n```text\n### Must-fix\n- <severity:high> [a.js:3] Refund is not "
            "recorded.\n### Notes\n- <severity:low> [a.js:9] fine\n\nverdict: request-changes\n```\n")
    (run_dir / "multi-review-types-codex.md").write_text(body, encoding="utf-8")
    (run_dir / "multi-review-types-claude.md").write_text(body.replace("Refund", "Other"), encoding="utf-8")
    (run_dir / "multi-review-review-results.json").write_text(json.dumps({"outcomes": [
        {"artifact": "multi-review-types-codex.md", "job_id": "codex-types", "verdict": "request-changes"},
        {"artifact": "multi-review-types-claude.md", "job_id": "claude-types", "verdict": "approve"}]}),
        encoding="utf-8")
    monkeypatch.setattr(arm_compare, "phase_artifact",
                        lambda worktree, status, phase: run_dir / "artifacts" / f"{phase}.md")
    found = arm_compare.open_findings(tmp_path, _cap_status(tmp_path))
    assert [(item["job_id"], item["section"]) for item in found] == [("codex-types", "must-fix")]
    assert "Refund is not recorded" in found[0]["text"]


def test_the_total_cost_adds_the_review_classifier():
    assert arm_compare.total_cost(1.0, None, None, 0.5, 0.25) == 1.75
    assert arm_compare.total_cost(1.0, None, None, 0.0, None) is None


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
    assert "| 1 / 1 | 1 |" in c_line


# ─── #409 v2.1: arm D and the phase trace ───

def test_the_default_workflow_is_arm_d(tmp_path):
    run = tmp_path / "D-1"
    meta = arm_compare.prepare(ROOT, "D", "devflow", 1, run)
    prompt = (run / "prompt.md").read_text(encoding="utf-8")
    assert "--workflow default" in prompt and "EVAL-STATUS" in prompt
    assert meta["approval_rules"] == arm_compare.HARNESS_APPROVES
    assert (run / "project" / ".agent-flow" / "bin" / "agent-flow").exists()


def test_an_external_case_prepares_arm_d(tmp_path):
    case_dir, _ = _external_case(tmp_path)
    run = tmp_path / "runs" / "D-1"
    arm_compare.prepare(ROOT, "D", "ignored", 1, run, case_dir=case_dir)
    assert "--workflow default" in (run / "prompt.md").read_text(encoding="utf-8")


def _events(path: Path, rows: list[tuple[str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"ts": ts, "event": event, "details": {"phase": phase}}) + "\n"
                            for ts, event, phase in rows), encoding="utf-8")


def _message(message_id: str, at: str, output: int) -> dict:
    return {"type": "assistant", "timestamp": at,
            "message": {"id": message_id, "model": "claude-opus-5-5", "content": [],
                        "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                                  "output_tokens": output,
                                  "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}}}


def test_the_phase_trace_splits_time_and_spend_by_phase_and_names_skipped_phases(tmp_path):
    project = tmp_path / "project"
    workflow = project / ".agent-flow" / "runtime" / "python" / "agent_flow" / "workflows" / "default.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("id: default\nphases:\n  - id: design\n  - id: implement\n  - id: gates\n  - id: commit\n",
                        encoding="utf-8")
    _events(project / ".git" / "agent-flow" / "r1" / "context" / "events.jsonl", [
        ("2026-10-23T01:00:00+00:00", "phase_entered", "design"),
        ("2026-10-23T01:00:00+00:00", "phase_entered", "design"),
        ("2026-10-23T01:10:00+00:00", "phase_exited", "design"),
        ("2026-10-23T01:10:00+00:00", "phase_entered", "implement"),
        ("2026-10-23T01:40:00+00:00", "phase_entered", "commit"),
    ])
    rows = [_message("m0", "2026-10-23T00:59:00Z", 1_000_000),   # before any phase: outside
            _message("m1", "2026-10-23T01:05:00Z", 100_000),     # design
            _message("m2", "2026-10-23T01:20:00Z", 200_000),     # implement
            _message("m3", "2026-10-23T01:45:00Z", 50_000)]      # commit (open window)
    trace = arm_compare.phase_trace(project, project, "default", rows)
    assert trace["declared"] == ["design", "implement", "gates", "commit"]
    assert trace["entered"] == ["design", "implement", "commit"] and trace["skipped"] == ["gates"]
    assert trace["phases"]["design"]["entries"] == 1 and trace["phases"]["design"]["seconds"] == 600
    assert trace["phases"]["implement"]["seconds"] == 1800 and trace["phases"]["commit"]["seconds"] == 300
    assert trace["phases"]["implement"]["output_tokens"] == 200_000
    spend = trace["spend_by_category"]
    assert spend["plan"]["cost_usd"] == 2.0 and spend["build"]["cost_usd"] == 4.0
    assert spend["deliver"]["cost_usd"] == 1.0 and spend["outside"]["cost_usd"] == 20.0
    assert trace["orchestration_share"] == round(21 / 27, 3)


def test_a_project_local_workflow_overrides_the_kit_copy(tmp_path):
    project = tmp_path / "project"
    for path, ids in ((project / ".agent-flow" / "workflows" / "development.yaml", ["explore", "implement"]),
                      (project / ".agent-flow" / "runtime" / "python" / "agent_flow" / "workflows" / "development.yaml",
                       ["explore", "implement", "review", "qa"])):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("phases:\n" + "".join(f"  - id: {phase}\n" for phase in ids), encoding="utf-8")
    assert arm_compare.declared_phases(project, project, "development") == ["explore", "implement"]


def test_an_unpriced_model_leaves_phase_costs_null_not_zero(tmp_path):
    project = tmp_path / "project"
    _events(project / ".git" / "agent-flow" / "r1" / "context" / "events.jsonl",
            [("2026-10-23T01:00:00+00:00", "phase_entered", "explore")])
    row = _message("m1", "2026-10-23T01:05:00Z", 10)
    row["message"]["model"] = "unknown-model"
    trace = arm_compare.phase_trace(project, project, "development", [row])
    assert trace["phases"]["explore"]["cost_usd"] is None and trace["orchestration_share"] is None
    assert trace["declared"] is None and trace["skipped"] is None


def test_the_summary_reports_phases_and_the_orchestration_share():
    trace = {"entered": ["design", "implement"], "declared": ["design", "implement", "gates", "commit"],
             "orchestration_share": 0.25}
    rows = [_summary_row("A"), _summary_row("D", phase_trace=trace),
            _summary_row("D", phase_trace={**trace, "entered": ["design"], "orchestration_share": 0.75})]
    d_line = next(line for line in arm_compare.summarize(rows).splitlines() if line.startswith("| D |"))
    assert d_line.endswith("| 1.5 / 4 | 50% | - | - | - |")
    a_line = next(line for line in arm_compare.summarize(rows).splitlines() if line.startswith("| A |"))
    assert a_line.endswith("| - | - | - | - | - |")


# ─── #409 v2 §3c: follow-on changes ───

def _followon_case(tmp_path):
    case_dir = tmp_path / "private" / "mobile" / "followon" / "F1"
    (case_dir / "task").mkdir(parents=True)
    (case_dir / "task" / "prd.md").write_text("# F1 memo\n", encoding="utf-8")
    (case_dir / "checks").mkdir()
    (case_dir / "checks" / "HiddenFu.kt").write_text("secret\n", encoding="utf-8")
    (case_dir / "case.json").write_text(json.dumps({
        "name": "mobile-F1", "files": {"followon-prd.md": "task/prd.md"}, "task": "Add the delivery memo.",
        "bare_note": "Run ./gradlew test.", "score_keys": ["S", "F", "G", "R", "B"]}), encoding="utf-8")
    return case_dir


def test_a_followon_starts_from_the_arms_code_without_the_kit_or_its_history(tmp_path):
    run = tmp_path / "C-1"
    arm_compare.prepare(ROOT, "C", "devflow", 1, run)
    project = run / "project"
    (project / "feature.txt").write_text("the arm's work, left uncommitted\n", encoding="utf-8")
    (project / "build").mkdir()
    (project / "build" / "out.bin").write_text("x", encoding="utf-8")
    assert (project / ".agent-flow").is_dir()
    # What the installer adds outside the fixed list, as it records it in .git/info/exclude.
    (project / "tools-from-kit").mkdir()
    (project / "tools-from-kit" / "hook.ts").write_text("kit\n", encoding="utf-8")
    with (project / ".git" / "info" / "exclude").open("a", encoding="utf-8") as exclude:
        exclude.write("/tools-from-kit/\n")
    (project / "src-link").symlink_to(project / ".agent-flow")
    out = tmp_path / "C-1.F1"
    meta = arm_compare.prepare_followon(run, _followon_case(tmp_path), 1, out)
    follow = out / "project"
    assert (follow / "feature.txt").read_text(encoding="utf-8") == "the arm's work, left uncommitted\n"
    assert (follow / "followon-prd.md").read_text(encoding="utf-8") == "# F1 memo\n"
    for trace in (".agent-flow", ".agents", ".omp", ".claude", ".codex", "CLAUDE.md", "AGENTS.md", "build", "checks",
                  "tools-from-kit", "src-link"):
        assert not (follow / trace).exists(), trace
    assert [path for path in follow.rglob("*") if path.is_symlink()] == []
    assert arm_compare._git(["log", "--format=%s"], follow).splitlines() == ["base"]
    assert arm_compare._git(["branch", "--show-current"], follow) == arm_compare.BRANCH
    assert meta["arm"] == "A" and meta["case"] == "mobile-F1" and meta["case_kind"] == "external"
    assert meta["followon_of"]["arm"] == "C" and meta["followon_of"]["artifact_sha256"]
    prompt = (out / "prompt.md").read_text(encoding="utf-8")
    assert "Add the delivery memo." in prompt and "EVAL-STATUS" in prompt and "Agent Flow" not in prompt
    assert str(tmp_path / "private") not in (out / "arm.json").read_text(encoding="utf-8")


def test_two_arms_give_the_followon_agent_the_same_prompt(tmp_path):
    case_dir = _followon_case(tmp_path)
    prompts = []
    for arm in ("A", "C"):
        run = tmp_path / f"{arm}-1"
        arm_compare.prepare(ROOT, arm, "devflow", 1, run)
        arm_compare.prepare_followon(run, case_dir, 1, tmp_path / f"{arm}-1.F1")
        prompts.append((tmp_path / f"{arm}-1.F1" / "prompt.md").read_text(encoding="utf-8"))
    assert prompts[0] == prompts[1]


def test_a_blocked_exit_keeps_the_work_inside_its_phase(tmp_path):
    project = tmp_path / "project"
    path = project / ".git" / "agent-flow" / "r1" / "context" / "events.jsonl"
    path.parent.mkdir(parents=True)
    rows = [{"ts": "2026-10-23T01:00:00+00:00", "event": "phase_entered", "details": {"phase": "implement"}},
            {"ts": "2026-10-23T01:10:00+00:00", "event": "phase_exited", "details": {"phase": "implement", "blocked": True}},
            {"ts": "2026-10-23T01:30:00+00:00", "event": "phase_exited", "details": {"phase": "implement", "blocked": False}},
            {"ts": "2026-10-23T01:30:00+00:00", "event": "phase_entered", "details": {"phase": "review"}}]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    trace = arm_compare.phase_trace(project, project, "development",
                                    [_message("m1", "2026-10-23T01:20:00Z", 1_000_000)])
    assert trace["phases"]["implement"]["seconds"] == 1800
    assert trace["spend_by_category"]["build"]["cost_usd"] == 20.0 and trace["spend_by_category"]["outside"]["cost_usd"] == 0.0


def test_unmeasured_subagents_leave_phase_costs_null(tmp_path):
    project = tmp_path / "project"
    _events(project / ".git" / "agent-flow" / "r1" / "context" / "events.jsonl",
            [("2026-10-23T01:00:00+00:00", "phase_entered", "explore")])
    trace = arm_compare.phase_trace(project, project, "development", [_message("m1", "2026-10-23T01:05:00Z", 10)],
                                    measured=False)
    assert trace["phases"]["explore"]["cost_usd"] is None and trace["orchestration_share"] is None
    assert trace["phases"]["explore"]["output_tokens"] == 10


# ─── #409 v2.2: reviewer lower bound and model preflight ───

def test_the_reviewer_record_is_a_lower_bound_from_the_runners_job_metrics(tmp_path):
    project = tmp_path / "project"
    run = project / ".git" / "agent-flow" / "r1"
    run.mkdir(parents=True)
    jobs = [{"provider": "claude", "model": "claude-opus-5-5", "effort": "high", "status": "ok", "duration_s": 100.4,
             "prompt_utf8_bytes": 400_000, "stdout_utf8_bytes": 40_000},
            {"provider": "claude", "model": "claude-opus-5-5", "effort": "high", "status": "error", "duration_s": 3,
             "prompt_utf8_bytes": 400_000, "stdout_utf8_bytes": 0},
            {"provider": "codex", "model": "unspecified", "effort": "unspecified", "status": "ok", "duration_s": 50,
             "prompt_utf8_bytes": 4_000, "stdout_utf8_bytes": 400}]
    (run / "review-metrics-a.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    record = arm_compare.reviewer_record(project, project)
    assert record["status"] == "lower-bound" and record["jobs"] == 3 and record["waves"] == 1
    claude = record["providers"]["claude"]
    # The failed job may never have reached the model: its prompt is kept apart, not charged.
    assert claude["failed"] == 1 and claude["attempted_prompt_bytes"] == 800_000
    assert claude["token_lower_bound"] == 110_000 and claude["seconds"] == 103
    assert claude["cost_lower_bound_usd"] == round((100_000 * 4.0 + 10_000 * 20.0) / 1_000_000, 4)
    assert record["providers"]["codex"]["cost_lower_bound_usd"] is None and record["providers"]["codex"]["token_lower_bound"] == 1_100
    assert record["cost_lower_bound_usd"] is None and record["priced_cost_lower_bound_usd"] == 0.6


def test_a_reviewer_on_the_cli_default_model_is_not_priced_as_the_controller(tmp_path):
    run = tmp_path / ".git" / "agent-flow" / "r1"
    run.mkdir(parents=True)
    job = {"provider": "claude", "model": "unspecified", "effort": "unspecified", "status": "ok", "duration_s": 10,
           "prompt_utf8_bytes": 40_000, "stdout_utf8_bytes": 4_000}
    (run / "review-metrics-a.json").write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    record = arm_compare.reviewer_record(tmp_path, tmp_path)
    assert record["providers"]["claude"]["cost_lower_bound_usd"] is None and record["cost_lower_bound_usd"] is None
    assert record["providers"]["claude"]["token_lower_bound"] == 11_000


def test_no_reviewer_metrics_means_no_jobs_and_no_cost(tmp_path):
    record = arm_compare.reviewer_record(tmp_path, tmp_path)
    assert record["jobs"] == 0 and record["cost_lower_bound_usd"] is None


def test_preflight_reports_an_unavailable_model_instead_of_substituting(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        ok = "claude-opus-5-5" in argv
        return arm_compare.subprocess.CompletedProcess(argv, 0 if ok else 1, "OK\n" if ok else "", "" if ok else "no such model")

    monkeypatch.setattr(arm_compare.subprocess, "run", fake_run)
    rows = arm_compare.preflight([("claude", "claude-opus-5-5"), ("claude", "claude-cheap-x")])
    assert [row["available"] for row in rows] == [True, False] and "no such model" in rows[1]["error"]
    assert calls[1][:4] == ["claude", "-p", "--model", "claude-cheap-x"]


def test_the_summary_shows_reviewer_tokens_and_leaves_an_unpriced_cost_unpriced():
    record = {"jobs": 40, "cost_lower_bound_usd": None, "priced_cost_lower_bound_usd": 0.0,
              "providers": {"claude": {"seconds": 1500, "token_lower_bound": 127_000},
                            "codex": {"seconds": 1450, "token_lower_bound": 116_000}}}
    line = next(row for row in arm_compare.summarize([_summary_row("C", reviewers=record)]).splitlines()
                if row.startswith("| C |"))
    assert line.endswith("| 40.0 / 2,950 / ≥243,000 tok, unpriced | - |")


# ─── #409 v2: arm A+P ───

def test_arm_ap_is_arm_a_plus_the_fixed_principles(tmp_path):
    bare = arm_compare.prepare(ROOT, "A", "devflow", 1, tmp_path / "A-1")
    ap = arm_compare.prepare(ROOT, "AP", "devflow", 1, tmp_path / "AP-1")
    project = tmp_path / "AP-1" / "project"
    assert (project / "CLAUDE.md").read_text(encoding="utf-8") == arm_compare.PRINCIPLES
    assert not (tmp_path / "A-1" / "project" / "CLAUDE.md").exists()
    assert ((tmp_path / "AP-1" / "prompt.md").read_text(encoding="utf-8")
            == (tmp_path / "A-1" / "prompt.md").read_text(encoding="utf-8"))
    assert ap["principles"] == {"version": "ap-v1", "sha256": arm_compare.PRINCIPLES_SHA256} and bare["principles"] is None
    assert arm_compare._git(["log", "--format=%s", "-1"], project) == "principles"
    assert not (project / ".agent-flow").exists()


def test_an_external_case_prepares_arm_ap(tmp_path):
    case_dir, _ = _external_case(tmp_path)
    arm_compare.prepare(ROOT, "AP", "ignored", 1, tmp_path / "runs" / "AP-1", case_dir=case_dir)
    assert (tmp_path / "runs" / "AP-1" / "project" / "CLAUDE.md").is_file()


def test_arm_ap_commits_its_principles_when_the_seed_ignores_claude_md(tmp_path):
    case_dir, _ = _external_case(tmp_path)
    seed = tmp_path / "seed-repo"
    (seed / ".gitignore").write_text("CLAUDE.md\n", encoding="utf-8")
    arm_compare._git(["add", ".gitignore"], seed)
    arm_compare._git(["commit", "-qm", "ignore"], seed)
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    case["seed"]["commit"] = arm_compare._git(["rev-parse", "HEAD"], seed)
    (case_dir / "case.json").write_text(json.dumps(case), encoding="utf-8")
    run = tmp_path / "runs" / "AP-1"
    arm_compare.prepare(ROOT, "AP", "ignored", 1, run, case_dir=case_dir)
    project = run / "project"
    assert arm_compare._git(["ls-files", "CLAUDE.md"], project) == "CLAUDE.md"
    assert arm_compare._git(["status", "--porcelain"], project) == ""


def test_a_followon_from_arm_ap_drops_its_principles(tmp_path):
    run = tmp_path / "AP-1"
    arm_compare.prepare(ROOT, "AP", "devflow", 1, run)
    arm_compare.prepare_followon(run, _followon_case(tmp_path), 1, tmp_path / "AP-1.F1")
    assert not (tmp_path / "AP-1.F1" / "project" / "CLAUDE.md").exists()


# ─── #409 pstack review: arm PS ───

def test_arm_ps_is_arm_a_plus_the_pstack_principles(tmp_path):
    arm_compare.prepare(ROOT, "A", "devflow", 1, tmp_path / "A-1")
    ps = arm_compare.prepare(ROOT, "PS", "devflow", 1, tmp_path / "PS-1")
    project = tmp_path / "PS-1" / "project"
    assert (project / "CLAUDE.md").read_text(encoding="utf-8") == arm_compare.PSTACK_PRINCIPLES
    assert ((tmp_path / "PS-1" / "prompt.md").read_text(encoding="utf-8")
            == (tmp_path / "A-1" / "prompt.md").read_text(encoding="utf-8"))
    assert ps["principles"] == {"version": "ps-v1", "sha256": arm_compare.PSTACK_PRINCIPLES_SHA256}
    assert arm_compare._git(["log", "--format=%s", "-1"], project) == "principles"
    assert not (project / ".agent-flow").exists()


def test_the_two_principle_sets_differ_and_ps_never_allows_deleting_tests():
    assert arm_compare.PSTACK_PRINCIPLES != arm_compare.PRINCIPLES
    assert arm_compare.PSTACK_PRINCIPLES_SHA256 != arm_compare.PRINCIPLES_SHA256
    assert "delete" not in arm_compare.PSTACK_PRINCIPLES.lower()
    assert "pstack" not in arm_compare.PSTACK_PRINCIPLES.lower()  # the agent is not told where the text comes from


def test_an_external_case_prepares_arm_ps_and_a_followon_drops_it(tmp_path):
    case_dir, _ = _external_case(tmp_path)
    run = tmp_path / "runs" / "PS-1"
    meta = arm_compare.prepare(ROOT, "PS", "ignored", 1, run, case_dir=case_dir)
    assert (run / "project" / "CLAUDE.md").read_text(encoding="utf-8") == arm_compare.PSTACK_PRINCIPLES
    assert meta["principles"]["version"] == "ps-v1"
    arm_compare.prepare_followon(run, _followon_case(tmp_path), 1, tmp_path / "runs" / "PS-1.F1")
    assert not (tmp_path / "runs" / "PS-1.F1" / "project" / "CLAUDE.md").exists()


# ─── #409 §5: arm S (reduced-workflow search variant) ───

def test_arm_s_installs_the_kit_and_runs_the_variant(tmp_path):
    meta = arm_compare.prepare(ROOT, "S", "devflow", 1, tmp_path / "S-1", variant={"planning": False, "reviewers": 2})
    project = tmp_path / "S-1" / "project"
    name = meta["variant"]["name"]
    assert meta["workflow"] == name and (project / ".agent-flow" / "workflows" / f"{name}.yaml").is_file()
    assert f"--workflow {name}" in (tmp_path / "S-1" / "prompt.md").read_text(encoding="utf-8")
    assert meta["approval_rules"] == arm_compare.HARNESS_APPROVES


def test_only_arm_s_takes_a_variant(tmp_path):
    with pytest.raises(ValueError):
        arm_compare.prepare(ROOT, "S", "devflow", 1, tmp_path / "S-1")
    with pytest.raises(ValueError):
        arm_compare.prepare(ROOT, "B", "devflow", 1, tmp_path / "B-1", variant={"tdd": False})


# ─── #409 adversarial review: arm V (verdict only) ───

def _gate_hook(project: Path) -> dict:
    settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    (entry,) = settings["hooks"]["Stop"]
    (hook,) = entry["hooks"]
    return hook


def test_arm_v_is_arm_a_plus_a_stop_hook_outside_the_project(tmp_path):
    arm_compare.prepare(ROOT, "A", "devflow", 1, tmp_path / "A-1")
    run = tmp_path / "V-1"
    meta = arm_compare.prepare(ROOT, "V", "devflow", 1, run)
    project = run / "project"
    assert (run / "prompt.md").read_text(encoding="utf-8") == (tmp_path / "A-1" / "prompt.md").read_text(encoding="utf-8")
    hook = _gate_hook(project)
    script, config = (run / "gate" / "verify_gate.py").resolve(), (run / "gate" / "config.json").resolve()
    assert hook["type"] == "command" and hook["command"] == f"python3 -I {script} {config}"
    assert hook["timeout"] == arm_compare.verify_gate.HOOK_TIMEOUT
    assert project.resolve() not in script.parents
    gate = meta["verify_gate"]
    assert gate["script_sha256"] == hashlib.sha256(script.read_bytes()).hexdigest()
    assert gate["script_sha256"] == hashlib.sha256((ROOT / "evals" / "verify_gate.py").read_bytes()).hexdigest()
    case = json.loads((ROOT / "evals" / "phase-cases" / "devflow" / "case.json").read_text(encoding="utf-8"))
    assert gate["verify_command"] == case["test_command"] and gate["verify_source"] == "test_command"
    assert arm_compare._git(["ls-files", ".claude/settings.json"], project) == ".claude/settings.json"
    assert arm_compare._git(["status", "--porcelain"], project) == ""
    assert arm_compare._git(["log", "--format=%s", "-1"], project) == "verify gate"
    assert arm_compare._git(["rev-parse", "HEAD"], project) == meta["start_commit"]
    assert arm_compare._git(["branch", "--show-current"], project) == arm_compare.BRANCH
    assert not (project / "CLAUDE.md").exists() and not (project / ".agent-flow").exists()
    assert meta["principles"] is None and meta["approval_rules"] is None
    stored = json.loads(config.read_text(encoding="utf-8"))
    assert stored["env"] == {"keys": list(arm_compare.CONTROLLER_ENV), "set": arm_compare.controller_env({})}
    assert stored["task"] == case["task"] and stored["documents"] == []


def test_an_external_case_commits_arm_vs_hook_past_an_ignored_claude_dir(tmp_path):
    case_dir, _ = _external_case(tmp_path)
    seed = tmp_path / "seed-repo"
    (seed / ".gitignore").write_text(".claude\n", encoding="utf-8")
    arm_compare._git(["add", ".gitignore"], seed)
    arm_compare._git(["commit", "-qm", "ignore"], seed)
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    case["seed"]["commit"] = arm_compare._git(["rev-parse", "HEAD"], seed)
    (case_dir / "case.json").write_text(json.dumps(case), encoding="utf-8")
    run = tmp_path / "runs" / "V-1"
    meta = arm_compare.prepare(ROOT, "V", "ignored", 1, run, case_dir=case_dir)
    project = run / "project"
    assert arm_compare._git(["ls-files", ".claude/settings.json"], project) == ".claude/settings.json"
    assert arm_compare._git(["status", "--porcelain"], project) == ""
    # android: the gradle command the bare note names; the task documents are the case's files.
    assert meta["verify_gate"]["verify_command"] == ["./gradlew", "test"]
    assert meta["verify_gate"]["verify_source"] == "bare_note" and meta["verify_gate"]["documents"] == ["prd.md"]
    assert str(case_dir) not in (run / "gate" / "config.json").read_text(encoding="utf-8")
    arm_compare.prepare_followon(run, _followon_case(tmp_path), 1, tmp_path / "runs" / "V-1.F1")
    assert not (tmp_path / "runs" / "V-1.F1" / "project" / ".claude").exists()


def test_the_gate_verify_command_comes_from_the_case_and_is_null_without_one():
    assert arm_compare.gate_verify_command({"verify_command": "npm run check -- --ci"}, "external") == (
        ["npm", "run", "check", "--", "--ci"], "case")
    assert arm_compare.gate_verify_command(
        {"profile": "android", "bare_note": "Build with `./gradlew :app:testDebugUnitTest --offline`."},
        "external") == (["./gradlew", ":app:testDebugUnitTest", "--offline"], "bare_note")
    assert arm_compare.gate_verify_command({"profile": "node", "bare_note": "Run ./gradlew test."}, "external") == (
        None, None)
    assert arm_compare.gate_verify_command({}, "ui") == (None, None)


def _fake_claude(bin_dir: Path, result: str, seen: Path) -> None:
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"open({str(seen)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], 'env': dict(os.environ), "
        "'cwd': os.getcwd()}))\n"
        "open('reviewer-was-here', 'w').write('x')\n"
        f"print(json.dumps({{'result': {result!r}, 'total_cost_usd': 0.5, 'num_turns': 3, 'is_error': False, "
        "'usage': {'input_tokens': 100, 'output_tokens': 20}}))\n", encoding="utf-8")
    fake.chmod(0o755)


def test_the_installed_hook_blocks_once_on_review_findings_and_never_touches_the_project(tmp_path, monkeypatch):
    run = tmp_path / "V-1"
    arm_compare.prepare(ROOT, "V", "devflow", 1, run)
    project = run / "project"
    seen = tmp_path / "reviewer.json"
    _fake_claude(tmp_path / "bin", "1. The quote has no discount field.", seen)
    command = _gate_hook(project)["command"]
    env = {"PATH": f"{tmp_path / 'bin'}:{arm_compare.os.environ['PATH']}", "HOME": str(tmp_path),
           "CLAUDE_CODE_SESSION_ID": "controller-session", "CLAUDE_PROJECT_DIR": str(project)}

    def stop(active):
        done = arm_compare.subprocess.run(["sh", "-c", command], cwd=project, env=env, capture_output=True, text=True,
                                          input=json.dumps({"hook_event_name": "Stop", "stop_hook_active": active}))
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout) if done.stdout.strip() else {}

    first = stop(False)
    assert first["decision"] == "block" and "1. The quote has no discount field." in first["reason"]
    reviewer = json.loads(seen.read_text(encoding="utf-8"))
    # the controller's allowlisted env only; the reviewer worked in a throwaway copy with read-only tools
    assert "CLAUDE_CODE_SESSION_ID" not in reviewer["env"] and "CLAUDE_PROJECT_DIR" not in reviewer["env"]
    assert reviewer["env"]["CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"] == "1"
    assert Path(reviewer["cwd"]).resolve() != project.resolve()
    argv = reviewer["argv"]
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5" and argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep,Bash"
    assert "Edit" in argv[argv.index("--disallowedTools") + 1] and argv[argv.index("--setting-sources") + 1] == ""
    assert "--max-budget-usd" in argv
    assert stop(True) == {}  # one review round: the second stop goes through
    assert arm_compare._git(["status", "--porcelain"], project) == ""
    summary = arm_compare.verify_gate.summary(run / "verify-gate.jsonl")
    assert summary["blocks"] == 1 and summary["review_rounds"] == 1 and summary["review_findings"] == 1
    assert summary["reviewer_cost_usd"] == 0.5 and summary["reviewer_usage"] == {"input_tokens": 100, "output_tokens": 20}
    assert summary["gate_cap_reached"] == "review" and summary["verify_blocks"] == 0


def test_drive_gives_the_gate_the_controllers_model_and_records_its_events(tmp_path, monkeypatch):
    run = tmp_path / "V-1"
    arm_compare.prepare(ROOT, "V", "devflow", 1, run)

    def controller_once(argv, project, out, err, timeout):
        out.write((json.dumps({"type": "system", "subtype": "init", "session_id": "s-1"}) + "\n" +
                   json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 2.0,
                               "result": "EVAL-STATUS: complete"}) + "\n").encode())
        (run / "verify-gate.jsonl").write_text(json.dumps({"step": "verify", "decision": "block"}) + "\n",
                                               encoding="utf-8")
        return 0, False

    monkeypatch.setattr(arm_compare, "_controller_once", controller_once)
    record = arm_compare.drive(run, "claude-opus-x", 600, 30.0)
    assert json.loads((run / "gate" / "config.json").read_text(encoding="utf-8"))["model"] == "claude-opus-x"
    assert record["verify_gate"]["verify_blocks"] == 1 and record["cost_usd"] == 2.0


def test_arm_vs_score_adds_the_reviewer_cost_to_the_total_and_checks_the_wiring(tmp_path):
    run = tmp_path / "V-1"
    arm_compare.prepare(ROOT, "V", "devflow", 1, run)
    (run / "drive.json").write_text(json.dumps({"cost_usd": 2.0, "exit": 0}), encoding="utf-8")
    (run / "verify-gate.jsonl").write_text(
        json.dumps({"step": "verify", "decision": "block"}) + "\n"
        + json.dumps({"step": "review", "decision": "block", "findings": 2,
                      "review": {"cost_usd": 0.75, "seconds": 40, "usage": {"output_tokens": 9}}}) + "\n",
        encoding="utf-8")
    result = arm_compare.score(run, None)
    gate = result["verify_gate"]
    assert gate["blocks"] == 2 and gate["review_findings"] == 2 and gate["wiring_intact"] is True
    assert result["controller_cost_usd"] == 2.0 and result["total_cost_usd"] == 2.75
    (run / "project" / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    assert arm_compare.score(run, None)["verify_gate"]["wiring_intact"] is False
    rows = [_summary_row("A", controller_cost_usd=1.0, total_cost_usd=1.0),
            _summary_row("V", controller_cost_usd=2.0, total_cost_usd=2.75, verify_gate=gate)]
    table = arm_compare.summarize(rows)
    v_line = next(line for line in table.splitlines() if line.startswith("| V |"))
    a_line = next(line for line in table.splitlines() if line.startswith("| A |"))
    assert "| $2.00 / $2.75 | 2.0 / 2.0 / $0.75 |" in v_line and "| $1.00 / $1.00 | - |" in a_line


def test_the_cli_and_preflight_take_arm_v(tmp_path, monkeypatch):
    run = tmp_path / "V-cli"
    assert arm_compare.main(["prepare", "--kit", str(ROOT), "--arm", "V", "--case", "devflow",
                             "--output", str(run)]) == 0
    assert (run / "gate" / "verify_gate.py").is_file()
    # V's reviewer runs on the controller's model, so the controller's preflight entry covers it.
    monkeypatch.setattr(arm_compare.subprocess, "run", lambda argv, **_: arm_compare.subprocess.CompletedProcess(
        argv, 0, "OK\n", ""))
    assert arm_compare.main(["preflight", "--model", "claude:claude-opus-5-5"]) == 0


# ─── arm VK (V + the kit's skills as project skills) ───

def _kit_skill_names() -> list[str]:
    return sorted(path.name for path in (ROOT / "skills").iterdir() if (path / "SKILL.md").is_file())


def test_arm_vk_is_arm_v_plus_the_kits_skills_committed_as_project_skills(tmp_path):
    arm_compare.prepare(ROOT, "A", "devflow", 1, tmp_path / "A-1")
    v = arm_compare.prepare(ROOT, "V", "devflow", 1, tmp_path / "V-1")
    run = tmp_path / "VK-1"
    meta = arm_compare.prepare(ROOT, "VK", "devflow", 1, run)
    project = run / "project"
    prompt = (run / "prompt.md").read_text(encoding="utf-8")
    assert prompt == (tmp_path / "A-1" / "prompt.md").read_text(encoding="utf-8")
    assert prompt == (tmp_path / "V-1" / "prompt.md").read_text(encoding="utf-8")
    assert meta["prompt_sha256"] == v["prompt_sha256"] and "skill" not in prompt.lower()
    # V's gate, unchanged
    assert meta["verify_gate"]["script_sha256"] == v["verify_gate"]["script_sha256"]
    assert {key: value for key, value in meta["verify_gate"].items() if key not in ("script", "settings_sha256")} == {
        key: value for key, value in v["verify_gate"].items() if key not in ("script", "settings_sha256")}
    assert _gate_hook(project)["command"].endswith(f"{(run / 'gate' / 'config.json').resolve()}")
    # the kit's skills minus the exclusions, each file as in the kit, without Codex's host metadata
    skills = meta["skills"]
    excluded = set(arm_compare.EXCLUDED_SKILLS)
    assert skills["names"] == [name for name in _kit_skill_names() if name not in excluded]
    assert skills["excluded"] == [{"name": name, "reason": arm_compare.EXCLUDED_SKILLS[name]}
                                  for name in _kit_skill_names() if name in excluded]
    assert {item["name"] for item in skills["excluded"]} == excluded
    assert all(item["reason"] for item in skills["excluded"])
    assert sorted(item["name"] for item in skills["cli_mentions_kept"]) == sorted(arm_compare.CLI_MENTIONS_KEPT)
    target = project / ".claude" / "skills"
    assert sorted(path.name for path in target.iterdir()) == skills["names"]
    for item in skills["items"]:
        source, copied = ROOT / "skills" / item["name"], target / item["name"]
        assert item["sha256"] == arm_compare.skill_sha256(source) == arm_compare.skill_sha256(copied)
        assert item["skill_md_sha256"] == hashlib.sha256((source / "SKILL.md").read_bytes()).hexdigest()
        assert arm_compare.skill_files(copied) == arm_compare.skill_files(source)
        assert not (copied / "agents").exists()
        # no kept skill tells the agent to run an `agent-flow` command, except the recorded mentions
        assert not arm_compare.skill_cli_commands(copied) or item["name"] in arm_compare.CLI_MENTIONS_KEPT
        front = (copied / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
        assert not arm_compare.CLI_COMMAND.search(front) and ".agent-flow/" not in front
    for name in excluded:
        assert not (target / name).exists()
    assert (target / "tdd" / "SKILL.md").is_file() and (target / "security-audit" / "validate-findings.cjs").is_file()
    # committed before the agent starts; nothing else of the kit is there
    tracked = arm_compare._git(["ls-files", ".claude/skills"], project).splitlines()
    assert ".claude/skills/tdd/SKILL.md" in tracked and len(tracked) == sum(item["files"] for item in skills["items"])
    assert arm_compare._git(["status", "--porcelain"], project) == ""
    assert arm_compare._git(["log", "--format=%s", "-2"], project).splitlines() == ["skills", "verify gate"]
    assert arm_compare._git(["rev-parse", "HEAD"], project) == meta["start_commit"]
    assert arm_compare._git(["branch", "--show-current"], project) == arm_compare.BRANCH
    assert not (project / "CLAUDE.md").exists() and not (project / ".agent-flow").exists()
    assert meta["principles"] is None and meta["approval_rules"] is None and "workflow" not in meta
    assert json.loads((run / "arm.json").read_text(encoding="utf-8"))["skills"] == skills
    assert "skills" not in v


def test_a_skill_that_names_agent_flow_commands_is_refused_unless_it_is_listed(tmp_path):
    kit = tmp_path / "kit"
    for name, body in (("plain", "Write tests first."), ("driver", "Run `agent-flow status` and continue."),
                       ("agent-flow", "Run `.agent-flow/bin/agent-flow run`.")):
        (kit / "skills" / name).mkdir(parents=True)
        (kit / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n{body}\n",
                                                        encoding="utf-8")
    project = tmp_path / "project"
    _git_init(project)
    (project / "a.txt").write_text("a\n", encoding="utf-8")
    arm_compare._git(["add", "a.txt"], project)
    arm_compare._git(["commit", "-qm", "seed"], project)
    with pytest.raises(SystemExit, match="driver"):
        arm_compare._install_skills(kit, project)
    (kit / "skills" / "driver" / "SKILL.md").unlink()
    record = arm_compare._install_skills(kit, project)
    assert record["names"] == ["plain"] and [item["name"] for item in record["excluded"]] == ["agent-flow"]


def test_an_external_case_commits_arm_vks_skills_past_an_ignored_claude_dir(tmp_path):
    case_dir, _ = _external_case(tmp_path)
    seed = tmp_path / "seed-repo"
    (seed / ".gitignore").write_text(".claude\n", encoding="utf-8")
    arm_compare._git(["add", ".gitignore"], seed)
    arm_compare._git(["commit", "-qm", "ignore"], seed)
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    case["seed"]["commit"] = arm_compare._git(["rev-parse", "HEAD"], seed)
    (case_dir / "case.json").write_text(json.dumps(case), encoding="utf-8")
    run = tmp_path / "runs" / "VK-1"
    meta = arm_compare.prepare(ROOT, "VK", "ignored", 1, run, case_dir=case_dir)
    project = run / "project"
    assert ".claude/skills/tdd/SKILL.md" in arm_compare._git(["ls-files", ".claude"], project).splitlines()
    assert arm_compare._git(["status", "--porcelain"], project) == ""
    assert meta["verify_gate"]["documents"] == ["prd.md"] and meta["skills"]["names"]
    arm_compare.prepare_followon(run, _followon_case(tmp_path), 1, tmp_path / "runs" / "VK-1.F1")
    assert not (tmp_path / "runs" / "VK-1.F1" / "project" / ".claude").exists()


def _skill_transcript(path: Path) -> Path:
    rows = [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Let me check the skills."},
            {"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": "tdd"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "loaded"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": ".claude/skills/qa/SKILL.md"}},
            {"type": "tool_use", "id": "t3", "name": "Skill", "input": {"skill": "no-such-skill"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t2", "content": "..."},
            {"type": "tool_result", "tool_use_id": "t3", "content": "Unknown skill", "is_error": True}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t4", "name": "Skill", "input": {"command": "/code-review", "args": "x"}},
            {"type": "tool_use", "id": "t5", "name": "Skill", "input": {"skill": "tdd"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t4", "content": "ok"},
                                                 {"type": "tool_result", "tool_use_id": "t5", "content": "ok"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done.\n\nEVAL-STATUS: complete"}]}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def test_skills_invoked_reads_the_skill_tool_calls_and_keeps_failed_ones_apart(tmp_path):
    rows = arm_compare.run_usage._jsonl(_skill_transcript(tmp_path / "t.jsonl"))
    assert arm_compare.skills_invoked(rows) == {"skills_invoked": ["tdd", "code-review", "tdd"],
                                                "skill_calls_failed": ["no-such-skill"]}
    assert arm_compare.skills_invoked([]) == {"skills_invoked": [], "skill_calls_failed": []}


def test_arm_vks_score_records_the_invoked_skills_and_the_summary_shows_them(tmp_path):
    run = tmp_path / "VK-1"
    arm_compare.prepare(ROOT, "VK", "devflow", 1, run)
    (run / "drive.json").write_text(json.dumps({"cost_usd": 2.0, "exit": 0}), encoding="utf-8")
    result = arm_compare.score(run, _skill_transcript(tmp_path / "t.jsonl"))
    assert result["skills_invoked"] == ["tdd", "code-review", "tdd"]
    assert result["skill_calls_failed"] == ["no-such-skill"]
    assert result["verify_gate"]["wiring_intact"] is True and result["skills"]["names"]
    assert json.loads((run / "result.json").read_text(encoding="utf-8"))["skills_invoked"] == result["skills_invoked"]
    rows = [_summary_row("A", skills_invoked=[]), _summary_row("VK", skills_invoked=["tdd", "qa", "tdd"]),
            _summary_row("VK", skills_invoked=[]), _summary_row("VK", skills_invoked=["qa"])]
    table = arm_compare.summarize(rows)
    assert "skills invoked (runs that invoked any: name×calls)" in table.splitlines()[0]
    vk = next(line for line in table.splitlines() if line.startswith("| VK |"))
    assert vk.endswith("| 2/3: qa×2, tdd×2 |")
    a = next(line for line in table.splitlines() if line.startswith("| A |"))
    assert a.endswith("| 0/1: none |")
    old = arm_compare.summarize([_summary_row("A")])
    assert next(line for line in old.splitlines() if line.startswith("| A |")).endswith("| - |")


def test_the_cli_and_preflight_take_arm_vk(tmp_path, monkeypatch):
    run = tmp_path / "VK-cli"
    assert arm_compare.main(["prepare", "--kit", str(ROOT), "--arm", "VK", "--case", "devflow",
                             "--output", str(run)]) == 0
    assert (run / "gate" / "verify_gate.py").is_file() and (run / "project" / ".claude" / "skills").is_dir()
    assert "VK" in arm_compare.ARMS and "VK" in arm_compare.GATE_ARMS and "VK" in arm_compare.BARE_ARMS
    # VK's reviewer runs on the controller's model, so the controller's preflight entry covers it.
    monkeypatch.setattr(arm_compare.subprocess, "run", lambda argv, **_: arm_compare.subprocess.CompletedProcess(
        argv, 0, "OK\n", ""))
    assert arm_compare.main(["preflight", "--model", "claude:claude-opus-5-5"]) == 0
