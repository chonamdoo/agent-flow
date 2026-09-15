from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from agent_flow.core.phase_workflow import (
    CorruptRunCursorError,
    PhaseWorkflowDefinition,
    load_phase_workflow_definition,
)
from agent_flow.core.workflow_pin import (
    WorkflowDefinitionPinError,
    load_run_workflow_definition,
    workflow_pin_metadata,
)


SOURCE = (
    "id: custom\ncompletion_disposition: local-handoff\nphases:\n"
    "  - id: implement\n    prompt: Preserve the original implementation obligation.\n"
    "    skills:\n      required: [clean-architecture-core]\n"
    "    routes:\n      default: review\n"
    "  - id: review\n    multi_review: true\n    pause_after: true\n"
    "    required_markers: [verdict]\n    artifact: original-review.md\n"
    "    routes:\n      request-changes: implement\n      default: done\n"
    "  - id: done\n"
)


def _source(tmp_path: Path, content: str = SOURCE) -> Path:
    """Write a workflow source fixture and return its path."""
    path = tmp_path / "workflows" / "custom.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    return path


def _meta(definition: PhaseWorkflowDefinition) -> dict[str, Any]:
    """Build valid run metadata bound to a workflow definition."""
    return {
        "run_id": "existing-run",
        "workflow": "custom",
        "phase_index": 1,
        "current_phase": "review",
        "phase_approval": {"approved": True, "nonce": "old-approval"},
        "review_nonce": "old-review",
        **workflow_pin_metadata(definition, workflow="custom"),
    }


def _evidence(tmp_path: Path, meta: dict[str, Any]) -> tuple[Path, dict[str, bytes]]:
    """Create approval evidence whose bytes must remain unchanged."""
    run = tmp_path / "run"
    run.mkdir()
    contents = {
        "meta.json": json.dumps(meta).encode("utf-8"),
        "original-review.md": b"Original review evidence\n",
        "transitions.jsonl": b'{"from_phase":"implement","to_phase":"review"}\n',
    }
    for name, content in contents.items():
        (run / name).write_bytes(content)
    return run, contents


def _assert_evidence(run: Path, contents: dict[str, bytes]) -> None:
    """Assert that workflow-pin operations preserved every evidence byte."""
    assert {path.name: path.read_bytes() for path in run.iterdir()} == contents


def test_in_flight_pin_preserves_full_definition_and_old_evidence(tmp_path: Path) -> None:
    """Pin the full in-flight definition without changing prior evidence."""
    path = _source(tmp_path)
    definition = load_phase_workflow_definition(tmp_path, "custom")
    meta = _meta(definition)
    original_meta = copy.deepcopy(meta)
    run, contents = _evidence(tmp_path, meta)
    path.write_text("id: custom\nphases:\n  - id: replacement\n", encoding="utf-8")

    resumed = load_run_workflow_definition(tmp_path, "custom", meta)

    assert resumed.digest == definition.digest
    assert resumed.completion_disposition == "local-handoff"
    assert tuple(phase.id for phase in resumed.phases) == ("implement", "review", "done")
    assert resumed.phases[0].prompt == definition.phases[0].prompt
    assert resumed.phases[0].routes == {"default": "review"}
    assert resumed.phases[1].multi_review
    assert resumed.phases[1].pause_after
    assert resumed.phases[1].required_markers == ("verdict",)
    assert resumed.source_bytes == SOURCE.encode("utf-8")
    assert meta == original_meta
    _assert_evidence(run, contents)
    fresh = load_phase_workflow_definition(tmp_path, "custom")
    assert tuple(phase.id for phase in fresh.phases) == ("replacement",)
    assert fresh.digest != resumed.digest


@pytest.mark.parametrize("mode", ["clean", "local", "pending"])
def test_old_pin_preserves_marker_export_and_python_guard_classification(
    tmp_path: Path, mode: str
) -> None:
    from agent_flow.core.markers import (
        missing_architecture_assessment_markers,
        missing_markers,
    )
    from agent_flow.core.phase_workflow import effective_phase_markers

    original = SOURCE.replace(
        "required_markers: [verdict]",
        "required_markers: ['clean-architecture: applied|n/a', 'must-avoid-check: pass|fail|n/a']",
    )
    path = _source(tmp_path, original)
    definition = load_phase_workflow_definition(tmp_path, "custom")
    exported = definition.to_json_dict()
    meta = _meta(definition)
    original_meta = copy.deepcopy(meta)
    run, evidence = _evidence(tmp_path, meta)
    path.write_text(
        "id: custom\nphases:\n  - id: review\n"
        "    required_markers: ['architecture-contract: applied|n/a']\n"
        "    required_markers_by_architecture: {}\n",
        encoding="utf-8",
    )

    resumed = load_run_workflow_definition(tmp_path, "custom", meta)
    phase = resumed.phases[1]
    markers = effective_phase_markers(phase, mode)
    invalid = "## Completion Gate\nclean-architecture: n/a\nmust-avoid-check: n/a\n"
    valid = "## Completion Gate\nclean-architecture: applied\nmust-avoid-check: pass\n"

    assert missing_markers(invalid, markers) == []
    assert missing_architecture_assessment_markers(
        invalid, contract_required=True,
        conditional=phase.required_markers_by_architecture is not None,
    ) == ["clean-architecture: applied", "must-avoid-check: pass|fail"]
    assert missing_markers(valid, markers) == []
    assert missing_architecture_assessment_markers(
        valid, contract_required=True,
        conditional=phase.required_markers_by_architecture is not None,
    ) == []
    assert missing_markers(
        "## Completion Gate\narchitecture-contract: applied\nmust-avoid-check: pass\n",
        markers,
    ) == ["clean-architecture: applied|n/a"]
    assert resumed.to_json_dict() == exported
    assert meta == original_meta
    _assert_evidence(run, evidence)


def test_valid_pin_does_not_require_original_source_to_exist(tmp_path: Path) -> None:
    """Load a valid pin even after its original source disappears."""
    path = _source(tmp_path)
    definition = load_phase_workflow_definition(tmp_path, "custom")
    meta = _meta(definition)
    path.unlink()

    resumed = load_run_workflow_definition(tmp_path, "custom", meta)

    assert resumed.phases[1].artifact == "original-review.md"
    assert resumed.phases[1].routes == {"request-changes": "implement", "default": "done"}


@pytest.mark.parametrize(
    "corruption",
    ["payload", "source", "authority", "digest", "schema", "missing-payload"],
)
def test_invalid_pin_never_falls_back_or_mutates_old_evidence(
    tmp_path: Path, corruption: str
) -> None:
    """Fail closed on invalid pins without fallback or evidence mutation."""
    _source(tmp_path)
    meta = _meta(load_phase_workflow_definition(tmp_path, "custom"))
    if corruption == "payload":
        meta["workflow_definition"] = []
    elif corruption == "source":
        meta["workflow_definition"]["source_text"] += "# altered\n"
    elif corruption == "authority":
        meta["workflow_definition"]["kit_owned"] = True
    elif corruption == "digest":
        meta["workflow_digest"] = "0" * 64
    elif corruption == "schema":
        meta["workflow_definition"]["schema_version"] = True
    else:
        del meta["workflow_definition"]
    run, contents = _evidence(tmp_path, meta)

    with pytest.raises(WorkflowDefinitionPinError, match="Existing records and approvals"):
        load_run_workflow_definition(tmp_path, "custom", meta)

    _assert_evidence(run, contents)


def test_pin_cannot_reanchor_an_invalid_cursor(tmp_path: Path) -> None:
    """Prevent a valid definition pin from reanchoring an invalid cursor."""
    _source(tmp_path)
    meta = _meta(load_phase_workflow_definition(tmp_path, "custom"))
    meta["phase_index"] = 0
    run, contents = _evidence(tmp_path, meta)

    with pytest.raises(CorruptRunCursorError):
        load_run_workflow_definition(tmp_path, "custom", meta)

    _assert_evidence(run, contents)


def test_legacy_matching_definition_recovers_exact_obsolete_name(tmp_path: Path) -> None:
    """Recover an obsolete legacy workflow only when its exact definition matches."""
    source = SOURCE.replace("clean-architecture-core", "clean-architecture")
    path = _source(tmp_path, source)
    meta = {
        "run_id": "legacy-run",
        "workflow": "custom",
        "workflow_digest": hashlib.sha256(path.read_bytes()).hexdigest(),
        "phase_index": 1,
        "current_phase": "review",
        "phase_approval": {"nonce": "legacy-approval"},
    }
    run, contents = _evidence(tmp_path, meta)
    original_approval = copy.deepcopy(meta["phase_approval"])

    recovered = load_run_workflow_definition(tmp_path, "custom", meta)

    skills = recovered.phases[0].skills
    assert skills is not None
    assert skills.required == ("clean-architecture",)
    assert skills.pinned_legacy
    assert not skills.replaceable_architecture
    _assert_evidence(run, contents)
    bound = {**meta, **workflow_pin_metadata(recovered, workflow="custom")}
    path.write_text(SOURCE, encoding="utf-8")
    resumed = load_run_workflow_definition(tmp_path, "custom", bound)
    assert resumed.source_bytes == source.encode("utf-8")
    assert bound["phase_approval"] == original_approval
    path.write_text(source, encoding="utf-8")
    with pytest.raises(ValueError, match="migration required"):
        load_phase_workflow_definition(tmp_path, "custom")


@pytest.mark.parametrize("recorded_digest", [None, "0" * 64])
def test_unrecoverable_legacy_run_cannot_acquire_new_approval(
    tmp_path: Path, recorded_digest: str | None
) -> None:
    """Deny new approval when a legacy run's definition cannot be recovered."""
    _source(tmp_path)
    meta = {
        "workflow": "custom",
        "workflow_digest": recorded_digest,
        "phase_index": 1,
        "current_phase": "review",
        "phase_approval": {"nonce": "legacy-approval"},
    }
    run, contents = _evidence(tmp_path, meta)

    with pytest.raises(WorkflowDefinitionPinError, match="start a new run"):
        load_run_workflow_definition(tmp_path, "custom", meta)

    _assert_evidence(run, contents)


def test_pinned_kit_authority_survives_upgrade_without_granting_custom_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve pinned kit authority across upgrades without granting it to custom workflows."""
    path = _source(tmp_path)
    monkeypatch.setattr(
        "agent_flow.core.phase_workflow._packaged_workflow_path", lambda name: path
    )
    definition = load_phase_workflow_definition(tmp_path, "custom")
    meta = _meta(definition)
    path.write_text(SOURCE.replace("original-review.md", "new-review.md"), encoding="utf-8")
    monkeypatch.setattr(
        "agent_flow.core.phase_workflow._packaged_workflow_path", lambda name: None
    )

    resumed = load_run_workflow_definition(tmp_path, "custom", meta)
    fresh = load_phase_workflow_definition(tmp_path, "custom")

    assert resumed.phases[0].skills is not None
    assert resumed.phases[0].skills.replaceable_architecture
    assert fresh.phases[0].skills is not None
    assert not fresh.phases[0].skills.replaceable_architecture


@pytest.mark.parametrize(
    "source",
    [
        "id: custom\nphases:\n  - id: implement\n    routes: {default: missing}\n",
        "id: custom\nphases:\n  - id: implement\n    skills:\n"
        "      required: [clean-architecture]\n",
    ],
)
def test_matching_checksums_do_not_authorize_invalid_or_obsolete_new_pins(
    tmp_path: Path, source: str
) -> None:
    """Do not let matching checksums authorize invalid or newly obsolete pins."""
    _source(tmp_path)
    meta = _meta(load_phase_workflow_definition(tmp_path, "custom"))
    payload = meta["workflow_definition"]
    payload["source_text"] = source
    meta["workflow_digest"] = hashlib.sha256(source.encode("utf-8")).hexdigest()
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    meta["workflow_definition_digest"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    run, contents = _evidence(tmp_path, meta)

    with pytest.raises(WorkflowDefinitionPinError, match="invalid pinned definition"):
        load_run_workflow_definition(tmp_path, "custom", meta)

    _assert_evidence(run, contents)


@pytest.mark.parametrize("mode", ["clean", "local", "pending"])
def test_artifact_completion_selects_bound_conditional_obligations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    from agent_flow.artifact import ActiveRun, _missing_completion_markers, write_meta
    from agent_flow.core.architecture_policy import architecture_snapshot
    from tests.test_architecture_selection import (
        _clean_contract, _declare, _git_project, _local_contract,
    )

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = _git_project(tmp_path)
    if mode == "clean":
        _clean_contract(project)
    elif mode == "local":
        _local_contract(project)
    else:
        _declare(project, "schema_version: 1\narchitecture:\n  mode: pending\n")
    _source(project,
        "id: custom\nphases:\n  - id: inspect\n"
        "    required_markers: ['architecture-contract: applied']\n"
        "    required_markers_by_architecture:\n"
        "      clean: ['repository-boundary: pass|fail']\n"
    )
    definition = load_phase_workflow_definition(project, "custom")
    run = project / ".agent-flow/runs/r1"
    run.mkdir(parents=True)
    write_meta(run, {
        "workflow": "custom", "current_phase": "inspect", "phase_index": 0,
        "architecture_digest": architecture_snapshot(project).digest,
        **workflow_pin_metadata(definition, workflow="custom"),
    })
    artifact = run / "inspect.md"
    artifact.write_text("## Completion Gate\narchitecture-contract: applied\n")
    missing = _missing_completion_markers(
        run, "custom", "inspect", config_root=project, project_root=project,
    )
    ActiveRun(
        path=run, run_id="r1", workflow="custom", task="Inspect existing boundaries", started_at="",
    ).print_status(config_root=project, project_root=project)
    status = capsys.readouterr().out
    expected_reason = (
        "missing_completion_markers" if mode == "clean" else "phase_artifact_written_continue_required"
    )
    assert f"reason: {expected_reason}" in status
    assert missing == (["repository-boundary: pass|fail"] if mode == "clean" else [])
    artifact.write_text(
        "## Completion Gate\narchitecture-contract: applied\nrepository-boundary: pass\n"
    )
    assert _missing_completion_markers(
        run, "custom", "inspect", config_root=project, project_root=project,
    ) == []


def test_artifact_completion_keeps_legacy_python_guards_under_old_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_flow.artifact import _missing_completion_markers, write_meta
    from agent_flow.core.architecture_policy import architecture_snapshot
    from tests.test_architecture_selection import _git_project, _local_contract

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = _git_project(tmp_path)
    _local_contract(project)
    source = _source(project,
        "id: custom\nphases:\n  - id: implement\n"
        "    skills:\n      required: [architecture]\n"
        "    required_markers: ['clean-architecture: applied|n/a', 'must-avoid-check: pass|fail|n/a']\n"
    )
    definition = load_phase_workflow_definition(project, "custom")
    run = project / ".agent-flow/runs/r1"
    run.mkdir(parents=True)
    write_meta(run, {
        "workflow": "custom", "current_phase": "implement", "phase_index": 0,
        "architecture_digest": architecture_snapshot(project).digest,
        **workflow_pin_metadata(definition, workflow="custom"),
    })
    before = (run / "meta.json").read_bytes()
    source.write_text(
        "id: custom\nphases:\n  - id: implement\n"
        "    required_markers: ['architecture-contract: applied|n/a']\n"
        "    required_markers_by_architecture: {}\n"
    )
    artifact = run / "implement.md"
    content = (
        "## Completion Gate\nclean-architecture: n/a\nmust-avoid-check: n/a\n"
        "skill-availability: pass\nskill-use-evidence: unavailable\n"
        "project-local-skills: checked\nproject-local-skills-used: architecture\n"
        "project-local-skill-docs: applied\n"
    )
    artifact.write_text(content)
    assert _missing_completion_markers(
        run, "custom", "implement", config_root=project, project_root=project,
    ) == ["clean-architecture: applied", "must-avoid-check: pass|fail"]
    artifact.write_text(content.replace("architecture: n/a", "architecture: applied").replace(
        "must-avoid-check: n/a", "must-avoid-check: pass",
    ))
    assert _missing_completion_markers(
        run, "custom", "implement", config_root=project, project_root=project,
    ) == []
    assert (run / "meta.json").read_bytes() == before


def test_artifact_completion_rejects_changed_architecture_before_selecting_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_flow.artifact import _missing_completion_markers, write_meta
    from agent_flow.core.architecture_policy import architecture_snapshot
    from tests.test_architecture_selection import _clean_contract, _declare, _git_project

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = _git_project(tmp_path)
    _clean_contract(project)
    _source(project,
        "id: custom\nphases:\n  - id: inspect\n"
        "    required_markers_by_architecture:\n"
        "      clean: ['repository-boundary: pass|fail']\n"
    )
    definition = load_phase_workflow_definition(project, "custom")
    run = project / ".agent-flow/runs/r1"
    run.mkdir(parents=True)
    write_meta(run, {
        "workflow": "custom", "current_phase": "inspect", "phase_index": 0,
        "architecture_digest": architecture_snapshot(project).digest,
        **workflow_pin_metadata(definition, workflow="custom"),
    })
    (run / "inspect.md").write_text("## Completion Gate\n")
    _declare(project, "schema_version: 1\narchitecture:\n  mode: pending\n")
    with pytest.raises(ValueError, match="architecture_policy_drift"):
        _missing_completion_markers(
            run, "custom", "inspect", config_root=project, project_root=project,
        )


def test_invalid_conditional_workflow_cannot_use_legacy_artifact_fallback(tmp_path: Path) -> None:
    from agent_flow.artifact import _missing_completion_markers

    project = tmp_path / "project"
    workflows = project / ".agent-flow/workflows"
    workflows.mkdir(parents=True)
    (workflows / "custom.yaml").write_text(
        "id: custom\nphases:\n  - id: inspect\n"
        "    required_markers_by_architecture:\n      typo: ['evidence: applied']\n"
    )
    run = project / ".agent-flow/runs/r1"
    run.mkdir(parents=True)
    (run / "inspect.md").write_text("## Completion Gate\n")
    with pytest.raises(ValueError, match="required_markers_by_architecture"):
        _missing_completion_markers(
            run, "custom", "inspect", config_root=project, project_root=project,
        )
