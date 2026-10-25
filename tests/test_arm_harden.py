"""#409 v3.14: opaque attempts, the graded snapshot, pinned models, the budget ledger, condition alignment,
content-based contamination and stratified metrics. Synthetic fixtures only."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src"))
import arm_compare  # noqa: E402
import attempts  # noqa: E402
import snapshot  # noqa: E402
from tests.test_arm_compare import _external_case, _git_init, _summary_row  # noqa: E402

_git = arm_compare._git


# ─── A. opaque attempts, never moved ───

def test_a_run_directory_is_an_opaque_id_and_the_assignment_lives_in_the_ledger(tmp_path):
    root = tmp_path / "runs"
    first = attempts.new_run(root, case="toy", layer="complete", arm="C", trial=1)
    second = attempts.new_run(root, case="toy", layer="complete", arm="A", trial=1)
    assert first.parent == root and first.name.startswith("r-") and first != second
    for name in (first.name, second.name):
        assert not any(word in name for word in ("toy", "complete", "C-1", "A-1"))
    rows = attempts.ledger(root)
    assert [(row["run_id"], row["arm"]) for row in rows] == [(first.name, "C"), (second.name, "A")]
    meta = arm_compare.prepare(ROOT, "A", "devflow", 1, first)
    assert meta["run_id"] == first.name and not (first / attempts.LEDGER).exists()


def test_the_cli_prepares_into_a_runs_root(tmp_path, capsys):
    assert arm_compare.main(["prepare", "--kit", str(ROOT), "--arm", "A", "--case", "devflow",
                             "--runs-root", str(tmp_path / "runs")]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["run_id"].startswith("r-") and attempts.ledger(tmp_path / "runs")[0]["run_id"] == printed["run_id"]


def test_the_assignment_check_names_missing_duplicate_and_unassigned_attempts():
    assignment = [{"case": "c", "layer": "l", "arm": arm, "trial": trial, "step": 1}
                  for arm in ("A", "C") for trial in (1, 2)]
    results = [{"case": "c", "layer": "l", "arm": "A", "trial": 1, "run_id": "r-1"},
               {"case": "c", "layer": "l", "arm": "A", "trial": 1, "run_id": "r-2"},
               {"case": "c", "layer": "l", "arm": "C", "trial": 1, "run_id": "r-3"},
               {"case": "c", "layer": "l", "arm": "V", "trial": 1, "run_id": "r-3"}]
    found = attempts.check(assignment, results)
    assert found["missing"] == [["c", "l", "A", 2, 1], ["c", "l", "C", 2, 1]]
    assert found["duplicate"] == [["c", "l", "A", 1, 1]] and found["duplicate_ids"] == ["r-3"]
    assert found["unassigned"] == [["c", "l", "V", 1, 1]]
    table = arm_compare.summarize([_summary_row("A", case="c", layer="l", trial=1)], assignment)
    assert "Assignment check: 1 results for 4 assigned attempts" in table and "- missing: 3" in table


def test_exclusions_use_the_fixed_taxonomy_and_are_listed_by_attempt():
    assert attempts.exclusion_code("sandbox: 2 processes outlived") == "sandbox"
    assert attempts.exclusion_code("leaked env") == "unclassified"
    with pytest.raises(ValueError):
        attempts.check_reason("moved aside")
    rows = [_summary_row("A"), _summary_row("C", run_id="r-9", harness_error="model-pin: kit reviewer x: codex")]
    table = arm_compare.summarize(rows)
    assert "| r-9 | None / - / 1 | C | model-pin | model-pin: kit reviewer x: codex |" in table


# ─── C. the graded snapshot ───

def test_the_snapshot_keeps_modes_symlinks_and_uncommitted_work_and_is_frozen_once(tmp_path):
    checkout = tmp_path / "checkout"
    _git_init(checkout)
    (checkout / "run.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (checkout / "run.sh").chmod(0o755)
    _git(["add", "run.sh"], checkout)
    _git(["commit", "-qm", "script"], checkout)
    start = _git(["rev-parse", "HEAD~1"], checkout)
    (checkout / "link").symlink_to("run.sh")
    (checkout / "draft.txt").write_text("not committed\n", encoding="utf-8")
    (checkout / "node_modules").mkdir()
    (checkout / "node_modules" / "big.js").write_text("x", encoding="utf-8")
    run = tmp_path / "run"
    run.mkdir()
    record = snapshot.freeze(checkout, run, start)
    entries = {row["path"]: row for row in snapshot.load(run)["entries"]}
    assert entries["run.sh"]["mode"] == 0o555 and entries["link"] == {"path": "link", "type": "symlink",
                                                                        "mode": entries["link"]["mode"],
                                                                        "target": "run.sh"}
    assert "node_modules/big.js" not in entries and record["has_uncommitted"] is True
    assert record["uncommitted"] == ["draft.txt", "link"] and record["head"] == _git(["rev-parse", "HEAD"], checkout)
    with pytest.raises(snapshot.SnapshotError):
        snapshot.freeze(checkout, run, start)
    copy = snapshot.materialize(run, tmp_path / "copy")
    assert _git(["rev-parse", "HEAD"], copy) == record["head"] and (copy / "link").is_symlink()
    assert os.access(copy / "run.sh", os.X_OK) and (copy / "draft.txt").is_file()
    assert _git(["rev-list", "--count", f"{start}..HEAD"], copy) == "1"
    assert sorted(line[3:] for line in _git(["status", "--porcelain"], copy).splitlines()) == ["draft.txt", "link"]
    (checkout / "run.sh").write_text("changed\n", encoding="utf-8")
    assert snapshot.verify(run)["tree_sha256"] == record["tree_sha256"]
    (run / "graded" / "tree" / "draft.txt").chmod(0o644)
    (run / "graded" / "tree" / "draft.txt").write_text("edited\n", encoding="utf-8")
    with pytest.raises(snapshot.SnapshotError):
        snapshot.verify(run)


def test_a_workflow_runs_checkout_must_be_recorded_by_drive(tmp_path):
    meta = {"arm": "C", "project": str(tmp_path / "project")}
    with pytest.raises(SystemExit, match="checkout"):
        arm_compare.recorded_checkout(tmp_path, meta, {"driven_at": "x"})
    path, record = arm_compare.recorded_checkout(tmp_path, meta, {"checkout": {"host": "/w/x", "head": "abc"}})
    assert path == Path("/w/x") and record["head"] == "abc"
    assert arm_compare.recorded_checkout(tmp_path, {**meta, "arm": "A"}, {})[0] == tmp_path / "project"


def test_a_worktree_made_inside_the_sandbox_is_found_at_its_host_path(tmp_path):
    project, home = tmp_path / "run" / "project", tmp_path / "run" / "sandbox" / "home"
    _git_init(project)
    host = home / ".agent-flow" / "worktrees" / "feat-x"
    _git(["worktree", "add", "-q", "-b", "feat/x", str(host)], project)
    (project / ".git" / "agent-flow" / "worktrees" / "feat-x" / ".agent-flow" / "runs" / "r1").mkdir(parents=True)
    gitdir = project / ".git" / "worktrees" / "feat-x" / "gitdir"
    gitdir.write_text("/root/.agent-flow/worktrees/feat-x/.git\n", encoding="utf-8")  # as git records it inside
    assert arm_compare.select_checkout(project, "C", home)[0] == host


def test_the_evaluator_gets_every_log_as_a_scan_and_a_browser_repeat_where_it_has_one(tmp_path):
    evaluator = tmp_path / "score.py"
    evaluator.write_text("import json, sys\nargs = sys.argv[1:]\n"
                         "# accepts --browser-repeat\n"
                         "json.dump({'S': 1, 'args': args}, open(args[args.index('--output') + 1], 'w'))\n",
                         encoding="utf-8")
    logs = [tmp_path / "a.jsonl", tmp_path / "b.jsonl"]
    out = arm_compare._grade_ui(tmp_path, logs, evaluator, None, tmp_path / "out.json",
                                arm_compare.BROWSER_REPEAT if arm_compare.evaluator_supports(evaluator,
                                                                                             "--browser-repeat") else None)
    args = out["args"]
    assert args.count("--scan") == 2 and args[args.index("--browser-repeat") + 1] == "2" and "--repeat" not in args


# ─── B. pinned models and the budget ledger ───

def test_workflow_arms_declare_claude_opus_low_reviewers_in_the_profile_override(tmp_path):
    from agent_flow.core.reviewer_launch import select_launch_candidate, validate_reviewer_launch_declaration
    import yaml
    meta = arm_compare.prepare(ROOT, "S", "devflow", 1, tmp_path / "S-1", variant={"reviewers": 2})
    override = tmp_path / "S-1" / "project" / meta["reviewer_pin"]["path"]
    declared = yaml.safe_load(override.read_text(encoding="utf-8"))
    validate_reviewer_launch_declaration(declared["execution"])
    rule = declared["execution"]["reviewers"][0]
    assert "match" not in rule
    candidate = select_launch_candidate(rule, provider="claude")
    assert (candidate.model, candidate.effort) == ("claude-opus-5-5", "low")
    assert len(declared["review_angles"]) == 1  # the variant's reviewer count, merged into the same file
    assert meta["variant"]["compound_treatment"]


def test_a_reviewer_or_simulator_on_another_model_is_a_pin_mismatch(tmp_path):
    project = tmp_path / "project"
    runs = project / ".git" / "agent-flow" / "worktrees" / "w" / ".agent-flow" / "runs" / "r1"
    runs.mkdir(parents=True)
    (runs / "multi-review-types-claude.md").write_text(
        "# x\n\n- provider: claude\n- model: claude-opus-5-5\n- effort: low\n", encoding="utf-8")
    (runs / "multi-review-types-codex.md").write_text(
        "# x\n\n- provider: codex\n- model: unspecified\n- effort: unspecified\n", encoding="utf-8")
    archive = project / ".git" / "agent-flow" / "archive" / "worktrees" / "w-step1" / ".agent-flow" / "runs" / "r0"
    archive.mkdir(parents=True)
    (archive / "old-codex.md").write_text("- provider: codex\n- model: gpt\n- effort: high\n", encoding="utf-8")
    meta = {"arm": "C", "project": str(project)}
    pins = arm_compare.verify_pins(tmp_path, meta, project, project, {"model": "claude-opus-5-5"}, [])
    assert pins["kit_reviewers_checked"] == 2  # the archive of an earlier chain step is not this run's
    assert any("codex" in item for item in pins["mismatches"]) and not any("old-codex" in m for m in pins["mismatches"])
    ok = arm_compare.verify_pins(tmp_path, {"arm": "A", "project": str(project)}, project, project,
                                 {"model": "claude-opus-5-5"}, [])
    assert ok["mismatches"] == []
    bad = arm_compare.verify_pins(tmp_path, {"arm": "A", "project": str(project),
                                             "router": {"model": "claude-opus-5-5", "effort": "medium"}},
                                  project, project, {"model": "claude-x"}, [])
    assert len(bad["mismatches"]) == 2


def test_the_ledger_is_incomplete_while_the_kit_reviewers_are_unpriced_and_the_cap_uses_their_floor(tmp_path):
    project = tmp_path / "project"
    runs = project / ".git" / "agent-flow" / "worktrees" / "w" / ".agent-flow" / "runs" / "r1"
    runs.mkdir(parents=True)
    (runs / "review-metrics-1.json").write_text(json.dumps({"jobs": [
        {"provider": "claude", "model": "claude-opus-5-5", "effort": "low", "status": "ok", "duration_s": 10,
         "prompt_utf8_bytes": 400_000, "stdout_utf8_bytes": 40_000}]}), encoding="utf-8")
    meta = {"arm": "C", "project": str(project)}
    ledger = arm_compare.budget_ledger(tmp_path, meta, 2.0, 0.1, project, project)
    assert ledger["total_cost_usd"] is None and ledger["cost_complete"] is False
    assert ledger["unpriced"] == ["kit_reviewers"] and ledger["kit_reviewer_jobs"] == 1
    assert ledger["enforced_usd"] == round(2.1 + ledger["kit_reviewer_cost_lower_bound_usd"], 4) > 2.1
    bare = arm_compare.budget_ledger(tmp_path, {"arm": "A", "project": str(project)}, 2.0, 0.0, project, project)
    assert bare["total_cost_usd"] == 2.0 and bare["cost_complete"] is True


def test_the_controller_gets_one_tool_list_and_the_pinned_effort():
    argv = arm_compare.controller_argv("t", "claude-opus-5-5", 3.0)
    assert argv[argv.index("--tools") + 1] == argv[argv.index("--allowedTools") + 1] == ",".join(arm_compare.CLAUDE_TOOLS)
    assert argv[argv.index("--effort") + 1] == "low" and "WebFetch" not in arm_compare.CLAUDE_TOOLS
    assert {"Bash", "Skill", "Task", "Read", "Edit", "Write"} <= set(arm_compare.CLAUDE_TOOLS)


def test_the_base_skill_set_is_the_clis_own_after_the_projects_skills(tmp_path):
    project = tmp_path / "project"
    (project / ".claude" / "skills" / "kit-skill").mkdir(parents=True)
    (project / ".claude" / "skills" / "kit-skill" / "SKILL.md").write_text("x", encoding="utf-8")
    init = {"skills": [*arm_compare.BASE_SKILLS, "kit-skill"]}
    assert arm_compare.base_skills(init, project)["matches_pin"] is True
    drifted = arm_compare.base_skills({"skills": [*arm_compare.BASE_SKILLS[1:], "operator-skill"]}, project)
    assert drifted["missing"] == [arm_compare.BASE_SKILLS[0]] and drifted["extra"] == ["operator-skill"]


def test_a_project_skill_shadowing_a_base_skill_is_the_arms_treatment(tmp_path):
    project = tmp_path / "project"
    shadow = arm_compare.BASE_SKILLS[0]
    (project / ".claude" / "skills" / shadow).mkdir(parents=True)
    (project / ".claude" / "skills" / shadow / "SKILL.md").write_text("x", encoding="utf-8")
    skills = arm_compare.base_skills({"skills": list(arm_compare.BASE_SKILLS)}, project)
    assert skills["matches_pin"] is True and skills["shadowed"] == [shadow] and skills["missing"] == []


# ─── D. condition alignment ───

def test_every_arm_gets_the_cases_note_in_the_task_text(tmp_path):
    case_dir, _ = _external_case(tmp_path)
    bare = tmp_path / "runs" / "A-1"
    arm_compare.prepare(ROOT, "A", "x", 1, bare, case_dir=case_dir)
    workflow = tmp_path / "runs" / "C-1"
    arm_compare.prepare(ROOT, "C", "x", 1, workflow, case_dir=case_dir)
    a = (bare / "prompt.md").read_text(encoding="utf-8")
    c = (workflow / "prompt.md").read_text(encoding="utf-8")
    task = "Build the cart.\n\nRun ./gradlew test."
    assert a.startswith(f"# Task\n\n{task}\n\n") and c.startswith(f"# Task\n\n{task}\n\n")
    assert a.endswith(arm_compare.DECLARATION) and c.endswith(arm_compare.DECLARATION)
    assert "no\ngit remote" in arm_compare.DECLARATION


def test_the_seed_is_an_export_of_the_pinned_commit_only(tmp_path):
    case_dir, _ = _external_case(tmp_path)
    seed = tmp_path / "seed-repo"
    (seed / "future.txt").write_text("a later build\n", encoding="utf-8")
    _git(["add", "future.txt"], seed)
    _git(["commit", "-qm", "future"], seed)
    _git(["tag", "v-future"], seed)
    run = tmp_path / "runs" / "A-1"
    meta = arm_compare.prepare(ROOT, "A", "x", 1, run, case_dir=case_dir)
    project = run / "project"
    assert _git(["log", "--all", "--format=%s"], project).splitlines() == ["task", "seed"]
    assert _git(["tag"], project) == "" and not (project / "future.txt").exists()
    objects = _git(["cat-file", "--batch-all-objects", "--batch-check"], project)
    assert "a later build" not in objects and meta["seed_export"]["tree"]


def test_the_harness_polls_with_read_only_status_and_approves_with_continue(tmp_path):
    project = tmp_path / "project"
    _git_init(project)
    worktree = tmp_path / "wt" / "feat-x"
    _git(["worktree", "add", "-q", "-b", "feat/x", str(worktree)], project)
    (project / ".git" / "agent-flow" / "worktrees" / "feat-x" / ".agent-flow" / "runs" / "r1").mkdir(parents=True)
    calls = []

    def call(argv, cwd):
        calls.append(argv)
        return type("R", (), {"returncode": 0, "stdout": 'status_json: {"reason": "phase_approval_required", '
                                                          '"current_phase": "prd", "run": "full-feature/r1", '
                                                          '"approval_command": "agent-flow continue --root /p '
                                                          '--worktree feat-x --approve t"}\n'})()
    status = arm_compare.runner_status(project, None, call)
    assert calls[-1][1] == "status" and status["reason"] == "phase_approval_required"
    assert arm_compare.approve_phase(project, status, call) and calls[-1][1:3] == ["continue", "--root"]


def test_the_runner_call_gets_the_controllers_environment_and_a_timeout(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "operator")
    monkeypatch.setattr(arm_compare.c_prime, "_sh", lambda argv, cwd, **kw: seen.update(kw) or
                        type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    arm_compare.runner_call(None, None, ["true"], tmp_path, 42)
    assert seen["timeout"] == 42 and "CLAUDE_CODE_SESSION_ID" not in seen["env"]
    assert seen["env"]["AGENT_FLOW_REVIEWERS"] == "claude"


def test_a_gate_error_counts_as_a_failure_apart_from_infra_errors():
    row = _summary_row("V", verify_gate={"gate_errors": 1, "blocks": 0, "review_findings": 0,
                                         "reviewer_cost_usd": 0.0})
    assert arm_compare.run_status(row) == "gate-error" and not arm_compare._succeeded(row)
    line = next(item for item in arm_compare.summarize([row]).splitlines() if item.startswith("| V |"))
    assert "| 1 | 0/1 |" in line


# ─── C. content-based contamination ───

def _rows(command: str, output: str, *, error: bool = False) -> list[dict]:
    return [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                           "input": {"command": command}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": output,
                                                      "is_error": error}]}}]


def test_canaries_in_any_log_are_contamination_and_listings_are_not(tmp_path, monkeypatch):
    monkeypatch.setenv(arm_compare.HIDDEN_ROOTS_ENV, "/srv/hidden")
    run = tmp_path / "run"
    run.mkdir()
    log = run / "verify-gate.jsonl"
    log.write_text(json.dumps({"review_text": "found CANARY-test-123 in a file"}) + "\n", encoding="utf-8")
    clean = run / "controller.jsonl"
    clean.write_text("{}\n", encoding="utf-8")
    listed = arm_compare.scan_contamination(run, _rows("ls /srv", "/srv/hidden\n/srv/other"), [clean],
                                            ["CANARY-test-123"])
    assert listed["contaminated"] is False and listed["hidden_path_listed"] == 1 and listed["hidden_path_reads"] == 0
    missing = arm_compare.scan_contamination(run, _rows("cat /srv/hidden/checks.json", "cat: No such file or directory",
                                                        error=True), [clean], [])
    assert missing["contaminated"] is False and missing["hidden_path_named"] == 1
    read = arm_compare.scan_contamination(run, _rows("cat /srv/hidden/checks.json", '{"expected": 3}'), [clean], [])
    assert read["contaminated"] is True and read["hidden_path_reads"] == 1
    own = arm_compare.scan_contamination(run, _rows(f"cat {run}/prompt.md", "task"), [clean], [])
    assert own["hidden_path_named"] == 0
    hit = arm_compare.scan_contamination(run, [], [clean, log], ["CANARY-test-123"])
    assert hit["contaminated"] is True and hit["canary_hits"] == {str(log): 1}
    disclosed = arm_compare.scan_contamination(run, [], [log], ["CANARY-test-123"], disclosed="CANARY-test-123")
    assert disclosed["contaminated"] is False and disclosed["canaries_disclosed_by_oracle"] == 1


def test_the_canaries_come_from_the_case_and_only_their_digest_is_in_the_run(tmp_path):
    case_dir, evaluator = _external_case(tmp_path)
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    case["canaries"] = ["CANARY-synthetic-1", "CANARY-synthetic-2"]
    (case_dir / "case.json").write_text(json.dumps(case), encoding="utf-8")
    run = tmp_path / "runs" / "A-1"
    meta = arm_compare.prepare(ROOT, "A", "x", 1, run, case_dir=case_dir)
    assert meta["canaries_sha256"] and "CANARY-synthetic" not in (run / "arm.json").read_text(encoding="utf-8")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "x", "content": "CANARY-synthetic-2"}]}}) + "\n", encoding="utf-8")
    result = arm_compare.score(run, transcript, evaluator, case_dir=case_dir)
    assert result["contaminated"] is True and result["contamination_scan"]["canary_hits"] == {str(transcript): 1}


HEALTHY_SANDBOX = {"mode": "required", "checks": {"host_read_only": True, "hidden": ["/home"], "hidden_leaks": []},
                   "errors": [], "survivors": []}


def _hidden_read_run(tmp_path, monkeypatch, sandbox_record, canary_text="nothing"):
    monkeypatch.setenv(arm_compare.HIDDEN_ROOTS_ENV, "/srv/hidden")
    case_dir, evaluator = _external_case(tmp_path)
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    case["canaries"] = ["CANARY-synthetic-9"]
    (case_dir / "case.json").write_text(json.dumps(case), encoding="utf-8")
    run = tmp_path / "runs" / "C-1"
    arm_compare.prepare(ROOT, "A", "x", 1, run, case_dir=case_dir)
    (run / "drive.json").write_text(json.dumps({"cost_usd": 1.0, "exit": 0,
                                                **({"sandbox": sandbox_record} if sandbox_record else {})}),
                                    encoding="utf-8")
    transcript = tmp_path / "t.jsonl"
    # the agent made its own file under a hidden root (inside the sandbox that root is an empty tmpfs) and read it
    transcript.write_text("".join(json.dumps(row) + "\n" for row in _rows("cat /srv/hidden/notes.txt", canary_text)),
                          encoding="utf-8")
    return arm_compare.score(run, transcript, evaluator, case_dir=case_dir)


def test_a_hidden_path_read_in_a_healthy_sandbox_is_a_diagnostic_not_contamination(tmp_path, monkeypatch):
    result = _hidden_read_run(tmp_path, monkeypatch, HEALTHY_SANDBOX)
    assert result["contamination_scan"]["hidden_path_reads"] == 1
    assert result["contamination_rule"] == "content" and result["contaminated"] is False
    assert result["contamination_evidence"]["hidden_path_reads"] == 1
    assert arm_compare.run_status(result) == "ok"


def test_a_canary_hit_in_a_healthy_sandbox_is_contamination(tmp_path, monkeypatch):
    result = _hidden_read_run(tmp_path, monkeypatch, HEALTHY_SANDBOX, canary_text="CANARY-synthetic-9")
    assert result["contamination_rule"] == "content" and result["contaminated"] is True
    assert arm_compare.run_status(result) == "contaminated"


@pytest.mark.parametrize("record", [None, {**HEALTHY_SANDBOX, "checks": {"host_read_only": False, "hidden_leaks": []}},
                                    {**HEALTHY_SANDBOX, "checks": {"host_read_only": True, "hidden_leaks": ["/home/x"]}},
                                    {**HEALTHY_SANDBOX, "mode": "off"}])
def test_without_a_healthy_sandbox_a_hidden_path_read_is_still_contamination(tmp_path, monkeypatch, record):
    result = _hidden_read_run(tmp_path, monkeypatch, record)
    assert result["contamination_rule"] == "path" and result["contaminated"] is True


def test_the_rule_is_applied_from_the_evidence_by_run_status_the_chain_and_the_search():
    sys.path.insert(0, str(ROOT / "evals"))
    import arm_search
    diagnostic = {"contaminated": True, "oracle": True, "declaration": "declared-complete",
                  "contamination_evidence": {"sandbox_healthy": True, "canary_hits": 0, "evaluator_read": False,
                                             "hidden_path_reads": 2, "oracle_reads": 1, "chain_reads": 1,
                                             "phase_hidden_markers": True, "tripwire_changed": True}}
    assert arm_compare.run_status(diagnostic) == "ok"
    assert arm_search.outcome(diagnostic, {})["run_status"] == "ok"
    unhealthy = {**diagnostic, "contamination_evidence": {**diagnostic["contamination_evidence"],
                                                          "sandbox_healthy": False}}
    assert arm_compare.run_status(unhealthy) == "contaminated"
    assert arm_search.outcome(unhealthy, {})["run_status"] == "contaminated"
    evaluator = {**diagnostic, "contamination_evidence": {**diagnostic["contamination_evidence"],
                                                          "evaluator_read": True}}
    assert arm_compare.run_status(evaluator) == "contaminated"
    assert "contaminated" in arm_compare.CHAIN_REFUSED
    assert arm_compare.run_status({"contaminated": True}) == "contaminated"  # scored before the evidence existed


# ─── E. metrics ───

def test_the_summary_never_pools_strata_and_pairs_false_complete_with_false_block():
    rows = [_summary_row("A", case="c1"), _summary_row("C", False, case="c1"),
            _summary_row("A", case="c2", layer="underspec"),
            _summary_row("C", True, case="c2", layer="underspec", declaration="declared-blocked",
                         final="EVAL-STATUS: blocked: x")]
    table = arm_compare.summarize(rows)
    assert "## case c1, layer -, chain step 1" in table and "## case c2, layer underspec, chain step 1" in table
    c_lines = [line for line in table.splitlines() if line.startswith("| C | 1 |")]
    assert len(c_lines) == 2
    assert "| 1/1 / 0/0 |" in c_lines[0] and "| 0/0 / 1/1 |" in c_lines[1]


def test_completeness_comes_from_the_hidden_checks_for_every_arm():
    import review_findings
    failures = review_findings.failures_from(
        {"failed_checks": ["A1", "A2@mobile", "B1"], "api_failures": {"A1": "404 Not Found",
                                                                       "B1": "total: got 3, want 4"},
         "ui_failures": {"A2@mobile": "locator.click: Timeout 5000ms exceeded"}}, {"S": 0, "G": 0.5})
    assert (failures["missing"], failures["wrong"], failures["no_build"]) == (2, 1, False)
    rows = [_summary_row("A", False, no_start=False, complete_build=False),
            _summary_row("A", False, no_start=True, complete_build=False)]
    line = next(item for item in arm_compare.summarize(rows).splitlines() if item.startswith("| A |"))
    assert "| 1 / 0 of 2 |" in line


def test_chain_metrics_leave_the_archive_out_and_carry_only_kit_managed_paths(tmp_path):
    project = tmp_path / "project"
    _git_init(project)
    archived = project / ".git" / "agent-flow" / "archive" / "worktrees" / "w-step1" / ".agent-flow" / "runs" / "r0"
    archived.mkdir(parents=True)
    (archived / arm_compare.MANUAL_APPROVALS).write_text(json.dumps({"approvals": [1]}), encoding="utf-8")
    assert arm_compare.manual_spec_approvals(project, project) == 0
    (project / ".git" / "info" / "exclude").write_text("# comment\n/.agent-flow/\n/.claude/\n*.log\n!keep\n",
                                                       encoding="utf-8")
    assert arm_compare.kit_managed_paths(project) == [".agent-flow", ".claude"]
    (project / ".agent-flow" / "profiles").mkdir(parents=True)
    (project / ".agent-flow" / "profiles" / "x.local.yaml").write_text("a: 1\n", encoding="utf-8")
    (project / ".gitignore").write_text(".env\ndata/\n", encoding="utf-8")
    (project / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (project / "data").mkdir()
    target = tmp_path / "next"
    _git_init(target)
    copied = arm_compare._carry_kit_state(project, target)
    assert copied == [".agent-flow"] and not (target / ".env").exists() and not (target / "data").exists()


def test_the_sandbox_shows_only_the_clis_credential_files_read_only(tmp_path, monkeypatch):
    token = tmp_path / ".oauth_token"
    token.write_text("t", encoding="utf-8")
    monkeypatch.setenv(arm_compare.AUTH_FILES_ENV, f"{token}:{tmp_path / 'absent'}")
    assert arm_compare.auth_exposes() == [{"path": str(token), "mode": "ro"}]
    run = tmp_path / "run"
    (run / "project").mkdir(parents=True)
    box = arm_compare.run_box(run, {"arm": "A", "project": str(run / "project")}, run / "project", 10, "t")
    assert {"path": str(token), "mode": "ro"} in box["expose"] and box["env"]["HOME"] == "/root"
    assert box["home"] == str(run / "sandbox" / "home")


def test_the_agents_own_tmp_files_are_not_hidden_paths(monkeypatch):
    monkeypatch.delenv(arm_compare.HIDDEN_ROOTS_ENV, raising=False)
    own_task_output = "/tmp/claude-0/-tmp-run-project/abc/tasks/x.output"
    assert arm_compare._hidden_paths(f"cat {own_task_output}", arm_compare.hidden_roots(), []) == []
    monkeypatch.setenv(arm_compare.HIDDEN_ROOTS_ENV, "/tmp/claude-0/-home-user/operator-session")
    assert arm_compare._hidden_paths("cat /tmp/claude-0/-home-user/operator-session/x/prd.md",
                                     arm_compare.hidden_roots(), [])
