"""#415: slice-plan `- Files:` 밖의 변경은 사람 확인으로 간다. 의미 판정은 하지 않는다(#222)."""
import subprocess
from pathlib import Path

import pytest

from agent_flow.artifact import approve_phase_artifact, create_run, pending_phase_approval, read_meta, write_meta
from agent_flow.core.local_skills import out_of_plan_paths
from agent_flow.core.phase_workflow import find_kit_root, load_phase_workflow_definition
from agent_flow.core.worktree_isolation import WorktreeIsolationError, resolve_run_subpath
from agent_flow.runner import Runner


def _git(project: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(project), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   check=True, capture_output=True)


def _comment_authoring_run(tmp_path, monkeypatch, workflow, files="src/app.py"):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (project / "src" / "other.py").write_text("y = 1\n", encoding="utf-8")
    _git(project, "init", "-q")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "seed")
    definition = load_phase_workflow_definition(find_kit_root(), workflow)
    ids = [phase.id for phase in definition.phases]
    index = ids.index("comment-authoring")
    run_dir = create_run(project, workflow, "Change the app.", workflow_definition=definition)
    runner = Runner(project, run_dir=run_dir)
    plan = resolve_run_subpath(run_dir, Path(runner.phases[ids.index("slice-plan")].artifact))
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(f"## Slice 1\n- Goal: change the app\n- Files: {files}\n", encoding="utf-8")
    meta = read_meta(run_dir)
    meta.update(phase_index=index, current_phase="comment-authoring", phase_entered_at="2026-10-09T00:00:00+00:00")
    write_meta(run_dir, meta)
    monkeypatch.setattr(runner, "_emit_observation", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_check_spec_transition", lambda *args, **kwargs: None)
    phase = runner.phases[index]
    artifact = resolve_run_subpath(run_dir, Path(phase.artifact))
    artifact.write_text("Comments reviewed.\n", encoding="utf-8")
    return project, run_dir, runner, index


@pytest.mark.parametrize("workflow", ["default", "full-feature"])
def test_planned_changes_keep_the_existing_flow(tmp_path, monkeypatch, workflow):
    project, run_dir, runner, index = _comment_authoring_run(tmp_path, monkeypatch, workflow)
    (project / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    phase = runner.phases[index]
    assert not runner._pause_for_approval(phase)
    runner._commit_transition(runner._plan_transition(index, phase))
    assert read_meta(run_dir)["current_phase"] != "comment-authoring"


@pytest.mark.parametrize("workflow", ["default", "full-feature"])
def test_a_change_outside_the_slice_files_needs_user_confirmation(tmp_path, monkeypatch, capsys, workflow):
    """반증: `refactor-scope: none`은 required marker였지만 diff와 대조하는 코드가 없었다."""
    project, run_dir, runner, index = _comment_authoring_run(tmp_path, monkeypatch, workflow)
    (project / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    (project / "src" / "other.py").write_text("y = 2\n", encoding="utf-8")
    phase = runner.phases[index]

    assert runner._pause_for_approval(phase)
    assert "src/other.py" in capsys.readouterr().out
    shown = pending_phase_approval(run_dir)
    assert shown["out_of_plan_files"] == ["src/other.py"]
    with pytest.raises(WorktreeIsolationError, match="approval is missing or stale"):
        runner._commit_transition(runner._plan_transition(index, phase))

    (project / "src" / "new.py").write_text("z = 1\n", encoding="utf-8")
    assert runner._pause_for_approval(phase)
    with pytest.raises(ValueError, match="does not match"):
        approve_phase_artifact(run_dir, token=shown["token"])

    approve_phase_artifact(run_dir, token=pending_phase_approval(run_dir)["token"])
    runner._commit_transition(runner._plan_transition(index, phase))
    meta = read_meta(run_dir)
    assert meta["current_phase"] != "comment-authoring"
    assert meta["scope_confirmed_files"] == ["src/new.py", "src/other.py"]

    meta.update(phase_index=index, current_phase="comment-authoring", phase_entered_at="2026-10-10T00:00:00+00:00")
    write_meta(run_dir, meta)
    assert not runner._pause_for_approval(phase)


def test_a_plan_without_file_lists_cannot_be_compared(tmp_path, monkeypatch):
    project, run_dir, runner, _ = _comment_authoring_run(tmp_path, monkeypatch, "full-feature", files="")
    assert out_of_plan_paths(project, run_dir, ["src/other.py"]) is None


def test_runtime_state_is_not_a_scope_change(tmp_path, monkeypatch):
    project, run_dir, _, _ = _comment_authoring_run(tmp_path, monkeypatch, "full-feature")
    assert out_of_plan_paths(project, run_dir, [".agent-flow/runs/x/meta.json", "src/app.py"]) == ()
