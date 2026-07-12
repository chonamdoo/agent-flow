from __future__ import annotations

import hashlib
import base64
import hmac
import json
import math
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


LEDGER_MODES = (
    "artifacts-only",
    "ledger-always",
    "ledger-selective",
    "action-self-review",
)
SCHEMA_VERSION = 1
EXTRACTOR_VERSION = "execution-ledger-v1"
MAX_FIX_LOOP_ROUNDS = 3
MAX_BLOCK_LINES = 5
MAX_ITEM_BYTES = 160
MAX_BLOCK_BYTES = 720
REVIEW_SUMMARY_SCHEMA_VERSION = 1
USAGE_RECEIPT_SCHEMA_VERSION = 1
USAGE_RECEIPT_KIND = "provider-usage-receipt"
CONTROL_RECEIPT_SCHEMA_VERSION = 1
CONTROL_RECEIPT_KIND = "execution-control-receipt"
PROVIDER_ATTESTATION_SCHEMA_VERSION = 1
PROVIDER_ATTESTATION_KIND = "provider-usage-attestation"
PROVIDER_ATTESTATION_ALGORITHM = "RS256"
VERIFIED_USAGE_EVIDENCE = "verified-provider-receipt"
SUMMARY_USAGE_EVIDENCE = "summary-only"
VERIFIED_CONTROL_EVIDENCE = "verified-control-receipt"
_COMMITMENT_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_COMMITTED_RECORD_TYPES = ("capture", "injection", "usage")
_STRICT_RFC3339 = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$"
)
ELIGIBLE_PHASES = frozenset(
    {
        "implement",
        "implement-fix",
        "red",
        "green",
        "refactor",
        "fix-loop",
        "pr-ci-fix",
        "pr-comment-fix",
    }
)
SELECTIVE_FIX_PHASES = frozenset({"fix-loop", "implement-fix"})
FIX_ROUTE_TARGETS = frozenset(
    {"fix-loop", "implement-fix", "refactor", "pr-ci-fix", "pr-comment-fix"}
)
REVIEW_PHASES = frozenset(
    {"plan-review", "review", "final-review", "multi-review", "architecture-review"}
)
EXPOSURE_POLICY = {
    "schema_version": 1,
    "selective_trigger": "repeat-or-literal-fix-loop",
    "selective_fix_phases": sorted(SELECTIVE_FIX_PHASES),
    "eligible_phases": sorted(ELIGIBLE_PHASES),
    "excluded_phases": ["multi-review"],
    "independent_reviewer_exclusion": "phase.multi_review-or-multi-review-id",
    "non_agent_excluded_phases": [
        "commit",
        "gates",
        "merge",
        "merge-approval",
        "pr-watch",
        "push-pr",
    ],
    "target_prompt_kind": "action-agent",
    "max_lines": MAX_BLOCK_LINES,
    "max_bytes": MAX_BLOCK_BYTES,
    "per_exposure_token_cap": math.ceil(MAX_BLOCK_BYTES / 4),
}
_ANSI_PATTERN = re.compile(r"\x1b(?:[@-_][0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def resolve_ledger_mode(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return "artifacts-only"
    if normalized not in LEDGER_MODES:
        raise ValueError(f"invalid AGENT_FLOW_LEDGER_MODE: {value}")
    return normalized


def validate_execution_state_ledger(
    *,
    run_dir: Path,
    run_id: str | None = None,
    mode: str | None = None,
    require_completion: bool = False,
) -> dict[str, object]:
    try:
        paths = _ledger_paths(run_dir)
        _recover_pending_transaction(Path(run_dir).resolve(), paths)
        config = _read_json(paths["config"])
        normalized_mode = resolve_ledger_mode(mode or config.get("mode"))
        expected_run_id = run_id or config.get("run_id")
        _assert_run_document(config, expected_run_id, normalized_mode, "config.json")
        _assert_runner_control_snapshot(config)
        workflow = _read_json(paths["workflow"])
        _assert_workflow_snapshot(config, workflow)
        if normalized_mode != "artifacts-only":
            _assert_ledger(_read_json(paths["ledger"]), expected_run_id)
        validation = _validate_committed_records(
            run_dir=Path(run_dir).resolve(),
            paths=paths,
            config=config,
            workflow=workflow,
            require_completion=require_completion,
        )
        return {
            "ok": True,
            "verified": True,
            "completed": validation["completion"] is not None,
            "event_count": validation["event_count"],
            "event_head_sha256": validation["event_head_sha256"],
            "config": config,
            "workflow": workflow,
            "captures": validation["records_by_type"]["capture"],
            "injections": validation["records_by_type"]["injection"],
            "usage": validation["records_by_type"]["usage"],
            "metrics": validation["metrics"],
            "reasons": [],
        }
    except Exception as exc:
        return _fail_open(
            exc, verified=False, completed=False, reasons=[str(exc)]
        )


def _canonical_workflow_snapshot(
    workflow_id: object,
    workflow_phases: Iterable[object],
) -> dict[str, object]:
    workflow = _require_string(workflow_id, "workflow_id")
    phases = list(workflow_phases)
    if not phases:
        raise ValueError("missing execution ledger workflow phases")
    canonical_phases: list[dict[str, object]] = []
    for index, phase in enumerate(phases):
        if not isinstance(phase, dict) and not hasattr(phase, "__dict__"):
            raise ValueError(f"invalid execution ledger workflow phase: {index}")
        routes_value = _get(phase, "routes")
        routes = None if routes_value is None else _canonical_workflow_routes(routes_value, index)
        required_markers = _get(phase, "required_markers", [])
        if not isinstance(required_markers, (list, tuple)) or not all(
            isinstance(item, str) for item in required_markers
        ):
            raise ValueError(f"invalid execution ledger workflow required_markers: {index}")
        canonical_phases.append(
            {
                "index": index,
                "id": _require_string(_get(phase, "id"), f"workflow phase {index} id"),
                "artifact": _optional_string(_get(phase, "artifact")),
                "description": (
                    _get(phase, "description")
                    if isinstance(_get(phase, "description"), str)
                    else ""
                ),
                "instruction": (
                    _get(phase, "instruction")
                    if isinstance(_get(phase, "instruction"), str)
                    else (
                        _get(phase, "prompt")
                        if isinstance(_get(phase, "prompt"), str)
                        else ""
                    )
                ),
                "prompt": _optional_string(_get(phase, "prompt")),
                "required_markers": list(required_markers),
                "optional": _get(phase, "optional", False) is True,
                "pause_after": _get(phase, "pause_after", False) is True,
                "multi_review": _get(phase, "multi_review", False) is True,
                "cite_lore": _get(phase, "cite_lore", False) is True,
                "routes": routes,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": workflow,
        "phases": canonical_phases,
    }


def _canonical_workflow_routes(value: object, index: int) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"invalid execution ledger workflow routes: {index}")
    routes: dict[str, str] = {}
    for key in sorted(value):
        target = value[key]
        if not isinstance(key, str) or not key or not isinstance(target, str) or not target:
            raise ValueError(f"invalid execution ledger workflow route: {index}")
        routes[key] = target
    return routes


def _canonical_runner_control_snapshot(
    *,
    task: str,
    workflow_id: str,
    workflow_snapshot_sha256: str,
    base_commit: str | None,
    run_snapshot: dict[str, object],
    exposure_policy_sha256: str,
    execution_controls_sha256: str,
) -> dict[str, object]:
    if not isinstance(run_snapshot, dict):
        raise ValueError("invalid execution ledger run snapshot")
    return {
        "schema_version": SCHEMA_VERSION,
        "runtime_id": _require_identifier(
            run_snapshot.get("runtime_id"), "runtime_id"
        ),
        "task_sha256": _sha256(task),
        "workflow_id": _require_string(workflow_id, "workflow_id"),
        "workflow_sha256": _require_commitment(
            workflow_snapshot_sha256, "workflow_sha256"
        ),
        "base_commit": _optional_string(base_commit),
        "profile_snapshot_sha256": _require_commitment(
            run_snapshot.get("profile_snapshot_sha256"),
            "profile_snapshot_sha256",
        ),
        "installed_skill_plan_sha256": _require_commitment(
            run_snapshot.get("installed_skill_plan_sha256"),
            "installed_skill_plan_sha256",
        ),
        "local_skill_plan_sha256": _require_commitment(
            run_snapshot.get("local_skill_plan_sha256"),
            "local_skill_plan_sha256",
        ),
        "lore_snapshot_sha256": _require_commitment(
            run_snapshot.get("lore_snapshot_sha256"),
            "lore_snapshot_sha256",
        ),
        "prompt_controls_sha256": _require_commitment(
            run_snapshot.get("prompt_controls_sha256"),
            "prompt_controls_sha256",
        ),
        "fix_loop_max_rounds": MAX_FIX_LOOP_ROUNDS,
        "exposure_policy_sha256": _require_commitment(
            exposure_policy_sha256, "exposure_policy_sha256"
        ),
        "execution_controls_sha256": _require_commitment(
            execution_controls_sha256, "execution_controls_sha256"
        ),
    }


def _canonical_execution_controls(experiment: object) -> dict[str, object]:
    if not isinstance(experiment, dict):
        raise ValueError("invalid execution ledger experiment controls")
    pricing_snapshot = _normalize_pricing_snapshot(experiment.get("pricing_snapshot"))
    public_key = _normalize_provider_public_key(
        experiment.get("provider_attestation_public_key")
    )
    key_id = _optional_control_identifier(
        experiment.get("provider_attestation_key_id"),
        "provider_attestation_key_id",
    )
    if (public_key is None) != (key_id is None):
        raise ValueError(
            "provider attestation key id and public key must be configured together"
        )
    return {
        "model_id": _optional_string(experiment.get("model_id")),
        "tool_permissions_sha256": _optional_commitment(
            experiment.get("tool_permissions_sha256"), "tool_permissions_sha256"
        ),
        "system_prompt_sha256": _optional_commitment(
            experiment.get("system_prompt_sha256"), "system_prompt_sha256"
        ),
        "caps_sha256": _optional_commitment(
            experiment.get("caps_sha256"), "caps_sha256"
        ),
        "provider_retry_policy_sha256": _optional_commitment(
            experiment.get("provider_retry_policy_sha256"),
            "provider_retry_policy_sha256",
        ),
        "provider_max_retries": _optional_nonnegative_integer(
            experiment.get("provider_max_retries"), "provider_max_retries"
        ),
        "pricing_snapshot": pricing_snapshot,
        "pricing_snapshot_sha256": (
            None if pricing_snapshot is None else _sha256(_canonical_json(pricing_snapshot))
        ),
        "provider_attestation_key_id": key_id,
        "provider_attestation_public_key": public_key,
        "provider_attestation_public_key_sha256": (
            None if public_key is None else _sha256(_canonical_json(public_key))
        ),
    }


def _normalize_pricing_snapshot(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    _assert_exact_object_keys(
        value,
        {"currency", "input_per_million", "output_per_million", "snapshot_id"},
        "pricing snapshot",
    )
    assert isinstance(value, dict)
    return {
        "currency": _require_string(value.get("currency"), "pricing currency"),
        "input_per_million": _normalize_pricing_rate(
            value.get("input_per_million"), "pricing input_per_million"
        ),
        "output_per_million": _normalize_pricing_rate(
            value.get("output_per_million"), "pricing output_per_million"
        ),
        "snapshot_id": _require_string(
            value.get("snapshot_id"), "pricing snapshot_id"
        ),
    }


def _normalize_provider_public_key(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    _assert_exact_object_keys(value, {"e", "kty", "n"}, "provider attestation public key")
    assert isinstance(value, dict)
    if value.get("kty") != "RSA":
        raise ValueError("provider attestation public key must be RSA")
    modulus = _decode_base64url(value.get("n"), "provider attestation modulus")
    exponent = _decode_base64url(value.get("e"), "provider attestation exponent")
    if (
        len(modulus) < 128
        or modulus[0] < 0x80
        or not exponent
        or exponent[0] == 0
        or len(exponent) > 4
    ):
        raise ValueError("provider attestation RSA key is too weak or malformed")
    exponent_value = int.from_bytes(exponent, "big")
    if exponent_value < 3 or exponent_value % 2 == 0:
        raise ValueError("provider attestation RSA exponent is invalid")
    return {"kty": "RSA", "n": str(value["n"]), "e": str(value["e"])}


def initialize_execution_state_ledger(
    *,
    run_dir: Path,
    run_id: str,
    mode: str,
    experiment_enabled: bool = False,
    task: str = "",
    workflow_id: str,
    workflow_phases: Iterable[object] = (),
    base_commit: str | None = None,
    experiment: dict[str, object] | None = None,
    run_snapshot: dict[str, object] | None = None,
) -> dict[str, object]:
    try:
        if experiment_enabled is not True:
            return {"ok": True, "enabled": False}
        normalized_mode = resolve_ledger_mode(mode)
        paths = _ledger_paths(run_dir)
        controls = experiment or {}
        execution_controls = _canonical_execution_controls(controls)
        execution_controls_sha256 = _sha256(_canonical_json(execution_controls))
        workflow = _canonical_workflow_snapshot(workflow_id, workflow_phases)
        workflow_snapshot_sha256 = _sha256(_canonical_json(workflow))
        exposure_policy_sha256 = _sha256(_canonical_json(EXPOSURE_POLICY))
        runner_control_snapshot = _canonical_runner_control_snapshot(
            task=task,
            workflow_id=workflow_id,
            workflow_snapshot_sha256=workflow_snapshot_sha256,
            base_commit=base_commit,
            run_snapshot=run_snapshot or {},
            exposure_policy_sha256=exposure_policy_sha256,
            execution_controls_sha256=execution_controls_sha256,
        )
        config = {
            "schema_version": SCHEMA_VERSION,
            "run_id": _require_string(run_id, "run_id"),
            "mode": normalized_mode,
            "task_sha256": _sha256(task),
            "workflow_id": _require_string(workflow_id, "workflow_id"),
            "workflow_sha256": workflow_snapshot_sha256,
            "workflow_snapshot_sha256": workflow_snapshot_sha256,
            "base_commit": _optional_string(base_commit),
            "fix_loop_max_rounds": MAX_FIX_LOOP_ROUNDS,
            "runner_control_snapshot": runner_control_snapshot,
            "runner_control_snapshot_sha256": _sha256(
                _canonical_json(runner_control_snapshot)
            ),
            "exposure_policy": EXPOSURE_POLICY,
            "exposure_policy_sha256": exposure_policy_sha256,
            "per_exposure_token_cap": EXPOSURE_POLICY[
                "per_exposure_token_cap"
            ],
            "experiment_id": _optional_string(controls.get("experiment_id")),
            **execution_controls,
            "execution_controls_sha256": execution_controls_sha256,
        }
        _write_immutable_json(paths["workflow"], workflow)
        _write_immutable_json(paths["config"], config)
        _ensure_jsonl(paths["captures"])
        _ensure_jsonl(paths["injections"])
        _ensure_jsonl(paths["usage"])
        _ensure_jsonl(paths["events"])
        if normalized_mode != "artifacts-only":
            _write_immutable_json(paths["ledger"], _empty_ledger(run_id))
        if not paths["metrics"].exists():
            _write_canonical_json(
                paths["metrics"], _empty_metrics(run_id, normalized_mode, config)
            )
        else:
            _assert_run_document(_read_json(paths["metrics"]), run_id, normalized_mode, "metrics.json")
        return {"ok": True, "enabled": True, "config": config}
    except Exception as exc:
        return _fail_open(exc)


def capture_execution_state(
    *,
    run_dir: Path,
    run_id: str,
    mode: str,
    experiment_enabled: bool = False,
    phase: object,
    artifact_path: Path,
    project_root: Path,
    round: int,
    fix_loop_rounds: int = 0,
    generated_at: str | None = None,
    workflow_id: str | None = None,
    route_key: str | None = None,
    routed_to: str | None = None,
    transition_occurrence_id: str | None = None,
) -> dict[str, object]:
    return _capture_execution_state_impl(**locals())


def observe_execution_state_injection(
    *,
    run_dir: Path,
    run_id: str,
    mode: str,
    experiment_enabled: bool = False,
    phase: object,
    project_root: Path,
    round: int,
    generated_at: str | None = None,
    prompt_bytes: int | None = None,
    prompt_chars: int = 0,
) -> dict[str, object]:
    return _observe_execution_state_injection_impl(**locals())


def execution_state_prompt_block(
    *,
    run_dir: Path,
    run_id: str,
    mode: str,
    experiment_enabled: bool = False,
    phase: object,
    project_root: Path,
    round: int,
) -> str:
    return _execution_state_prompt_block_impl(**locals())


def record_execution_state_usage(
    *,
    run_dir: Path,
    run_id: str,
    mode: str,
    experiment_enabled: bool = False,
    event_id: str | None = None,
    generated_at: str | None = None,
    scope: str | None = None,
    phase_id: str | None = None,
    round: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    additional_tokens: int | None = None,
    latency_ms: int | None = None,
    estimated_cost_usd: str | None = None,
    model_id: str | None = None,
    receipt_path: Path | None = None,
    receipt_sha256: str | None = None,
) -> dict[str, object]:
    return _record_execution_state_usage_impl(**locals())


def _ledger_paths(run_dir: Path | str) -> dict[str, Path]:
    root = Path(run_dir).resolve() / "artifacts" / "execution-ledger"
    return {
        "root": root,
        "config": root / "config.json",
        "workflow": root / "workflow.json",
        "captures": root / "captures.jsonl",
        "injections": root / "injections.jsonl",
        "events": root / "events.jsonl",
        "metrics": root / "metrics.json",
        "ledger": root / "ledger.json",
        "sources": root / "sources",
        "usage": root / "usage.jsonl",
        "completion": root / "completion.json",
        "journal": root / "transaction.json",
        "lock": root / "transaction.lock",
    }


def _empty_ledger(run_id: str) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "entries": {"status": [], "knowledge": [], "procedural": []},
    }


def _empty_metrics(
    run_id: str, mode: str, config: dict[str, object] | None = None
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "mode": mode,
        "gate_green_le_3": None,
        "gate_green_attempt": None,
        "repeated_command_fingerprint_count": 0,
        "repeated_command_fingerprint_rate": 0,
        "repeated_failure_fingerprint_count": 0,
        "repeated_failure_fingerprint_rate": 0,
        "repeated_diagnosis_fingerprint_count": 0,
        "repeated_diagnosis_fingerprint_rate": 0,
        "repeated_finding_fingerprint_count": 0,
        "repeated_finding_fingerprint_rate": 0,
        "fix_loop_rounds": 0,
        "prompt_bytes": 0,
        "injected_bytes": 0,
        "prompt_token_proxy": 0,
        "injected_token_proxy": 0,
        "runner_control_snapshot_sha256": (
            config.get("runner_control_snapshot_sha256") if config else None
        ),
        "exposure_policy_sha256": (
            config.get("exposure_policy_sha256") if config else None
        ),
        "per_exposure_token_cap": (
            config.get("per_exposure_token_cap", 0) if config else 0
        ),
        "exposure_budget_compliant": config is not None,
        "max_injected_token_proxy": 0,
        "actual_usage_coverage": False,
        "actual_control_coverage": False,
        "actual_control_evidence_status": None,
        "actual_input_tokens": None,
        "actual_output_tokens": None,
        "actual_total_tokens": None,
        "actual_additional_tokens": None,
        "actual_additional_token_coverage": False,
        "actual_usage_budget_matched": False,
        "actual_usage_evidence_status": None,
        "summary_usage_coverage": False,
        "summary_input_tokens": None,
        "summary_output_tokens": None,
        "summary_additional_tokens": None,
        "summary_latency_ms": None,
        "summary_estimated_cost": None,
        "injected_event_count": 0,
        "latency_proxy_ms": None,
        "latency_ms": None,
        "estimated_cost": None,
        "stale_candidate_count": 0,
        "unsupported_source_count": 0,
        "malformed_source_count": 0,
        "excluded_source_count": 0,
        "stale_reminder_count": 0,
        "stale_reminder_rate": 0,
        "unsupported_reminder_count": 0,
        "unsupported_reminder_rate": 0,
        "stale_count": 0,
        "unsupported_count": 0,
        "canonical_routing_violations": 0,
        "canonical_transition_count": 0,
        "canonical_route_coverage_complete": False,
        "canonical_route_coverage_gap_count": 1,
        "routing_provenance": [],
        "experiment_valid": False,
    }


def _load_context(run_dir: Path | str, run_id: object, mode: object) -> dict[str, object]:
    paths = _ledger_paths(run_dir)
    _recover_pending_transaction(Path(run_dir).resolve(), paths)
    normalized_mode = resolve_ledger_mode(mode)
    config = _read_json(paths["config"])
    _assert_run_document(config, run_id, normalized_mode, "config.json")
    _assert_runner_control_snapshot(config)
    workflow = _read_json(paths["workflow"])
    _assert_workflow_snapshot(config, workflow)
    ledger = None
    if normalized_mode != "artifacts-only":
        ledger = _read_json(paths["ledger"])
        _assert_ledger(ledger, run_id)
    validation = _validate_committed_records(
        run_dir=Path(run_dir).resolve(),
        paths=paths,
        config=config,
        workflow=workflow,
        require_completion=False,
    )
    return {
        "run_dir": Path(run_dir).resolve(),
        "paths": paths,
        "config": config,
        "workflow": workflow,
        "ledger": ledger,
        "completion": validation["completion"],
    }


def _assert_workflow_snapshot(
    config: dict[str, Any], workflow: dict[str, Any]
) -> None:
    if (
        workflow.get("schema_version") != SCHEMA_VERSION
        or workflow.get("workflow_id") != config.get("workflow_id")
        or not isinstance(workflow.get("phases"), list)
    ):
        raise RuntimeError("execution ledger workflow snapshot identity mismatch")
    canonical = _canonical_workflow_snapshot(
        workflow["workflow_id"], workflow["phases"]
    )
    if _canonical_json(canonical) != _canonical_json(workflow):
        raise RuntimeError("invalid execution ledger workflow snapshot")
    digest = _sha256(_canonical_json(workflow))
    if (
        config.get("workflow_snapshot_sha256") != digest
        or config.get("workflow_sha256") != digest
    ):
        raise RuntimeError("execution ledger workflow snapshot hash mismatch")


def _committed_record_path(paths: dict[str, Path], record_type: str) -> Path:
    if record_type == "capture":
        return paths["captures"]
    if record_type == "injection":
        return paths["injections"]
    if record_type == "usage":
        return paths["usage"]
    raise ValueError(f"invalid execution ledger record type: {record_type}")


def _without_content_commitment(
    record: object, *, include_chain: bool = True
) -> dict[str, object]:
    if not isinstance(record, dict):
        raise RuntimeError("invalid execution ledger committed record")
    copy = dict(record)
    copy.pop("content_sha256", None)
    if not include_chain:
        copy.pop("sequence", None)
        copy.pop("previous_content_sha256", None)
    return copy


def _append_committed_record(
    context: dict[str, object],
    record_type: str,
    payload: dict[str, object],
    *,
    strict: bool,
    completed_at: str | None = None,
) -> bool:
    if record_type not in _COMMITTED_RECORD_TYPES:
        raise ValueError(f"invalid execution ledger record type: {record_type}")
    paths = context["paths"]
    owner = _acquire_transaction_lock(paths)
    try:
        _recover_pending_transaction_locked(Path(context["run_dir"]), paths)
        target = _committed_record_path(paths, record_type)
        existing = next(
            (
                record
                for record in _read_jsonl(target)
                if record.get("id") == payload.get("id")
            ),
            None,
        )
        if existing is not None:
            if strict and _canonical_json(
                _without_content_commitment(existing, include_chain=False)
            ) != _canonical_json(payload):
                raise RuntimeError(
                    f"execution ledger event id collision: {payload['id']}"
                )
            return False
        if _sidecar_path_exists(paths["completion"]):
            raise RuntimeError("execution ledger is finalized")
        _validate_prospective_record(context, record_type, payload)
        events = _read_jsonl(paths["events"])
        sequence = len(events) + 1
        previous_content_sha256 = (
            events[-1]["record_content_sha256"] if events else None
        )
        committed_base = {
            **payload,
            "sequence": sequence,
            "previous_content_sha256": previous_content_sha256,
        }
        committed = {
            **committed_base,
            "content_sha256": _sha256(_canonical_json(committed_base)),
        }
        index_base = {
            "schema_version": SCHEMA_VERSION,
            "run_id": context["config"]["run_id"],
            "sequence": sequence,
            "record_type": record_type,
            "record_id": committed["id"],
            "record_content_sha256": committed["content_sha256"],
            "previous_content_sha256": previous_content_sha256,
        }
        event = {**index_base, "id": _sha256(_canonical_json(index_base))}
        journal_base = {
            "schema_version": SCHEMA_VERSION,
            "run_id": context["config"]["run_id"],
            "record_type": record_type,
            "record": committed,
            "event": event,
            "completed_at": completed_at,
        }
        journal = {
            **journal_base,
            "content_sha256": _sha256(_canonical_json(journal_base)),
        }
        _write_immutable_json(paths["journal"], journal)
        _apply_pending_transaction_locked(
            Path(context["run_dir"]), paths, journal, allow_fault_injection=True
        )
        _unlink_safe_sidecar_file(paths["journal"])
        return True
    finally:
        _release_transaction_lock(paths, owner)


def _validate_prospective_record(
    context: dict[str, object],
    record_type: str,
    payload: dict[str, object],
) -> None:
    validation = _validate_committed_records(
        run_dir=Path(context["run_dir"]),
        paths=context["paths"],
        config=context["config"],
        workflow=context["workflow"],
        require_completion=False,
    )
    if record_type == "capture":
        if any(
            record.get("transition_occurrence_id")
            == payload.get("transition_occurrence_id")
            for record in validation["records_by_type"]["capture"]
        ):
            raise RuntimeError(
                "execution ledger transition occurrence id collision"
            )
        gate_attempt = (
            sum(
                1
                for record in validation["records_by_type"]["capture"]
                if record.get("phase") == "gates"
            )
            + 1
            if payload.get("phase") == "gates"
            else None
        )
        _validate_capture_projection(
            Path(context["run_dir"]),
            context["config"],
            context["workflow"],
            payload,
            gate_attempt,
        )
    elif record_type == "injection":
        _validate_injection_evidence(
            payload,
            Path(context["run_dir"]),
            context["config"],
            context["workflow"],
            validation["replayed_ledger"],
        )
    else:
        _validate_usage_evidence(
            payload, Path(context["run_dir"]), context["config"]
        )
        if payload.get("scope") == "run-total" and any(
            record.get("scope") == "run-total"
            for record in validation["records_by_type"]["usage"]
        ):
            raise RuntimeError(
                "execution ledger already has a distinct run-total usage event"
            )


def _recover_pending_transaction(
    run_dir: Path, paths: dict[str, Path] | None = None
) -> None:
    resolved_paths = paths or _ledger_paths(run_dir)
    if not _sidecar_path_exists(resolved_paths["journal"]):
        return
    owner = _acquire_transaction_lock(resolved_paths)
    try:
        _recover_pending_transaction_locked(run_dir, resolved_paths)
    finally:
        _release_transaction_lock(resolved_paths, owner)


def _recover_pending_transaction_locked(
    run_dir: Path, paths: dict[str, Path]
) -> None:
    if not _sidecar_path_exists(paths["journal"]):
        return
    journal = _read_json(paths["journal"])
    _apply_pending_transaction_locked(
        run_dir, paths, journal, allow_fault_injection=False
    )
    _unlink_safe_sidecar_file(paths["journal"])


def _apply_pending_transaction_locked(
    run_dir: Path,
    paths: dict[str, Path],
    journal: dict[str, Any],
    *,
    allow_fault_injection: bool,
) -> None:
    config = _read_json(paths["config"])
    workflow = _read_json(paths["workflow"])
    _assert_workflow_snapshot(config, workflow)
    _validate_transaction_journal(journal, config)
    _ensure_transaction_record(
        _committed_record_path(paths, journal["record_type"]), journal["record"]
    )
    _maybe_inject_transaction_fault("target", allow_fault_injection)
    _ensure_transaction_record(paths["events"], journal["event"])
    _maybe_inject_transaction_fault("event", allow_fault_injection)
    raw_validation = _validate_committed_records(
        run_dir=run_dir.resolve(),
        paths=paths,
        config=config,
        workflow=workflow,
        require_completion=False,
        allow_pending_completion=True,
        skip_derived=True,
    )
    repaired_context = {
        "run_dir": run_dir.resolve(),
        "paths": paths,
        "config": config,
        "workflow": workflow,
        "ledger": raw_validation["replayed_ledger"],
        "completion": None,
    }
    if config["mode"] != "artifacts-only":
        _write_canonical_json(paths["ledger"], raw_validation["replayed_ledger"])
    _maybe_inject_transaction_fault("ledger", allow_fault_injection)
    _recompute_metrics(repaired_context)
    _maybe_inject_transaction_fault("metrics", allow_fault_injection)
    if journal.get("completed_at") is not None:
        _write_completion_for_transaction(
            repaired_context, journal["completed_at"], raw_validation
        )
        _maybe_inject_transaction_fault("completion", allow_fault_injection)
    _validate_committed_records(
        run_dir=run_dir.resolve(),
        paths=paths,
        config=config,
        workflow=workflow,
        require_completion=journal.get("completed_at") is not None,
    )


def _validate_transaction_journal(
    journal: dict[str, Any], config: dict[str, Any]
) -> None:
    record = journal.get("record")
    event = journal.get("event")
    if (
        journal.get("schema_version") != SCHEMA_VERSION
        or journal.get("run_id") != config.get("run_id")
        or journal.get("record_type") not in _COMMITTED_RECORD_TYPES
        or not isinstance(record, dict)
        or not isinstance(event, dict)
        or not isinstance(journal.get("content_sha256"), str)
        or _COMMITMENT_SHA256.fullmatch(journal["content_sha256"]) is None
        or _sha256(_canonical_json(_without_content_commitment(journal)))
        != journal["content_sha256"]
        or record.get("run_id") != config.get("run_id")
        or event.get("run_id") != config.get("run_id")
        or event.get("record_type") != journal.get("record_type")
        or event.get("record_id") != record.get("id")
        or event.get("record_content_sha256") != record.get("content_sha256")
        or event.get("sequence") != record.get("sequence")
        or event.get("previous_content_sha256")
        != record.get("previous_content_sha256")
    ):
        raise RuntimeError("invalid execution ledger transaction journal")
    if (
        not isinstance(record.get("content_sha256"), str)
        or _COMMITMENT_SHA256.fullmatch(record["content_sha256"]) is None
        or _sha256(_canonical_json(_without_content_commitment(record)))
        != record["content_sha256"]
    ):
        raise RuntimeError("invalid execution ledger transaction record commitment")
    semantic_payload = _without_content_commitment(record, include_chain=False)
    semantic_payload.pop("id", None)
    expected_record_id = (
        _sha256(
            _canonical_json(
                {"run_id": record.get("run_id"), "event_id": record.get("event_id")}
            )
        )
        if journal["record_type"] == "usage"
        else _sha256(_canonical_json(semantic_payload))
    )
    if record.get("id") != expected_record_id:
        raise RuntimeError("invalid execution ledger transaction record id")
    index_base = dict(event)
    index_base.pop("id", None)
    if event.get("id") != _sha256(_canonical_json(index_base)):
        raise RuntimeError("invalid execution ledger transaction event commitment")
    completed_at = journal.get("completed_at")
    if completed_at is not None:
        if journal["record_type"] != "usage" or record.get("scope") != "run-total":
            raise RuntimeError("invalid execution ledger transaction completion")
        if _normalize_timestamp(completed_at) != completed_at:
            raise RuntimeError("invalid execution ledger transaction timestamp")


def _ensure_transaction_record(path: Path, expected: dict[str, Any]) -> None:
    records = _read_jsonl(path)
    existing = next(
        (record for record in records if record.get("id") == expected.get("id")),
        None,
    )
    if existing is not None:
        if _canonical_json(existing) != _canonical_json(expected):
            raise RuntimeError("execution ledger transaction record mismatch")
        return
    if any(record.get("sequence") == expected.get("sequence") for record in records):
        raise RuntimeError("execution ledger transaction sequence collision")
    _append_jsonl_record(path, expected)


def _write_completion_for_transaction(
    context: dict[str, object], completed_at: str, validation: dict[str, object]
) -> None:
    completion_base = {
        "schema_version": SCHEMA_VERSION,
        "run_id": context["config"]["run_id"],
        "mode": context["config"]["mode"],
        "completed_at": completed_at,
        "event_count": validation["event_count"],
        "event_head_sha256": validation["event_head_sha256"],
        "terminal_state": _canonical_terminal_state(
            context["run_dir"], context["config"], context["workflow"]
        ),
        "files": _finalized_file_commitments(
            context["paths"], context["config"]["mode"]
        ),
    }
    _write_immutable_json(
        context["paths"]["completion"],
        {
            **completion_base,
            "content_sha256": _sha256(_canonical_json(completion_base)),
        },
    )


def _maybe_inject_transaction_fault(point: str, enabled: bool) -> None:
    if enabled and os.environ.get("AGENT_FLOW_LEDGER_FAULT_AFTER") == point:
        raise RuntimeError(
            f"injected execution ledger transaction fault after {point}"
        )


def _capture_source_identities(
    run_dir: Path,
    artifact_path: Path,
    phase_id: str,
    route_key: str,
    source_eligible: bool,
) -> list[dict[str, str]]:
    if not source_eligible:
        return []
    if not artifact_path.exists() and not artifact_path.is_symlink():
        return []
    resolved_run = run_dir.resolve()
    resolved_artifact = artifact_path.resolve()
    original_path = _safe_relative_path(
        resolved_run, resolved_artifact, "source artifact"
    )
    supported_review = bool(
        phase_id != "gates"
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", phase_id)
        and route_key in {"approve", "request-changes", "blocked"}
    )
    return [
        {
            "selector": (
                "/"
                if phase_id == "gates"
                else (
                    "/review-artifact"
                    if supported_review
                    else "/unsupported-review-summary"
                )
            ),
            "original_path": original_path,
            "sha256": _sha256(resolved_artifact.read_bytes()),
        }
    ]


def _recorded_capture_source_identities(
    capture: dict[str, object],
) -> list[dict[str, str]]:
    provenance_records = capture.get("source_provenance", [])
    if not isinstance(provenance_records, list):
        return []
    return sorted(
        (
            {
                "selector": source["selector"],
                "original_path": source["original_path"],
                "sha256": source["sha256"],
            }
            for source in provenance_records
            if isinstance(source, dict)
            and isinstance(source.get("selector"), str)
            and isinstance(source.get("original_path"), str)
            and isinstance(source.get("sha256"), str)
            and (
                source.get("selector")
                in {"/review-artifact", "/unsupported-review-summary"}
                or (
                    capture.get("phase") == "gates"
                    and source.get("selector") == "/"
                )
            )
        ),
        key=_canonical_json,
    )


def _find_equivalent_capture(
    captures: list[dict[str, object]],
    *,
    run_dir: Path,
    artifact_path: Path,
    phase_id: str,
    round_number: int,
    workflow_id: str,
    route_key: str,
    routed_to: str,
    fix_loop_rounds: int,
    normalization_context: dict[str, object],
    source_eligible: bool,
    source_identities: list[dict[str, str]] | None,
    transition_occurrence_id: str,
) -> dict[str, object] | None:
    identities = source_identities or _capture_source_identities(
        run_dir, artifact_path, phase_id, route_key, source_eligible
    )
    return next(
        (
            capture
            for capture in captures
            if capture.get("transition_occurrence_id")
            == transition_occurrence_id
            and capture.get("phase") == phase_id
            and capture.get("round") == round_number
            and capture.get("workflow_id") == workflow_id
            and capture.get("route_key") == route_key
            and capture.get("routed_to") == routed_to
            and isinstance(capture.get("measurement"), dict)
            and capture["measurement"].get("fix_loop_rounds") == fix_loop_rounds
            and _canonical_json(
                capture.get("extracted_projection", {}).get(
                    "normalization_context"
                )
            )
            == _canonical_json(normalization_context)
            and _recorded_capture_source_identities(capture) == identities
        ),
        None,
    )


def _capture_replay_result(capture: dict[str, object]) -> dict[str, object]:
    measurement = capture.get("measurement")
    return {
        "ok": True,
        "captured": False,
        "entry_ids": capture["entry_ids"],
        "unsupported_count": capture["unsupported_count"],
        "gate_attempt": (
            measurement.get("gate_attempt")
            if isinstance(measurement, dict)
            else None
        ),
        "transition_occurrence_id": capture["transition_occurrence_id"],
    }


def _completed_capture_replay(
    context: dict[str, object],
    *,
    phase_id: str,
    artifact_path: Path,
    project_root: object,
    round_number: int,
    fix_loop_rounds: object,
    workflow_id: str,
    route_key: str,
    routed_to: str,
    source_eligible: bool,
    source_identities: list[dict[str, str]],
    transition_occurrence_id: str,
) -> dict[str, object]:
    existing = _find_equivalent_capture(
        _read_jsonl(context["paths"]["captures"]),
        run_dir=Path(context["run_dir"]),
        artifact_path=artifact_path,
        phase_id=phase_id,
        round_number=round_number,
        workflow_id=workflow_id,
        route_key=route_key,
        routed_to=routed_to,
        fix_loop_rounds=max(0, _integer_or_zero(fix_loop_rounds)),
        source_eligible=source_eligible,
        source_identities=source_identities,
        transition_occurrence_id=transition_occurrence_id,
        normalization_context={
            "extractor_version": EXTRACTOR_VERSION,
            "project_root": _canonical_project_root(project_root),
        },
    )
    if existing is None:
        raise RuntimeError("execution ledger is finalized")
    return _capture_replay_result(existing)


def _normalize_transition_occurrence_id(
    value: object,
    legacy_identity: dict[str, object],
) -> str:
    if value in (None, ""):
        return _sha256(
            _canonical_json(
                {"schema_version": SCHEMA_VERSION, **legacy_identity}
            )
        )
    if (
        not isinstance(value, str)
        or _COMMITMENT_SHA256.fullmatch(value) is None
    ):
        raise ValueError("invalid execution ledger transition occurrence id")
    return value


def _validate_committed_records(
    *,
    run_dir: Path,
    paths: dict[str, Path],
    config: dict[str, Any],
    workflow: dict[str, Any],
    require_completion: bool,
    allow_pending_completion: bool = False,
    skip_derived: bool = False,
) -> dict[str, object]:
    records_by_sequence: dict[int, tuple[str, dict[str, Any]]] = {}
    records_by_type: dict[str, list[dict[str, Any]]] = {}
    seen_record_ids: set[tuple[str, str]] = set()
    seen_transition_occurrence_ids: set[str] = set()
    gate_capture_count = 0
    for record_type in _COMMITTED_RECORD_TYPES:
        records = _read_jsonl(_committed_record_path(paths, record_type))
        records_by_type[record_type] = records
        previous_sequence = 0
        for record in records:
            sequence = record.get("sequence")
            previous_hash = record.get("previous_content_sha256")
            if (
                record.get("schema_version") != SCHEMA_VERSION
                or record.get("run_id") != config.get("run_id")
                or not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or sequence < 1
                or sequence <= previous_sequence
                or not isinstance(record.get("id"), str)
                or not isinstance(record.get("content_sha256"), str)
                or _COMMITMENT_SHA256.fullmatch(record["content_sha256"]) is None
                or (
                    previous_hash is not None
                    and (
                        not isinstance(previous_hash, str)
                        or _COMMITMENT_SHA256.fullmatch(previous_hash) is None
                    )
                )
            ):
                raise RuntimeError(f"invalid execution ledger {record_type} commitment")
            if _sha256(
                _canonical_json(_without_content_commitment(record))
            ) != record["content_sha256"]:
                raise RuntimeError(
                    f"execution ledger {record_type} content commitment mismatch"
                )
            semantic_payload = _without_content_commitment(
                record, include_chain=False
            )
            semantic_payload.pop("id", None)
            expected_record_id = (
                _sha256(
                    _canonical_json(
                        {
                            "run_id": record.get("run_id"),
                            "event_id": record.get("event_id"),
                        }
                    )
                )
                if record_type == "usage"
                else _sha256(_canonical_json(semantic_payload))
            )
            if record.get("id") != expected_record_id:
                raise RuntimeError(
                    f"execution ledger {record_type} id commitment mismatch"
                )
            if (
                record_type == "usage"
                and record.get("additional_token_scope") != "condition-total"
            ):
                raise RuntimeError(
                    "execution ledger usage additional token scope mismatch"
                )
            if record_type == "usage":
                _validate_usage_evidence(record, run_dir, config)
            if sequence in records_by_sequence:
                raise RuntimeError("duplicate execution ledger event sequence")
            record_key = (record_type, record["id"])
            if record_key in seen_record_ids:
                raise RuntimeError("duplicate execution ledger committed record id")
            seen_record_ids.add(record_key)
            previous_sequence = sequence
            records_by_sequence[sequence] = (record_type, record)
            if record_type == "capture":
                transition_occurrence_id = record.get(
                    "transition_occurrence_id"
                )
                if (
                    not isinstance(transition_occurrence_id, str)
                    or _COMMITMENT_SHA256.fullmatch(
                        transition_occurrence_id
                    )
                    is None
                    or transition_occurrence_id
                    in seen_transition_occurrence_ids
                ):
                    raise RuntimeError(
                        "duplicate or invalid execution ledger transition occurrence id"
                    )
                seen_transition_occurrence_ids.add(
                    transition_occurrence_id
                )
                if record.get("phase") == "gates":
                    gate_capture_count += 1
                _validate_capture_projection(
                    run_dir,
                    config,
                    workflow,
                    record,
                    gate_capture_count if record.get("phase") == "gates" else None,
                )

    events = _read_jsonl(paths["events"])
    if len(events) != len(records_by_sequence):
        raise RuntimeError("execution ledger event index count mismatch")
    previous_content_sha256: str | None = None
    ledger_at_event = (
        None
        if config.get("mode") == "artifacts-only"
        else _empty_ledger(config["run_id"])
    )
    for index, event in enumerate(events):
        sequence = index + 1
        located = records_by_sequence.get(sequence)
        if located is None:
            raise RuntimeError("execution ledger event sequence gap")
        record_type, record = located
        expected_index_base = {
            "schema_version": SCHEMA_VERSION,
            "run_id": config["run_id"],
            "sequence": sequence,
            "record_type": record_type,
            "record_id": record["id"],
            "record_content_sha256": record["content_sha256"],
            "previous_content_sha256": previous_content_sha256,
        }
        expected_event = {
            **expected_index_base,
            "id": _sha256(_canonical_json(expected_index_base)),
        }
        if (
            _canonical_json(event) != _canonical_json(expected_event)
            or record.get("previous_content_sha256") != previous_content_sha256
        ):
            raise RuntimeError("execution ledger event chain mismatch")
        previous_content_sha256 = record["content_sha256"]
        if record_type == "capture" and ledger_at_event is not None:
            projection = record["extracted_projection"]
            ledger_at_event = _merge_ledger_entries(
                ledger_at_event,
                projection["entries"],
                gate_resolutions=projection["gate_resolutions"],
                gate_snapshot_resolution=projection.get("gate_snapshot_resolution"),
                gate_command_fingerprints=projection.get("gate_command_fingerprints", []),
                review_observation=projection.get("review_observation"),
            )
        elif record_type == "injection":
            _validate_injection_evidence(
                record, run_dir, config, workflow, ledger_at_event
            )

    usage = _read_jsonl(paths["usage"])
    run_totals = [record for record in usage if record.get("scope") == "run-total"]
    if len(run_totals) > 1:
        raise RuntimeError("execution ledger has multiple run-total usage events")
    if skip_derived:
        return {
            "completion": None,
            "event_count": len(events),
            "event_head_sha256": previous_content_sha256,
            "records_by_type": records_by_type,
            "metrics": None,
            "replayed_ledger": ledger_at_event,
        }
    completion_present = _sidecar_path_exists(paths["completion"])
    if len(run_totals) == 1 and not completion_present and not allow_pending_completion:
        raise RuntimeError("execution ledger run-total is not finalized")
    if completion_present and len(run_totals) != 1:
        raise RuntimeError("execution ledger completion is missing run-total usage")
    completion = None
    if completion_present:
        completion = _read_json(paths["completion"])
        _validate_completion(paths, config, completion, events, run_totals[0])
    elif require_completion:
        raise RuntimeError("execution ledger completion is required")
    if config.get("mode") != "artifacts-only":
        replayed_ledger = ledger_at_event
        if _canonical_json(replayed_ledger) != _canonical_json(
            _read_json(paths["ledger"])
        ):
            raise RuntimeError("execution ledger semantic replay mismatch")
    stored_metrics = _read_json(paths["metrics"])
    _assert_run_document(
        stored_metrics, config["run_id"], config["mode"], "metrics.json"
    )
    recomputed_metrics = _recompute_metrics(
        {
            "run_dir": run_dir,
            "paths": paths,
            "config": config,
            "workflow": workflow,
            "ledger": replayed_ledger if config.get("mode") != "artifacts-only" else None,
        },
        write=False,
    )
    if _canonical_json(stored_metrics) != _canonical_json(recomputed_metrics):
        raise RuntimeError("execution ledger metrics replay mismatch")
    return {
        "completion": completion,
        "event_count": len(events),
        "event_head_sha256": previous_content_sha256,
        "records_by_type": records_by_type,
        "metrics": recomputed_metrics,
        "replayed_ledger": (
            replayed_ledger if config.get("mode") != "artifacts-only" else None
        ),
    }


def _validate_injection_evidence(
    injection: dict[str, Any],
    run_dir: Path,
    config: dict[str, Any],
    workflow: dict[str, Any],
    ledger_at_event: dict[str, Any] | None,
) -> None:
    evidence_entries = injection.get("evidence_entries")
    evidence_entry_ids = injection.get("evidence_entry_ids")
    normalization_context = injection.get("normalization_context")
    block = injection.get("block")
    if (
        not isinstance(evidence_entries, list)
        or not isinstance(evidence_entry_ids, list)
        or [
            entry.get("id") if isinstance(entry, dict) else None
            for entry in evidence_entries
        ]
        != evidence_entry_ids
        or not isinstance(normalization_context, dict)
        or normalization_context.get("extractor_version") != EXTRACTOR_VERSION
        or not isinstance(normalization_context.get("project_root"), str)
        or _canonical_project_root(normalization_context["project_root"])
        != normalization_context["project_root"]
        or not isinstance(
            normalization_context.get("action_project_root_realpath"), str
        )
        or not os.path.isabs(
            normalization_context["action_project_root_realpath"]
        )
        or _canonical_project_root(
            normalization_context["action_project_root_realpath"]
        )
        != normalization_context["action_project_root_realpath"]
        or not isinstance(block, str)
        or injection.get("block_sha256") != _sha256(block)
        or injection.get("byte_count") != _utf8_byte_length(block)
        or injection.get("exposure_policy_sha256")
        != config.get("exposure_policy_sha256")
        or injection.get("per_exposure_token_cap")
        != config.get("per_exposure_token_cap")
        or _token_proxy(injection.get("byte_count", 0))
        > config.get("per_exposure_token_cap", 0)
        or not isinstance(injection.get("prompt_bytes"), int)
        or isinstance(injection.get("prompt_bytes"), bool)
        or injection["prompt_bytes"] < 0
        or injection.get("line_count") != (len(block.split("\n")) if block else 0)
    ):
        raise RuntimeError("execution ledger injection evidence mismatch")
    phase = next(
        (
            candidate
            for candidate in workflow["phases"]
            if candidate.get("id") == injection.get("phase")
        ),
        None,
    )
    if phase is None:
        raise RuntimeError("execution ledger injection workflow mismatch")
    if phase.get("multi_review") is True and (
        injection.get("injected") is True or block or evidence_entries
    ):
        raise RuntimeError("execution ledger multi-review injection violation")
    selected = _select_prompt_block(
        {
            "run_dir": run_dir,
            "config": config,
            "workflow": workflow,
            "ledger": ledger_at_event,
        },
        phase,
        normalization_context["project_root"],
        injection["round"],
        normalization_context["action_project_root_realpath"],
    )
    expected_exposure = {
        "block": selected["block"],
        "reason": selected["reason"],
        "entry_ids": selected["entry_ids"],
        "evidence_entry_ids": selected["evidence_entry_ids"],
        "evidence_entries": selected["evidence_entries"],
        "injected": bool(selected["block"]),
        "line_count": len(selected["block"].split("\n"))
        if selected["block"]
        else 0,
        "byte_count": _utf8_byte_length(selected["block"]),
        "block_sha256": _sha256(selected["block"]),
        "stale_candidate_count": selected["stale_count"],
        "unsupported_candidate_count": selected["unsupported_count"],
    }
    actual_exposure = {
        key: injection.get(key) for key in expected_exposure
    }
    if _canonical_json(actual_exposure) != _canonical_json(expected_exposure):
        raise RuntimeError("execution ledger injection selection replay mismatch")
    if ledger_at_event is None:
        if evidence_entries or injection.get("injected") is True or block:
            raise RuntimeError("execution ledger artifacts-only injection violation")
        return
    available: dict[str, dict[str, object]] = {}
    for bucket in ("status", "knowledge", "procedural"):
        for entry in ledger_at_event["entries"][bucket]:
            available[entry["id"]] = entry
    for entry in evidence_entries:
        expected = available.get(entry.get("id")) if isinstance(entry, dict) else None
        if expected is None or _canonical_json(expected) != _canonical_json(entry):
            raise RuntimeError("execution ledger injection semantic replay mismatch")
    expected_prompt_ids = (
        [] if injection.get("mode") == "action-self-review" else evidence_entry_ids
    )
    if injection.get("entry_ids") != expected_prompt_ids:
        raise RuntimeError("execution ledger injection exposure mismatch")


def _validate_capture_projection(
    run_dir: Path,
    config: dict[str, Any],
    workflow: dict[str, Any],
    capture: dict[str, Any],
    gate_attempt: int | None,
) -> None:
    projection = capture.get("extracted_projection")
    if (
        not isinstance(projection, dict)
        or not isinstance(projection.get("entries"), list)
        or not isinstance(projection.get("gate_resolutions"), list)
        or not isinstance(projection.get("gate_command_fingerprints"), list)
        or not isinstance(projection.get("routing"), dict)
        or not isinstance(projection.get("normalization_context"), dict)
        or not isinstance(capture.get("transition_occurrence_id"), str)
        or _COMMITMENT_SHA256.fullmatch(
            capture["transition_occurrence_id"]
        )
        is None
        or capture.get("extracted_projection_sha256")
        != _sha256(_canonical_json(projection))
    ):
        raise RuntimeError("execution ledger capture projection mismatch")
    provenance_records = capture.get("source_provenance")
    if not isinstance(provenance_records, list):
        raise RuntimeError("invalid execution ledger capture source provenance")
    sources: list[dict[str, object]] = []
    for provenance in provenance_records:
        if not isinstance(provenance, dict):
            raise RuntimeError("execution ledger capture archive commitment mismatch")
        source = _read_verified_archive(run_dir, config["run_id"], provenance)
        if source is None:
            raise RuntimeError("execution ledger capture archive commitment mismatch")
        sources.append(source)
    normalization_context = projection["normalization_context"]
    project_root = normalization_context.get("project_root")
    if (
        normalization_context.get("extractor_version") != EXTRACTOR_VERSION
        or not isinstance(project_root, str)
        or _canonical_project_root(project_root) != project_root
    ):
        raise RuntimeError("execution ledger normalization context mismatch")
    phase = next(
        (
            candidate
            for candidate in workflow["phases"]
            if candidate.get("id") == capture.get("phase")
        ),
        None,
    )
    if phase is None or capture.get("workflow_id") != config.get("workflow_id"):
        raise RuntimeError("execution ledger capture workflow mismatch")
    routing = _routing_observation(
        workflow, phase, capture.get("route_key"), capture.get("routed_to")
    )
    if capture.get("phase") == "gates":
        if len(sources) != 1:
            raise RuntimeError("invalid execution ledger gate source count")
        extracted = _extract_gate_entries(
            sources[0], project_root, capture["generated_at"], capture["round"]
        )
        if extracted["gate_observation"] is not None:
            extracted["gate_observation"]["attempt"] = gate_attempt
            extracted["gate_observation"]["passed"] = _canonical_gate_passed(
                extracted["gate_observation"],
                routing,
                capture.get("routed_to"),
            )
        gate_snapshot_resolution = (
            {**provenance_records[0], "selector": "/"}
            if extracted["gate_observation"] is not None
            and extracted["gate_observation"].get("snapshot_complete") is True
            else None
        )
        expected_projection = {
            "entries": extracted["entries"],
            "gate_resolutions": extracted["resolutions"],
            "gate_snapshot_resolution": gate_snapshot_resolution,
            "review_observation": None,
            "gate_observation": extracted["gate_observation"],
            "gate_command_fingerprints": extracted["command_fingerprints"],
            "unsupported_count": extracted["unsupported_count"]
            + routing["unsupported_count"],
            "fix_loop_rounds": projection.get("fix_loop_rounds"),
            "routing": routing,
            "normalization_context": normalization_context,
        }
    elif not _is_ledger_source_phase(phase):
        if sources:
            raise RuntimeError("invalid execution ledger routing-only source count")
        expected_projection = {
            "entries": [],
            "gate_resolutions": [],
            "gate_snapshot_resolution": None,
            "review_observation": None,
            "gate_observation": None,
            "gate_command_fingerprints": [],
            "unsupported_count": routing["unsupported_count"],
            "fix_loop_rounds": projection.get("fix_loop_rounds"),
            "routing": routing,
            "normalization_context": normalization_context,
        }
    elif len(sources) == 2:
        summary = sources[0].get("payload")
        if not isinstance(summary, dict):
            raise RuntimeError("invalid execution ledger review summary")
        verdict = (
            capture.get("route_key")
            if capture.get("route_key")
            in {"approve", "request-changes", "blocked"}
            else None
        )
        if verdict is None or provenance_records[1].get("selector") != "/review-artifact":
            raise RuntimeError("invalid execution ledger review artifact binding")
        expected_summary = {
            "schema_version": REVIEW_SUMMARY_SCHEMA_VERSION,
            "run_id": config["run_id"],
            "phase_id": capture["phase"],
            "round": capture["round"],
            "generated_at": capture["generated_at"],
            "artifact_path": provenance_records[1]["original_path"],
            "artifact_sha256": provenance_records[1]["sha256"],
            "artifact_archive_path": provenance_records[1]["archive_path"],
            "verdict": verdict,
            "findings": (
                []
                if verdict == "approve"
                else _parse_review_findings(sources[1]["bytes"], project_root)
            ),
        }
        if _canonical_json(summary) != _canonical_json(expected_summary):
            raise RuntimeError("execution ledger review summary replay mismatch")
        extracted = _extract_review_entries(
            sources[0], project_root, capture["generated_at"], capture["round"]
        )
        summary_complete = summary.get("verdict") == "approve" or bool(
            summary.get("findings")
            if isinstance(summary.get("findings"), list)
            else []
        )
        expected_projection = {
            "entries": extracted["entries"],
            "gate_resolutions": [],
            "gate_snapshot_resolution": None,
            "review_observation": {
                "complete": summary_complete,
                "phase_id": capture["phase"],
                "finding_fingerprints": [
                    entry["payload"]["finding_fingerprint"]
                    for entry in extracted["entries"]
                ],
                "provenance": {
                    **provenance_records[0],
                    "selector": "/review-result",
                },
            },
            "gate_observation": None,
            "gate_command_fingerprints": [],
            "unsupported_count": extracted["unsupported_count"]
            + (0 if summary_complete else 1)
            + routing["unsupported_count"],
            "fix_loop_rounds": projection.get("fix_loop_rounds"),
            "routing": routing,
            "normalization_context": normalization_context,
        }
    elif len(sources) == 0 or (
        len(sources) == 1
        and provenance_records[0].get("selector") == "/unsupported-review-summary"
    ):
        expected_projection = {
            "entries": [],
            "gate_resolutions": [],
            "gate_snapshot_resolution": None,
            "review_observation": None,
            "gate_observation": None,
            "gate_command_fingerprints": [],
            "unsupported_count": 1 + routing["unsupported_count"],
            "fix_loop_rounds": projection.get("fix_loop_rounds"),
            "routing": routing,
            "normalization_context": normalization_context,
        }
    else:
        raise RuntimeError("invalid execution ledger review source count")
    if _canonical_json(expected_projection) != _canonical_json(projection):
        raise RuntimeError("execution ledger archive extraction projection mismatch")
    entry_ids = sorted(entry["id"] for entry in projection["entries"])
    expected_measurement = _capture_measurement(
        projection["entries"],
        projection.get("gate_observation"),
        projection.get("fix_loop_rounds"),
        projection["routing"],
        projection["gate_command_fingerprints"],
    )
    if (
        _canonical_json(entry_ids) != _canonical_json(capture.get("entry_ids"))
        or _canonical_json(expected_measurement)
        != _canonical_json(capture.get("measurement"))
        or capture.get("unsupported_count") != projection.get("unsupported_count")
        or capture.get("expected_target") != routing.get("expected_target")
    ):
        raise RuntimeError("execution ledger capture measurement projection mismatch")


def _finalized_file_commitments(
    paths: dict[str, Path], mode: str
) -> list[dict[str, object]]:
    names = [
        "config",
        "workflow",
        "captures",
        "injections",
        "usage",
        "events",
        "metrics",
    ]
    if mode != "artifacts-only":
        names.append("ledger")
    commitments = []
    for name in names:
        content = _read_safe_sidecar_bytes(paths[name])
        commitments.append(
            {
                "path": paths[name].name,
                "byte_length": len(content),
                "sha256": _sha256(content),
            }
        )
    return sorted(commitments, key=lambda item: str(item["path"]))


def _validate_completion(
    paths: dict[str, Path],
    config: dict[str, Any],
    completion: dict[str, Any],
    events: list[dict[str, Any]],
    run_total: dict[str, Any],
) -> None:
    completed_at = completion.get("completed_at")
    if (
        completion.get("schema_version") != SCHEMA_VERSION
        or completion.get("run_id") != config.get("run_id")
        or completion.get("mode") != config.get("mode")
        or not isinstance(completed_at, str)
        or _normalize_timestamp(completed_at) != completed_at
        or not isinstance(completion.get("content_sha256"), str)
        or _COMMITMENT_SHA256.fullmatch(completion["content_sha256"]) is None
        or _sha256(_canonical_json(_without_content_commitment(completion)))
        != completion["content_sha256"]
    ):
        raise RuntimeError("invalid execution ledger completion commitment")
    event_head_sha256 = events[-1]["record_content_sha256"] if events else None
    if (
        completion.get("event_count") != len(events)
        or completion.get("event_head_sha256") != event_head_sha256
        or not events
        or events[-1].get("record_type") != "usage"
        or events[-1].get("record_id") != run_total.get("id")
    ):
        raise RuntimeError("execution ledger completion event head mismatch")
    expected_files = _finalized_file_commitments(paths, config["mode"])
    if _canonical_json(completion.get("files")) != _canonical_json(expected_files):
        raise RuntimeError("execution ledger completion file commitment mismatch")
    run_dir = paths["root"].parent.parent
    terminal_state = _canonical_terminal_state(
        run_dir, config, _read_json(paths["workflow"])
    )
    if _canonical_json(completion.get("terminal_state")) != _canonical_json(
        terminal_state
    ):
        raise RuntimeError("execution ledger completion terminal state mismatch")


def _sidecar_path_exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _canonical_terminal_state(
    run_dir: Path | str,
    config: dict[str, Any],
    workflow: dict[str, Any],
) -> dict[str, object]:
    resolved_run = Path(run_dir).resolve()
    manifest_path = resolved_run / "manifest.json"
    meta_path = resolved_run / "meta.json"
    has_manifest = _sidecar_path_exists(manifest_path)
    has_meta = _sidecar_path_exists(meta_path)
    if has_manifest == has_meta:
        raise RuntimeError(
            "execution ledger terminal state evidence is missing or ambiguous"
        )
    evidence_path = manifest_path if has_manifest else meta_path
    content = _read_direct_run_file(evidence_path)
    payload = json.loads(content.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("invalid execution ledger terminal state evidence")
    phase_index = payload.get("phase_index")
    if (
        payload.get("run_id") != config.get("run_id")
        or payload.get("workflow") != config.get("workflow_id")
        or not isinstance(phase_index, int)
        or isinstance(phase_index, bool)
        or phase_index < len(workflow["phases"])
    ):
        raise RuntimeError("execution ledger terminal state identity mismatch")
    if has_manifest:
        if payload.get("status") != "complete" or payload.get("phase") != "complete":
            raise RuntimeError("execution ledger Node run is not complete")
        return {
            "runtime": "node",
            "path": "manifest.json",
            "sha256": _sha256(content),
            "run_id": payload["run_id"],
            "workflow_id": payload["workflow"],
            "status": "complete",
            "current_phase": "complete",
            "phase_index": phase_index,
        }
    if payload.get("current_phase") is not None or _sidecar_path_exists(
        resolved_run / "active"
    ):
        raise RuntimeError("execution ledger Python run is not complete")
    return {
        "runtime": "python",
        "path": "meta.json",
        "sha256": _sha256(content),
        "run_id": payload["run_id"],
        "workflow_id": payload["workflow"],
        "status": "complete",
        "current_phase": None,
        "phase_index": phase_index,
    }


def _read_direct_run_file(path: Path) -> bytes:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"unsafe execution ledger terminal state file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError(f"unsafe execution ledger terminal state file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _canonical_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _canonical_value(value[key]) for key in sorted(value, key=lambda item: str(item))}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        if value.is_integer():
            return int(value)
    if hasattr(value, "__dict__"):
        return _canonical_value(vars(value))
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _sha256(value: object) -> str:
    data = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _write_canonical_json(path: Path, payload: object) -> None:
    _write_atomic(path, (_canonical_json(payload) + "\n").encode("utf-8"))


def _write_atomic(path: Path, content: bytes) -> None:
    _ensure_safe_sidecar_parent(path, create=True)
    if path.exists() or path.is_symlink():
        _assert_safe_sidecar_file(path)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _preserve_atomic_bytes(path: Path, content: bytes) -> None:
    _ensure_safe_sidecar_parent(path, create=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            raise RuntimeError(f"execution ledger source hash collision: {path}")
        return
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise RuntimeError(f"execution ledger source hash collision: {path}")
        else:
            os.replace(temp, path)
            _fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _write_immutable_json(path: Path, payload: object) -> None:
    if path.exists():
        if _canonical_json(_read_json(path)) != _canonical_json(payload):
            raise RuntimeError(f"immutable execution ledger config mismatch: {path}")
        return
    _write_canonical_json(path, payload)


def _ensure_jsonl(path: Path) -> None:
    _ensure_safe_sidecar_parent(path, create=True)
    if path.exists() or path.is_symlink():
        _assert_safe_sidecar_file(path)
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError(f"unsafe execution ledger file: {path}")
    finally:
        os.close(descriptor)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(_read_safe_sidecar_bytes(path).decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid execution ledger JSON: {path}")
    return payload


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in _read_safe_sidecar_bytes(path).decode("utf-8").splitlines():
        if not line:
            continue
        parsed = json.loads(line)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("id"), str):
            raise RuntimeError(f"invalid JSONL record: {path}")
        records.append(parsed)
    return records


def _read_safe_sidecar_bytes(path: Path) -> bytes:
    _assert_safe_sidecar_file(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError(f"unsafe execution ledger file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _append_jsonl_record(path: Path, payload: dict[str, object]) -> None:
    current = _read_safe_sidecar_bytes(path)
    separator = b"" if not current or current.endswith(b"\n") else b"\n"
    _write_atomic(
        path,
        current + separator + (_canonical_json(payload) + "\n").encode("utf-8"),
    )


def _acquire_transaction_lock(paths: dict[str, Path]) -> dict[str, object]:
    lock = paths["lock"]
    _ensure_safe_sidecar_parent(lock, create=True)
    for _ in range(3):
        owner = {"pid": os.getpid(), "token": os.urandom(16).hex()}
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock, flags, 0o600)
        except FileExistsError:
            existing = _read_json(lock)
            pid = existing.get("pid")
            token = existing.get("token")
            if (
                not isinstance(pid, int)
                or isinstance(pid, bool)
                or pid < 1
                or not isinstance(token, str)
            ):
                raise RuntimeError("invalid execution ledger transaction lock")
            if _process_is_alive(pid):
                raise RuntimeError("execution ledger transaction is locked")
            _unlink_safe_sidecar_file(lock)
            continue
        try:
            content = (_canonical_json(owner) + "\n").encode("utf-8")
            os.write(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(lock.parent)
        return owner
    raise RuntimeError("execution ledger transaction lock recovery failed")


def _release_transaction_lock(
    paths: dict[str, Path], owner: dict[str, object]
) -> None:
    lock = paths["lock"]
    if not _sidecar_path_exists(lock):
        return
    current = _read_json(lock)
    if current.get("pid") != owner.get("pid") or current.get("token") != owner.get(
        "token"
    ):
        raise RuntimeError("execution ledger transaction lock ownership changed")
    _unlink_safe_sidecar_file(lock)


def _unlink_safe_sidecar_file(path: Path) -> None:
    _assert_safe_sidecar_file(path)
    path.unlink()
    _fsync_directory(path.parent)


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in {22, 45, 95}:
                raise
    finally:
        os.close(descriptor)


def _assert_safe_sidecar_file(path: Path) -> None:
    _ensure_safe_sidecar_parent(path, create=False)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"unsafe execution ledger file: {path}")


def _ensure_safe_sidecar_parent(path: Path, *, create: bool) -> None:
    absolute = path.absolute()
    run_root = _sidecar_run_root(absolute)
    if run_root.resolve() != run_root:
        raise RuntimeError(f"unsafe execution ledger parent: {path}")
    if not run_root.exists() and not run_root.is_symlink():
        if not create:
            raise RuntimeError(f"missing execution ledger parent: {run_root}")
        run_root.mkdir(parents=True)
    if run_root.is_symlink() or not run_root.is_dir():
        raise RuntimeError(f"unsafe execution ledger parent: {run_root}")
    try:
        relative_parent = absolute.parent.relative_to(run_root)
    except ValueError as exc:
        raise RuntimeError(f"execution ledger path escapes run: {path}") from exc
    if not relative_parent.parts:
        raise RuntimeError(f"execution ledger path escapes run: {path}")
    current = run_root
    for part in relative_parent.parts:
        current /= part
        if not current.exists() and not current.is_symlink():
            if not create:
                raise RuntimeError(f"missing execution ledger parent: {current}")
            current.mkdir()
            continue
        if current.is_symlink() or not current.is_dir():
            raise RuntimeError(f"unsafe execution ledger parent: {current}")


def _sidecar_run_root(path: Path) -> Path:
    current = path.parent
    while current.parent != current:
        if current.name == "execution-ledger" and current.parent.name == "artifacts":
            return current.parent.parent
        current = current.parent
    raise RuntimeError(f"execution ledger path escapes sidecar: {path}")


def _assert_run_document(payload: dict[str, Any], run_id: object, mode: str, label: str) -> None:
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("run_id") != run_id
        or payload.get("mode") != mode
    ):
        raise RuntimeError(f"execution ledger {label} identity mismatch")


def _assert_runner_control_snapshot(config: dict[str, Any]) -> None:
    snapshot = config.get("runner_control_snapshot")
    _assert_exact_object_keys(
        snapshot,
        {
            "base_commit",
            "execution_controls_sha256",
            "exposure_policy_sha256",
            "fix_loop_max_rounds",
            "installed_skill_plan_sha256",
            "local_skill_plan_sha256",
            "lore_snapshot_sha256",
            "profile_snapshot_sha256",
            "prompt_controls_sha256",
            "runtime_id",
            "schema_version",
            "task_sha256",
            "workflow_id",
            "workflow_sha256",
        },
        "runner control snapshot",
    )
    assert isinstance(snapshot, dict)
    for field in (
        "execution_controls_sha256",
        "exposure_policy_sha256",
        "installed_skill_plan_sha256",
        "local_skill_plan_sha256",
        "lore_snapshot_sha256",
        "profile_snapshot_sha256",
        "prompt_controls_sha256",
        "task_sha256",
        "workflow_sha256",
    ):
        _require_commitment(snapshot.get(field), f"runner control snapshot {field}")
    _require_identifier(snapshot.get("runtime_id"), "runner control snapshot runtime_id")
    if (
        snapshot.get("schema_version") != SCHEMA_VERSION
        or snapshot.get("task_sha256") != config.get("task_sha256")
        or snapshot.get("workflow_id") != config.get("workflow_id")
        or snapshot.get("workflow_sha256") != config.get("workflow_sha256")
        or snapshot.get("base_commit") != config.get("base_commit")
        or snapshot.get("fix_loop_max_rounds") != config.get("fix_loop_max_rounds")
        or _canonical_json(config.get("exposure_policy"))
        != _canonical_json(EXPOSURE_POLICY)
        or config.get("exposure_policy_sha256")
        != _sha256(_canonical_json(EXPOSURE_POLICY))
        or snapshot.get("exposure_policy_sha256")
        != config.get("exposure_policy_sha256")
        or snapshot.get("execution_controls_sha256")
        != config.get("execution_controls_sha256")
        or config.get("per_exposure_token_cap")
        != EXPOSURE_POLICY["per_exposure_token_cap"]
        or config.get("runner_control_snapshot_sha256")
        != _sha256(_canonical_json(snapshot))
    ):
        raise RuntimeError("execution ledger runner control snapshot mismatch")
    controls = _canonical_execution_controls(config)
    recorded_controls = {key: config.get(key) for key in controls}
    if (
        _canonical_json(controls) != _canonical_json(recorded_controls)
        or config.get("execution_controls_sha256")
        != _sha256(_canonical_json(controls))
    ):
        raise RuntimeError("execution ledger execution controls mismatch")


def _assert_ledger(payload: dict[str, Any], run_id: object) -> None:
    entries = payload.get("entries")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("run_id") != run_id or not isinstance(entries, dict):
        raise RuntimeError("execution ledger identity mismatch")
    for bucket in ("status", "knowledge", "procedural"):
        if not isinstance(entries.get(bucket), list):
            raise RuntimeError(f"invalid execution ledger bucket: {bucket}")


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing execution ledger {label}")
    return value


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _fail_open(error: Exception, **extra: object) -> dict[str, object]:
    return {"ok": False, "error": str(error), **extra}


def _get(value: object, key: str, default: object = None) -> object:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _safe_relative_path(root: Path, target: Path, label: str) -> str:
    try:
        relative = target.resolve().relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{label} is outside the run: {target}") from exc
    if not relative.parts:
        raise RuntimeError(f"{label} is outside the run: {target}")
    return relative.as_posix()


def _resolve_relative_child(root: Path, relative: object, label: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise RuntimeError(f"invalid {label} path")
    resolved = (root / relative).resolve()
    _safe_relative_path(root, resolved, label)
    return resolved


def _safe_extension(path: Path) -> str:
    extension = path.suffix.lower()
    return extension if re.fullmatch(r"\.[a-z0-9]{1,8}", extension) else ".bin"


def _normalize_round(value: object) -> int:
    try:
        round_number = int(value)
    except (TypeError, ValueError):
        return 1
    return min(round_number, MAX_FIX_LOOP_ROUNDS) if round_number >= 1 else 1


def _normalize_timestamp(value: object) -> str:
    match = _STRICT_RFC3339.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("invalid execution ledger timestamp")
    year, month, day, hour, minute, second = map(
        int, (match.group(index) for index in range(1, 7))
    )
    if (
        year < 1
        or month < 1
        or month > 12
        or day < 1
        or hour > 23
        or minute > 59
        or second > 59
    ):
        raise ValueError("invalid execution ledger timestamp")
    zone = match.group(8)
    if zone != "Z":
        zone_hour, zone_minute = map(int, zone[1:].split(":"))
        if zone_hour > 23 or zone_minute > 59:
            raise ValueError("invalid execution ledger timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        normalized = parsed.astimezone(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
    except (OverflowError, ValueError) as exc:
        raise ValueError("invalid execution ledger timestamp") from exc
    if not re.fullmatch(r"(?!0000)\d{4}-.*", normalized):
        raise ValueError("invalid execution ledger timestamp")
    return normalized


def _integer_or_zero(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        converted = float(value)  # Number("1") parity
    except (TypeError, ValueError):
        return 0
    return int(converted) if converted.is_integer() else 0


def _require_nonnegative_integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"invalid execution ledger {label}")
    return value


def _optional_nonnegative_integer(value: object, label: str) -> int | None:
    if value in (None, ""):
        return None
    return _require_nonnegative_integer(value, label)


def _normalize_pricing_rate(value: object, label: str) -> str:
    if isinstance(value, bool):
        raise ValueError(f"invalid execution ledger {label}")
    if isinstance(value, int) and 0 <= value <= 9_007_199_254_740_991:
        text = str(value)
    elif (
        isinstance(value, float)
        and math.isfinite(value)
        and value.is_integer()
        and 0 <= value <= 9_007_199_254_740_991
    ):
        text = str(int(value))
    elif isinstance(value, str):
        text = value
    else:
        raise ValueError(f"invalid execution ledger {label}")
    if re.fullmatch(r"(0|[1-9]\d*)(?:\.\d{1,12})?", text) is None:
        raise ValueError(f"invalid execution ledger {label}")
    if not math.isfinite(float(text)) or float(text) > 9_007_199_254_740_991:
        raise ValueError(f"invalid execution ledger {label}")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _require_commitment(value: object, label: str) -> str:
    if not isinstance(value, str) or _COMMITMENT_SHA256.fullmatch(value) is None:
        raise ValueError(f"invalid execution ledger {label}")
    return value


def _optional_commitment(value: object, label: str) -> str | None:
    if value in (None, ""):
        return None
    return _require_commitment(value, label)


def _canonical_decimal_cost(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(
        r"(0|[1-9]\d*)(?:\.\d{1,12})?", value
    ):
        raise ValueError("invalid execution ledger estimated_cost_usd")
    if "." not in value:
        return value
    integer, fraction = value.split(".", 1)
    normalized_fraction = fraction.rstrip("0")
    return f"{integer}.{normalized_fraction}" if normalized_fraction else integer


def _unicode_scalars(value: object) -> str:
    return "".join(
        "\ufffd" if 0xD800 <= ord(character) <= 0xDFFF else character
        for character in str(value)
    )


def _utf8_byte_length(value: object) -> int:
    return len(_unicode_scalars(value).encode("utf-8"))


def _truncate_utf8(value: object, maximum: int) -> str:
    result: list[str] = []
    size = 0
    for character in _unicode_scalars(value):
        encoded_size = len(character.encode("utf-8"))
        if size + encoded_size > maximum:
            break
        result.append(character)
        size += encoded_size
    return "".join(result)


def _normalize_one_line(value: object, limit: int) -> str:
    normalized = _ANSI_PATTERN.sub("", str(value or ""))
    normalized = _CONTROL_PATTERN.sub(" ", normalized)
    normalized = re.sub(r"[\r\n]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return _truncate_utf8(normalized, limit)


def _normalize_diagnostic(value: object, project_root: object, limit: int) -> str:
    normalized = _ANSI_PATTERN.sub("", str(value or ""))
    normalized = _CONTROL_PATTERN.sub(" ", normalized)
    root = _canonical_project_root(project_root)
    if root:
        normalized = normalized.replace(root, "")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return _truncate_utf8(normalized, limit)


def _canonical_project_root(value: object) -> str:
    if value is None or str(value) == "":
        return ""
    return os.path.abspath(os.path.normpath(str(value)))


def _normalize_argument(value: object, project_root: object) -> str:
    return _normalize_diagnostic(str(value), project_root, 512)


def _first_nonblank_line(value: object, project_root: object) -> str:
    if not isinstance(value, str):
        return ""
    for line in re.split(r"\r?\n", value):
        normalized = _normalize_diagnostic(line, project_root, 120)
        if normalized:
            return normalized
    return ""


def _gate_diagnosis(stderr: object, stdout: object, project_root: object) -> str:
    return _truncate_utf8(
        _first_nonblank_line(stderr, project_root) or _first_nonblank_line(stdout, project_root),
        120,
    )


def _normalize_finding_path(value: object) -> str:
    normalized = str(value).replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized.startswith("/") or ".." in normalized.split("/"):
        return ""
    return _normalize_one_line(normalized, 240)


def _parse_json_bytes(content: bytes) -> object:
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _archive_source(
    run_dir: Path,
    source_path: Path,
    selector: str,
    generated_at: str,
    round_number: int,
    run_id: str,
) -> dict[str, object]:
    resolved_run = run_dir.resolve()
    resolved_source = source_path.resolve()
    original_path = _safe_relative_path(resolved_run, resolved_source, "source artifact")
    content = resolved_source.read_bytes()
    digest = _sha256(content)
    archive = resolved_run / "artifacts" / "execution-ledger" / "sources" / f"{digest}{_safe_extension(resolved_source)}"
    _preserve_atomic_bytes(archive, content)
    return {
        "bytes": content,
        "payload": _parse_json_bytes(content),
        "provenance": {
            "run_id": run_id,
            "round": round_number,
            "generated_at": generated_at,
            "original_path": original_path,
            "archive_path": _safe_relative_path(resolved_run, archive, "archive artifact"),
            "sha256": digest,
            "selector": selector,
            "extractor_version": EXTRACTOR_VERSION,
        },
    }


def _archive_usage_receipt(
    run_dir: Path,
    source_path: Path,
    expected_sha256: object,
    config: dict[str, object],
) -> dict[str, object]:
    run_id = str(config["run_id"])
    if (
        not isinstance(expected_sha256, str)
        or _COMMITMENT_SHA256.fullmatch(expected_sha256) is None
    ):
        raise ValueError("verified usage requires --receipt-sha256")
    resolved_run = run_dir.resolve()
    lexical_source = source_path.absolute()
    metadata = lexical_source.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(
            "provider usage receipt must be a regular non-symlink file"
        )
    resolved_source = lexical_source.resolve()
    original_path = _safe_relative_path(
        resolved_run, resolved_source, "provider usage receipt"
    )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lexical_source, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("provider usage receipt must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read()
    finally:
        os.close(descriptor)
    if lexical_source.resolve() != resolved_source:
        raise RuntimeError("provider usage receipt changed during archival")
    digest = _sha256(content)
    if digest != expected_sha256:
        raise ValueError("provider usage receipt sha256 mismatch")
    payload = _parse_json_bytes(content)
    fields = _usage_fields_from_receipt(payload, config)
    archive = (
        resolved_run
        / "artifacts"
        / "execution-ledger"
        / "sources"
        / f"{digest}.json"
    )
    _preserve_atomic_bytes(archive, content)
    return {
        "payload": payload,
        "fields": fields,
        "provenance": {
            "source_kind": USAGE_RECEIPT_KIND,
            "run_id": run_id,
            "original_path": original_path,
            "archive_path": _safe_relative_path(
                resolved_run, archive, "provider usage receipt archive"
            ),
            "sha256": digest,
            "expected_sha256": expected_sha256,
            "provider": payload["provider"],
            "request_id": payload["request_id"],
            "receipt_id": payload["receipt_id"],
            "attestation_algorithm": fields["attestation_algorithm"],
            "attestation_key_id": fields["provider_attestation_key_id"],
            "attestation_public_key_sha256": fields[
                "provider_attestation_public_key_sha256"
            ],
            "signed_payload_sha256": fields["signed_payload_sha256"],
            "control_receipt_id": fields["control_receipt_id"],
            "runner_control_snapshot_sha256": fields[
                "runner_control_snapshot_sha256"
            ],
            "extractor_version": EXTRACTOR_VERSION,
        },
    }


def _usage_fields_from_receipt(
    payload: object, config: dict[str, object]
) -> dict[str, object]:
    top_keys = {
        "attestation",
        "event_id",
        "generated_at",
        "kind",
        "control_receipt",
        "model_id",
        "phase_id",
        "provider",
        "receipt_id",
        "request_id",
        "round",
        "run_id",
        "schema_version",
        "scope",
        "usage",
    }
    usage_keys = {
        "additional_token_scope",
        "additional_tokens",
        "estimated_cost_usd",
        "input_tokens",
        "latency_ms",
        "output_tokens",
    }
    _assert_exact_object_keys(payload, top_keys, "provider usage receipt")
    usage = payload["usage"]
    _assert_exact_object_keys(usage, usage_keys, "provider usage receipt usage")
    if (
        payload.get("schema_version") != USAGE_RECEIPT_SCHEMA_VERSION
        or payload.get("kind") != USAGE_RECEIPT_KIND
    ):
        raise ValueError("invalid provider usage receipt identity")
    if payload.get("run_id") != config.get("run_id"):
        raise ValueError("provider usage receipt run_id mismatch")
    attestation = _verify_provider_usage_attestation(payload, config)
    control_receipt = _validate_control_receipt(
        payload.get("control_receipt"), config
    )
    provider = _require_identifier(payload.get("provider"), "provider")
    request_id = _optional_identifier(payload.get("request_id"), "request_id")
    receipt_id = _optional_identifier(payload.get("receipt_id"), "receipt_id")
    if request_id is None and receipt_id is None:
        raise ValueError("provider usage receipt requires request_id or receipt_id")
    event_id = _require_string(payload.get("event_id"), "receipt event_id")
    if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", event_id) is None:
        raise ValueError("invalid provider usage receipt event_id")
    scope = _require_string(payload.get("scope"), "receipt scope")
    if scope not in {"phase", "run-total"}:
        raise ValueError("invalid provider usage receipt scope")
    phase_id = payload.get("phase_id")
    usage_round = payload.get("round")
    if scope == "phase":
        phase_id = _require_string(phase_id, "receipt phase_id")
        if (
            not isinstance(usage_round, int)
            or isinstance(usage_round, bool)
            or usage_round < 0
        ):
            raise ValueError("invalid provider usage receipt round")
    elif phase_id is not None or usage_round is not None:
        raise ValueError(
            "run-total provider usage receipt must have null phase_id and round"
        )
    model_id = _require_string(payload.get("model_id"), "receipt model_id")
    if config.get("model_id") is not None and model_id != config.get("model_id"):
        raise ValueError("provider usage receipt model_id mismatch")
    if usage.get("additional_token_scope") != "condition-total":
        raise ValueError("provider usage receipt additional token scope mismatch")
    input_tokens = _require_nonnegative_integer(
        usage.get("input_tokens"), "receipt input_tokens"
    )
    output_tokens = _require_nonnegative_integer(
        usage.get("output_tokens"), "receipt output_tokens"
    )
    additional_tokens = _require_nonnegative_integer(
        usage.get("additional_tokens"), "receipt additional_tokens"
    )
    if additional_tokens > input_tokens:
        raise ValueError(
            "provider usage receipt additional_tokens exceeds input_tokens"
        )
    return {
        "provider": provider,
        "request_id": request_id,
        "receipt_id": receipt_id,
        "event_id": event_id,
        "generated_at": _normalize_timestamp(payload.get("generated_at")),
        "scope": scope,
        "phase_id": phase_id,
        "round": usage_round,
        "model_id": model_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "additional_tokens": additional_tokens,
        "latency_ms": _require_nonnegative_integer(
            usage.get("latency_ms"), "receipt latency_ms"
        ),
        "estimated_cost_usd": _canonical_decimal_cost(
            usage.get("estimated_cost_usd")
        ),
        **attestation,
        **control_receipt,
    }


def _verify_provider_usage_attestation(
    payload: dict[str, object], config: dict[str, object]
) -> dict[str, object]:
    attestation = payload.get("attestation")
    _assert_exact_object_keys(
        attestation,
        {
            "algorithm",
            "key_id",
            "kind",
            "schema_version",
            "signature_base64url",
            "signed_payload_sha256",
        },
        "provider usage attestation",
    )
    assert isinstance(attestation, dict)
    public_key = config.get("provider_attestation_public_key")
    if (
        attestation.get("schema_version") != PROVIDER_ATTESTATION_SCHEMA_VERSION
        or attestation.get("kind") != PROVIDER_ATTESTATION_KIND
        or attestation.get("algorithm") != PROVIDER_ATTESTATION_ALGORITHM
        or attestation.get("key_id") != config.get("provider_attestation_key_id")
        or not isinstance(public_key, dict)
        or config.get("provider_attestation_public_key_sha256")
        != _sha256(_canonical_json(public_key))
    ):
        raise ValueError(
            "provider usage receipt lacks a trusted independent attestation"
        )
    unsigned_payload = {
        key: value for key, value in payload.items() if key != "attestation"
    }
    signed_bytes = _canonical_json(unsigned_payload).encode("utf-8")
    signed_payload_sha256 = hashlib.sha256(signed_bytes).hexdigest()
    if attestation.get("signed_payload_sha256") != signed_payload_sha256:
        raise ValueError("provider usage attestation payload commitment mismatch")
    signature = _decode_base64url(
        attestation.get("signature_base64url"),
        "provider usage attestation signature",
    )
    if not _verify_rsa_sha256_signature(public_key, signed_bytes, signature):
        raise ValueError("provider usage attestation signature verification failed")
    return {
        "attestation_algorithm": PROVIDER_ATTESTATION_ALGORITHM,
        "provider_attestation_key_id": attestation["key_id"],
        "provider_attestation_public_key_sha256": config[
            "provider_attestation_public_key_sha256"
        ],
        "signed_payload_sha256": signed_payload_sha256,
    }


def _validate_control_receipt(
    receipt: object, config: dict[str, object]
) -> dict[str, object]:
    _assert_exact_object_keys(
        receipt,
        {
            "caps_sha256",
            "control_receipt_id",
            "execution_controls_sha256",
            "experiment_id",
            "exposure_policy_sha256",
            "kind",
            "model_id",
            "per_exposure_token_cap",
            "pricing_snapshot_sha256",
            "provider_attestation_key_id",
            "provider_attestation_public_key_sha256",
            "provider_max_retries",
            "provider_retry_policy_sha256",
            "run_id",
            "runner_control_snapshot_sha256",
            "schema_version",
            "system_prompt_sha256",
            "tool_permissions_sha256",
        },
        "execution control receipt",
    )
    assert isinstance(receipt, dict)
    if (
        receipt.get("schema_version") != CONTROL_RECEIPT_SCHEMA_VERSION
        or receipt.get("kind") != CONTROL_RECEIPT_KIND
        or receipt.get("run_id") != config.get("run_id")
        or receipt.get("experiment_id") != config.get("experiment_id")
        or receipt.get("model_id") != config.get("model_id")
        or receipt.get("runner_control_snapshot_sha256")
        != config.get("runner_control_snapshot_sha256")
        or receipt.get("exposure_policy_sha256")
        != config.get("exposure_policy_sha256")
        or receipt.get("per_exposure_token_cap")
        != config.get("per_exposure_token_cap")
        or receipt.get("tool_permissions_sha256")
        != config.get("tool_permissions_sha256")
        or receipt.get("system_prompt_sha256")
        != config.get("system_prompt_sha256")
        or receipt.get("caps_sha256") != config.get("caps_sha256")
        or receipt.get("provider_retry_policy_sha256")
        != config.get("provider_retry_policy_sha256")
        or receipt.get("provider_max_retries") != config.get("provider_max_retries")
        or receipt.get("pricing_snapshot_sha256")
        != config.get("pricing_snapshot_sha256")
        or receipt.get("provider_attestation_key_id")
        != config.get("provider_attestation_key_id")
        or receipt.get("provider_attestation_public_key_sha256")
        != config.get("provider_attestation_public_key_sha256")
        or receipt.get("execution_controls_sha256")
        != config.get("execution_controls_sha256")
    ):
        raise ValueError("execution control receipt does not match the pinned run")
    for field in (
        "runner_control_snapshot_sha256",
        "exposure_policy_sha256",
        "tool_permissions_sha256",
        "system_prompt_sha256",
        "caps_sha256",
        "provider_retry_policy_sha256",
        "pricing_snapshot_sha256",
        "provider_attestation_public_key_sha256",
        "execution_controls_sha256",
    ):
        _require_commitment(receipt.get(field), f"control receipt {field}")
    _require_nonnegative_integer(
        receipt.get("provider_max_retries"),
        "control receipt provider_max_retries",
    )
    _require_identifier(
        receipt.get("provider_attestation_key_id"), "provider_attestation_key_id"
    )
    return {
        "control_receipt_id": _require_identifier(
            receipt.get("control_receipt_id"), "control_receipt_id"
        ),
        "runner_control_snapshot_sha256": receipt[
            "runner_control_snapshot_sha256"
        ],
        "exposure_policy_sha256": receipt["exposure_policy_sha256"],
        "per_exposure_token_cap": receipt["per_exposure_token_cap"],
        "tool_permissions_sha256": receipt["tool_permissions_sha256"],
        "system_prompt_sha256": receipt["system_prompt_sha256"],
        "caps_sha256": receipt["caps_sha256"],
        "provider_retry_policy_sha256": receipt["provider_retry_policy_sha256"],
        "provider_max_retries": receipt["provider_max_retries"],
        "pricing_snapshot_sha256": receipt["pricing_snapshot_sha256"],
        "provider_attestation_key_id": receipt["provider_attestation_key_id"],
        "provider_attestation_public_key_sha256": receipt[
            "provider_attestation_public_key_sha256"
        ],
        "execution_controls_sha256": receipt["execution_controls_sha256"],
    }


def _validate_usage_evidence(
    record: dict[str, Any], run_dir: Path, config: dict[str, Any]
) -> None:
    if record.get("evidence_status") == SUMMARY_USAGE_EVIDENCE:
        if (
            record.get("usage_provenance") is not None
            or record.get("control_evidence_status") != SUMMARY_USAGE_EVIDENCE
            or any(
                record.get(field) is not None
                for field in (
                    "control_receipt_id",
                    "runner_control_snapshot_sha256",
                    "exposure_policy_sha256",
                    "per_exposure_token_cap",
                    "tool_permissions_sha256",
                    "system_prompt_sha256",
                    "caps_sha256",
                    "provider_retry_policy_sha256",
                    "provider_max_retries",
                    "pricing_snapshot_sha256",
                    "provider_attestation_key_id",
                    "provider_attestation_public_key_sha256",
                    "execution_controls_sha256",
                )
            )
        ):
            raise RuntimeError(
                "summary-only usage may not claim provider provenance"
            )
        return
    provenance = record.get("usage_provenance")
    if (
        record.get("evidence_status") != VERIFIED_USAGE_EVIDENCE
        or not isinstance(provenance, dict)
    ):
        raise RuntimeError("invalid execution ledger usage evidence status")
    _assert_exact_object_keys(
        provenance,
        {
            "attestation_algorithm",
            "attestation_key_id",
            "attestation_public_key_sha256",
            "archive_path",
            "control_receipt_id",
            "expected_sha256",
            "extractor_version",
            "original_path",
            "provider",
            "receipt_id",
            "request_id",
            "run_id",
            "runner_control_snapshot_sha256",
            "sha256",
            "signed_payload_sha256",
            "source_kind",
        },
        "provider usage provenance",
    )
    digest = provenance.get("sha256")
    if (
        provenance.get("source_kind") != USAGE_RECEIPT_KIND
        or provenance.get("run_id") != config.get("run_id")
        or provenance.get("extractor_version") != EXTRACTOR_VERSION
        or not isinstance(digest, str)
        or _COMMITMENT_SHA256.fullmatch(digest) is None
        or provenance.get("expected_sha256") != digest
    ):
        raise RuntimeError("invalid provider usage provenance")
    archive = _resolve_relative_child(
        run_dir, provenance.get("archive_path"), "provider usage receipt archive"
    )
    content = _read_safe_sidecar_bytes(archive)
    if _sha256(content) != digest:
        raise RuntimeError("provider usage receipt archive hash mismatch")
    payload = _parse_json_bytes(content)
    fields = _usage_fields_from_receipt(payload, config)
    expected = {
        "event_id": fields["event_id"],
        "run_id": config["run_id"],
        "generated_at": fields["generated_at"],
        "scope": fields["scope"],
        "phase_id": fields["phase_id"],
        "round": fields["round"],
        "model_id": fields["model_id"],
        "additional_token_scope": "condition-total",
        "input_tokens": fields["input_tokens"],
        "output_tokens": fields["output_tokens"],
        "additional_tokens": fields["additional_tokens"],
        "latency_ms": fields["latency_ms"],
        "estimated_cost": fields["estimated_cost_usd"],
        "control_evidence_status": VERIFIED_CONTROL_EVIDENCE,
        "control_receipt_id": fields["control_receipt_id"],
        "runner_control_snapshot_sha256": fields[
            "runner_control_snapshot_sha256"
        ],
        "exposure_policy_sha256": fields["exposure_policy_sha256"],
        "per_exposure_token_cap": fields["per_exposure_token_cap"],
        "tool_permissions_sha256": fields["tool_permissions_sha256"],
        "system_prompt_sha256": fields["system_prompt_sha256"],
        "caps_sha256": fields["caps_sha256"],
        "provider_retry_policy_sha256": fields["provider_retry_policy_sha256"],
        "provider_max_retries": fields["provider_max_retries"],
        "pricing_snapshot_sha256": fields["pricing_snapshot_sha256"],
        "provider_attestation_key_id": fields["provider_attestation_key_id"],
        "provider_attestation_public_key_sha256": fields[
            "provider_attestation_public_key_sha256"
        ],
        "execution_controls_sha256": fields["execution_controls_sha256"],
    }
    for key, value in expected.items():
        if _canonical_json(record.get(key)) != _canonical_json(value):
            raise RuntimeError(
                f"provider usage receipt projection mismatch: {key}"
            )
    if (
        provenance.get("provider") != fields["provider"]
        or provenance.get("request_id") != fields["request_id"]
        or provenance.get("receipt_id") != fields["receipt_id"]
        or provenance.get("control_receipt_id")
        != fields["control_receipt_id"]
        or provenance.get("runner_control_snapshot_sha256")
        != fields["runner_control_snapshot_sha256"]
        or provenance.get("attestation_algorithm")
        != fields["attestation_algorithm"]
        or provenance.get("attestation_key_id")
        != fields["provider_attestation_key_id"]
        or provenance.get("attestation_public_key_sha256")
        != fields["provider_attestation_public_key_sha256"]
        or provenance.get("signed_payload_sha256")
        != fields["signed_payload_sha256"]
    ):
        raise RuntimeError("provider usage receipt identity mismatch")


def _assert_manual_usage_matches_receipt(
    manual: dict[str, object], receipt: dict[str, object]
) -> None:
    mappings = (
        ("event_id", "event_id"),
        ("generated_at", "generated_at"),
        ("scope", "scope"),
        ("phase_id", "phase_id"),
        ("round", "round"),
        ("model_id", "model_id"),
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("additional_tokens", "additional_tokens"),
        ("latency_ms", "latency_ms"),
    )
    for manual_key, receipt_key in mappings:
        value = manual.get(manual_key)
        if value is not None and _canonical_json(value) != _canonical_json(
            receipt[receipt_key]
        ):
            raise ValueError(
                f"manual usage {manual_key} conflicts with provider receipt"
            )
    cost = manual.get("estimated_cost_usd")
    if cost is not None and _canonical_decimal_cost(cost) != receipt.get(
        "estimated_cost_usd"
    ):
        raise ValueError(
            "manual usage estimated_cost_usd conflicts with provider receipt"
        )


def _assert_exact_object_keys(
    value: object, expected: set[str], label: str
) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"invalid {label} fields")


def _require_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(
        r"[A-Za-z0-9._:-]{1,128}", value
    ) is None:
        raise ValueError(f"invalid provider usage receipt {label}")
    return value


def _optional_identifier(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _require_identifier(value, label)


def _optional_control_identifier(value: object, label: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value) is None:
        raise ValueError(f"invalid execution ledger {label}")
    return value


def _decode_base64url(value: object, label: str) -> bytes:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError(f"invalid execution ledger {label}")
    try:
        content = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid execution ledger {label}") from exc
    encoded = base64.urlsafe_b64encode(content).decode("ascii").rstrip("=")
    if not content or not hmac.compare_digest(encoded, value):
        raise ValueError(f"invalid execution ledger {label}")
    return content


def _verify_rsa_sha256_signature(
    public_key: dict[str, object], message: bytes, signature: bytes
) -> bool:
    try:
        modulus_bytes = _decode_base64url(public_key.get("n"), "provider attestation modulus")
        exponent_bytes = _decode_base64url(public_key.get("e"), "provider attestation exponent")
        modulus = int.from_bytes(modulus_bytes, "big")
        exponent = int.from_bytes(exponent_bytes, "big")
        key_size = (modulus.bit_length() + 7) // 8
        if len(signature) != key_size:
            return False
        signature_value = int.from_bytes(signature, "big")
        if signature_value >= modulus:
            return False
        encoded = pow(signature_value, exponent, modulus).to_bytes(key_size, "big")
        digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(message).digest()
        padding_length = key_size - len(digest_info) - 3
        if padding_length < 8:
            return False
        expected = b"\x00\x01" + (b"\xff" * padding_length) + b"\x00" + digest_info
        return hmac.compare_digest(encoded, expected)
    except (ValueError, OverflowError):
        return False


def _make_entry(
    kind: str,
    round_number: int,
    generated_at: str,
    payload: dict[str, object],
    provenance: dict[str, object],
) -> dict[str, object]:
    entry_id = _sha256(
        _canonical_json(
            {
                "kind": kind,
                "round": round_number,
                "generated_at": generated_at,
                "payload": payload,
                "source_sha256": provenance["sha256"],
                "selector": provenance["selector"],
            }
        )
    )
    return {
        "id": entry_id,
        "round": round_number,
        "generated_at": generated_at,
        "payload": payload,
        "provenance": provenance,
    }


def _extract_gate_entries(
    source: dict[str, object],
    project_root: object,
    generated_at: str,
    round_number: int,
) -> dict[str, object]:
    payload = source["payload"]
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list) or not isinstance(payload.get("passed"), bool):
        return {
            "entries": [],
            "resolutions": [],
            "command_fingerprints": [],
            "unsupported_count": 1,
            "gate_observation": None,
        }
    entries: list[dict[str, object]] = []
    resolutions: list[dict[str, object]] = []
    command_fingerprints: list[str] = []
    unsupported = 0
    snapshot_complete = len(payload["results"]) > 0
    required_row_count = 0
    required_rows_passed = True
    for index, result in enumerate(payload["results"]):
        if not isinstance(result, dict):
            unsupported += 1
            snapshot_complete = False
            continue
        gate_id = _normalize_one_line(result.get("gate_id"), 80) if isinstance(result.get("gate_id"), str) else ""
        raw_argv = result.get("argv")
        argv = (
            [_normalize_argument(item, project_root) for item in raw_argv]
            if isinstance(raw_argv, list) and all(isinstance(item, str) for item in raw_argv)
            else None
        )
        if (
            not gate_id
            or argv is None
            or not isinstance(result.get("required"), bool)
        ):
            unsupported += 1
            snapshot_complete = False
            continue
        command_fingerprint = _sha256(_canonical_json({"argv": argv}))
        command_fingerprints.append(command_fingerprint)
        if result["required"] is False:
            continue
        required_row_count += 1
        if not isinstance(result.get("passed"), bool):
            unsupported += 1
            snapshot_complete = False
            required_rows_passed = False
            continue
        if result["passed"] is True:
            resolutions.append(
                {
                    "command_fingerprint": command_fingerprint,
                    "provenance": {
                        **source["provenance"],
                        "selector": f"/results/{index}",
                    },
                }
            )
            continue
        required_rows_passed = False
        diagnosis = _gate_diagnosis(result.get("stderr"), result.get("stdout"), project_root)
        raw_exit = result.get("exit_code")
        exit_code = raw_exit if isinstance(raw_exit, int) and not isinstance(raw_exit, bool) else None
        diagnosis_fingerprint = _sha256(diagnosis)
        failure_fingerprint = _sha256(
            _canonical_json(
                {
                    "command_fingerprint": command_fingerprint,
                    "exit_code": exit_code,
                    "diagnosis_fingerprint": diagnosis_fingerprint,
                }
            )
        )
        entry_payload = {
            "type": "gate-failure",
            "gate_id": gate_id,
            "required": result["required"],
            "exit_code": exit_code,
            "diagnosis": diagnosis,
            "command_fingerprint": command_fingerprint,
            "diagnosis_fingerprint": diagnosis_fingerprint,
            "failure_fingerprint": failure_fingerprint,
        }
        provenance = {**source["provenance"], "selector": f"/results/{index}"}
        entries.append(_make_entry("status", round_number, generated_at, entry_payload, provenance))
    snapshot_complete = snapshot_complete and required_row_count > 0
    required_rows_passed = snapshot_complete and required_rows_passed
    return {
        "entries": entries,
        "resolutions": resolutions,
        "command_fingerprints": sorted(command_fingerprints),
        "unsupported_count": unsupported,
        "gate_observation": {
            "reported_passed": payload["passed"],
            "required_rows_passed": required_rows_passed,
            "snapshot_complete": snapshot_complete,
            "passed": False,
            "attempt": None,
        },
    }


def _normalize_finding(value: object, project_root: object) -> dict[str, object] | None:
    if isinstance(value, str):
        finding = _normalize_diagnostic(value, project_root, MAX_ITEM_BYTES)
        if not finding:
            return None
        return {"type": "review-finding", "finding": finding, "finding_fingerprint": _sha256(finding)}
    if not isinstance(value, dict):
        return None
    expected = ["line", "must_fix", "path", "severity", "statement"]
    if sorted(value) != expected or value.get("must_fix") is not True:
        return None
    line = value.get("line")
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        return None
    if not all(isinstance(value.get(key), str) for key in ("severity", "path", "statement")):
        return None
    severity = _normalize_one_line(value["severity"], 24).lower()
    finding_path = _normalize_finding_path(value["path"])
    statement = _normalize_diagnostic(value["statement"], project_root, MAX_ITEM_BYTES)
    if not severity or not finding_path or not statement:
        return None
    exact = f"{severity}|{finding_path}:{line}|{statement}"
    return {
        "type": "review-finding",
        "must_fix": True,
        "severity": severity,
        "path": finding_path,
        "line": line,
        "statement": statement,
        "finding": exact,
        "finding_fingerprint": _sha256(exact),
    }


def _extract_review_entries(
    source: dict[str, object],
    project_root: object,
    generated_at: str,
    round_number: int,
) -> dict[str, object]:
    payload = source["payload"]
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != REVIEW_SUMMARY_SCHEMA_VERSION
        or not isinstance(payload.get("phase_id"), str)
        or not isinstance(payload.get("artifact_sha256"), str)
        or not isinstance(payload.get("artifact_archive_path"), str)
        or not isinstance(payload.get("findings"), list)
    ):
        return {"entries": [], "unsupported_count": 1}
    if not payload["findings"]:
        return {"entries": [], "unsupported_count": 0}
    entries: list[dict[str, object]] = []
    unsupported = 0
    for index, item in enumerate(payload["findings"]):
        normalized = _normalize_finding(item, project_root)
        if normalized is None:
            unsupported += 1
            continue
        normalized["review_phase_id"] = payload["phase_id"]
        normalized["review_artifact_sha256"] = payload["artifact_sha256"]
        provenance = {**source["provenance"], "selector": f"/findings/{index}"}
        entries.append(_make_entry("knowledge", round_number, generated_at, normalized, provenance))
    return {"entries": entries, "unsupported_count": unsupported}


def _is_ledger_source_phase(phase: object) -> bool:
    phase_id = str(_get(phase, "id", "") or "")
    return bool(
        phase_id == "gates"
        or _get(phase, "multi_review", False) is True
        or phase_id in REVIEW_PHASES
    )


def _routing_observation(
    workflow: object,
    phase: object,
    route_key: object,
    routed_to: object,
) -> dict[str, object]:
    routes = _get(phase, "routes")
    if not isinstance(routes, dict):
        key = str(route_key or "")
        phases = workflow.get("phases") if isinstance(workflow, dict) else None
        phase_id = str(_get(phase, "id", "") or "")
        phase_index = next(
            (
                index
                for index, candidate in enumerate(phases or [])
                if isinstance(candidate, dict) and candidate.get("id") == phase_id
            ),
            None,
        )
        if key != "sequential" or phase_index is None:
            return {"expected_target": None, "violation": False, "unsupported_count": 1}
        expected = (
            phases[phase_index + 1]["id"]
            if phase_index + 1 < len(phases)
            else "complete"
        )
        return {
            "expected_target": expected,
            "violation": expected != str(routed_to or ""),
            "unsupported_count": 0,
        }
    key = str(route_key or "")
    expected = routes.get(key, routes.get("default"))
    if not isinstance(expected, str) or not expected or expected == "block":
        return {"expected_target": None, "violation": False, "unsupported_count": 1}
    return {
        "expected_target": expected,
        "violation": expected != str(routed_to or ""),
        "unsupported_count": 0,
    }


def _canonical_gate_passed(
    gate_observation: object,
    routing: object,
    routed_to: object,
) -> bool:
    if not isinstance(gate_observation, dict) or not isinstance(routing, dict):
        return False
    target = str(routed_to or "")
    return bool(
        gate_observation.get("reported_passed") is True
        and gate_observation.get("required_rows_passed") is True
        and gate_observation.get("snapshot_complete") is True
        and routing.get("violation") is False
        and isinstance(routing.get("expected_target"), str)
        and routing.get("expected_target") == target
        and target not in FIX_ROUTE_TARGETS
    )


def _capture_measurement(
    entries: list[dict[str, object]],
    gate_observation: dict[str, object] | None,
    fix_loop_rounds: object,
    routing: dict[str, object],
    command_fingerprints: Iterable[str] = (),
) -> dict[str, object]:
    status = [entry for entry in entries if entry["payload"]["type"] == "gate-failure"]
    knowledge = [entry for entry in entries if entry["payload"]["type"] == "review-finding"]
    return {
        "command_fingerprints": sorted(command_fingerprints),
        "failure_fingerprints": sorted(
            {entry["payload"]["failure_fingerprint"] for entry in status}
        ),
        "diagnosis_fingerprints": sorted(
            {entry["payload"]["diagnosis_fingerprint"] for entry in status}
        ),
        "finding_fingerprints": sorted(
            {entry["payload"]["finding_fingerprint"] for entry in knowledge}
        ),
        "gate_passed": gate_observation["passed"] if gate_observation else None,
        "gate_attempt": gate_observation["attempt"] if gate_observation else None,
        "fix_loop_rounds": max(0, _integer_or_zero(fix_loop_rounds)),
        "canonical_routing_violation": routing["violation"],
    }


def _entry_sort_key(entry: dict[str, object]) -> tuple[int, str, str]:
    return int(entry["round"]), str(entry["generated_at"]), str(entry["id"])


def _build_procedural_entries(ledger: dict[str, Any]) -> list[dict[str, object]]:
    groups: dict[str, dict[str, object]] = {}

    def collect(entry: dict[str, object], fingerprint: object, repeat_kind: str, template: str) -> None:
        if not isinstance(fingerprint, str) or not fingerprint:
            return
        group = groups.setdefault(
            fingerprint,
            {"entries": [], "repeat_kind": repeat_kind, "template": template},
        )
        group["entries"].append(entry)

    for entry in ledger["entries"]["status"]:
        if entry.get("resolution"):
            continue
        collect(
            entry,
            entry["payload"].get("failure_fingerprint"),
            "gate-failure-repeat",
            "Before retrying, change the action that produced this repeated gate failure.",
        )
    for entry in ledger["entries"]["knowledge"]:
        if entry.get("resolution"):
            continue
        collect(
            entry,
            entry["payload"].get("finding_fingerprint"),
            "review-finding-repeat",
            "Before editing, address this repeated verified review finding.",
        )
    procedural: list[dict[str, object]] = []
    for fingerprint, group in groups.items():
        entries = group["entries"]
        occurrences = {
            _canonical_json(
                {
                    "round": entry["round"],
                    "generated_at": entry["generated_at"],
                    "source_sha256": entry["provenance"]["sha256"],
                }
            )
            for entry in entries
        }
        if len(occurrences) < 2:
            continue
        latest = sorted(entries, key=_entry_sort_key)[-1]
        payload = {
            "type": group["repeat_kind"],
            "fingerprint": fingerprint,
            "occurrences": len(occurrences),
            "template": group["template"],
        }
        provenance = {**latest["provenance"], "selector": f"/repeat/{fingerprint}"}
        procedural.append(
            _make_entry("procedural", latest["round"], latest["generated_at"], payload, provenance)
        )
    return sorted(procedural, key=_entry_sort_key)


def _merge_ledger_entries(
    ledger: dict[str, Any],
    additions: list[dict[str, object]],
    *,
    gate_resolutions: list[dict[str, object]] | None = None,
    gate_snapshot_resolution: dict[str, object] | None = None,
    gate_command_fingerprints: list[str] | None = None,
    review_observation: dict[str, object] | None = None,
) -> dict[str, Any]:
    next_ledger = json.loads(_canonical_json(ledger))
    for entry in additions:
        bucket = "status" if entry["payload"]["type"] == "gate-failure" else "knowledge"
        if not any(existing["id"] == entry["id"] for existing in next_ledger["entries"][bucket]):
            next_ledger["entries"][bucket].append(entry)
    for bucket in ("status", "knowledge"):
        next_ledger["entries"][bucket].sort(key=_entry_sort_key)
    resolved_commands = {
        item["command_fingerprint"]: item["provenance"]
        for item in (gate_resolutions or [])
    }
    snapshot_commands = set(gate_command_fingerprints or [])
    for entry in next_ledger["entries"]["status"]:
        provenance = resolved_commands.get(entry["payload"].get("command_fingerprint"))
        if not entry.get("resolution") and provenance is not None:
            entry["resolution"] = _resolution_record("gate-success", provenance)
        elif (
            not entry.get("resolution")
            and gate_snapshot_resolution is not None
            and entry["payload"].get("command_fingerprint") not in snapshot_commands
        ):
            entry["resolution"] = _resolution_record(
                "gate-snapshot-absent",
                gate_snapshot_resolution,
            )
    if review_observation and review_observation.get("complete") is True:
        current = set(review_observation.get("finding_fingerprints", []))
        for entry in next_ledger["entries"]["knowledge"]:
            if (
                not entry.get("resolution")
                and entry["payload"].get("review_phase_id") == review_observation.get("phase_id")
                and entry["payload"].get("finding_fingerprint") not in current
            ):
                entry["resolution"] = _resolution_record(
                    "review-result",
                    review_observation["provenance"],
                )
    next_ledger["entries"]["procedural"] = _build_procedural_entries(next_ledger)
    return next_ledger


def _resolution_record(kind: str, provenance: dict[str, object]) -> dict[str, object]:
    return {
        "kind": kind,
        "round": provenance["round"],
        "generated_at": provenance["generated_at"],
        "provenance": provenance,
    }


def _create_canonical_review_summary(
    *,
    run_dir: Path,
    run_id: str,
    phase_id: str,
    artifact_path: Path,
    route_key: object,
    project_root: object,
    generated_at: str,
    round_number: int,
) -> dict[str, object] | None:
    if not artifact_path.exists() or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", phase_id):
        return None
    verdict = str(route_key) if route_key in {"approve", "request-changes", "blocked"} else None
    if verdict is None:
        return None
    artifact = _archive_source(
        run_dir,
        artifact_path,
        "/review-artifact",
        generated_at,
        round_number,
        run_id,
    )
    findings = (
        []
        if verdict == "approve"
        else _parse_review_findings(artifact["bytes"], project_root)
    )
    payload = {
        "schema_version": REVIEW_SUMMARY_SCHEMA_VERSION,
        "run_id": run_id,
        "phase_id": phase_id,
        "round": round_number,
        "generated_at": generated_at,
        "artifact_path": artifact["provenance"]["original_path"],
        "artifact_sha256": artifact["provenance"]["sha256"],
        "artifact_archive_path": artifact["provenance"]["archive_path"],
        "verdict": verdict,
        "findings": findings,
    }
    summary_id = _sha256(_canonical_json(payload))
    summary_path = (
        run_dir.resolve()
        / "artifacts"
        / "execution-ledger"
        / "review-summaries"
        / f"{summary_id}.json"
    )
    _write_immutable_json(summary_path, payload)
    return {
        "path": summary_path,
        "artifact_provenance": artifact["provenance"],
        "complete": verdict == "approve" or bool(findings),
        "unsupported_count": 0 if verdict == "approve" or findings else 1,
    }


def _parse_review_findings(content: bytes, project_root: object) -> list[str]:
    text = content.decode("utf-8")
    findings: list[str] = []
    in_findings = False
    in_reviewer = False
    in_code_fence = False
    for line in re.split(r"\r?\n", text):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue
        heading = re.fullmatch(r"#{1,6}\s+(.+)", stripped)
        if heading is not None:
            title = heading.group(1).strip()
            in_findings = re.search(
                r"\bfinding(?:s)?\b", title, re.IGNORECASE
            ) is not None
            in_reviewer = re.match(r"^reviewer(?:\s|$)", title, re.IGNORECASE) is not None
            continue
        if (not in_findings and not in_reviewer) or not stripped.startswith("- "):
            continue
        finding = _normalize_diagnostic(stripped[2:], project_root, MAX_ITEM_BYTES)
        if finding and finding.lower() not in {"none", "n/a", "없음"}:
            findings.append(finding)
    return sorted(set(findings))


def _capture_execution_state_impl(**kwargs: object) -> dict[str, object]:
    try:
        if kwargs.get("experiment_enabled", False) is not True:
            return {"ok": True, "enabled": False, "captured": False}
        phase = kwargs.get("phase")
        phase_id = str(_get(phase, "id", "") or "")
        run_dir = Path(str(kwargs["run_dir"]))
        run_id = kwargs["run_id"]
        mode = kwargs["mode"]
        artifact_path = Path(str(kwargs["artifact_path"]))
        project_root = kwargs.get("project_root")
        normalized_round = _normalize_round(kwargs.get("round", 1))
        generated_at = _normalize_timestamp(
            kwargs.get("generated_at") or datetime.now(timezone.utc).isoformat()
        )
        fix_loop_rounds = kwargs.get("fix_loop_rounds", 0)
        workflow_id = kwargs.get("workflow_id")
        route_key = kwargs.get("route_key")
        routed_to = kwargs.get("routed_to")
        context = _load_context(run_dir, run_id, mode)
        config = context["config"]
        paths = context["paths"]
        snapshot_phase = next(
            (
                candidate
                for candidate in context["workflow"]["phases"]
                if candidate.get("id") == phase_id
            ),
            None,
        )
        supplied_routes = _get(phase, "routes")
        routes_match = (
            supplied_routes is None
            if snapshot_phase is not None and snapshot_phase.get("routes") is None
            else isinstance(supplied_routes, dict)
            and all(
                snapshot_phase.get("routes", {}).get(key) == target
                for key, target in supplied_routes.items()
            )
            if snapshot_phase is not None
            else False
        )
        if (
            snapshot_phase is None
            or snapshot_phase.get("multi_review")
            is not (_get(phase, "multi_review", False) is True)
            or not routes_match
        ):
            raise RuntimeError(
                f"execution ledger phase does not match workflow snapshot: {phase_id}"
            )
        source_eligible = _is_ledger_source_phase(snapshot_phase)
        normalized_workflow_id = str(workflow_id or config["workflow_id"])
        normalized_route_key = str(route_key or "")
        normalized_routed_to = str(routed_to or "")
        normalized_fix_loop_rounds = max(0, _integer_or_zero(fix_loop_rounds))
        normalization_context = {
            "extractor_version": EXTRACTOR_VERSION,
            "project_root": _canonical_project_root(project_root),
        }
        source_identities = _capture_source_identities(
            run_dir,
            artifact_path,
            phase_id,
            normalized_route_key,
            source_eligible,
        )
        normalized_transition_occurrence_id = (
            _normalize_transition_occurrence_id(
                kwargs.get("transition_occurrence_id"),
                {
                    "runId": config["run_id"],
                    "workflowId": normalized_workflow_id,
                    "phaseId": phase_id,
                    "round": normalized_round,
                    "routeKey": normalized_route_key,
                    "routedTo": normalized_routed_to,
                    "fixLoopRounds": normalized_fix_loop_rounds,
                    "sourceIdentities": source_identities,
                },
            )
        )
        if context.get("completion") is not None:
            return _completed_capture_replay(
                context,
                phase_id=phase_id,
                artifact_path=artifact_path,
                project_root=project_root,
                round_number=normalized_round,
                fix_loop_rounds=fix_loop_rounds,
                workflow_id=normalized_workflow_id,
                route_key=normalized_route_key,
                routed_to=normalized_routed_to,
                source_eligible=source_eligible,
                source_identities=source_identities,
                transition_occurrence_id=(
                    normalized_transition_occurrence_id
                ),
            )
        prior_captures = _read_jsonl(paths["captures"])
        equivalent_capture = _find_equivalent_capture(
            prior_captures,
            run_dir=run_dir,
            artifact_path=artifact_path,
            phase_id=phase_id,
            round_number=normalized_round,
            workflow_id=normalized_workflow_id,
            route_key=normalized_route_key,
            routed_to=normalized_routed_to,
            fix_loop_rounds=normalized_fix_loop_rounds,
            normalization_context=normalization_context,
            source_eligible=source_eligible,
            source_identities=source_identities,
            transition_occurrence_id=(
                normalized_transition_occurrence_id
            ),
        )
        if equivalent_capture is not None:
            return _capture_replay_result(equivalent_capture)
        source_records: list[dict[str, object]] = []
        extracted_entries: list[dict[str, object]] = []
        gate_resolutions: list[dict[str, object]] = []
        gate_snapshot_resolution = None
        gate_source_provenance = None
        gate_command_fingerprints: list[str] = []
        unsupported_count = 0
        gate_observation = None
        review_observation = None

        if phase_id == "gates":
            source = _archive_source(run_dir, artifact_path, "/", generated_at, normalized_round, config["run_id"])
            gate_source_provenance = source["provenance"]
            source_records.append(source["provenance"])
            extracted = _extract_gate_entries(source, project_root, generated_at, normalized_round)
            extracted_entries.extend(extracted["entries"])
            gate_resolutions.extend(extracted["resolutions"])
            gate_command_fingerprints.extend(extracted["command_fingerprints"])
            unsupported_count += extracted["unsupported_count"]
            gate_observation = extracted["gate_observation"]
        elif _get(phase, "multi_review", False) is True or phase_id in REVIEW_PHASES:
            summary = _create_canonical_review_summary(
                run_dir=run_dir,
                run_id=config["run_id"],
                phase_id=phase_id,
                artifact_path=artifact_path,
                route_key=route_key,
                project_root=project_root,
                generated_at=generated_at,
                round_number=normalized_round,
            )
            if summary is not None:
                source = _archive_source(
                    run_dir,
                    summary["path"],
                    "/",
                    generated_at,
                    normalized_round,
                    config["run_id"],
                )
                source_records.append(source["provenance"])
                extracted = _extract_review_entries(source, project_root, generated_at, normalized_round)
                extracted_entries.extend(extracted["entries"])
                unsupported_count += (
                    extracted["unsupported_count"] + summary["unsupported_count"]
                )
                source_records.append(summary["artifact_provenance"])
                review_observation = {
                    "complete": summary["complete"],
                    "phase_id": phase_id,
                    "finding_fingerprints": [
                        entry["payload"]["finding_fingerprint"]
                        for entry in extracted["entries"]
                    ],
                    "provenance": {
                        **source["provenance"],
                        "selector": "/review-result",
                    },
                }
            else:
                if artifact_path.exists():
                    source = _archive_source(
                        run_dir,
                        artifact_path,
                        "/unsupported-review-summary",
                        generated_at,
                        normalized_round,
                        config["run_id"],
                    )
                    source_records.append(source["provenance"])
                unsupported_count += 1

        if gate_observation is not None:
            gate_observation["attempt"] = (
                sum(
                    1
                    for capture in prior_captures
                    if capture.get("phase") == "gates"
                )
                + 1
            )
        routing = _routing_observation(
            context["workflow"],
            snapshot_phase,
            normalized_route_key,
            normalized_routed_to,
        )
        unsupported_count += routing["unsupported_count"]
        if gate_observation is not None:
            gate_observation["passed"] = _canonical_gate_passed(
                gate_observation,
                routing,
                normalized_routed_to,
            )
            if (
                gate_observation.get("snapshot_complete") is True
                and isinstance(gate_source_provenance, dict)
            ):
                gate_snapshot_resolution = {
                    **gate_source_provenance,
                    "selector": "/",
                }
        entry_ids = sorted(entry["id"] for entry in extracted_entries)
        extracted_projection = {
            "entries": extracted_entries,
            "gate_resolutions": gate_resolutions,
            "gate_snapshot_resolution": gate_snapshot_resolution,
            "review_observation": review_observation,
            "gate_observation": gate_observation,
            "gate_command_fingerprints": gate_command_fingerprints,
            "unsupported_count": unsupported_count,
            "fix_loop_rounds": normalized_fix_loop_rounds,
            "routing": routing,
            "normalization_context": normalization_context,
        }
        capture_base = {
            "schema_version": SCHEMA_VERSION,
            "run_id": config["run_id"],
            "round": normalized_round,
            "generated_at": generated_at,
            "phase": phase_id,
            "source_provenance": source_records,
            "entry_ids": entry_ids,
            "unsupported_count": unsupported_count,
            "measurement": _capture_measurement(
                extracted_entries,
                gate_observation,
                fix_loop_rounds,
                routing,
                gate_command_fingerprints,
            ),
            "workflow_id": normalized_workflow_id,
            "route_key": normalized_route_key,
            "routed_to": normalized_routed_to,
            "expected_target": routing["expected_target"],
            "transition_occurrence_id": (
                normalized_transition_occurrence_id
            ),
            "extracted_projection": extracted_projection,
            "extracted_projection_sha256": _sha256(
                _canonical_json(extracted_projection)
            ),
        }
        capture_payload = {
            **capture_base,
            "id": _sha256(_canonical_json(capture_base)),
        }
        appended = _append_committed_record(
            context, "capture", capture_payload, strict=True
        )
        return {
            "ok": True,
            "captured": appended,
            "entry_ids": entry_ids,
            "unsupported_count": unsupported_count,
            "gate_attempt": (
                gate_observation.get("attempt")
                if isinstance(gate_observation, dict)
                else None
            ),
            "transition_occurrence_id": (
                normalized_transition_occurrence_id
            ),
        }
    except Exception as exc:
        return _fail_open(exc)


def _verify_entry(
    run_dir: Path,
    run_id: str,
    entry: object,
    project_root: object,
) -> str:
    try:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("id"), str)
            or not isinstance(entry.get("round"), int)
            or isinstance(entry.get("round"), bool)
            or not isinstance(entry.get("generated_at"), str)
            or not isinstance(entry.get("payload"), dict)
            or not isinstance(entry.get("provenance"), dict)
        ):
            return "unsupported"
        source = _read_verified_archive(run_dir, run_id, entry["provenance"])
        if source is None:
            return "stale"
        if entry["payload"].get("type") == "gate-failure":
            candidates = _extract_gate_entries(
                source,
                project_root,
                entry["generated_at"],
                entry["round"],
            )["entries"]
        elif entry["payload"].get("type") == "review-finding":
            if not _verify_review_artifact_binding(run_dir, source.get("payload")):
                return "stale"
            candidates = _extract_review_entries(
                source,
                project_root,
                entry["generated_at"],
                entry["round"],
            )["entries"]
        else:
            return "unsupported"
        expected = next(
            (
                candidate
                for candidate in candidates
                if candidate["provenance"]["selector"]
                == entry["provenance"].get("selector")
            ),
            None,
        )
        if expected is None:
            return "unsupported"
        base_entry = dict(entry)
        base_entry.pop("resolution", None)
        if _canonical_json(base_entry) != _canonical_json(expected):
            return "unsupported"
        if entry.get("resolution") and not _verify_resolution(
            run_dir,
            run_id,
            entry,
            project_root,
        ):
            return "unsupported"
        return "verified"
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return "unsupported"


def _verify_recorded_entry_integrity(run_dir: Path, run_id: str, entry: object) -> str:
    try:
        if not isinstance(entry, dict) or not isinstance(entry.get("provenance"), dict):
            return "unsupported"
        if _read_verified_archive(run_dir, run_id, entry["provenance"]) is None:
            return "stale"
        resolution = entry.get("resolution")
        if resolution:
            if not isinstance(resolution, dict) or not isinstance(resolution.get("provenance"), dict):
                return "unsupported"
            if _read_verified_archive(run_dir, run_id, resolution["provenance"]) is None:
                return "stale"
        return "verified"
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return "unsupported"


def _read_verified_archive(
    run_dir: Path,
    run_id: str,
    provenance: dict[str, object],
) -> dict[str, object] | None:
    expected_keys = [
        "archive_path",
        "extractor_version",
        "generated_at",
        "original_path",
        "round",
        "run_id",
        "selector",
        "sha256",
    ]
    if (
        sorted(provenance) != expected_keys
        or provenance.get("run_id") != run_id
        or provenance.get("extractor_version") != EXTRACTOR_VERSION
        or not isinstance(provenance.get("round"), int)
        or isinstance(provenance.get("round"), bool)
        or not isinstance(provenance.get("generated_at"), str)
        or not isinstance(provenance.get("original_path"), str)
        or not isinstance(provenance.get("archive_path"), str)
        or not isinstance(provenance.get("selector"), str)
        or not isinstance(provenance.get("sha256"), str)
        or re.fullmatch(r"[a-f0-9]{64}", provenance["sha256"]) is None
    ):
        return None
    archive = _resolve_relative_child(run_dir, provenance["archive_path"], "ledger archive")
    if not archive.exists() or archive.is_symlink() or not archive.is_file():
        return None
    content = _read_safe_sidecar_bytes(archive)
    if _sha256(content) != provenance["sha256"]:
        return None
    return {
        "bytes": content,
        "payload": _parse_json_bytes(content),
        "provenance": {**provenance, "selector": "/"},
    }


def _verify_review_artifact_binding(run_dir: Path, payload: object) -> bool:
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("artifact_archive_path"), str)
        or not isinstance(payload.get("artifact_sha256"), str)
        or re.fullmatch(r"[a-f0-9]{64}", payload["artifact_sha256"]) is None
    ):
        return False
    artifact = _resolve_relative_child(
        run_dir,
        payload["artifact_archive_path"],
        "review artifact archive",
    )
    return artifact.exists() and _sha256(_read_safe_sidecar_bytes(artifact)) == payload["artifact_sha256"]


def _verify_resolution(
    run_dir: Path,
    run_id: str,
    entry: dict[str, object],
    project_root: object,
) -> bool:
    resolution = entry.get("resolution")
    if (
        not isinstance(resolution, dict)
        or resolution.get("kind")
        not in {"gate-success", "gate-snapshot-absent", "review-result"}
        or not isinstance(resolution.get("round"), int)
        or isinstance(resolution.get("round"), bool)
        or not isinstance(resolution.get("generated_at"), str)
        or not isinstance(resolution.get("provenance"), dict)
        or resolution["round"] != resolution["provenance"].get("round")
        or resolution["generated_at"] != resolution["provenance"].get("generated_at")
    ):
        return False
    source = _read_verified_archive(run_dir, run_id, resolution["provenance"])
    if source is None:
        return False
    if resolution["kind"] == "gate-snapshot-absent":
        extracted = _extract_gate_entries(
            source,
            project_root,
            resolution["generated_at"],
            resolution["round"],
        )
        return bool(
            resolution["provenance"].get("selector") == "/"
            and isinstance(extracted.get("gate_observation"), dict)
            and extracted["gate_observation"].get("snapshot_complete") is True
            and entry["payload"].get("command_fingerprint")
            not in extracted["command_fingerprints"]
        )
    if resolution["kind"] == "gate-success":
        resolutions = _extract_gate_entries(
            source,
            project_root,
            resolution["generated_at"],
            resolution["round"],
        )["resolutions"]
        return any(
            candidate["command_fingerprint"]
            == entry["payload"].get("command_fingerprint")
            and candidate["provenance"]["selector"]
            == resolution["provenance"].get("selector")
            for candidate in resolutions
        )
    if not _verify_review_artifact_binding(run_dir, source.get("payload")):
        return False
    payload = source["payload"]
    return (
        payload.get("phase_id") == entry["payload"].get("review_phase_id")
        and all(
            (_normalize_finding(finding, project_root) or {}).get("finding_fingerprint")
            != entry["payload"].get("finding_fingerprint")
            for finding in payload.get("findings", [])
        )
    )


def _prompt_line_for_entry(entry: dict[str, object]) -> str:
    payload = entry["payload"]
    if payload["type"] == "gate-failure":
        exit_code = "unknown" if payload["exit_code"] is None else payload["exit_code"]
        return _truncate_utf8(
            f"- Gate {payload['gate_id']} exit {exit_code}: {payload['diagnosis']}",
            MAX_ITEM_BYTES,
        )
    if payload["type"] == "review-finding":
        if payload.get("path"):
            return _truncate_utf8(
                f"- Review {payload['severity']} {payload['path']}:{payload['line']}: {payload['statement']}",
                MAX_ITEM_BYTES,
            )
        return _truncate_utf8(f"- Review: {payload['finding']}", MAX_ITEM_BYTES)
    return _truncate_utf8(f"- {payload['template']}", MAX_ITEM_BYTES)


def _bounded_block(header: str, item_lines: Iterable[str]) -> str:
    lines = [header]
    for raw_line in item_lines:
        if len(lines) >= MAX_BLOCK_LINES:
            break
        line = _truncate_utf8(_normalize_one_line(raw_line, MAX_ITEM_BYTES), MAX_ITEM_BYTES)
        if not line:
            continue
        candidate = "\n".join([*lines, line])
        if _utf8_byte_length(candidate) > MAX_BLOCK_BYTES:
            break
        lines.append(line)
    return "\n".join(lines) if len(lines) > 1 else ""


def _empty_selection(reason: str) -> dict[str, object]:
    return {
        "block": "",
        "entry_ids": [],
        "evidence_entry_ids": [],
        "evidence_entries": [],
        "stale_count": 0,
        "unsupported_count": 0,
        "reason": reason,
    }


def _select_prompt_block(
    context: dict[str, object],
    phase: object,
    project_root: object,
    round_number: int,
    action_project_root: str | None = None,
) -> dict[str, object]:
    if action_project_root is None:
        action_project_root = str(Path(str(project_root)).resolve())
    phase_id = str(_get(phase, "id", "") or "")
    workflow = context.get("workflow")
    pinned_phase = (
        next(
            (
                candidate
                for candidate in workflow["phases"]
                if candidate.get("id") == phase_id
            ),
            None,
        )
        if isinstance(workflow, dict)
        else None
    )
    if (
        phase_id == "multi-review"
        or _get(phase, "multi_review", False) is True
        or (isinstance(pinned_phase, dict) and pinned_phase.get("multi_review") is True)
    ):
        return _empty_selection("multi-review")
    if isinstance(workflow, dict) and pinned_phase is None:
        return _empty_selection("ineligible-phase")
    config = context["config"]
    ledger = context["ledger"]
    if config["mode"] == "artifacts-only" or not isinstance(ledger, dict):
        return _empty_selection("artifacts-only")
    if phase_id not in ELIGIBLE_PHASES:
        return _empty_selection("ineligible-phase")
    verified: dict[str, list[dict[str, object]]] = {"status": [], "knowledge": []}
    stale_count = 0
    unsupported_count = 0
    for bucket in ("status", "knowledge"):
        for entry in ledger["entries"][bucket]:
            verification = _verify_entry(
                context["run_dir"],
                config["run_id"],
                entry,
                project_root,
            )
            if verification == "verified":
                verified[bucket].append(entry)
            elif verification == "stale":
                stale_count += 1
            else:
                unsupported_count += 1
    active_status = [entry for entry in verified["status"] if not entry.get("resolution")]
    active_knowledge = [
        entry for entry in verified["knowledge"] if not entry.get("resolution")
    ]
    procedural = _build_procedural_entries(
        {"entries": {"status": active_status, "knowledge": active_knowledge}}
    )
    latest = {
        "status": _latest_semantic_entries(
            active_status,
            lambda entry: str(entry["payload"]["command_fingerprint"]),
        ),
        "knowledge": _latest_semantic_entries(
            active_knowledge,
            lambda entry: (
                f"{entry['payload']['review_phase_id']}:"
                f"{entry['payload']['finding_fingerprint']}"
            ),
        ),
        "procedural": sorted(procedural, key=_entry_sort_key, reverse=True),
    }
    if not any(latest.values()):
        return {
            **_empty_selection("no-verified-entries"),
            "stale_count": stale_count,
            "unsupported_count": unsupported_count,
        }
    selective_candidates = (
        [*latest["procedural"], *latest["status"], *latest["knowledge"]]
        if phase_id in SELECTIVE_FIX_PHASES
        else [*latest["procedural"]]
    )
    if config["mode"] in {"ledger-selective", "action-self-review"}:
        mode_candidates = (
            [
                *sorted(
                    [*latest["status"], *latest["knowledge"]],
                    key=_entry_sort_key,
                    reverse=True,
                ),
                *latest["procedural"],
            ]
            if config["mode"] == "action-self-review"
            and phase_id in SELECTIVE_FIX_PHASES
            else selective_candidates
        )
        if not mode_candidates:
            return {
                **_empty_selection("selective-no-repeat"),
                "stale_count": stale_count,
                "unsupported_count": unsupported_count,
            }
        selected = mode_candidates[: MAX_BLOCK_LINES - 1]
        header = (
            f"## Execution ledger (advisory; run {config['run_id']}, "
            f"round {round_number}/{MAX_FIX_LOOP_ROUNDS})"
        )
        selective_block = _bounded_block(
            header,
            (_prompt_line_for_entry(entry) for entry in selected),
        )
        action_evidence = _action_review_evidence_entry(
            selected[0], active_status, active_knowledge
        )
        action_block = _action_review_block(
            action_evidence,
            context["run_dir"],
            action_project_root,
        ) if action_evidence is not None else ""
        target_proxy = max(
            _token_proxy(_utf8_byte_length(selective_block)),
            _token_proxy(_utf8_byte_length(action_block)),
        )
        block = _pad_block_to_token_proxy(
            action_block if config["mode"] == "action-self-review" else selective_block,
            target_proxy,
        )
        return {
            "block": block,
            "entry_ids": (
                []
                if config["mode"] == "action-self-review"
                else [entry["id"] for entry in selected]
            ),
            "evidence_entry_ids": (
                [action_evidence["id"]]
                if config["mode"] == "action-self-review"
                and action_evidence is not None
                else []
                if config["mode"] == "action-self-review"
                else [entry["id"] for entry in selected]
            ),
            "evidence_entries": (
                [action_evidence]
                if config["mode"] == "action-self-review"
                and action_evidence is not None
                else []
                if config["mode"] == "action-self-review"
                else selected
            ),
            "stale_count": stale_count,
            "unsupported_count": unsupported_count,
            "reason": (
                "action-self-review"
                if block and config["mode"] == "action-self-review"
                else "verified-entries" if block else "char-cap"
            ),
        }
    header = (
        f"## Execution ledger (advisory; run {config['run_id']}, "
        f"round {round_number}/{MAX_FIX_LOOP_ROUNDS})"
    )
    candidates = [*latest["procedural"], *latest["status"], *latest["knowledge"]]
    selected = candidates[: MAX_BLOCK_LINES - 1]
    block = _bounded_block(header, (_prompt_line_for_entry(entry) for entry in selected))
    return {
        "block": block,
        "entry_ids": [entry["id"] for entry in selected],
        "evidence_entry_ids": [entry["id"] for entry in selected],
        "evidence_entries": selected,
        "stale_count": stale_count,
        "unsupported_count": unsupported_count,
        "reason": "verified-entries" if block else "char-cap",
    }


def _latest_semantic_entries(
    entries: Iterable[dict[str, object]],
    semantic_key: Any,
) -> list[dict[str, object]]:
    latest: dict[str, dict[str, object]] = {}
    for entry in sorted(entries, key=_entry_sort_key, reverse=True):
        key = semantic_key(entry)
        if key not in latest:
            latest[key] = entry
    return sorted(latest.values(), key=_entry_sort_key, reverse=True)


def _action_review_evidence_entry(
    entry: dict[str, object],
    status_entries: list[dict[str, object]],
    knowledge_entries: list[dict[str, object]],
) -> dict[str, object] | None:
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return None
    fingerprint = payload.get("fingerprint")
    if not isinstance(fingerprint, str):
        return entry
    fields = {
        "gate-command-repeat": "command_fingerprint",
        "gate-failure-repeat": "failure_fingerprint",
        "gate-diagnosis-repeat": "diagnosis_fingerprint",
        "review-finding-repeat": "finding_fingerprint",
    }
    repeat_type = payload.get("type")
    field = fields.get(repeat_type)
    if field is None:
        return None
    candidates = knowledge_entries if repeat_type == "review-finding-repeat" else status_entries
    matching = [
        candidate
        for candidate in candidates
        if isinstance(candidate.get("payload"), dict)
        and candidate["payload"].get(field) == fingerprint
    ]
    return max(matching, key=_entry_sort_key) if matching else None


def _action_review_block(
    entry: dict[str, object],
    run_dir: Path | str,
    action_project_root: object,
) -> str:
    provenance = entry.get("provenance")
    if not isinstance(provenance, dict) or not provenance.get("archive_path"):
        return ""
    source = _read_verified_archive(
        Path(run_dir), str(provenance.get("run_id") or ""), provenance
    )
    if source is None:
        return ""
    raw_slice = _bounded_raw_self_review_slice(entry, source, Path(run_dir))
    if raw_slice is None:
        return ""
    lines = [f"- Source sha256: {provenance['sha256']}"]
    remaining = _CONTROL_PATTERN.sub(" ", _ANSI_PATTERN.sub("", _canonical_json(raw_slice)))
    project_root = _canonical_project_root(action_project_root)
    if project_root:
        remaining = remaining.replace(project_root, "")
    while remaining and len(lines) < MAX_BLOCK_LINES - 1:
        chunk = _truncate_utf8(remaining, MAX_ITEM_BYTES - 8)
        if not chunk:
            break
        lines.append(f"- Raw: {chunk}")
        remaining = remaining[len(chunk) :]
    return _bounded_block(
        "## Action self-review (bounded verified artifact)", lines
    )


def _bounded_raw_self_review_slice(
    entry: dict[str, object], source: dict[str, object], run_dir: Path
) -> object | None:
    payload = source.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        match = re.fullmatch(
            r"/results/(\d+)", str(entry["provenance"].get("selector") or "")
        )
        selected = (
            payload["results"][int(match.group(1))]
            if match is not None and int(match.group(1)) < len(payload["results"])
            else None
        )
        return selected if isinstance(selected, dict) else None
    if (
        isinstance(payload, dict)
        and payload.get("schema_version") == REVIEW_SUMMARY_SCHEMA_VERSION
        and isinstance(payload.get("findings"), list)
        and _verify_review_artifact_binding(run_dir, payload)
    ):
        match = re.fullmatch(
            r"/findings/(\d+)", str(entry["provenance"].get("selector") or "")
        )
        finding = (
            payload["findings"][int(match.group(1))]
            if match is not None and int(match.group(1)) < len(payload["findings"])
            else None
        )
        return (
            None
            if finding is None
            else {
                "artifact_sha256": payload.get("artifact_sha256"),
                "finding": finding,
            }
        )
    return None


def _pad_block_to_token_proxy(block: str, target_proxy: int) -> str:
    if not block or _token_proxy(_utf8_byte_length(block)) >= target_proxy:
        return block
    target_bytes = min(((target_proxy - 1) * 4) + 1, MAX_BLOCK_BYTES)
    lines = [_truncate_utf8(line, MAX_ITEM_BYTES) for line in block.split("\n")]
    while len(lines) < MAX_BLOCK_LINES and _utf8_byte_length("\n".join(lines)) < target_bytes:
        remaining = target_bytes - _utf8_byte_length("\n".join(lines)) - 1
        if remaining <= 0:
            break
        lines.append("." * min(MAX_ITEM_BYTES, remaining))
    for index in range(len(lines) - 1, -1, -1):
        if _utf8_byte_length("\n".join(lines)) >= target_bytes:
            break
        remaining = target_bytes - _utf8_byte_length("\n".join(lines))
        capacity = MAX_ITEM_BYTES - _utf8_byte_length(lines[index])
        lines[index] += "." * min(capacity, remaining)
    return _truncate_utf8("\n".join(lines), MAX_BLOCK_BYTES)


def _execution_state_prompt_block_impl(**kwargs: object) -> str:
    try:
        if kwargs.get("experiment_enabled", False) is not True:
            return ""
        context = _load_context(kwargs["run_dir"], kwargs["run_id"], kwargs["mode"])
        return _select_prompt_block(
            context,
            kwargs.get("phase"),
            kwargs.get("project_root"),
            _normalize_round(kwargs.get("round", 1)),
        )["block"]
    except Exception:
        return ""


def _observe_execution_state_injection_impl(**kwargs: object) -> dict[str, object]:
    try:
        if kwargs.get("experiment_enabled", False) is not True:
            return {"ok": True, "enabled": False, "observed": False, "block": ""}
        context = _load_context(kwargs["run_dir"], kwargs["run_id"], kwargs["mode"])
        config = context["config"]
        round_number = _normalize_round(kwargs.get("round", 1))
        generated_at = _normalize_timestamp(
            kwargs.get("generated_at") or datetime.now(timezone.utc).isoformat()
        )
        phase = kwargs.get("phase")
        phase_id = str(_get(phase, "id", "") or "")
        action_project_root = str(Path(str(kwargs.get("project_root"))).resolve())
        selected = _select_prompt_block(
            context,
            phase,
            kwargs.get("project_root"),
            round_number,
            action_project_root,
        )
        block = selected["block"]
        event_base = {
            "schema_version": SCHEMA_VERSION,
            "run_id": config["run_id"],
            "round": round_number,
            "generated_at": generated_at,
            "phase": phase_id,
            "mode": config["mode"],
            "exposure_policy_sha256": config["exposure_policy_sha256"],
            "per_exposure_token_cap": config["per_exposure_token_cap"],
            "injected": bool(block),
            "reason": selected["reason"],
            "entry_ids": selected["entry_ids"],
            "evidence_entry_ids": selected["evidence_entry_ids"],
            "evidence_entries": selected["evidence_entries"],
            "normalization_context": {
                "extractor_version": EXTRACTOR_VERSION,
                "project_root": _canonical_project_root(kwargs.get("project_root")),
                "action_project_root_realpath": action_project_root,
            },
            "line_count": len(block.split("\n")) if block else 0,
            "byte_count": _utf8_byte_length(block),
            "block_sha256": _sha256(block),
            "block": block,
            "prompt_bytes": max(
                0,
                _integer_or_zero(
                    kwargs.get("prompt_bytes")
                    if kwargs.get("prompt_bytes") is not None
                    else kwargs.get("prompt_chars", 0)
                ),
            ),
            "stale_candidate_count": selected["stale_count"],
            "unsupported_candidate_count": selected["unsupported_count"],
        }
        event = {**event_base, "id": _sha256(_canonical_json(event_base))}
        appended = _append_committed_record(
            context, "injection", event, strict=True
        )
        return {"ok": True, "observed": appended, "block": block}
    except Exception as exc:
        return _fail_open(exc, block="")


def _apply_repeat_metric(metrics: dict[str, object], label: str, values: Iterable[object]) -> None:
    filtered = [value for value in values if isinstance(value, str) and value]
    counts: dict[str, int] = {}
    for value in filtered:
        counts[value] = counts.get(value, 0) + 1
    repeated = sum(max(0, count - 1) for count in counts.values())
    metrics[f"repeated_{label}_fingerprint_count"] = repeated
    metrics[f"repeated_{label}_fingerprint_rate"] = repeated / len(filtered) if filtered else 0


def _token_proxy(characters: object) -> int:
    count = max(0, _integer_or_zero(characters))
    return math.ceil(count / 4) if count > 0 else 0


def _recompute_metrics(
    context: dict[str, object], *, write: bool = True
) -> dict[str, object]:
    config = context["config"]
    paths = context["paths"]
    metrics = _empty_metrics(config["run_id"], config["mode"])
    captures = _read_jsonl(paths["captures"])
    injections = _read_jsonl(paths["injections"]) if paths["injections"].exists() else []
    usage = _read_jsonl(paths["usage"]) if paths["usage"].exists() else []
    run_totals = [event for event in usage if event.get("scope") == "run-total"]
    summary_run_total = run_totals[-1] if run_totals else None
    run_total = next(
        (
            event
            for event in run_totals
            if event.get("evidence_status") == VERIFIED_USAGE_EVIDENCE
        ),
        None,
    )
    measurements = [capture["measurement"] for capture in captures if isinstance(capture.get("measurement"), dict)]
    _apply_repeat_metric(
        metrics,
        "command",
        [value for item in measurements for value in item.get("command_fingerprints", [])],
    )
    _apply_repeat_metric(
        metrics,
        "failure",
        [value for item in measurements for value in item.get("failure_fingerprints", [])],
    )
    _apply_repeat_metric(
        metrics,
        "diagnosis",
        [value for item in measurements for value in item.get("diagnosis_fingerprints", [])],
    )
    _apply_repeat_metric(
        metrics,
        "finding",
        [value for item in measurements for value in item.get("finding_fingerprints", [])],
    )
    first_green = next(
        (
            item
            for item in measurements
            if item.get("gate_passed") is True and isinstance(item.get("gate_attempt"), int)
        ),
        None,
    )
    last_gate_attempt = max(
        (
            item["gate_attempt"]
            for item in measurements
            if isinstance(item.get("gate_attempt"), int)
            and not isinstance(item.get("gate_attempt"), bool)
        ),
        default=0,
    )
    metrics["gate_green_attempt"] = (
        first_green["gate_attempt"]
        if first_green
        else (
            last_gate_attempt
            if summary_run_total is not None and last_gate_attempt > 0
            else None
        )
    )
    metrics["gate_green_le_3"] = (
        first_green["gate_attempt"] <= MAX_FIX_LOOP_ROUNDS
        if first_green
        else False if summary_run_total is not None else None
    )
    metrics["fix_loop_rounds"] = max(
        (max(0, _integer_or_zero(item.get("fix_loop_rounds"))) for item in measurements),
        default=0,
    )
    metrics["prompt_bytes"] = sum(max(0, _integer_or_zero(item.get("prompt_bytes"))) for item in injections)
    metrics["injected_bytes"] = sum(max(0, _integer_or_zero(item.get("byte_count"))) for item in injections)
    metrics["injected_event_count"] = sum(
        1 for item in injections if item.get("injected") is True
    )
    metrics["prompt_token_proxy"] = sum(
        _token_proxy(max(0, _integer_or_zero(item.get("prompt_bytes"))))
        for item in injections
    )
    metrics["injected_token_proxy"] = sum(
        _token_proxy(max(0, _integer_or_zero(item.get("byte_count"))))
        for item in injections
    )
    metrics["runner_control_snapshot_sha256"] = config[
        "runner_control_snapshot_sha256"
    ]
    metrics["exposure_policy_sha256"] = config["exposure_policy_sha256"]
    metrics["per_exposure_token_cap"] = config["per_exposure_token_cap"]
    metrics["max_injected_token_proxy"] = max(
        (
            _token_proxy(max(0, _integer_or_zero(item.get("byte_count"))))
            for item in injections
        ),
        default=0,
    )
    metrics["exposure_budget_compliant"] = all(
        item.get("exposure_policy_sha256") == config["exposure_policy_sha256"]
        and item.get("per_exposure_token_cap")
        == config["per_exposure_token_cap"]
        and _token_proxy(max(0, _integer_or_zero(item.get("byte_count"))))
        <= config["per_exposure_token_cap"]
        for item in injections
    )
    metrics["stale_candidate_count"] = sum(
        max(0, _integer_or_zero(item.get("stale_candidate_count"))) for item in injections
    )
    metrics["malformed_source_count"] = sum(
        max(0, _integer_or_zero(item.get("unsupported_count"))) for item in captures
    )
    metrics["excluded_source_count"] = sum(
        max(0, _integer_or_zero(item.get("unsupported_candidate_count"))) for item in injections
    )
    metrics["unsupported_source_count"] = (
        metrics["malformed_source_count"] + metrics["excluded_source_count"]
    )
    ledger_entries: dict[str, dict[str, object]] = {}
    ledger = context.get("ledger")
    if isinstance(ledger, dict):
        for bucket in ("status", "knowledge", "procedural"):
            for entry in ledger["entries"][bucket]:
                ledger_entries[entry["id"]] = entry
    stale_reminders = 0
    unsupported_reminders = 0
    reminder_denominator = 0
    for injection in injections:
        evidence_entry_ids = injection.get(
            "evidence_entry_ids",
            injection.get("entry_ids", []),
        )
        evidence_entries = injection.get("evidence_entries")
        if not isinstance(evidence_entries, list):
            evidence_entries = [
                ledger_entries[entry_id]
                for entry_id in evidence_entry_ids
                if entry_id in ledger_entries
            ]
        if [
            entry.get("id") if isinstance(entry, dict) else None
            for entry in evidence_entries
        ] != evidence_entry_ids:
            count = max(len(evidence_entry_ids), len(evidence_entries))
            unsupported_reminders += count
            reminder_denominator += count
            continue
        for entry in evidence_entries:
            reminder_denominator += 1
            if not isinstance(entry, dict):
                unsupported_reminders += 1
                continue
            verification = _verify_recorded_entry_integrity(
                context["run_dir"],
                config["run_id"],
                entry,
            )
            if verification == "stale":
                stale_reminders += 1
            elif verification != "verified":
                unsupported_reminders += 1
    metrics["stale_reminder_count"] = stale_reminders
    metrics["unsupported_reminder_count"] = unsupported_reminders
    metrics["stale_reminder_rate"] = (
        metrics["stale_reminder_count"] / reminder_denominator if reminder_denominator else 0
    )
    metrics["unsupported_reminder_rate"] = (
        metrics["unsupported_reminder_count"] / reminder_denominator if reminder_denominator else 0
    )
    metrics["stale_count"] = metrics["stale_reminder_count"]
    metrics["unsupported_count"] = metrics["unsupported_reminder_count"]
    metrics["canonical_routing_violations"] = sum(
        1 for item in measurements if item.get("canonical_routing_violation") is True
    )
    metrics["canonical_transition_count"] = len(captures)
    route_coverage = _canonical_route_coverage(context["workflow"], captures)
    metrics["canonical_route_coverage_complete"] = route_coverage["complete"]
    metrics["canonical_route_coverage_gap_count"] = route_coverage["gap_count"]
    metrics["routing_provenance"] = sorted(
        (
            {
                "transition_occurrence_id": capture.get(
                    "transition_occurrence_id"
                ),
                "workflow_id": capture.get("workflow_id"),
                "phase": capture.get("phase"),
                "round": capture.get("round"),
                "route_key": capture.get("route_key"),
                "routed_to": capture.get("routed_to"),
                "expected_target": capture.get("expected_target"),
            }
            for capture in captures
        ),
        key=_canonical_json,
    )
    metrics["actual_usage_coverage"] = run_total is not None
    metrics["actual_control_coverage"] = bool(
        run_total is not None
        and run_total.get("control_evidence_status") == VERIFIED_CONTROL_EVIDENCE
    )
    metrics["actual_control_evidence_status"] = (
        run_total.get("control_evidence_status") if run_total else None
    )
    metrics["actual_usage_evidence_status"] = (
        run_total.get("evidence_status") if run_total else None
    )
    metrics["actual_input_tokens"] = run_total.get("input_tokens") if run_total else None
    metrics["actual_output_tokens"] = run_total.get("output_tokens") if run_total else None
    metrics["actual_total_tokens"] = (
        run_total["input_tokens"] + run_total["output_tokens"] if run_total else None
    )
    metrics["actual_additional_tokens"] = run_total.get("additional_tokens") if run_total else None
    metrics["actual_additional_token_coverage"] = bool(
        run_total is not None
        and run_total.get("additional_token_scope") == "condition-total"
    )
    metrics["actual_usage_budget_matched"] = bool(
        run_total is not None
        and run_total.get("additional_tokens") == metrics["injected_token_proxy"]
    )
    metrics["latency_ms"] = run_total.get("latency_ms") if run_total else None
    metrics["estimated_cost"] = run_total.get("estimated_cost") if run_total else None
    metrics["summary_usage_coverage"] = summary_run_total is not None
    metrics["summary_input_tokens"] = (
        summary_run_total.get("input_tokens") if summary_run_total else None
    )
    metrics["summary_output_tokens"] = (
        summary_run_total.get("output_tokens") if summary_run_total else None
    )
    metrics["summary_additional_tokens"] = (
        summary_run_total.get("additional_tokens") if summary_run_total else None
    )
    metrics["summary_latency_ms"] = (
        summary_run_total.get("latency_ms") if summary_run_total else None
    )
    metrics["summary_estimated_cost"] = (
        summary_run_total.get("estimated_cost") if summary_run_total else None
    )
    controls_complete = all(
        config.get(key) is not None
        for key in (
            "base_commit",
            "experiment_id",
            "model_id",
            "tool_permissions_sha256",
            "system_prompt_sha256",
            "caps_sha256",
            "provider_retry_policy_sha256",
            "provider_max_retries",
            "pricing_snapshot_sha256",
            "provider_attestation_key_id",
            "provider_attestation_public_key_sha256",
            "execution_controls_sha256",
        )
    )
    metrics["experiment_valid"] = bool(
        metrics.get("actual_usage_coverage")
        and metrics.get("actual_control_coverage")
        and metrics.get("actual_additional_token_coverage")
        and last_gate_attempt > 0
        and controls_complete
        and metrics["stale_reminder_count"] == 0
        and metrics["unsupported_reminder_count"] == 0
        and metrics["canonical_routing_violations"] == 0
        and metrics["canonical_route_coverage_complete"]
        and metrics["exposure_budget_compliant"]
    )
    if write:
        _write_canonical_json(paths["metrics"], metrics)
    return metrics


def _canonical_route_coverage(
    workflow: dict[str, object], captures: list[dict[str, object]]
) -> dict[str, object]:
    phases = workflow["phases"]
    phase_ids = {phase["id"] for phase in phases}
    expected_phase: object = phases[0]["id"] if phases else "complete"
    gap_count = 0
    for capture in captures:
        if expected_phase == "complete" or capture.get("phase") != expected_phase:
            gap_count += 1
        target = capture.get("routed_to")
        if target != "complete" and target not in phase_ids:
            gap_count += 1
            expected_phase = None
            continue
        expected_phase = target
    if expected_phase != "complete":
        gap_count += 1
    return {"complete": gap_count == 0, "gap_count": gap_count}


def _record_execution_state_usage_impl(**kwargs: object) -> dict[str, object]:
    try:
        if kwargs.get("experiment_enabled", False) is not True:
            return {"ok": True, "enabled": False, "recorded": False}
        context = _load_context(kwargs["run_dir"], kwargs["run_id"], kwargs["mode"])
        receipt_path = kwargs.get("receipt_path")
        receipt = (
            None
            if receipt_path is None
            else _archive_usage_receipt(
                Path(context["run_dir"]),
                Path(receipt_path),
                kwargs.get("receipt_sha256"),
                context["config"],
            )
        )
        receipt_fields = (
            _usage_fields_from_receipt(receipt["payload"], context["config"])
            if receipt is not None
            else None
        )
        generated_at = _normalize_timestamp(
            receipt_fields["generated_at"]
            if receipt_fields is not None
            else kwargs.get("generated_at")
        )
        scope = _require_string(
            receipt_fields["scope"]
            if receipt_fields is not None
            else kwargs.get("scope"),
            "scope",
        )
        if scope not in {"phase", "run-total"}:
            raise ValueError(f"invalid execution ledger usage scope: {scope}")
        raw_event_id = _require_string(
            receipt_fields["event_id"]
            if receipt_fields is not None
            else kwargs.get("event_id"),
            "event_id",
        )
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", raw_event_id):
            raise ValueError("invalid execution ledger event_id")
        phase_id = (
            receipt_fields["phase_id"]
            if receipt_fields is not None
            else kwargs.get("phase_id")
        )
        usage_round = (
            receipt_fields["round"]
            if receipt_fields is not None
            else kwargs.get("round")
        )
        if scope == "phase":
            phase_id = _require_string(phase_id, "phase_id")
            if not isinstance(usage_round, int) or isinstance(usage_round, bool) or usage_round < 0:
                raise ValueError("invalid execution ledger usage round")
        elif phase_id is not None or usage_round is not None:
            raise ValueError("run-total usage must have null phase_id and round")
        model_id = _require_string(
            receipt_fields["model_id"]
            if receipt_fields is not None
            else kwargs.get("model_id"),
            "model_id",
        )
        if model_id != context["config"].get("model_id"):
            raise ValueError("execution ledger usage model_id mismatch")
        input_tokens = _require_nonnegative_integer(
            receipt_fields["input_tokens"]
            if receipt_fields is not None
            else kwargs.get("input_tokens"),
            "input_tokens",
        )
        output_tokens = _require_nonnegative_integer(
            receipt_fields["output_tokens"]
            if receipt_fields is not None
            else kwargs.get("output_tokens"),
            "output_tokens",
        )
        additional_tokens = _require_nonnegative_integer(
            receipt_fields["additional_tokens"]
            if receipt_fields is not None
            else kwargs.get("additional_tokens"),
            "additional_tokens",
        )
        if additional_tokens > input_tokens:
            raise ValueError("execution ledger additional_tokens exceeds input_tokens")
        latency_ms = _require_nonnegative_integer(
            receipt_fields["latency_ms"]
            if receipt_fields is not None
            else kwargs.get("latency_ms"),
            "latency_ms",
        )
        cost = (
            receipt_fields["estimated_cost_usd"]
            if receipt_fields is not None
            else kwargs.get("estimated_cost_usd")
        )
        canonical_cost = _canonical_decimal_cost(cost)
        if receipt_fields is not None:
            _assert_manual_usage_matches_receipt(kwargs, receipt_fields)
        elif kwargs.get("receipt_sha256") is not None:
            raise ValueError("execution ledger receipt_sha256 requires receipt_path")
        event = {
            "schema_version": SCHEMA_VERSION,
            "id": _sha256(
                _canonical_json({"run_id": kwargs["run_id"], "event_id": raw_event_id})
            ),
            "event_id": raw_event_id,
            "run_id": kwargs["run_id"],
            "generated_at": generated_at,
            "scope": scope,
            "phase_id": phase_id,
            "round": usage_round,
            "model_id": model_id,
            "additional_token_scope": "condition-total",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "additional_tokens": additional_tokens,
            "latency_ms": latency_ms,
            "estimated_cost": canonical_cost,
            "evidence_status": (
                VERIFIED_USAGE_EVIDENCE
                if receipt is not None
                else SUMMARY_USAGE_EVIDENCE
            ),
            "control_evidence_status": (
                VERIFIED_CONTROL_EVIDENCE
                if receipt is not None
                else SUMMARY_USAGE_EVIDENCE
            ),
            "control_receipt_id": (
                receipt_fields["control_receipt_id"]
                if receipt_fields is not None
                else None
            ),
            "runner_control_snapshot_sha256": (
                receipt_fields["runner_control_snapshot_sha256"]
                if receipt_fields is not None
                else None
            ),
            "exposure_policy_sha256": (
                receipt_fields["exposure_policy_sha256"]
                if receipt_fields is not None
                else None
            ),
            "per_exposure_token_cap": (
                receipt_fields["per_exposure_token_cap"]
                if receipt_fields is not None
                else None
            ),
            "tool_permissions_sha256": (
                receipt_fields["tool_permissions_sha256"]
                if receipt_fields is not None
                else None
            ),
            "system_prompt_sha256": (
                receipt_fields["system_prompt_sha256"]
                if receipt_fields is not None
                else None
            ),
            "caps_sha256": (
                receipt_fields["caps_sha256"]
                if receipt_fields is not None
                else None
            ),
            "provider_retry_policy_sha256": (
                receipt_fields["provider_retry_policy_sha256"]
                if receipt_fields is not None
                else None
            ),
            "provider_max_retries": (
                receipt_fields["provider_max_retries"]
                if receipt_fields is not None
                else None
            ),
            "pricing_snapshot_sha256": (
                receipt_fields["pricing_snapshot_sha256"]
                if receipt_fields is not None
                else None
            ),
            "provider_attestation_key_id": (
                receipt_fields["provider_attestation_key_id"]
                if receipt_fields is not None
                else None
            ),
            "provider_attestation_public_key_sha256": (
                receipt_fields["provider_attestation_public_key_sha256"]
                if receipt_fields is not None
                else None
            ),
            "execution_controls_sha256": (
                receipt_fields["execution_controls_sha256"]
                if receipt_fields is not None
                else None
            ),
            "usage_provenance": (
                receipt["provenance"] if receipt is not None else None
            ),
        }
        existing_usage = _read_jsonl(context["paths"]["usage"])
        if scope == "run-total" and any(
            item.get("scope") == "run-total" and item.get("id") != event["id"]
            for item in existing_usage
        ):
            raise ValueError("execution ledger already has a distinct run-total usage event")
        if scope == "run-total":
            _canonical_terminal_state(
                context["run_dir"], context["config"], context["workflow"]
            )
        recorded = _append_committed_record(
            context,
            "usage",
            event,
            strict=True,
            completed_at=generated_at if scope == "run-total" else None,
        )
        return {
            "ok": True,
            "recorded": recorded,
            "phase_id": phase_id,
            "model_id": model_id,
            "evidence_status": event["evidence_status"],
            "control_evidence_status": event["control_evidence_status"],
        }
    except Exception as exc:
        return _fail_open(exc)
