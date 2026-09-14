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


def _skills_inspection_run(tmp_path, monkeypatch):
    kit = tmp_path / "kit"
    source = kit / "workflows" / "custom.yaml"
    source.parent.mkdir(parents=True)
    source.write_text(
        "id: custom\nphases:\n"
        "  - id: explore\n"
        "  - id: green\n"
        "    skills:\n      required: [pinned-guide]\n"
        "    required_markers: ['decision: reviewed']\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    for name in ("pinned-guide", "fresh-guide"):
        document = project / "skills" / name / "SKILL.md"
        document.parent.mkdir(parents=True)
        document.write_text(
            f"---\nname: {name}\ndescription: Inspection guide.\n---\n# {name}\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLOW_PROFILE", "generic")
    monkeypatch.setattr("agent_flow.cli._find_kit_root", lambda: kit)
    definition = load_phase_workflow_definition(kit, "custom")
    run = create_run(project, "custom", "Bound task", workflow_definition=definition)
    meta = read_meta(run)
    meta.update(
        current_phase="explore", phase_index=0,
        phase_approval={"approved": True, "nonce": "original-approval"},
    )
    write_meta(run, meta)
    artifact = project / "inspection.md"
    artifact.write_text(
        "## Completion Gate\n"
        "decision: reviewed\n"
        "skill-availability: pass\n"
        "skill-use-evidence: verified\n"
        "project-local-skills: checked\n"
        "project-local-skills-used: pinned-guide\n"
        "project-local-skill-docs: applied\n",
        encoding="utf-8",
    )
    return project, kit, source, run, artifact


@pytest.mark.parametrize("explicit_workflow", [False, True])
@pytest.mark.parametrize("remove_source", [False, True])
def test_cli_skills_inspection_keeps_active_pin_across_all_surfaces(
    tmp_path, monkeypatch, capsys, explicit_workflow, remove_source,
):
    from agent_flow.cli import main

    project, kit, source, run, artifact = _skills_inspection_run(tmp_path, monkeypatch)
    if remove_source:
        source.unlink()
    else:
        source.write_text(
            source.read_text(encoding="utf-8").replace("pinned-guide", "fresh-guide"),
            encoding="utf-8",
        )
    before = {path.name: path.read_bytes() for path in run.iterdir() if path.is_file()}
    options = ["--workflow", "custom"] if explicit_workflow else []
    for command in ("resolve", "prompt", "markers"):
        args = ["skills", command, "--root", str(project), "--phase", "green", *options]
        if command == "markers":
            args.extend(["--artifact", str(artifact)])
        assert main(args) == 0
        output = capsys.readouterr().out
        if command == "markers":
            assert json.loads(output) == []
        else:
            assert "pinned-guide" in output
            assert "fresh-guide" not in output
    assert {path.name: path.read_bytes() for path in run.iterdir() if path.is_file()} == before


@pytest.mark.parametrize("command", ["resolve", "prompt", "markers"])
@pytest.mark.parametrize("broken_pin", ["digest", "payload", "legacy-missing", "legacy-drift"])
def test_cli_skills_inspection_rejects_invalid_pin_without_mutation(
    tmp_path, monkeypatch, capsys, command, broken_pin,
):
    from agent_flow.cli import main

    project, kit, source, run, artifact = _skills_inspection_run(tmp_path, monkeypatch)
    meta = read_meta(run)
    if broken_pin == "digest":
        meta["workflow_definition_digest"] = "0" * 64
    elif broken_pin == "payload":
        meta["workflow_definition"] = None
    else:
        del meta["workflow_definition"]
        del meta["workflow_definition_digest"]
        if broken_pin == "legacy-missing":
            del meta["workflow_digest"]
        else:
            source.write_text(
                source.read_text(encoding="utf-8").replace("pinned-guide", "fresh-guide"),
                encoding="utf-8",
            )
    write_meta(run, meta)
    before = {path.name: path.read_bytes() for path in run.iterdir() if path.is_file()}
    args = ["skills", command, "--root", str(project), "--phase", "green"]
    if command == "markers":
        args.extend(["--artifact", str(artifact)])
    assert main(args) == 2
    captured = capsys.readouterr()
    assert "workflow custom" in captured.err
    assert "Traceback" not in captured.err
    assert {path.name: path.read_bytes() for path in run.iterdir() if path.is_file()} == before


def test_cli_skills_fresh_uses_current_kit_and_rejects_ambiguous_workflow(
    tmp_path, monkeypatch, capsys,
):
    from agent_flow.cli import main

    project, kit, source, run, artifact = _skills_inspection_run(tmp_path, monkeypatch)
    source.write_text(
        source.read_text(encoding="utf-8").replace("pinned-guide", "fresh-guide"),
        encoding="utf-8",
    )
    (kit / "workflows" / "default.yaml").write_text(
        "id: default\nphases:\n  - id: green\n"
        "    skills:\n      required: [default-guide]\n",
        encoding="utf-8",
    )
    base = ["skills", "resolve", "--root", str(project), "--phase", "green"]
    assert main([*base, "--workflow", "default"]) == 2
    assert "--fresh" in capsys.readouterr().err
    assert main([*base, "--fresh", "--workflow", "custom"]) == 0
    assert "required fresh-guide:" in capsys.readouterr().out
    assert main([*base, "--fresh"]) == 0
    assert "required default-guide:" in capsys.readouterr().out
    assert main(["skills", "resolve", "--root", str(project), "--phase", "missing"]) == 2
    assert "phase 'missing'" in capsys.readouterr().err
    (run / "active").unlink()
    assert main(base) == 0
    assert "required default-guide:" in capsys.readouterr().out


def test_cli_skills_inspection_recovers_exact_legacy_pin_without_upgrading_it(
    tmp_path, monkeypatch, capsys,
):
    from agent_flow.cli import main

    project, kit, source, run, artifact = _skills_inspection_run(tmp_path, monkeypatch)
    meta = read_meta(run)
    del meta["workflow_definition"]
    del meta["workflow_definition_digest"]
    write_meta(run, meta)
    before = (run / "meta.json").read_bytes()
    for command in ("resolve", "prompt", "markers"):
        args = ["skills", command, "--root", str(project), "--phase", "green"]
        if command == "markers":
            args.extend(["--artifact", str(artifact)])
        assert main(args) == 0
        output = capsys.readouterr().out
        if command == "markers":
            assert json.loads(output) == []
        else:
            assert "pinned-guide" in output
    assert (run / "meta.json").read_bytes() == before


def test_cli_skills_context_keeps_bound_concerns_but_fresh_does_not(
    tmp_path, monkeypatch, capsys,
):
    from agent_flow.cli import main

    project, kit, source, run, artifact = _skills_inspection_run(tmp_path, monkeypatch)
    profile = project / ".agent-flow" / "profiles" / "inspection.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        "id: inspection\nskills:\n  required_review:\n"
        "    - group: security\n"
        "      concerns: [security]\n"
        "      skills: [concern-guide]\n",
        encoding="utf-8",
    )
    task_guide = project / "skills" / "task-guide" / "SKILL.md"
    task_guide.parent.mkdir(parents=True)
    task_guide.write_text(
        "---\nname: task-guide\ndescription: Task-scoped guide.\n"
        "workflowPhases: [green]\ntaskTerms: [bound task]\n---\nTask instructions.\n",
        encoding="utf-8",
    )
    meta = read_meta(run)
    meta["concerns"] = ["security"]
    write_meta(run, meta)
    artifact.write_text(
        artifact.read_text(encoding="utf-8")
        + "missing-required-profile-skills: none\n",
        encoding="utf-8",
    )
    options = ["--root", str(project), "--phase", "green", "--profile", "inspection"]
    for command in ("resolve", "prompt", "markers"):
        args = ["skills", command, *options]
        if command == "markers":
            args.extend(["--artifact", str(artifact)])
        assert main(args) == 0
        output = capsys.readouterr().out
        if command == "markers":
            assert any("concern-guide" in marker for marker in json.loads(output))
        else:
            assert "concern-guide" in output
            assert "task-guide" in output
        assert main([*args, "--fresh", "--workflow", "custom"]) == 0
        fresh_output = capsys.readouterr().out
        assert "concern-guide" not in fresh_output
        assert "task-guide" not in fresh_output
    assert main(["skills", "resolve", *options, "--task", "unrelated"]) == 0
    overridden = capsys.readouterr().out
    assert "concern-guide" in overridden
    assert "task-guide" not in overridden
    assert main([
        "skills", "resolve", *options, "--fresh", "--workflow", "custom",
        "--task", "Bound task",
    ]) == 0
    explicit = capsys.readouterr().out
    assert "task-guide" in explicit
    assert "concern-guide" not in explicit


def test_cli_skills_inspection_uses_current_checkout_not_newest_sibling_run(
    tmp_path, monkeypatch, capsys,
):
    from agent_flow.cli import main
    from agent_flow.core.worktrees import create_worktree, plan_worktree, worktree_runtime_root
    from tests.test_cli import _init_git_repo

    project, kit, source, leader_run, artifact = _skills_inspection_run(tmp_path, monkeypatch)
    _init_git_repo(project)
    (leader_run / "active").unlink()
    own = create_worktree(root=project, plan=plan_worktree(root=project, name="own"))
    foreign = create_worktree(root=project, plan=plan_worktree(root=project, name="foreign"))
    own_definition = load_phase_workflow_definition(kit, "custom")
    own_state = worktree_runtime_root(root=project, name=own.name)
    own_run = create_run(
        own_state, "custom", "Own task", run_id="20000101-000000",
        workflow_definition=own_definition,
    )
    source.write_text(
        source.read_text(encoding="utf-8").replace("pinned-guide", "fresh-guide"),
        encoding="utf-8",
    )
    foreign_definition = load_phase_workflow_definition(kit, "custom")
    foreign_run = create_run(
        worktree_runtime_root(root=project, name=foreign.name), "custom", "Foreign task",
        run_id="20990101-000000", concerns=["security"], workflow_definition=foreign_definition,
    )
    for run in (own_run, foreign_run):
        meta = read_meta(run)
        meta.update(current_phase="explore", phase_index=0)
        write_meta(run, meta)
    scoped = project / "skills" / "checkout-guide" / "SKILL.md"
    scoped.parent.mkdir(parents=True)
    scoped.write_text(
        "---\nname: checkout-guide\ndescription: Checkout-scoped guide.\n"
        "workflowPhases: [green]\npathGlobs: [checkout-only.txt]\n---\nCheckout instructions.\n",
        encoding="utf-8",
    )
    (own.path / "checkout-only.txt").write_text("changed\n", encoding="utf-8")
    monkeypatch.chdir(own.path)
    before = {run: (run / "meta.json").read_bytes() for run in (own_run, foreign_run)}
    for root in (own.path, project):
        assert main(["skills", "resolve", "--root", str(root), "--phase", "green"]) == 0
        output = capsys.readouterr().out
        assert "required pinned-guide:" in output
        assert "required checkout-guide:" in output
        assert "fresh-guide" not in output
    assert {run: (run / "meta.json").read_bytes() for run in before} == before
    (own_run / "active").unlink()
    source.write_text(
        source.read_text(encoding="utf-8").replace("fresh-guide", "unbound-guide"),
        encoding="utf-8",
    )
    assert main([
        "skills", "resolve", "--root", str(own.path), "--phase", "green",
        "--workflow", "custom",
    ]) == 0
    output = capsys.readouterr().out
    assert "required unbound-guide:" in output
    assert "fresh-guide" not in output


def test_cli_skills_markers_since_override_and_fresh_do_not_inherit_run_time(
    tmp_path, monkeypatch, capsys,
):
    from agent_flow.cli import main
    from agent_flow.core.local_skills import record_skill_read

    project, kit, source, run, artifact = _skills_inspection_run(tmp_path, monkeypatch)
    meta = read_meta(run)
    meta["phase_entered_at"] = "2999-01-01T00:00:00+00:00"
    write_meta(run, meta)
    record_skill_read(project, project / "skills" / "pinned-guide" / "SKILL.md")
    artifact.write_text(
        artifact.read_text(encoding="utf-8").replace("skill-use-evidence: verified\n", ""),
        encoding="utf-8",
    )
    args = [
        "skills", "markers", "--root", str(project), "--phase", "green",
        "--artifact", str(artifact),
    ]
    assert main(args) == 0
    bound = json.loads(capsys.readouterr().out)
    assert any("nothing was recorded" in marker for marker in bound)
    for overrides in (["--since", "0"], ["--fresh", "--workflow", "custom"]):
        assert main([*args, *overrides]) == 0
        unbounded = json.loads(capsys.readouterr().out)
        assert "skill-use-evidence: verified|unavailable" in unbounded
        assert not any("nothing was recorded" in marker for marker in unbounded)


@pytest.mark.parametrize("mode", [None, "pending", "clean", "malformed"])
def test_cli_status_architecture_guidance_does_not_change_structured_status(
    tmp_path, monkeypatch, capsys, mode,
):
    from agent_flow.cli import main

    project, kit, source, run, artifact = _skills_inspection_run(tmp_path, monkeypatch)
    monkeypatch.setattr("agent_flow.artifact.find_kit_root", lambda: kit)
    if mode is not None:
        selection = project / ".agent-flow.project.yaml"
        selection.write_text(
            "schema_version: 1\narchitecture:\n  mode: " + mode + "\n",
            encoding="utf-8",
        )
    before = (run / "meta.json").read_bytes()
    find_active_run(project).print_status(
        config_root=project, project_root=project,
        next_command=f"agent-flow continue --root {project}",
    )
    expected = next(
        line for line in capsys.readouterr().out.splitlines()
        if line.startswith("status_json: ")
    )
    assert main(["status", "--root", str(project)]) == 0
    output = capsys.readouterr().out
    actual = next(line for line in output.splitlines() if line.startswith("status_json: "))
    assert actual == expected
    assert (run / "meta.json").read_bytes() == before


def test_cli_status_without_run_reports_selection_absence_without_creating_state(
    tmp_path, capsys,
):
    from agent_flow.cli import main

    assert main(["status", "--root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "status_json:" not in output
    assert not (tmp_path / ".agent-flow").exists()


@pytest.mark.parametrize("mode", ["clean", "pending"])
def test_cli_skills_conditional_markers_use_validated_run_selection(
    tmp_path, monkeypatch, capsys, mode,
):
    from agent_flow.cli import main
    from agent_flow.core.architecture_policy import architecture_snapshot
    from agent_flow.core.workflow_pin import workflow_pin_metadata
    from tests.test_cli import _init_git_repo

    project, kit, source, run, artifact = _skills_inspection_run(tmp_path, monkeypatch)
    source.write_text(
        source.read_text(encoding="utf-8")
        + "    required_markers_by_architecture:\n"
        + "      clean: ['clean-check: pass']\n"
        + "      pending: ['scope-check: existing']\n",
        encoding="utf-8",
    )
    selection = project / ".agent-flow.project.yaml"
    selection.write_text(
        f"schema_version: 1\narchitecture:\n  mode: {mode}\n", encoding="utf-8",
    )
    _init_git_repo(project)
    definition = load_phase_workflow_definition(kit, "custom")
    meta = read_meta(run)
    meta.update(
        workflow_pin_metadata(definition, workflow="custom"),
        architecture_digest=architecture_snapshot(project).digest,
    )
    write_meta(run, meta)
    source.write_text("id: custom\nphases:\n  - id: replaced\n", encoding="utf-8")
    args = [
        "skills", "markers", "--root", str(project), "--phase", "green",
        "--artifact", str(artifact),
    ]
    expected = "clean-check: pass" if mode == "clean" else "scope-check: existing"
    assert main(["skills", "prompt", "--root", str(project), "--phase", "green"]) == 0
    prompt = capsys.readouterr().out
    assert expected in prompt
    assert ("scope-check: existing" if mode == "clean" else "clean-check: pass") not in prompt
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out) == [expected]
    artifact.write_text(artifact.read_text(encoding="utf-8") + expected + "\n", encoding="utf-8")
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out) == []
    artifact.write_text(
        artifact.read_text(encoding="utf-8").replace("decision: reviewed", "decision: pending"),
        encoding="utf-8",
    )
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out) == ["decision: reviewed"]
    selection.write_text(
        "schema_version: 1\narchitecture:\n  mode: "
        + ("pending" if mode == "clean" else "clean") + "\n",
        encoding="utf-8",
    )
    before = (run / "meta.json").read_bytes()
    for command in ("resolve", "prompt", "markers"):
        check = ["skills", command, "--root", str(project), "--phase", "green"]
        if command == "markers":
            check.extend(["--artifact", str(artifact)])
        assert main(check) == 2
        assert "architecture_policy_drift" in capsys.readouterr().err
    assert (run / "meta.json").read_bytes() == before
    assert main([
        "skills", "resolve", "--root", str(project), "--phase", "replaced",
        "--workflow", "custom", "--fresh",
    ]) == 0
