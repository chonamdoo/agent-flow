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
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from agent_flow.core.design_ledger import LEDGER_SOURCE_PHASES, ledger_prompt_block
from agent_flow.core.local_skills import declared_concern_ids, local_skill_prompt_block

if TYPE_CHECKING:
    from agent_flow.runner import Phase

# resolver가 이미 구체적인 skill 목록으로 바꿔 놓는 키들. 원본 선언을 다시 넣지 않는다.
_RESOLVER_OWNED_PROFILE_KEYS = frozenset({"skills", "skill_sources"})


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
        self._config_root: Path | None = None
        self._task_text: str = ""
        self._changed_files: tuple[str, ...] = ()
        # 열거된 concern. free-form task 문구와 달리 required tier를 만들 수 있다.
        self._concerns: tuple[str, ...] = ()
        # 렌더된 envelope를 받아 갈 관측자. runner가 꽂고, 없으면 아무 일도
        # 없다. `execute`를 넓히지 않고 정확한 주입 프롬프트를 남기는 유일한
        # 자리다 — envelope는 여기서만 만들어지고 곧바로 stdout으로 사라진다.
        # 인자는 `(phase_id, payload_name, envelope)`다. 한 phase가 envelope를
        # 두 번 렌더하므로(host용, reviewer base용) 이름은 렌더러가 정한다.
        # 이름을 관측자가 phase_id에서 만들면 두 렌더가 같은 이름으로 겹쳐
        # 어느 쪽이 host에게 실제로 갔는지 trace로 고를 수 없다.
        self._observer: Callable[[str, str, str], None] | None = None

    def config_root_or(self, project_root: Path) -> Path:
        """선언을 읽을 뿌리. runner가 꽂지 않았으면 project_root.

        managed checkout에는 `.agent-flow/`가 없다(gitignored). 이 fallback이
        갈라지면 한쪽은 선언을 찾고 다른 쪽은 못 찾는다 — reviewer launch 선언과
        local skill 블록이 서로 다른 뿌리를 보게 된다.
        """
        return self._config_root or project_root

    def profile_review_angles(self) -> list[Mapping[str, object]]:
        angles = self._profile_snapshot.get("review_angles")
        if not isinstance(angles, list):
            return []
        return [angle for angle in angles if isinstance(angle, dict)]


    @abstractmethod
    def execute(self, phase: "Phase", run_dir: Path, project_root: Path) -> bool:
        """Run one phase; return True iff the artifact was written."""

    @staticmethod
    def artifact_path(phase: "Phase", run_dir: Path) -> Path:
        return run_dir / (phase.artifact or f"{phase.id}.md")

    def render_envelope(self, phase: "Phase", run_dir: Path,
                        project_root: Path, host_hint: str = "",
                        *, prompt_variant: str = "",
                        skill_host: str | None = None) -> str:
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
        profile_block = self._render_profile_block(phase)
        architecture_block = self._render_architecture_block(phase)
        completion_gate_block = self._render_completion_gate_block(phase)
        config_root = self.config_root_or(project_root)
        local_skill_block = local_skill_prompt_block(
            config_root,
            phase.id,
            phase_skills=getattr(phase, "skills", None),
            profile=self._profile_snapshot,
            changed_files=self._changed_files,
            task_text=self._task_text,
            concerns=self._concerns,
            host=skill_host,
        )
        # 이전 phase의 수치는 대화 컨텍스트가 아니라 여기로만 건너온다.
        # 렌더러가 넣으므로 agent가 빼거나 잊을 수 없다.
        design_values_block = ledger_prompt_block(run_dir)
        task_line = f"**Task**: {self._task_text}\n" if self._task_text else ""
        envelope = (
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
            f"{host_block}"
            f"\n## When complete\n"
            f"After writing the artifact, run `agent-flow status` from "
            f"`{project_root}` and follow the printed `next_command`."
        )
        if self._observer is not None:
            # variant가 있으면 이름이 갈린다. `prompt-<phase>`는 host가 실제로
            # 받은 envelope 하나만 가리켜야 한다.
            suffix = f"-{prompt_variant}" if prompt_variant else ""
            self._observer(phase.id, f"prompt-{phase.id}{suffix}", envelope)
        return envelope

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

    def _render_profile_block(self, phase: "Phase") -> str:
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
        ) + self._render_declared_concerns(phase)

    def _render_declared_concerns(self, phase: "Phase") -> str:
        """`concerns:` 로 적을 수 있는 id 목록. `skills`/`skill_sources`는 프롬프트에서
        떼어내므로(`_RESOLVER_OWNED_PROFILE_KEYS`) 이 목록은 여기서만 보인다.

        선언하는 phase에만 싣는다. 선언할 수 없는 phase에 목록을 실으면 그 phase는
        쓸 수 없는 어휘값만 지불한다.
        """
        if getattr(phase, "id", "") not in LEDGER_SOURCE_PHASES:
            return ""
        ids = sorted(declared_concern_ids(self._profile_snapshot))
        if not ids:
            return ""
        return f"\nDeclared concern ids: {', '.join(ids)}\n"
