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
    path = tmp_path / "workflows" / "custom.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    return path


def _meta(definition: PhaseWorkflowDefinition) -> dict[str, Any]:
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
    assert {path.name: path.read_bytes() for path in run.iterdir()} == contents


def test_in_flight_pin_preserves_full_definition_and_old_evidence(tmp_path: Path) -> None:
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


def test_valid_pin_does_not_require_original_source_to_exist(tmp_path: Path) -> None:
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
    _source(tmp_path)
    meta = _meta(load_phase_workflow_definition(tmp_path, "custom"))
    meta["phase_index"] = 0
    run, contents = _evidence(tmp_path, meta)

    with pytest.raises(CorruptRunCursorError):
        load_run_workflow_definition(tmp_path, "custom", meta)

    _assert_evidence(run, contents)


def test_legacy_matching_definition_recovers_exact_obsolete_name(tmp_path: Path) -> None:
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
    assert bound["phase_approval"] == meta["phase_approval"]
    path.write_text(source, encoding="utf-8")
    with pytest.raises(ValueError, match="migration required"):
        load_phase_workflow_definition(tmp_path, "custom")


@pytest.mark.parametrize("recorded_digest", [None, "0" * 64])
def test_unrecoverable_legacy_run_cannot_acquire_new_approval(
    tmp_path: Path, recorded_digest: str | None
) -> None:
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
