from __future__ import annotations

import json
from pathlib import Path
import shlex

import pytest

from agent_flow.artifact import create_run, find_active_run, read_meta, write_meta
from agent_flow.core.phase_workflow import load_phase_workflow_definition
from agent_flow.core.workflow_pin import WorkflowDefinitionPinError
from agent_flow.runner import Runner


def _workflow(root: Path, *, marker: str, artifact: str) -> Path:
    """Write a workflow fixture with a caller-selected marker and artifact."""
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
    """Keep status bound to the pinned completion contract after source changes."""
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
    _workflow(project / ".agent-flow", marker="pending", artifact="configured.md")
    before = (run / "meta.json").read_bytes()

    find_active_run(project).print_status(config_root=project, project_root=project)

    output = capsys.readouterr().out
    payload = json.loads(next(line.removeprefix("status_json: ") for line in output.splitlines() if line.startswith("status_json: ")))
    assert payload["reason"] == "missing_completion_markers"
    assert payload["required_artifact"] == str(run / "original.md")
    assert "decision: reviewed" in payload["missing_completion_markers"]
    assert (run / "meta.json").read_bytes() == before


def test_run_rejects_definition_for_another_workflow_before_publication(tmp_path):
    """Reject a mismatched workflow definition before publishing run state."""
    kit = tmp_path / "kit"
    _workflow(kit, marker="reviewed", artifact="original.md")
    definition = load_phase_workflow_definition(kit, "custom")
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(ValueError):
        create_run(project, "development", "Inspect ownership", workflow_definition=definition)

    assert find_active_run(project) is None


def test_cli_status_reports_invalid_pin_without_rewriting_evidence(tmp_path, capsys):
    """Report an invalid pin without rewriting existing evidence."""
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
    """Retain legacy diagnostics without authorizing resume when the digest is absent."""
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
    """Construct the runner from pinned phases after the installed kit changes."""
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
    """Reject changed legacy definitions without rewriting their evidence."""
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
    """Preserve corrupt metadata when runner construction fails closed."""
    run = create_run(tmp_path, "development", "Inspect ownership")
    meta_path = run / "meta.json"
    corrupt = b'{"run_id": "r1", "task": "ship it", "gate_nonce": "n1", "phase_index": 0'
    meta_path.write_bytes(corrupt)

    with pytest.raises(WorkflowDefinitionPinError):
        Runner(tmp_path, run_dir=run)

    assert meta_path.read_bytes() == corrupt


def test_retired_workflow_drift_flag_fails_before_approval_or_state_mutation(tmp_path, capsys):
    """Reject the retired drift flag before approval or run-state mutation."""
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


@pytest.mark.parametrize("workflow", ["missing", "missing-legacy", None, 17, "", "../development"])
def test_malformed_workflow_identity_blocks_status_and_approval(tmp_path, capsys, workflow):
    """Block status continuation and approval for malformed workflow identities."""
    from agent_flow.artifact import approve_phase_artifact, pending_phase_approval
    from agent_flow.cli import main

    run = create_run(tmp_path, "development", "Preserve unidentified run")
    meta = read_meta(run)
    meta.update(
        current_phase="explore", phase_index=0, phase_entered_at="first",
        phase_approval_request={
            "phase_id": "explore", "phase_entered_at": "first", "artifact": "explore.md",
        },
    )
    write_meta(run, meta)
    (run / "explore.md").write_text("Pending scope\n", encoding="utf-8")
    token = pending_phase_approval(run)["token"]
    if workflow in ("missing", "missing-legacy"):
        del meta["workflow"]
        if workflow == "missing-legacy":
            del meta["workflow_definition"]
            del meta["workflow_definition_digest"]
    else:
        meta["workflow"] = workflow
    write_meta(run, meta)
    before = {
        path.relative_to(run): path.read_bytes()
        for path in run.rglob("*") if path.is_file()
    }

    assert main(["status", "--root", str(tmp_path)]) == 2

    output = capsys.readouterr()
    payload = json.loads(next(
        line.removeprefix("status_json: ")
        for line in output.out.splitlines() if line.startswith("status_json: ")
    ))
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workflow_definition_unavailable"
    assert payload["next_command"] == ""
    assert payload["required_artifact"] is None
    assert "Traceback" not in output.err
    with pytest.raises(WorkflowDefinitionPinError):
        approve_phase_artifact(run, token=token)
    assert main(["continue", "--root", str(tmp_path), "--approve", token]) == 2
    assert "Traceback" not in capsys.readouterr().err
    assert {
        path.relative_to(run): path.read_bytes()
        for path in run.rglob("*") if path.is_file()
    } == before


@pytest.mark.parametrize("surface", ["approval", "continue"])
def test_invalid_pin_cannot_record_pending_approval(tmp_path, capsys, surface):
    """Prevent every approval surface from recording against an invalid pin."""
    from agent_flow.artifact import approve_phase_artifact, pending_phase_approval
    from agent_flow.cli import main

    run = create_run(tmp_path, "development", "Preserve pending approval")
    meta = read_meta(run)
    meta.update(
        current_phase="explore", phase_index=0, phase_entered_at="first",
        phase_approval_request={
            "phase_id": "explore", "phase_entered_at": "first", "artifact": "explore.md",
        },
    )
    write_meta(run, meta)
    (run / "explore.md").write_text("Pending scope\n", encoding="utf-8")
    token = pending_phase_approval(run)["token"]
    meta["workflow_definition_digest"] = "0" * 64
    write_meta(run, meta)
    before = {
        path.relative_to(run): path.read_bytes()
        for path in run.rglob("*") if path.is_file()
    }

    if surface == "approval":
        with pytest.raises(WorkflowDefinitionPinError):
            approve_phase_artifact(run, token=token)
    else:
        assert main(["continue", "--root", str(tmp_path), "--approve", token]) == 2
        capsys.readouterr()

    assert {
        path.relative_to(run): path.read_bytes()
        for path in run.rglob("*") if path.is_file()
    } == before


def _configured_legacy_run(tmp_path, monkeypatch, *, private_state=False, kit_bound=False):
    """Create a configured legacy run with optional private or kit-bound state."""
    config = tmp_path / "config"
    project = tmp_path / "checkout" if private_state else config
    project.mkdir(parents=True)
    source = _workflow(config / ".agent-flow", marker="reviewed", artifact="original.md")
    kit = tmp_path / "kit"
    kit.mkdir()
    definition_root = config / ".agent-flow"
    if kit_bound:
        _workflow(kit, marker="reviewed", artifact="original.md")
        definition_root = kit
    definition = load_phase_workflow_definition(definition_root, "custom")
    monkeypatch.setenv("AGENT_FLOW_PROFILE", "generic")
    monkeypatch.setattr("agent_flow.artifact.find_kit_root", lambda: kit)
    monkeypatch.setattr("agent_flow.runner._find_kit_root", lambda: kit)
    state = config / ".git" / "agent-flow" / "worktrees" / "worker" if private_state else config
    run = create_run(state, "custom", "Preserve custom contract", workflow_definition=definition)
    meta = read_meta(run)
    del meta["workflow_definition"]
    del meta["workflow_definition_digest"]
    meta.update(
        current_phase="explore", phase_index=0, phase_entered_at="2000-01-01T00:00:00+00:00",
        phase_approval={"approved": True, "nonce": "legacy-approval"},
    )
    write_meta(run, meta)
    (run / "original.md").write_text("## Completion Gate\ndecision: pending\n", encoding="utf-8")
    return config, project, state, kit, source, run


def test_configured_legacy_custom_status_command_resumes_exact_definition(
    tmp_path, monkeypatch, capsys
):
    """Resume a configured legacy custom run only from its exact definition."""
    from agent_flow.adapters.hosted import HostedAdapter
    from agent_flow.cli import main

    config, project, state, kit, source, run = _configured_legacy_run(tmp_path, monkeypatch)
    before = {path.name: path.read_bytes() for path in run.iterdir() if path.is_file()}
    original_meta = read_meta(run)

    assert main(["status", "--root", str(config)]) == 0

    payload = json.loads(next(
        line.removeprefix("status_json: ")
        for line in capsys.readouterr().out.splitlines() if line.startswith("status_json: ")
    ))
    assert payload["reason"] == "missing_completion_markers"
    assert payload["required_artifact"] == str(run / "original.md")
    assert "decision: reviewed" in payload["missing_completion_markers"]
    assert {path.name: path.read_bytes() for path in run.iterdir() if path.is_file()} == before
    monkeypatch.setattr("agent_flow.runner.assert_managed_hooks_registered", lambda *a, **k: None)
    monkeypatch.setattr("agent_flow.runner.detect_adapter", lambda: HostedAdapter("codex"))
    monkeypatch.setattr("agent_flow.runner.detect_available_clis", lambda: [])

    assert main(shlex.split(payload["next_command"])[1:]) == 0

    resumed_meta = read_meta(run)
    assert resumed_meta["workflow_digest"] == original_meta["workflow_digest"]
    assert resumed_meta["workflow_definition"]["source_text"] == source.read_text(encoding="utf-8")
    assert resumed_meta["phase_approval"] == original_meta["phase_approval"]
    assert (run / "original.md").read_bytes() == before["original.md"]
    assert resumed_meta["current_phase"] == "explore"


def test_configured_legacy_custom_in_private_state_uses_explicit_context(
    tmp_path, monkeypatch, capsys
):
    """Use explicit configuration context for private-state legacy custom runs."""
    from agent_flow.artifact import approve_phase_artifact, pending_phase_approval

    config, project, state, kit, source, run = _configured_legacy_run(
        tmp_path, monkeypatch, private_state=True,
    )
    meta = read_meta(run)
    meta["phase_approval_request"] = {
        "phase_id": "explore", "phase_entered_at": meta["phase_entered_at"],
        "artifact": "original.md",
    }
    write_meta(run, meta)
    before = (run / "meta.json").read_bytes()

    find_active_run(state).print_status(config_root=config, project_root=project)
    resumed = Runner(project, state_root=state, config_root=config, run_dir=run)

    payload = json.loads(next(
        line.removeprefix("status_json: ")
        for line in capsys.readouterr().out.splitlines() if line.startswith("status_json: ")
    ))
    assert payload["reason"] == "missing_completion_markers"
    assert "decision: reviewed" in payload["missing_completion_markers"]
    assert resumed.workflow.source_bytes == source.read_bytes()
    assert (run / "meta.json").read_bytes() == before

    token = pending_phase_approval(run)["token"]
    approval = approve_phase_artifact(run, token=token, config_root=config)

    assert read_meta(run)["phase_approval"] == approval
    assert pending_phase_approval(run) is None
    assert "workflow_definition" not in read_meta(run)


@pytest.mark.parametrize(("original_location", "replacement"), [
    ("configured", "id: custom\nphases:\n  - id: replacement\n"),
    ("kit", "id: custom\nphases: ["),
    ("packaged", "id: custom\nphases:\n  - id: replacement\n"),
])
def test_changed_configured_legacy_custom_recovers_exact_kit_definition(
    tmp_path, monkeypatch, capsys, original_location, replacement
):
    """Recover the exact kit definition when configured legacy custom source changes."""
    from agent_flow.cli import main

    config, project, state, kit, source, run = _configured_legacy_run(
        tmp_path, monkeypatch, kit_bound=original_location == "kit",
    )
    original = source.read_bytes()
    if original_location == "packaged":
        package = tmp_path / "package"
        _workflow(package, marker="reviewed", artifact="original.md")
        monkeypatch.setattr("agent_flow.core.phase_workflow.package_root", lambda: package)
    elif original_location != "kit":
        _workflow(kit, marker="reviewed", artifact="original.md")
    source.write_text(replacement, encoding="utf-8")
    before = {
        path.relative_to(run): path.read_bytes()
        for path in run.rglob("*") if path.is_file()
    }

    assert main(["status", "--root", str(config)]) == 0
    payload = json.loads(next(
        line.removeprefix("status_json: ")
        for line in capsys.readouterr().out.splitlines() if line.startswith("status_json: ")
    ))
    assert payload["reason"] == "missing_completion_markers"
    assert payload["required_artifact"] == str(run / "original.md")
    assert "decision: reviewed" in payload["missing_completion_markers"]
    resumed = Runner(project, state_root=state, config_root=config, run_dir=run)
    assert resumed.workflow.source_bytes == original
    assert resumed.workflow.digest == read_meta(run)["workflow_digest"]
    assert {
        path.relative_to(run): path.read_bytes()
        for path in run.rglob("*") if path.is_file()
    } == before


@pytest.mark.parametrize(("replacement", "packaged"), [
    ("id: custom\nphases:\n  - id: replacement\n", False),
    ("id: custom\nphases: [", True),
])
def test_changed_configured_legacy_custom_blocks_when_neither_digest_matches(
    tmp_path, monkeypatch, capsys, replacement, packaged
):
    """Block changed legacy custom runs when no candidate digest matches."""
    from agent_flow.artifact import approve_phase_artifact, pending_phase_approval
    from agent_flow.cli import main

    config, project, state, kit, source, run = _configured_legacy_run(tmp_path, monkeypatch)
    if packaged:
        package = tmp_path / "package"
        _workflow(package, marker="changed", artifact="original.md")
        monkeypatch.setattr("agent_flow.core.phase_workflow.package_root", lambda: package)
    else:
        _workflow(kit, marker="changed", artifact="original.md")
    meta = read_meta(run)
    meta["phase_approval_request"] = {
        "phase_id": "explore", "phase_entered_at": meta["phase_entered_at"],
        "artifact": "original.md",
    }
    write_meta(run, meta)
    token = pending_phase_approval(run)["token"]
    source.write_text(replacement, encoding="utf-8")
    before = {
        path.relative_to(run): path.read_bytes()
        for path in run.rglob("*") if path.is_file()
    }

    assert main(["status", "--root", str(config)]) == 2
    payload = json.loads(next(
        line.removeprefix("status_json: ")
        for line in capsys.readouterr().out.splitlines() if line.startswith("status_json: ")
    ))
    assert payload["reason"] == "workflow_definition_unavailable"
    assert payload["next_command"] == ""
    assert payload["required_artifact"] is None
    with pytest.raises(WorkflowDefinitionPinError):
        Runner(project, state_root=state, config_root=config, run_dir=run)
    with pytest.raises(WorkflowDefinitionPinError):
        approve_phase_artifact(run, token=token, config_root=config)
    assert main(["continue", "--root", str(config), "--approve", token]) == 2
    assert {
        path.relative_to(run): path.read_bytes()
        for path in run.rglob("*") if path.is_file()
    } == before
