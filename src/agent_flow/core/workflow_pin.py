from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from agent_flow.core.phase_workflow import (
    CursorScope,
    PhaseWorkflowDefinition,
    RunCursor,
    WorkflowDriftError,
    load_phase_workflow_definition,
    parse_phase_workflow_definition,
)
from agent_flow.core.security import validate_safe_name


class WorkflowDefinitionPinError(WorkflowDriftError):
    pass


def workflow_pin_metadata(
    definition: PhaseWorkflowDefinition, *, workflow: str
) -> dict[str, Any]:
    """Capture the full definition for atomic metadata publication, not approval."""
    validate_safe_name(workflow, "workflow")
    if definition.id != workflow:
        raise WorkflowDefinitionPinError(
            f"workflow definition {definition.id!r} does not match {workflow!r}"
        )
    if not definition.source_bytes or (
        hashlib.sha256(definition.source_bytes).hexdigest() != definition.digest
    ):
        raise WorkflowDefinitionPinError("workflow definition has no matching source bytes")
    payload = {
        "schema_version": 1,
        "workflow": workflow,
        "source": definition.source,
        "source_text": definition.source_bytes.decode("utf-8"),
        "kit_owned": definition.kit_owned,
        "pinned_legacy": any(
            phase.skills is not None and phase.skills.pinned_legacy
            for phase in definition.phases
        ),
    }
    return {
        "workflow_digest": definition.digest,
        "workflow_definition": payload,
        "workflow_definition_digest": _pin_digest(payload),
    }


def load_run_workflow_definition(
    kit_root: Path, name: str, meta: Mapping[str, Any]
) -> PhaseWorkflowDefinition:
    """Recover a verified definition without modifying run or approval records."""
    validate_safe_name(name, "workflow")
    recorded_digest = meta.get("workflow_digest")
    if (
        meta.get("workflow") != name
        or not isinstance(recorded_digest, str)
        or len(recorded_digest) != 64
        or any(char not in "0123456789abcdef" for char in recorded_digest)
    ):
        raise _pin_error(name, "missing or invalid recorded workflow identity")
    if "workflow_definition" not in meta:
        if "workflow_definition_digest" in meta:
            raise _pin_error(name, "definition payload is missing")
        try:
            definition = load_phase_workflow_definition(
                kit_root, name, expected_digest=recorded_digest
            )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise _pin_error(name, f"legacy definition cannot be recovered: {exc}") from exc
    else:
        definition = _load_pin(name, meta, recorded_digest)
    RunCursor.from_meta(meta, CursorScope.of(definition))
    return definition


def _load_pin(
    name: str, meta: Mapping[str, Any], recorded_digest: str
) -> PhaseWorkflowDefinition:
    payload = meta["workflow_definition"]
    expected_keys = {
        "schema_version", "workflow", "source", "source_text", "kit_owned", "pinned_legacy"
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise _pin_error(name, "invalid definition payload")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or payload["workflow"] != name
        or not isinstance(payload["source"], str)
        or not payload["source"]
        or not isinstance(payload["source_text"], str)
        or type(payload["kit_owned"]) is not bool
        or type(payload["pinned_legacy"]) is not bool
    ):
        raise _pin_error(name, "invalid definition fields")
    if meta.get("workflow_definition_digest") != _pin_digest(payload):
        raise _pin_error(name, "definition binding digest does not match")
    try:
        source_bytes = payload["source_text"].encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _pin_error(name, "source text is not valid UTF-8") from exc
    if hashlib.sha256(source_bytes).hexdigest() != recorded_digest:
        raise _pin_error(name, "source bytes do not match the recorded workflow digest")
    try:
        return parse_phase_workflow_definition(
            source_bytes,
            source=Path(payload["source"]),
            name=name,
            kit_owned=payload["kit_owned"],
            pinned_legacy=payload["pinned_legacy"],
        )
    except (ValueError, yaml.YAMLError) as exc:
        raise _pin_error(name, f"invalid pinned definition: {exc}") from exc


def _pin_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _pin_error(name: str, reason: str) -> WorkflowDefinitionPinError:
    return WorkflowDefinitionPinError(
        f"workflow {name}: {reason}. Restore this run's original definition and "
        "binding from backup, or start a new run for the current definition. "
        "Existing records and approvals remain unchanged."
    )
