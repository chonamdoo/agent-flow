import json
import shlex
from pathlib import Path

import pytest

from agent_flow.artifact import ActiveRun, approve_phase_artifact, write_meta
from agent_flow.runner import Phase, Runner


@pytest.mark.parametrize("surface", ["continue", "status"])
def test_next_command_can_approve_only_the_displayed_artifact(tmp_path, monkeypatch, capsys, surface):
    project = tmp_path / "project"
    workflow = project / ".agent-flow/workflows/approval-example.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "id: approval-example\nphases:\n  - id: proposal\n    pause_after: true\n",
        encoding="utf-8",
    )
    run_dir = project / ".agent-flow/runs/run-1"
    run_dir.mkdir(parents=True)
    artifact = run_dir / "proposal.md"
    artifact.write_text("Approved operation and target scope.\n", encoding="utf-8")
    write_meta(run_dir, {
        "run_id": "run-1", "workflow": "approval-example", "task": "Review the operation scope.",
        "phase_index": 0, "current_phase": "proposal", "phase_entered_at": "2026-01-01T00:00:00+00:00",
    })
    phase = Phase(id="proposal", description="", artifact="proposal.md", pause_after=True)
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.workflow_name = "approval-example"
    runner.next_command = f"agent-flow continue --root {shlex.quote(str(project))}"
    monkeypatch.setattr(runner, "_emit_observation", lambda *args, **kwargs: None)
    assert runner._pause_for_approval(phase)
    if surface == "status":
        capsys.readouterr()
        ActiveRun(run_dir, "run-1", "approval-example", "Review the operation scope.", "").print_status(
            next_command=runner.next_command, config_root=project, project_root=project,
        )
    output = capsys.readouterr().out
    payload = json.loads(next(line.removeprefix("status_json: ") for line in output.splitlines()
                              if line.startswith("status_json: ")))
    command = shlex.split(payload["next_command"])
    assert payload["reason"] == "phase_approval_required"
    assert "--approve" in command
    token = command[command.index("--approve") + 1]
    approve_phase_artifact(run_dir, token=token)
    assert not runner._pause_for_approval(phase)

    artifact.write_text("Different target scope, not yet approved.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        approve_phase_artifact(run_dir, token=token)
