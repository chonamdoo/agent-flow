from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
from typing import Any

import yaml

from agent_flow.core.markers import normalize_required_markers, unfenced_markdown_text
from agent_flow.core.security import ensure_child_path, validate_safe_name
from agent_flow.core.skill_resolver import PhaseSkills


@dataclass(frozen=True)
class PhaseDefinition:
    id: str
    description: str
    prompt: str | None
    pause_after: bool
    optional: bool
    multi_review: bool
    cite_lore: bool
    routes: dict[str, str] | None
    required_markers: tuple[str, ...]
    artifact: str
    skills: PhaseSkills | None = None


@dataclass(frozen=True)
class PhaseWorkflowDefinition:
    id: str
    phases: tuple[PhaseDefinition, ...]
    source: str

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "phases": [asdict(phase) for phase in self.phases],
        }


def find_kit_root() -> Path:
    """Locate the agent-flow kit root (contains workflows/ and profiles/)."""
    here = Path(__file__).resolve()
    candidates = [
        parent
        for parent in here.parents
        if (parent / "workflows").is_dir() and (parent / "profiles").is_dir()
    ]
    for candidate in candidates:
        if (candidate / "pyproject.toml").is_file() or (candidate / "package.json").is_file():
            return candidate
    if candidates:
        return candidates[0]
    raise RuntimeError("Could not locate agent-flow kit root from " + str(here))


def load_phase_workflow_definition(kit_root: Path, name: str) -> PhaseWorkflowDefinition:
    validate_safe_name(name, "workflow")
    path = kit_root / "workflows" / f"{name}.yaml"
    ensure_child_path(kit_root / "workflows", path, "workflow")
    if not path.exists():
        raise FileNotFoundError(f"Workflow not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"workflow {path}: top-level must be a mapping")
    workflow_id = raw.get("id", name)
    if not isinstance(workflow_id, str) or not workflow_id:
        raise ValueError(f"workflow {path}: id must be a non-empty string")
    phases_raw = raw.get("phases") or []
    if not isinstance(phases_raw, list) or not phases_raw:
        raise ValueError(f"workflow {path}: missing or empty `phases`")
    phases = _normalize_phases(phases_raw, path, workflow_id)
    _validate_routes(phases, path)
    return PhaseWorkflowDefinition(id=workflow_id, phases=tuple(phases), source=str(path))


def _normalize_phases(phases_raw: list[object], path: Path, workflow_id: str) -> list[PhaseDefinition]:
    out: list[PhaseDefinition] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(phases_raw):
        if not isinstance(item, dict) or "id" not in item:
            raise ValueError(f"workflow {path}: phase {index} missing `id` (got {item!r})")
        phase_id = _string_field(item, "id", path, index)
        if phase_id in seen_ids:
            raise ValueError(
                f"workflow {path}: duplicate phase id {phase_id!r} at index {index}. "
                "Each phase id must be unique."
            )
        seen_ids.add(phase_id)
        routes = _routes(item.get("routes"), path, phase_id)
        out.append(
            PhaseDefinition(
                id=phase_id,
                description=_optional_string(item.get("description"), ""),
                prompt=_optional_string_or_none(item.get("prompt")),
                pause_after=_bool_field(item.get("pause_after", False), path, phase_id, "pause_after"),
                optional=_bool_field(item.get("optional", False), path, phase_id, "optional"),
                multi_review=_bool_field(item.get("multi_review", False), path, phase_id, "multi_review"),
                cite_lore=_bool_field(item.get("cite_lore", False), path, phase_id, "cite_lore"),
                routes=routes,
                required_markers=normalize_required_markers(item.get("required_markers")),
                artifact=_optional_string(item.get("artifact"), _default_artifact_for_phase(workflow_id, phase_id)),
                skills=_phase_skills(item.get("skills"), path, phase_id),
            )
        )
    return out


def _phase_skills(value: object, path: Path, phase_id: str) -> PhaseSkills | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"workflow {path}: phase {phase_id} `skills` must be a mapping")
    unknown = set(value) - {"required", "optional"}
    if unknown:
        raise ValueError(
            f"workflow {path}: phase {phase_id} `skills` has unknown keys {sorted(unknown)}"
        )
    skills = PhaseSkills(
        required=_skill_names(value.get("required"), path, phase_id, "required"),
        optional=_skill_names(value.get("optional"), path, phase_id, "optional"),
    )
    return None if skills.is_empty() else skills


def _skill_names(value: object, path: Path, phase_id: str, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"workflow {path}: phase {phase_id} `skills.{field}` must be a list")
    names: list[str] = []
    for item in value:
        name = str(item)
        validate_safe_name(name, f"phase {phase_id} skills.{field}")
        names.append(name)
    return tuple(dict.fromkeys(names))


def _validate_routes(phases: list[PhaseDefinition], path: Path) -> None:
    phase_ids = {phase.id for phase in phases}
    for phase in phases:
        if not phase.routes:
            continue
        for key, target in phase.routes.items():
            if target == "block":
                continue
            if target not in phase_ids:
                raise ValueError(f"workflow {path}: phase {phase.id} route {key!r} targets unknown phase {target!r}")


def _string_field(item: dict[str, object], field: str, path: Path, index: int) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"workflow {path}: phase {index} `{field}` must be a non-empty string")
    return value


def _optional_string(value: object, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("workflow phase string field must be a string")
    return value


def _optional_string_or_none(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("workflow phase prompt must be a string")
    return value


def overall_review_route_key(text: str) -> str:
    in_overall_section = False
    verdicts: list[str] = []
    overall_sections = 0
    for line in unfenced_markdown_text(text).splitlines():
        stripped = line.strip()
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped, re.IGNORECASE)
        if heading:
            in_overall_section = bool(
                heading.group(1) == "##"
                and re.fullmatch(
                    r"(?:overall|final)(?:\s+verdict)?",
                    heading.group(2).strip(),
                    re.IGNORECASE,
                )
            )
            if in_overall_section:
                overall_sections += 1
            continue
        if not in_overall_section:
            continue
        match = re.fullmatch(
            r"verdict:\s*(approve|request-changes)",
            stripped,
            re.IGNORECASE,
        )
        if match:
            verdicts.append(match.group(1).lower())
    if overall_sections > 1 or len(verdicts) > 1:
        return "invalid-verdict"
    return verdicts[0] if verdicts else "default"



def _bool_field(value: object, path: Path, phase_id: str, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"workflow {path}: phase {phase_id} `{field}` must be boolean")
    return value


def _routes(value: object, path: Path, phase_id: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"workflow {path}: phase {phase_id} `routes` must be a mapping")
    routes: dict[str, str] = {}
    for key, target in value.items():
        if not isinstance(key, str) or not isinstance(target, str):
            raise ValueError(f"workflow {path}: phase {phase_id} routes must map strings to strings")
        routes[key] = target
    return routes


def _default_artifact_for_phase(workflow_id: str, phase_id: str) -> str:
    if workflow_id != "full-feature":
        return f"{phase_id}.md"
    if phase_id == "red":
        return "artifacts/red.log"
    if phase_id == "green":
        return "artifacts/green.log"
    if phase_id == "gates":
        return "artifacts/gate-results.json"
    return f"artifacts/{phase_id}.md"
