"""Phase loop runner.

Reads a workflow YAML, iterates phases, delegates execution to the detected
adapter, and treats artifact files as the state machine. The chain itself is
the enforcement mechanism: a phase only advances when the previous artifact
exists, and the run only ends when all phases complete.

Profile awareness:
  - The active profile is detected at install time (`kit.json:profile`) and
    re-read on each run.
  - Profile YAML (branching / gates / review_angles / vocabulary / etc) is
    parsed and injected into the prompt envelope so the host AI sees the
    stack-specific knobs as real data, not as text instructions to "look it
    up somewhere".

Workflow validation:
  - Empty / missing `phases` raises a clear error rather than KeyError.
  - Each phase must have an `id`; missing fails fast with the file path.

Adapter contract:
  - `Adapter.execute(...)` returns True if the artifact was written here
    (advance to the next phase) or False if the host AI must follow up
    (emit prompt, exit, wait for `agent-flow continue`).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from agent_flow.adapters.auto import detect_adapter
from agent_flow.artifact import (
    create_run,
    has_artifact,
    mark_inactive,
    read_meta,
    write_meta,
)
from agent_flow.cli_detect import detect_available_clis
from agent_flow.memory.index import LoreIndex
from agent_flow.memory.lore import Lore


class ResumeMode(Enum):
    START = "start"
    RESUME = "resume"


@dataclass
class Phase:
    id: str
    description: str
    prompt: str | None = None
    pause_after: bool = False
    optional: bool = False
    multi_review: bool = False  # Triggers fan-out hint in adapter envelope
    cite_lore: bool = False     # Inject relevant lore into prompt envelope
    routes: dict[str, str] | None = None


class Runner:
    def __init__(
        self,
        project_root: Path,
        workflow: str = "default",
        run_dir: Path | None = None,
    ) -> None:
        self.project_root = project_root
        self.workflow_name = workflow
        self.run_dir = run_dir
        self.kit_root = _find_kit_root()
        if run_dir is not None:
            meta = read_meta(run_dir)
            self.workflow_name = meta.get("workflow", workflow)
        self.phases = _load_workflow(self.kit_root, self.workflow_name)
        self.profile_id, self.profile = _load_profile(self.kit_root, project_root)

    def run(self, mode: ResumeMode, task: str = "") -> None:
        if mode == ResumeMode.START:
            self.run_dir = create_run(self.project_root, self.workflow_name, task)
            print(f"▶ run started : {self.run_dir.name}")
            print(f"▶ task        : {task}")
        else:
            assert self.run_dir is not None
            meta = read_meta(self.run_dir)
            print(f"▶ resuming    : {self.run_dir.name}")
            print(f"▶ task        : {meta.get('task', '')}")

        adapter = detect_adapter()
        clis = detect_available_clis()
        cli_summary = ", ".join(c.name for c in clis) if clis else "none (generic fallback)"
        print(f"▶ host adapter: {adapter.name}")
        print(f"▶ available  : {cli_summary}")
        print(f"▶ profile    : {self.profile_id}")
        print(f"▶ workflow   : {self.workflow_name} ({len(self.phases)} phases)\n")

        # Inject profile snapshot into adapter so render_envelope can include
        # it. Both attributes are declared on the Adapter base class, so this
        # is plain instance-attribute assignment.
        adapter._profile_snapshot = self.profile
        adapter._profile_id = self.profile_id

        # Auto-cite lore: search the local lore index for entries relevant
        # to the task description and inject them into the prompt envelope.
        # Empty list when memory dir is missing or no matches.
        meta_for_lore = read_meta(self.run_dir) if self.run_dir else {}
        adapter._lore_citations = _search_lore(
            self.project_root, meta_for_lore.get("task", ""),
        )

        assert self.run_dir is not None
        meta = read_meta(self.run_dir)
        phase_index = int(meta.get("phase_index", 0) or 0)
        while phase_index < len(self.phases):
            phase = self.phases[phase_index]
            if has_artifact(self.run_dir, phase.id):
                print(f"  [skip] {phase.id}")
                phase_index, blocked = self._next_index(phase_index, phase)
                meta = read_meta(self.run_dir)
                meta["phase_index"] = phase_index
                meta["current_phase"] = (
                    self.phases[phase_index].id
                    if phase_index < len(self.phases)
                    else None
                )
                write_meta(self.run_dir, meta)
                if blocked:
                    print(
                        f"\n═══ phase '{phase.id}' is blocked. "
                        f"Update the artifact or rerun the watcher, then "
                        f"`agent-flow continue`. ═══"
                    )
                    return
                continue
            print(f"  [run]  {phase.id} — {phase.description}")
            completed = adapter.execute(
                phase, run_dir=self.run_dir, project_root=self.project_root,
            )
            meta = read_meta(self.run_dir)
            meta["current_phase"] = phase.id
            meta["phase_index"] = phase_index
            write_meta(self.run_dir, meta)
            if not completed:
                print(
                    f"\n═══ phase '{phase.id}' awaits host AI. "
                    f"Write artifact → `agent-flow continue`. ═══"
                )
                return
            if phase.pause_after:
                print(
                    f"\n═══ pause: '{phase.id}' 결과 검토 후 "
                    f"`agent-flow continue` ═══"
                )
                return
            phase_index, blocked = self._next_index(phase_index, phase)
            meta = read_meta(self.run_dir)
            meta["phase_index"] = phase_index
            meta["current_phase"] = (
                self.phases[phase_index].id
                if phase_index < len(self.phases)
                else None
            )
            write_meta(self.run_dir, meta)
            if blocked:
                print(
                    f"\n═══ phase '{phase.id}' is blocked. "
                    f"Update the artifact or rerun the watcher, then "
                    f"`agent-flow continue`. ═══"
                )
                return

        mark_inactive(self.run_dir)
        print("\n✓ run complete.")

    def _next_index(self, current_index: int, phase: Phase) -> tuple[int, bool]:
        if not phase.routes:
            return current_index + 1, False
        assert self.run_dir is not None
        artifact = self.run_dir / f"{phase.id}.md"
        text = artifact.read_text(encoding="utf-8") if artifact.exists() else ""
        key = _route_key(text)
        target = phase.routes.get(key)
        if target == "block":
            print(f"  [block] {phase.id} status={key}")
            return current_index, True
        if target:
            for i, candidate in enumerate(self.phases):
                if candidate.id == target:
                    if i <= current_index:
                        target_artifact = self.run_dir / f"{candidate.id}.md"
                        if target_artifact.exists():
                            target_artifact.unlink()
                    return i, False
            raise ValueError(f"phase {phase.id}: route target not found: {target}")
        return current_index + 1, False


def _find_kit_root() -> Path:
    """Locate the agent-flow kit root (contains workflows/ and profiles/)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "workflows").is_dir() and (parent / "profiles").is_dir():
            return parent
    raise RuntimeError("Could not locate agent-flow kit root from " + str(here))


def _load_workflow(kit_root: Path, name: str) -> list[Phase]:
    path = kit_root / "workflows" / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Workflow not found: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"workflow {path}: top-level must be a mapping")
    phases_raw = raw.get("phases") or []
    if not isinstance(phases_raw, list) or not phases_raw:
        raise ValueError(f"workflow {path}: missing or empty `phases`")
    out: list[Phase] = []
    seen_ids: set[str] = set()
    for i, p in enumerate(phases_raw):
        if not isinstance(p, dict) or "id" not in p:
            raise ValueError(
                f"workflow {path}: phase {i} missing `id` (got {p!r})"
            )
        pid = str(p["id"])
        if pid in seen_ids:
            raise ValueError(
                f"workflow {path}: duplicate phase id {pid!r} at index {i}. "
                f"Each phase id must be unique — `has_artifact` would silently "
                f"skip the second."
            )
        seen_ids.add(pid)
        out.append(Phase(
            id=pid,
            description=str(p.get("description", "")),
            prompt=p.get("prompt"),
            pause_after=bool(p.get("pause_after", False)),
            optional=bool(p.get("optional", False)),
            multi_review=bool(p.get("multi_review", False)),
            cite_lore=bool(p.get("cite_lore", False)),
            routes=p.get("routes"),
        ))
    return out

def _route_key(text: str) -> str:
    lowered = text.lower()
    checks = (
        "request-changes",
        "ci-failed",
        "comments",
        "pending",
        "green",
        "approve",
        "merged",
        "closed",
        "error",
    )
    for key in checks:
        if f"verdict: {key}" in lowered or f"status: {key}" in lowered:
            return key
    return "default"


def _load_profile(kit_root: Path, project_root: Path) -> tuple[str, dict[str, Any]]:
    """Return (profile_id, profile_dict).

    Resolution order:
      1. `AGENT_FLOW_PROFILE` env override (always wins; user opted in)
      2. `.agent-flow/kit.json:profile` written by the installer
      3. fall back to "generic"

    A typo in `kit.json:profile` would otherwise run the entire workflow
    against the wrong stack (wrong branching, gates, PR target) — a
    correctness bug, not a degraded mode. So we treat that case as a hard
    error unless `AGENT_FLOW_FALLBACK_GENERIC=1` opts into silent fallback.
    Env-var override case stays lenient (the user explicitly set it; let
    them shoot their foot).
    """
    import os
    forced = os.environ.get("AGENT_FLOW_PROFILE")
    from_kit = _read_kit_profile(project_root)
    profile_id = forced or from_kit or "generic"
    explicit_fallback = os.environ.get("AGENT_FLOW_FALLBACK_GENERIC") == "1"

    profile_path = kit_root / "profiles" / f"{profile_id}.yaml"
    if not profile_path.exists():
        # Hard error when kit.json says a profile that doesn't exist (typo).
        # Lenient fallback only when explicitly requested via env var or when
        # the resolution path was already "generic" (true unknown setup).
        if forced is None and from_kit and not explicit_fallback:
            raise FileNotFoundError(
                f"profile {profile_id!r} not found at {profile_path}. "
                f"Likely a typo in `.agent-flow/kit.json:profile`. "
                f"Set `AGENT_FLOW_FALLBACK_GENERIC=1` to fall back silently, "
                f"or fix the kit.json value."
            )
        print(
            f"⚠️  profile {profile_id!r} not found at {profile_path}; "
            f"falling back to `generic`.",
            file=__import__("sys").stderr,
        )
        profile_id = "generic"
        profile_path = kit_root / "profiles" / "generic.yaml"

    raw = yaml.safe_load(profile_path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"profile {profile_path}: top-level must be a mapping")
    return profile_id, raw


def _read_kit_profile(project_root: Path) -> str | None:
    kit_json = project_root / ".agent-flow" / "kit.json"
    if not kit_json.exists():
        return None
    try:
        data = json.loads(kit_json.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    p = data.get("profile")
    return p if isinstance(p, str) else None


def _search_lore(project_root: Path, task: str, top_k: int = 5) -> list[Lore]:
    """Find lore entries relevant to the task description.

    Delegates tokenization to `LoreIndex.search_text` so the runner and
    `agent-flow lore search` use the same tokenizer. Tolerant of missing
    memory dir / unparseable files. Suppresses parse-warning at run time
    (the CLI surfaces them via `agent-flow lore list`).
    """
    if not task or not task.strip():
        return []
    lore_dir = project_root / ".agent-flow" / "memory" / "lore"
    index = LoreIndex.load(lore_dir, warn=False)
    if not index.entries:
        return []
    return index.search_text(task, top_k=top_k)
