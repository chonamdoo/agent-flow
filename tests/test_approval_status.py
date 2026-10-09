import json
import shlex
from pathlib import Path

import pytest

from agent_flow.adapters.hosted import HostedAdapter
from agent_flow.artifact import ActiveRun, approve_phase_artifact, create_run, read_meta, write_meta
from agent_flow.cli import main
from agent_flow.core.phase_workflow import load_phase_workflow_definition
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
