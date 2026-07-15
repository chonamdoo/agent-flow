"""Adapter contract.

Every AI host (HostedAdapter for claude/codex/omp, GenericAdapter for
fallback) implements `execute`. The runner only knows the contract; AI-
specific hints live in the host-name parameterization.

Return semantics:
  - True  → this call wrote the artifact; runner advances to the next phase.
  - False → this call emitted a prompt; the host AI must do the work, write
            the artifact file, then follow `agent-flow status` / `next_command`.
The artifact path is `<run_dir>/<phase.artifact>` when the workflow declares
one, otherwise `<run_dir>/<phase.id>.md`.

Profile injection:
  - The runner sets `self._profile_snapshot` and `self._profile_id` on the
    adapter before invoking execute. `render_envelope` includes a profile
    block so the host AI sees the parsed profile YAML as data, not as
    text instructions to "look it up somewhere".
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, TYPE_CHECKING

import yaml
from agent_flow.core.local_skills import local_skill_prompt_block
from agent_flow.core.skill_plan import profile_skill_prompt_block

if TYPE_CHECKING:
    from agent_flow.runner import Phase


class Adapter(ABC):
    """Base for all host adapters.

    Note: profile state (`_profile_snapshot`, `_profile_id`) is set as
    *instance* attributes by `__init__`. We deliberately avoid class-level
    defaults like `_profile_snapshot: dict = {}` to prevent the classic
    Python footgun where a mutable class default is shared across every
    instance and silently leaks mutations.
    """

    name: str = "base"

    def __init__(self) -> None:
        self._profile_snapshot: dict[str, Any] = {}
        self._profile_id: str = "generic"
        self._architecture: str = "default"
        self._lore_citations: list[Any] = []  # list[Lore]; typed loose to avoid import cycle
        self._config_root: Path | None = None
        self._task_scope: str = ""

    @abstractmethod
    def execute(self, phase: "Phase", run_dir: Path, project_root: Path) -> bool:
        """Run one phase; return True iff the artifact was written."""

    @staticmethod
    def artifact_path(phase: "Phase", run_dir: Path) -> Path:
        return run_dir / (phase.artifact or f"{phase.id}.md")

    def render_envelope(self, phase: "Phase", run_dir: Path,
                        project_root: Path, host_hint: str = "") -> str:
        """Render the prompt envelope shared by all AI adapters."""
        artifact = self.artifact_path(phase, run_dir)
        relative_artifact = (
            artifact.relative_to(project_root)
            if artifact.is_relative_to(project_root)
            else artifact
        )
        body = phase.prompt or "(no prompt body — see workflow YAML)"
        host_block = (
            f"\n\n## Host-specific guidance\n{host_hint}\n" if host_hint else ""
        )
        config_root = self._config_root or project_root
        profile_block = self._render_profile_block(config_root, phase)
        architecture_block = self._render_architecture_block(phase)
        completion_gate_block = self._render_completion_gate_block(phase, body)
        profile_skill_block = profile_skill_prompt_block(
            config_root,
            phase.id,
            project_root,
            self._task_scope,
            required_skills=phase.required_skills,
        )
        local_skill_block = local_skill_prompt_block(config_root, phase.id)
        lore_block = self._render_lore_block(project_root, phase)
        return (
            f"# agent-flow phase: {phase.id}\n\n"
            f"**Description**: {phase.description}\n\n"
            f"**Run id**: {run_dir.name}\n"
            f"**Project root**: {project_root}\n"
            f"**Artifact target** (write this when the phase is complete):\n"
            f"  `{relative_artifact}`\n"
            f"\n## Phase prompt\n\n{body}\n"
            f"{profile_block}"
            f"{architecture_block}"
            f"{completion_gate_block}"
            f"{profile_skill_block}"
            f"{local_skill_block}"
            f"{lore_block}"
            f"{host_block}"
            f"\n## When complete\n"
            f"After writing the artifact, run `agent-flow status` from "
            f"`{project_root}` and follow the printed `next_command`."
        )

    def _render_architecture_block(self, phase: "Phase") -> str:
        if self._architecture == "ddd":
            if phase.id in {"design", "slice-plan", "ddd-design", "architecture-review"}:
                return (
                    "\n## Architecture mode: `ddd`\n\n"
                    "DDD is explicit for this run. Do not complete this phase "
                    "as a shallow service split. Model the domain vocabulary, "
                    "context boundaries, objects, events, invariants, and "
                    "domain flow before Clean Architecture boundary checks. "
                    "If the artifact rejects DDD, label the work `service-layer "
                    "refactor` instead.\n\n"
                    "Required design vocabulary: Bounded Context, Aggregates, "
                    "Ubiquitous Language, Entities, Value Objects, Domain "
                    "Events, Domain Invariants, and Domain Flow.\n"
                )
        if self._architecture == "service-layer":
            return (
                "\n## Architecture mode: `service-layer`\n\n"
                "This run is not DDD. Label structural work as a service-layer "
                "refactor and do not claim DDD boundaries were enforced.\n"
            )
        return ""

    def _render_completion_gate_block(self, phase: "Phase", body: str = "") -> str:
        markers = tuple(
            marker
            for marker in getattr(phase, "required_markers", ())
            if marker not in body
        )
        required_skills = getattr(phase, "required_skills", ())
        requirements = getattr(phase, "requirements", ())
        if not markers and not required_skills and not requirements:
            return ""
        lines = [
            "\n## Completion gate",
            "",
            "Do not write the artifact as complete until the phase genuinely "
            "satisfies these markers. The runner blocks advancement when any "
            "marker is missing. The artifact must include a `## Completion Gate` "
            "heading followed by the marker lines.",
            "",
        ]
        if markers:
            lines.extend(("Required marker lines:", ""))
            lines.extend(f"- `{marker}`" for marker in markers)
        if required_skills or requirements:
            contract = json.dumps(
                {
                    "applied_skills": list(required_skills),
                    "requirements": {
                        requirement: "pass" for requirement in requirements
                    },
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            lines.extend(
                (
                    "",
                    "Record the complete applied canonical skill set and each "
                    "requirement status (`pass` or `fail`) in exactly one machine-readable line:",
                    "",
                    f"`phase-contract: {contract}`",
                )
            )
        lines.append("")
        return "\n".join(lines)

    def _render_lore_block(self, project_root: Path, phase: "Phase") -> str:
        """Inline relevant lore entries for phases that opt in.

        The runner pre-searches the lore index using the task description and
        sets `_lore_citations` on this adapter. We surface them here as a
        digest the host AI can cite or skim. Empty when no matches.

        Phases declare `cite_lore: true` in workflow YAML to receive the
        block — this avoids hardcoding `phase.id == "design"` so renamed
        phases or new workflows still work.
        """
        if not self._lore_citations:
            return ""
        if not getattr(phase, "cite_lore", False):
            return ""
        lines = [
            "\n## Relevant lore (auto-cited from `.agent-flow/memory/lore/`)",
            "",
            "These entries match the task keywords. Cite by relative path "
            "where they actually apply; ignore entries that aren't relevant. "
            "Do NOT fabricate citations.",
            "",
        ]
        for lore in self._lore_citations:
            try:
                rel = lore.path.relative_to(project_root)
            except ValueError:
                rel = lore.path
            lines.append(f"### `{rel}` (weight {lore.weight:.2f}, type {lore.type})")
            lines.append(f"**Title**: {lore.title}")
            if lore.constraint:
                lines.append(f"**Constraint**: {_oneline(lore.constraint, 200)}")
            if lore.directive:
                lines.append(f"**Directive**: {_oneline(lore.directive, 200)}")
            lines.append("")
        return "\n".join(lines) + "\n"

    def _render_profile_block(self, config_root: Path, phase: "Phase") -> str:
        if not self._profile_snapshot:
            return ""
        projection = _phase_profile_projection(self._profile_snapshot, phase.id)
        try:
            yaml_dump = yaml.safe_dump(
                projection, sort_keys=False, allow_unicode=True
            ).rstrip()
        except yaml.YAMLError:
            return ""
        return (
            f"\n## Active profile: `{self._profile_id}`\n\n"
            f"Phase-specific profile projection:\n\n"
            f"```yaml\n{yaml_dump}\n```\n"
        )

    def render_review_packet(
        self,
        phase: "Phase",
        run_dir: Path,
        project_root: Path,
    ) -> str:
        config_root = self._config_root or project_root
        artifact = self.artifact_path(phase, run_dir)
        skill_block = profile_skill_prompt_block(
            config_root,
            phase.id,
            project_root,
            self._task_scope,
            required_skills=phase.required_skills,
        )
        contract = {
            "required_skills": list(phase.required_skills),
            "requirements": list(phase.requirements),
            "artifact": str(artifact),
        }
        profile_contract = _compact_review_profile_contract(self._profile_snapshot)
        task_summary = _oneline(self._task_scope, 512)
        profile_block = ""
        if profile_contract:
            profile_block = (
                "Profile contract:\n"
                "```yaml\n"
                f"{yaml.safe_dump(profile_contract, sort_keys=False, allow_unicode=True).rstrip()}\n"
                "```\n"
            )
        return (
            f"# agent-flow compact review packet\n\n"
            f"Phase: {phase.id}\n"
            f"Project: {project_root}\n"
            f"Run: {run_dir.name}\n"
            f"Task: {task_summary}\n"
            f"Review source: current git diff and referenced run artifacts\n"
            f"Contract: {json.dumps(contract, separators=(',', ':'), sort_keys=True)}\n"
            f"{profile_block}"
            f"{skill_block}"
        )


def _oneline(text: str, max_len: int) -> str:
    """Compress whitespace and truncate for prompt-budget single-line digest."""
    import re
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) <= max_len:
        return flat
    return flat[:max_len - 1] + "…"


def _phase_profile_projection(profile: dict[str, Any], phase_id: str) -> dict[str, Any]:
    if profile.get("id") == "multi-profile" and isinstance(profile.get("profiles"), list):
        return {
            "id": "multi-profile",
            "active_profiles": list(profile.get("active_profiles") or []),
            "profiles": [
                _single_profile_projection(item, phase_id)
                for item in profile["profiles"]
                if isinstance(item, dict)
            ],
        }
    return _single_profile_projection(profile, phase_id)


def _single_profile_projection(profile: dict[str, Any], phase_id: str) -> dict[str, Any]:
    projection: dict[str, Any] = {"id": profile.get("id", "generic")}
    if phase_id == "worktree":
        _copy_profile_field(profile, projection, "branching")
    elif phase_id in {"gates", "qa"}:
        _copy_profile_field(profile, projection, "gates")
    elif phase_id in {"final-review", "review", "multi-review", "architecture-review"}:
        _copy_profile_field(profile, projection, "review_angles")
        architecture = _architecture_projection(profile)
        if architecture:
            projection["architecture"] = architecture
    elif phase_id in {
        "implement",
        "implement-fix",
        "red",
        "green",
        "refactor",
        "fix-loop",
        "pr-comment-fix",
        "pr-ci-fix",
    }:
        architecture = _architecture_projection(profile)
        if architecture:
            projection["architecture"] = architecture
        _copy_profile_field(profile, projection, "gates")
    elif phase_id in {"design", "ddd-design", "slice-plan", "plan-review"}:
        architecture = _architecture_projection(profile)
        if architecture:
            projection["architecture"] = architecture
    return projection


def _architecture_projection(profile: dict[str, Any]) -> dict[str, Any]:
    architecture = profile.get("architecture")
    if not isinstance(architecture, dict):
        return {}
    allowed = ("contract", "platform", "strict_when_roots_present", "activation_roots")
    return {key: architecture[key] for key in allowed if key in architecture}


def _compact_review_profile_contract(profile: dict[str, Any]) -> dict[str, Any]:
    if profile.get("id") == "multi-profile" and isinstance(profile.get("profiles"), list):
        profiles: list[dict[str, Any]] = []
        for item in profile["profiles"]:
            if not isinstance(item, dict):
                continue
            architecture = _architecture_projection(item)
            if architecture:
                profiles.append(
                    {
                        "id": item.get("id", "generic"),
                        "architecture": architecture,
                    }
                )
        if not profiles:
            return {}
        return {
            "id": "multi-profile",
            "active_profiles": list(profile.get("active_profiles") or []),
            "profiles": profiles,
        }
    architecture = _architecture_projection(profile)
    if not architecture:
        return {}
    return {
        "id": profile.get("id", "generic"),
        "architecture": architecture,
    }


def _copy_profile_field(source: dict[str, Any], target: dict[str, Any], key: str) -> None:
    if key in source:
        target[key] = source[key]
