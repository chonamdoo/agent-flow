import json
import shlex
import subprocess
from pathlib import Path

import pytest

from agent_flow.adapters.hosted import HostedAdapter
from agent_flow.artifact import (
    ActiveRun, approve_phase_artifact, create_run, pending_phase_approval, read_meta, write_meta,
)
from agent_flow.cli import main
from agent_flow.core.phase_workflow import find_kit_root, load_phase_workflow_definition
from agent_flow.core.worktree_isolation import WorktreeIsolationError, resolve_run_subpath
from agent_flow.runner import Runner


def _commit(project: Path, message: str) -> str:
    if not (project / ".git").exists():
        subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "commit", "-q", "--allow-empty", "-m", message], check=True)
    return subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


def _status_payload(output: str) -> dict:
    return json.loads(next(line.removeprefix("status_json: ") for line in output.splitlines()
                           if line.startswith("status_json: ")))


@pytest.mark.parametrize("surface", ["continue", "status"])
def test_next_command_can_approve_only_the_displayed_artifact(tmp_path, monkeypatch, capsys, surface):
    project = tmp_path / "project"
    workflow = project / ".agent-flow/workflows/approval-example.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "id: approval-example\nphases:\n  - id: proposal\n    pause_after: true\n",
        encoding="utf-8",
    )
    definition = load_phase_workflow_definition(project / ".agent-flow", "approval-example")
    run_dir = create_run(
        project, "approval-example", "Review the operation scope.", workflow_definition=definition,
    )
    artifact = run_dir / "proposal.md"
    artifact.write_text("Approved operation and target scope.\n", encoding="utf-8")
    meta = read_meta(run_dir)
    meta.update(
        phase_index=0, current_phase="proposal", phase_entered_at="2026-01-01T00:00:00+00:00",
    )
    write_meta(run_dir, meta)
    runner = Runner(project, run_dir=run_dir)
    phase = runner.phases[0]
    runner.next_command = f"agent-flow continue --root {shlex.quote(str(project))}"
    monkeypatch.setattr(runner, "_emit_observation", lambda *args, **kwargs: None)
    assert runner._pause_for_approval(phase)
    status = ActiveRun(run_dir, run_dir.name, "approval-example", "Review the operation scope.", "")
    if surface == "status":
        capsys.readouterr()
        status.print_status(next_command=runner.next_command, config_root=project, project_root=project)
    output = capsys.readouterr().out
    payload = _status_payload(output)
    assert payload["reason"] == "phase_approval_required"
    assert payload["next_command"] == runner.next_command
    approval = shlex.split(payload["approval_command"])
    assert approval[:-2] == shlex.split(runner.next_command)
    assert approval[-2] == "--approve"
    token = approval[-1]
    assert f"approval_command: {payload['approval_command']}" in output.splitlines()
    assert [line.split(":", 1)[0] for line in output.splitlines() if token in line] == [
        "approval_command", "status_json",
    ]
    assert token not in json.dumps({key: value for key, value in payload.items() if key != "approval_command"})

    monkeypatch.setattr("agent_flow.runner.assert_managed_hooks_registered", lambda *a, **k: None)
    monkeypatch.setattr("agent_flow.runner.detect_adapter", lambda: HostedAdapter("codex"))
    monkeypatch.setattr("agent_flow.runner.detect_available_clis", lambda: [])
    assert main(shlex.split(payload["next_command"])[1:]) == 0
    resumed = _status_payload(capsys.readouterr().out)
    assert resumed["reason"] == "phase_approval_required"
    assert resumed["current_phase"] == "proposal"
    assert shlex.split(resumed["approval_command"])[-1] == token
    assert read_meta(run_dir).get("phase_approval") is None

    approve_phase_artifact(run_dir, token=token)
    assert not runner._pause_for_approval(phase)
    capsys.readouterr()
    status.print_status(next_command=runner.next_command, config_root=project, project_root=project)
    approved = _status_payload(capsys.readouterr().out)
    assert approved["reason"] == "phase_artifact_written_continue_required"
    assert "approval_command" not in approved

    artifact.write_text("Different target scope, not yet approved.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        approve_phase_artifact(run_dir, token=token)


@pytest.mark.parametrize("workflow", ["default", "full-feature"])
def test_merge_approval_pauses_before_merge_until_the_displayed_artifact_is_approved(
    tmp_path, monkeypatch, workflow,
):
    """반증: default는 green PR을 곧장 merge로 보냈고, full-feature는 에이전트가 쓴
    `verdict: approve`만으로 merge에 들어갔다. merge 직전 승인은 runner pause가 막아야 한다."""
    project = tmp_path / "project"
    project.mkdir()
    _commit(project, "reviewed")
    definition = load_phase_workflow_definition(find_kit_root(), workflow)
    ids = [phase.id for phase in definition.phases]
    assert "merge-approval" in ids
    index = ids.index("merge-approval")
    assert ids[index + 1] == "merge"
    run_dir = create_run(project, workflow, "Merge the reviewed change.", workflow_definition=definition)
    meta = read_meta(run_dir)
    meta.update(
        phase_index=index, current_phase="merge-approval",
        phase_entered_at="2026-10-09T00:00:00+00:00",
    )
    write_meta(run_dir, meta)
    runner = Runner(project, run_dir=run_dir)
    phase = runner.phases[index]
    monkeypatch.setattr(runner, "_emit_observation", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_check_spec_transition", lambda *args, **kwargs: None)
    artifact = resolve_run_subpath(run_dir, Path(phase.artifact))
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("Merge PR #1 at the published HEAD.\n\nverdict: approve\n", encoding="utf-8")

    assert runner._pause_for_approval(phase)
    displayed = pending_phase_approval(run_dir)["token"]
    with pytest.raises(WorktreeIsolationError, match="approval is missing or stale"):
        runner._commit_transition(runner._plan_transition(index, phase))
    assert read_meta(run_dir)["current_phase"] == "merge-approval"

    artifact.write_text("Merge a different HEAD.\n\nverdict: approve\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        approve_phase_artifact(run_dir, token=displayed)
    with pytest.raises(WorktreeIsolationError, match="approval is missing or stale"):
        runner._commit_transition(runner._plan_transition(index, phase))

    assert runner._pause_for_approval(phase)
    approve_phase_artifact(run_dir, token=pending_phase_approval(run_dir)["token"])
    runner._commit_transition(runner._plan_transition(index, phase))
    assert read_meta(run_dir)["current_phase"] == "merge"


def _merge_approval_run(tmp_path, monkeypatch, workflow):
    project = tmp_path / "project"
    project.mkdir()
    approved_head = _commit(project, "reviewed")
    definition = load_phase_workflow_definition(find_kit_root(), workflow)
    index = [phase.id for phase in definition.phases].index("merge-approval")
    run_dir = create_run(project, workflow, "Merge the reviewed change.", workflow_definition=definition)
    meta = read_meta(run_dir)
    meta.update(phase_index=index, current_phase="merge-approval", phase_entered_at="2026-10-09T00:00:00+00:00")
    write_meta(run_dir, meta)
    runner = Runner(project, run_dir=run_dir)
    monkeypatch.setattr(runner, "_emit_observation", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_check_spec_transition", lambda *args, **kwargs: None)
    phase = runner.phases[index]
    artifact = resolve_run_subpath(run_dir, Path(phase.artifact))
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("Merge PR #1 at the published HEAD.\n\nverdict: approve\n", encoding="utf-8")
    return project, run_dir, runner, index, approved_head


@pytest.mark.parametrize("workflow", ["default", "full-feature"])
def test_merge_approval_token_is_bound_to_the_head_it_was_shown_for(tmp_path, monkeypatch, workflow):
    """반증: 승인 identity가 artifact 바이트뿐이면 승인 대기 중 생긴 commit도 같은 토큰으로 merge된다(#426)."""
    project, run_dir, runner, index, approved_head = _merge_approval_run(tmp_path, monkeypatch, workflow)
    phase = runner.phases[index]
    assert runner._pause_for_approval(phase)
    shown = pending_phase_approval(run_dir)
    assert shown["head_oid"] == approved_head

    _commit(project, "conflict resolution")
    assert runner._pause_for_approval(phase)
    with pytest.raises(ValueError, match="does not match"):
        approve_phase_artifact(run_dir, token=shown["token"])
    assert pending_phase_approval(run_dir)["token"] != shown["token"]


@pytest.mark.parametrize("workflow", ["default", "full-feature"])
def test_head_change_after_approval_cannot_leave_merge_without_reapproval(tmp_path, monkeypatch, workflow):
    """반증: merge phase가 승인 뒤 충돌 해결 commit을 만들어도 merge.md만 쓰면 handoff로 넘어갔다(#426)."""
    project, run_dir, runner, index, _ = _merge_approval_run(tmp_path, monkeypatch, workflow)
    phase = runner.phases[index]
    assert runner._pause_for_approval(phase)
    approve_phase_artifact(run_dir, token=pending_phase_approval(run_dir)["token"])
    runner._commit_transition(runner._plan_transition(index, phase))
    assert read_meta(run_dir)["current_phase"] == "merge"

    merge = runner.phases[index + 1]
    resolve_run_subpath(run_dir, Path(merge.artifact)).write_text("Merged PR #1.\n", encoding="utf-8")
    new_head = _commit(project, "conflict resolution")
    transition = runner._plan_transition(index + 1, merge)
    assert (transition.to_phase, transition.route_key, transition.blocked) == ("merge-approval", "head-changed", False)
    runner._commit_transition(transition)
    meta = read_meta(run_dir)
    assert meta["current_phase"] == "merge-approval"
    assert not resolve_run_subpath(run_dir, Path(phase.artifact)).exists()

    resolve_run_subpath(run_dir, Path(phase.artifact)).write_text("Merge the new HEAD.\n\nverdict: approve\n",
                                                                  encoding="utf-8")
    assert runner._pause_for_approval(phase)
    assert pending_phase_approval(run_dir)["head_oid"] == new_head


@pytest.mark.parametrize("workflow", ["default", "full-feature"])
def test_unchanged_head_keeps_the_existing_merge_flow(tmp_path, monkeypatch, workflow):
    project, run_dir, runner, index, approved_head = _merge_approval_run(tmp_path, monkeypatch, workflow)
    phase = runner.phases[index]
    assert runner._pause_for_approval(phase)
    approve_phase_artifact(run_dir, token=pending_phase_approval(run_dir)["token"])
    runner._commit_transition(runner._plan_transition(index, phase))
    assert read_meta(run_dir)["merge_approval_head"] == approved_head

    merge = runner.phases[index + 1]
    resolve_run_subpath(run_dir, Path(merge.artifact)).write_text("Merged PR #1.\n", encoding="utf-8")
    transition = runner._plan_transition(index + 1, merge)
    assert transition.to_index == index + 2 and not transition.blocked


@pytest.mark.parametrize("workflow", ["default", "full-feature"])
def test_conflict_commit_returns_to_merge_approval_before_the_merge_worker_runs(tmp_path, monkeypatch, workflow):
    """반증: merge 완료 때만 대조하면 worker가 승인 안 된 HEAD를 이미 merge한 뒤다(#426)."""
    from agent_flow import runner as runner_module
    from agent_flow.adapters.generic import GenericAdapter
    from agent_flow.runner import ResumeMode

    project, run_dir, runner, index, _ = _merge_approval_run(tmp_path, monkeypatch, workflow)
    phase = runner.phases[index]
    assert runner._pause_for_approval(phase)
    approve_phase_artifact(run_dir, token=pending_phase_approval(run_dir)["token"])
    runner._commit_transition(runner._plan_transition(index, phase))
    new_head = _commit(project, "conflict resolution")

    calls = []

    def execute(target, **kwargs):
        calls.append(target.id)
        if target.id == "merge":
            pytest.fail("an unapproved HEAD reached the merge worker")
        artifact = resolve_run_subpath(run_dir, Path(target.artifact))
        artifact.write_text("Merge the new HEAD.\n\nverdict: approve\n", encoding="utf-8")
        return True

    adapter = GenericAdapter()
    monkeypatch.setattr(adapter, "execute", execute)
    monkeypatch.setattr(runner_module, "detect_adapter", lambda: adapter)
    monkeypatch.setattr(runner_module, "detect_available_clis", lambda: [])
    monkeypatch.setattr(runner, "_missing_entry_spec_evidence", lambda *args, **kwargs: [])
    monkeypatch.setattr(runner_module, "assert_managed_hooks_registered", lambda *a, **k: None)
    runner.run(ResumeMode.RESUME)
    assert calls == ["merge-approval"]
    assert read_meta(run_dir)["current_phase"] == "merge-approval"
    assert runner._pause_for_approval(phase)
    assert pending_phase_approval(run_dir)["head_oid"] == new_head


def test_run_approved_before_head_binding_is_not_rerouted(tmp_path, monkeypatch):
    """승인 head가 없는 기존 run을 되돌리면 이미 merge한 run이 cleanup에 닿지 못한다."""
    project, run_dir, runner, index, _ = _merge_approval_run(tmp_path, monkeypatch, "full-feature")
    meta = read_meta(run_dir)
    meta.update(phase_index=index + 1, current_phase="merge")
    write_meta(run_dir, meta)
    merge = runner.phases[index + 1]
    resolve_run_subpath(run_dir, Path(merge.artifact)).write_text("Merged PR #1.\n", encoding="utf-8")
    _commit(project, "later")
    assert runner._plan_transition(index + 1, merge).to_index == index + 2
