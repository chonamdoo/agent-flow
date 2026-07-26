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

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, TYPE_CHECKING

import yaml
from agent_flow.core.design_ledger import ledger_prompt_block
from agent_flow.core.local_skills import local_skill_prompt_block

if TYPE_CHECKING:
    from agent_flow.runner import Phase

# resolver가 이미 구체적인 skill 목록으로 바꿔 놓는 키들. 원본 표를 다시 넣지 않는다.
_RESOLVER_OWNED_PROFILE_KEYS = frozenset(
    {"skills", "skill_sources", "android_skills", "chrisbanes_skills"}
)


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
        self._task_text: str = ""
        self._changed_files: tuple[str, ...] = ()

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
        profile_block = self._render_profile_block()
        architecture_block = self._render_architecture_block(phase)
        completion_gate_block = self._render_completion_gate_block(phase)
        config_root = self._config_root or project_root
        local_skill_block = local_skill_prompt_block(
            config_root,
            phase.id,
            phase_skills=getattr(phase, "skills", None),
            profile=self._profile_snapshot,
            changed_files=self._changed_files,
            task_text=self._task_text,
        )
        lore_block = self._render_lore_block(project_root, phase)
        # 이전 phase의 수치는 대화 컨텍스트가 아니라 여기로만 건너온다.
        # 렌더러가 넣으므로 agent가 빼거나 잊을 수 없다.
        design_values_block = ledger_prompt_block(run_dir)
        task_line = f"**Task**: {self._task_text}\n" if self._task_text else ""
        return (
            f"# agent-flow phase: {phase.id}\n\n"
            f"**Description**: {phase.description}\n\n"
            f"{task_line}"
            f"**Run id**: {run_dir.name}\n"
            f"**Project root**: {project_root}\n"
            f"**Artifact target** (write this when the phase is complete):\n"
            f"  `{relative_artifact}`\n"
            f"\n## Phase prompt\n\n{body}\n"
            f"{design_values_block}"
            f"{profile_block}"
            f"{architecture_block}"
            f"{completion_gate_block}"
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

    def _render_profile_block(self) -> str:
        """Inline the active profile YAML so the host AI sees real data.

        The runner injects `_profile_snapshot` and `_profile_id` before
        calling execute. Skill routing keys are excluded: the resolver already
        turned them into a concrete per-phase skill list, and dumping the raw
        tables again costs ~160 lines on every phase envelope.
        """
        if not self._profile_snapshot:
            return ""
        trimmed = {
            key: value
            for key, value in self._profile_snapshot.items()
            if key not in _RESOLVER_OWNED_PROFILE_KEYS
        }
        if not trimmed:
            return ""
        try:
            yaml_dump = yaml.safe_dump(trimmed, sort_keys=False, allow_unicode=True).rstrip()
        except yaml.YAMLError:
            return ""
        return (
            f"\n## Active profile: `{self._profile_id}`\n\n"
            f"This is the parsed profile YAML — use these values directly, "
            f"don't ask the user to look them up:\n\n"
            f"```yaml\n{yaml_dump}\n```\n"
        )


def _oneline(text: str, max_len: int) -> str:
    """Compress whitespace and truncate for prompt-budget single-line digest."""
    import re
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) <= max_len:
        return flat
    return flat[:max_len - 1] + "…"
