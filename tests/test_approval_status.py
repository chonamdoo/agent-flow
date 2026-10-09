import json
import shlex
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
