from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_flow.artifact import create_run, find_active_run, read_meta, write_meta
from agent_flow.core.phase_workflow import load_phase_workflow_definition
from agent_flow.core.workflow_pin import WorkflowDefinitionPinError
from agent_flow.runner import Runner


def _workflow(root: Path, *, marker: str, artifact: str) -> Path:
    path = root / "workflows" / "custom.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "id: custom\nphases:\n  - id: explore\n"
        f"    artifact: {artifact}\n"
        f"    required_markers: ['decision: {marker}']\n",
        encoding="utf-8",
    )
    return path


def test_status_keeps_pinned_completion_contract_after_source_changes(tmp_path, monkeypatch, capsys):
    kit = tmp_path / "kit"
    _workflow(kit, marker="reviewed", artifact="original.md")
    definition = load_phase_workflow_definition(kit, "custom")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("agent_flow.artifact.find_kit_root", lambda: kit)
    run = create_run(project, "custom", "Inspect ownership", workflow_definition=definition)
    meta = read_meta(run)
    meta.update(current_phase="explore", phase_index=0)
    write_meta(run, meta)
    (run / "original.md").write_text("## Completion Gate\ndecision: pending\n", encoding="utf-8")
    _workflow(kit, marker="pending", artifact="replacement.md")
    before = (run / "meta.json").read_bytes()

    find_active_run(project).print_status(config_root=project, project_root=project)

    output = capsys.readouterr().out
    payload = json.loads(next(line.removeprefix("status_json: ") for line in output.splitlines() if line.startswith("status_json: ")))
    assert payload["reason"] == "missing_completion_markers"
    assert payload["required_artifact"] == str(run / "original.md")
    assert "decision: reviewed" in payload["missing_completion_markers"]
    assert (run / "meta.json").read_bytes() == before


def test_run_rejects_definition_for_another_workflow_before_publication(tmp_path):
    kit = tmp_path / "kit"
    _workflow(kit, marker="reviewed", artifact="original.md")
    definition = load_phase_workflow_definition(kit, "custom")
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(ValueError):
        create_run(project, "development", "Inspect ownership", workflow_definition=definition)

    assert find_active_run(project) is None


def test_cli_status_reports_invalid_pin_without_rewriting_evidence(tmp_path, capsys):
    from agent_flow.cli import main

    run = create_run(tmp_path, "development", "Inspect ownership")
    meta = read_meta(run)
    meta.update(current_phase="explore", phase_index=0, workflow_definition_digest="0" * 64)
    write_meta(run, meta)
    before = (run / "meta.json").read_bytes()

    assert main(["status", "--root", str(tmp_path)]) == 2

    captured = capsys.readouterr()
    payload = json.loads(next(
        line.removeprefix("status_json: ")
        for line in captured.out.splitlines() if line.startswith("status_json: ")
    ))
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workflow_definition_unavailable"
    assert payload["next_command"] == ""
    assert payload["required_artifact"] is None
    assert "workflow development" in captured.err
    assert "Traceback" not in captured.err
    assert (run / "meta.json").read_bytes() == before


def test_missing_legacy_digest_keeps_diagnostics_without_authorizing_resume(tmp_path, capsys):
    from agent_flow.cli import main

    run = create_run(tmp_path, "development", "Preserve original approval")
    meta = read_meta(run)
    for key in ("workflow_digest", "workflow_definition", "workflow_definition_digest"):
        del meta[key]
    meta.update(
        current_phase="explore",
        phase_index=0,
        phase_approval={"approved": True, "nonce": "original-approval"},
    )
    write_meta(run, meta)
    (run / "review.md").write_text("Original review evidence\n", encoding="utf-8")
    before = {
        path.relative_to(run): path.read_bytes()
        for path in run.rglob("*") if path.is_file()
    }

    with pytest.raises(WorkflowDefinitionPinError):
        Runner(tmp_path, run_dir=run)

    assert {
        path.relative_to(run): path.read_bytes()
        for path in run.rglob("*") if path.is_file()
    } == before
    assert main(["status", "--root", str(tmp_path)]) == 2

    captured = capsys.readouterr()
    payload = json.loads(next(
        line.removeprefix("status_json: ")
        for line in captured.out.splitlines() if line.startswith("status_json: ")
    ))
    assert payload["status"] == "blocked"
    assert payload["run"] == f"development/{run.name}"
    assert payload["task"] == "Preserve original approval"
    assert payload["current_phase"] == "explore"
    assert payload["reason"] == "workflow_definition_unavailable"
    assert payload["required_artifact"] is None
    assert payload["next_command"] == ""
    assert "review.md" in captured.out
    assert "Traceback" not in captured.err
    assert {
        path.relative_to(run): path.read_bytes()
        for path in run.rglob("*") if path.is_file()
    } == before


def test_runner_constructor_keeps_pinned_phases_after_kit_changes(tmp_path, monkeypatch):
    kit = tmp_path / "kit"
    source = _workflow(kit, marker="reviewed", artifact="original.md")
    source.write_text(
        source.read_text(encoding="utf-8") + "  - id: review\n    pause_after: true\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("AGENT_FLOW_PROFILE", "generic")
    monkeypatch.setattr("agent_flow.runner._find_kit_root", lambda: kit)
    original = Runner(project, workflow="custom")
    run = create_run(project, "custom", "Inspect ownership", workflow_definition=original.workflow)
    meta = read_meta(run)
    meta.update(
        current_phase="review",
        phase_index=1,
        phase_approval={"approved": True, "nonce": "original-approval"},
    )
    write_meta(run, meta)
    (run / "review.md").write_text("Original review evidence\n", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in run.iterdir() if path.is_file()}
    source.write_text("id: custom\nphases:\n  - id: replacement\n", encoding="utf-8")

    resumed = Runner(project, run_dir=run)
    cursor = resumed._run_cursor(read_meta(run))

    assert [phase.id for phase in resumed.phases] == ["explore", "review"]
    assert resumed.phases[cursor.phase_index].id == "review"
    assert resumed.phases[cursor.phase_index].pause_after
    assert resumed.phases[0].artifact == "original.md"
    assert resumed.workflow.digest == original.workflow.digest
    assert {path.name: path.read_bytes() for path in run.iterdir() if path.is_file()} == before
    fresh = Runner(project, workflow="custom")
    assert [phase.id for phase in fresh.phases] == ["replacement"]


def test_runner_constructor_rejects_changed_legacy_definition_without_rewriting_evidence(
    tmp_path, monkeypatch
):
    kit = tmp_path / "kit"
    source = _workflow(kit, marker="reviewed", artifact="original.md")
    definition = load_phase_workflow_definition(kit, "custom")
    run = create_run(tmp_path, "custom", "Inspect ownership", workflow_definition=definition)
    meta = read_meta(run)
    del meta["workflow_definition"]
    del meta["workflow_definition_digest"]
    meta.update(phase_approval={"approved": True, "nonce": "legacy-approval"})
    write_meta(run, meta)
    before = (run / "meta.json").read_bytes()
    source.write_text("id: custom\nphases:\n  - id: replacement\n", encoding="utf-8")
    monkeypatch.setattr("agent_flow.runner._find_kit_root", lambda: kit)

    with pytest.raises(WorkflowDefinitionPinError):
        Runner(tmp_path, run_dir=run)

    assert (run / "meta.json").read_bytes() == before


def test_runner_constructor_preserves_corrupt_metadata(tmp_path):
    run = create_run(tmp_path, "development", "Inspect ownership")
    meta_path = run / "meta.json"
    corrupt = b'{"run_id": "r1", "task": "ship it", "gate_nonce": "n1", "phase_index": 0'
    meta_path.write_bytes(corrupt)

    with pytest.raises(WorkflowDefinitionPinError):
        Runner(tmp_path, run_dir=run)

    assert meta_path.read_bytes() == corrupt


def test_retired_workflow_drift_flag_fails_before_approval_or_state_mutation(tmp_path, capsys):
    from agent_flow.cli import main

    run = create_run(tmp_path, "development", "Inspect ownership")
    meta = read_meta(run)
    meta["phase_approval"] = {"approved": True, "nonce": "original-approval"}
    write_meta(run, meta)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*") if path.is_file()
    }

    with pytest.raises(SystemExit) as caught:
        main([
            "continue", "--root", str(tmp_path), "--approve", "original-approval",
            "--accept-workflow-drift",
        ])

    assert caught.value.code == 2
    error = capsys.readouterr().err
    assert "unrecognized arguments: --accept-workflow-drift" in error
    assert {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*") if path.is_file()
    } == before
