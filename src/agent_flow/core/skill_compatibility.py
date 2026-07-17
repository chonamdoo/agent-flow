from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from agent_flow.core.security import validate_portable_skill_name


_ALLOWED_STATUSES = frozenset({"active", "renamed", "deprecated", "removed"})


class SkillResolutionError(ValueError):
    def __init__(self, diagnostics: Iterable[dict[str, Any]]) -> None:
        self.diagnostics = tuple(diagnostics)
        super().__init__(
            "skill_resolution_error: "
            + json.dumps(
                self.diagnostics,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )


class SkillCompatibilityError(SkillResolutionError):
    def __init__(self, message: str) -> None:
        self.diagnostics: tuple[dict[str, Any], ...] = ()
        ValueError.__init__(self, message)


@dataclass(frozen=True)
class CompatibilityResolution:
    requested: str
    canonical: str
    capabilities: tuple[str, ...]
    reason: str | None = None

    @property
    def resolved(self) -> bool:
        return self.reason is None

    def diagnostic(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "requested": self.requested,
            "canonical": self.canonical,
            "capabilities": list(self.capabilities),
            "repairable": False,
        }


@dataclass(frozen=True)
class SkillCompatibilityCatalog:
    projection: dict[str, Any]
    by_canonical: dict[str, dict[str, Any]]
    references: dict[str, str]

    @classmethod
    def from_value(cls, value: object) -> SkillCompatibilityCatalog:
        projection = normalize_skill_compatibility(value)
        by_canonical = {
            record["canonical"]: record for record in projection["skills"]
        }
        references = {
            reference: record["canonical"]
            for record in projection["skills"]
            for reference in (
                record["canonical"],
                *record["aliases"],
                *record["renamed_from"],
            )
        }
        return cls(projection, by_canonical, references)

    def resolve(self, requested: object) -> CompatibilityResolution:
        requested_name = _logical_name(requested, "requested skill")
        canonical = self.references.get(requested_name, requested_name)
        visited: set[str] = set()
        while True:
            if canonical in visited:
                raise SkillCompatibilityError(f"replacement cycle: {canonical}")
            visited.add(canonical)
            record = self.by_canonical.get(canonical)
            if record is None:
                return CompatibilityResolution(requested_name, canonical, ())
            status = record["status"]
            capabilities = tuple(record["capabilities"])
            if status == "active":
                return CompatibilityResolution(requested_name, canonical, capabilities)
            replacements = record["replaced_by"]
            if not replacements:
                return CompatibilityResolution(
                    requested_name,
                    canonical,
                    capabilities,
                    f"{status}_without_replacement",
                )
            if len(replacements) != 1:
                return CompatibilityResolution(
                    requested_name,
                    canonical,
                    capabilities,
                    "multiple_replacements_unsupported",
                )
            canonical = replacements[0]

    def validate_concrete_ids(self, concrete_names: Iterable[object]) -> None:
        for value in concrete_names:
            concrete = _logical_name(value, "concrete skill")
            owner = self.references.get(concrete, concrete)
            if owner != concrete:
                raise SkillCompatibilityError(
                    f"compatibility reference shadows concrete skill: {concrete}"
                )



def normalize_skill_compatibility(value: object) -> dict[str, Any]:
    if value is None:
        return {"version": 1, "skills": []}
    if not isinstance(value, dict):
        raise SkillCompatibilityError("compatibility projection must be an object")
    version = value.get("version", 1)
    if (
        isinstance(version, bool)
        or not isinstance(version, (int, float))
        or version != 1
    ):
        raise SkillCompatibilityError("unsupported compatibility version")
    raw_skills = value.get("skills", [])
    if not isinstance(raw_skills, list):
        raise SkillCompatibilityError("compatibility skills must be a list")

    records: list[dict[str, Any]] = []
    by_canonical: dict[str, dict[str, Any]] = {}
    references: dict[str, str] = {}
    for raw in raw_skills:
        if not isinstance(raw, dict):
            raise SkillCompatibilityError("compatibility skill entry must be an object")
        canonical = _logical_name(raw.get("canonical"), "canonical skill")
        if canonical in by_canonical:
            raise SkillCompatibilityError(f"duplicate compatibility canonical: {canonical}")
        raw_status = raw.get("status", "active")
        if not isinstance(raw_status, str):
            raise SkillCompatibilityError(
                f"invalid compatibility status: {canonical}"
            )
        status = raw_status.strip().lower()
        if status not in _ALLOWED_STATUSES:
            raise SkillCompatibilityError(f"invalid compatibility status: {canonical}")
        capabilities = _logical_list(raw.get("capabilities"), "capability")
        aliases = _logical_list(raw.get("aliases"), "alias")
        renamed_from = _logical_list(raw.get("renamed_from"), "renamed reference")
        replaced_by = _replacement_list(raw.get("replaced_by"))
        if status == "active" and replaced_by:
            raise SkillCompatibilityError(f"active compatibility skill has replacement: {canonical}")
        record = {
            "canonical": canonical,
            "capabilities": capabilities,
            "aliases": aliases,
            "renamed_from": renamed_from,
            "status": status,
            "replaced_by": replaced_by,
        }
        by_canonical[canonical] = record
        records.append(record)
        for reference in (canonical, *aliases, *renamed_from):
            owner = references.get(reference)
            if owner is not None:
                raise SkillCompatibilityError(
                    f"duplicate compatibility reference: {reference} ({owner}, {canonical})"
                )
            references[reference] = canonical

    _canonicalize_replacements(by_canonical, references)
    _validate_replacement_cycles(by_canonical)
    records.sort(key=lambda record: record["canonical"])
    return {"version": 1, "skills": records}


def resolve_compatibility_reference(
    compatibility: object,
    requested: object,
) -> CompatibilityResolution:
    return SkillCompatibilityCatalog.from_value(compatibility).resolve(requested)


def compatible_reference_set(
    compatibility: object,
    values: Iterable[object],
) -> set[str]:
    catalog = SkillCompatibilityCatalog.from_value(compatibility)
    resolved_names: set[str] = set()
    diagnostics: list[dict[str, Any]] = []
    for value in values:
        resolved = catalog.resolve(value)
        if resolved.resolved:
            resolved_names.add(resolved.canonical)
        else:
            diagnostics.append(resolved.diagnostic())
    if diagnostics:
        raise SkillResolutionError(diagnostics)
    return resolved_names


def _logical_name(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SkillCompatibilityError(f"invalid {label}")
    try:
        return validate_portable_skill_name(value).lower()
    except (TypeError, ValueError) as exc:
        raise SkillCompatibilityError(f"invalid {label}") from exc


def _logical_list(value: object, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SkillCompatibilityError(f"{label} list is invalid")
    names = [_logical_name(item, label) for item in value]
    if len(set(names)) != len(names):
        raise SkillCompatibilityError(f"duplicate compatibility {label}")
    return sorted(names)


def _replacement_list(value: object) -> list[str]:
    if value is None or value == "":
        return []
    values = value if isinstance(value, list) else [value]
    names = [_logical_name(item, "replacement") for item in values]
    if len(set(names)) != len(names):
        raise SkillCompatibilityError("duplicate compatibility replacement")
    return sorted(names)


def _canonicalize_replacements(
    by_canonical: dict[str, dict[str, Any]],
    references: dict[str, str],
) -> None:
    for record in by_canonical.values():
        replacements = sorted(
            references.get(replacement, replacement)
            for replacement in record["replaced_by"]
        )
        if len(set(replacements)) != len(replacements):
            raise SkillCompatibilityError("duplicate compatibility replacement")
        record["replaced_by"] = replacements


def _validate_replacement_cycles(by_canonical: dict[str, dict[str, Any]]) -> None:
    def visit(name: str, path: tuple[str, ...]) -> None:
        if name in path:
            raise SkillCompatibilityError("replacement cycle: " + " -> ".join((*path, name)))
        record = by_canonical.get(name)
        if record is None:
            return
        for replacement in record["replaced_by"]:
            visit(replacement, (*path, name))

    for canonical in by_canonical:
        visit(canonical, ())
