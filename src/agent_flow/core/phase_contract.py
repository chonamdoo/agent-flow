from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from agent_flow.core.skill_compatibility import (
    SkillCompatibilityError,
    SkillResolutionError,
    compatible_reference_set,
    normalize_skill_compatibility,
)
from agent_flow.core.skill_plan import (
    CODE_SKILL_PHASES,
    _portable_casefold,
    assert_active_host_matches_catalog,
    authenticated_installed_skill_index,
    resolve_runtime_skill_plan,
    runtime_changed_files,
)


PhaseT = TypeVar("PhaseT")


def resolve_runtime_phase_contract(
    phase: PhaseT,
    *,
    config_root: Path,
    project_root: Path,
    meta: dict[str, Any],
) -> PhaseT:
    phase_id = str(getattr(phase, "id"))
    required_skills = tuple(getattr(phase, "required_skills", ()))
    requirements = tuple(getattr(phase, "requirements", ()))
    if phase_id not in CODE_SKILL_PHASES and not required_skills and not requirements:
        return phase
    index = authenticated_installed_skill_index(config_root)
    if index is None:
        return phase
    active_host = os.environ.get("AGENT_FLOW_ACTIVE_HOST") or os.environ.get("AGENT_FLOW_HOST")
    assert_active_host_matches_catalog(
        index, active_host.strip().lower() if isinstance(active_host, str) else None
    )
    workspace = meta.get("workspace")
    base_commit = workspace.get("head") if isinstance(workspace, dict) else None
    plan = resolve_runtime_skill_plan(
        index,
        phase_id,
        runtime_changed_files(
            config_root,
            project_root,
            base_commit if isinstance(base_commit, str) else None,
        ),
        str(meta.get("task") or ""),
        required_skills,
        index_root=config_root,
        active_host=active_host,
    )
    resolution_errors = plan.get("resolution_errors")
    if isinstance(resolution_errors, list) and resolution_errors:
        diagnostics = [
            diagnostic
            for diagnostic in resolution_errors
            if isinstance(diagnostic, dict)
        ]
        if diagnostics:
            raise SkillResolutionError(diagnostics)
    missing_profiles = plan.get("missing_profiles")
    if isinstance(missing_profiles, list) and missing_profiles:
        raise RuntimeError(
            "missing required skill profiles in project snapshot: "
            + ", ".join(str(name) for name in missing_profiles)
        )
    missing = plan.get("missing")
    if isinstance(missing, list) and missing:
        raise RuntimeError(
            "missing required profile skills in project snapshot: "
            + ", ".join(str(name) for name in missing)
        )
    skills = plan.get("skills")
    resolved = (
        tuple(
            str(skill["name"])
            for skill in skills
            if isinstance(skill, dict) and isinstance(skill.get("name"), str)
        )
        if isinstance(skills, list)
        else ()
    )
    lock = meta.get("resolved_skill_lock")
    if isinstance(lock, dict):
        locked_names = {
            _portable_casefold(entry["name"])
            for entry in lock.get("skills", [])
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        }
        host_loaded_names = {
            _portable_casefold(skill["name"])
            for skill in skills
            if isinstance(skill, dict)
            and isinstance(skill.get("name"), str)
            and skill.get("load_mode") == "plain_text"
            and skill.get("source_host")
            == (active_host.strip().lower() if isinstance(active_host, str) else None)
        }
        outside = sorted(
            name
            for name in resolved
            if _portable_casefold(name) not in locked_names
            and _portable_casefold(name) not in host_loaded_names
        )
        if outside:
            raise SkillResolutionError(
                [
                    {
                        "skill": name,
                        "reason": "skill_outside_run_lock",
                        "phase": phase_id,
                    }
                    for name in outside
                ]
            )
    return replace(
        phase,
        required_skills=resolved or required_skills,
        skill_compatibility=normalize_skill_compatibility(index.get("compatibility")),
    )


def phase_contract_issues(phase: object, text: str) -> list[str]:
    required_skills = tuple(getattr(phase, "required_skills", ()))
    required_requirements = tuple(getattr(phase, "requirements", ()))
    if not required_skills and not required_requirements:
        return []
    payload = _phase_contract_payload(text)
    if payload is None:
        return ["phase-contract payload is invalid"]
    applied = payload.get("applied_skills")
    requirements = payload.get("requirements")
    if (
        not isinstance(applied, list)
        or any(not isinstance(name, str) or not name for name in applied)
        or not isinstance(requirements, dict)
    ):
        return ["phase-contract payload is invalid"]
    try:
        required_references = compatible_reference_set(
            getattr(phase, "skill_compatibility", None),
            required_skills,
        )
        applied_references = compatible_reference_set(
            getattr(phase, "skill_compatibility", None),
            applied,
        )
    except SkillCompatibilityError:
        return ["phase-contract skill compatibility is invalid"]
    except SkillResolutionError as exc:
        return [str(exc)]
    missing_skills = sorted(required_references - applied_references)
    issues: list[str] = []
    if missing_skills:
        issues.append(
            "phase-contract missing required skills: " + ", ".join(missing_skills)
        )
    missing_requirements = [
        requirement
        for requirement in required_requirements
        if requirement not in requirements
    ]
    if missing_requirements:
        issues.append(
            "phase-contract missing requirements: " + ", ".join(missing_requirements)
        )
    invalid_requirements = [
        requirement
        for requirement in required_requirements
        if requirement in requirements and requirements[requirement] not in {"pass", "fail"}
    ]
    if invalid_requirements:
        issues.append(
            "phase-contract invalid requirement status: "
            + ", ".join(invalid_requirements)
        )
    return issues


def phase_contract_route_key(phase: object, text: str) -> str | None:
    required_skills = tuple(getattr(phase, "required_skills", ()))
    required_requirements = tuple(getattr(phase, "requirements", ()))
    if not required_skills and not required_requirements:
        return None
    if phase_contract_issues(phase, text):
        return None
    payload = _phase_contract_payload(text)
    assert payload is not None
    requirements = payload["requirements"]
    return (
        "failure"
        if any(requirements[requirement] == "fail" for requirement in required_requirements)
        else "success"
    )


def declared_artifact_issues(
    run_dir: Path,
    phase: object,
    phase_entered_at: object,
) -> list[str]:
    artifacts = tuple(getattr(phase, "artifacts", ()))
    issues: list[str] = []
    for relative in artifacts[1:]:
        artifact = run_dir / relative
        if not artifact.is_file():
            issues.append(f"missing declared artifact {relative}")
        elif artifact_is_stale(artifact, phase_entered_at):
            issues.append(f"stale declared artifact {relative}")
    return issues


def artifact_is_stale(artifact: Path, phase_entered_at: object) -> bool:
    entered_at = _meta_timestamp(phase_entered_at)
    if entered_at is None:
        return False
    try:
        artifact_mtime = artifact.stat().st_mtime
    except FileNotFoundError:
        return False
    return artifact_mtime < entered_at


def phase_entry_time(meta: dict[str, Any]) -> object:
    return meta.get("phase_entered_at") or meta.get("updated_at") or meta.get("started_at")


def _meta_timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _phase_contract_payload(text: str) -> dict[str, Any] | None:
    lines = [
        line[len("phase-contract:") :].strip()
        for line in text.splitlines()
        if line.startswith("phase-contract:")
    ]
    if len(lines) != 1 or not lines[0]:
        return None
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
