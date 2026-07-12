"""Adapter contract.

Every AI host (HostedAdapter for claude/codex/omp, GenericAdapter for
fallback) implements `execute`. The runner only knows the contract; AI-
specific hints live in the host-name parameterization.

Return semantics:
  - True  → this call wrote the artifact; runner advances to the next phase.
  - False → this call emitted a prompt; the host AI must do the work, write
            the artifact file, then follow `agent-flow-python status` / `next_command`.
The artifact path is `<run_dir>/<phase.artifact>` when the workflow declares
one, otherwise `<run_dir>/<phase.id>.md`.

Profile injection:
  - The runner sets `self._profile_snapshot` and `self._profile_id` on the
    adapter before invoking execute. `render_envelope` includes a profile
    block so the host AI sees the parsed profile YAML as data, not as
    text instructions to "look it up somewhere".
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, TYPE_CHECKING

import yaml
from agent_flow.core.execution_state_ledger import execution_state_prompt_block
from agent_flow.core.local_skills import local_skill_prompt_block
from agent_flow.core.profiles import (
    profile_prompt_projection,
    render_profile_prompt_block,
)
from agent_flow.core.skill_plan import profile_skill_prompt_block, runtime_changed_files

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
        self._active_phase_id: str = ""
        self._architecture: str = "default"
        self._lore_citations: list[Any] = []  # list[Lore]; typed loose to avoid import cycle
        self._config_root: Path | None = None
        self._task_scope: str = ""
        self._base_commit: str | None = None
        self._ledger_run_dir: Path | None = None
        self._ledger_run_id: str = ""
        self._ledger_mode: str = "artifacts-only"
        self._ledger_experiment_enabled: bool = False
        self._ledger_round: int = 1
        self._ledger_prompt_block: str | None = None

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
        self._active_phase_id = phase.id
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
        profile_block = self._render_profile_block(config_root)
        architecture_block = self._render_architecture_block(phase)
        completion_gate_block = self._render_completion_gate_block(phase)
        changed_files = runtime_changed_files(
            config_root,
            project_root,
            self._base_commit,
        )
        profile_skill_block = profile_skill_prompt_block(
            config_root,
            phase.id,
            project_root,
            self._task_scope,
            self._base_commit,
        )
        local_skill_block = local_skill_prompt_block(
            config_root,
            phase.id,
            self._task_scope,
            changed_files,
        )
        lore_block = self._render_lore_block(project_root, phase)
        ledger_prompt = ""
        if not getattr(phase, "multi_review", False) and self._ledger_run_dir is not None:
            if self._ledger_prompt_block is not None:
                ledger_prompt = self._ledger_prompt_block
            else:
                ledger_prompt = execution_state_prompt_block(
                    run_dir=self._ledger_run_dir,
                    run_id=self._ledger_run_id,
                    mode=self._ledger_mode,
                    experiment_enabled=self._ledger_experiment_enabled,
                    phase=phase,
                    project_root=project_root,
                    round=self._ledger_round,
                )
        ledger_block = f"\n\n{ledger_prompt}" if ledger_prompt else ""
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
            f"{ledger_block}"
            f"{host_block}"
            f"\n## When complete\n"
            f"After writing the artifact, run `agent-flow-python status` from "
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

    def _render_completion_gate_block(self, phase: "Phase") -> str:
        markers = getattr(phase, "required_markers", ())
        if not markers:
            return ""
        lines = [
            "\n## Completion gate",
            "",
            "Do not write the artifact as complete until the phase genuinely "
            "satisfies these markers. The runner blocks advancement when any "
            "marker is missing. The artifact must include a `## Completion Gate` "
            "heading followed by the marker lines.",
            "",
            "Required marker lines:",
            "",
        ]
        lines.extend(f"- `{marker}`" for marker in markers)
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

    def _render_profile_block(self, config_root: Path) -> str:
        """Inline the active profile YAML so the host AI sees real data.

        The runner injects `_profile_snapshot` and `_profile_id` before
        calling execute. If absent (e.g., adapter constructed standalone),
        this block is omitted gracefully.
        """
        if not self._profile_snapshot:
            return ""
        try:
            return render_profile_prompt_block(
                self._profile_id,
                self._profile_snapshot,
                self._active_phase_id,
            )
        except yaml.YAMLError:
            return ""


def _profile_projection(profile: dict[str, Any], phase_id: str) -> dict[str, Any]:
    """Keep phase prompts focused while preserving profile top-level contracts."""
    return profile_prompt_projection(profile, phase_id)


def _oneline(text: str, max_len: int) -> str:
    """Compress whitespace and truncate for prompt-budget single-line digest."""
    import re
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) <= max_len:
        return flat
    return flat[:max_len - 1] + "…"
