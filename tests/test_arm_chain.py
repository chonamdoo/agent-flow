"""H2-① chains: `chain-next` runs the same arm again, in a new session, on its own previous result."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
import arm_compare  # noqa: E402
from tests.test_arm_router import _answer, fake_router  # noqa: E402,F401  (fixture)

EVALUATOR = ("import json, sys\n"
             "args = sys.argv[1:]\n"
             "out = args[args.index('--output') + 1]\n"
             "json.dump({'S': 1, 'G': 0.5, 'contamination': []}, open(out, 'w'))\n")


def _git(args, cwd):
    return arm_compare._git(args, cwd)


def _cases(tmp_path):
    """A synthetic seed repository, step 1 (`files`) and step 2 (`layers`, one with a simulated user)."""
    seed = tmp_path / "seed-repo"
    seed.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.name", "t"], ["config", "user.email", "t@e"],
                 ["config", "commit.gpgsign", "false"]):
        _git(args, seed)
    (seed / "app.txt").write_text("v0 app\n", encoding="utf-8")
    _git(["add", "app.txt"], seed)
    _git(["commit", "-qm", "app"], seed)
    private = tmp_path / "private" / "toy"
    steps = {}
    for name in ("s1", "s2"):
        case_dir = private / name
        (case_dir / "task").mkdir(parents=True)
        (case_dir / "checks").mkdir()
        (case_dir / "checks" / "hidden.txt").write_text(f"secret {name}\n", encoding="utf-8")
        (case_dir / "score.py").write_text(EVALUATOR, encoding="utf-8")
        steps[name] = case_dir
    (steps["s1"] / "task" / "prd.md").write_text("# toy v0\n", encoding="utf-8")
    (steps["s1"] / "case.json").write_text(json.dumps({
        "name": "toy-s1", "seed": {"repository": str(seed), "commit": _git(["rev-parse", "HEAD"], seed)},
        "files": {"prd.md": "task/prd.md"}, "task": "Build toy v0.", "profile": "android", "install": [],
        "verify_command": "true", "score_keys": ["S", "G"]}), encoding="utf-8")
    (steps["s2"] / "task" / "prd-v1.md").write_text("# toy v1\n", encoding="utf-8")
    (steps["s2"] / "task" / "change.md").write_text("add the v1 change\n", encoding="utf-8")
    (steps["s2"] / "oracle.md").write_text("the owner wants blue\n", encoding="utf-8")
    (steps["s2"] / "case.json").write_text(json.dumps({
        "name": "toy-s2", "files": {"prd.md": "task/prd-v1.md"}, "task": "Develop toy v1 on the existing code.",
        "profile": "android", "install": [], "verify_command": "echo step2", "score_keys": ["S", "G"],
        "layers": {"complete": {"docs/change.md": "task/change.md"},
                   "underspec": {"files": {"docs/change.md": "task/change.md"}, "user_sim": {"oracle": "oracle.md"}}}}),
        encoding="utf-8")
    return steps


def _finish(run, *, commit="work.txt", uncommitted="draft.txt", cwd=None, drive=None):
    """What a driven arm leaves: a committed change, an uncommitted one, drive.json; then the harness scores it."""
    cwd = cwd or run / "project"
    (cwd / commit).write_text(f"{run.name} work\n", encoding="utf-8")
    _git(["add", commit], cwd)
    _git(["commit", "-qm", f"work of {run.name}"], cwd)
    if uncommitted:
        (cwd / uncommitted).write_text("left uncommitted\n", encoding="utf-8")
    (run / "drive.json").write_text(json.dumps(drive or {"cost_usd": 1.0, "exit": 0, "model": "claude-opus-5-5",
                                                         "effort": None}), encoding="utf-8")
    return _git(["rev-parse", "HEAD"], cwd)


def _score(run, case_dir):
    return arm_compare.score(run, None, case_dir / "score.py")


def test_chain_next_on_arm_a_builds_on_the_previous_head_and_overlays_the_next_task(tmp_path):
    steps = _cases(tmp_path)
    first = tmp_path / "work" / "A-1"
    arm_compare.prepare(ROOT, "A", "ignored", 1, first, case_dir=steps["s1"])
    head = _finish(first)
    _score(first, steps["s1"])
    second = tmp_path / "work" / "A-1.s2"
    assert arm_compare.main(["chain-next", str(first), "--case-dir", str(steps["s2"]), "--layer", "complete",
                             "--output", str(second)]) == 0
    meta = json.loads((second / "arm.json").read_text(encoding="utf-8"))
    project = second / "project"
    assert meta["chain"]["step"] == 2 and meta["chain"]["prev_run"] == str(first.resolve())
    assert meta["chain"]["prev_head"] == head and meta["chain"]["root_run"] == str(first.resolve())
    assert meta["arm"] == "A" and meta["case"] == "toy-s2" and meta["case_kind"] == "external"
    assert meta["layer"] == "complete" and meta["trial"] == 1 and meta["score_keys"] == ["S", "G"]
    assert meta["chain"]["uncommitted_carried"] == ["draft.txt"]
    # The previous head is in the new history; the arm's work and its uncommitted draft are there; the task overlays.
    _git(["merge-base", "--is-ancestor", head, "HEAD"], project)
    assert (project / "work.txt").read_text(encoding="utf-8") == "A-1 work\n"
    assert (project / "draft.txt").is_file() and (project / "app.txt").is_file()
    assert (project / "prd.md").read_text(encoding="utf-8") == "# toy v1\n"
    assert (project / "docs" / "change.md").read_text(encoding="utf-8") == "add the v1 change\n"
    assert _git(["log", "-2", "--format=%s"], project).splitlines() == [
        arm_compare.CHAIN_SEED.format(step=2), "uncommitted work of the previous step"]
    assert meta["start_commit"] == _git(["rev-parse", "HEAD"], project)
    assert _git(["branch", "--show-current"], project) == arm_compare.BRANCH
    assert _git(["remote"], project) == "" and _git(["status", "--porcelain"], project) == ""
    # Nothing hidden or private reaches the project or the record.
    assert not (project / "checks").exists() and not (project / "score.py").exists()
    assert not (project / "oracle.md").exists() and not (second / "user-sim").exists()
    assert str(tmp_path / "private") not in (second / "arm.json").read_text(encoding="utf-8")
    prompt = (second / "prompt.md").read_text(encoding="utf-8")
    assert "Develop toy v1 on the existing code." in prompt and "Build toy v0." not in prompt
    # score works unchanged; a third step keeps the root and the whole line of earlier runs.
    _finish(second, commit="work2.txt", uncommitted=None)
    result = _score(second, steps["s2"])
    assert result["oracle"] is True and result["chain"]["step"] == 2 and result["commits"] == 1
    third = arm_compare.prepare_chain_next(second, steps["s2"], tmp_path / "work" / "A-1.s3", "complete")
    assert third["chain"]["step"] == 3 and third["chain"]["root_run"] == str(first.resolve())
    assert third["chain"]["prior_runs"] == [str(first.resolve()), str(second.resolve())]
    assert (tmp_path / "work" / "A-1.s3" / "project" / "work2.txt").is_file()


def test_chain_next_on_a_workflow_arm_keeps_its_kit_state_and_runs_and_reinstalls_the_hooks(tmp_path):
    steps = _cases(tmp_path)
    first = tmp_path / "work" / "C-1"
    arm_compare.prepare(ROOT, "C", "ignored", 1, first, case_dir=steps["s1"])
    leader = first / "project"
    # What the arm left: work in the runner's worktree, a runner run with its plan, kit state it wrote in the leader.
    worktree = tmp_path / "wt" / "feat-toy"
    _git(["worktree", "add", "-q", "-b", "feat/toy", str(worktree)], leader)
    runs = leader / ".git" / "agent-flow" / "worktrees" / "feat-toy" / ".agent-flow" / "runs" / "r1"
    (runs / "artifacts").mkdir(parents=True)
    (runs / "artifacts" / "plan.md").write_text("# the arm's plan for v0\n", encoding="utf-8")
    (runs / "active").write_text("", encoding="utf-8")
    (leader / ".agent-flow" / "local-skills" / "toy-notes").mkdir(parents=True)
    (leader / ".agent-flow" / "local-skills" / "toy-notes" / "SKILL.md").write_text("notes\n", encoding="utf-8")
    (leader / ".agent-flow" / "state" / "current-run.json").write_text("{}", encoding="utf-8")
    head = _finish(first, cwd=worktree)
    _score(first, steps["s1"])
    second = tmp_path / "work" / "C-1.s2"
    meta = arm_compare.prepare_chain_next(first, steps["s2"], second, "complete")
    project = second / "project"
    assert meta["arm"] == "C" and meta["workflow"] == "full-feature" and meta["chain"]["prev_head"] == head
    assert meta["approval_rules"] == arm_compare.HARNESS_APPROVES
    _git(["merge-base", "--is-ancestor", head, "HEAD"], project)
    assert _git(["branch", "--show-current"], project) == "main"
    assert (project / "work.txt").is_file() and (project / "draft.txt").is_file()
    assert (project / "prd.md").read_text(encoding="utf-8") == "# toy v1\n"
    # The kit is installed again from the same kit copy: its hooks name the new project, never the previous one.
    assert (project / ".agent-flow" / "bin" / "agent-flow").exists()
    settings = (project / ".claude" / "settings.json").read_text(encoding="utf-8")
    assert str(project.resolve()) in settings and str(leader.resolve()) not in settings
    launcher = (project / ".agent-flow" / "bin" / "agent-flow").read_text(encoding="utf-8")
    assert f"'{second.resolve() / 'kit'}'" in launcher and f"'{first.resolve() / 'kit'}'" not in launcher
    assert (project / ".agent-flow" / "local-skills" / "toy-notes" / "SKILL.md").is_file()
    assert not (project / ".agent-flow" / "state" / "current-run.json").exists()
    # The earlier run is archived where the kit keeps finished runs, not as a live worktree.
    archived = project / ".git" / "agent-flow" / "archive" / "worktrees" / "feat-toy-step1" / ".agent-flow" / "runs"
    assert (archived / "r1" / "artifacts" / "plan.md").read_text(encoding="utf-8") == "# the arm's plan for v0\n"
    assert not (archived / "r1" / "active").exists()
    assert meta["chain"]["archived_runs"] == ["archive/worktrees/feat-toy-step1/.agent-flow/runs/r1"]
    assert not (project / ".git" / "agent-flow" / "worktrees" / "feat-toy").exists()
    assert arm_compare.linked_worktrees(project) == []
    assert arm_compare.select_checkout(project, "C")[0] == project
    prompt = (second / "prompt.md").read_text(encoding="utf-8")
    assert "--workflow full-feature" in prompt and "Develop toy v1" in prompt
    assert _git(["status", "--porcelain"], project) == ""


def test_chain_next_gives_arm_v_this_steps_gate_and_simulated_user(tmp_path):
    steps = _cases(tmp_path)
    first = tmp_path / "work" / "V-1"
    arm_compare.prepare(ROOT, "V", "ignored", 1, first, case_dir=steps["s1"])
    _finish(first)
    (first / "verify-gate.jsonl").write_text("", encoding="utf-8")
    _score(first, steps["s1"])
    second = tmp_path / "work" / "V-1.s2"
    meta = arm_compare.prepare_chain_next(first, steps["s2"], second, "underspec")
    project = second / "project"
    stops = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]["Stop"]
    (command,) = [hook["command"] for entry in stops for hook in entry["hooks"]]
    assert str((second / "gate").resolve()) in command and str((first / "gate").resolve()) not in command
    assert meta["verify_gate"]["verify_command"] == ["echo", "step2"]
    assert meta["verify_gate"]["documents"] == ["docs/change.md", "prd.md"]
    config = json.loads((second / "gate" / "config.json").read_text(encoding="utf-8"))
    assert config["project"] == str(project.resolve()) and "Develop toy v1" in config["task"]
    assert meta["user_sim"] and (second / "user-sim" / "ask-user").is_file()
    assert str(second / "user-sim" / "ask-user") in (second / "prompt.md").read_text(encoding="utf-8")
    assert not any(path.read_bytes() == b"the owner wants blue\n" for path in project.rglob("*") if path.is_file())


def test_arm_r_reuses_the_previous_frozen_decision_without_calling_the_router(tmp_path, fake_router):
    steps = _cases(tmp_path)
    calls = fake_router(_answer("none"))
    first = tmp_path / "work" / "R-1"
    prepared = arm_compare.prepare(ROOT, "R", "ignored", 1, first, case_dir=steps["s1"])
    assert prepared["effective_arm"] == "A" and len(calls()) == 1
    _finish(first)
    _score(first, steps["s1"])
    second = tmp_path / "work" / "R-1.s2"
    meta = arm_compare.prepare_chain_next(first, steps["s2"], second, "complete")
    assert len(calls()) == 1 and not (second / "router").exists()
    assert meta["arm"] == "R" and meta["effective_arm"] == "A"
    assert meta["router"]["choice"] == "none" and meta["router"]["reused"] is True
    assert meta["router"]["reused_from"] == str(first.resolve()) and meta["router"]["cost_usd"] == 0.0
    assert meta["router"]["original_cost_usd"] == prepared["router"]["cost_usd"]
    assert meta["router_frozen_at"] == prepared["router_frozen_at"]
    _finish(second, commit="work2.txt", uncommitted=None)
    result = _score(second, steps["s2"])
    assert result["total_cost_usd"] == 1.0 and result["router_cost_usd"] == 0.0


@pytest.mark.parametrize("damage, message", [
    ({"drop": "drive.json"}, "has no drive.json"),
    ({"drop": "result.json"}, "has no result.json"),
    ({"result": {"contaminated": True}}, "is contaminated"),
    ({"result": {"drive_exit": 1}}, "is infra-error"),
    ({"result": {"harness_error": "broken"}}, "is harness-error"),
])
def test_chain_next_refuses_a_previous_run_it_cannot_build_on(tmp_path, damage, message):
    steps = _cases(tmp_path)
    first = tmp_path / "work" / "A-1"
    arm_compare.prepare(ROOT, "A", "ignored", 1, first, case_dir=steps["s1"])
    _finish(first)
    _score(first, steps["s1"])
    if "drop" in damage:
        (first / damage["drop"]).unlink()
    else:
        result = json.loads((first / "result.json").read_text(encoding="utf-8"))
        (first / "result.json").write_text(json.dumps({**result, **damage["result"]}), encoding="utf-8")
    with pytest.raises(SystemExit, match=message):
        arm_compare.prepare_chain_next(first, steps["s2"], tmp_path / "work" / "A-1.s2", "complete")
    assert not (tmp_path / "work" / "A-1.s2").exists()


def test_chain_next_refuses_a_checkout_changed_after_scoring(tmp_path):
    steps = _cases(tmp_path)
    first = tmp_path / "work" / "A-1"
    arm_compare.prepare(ROOT, "A", "ignored", 1, first, case_dir=steps["s1"])
    _finish(first)
    _score(first, steps["s1"])
    (first / "project" / "late.txt").write_text("after scoring\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="changed after it was scored"):
        arm_compare.prepare_chain_next(first, steps["s2"], tmp_path / "work" / "A-1.s2", "complete")


def test_a_chain_step_is_driven_in_a_new_session_on_the_same_model_and_flags_reads_of_earlier_grades(
        tmp_path, monkeypatch):
    steps = _cases(tmp_path)
    first = tmp_path / "work" / "A-1"
    arm_compare.prepare(ROOT, "A", "ignored", 1, first, case_dir=steps["s1"])
    _finish(first)
    _score(first, steps["s1"])
    second = tmp_path / "work" / "A-1.s2"
    arm_compare.prepare_chain_next(first, steps["s2"], second, "complete")
    started = []

    def controller_once(argv, project, out, err, timeout):
        started.append(argv)
        out.write((json.dumps({"type": "system", "subtype": "init", "session_id": "new-session"}) + "\n" +
                   json.dumps({"type": "result", "total_cost_usd": 1.0, "result": "EVAL-STATUS: complete"})
                   + "\n").encode())
        return 0, False

    monkeypatch.setattr(arm_compare, "_controller_once", controller_once)
    monkeypatch.delenv("CLAUDE_CODE_EFFORT_LEVEL", raising=False)
    with pytest.raises(SystemExit, match="same model"):
        arm_compare.drive(second, "claude-other", 600, 30.0)
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "high")
    with pytest.raises(SystemExit, match="same effort"):
        arm_compare.drive(second, "claude-opus-5-5", 600, 30.0)
    monkeypatch.delenv("CLAUDE_CODE_EFFORT_LEVEL")
    record = arm_compare.drive(second, "claude-opus-5-5", 600, 30.0)
    (argv,) = started
    assert "--resume" not in argv and "--continue" not in argv and record["session_id"] == "new-session"
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "message": {"id": "m", "content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": str(first.resolve() / "ui-score.json")}}]}})
        + "\n", encoding="utf-8")
    result = arm_compare.score(second, transcript, steps["s2"] / "score.py")
    assert result["chain_reads"] == 1 and result["contaminated"] is True


def test_the_summary_groups_chains_by_root_and_counts_the_steps_survived():
    def row(run, oracle=True, g=0.5, step=None, root=None, **extra):
        base = {"arm": "C", "trial": 1, "project": f"/w/{run}/project", "oracle": oracle,
                "declaration": "declared-complete", "final_message": "EVAL-STATUS: complete", "task_kind": "normal",
                "usage": {"output": 1, "uncached_input": 1, "cached_input": 1}, "ui": {"S": int(oracle), "G": g},
                "drive_exit": 0, **extra}
        return {**base, "chain": {"step": step, "root_run": f"/w/{root}"}} if step else base
    rows = [row("C-1", g=0.9), row("C-1.s2", step=2, root="C-1", g=0.8), row("C-1.s3", oracle=False, step=3, root="C-1"),
            row("C-1.s4", step=4, root="C-1"),
            row("C-2"), row("C-2.s2", step=2, root="C-2", contaminated=True), row("C-2.s3", step=3, root="C-2"),
            row("C-3.s2", step=2, root="C-3")]
    table = arm_compare.summarize(rows)
    assert "Chains (the same arm, each step a new session on its own previous result):" in table
    lines = table.splitlines()
    assert ("| C-1 | C | 4 | 1: S=1 G=0.9; 2: S=1 G=0.8; 3: S=0 G=0.5; 4: S=1 G=0.5 | 2/4 |") in lines
    assert "| C-2 | C | 3 | 1: S=1 G=0.5; 2: S=1 G=0.5 (contaminated); 3: S=1 G=0.5 | 1/3 |" in lines
    assert "| C-3 | C | 2 | 1: missing; 2: S=1 G=0.5 | 0/2 |" in lines
    assert "Chains" not in arm_compare.summarize([row("C-1")])
