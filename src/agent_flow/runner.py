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
import re
import subprocess
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
from agent_flow.core.report import write_run_report
from agent_flow.core.security import ensure_child_path, validate_safe_name
from agent_flow.core.markers import missing_markers, normalize_required_markers
from agent_flow.memory.index import LoreIndex
from agent_flow.memory.lore import Lore


ARCHITECTURE_MODES = {"default", "ddd", "service-layer"}
GIT_DEPENDENT_PHASES = {
    "commit",
    "push-pr",
    "pr-watch",
    "pr-comment-fix",
    "pr-ci-fix",
    "merge",
    "merge-approval",
}
DDD_REQUIRED_DESIGN_SECTIONS = (
    ("bounded context", ("bounded context", "bounded contexts", "context map", "컨텍스트")),
    ("aggregate", ("aggregate", "aggregates", "aggregate root", "애그리거트")),
    ("entity", ("entity", "entities", "엔티티")),
    ("value object", ("value object", "value objects", "vo", "값 객체")),
    ("application use case", ("application use case", "application use cases", "use case", "use cases", "interactor", "interactors", "유스케이스")),
    ("infrastructure adapter", ("infrastructure adapter", "infrastructure adapters", "ports and adapters", "repository adapter", "adapter", "어댑터")),
    ("presentation route", ("presentation route", "presentation routes", "presentation", "route", "routes", "view", "views", "화면", "라우트")),
    ("dependency rule", ("dependency rule", "dependency direction", "dependency flow", "의존성")),
    ("implementation structure", ("implementation structure", "file structure", "module structure", "package structure", "folder structure", "구현 구조", "모듈 구조", "패키지 구조")),
)


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
    required_markers: tuple[str, ...] = ()


class Runner:
    def __init__(
        self,
        project_root: Path,
        workflow: str = "default",
        run_dir: Path | None = None,
        architecture: str = "default",
        next_command: str = "agent-flow continue",
    ) -> None:
        if architecture not in ARCHITECTURE_MODES:
            raise ValueError(f"invalid architecture mode: {architecture!r}")
        self.project_root = project_root
        self.workflow_name = workflow
        self.run_dir = run_dir
        self.architecture = architecture
        self.next_command = next_command
        self.kit_root = _find_kit_root()
        if run_dir is not None:
            meta = read_meta(run_dir)
            self.workflow_name = meta.get("workflow", workflow)
            self.architecture = meta.get("architecture", architecture)
        self.phases = _load_workflow(self.kit_root, self.workflow_name)
        self.profile_id, self.profile = _load_profile(self.kit_root, project_root)

    def run(self, mode: ResumeMode, task: str = "") -> None:
        if mode == ResumeMode.START:
            self.run_dir = create_run(
                self.project_root,
                self.workflow_name,
                task,
                architecture=self.architecture,
            )
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
        print(f"▶ architecture: {self.architecture}")
        print(f"▶ workflow   : {self.workflow_name} ({len(self.phases)} phases)\n")

        # Inject profile snapshot into adapter so render_envelope can include
        # it. Both attributes are declared on the Adapter base class, so this
        # is plain instance-attribute assignment.
        adapter._profile_snapshot = self.profile
        adapter._profile_id = self.profile_id
        adapter._architecture = self.architecture

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
                missing_markers = self._missing_required_markers(phase)
                if missing_markers:
                    artifact = self.run_dir / f"{phase.id}.md"
                    print(
                        f"\n═══ phase '{phase.id}' is blocked. "
                        f"Artifact is missing completion markers: "
                        f"{', '.join(missing_markers)}. "
                        f"Update the artifact, then `{self.next_command}`. ═══"
                    )
                    self._print_structured_status(
                        status="blocked",
                        phase=phase,
                        reason="missing_completion_markers",
                        required_artifact=artifact,
                    )
                    return
                if self._artifact_needs_auto_revalidation(phase):
                    (self.run_dir / f"{phase.id}.md").unlink()
                else:
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
                            f"`{self.next_command}`. ═══"
                        )
                        self._print_structured_status(
                            status="blocked",
                            phase=phase,
                            reason="route_blocked",
                        )
                        return
                    continue
            if self._write_automatic_artifact(phase):
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
                        f"`{self.next_command}`. ═══"
                    )
                    self._print_structured_status(
                        status="blocked",
                        phase=phase,
                        reason="route_blocked",
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
                    f"Write artifact → `{self.next_command}`. ═══"
                )
                self._print_structured_status(
                    status="awaiting_host",
                    phase=phase,
                    reason="missing_phase_artifact",
                    required_artifact=self.run_dir / f"{phase.id}.md",
                )
                return
            missing_markers = self._missing_required_markers(phase)
            if missing_markers:
                artifact = self.run_dir / f"{phase.id}.md"
                print(
                    f"\n═══ phase '{phase.id}' is blocked. "
                    f"Artifact is missing completion markers: "
                    f"{', '.join(missing_markers)}. "
                    f"Update the artifact, then `{self.next_command}`. ═══"
                )
                self._print_structured_status(
                    status="blocked",
                    phase=phase,
                    reason="missing_completion_markers",
                    required_artifact=artifact,
                )
                return
            if phase.pause_after:
                print(
                    f"\n═══ pause: '{phase.id}' 결과 검토 후 "
                    f"`{self.next_command}` ═══"
                )
                self._print_structured_status(
                    status="blocked",
                    phase=phase,
                    reason="pause_after",
                    required_artifact=self.run_dir / f"{phase.id}.md",
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
                    f"`{self.next_command}`. ═══"
                )
                self._print_structured_status(
                    status="blocked",
                    phase=phase,
                    reason="route_blocked",
                )
                return

        report_path = write_run_report(self.run_dir)
        mark_inactive(self.run_dir)
        print("\n✓ run complete.")
        self._print_structured_status(
            status="complete",
            phase=None,
            reason="workflow_complete",
            report=report_path,
        )

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

    def _write_automatic_artifact(self, phase: Phase) -> bool:
        assert self.run_dir is not None
        if phase.id in GIT_DEPENDENT_PHASES and not _is_git_repo(self.project_root):
            artifact = self.run_dir / f"{phase.id}.md"
            artifact.write_text(
                f"# {phase.id}\n\n"
                "status: skipped\n"
                "reason: project root is not a git repository\n",
                encoding="utf-8",
            )
            print(f"  [skip] {phase.id} status=skipped (not a git repository)")
            return True
        if self.architecture == "ddd" and phase.id == "architecture-review":
            missing = _missing_ddd_design_terms(self.run_dir)
            if missing:
                artifact = self.run_dir / f"{phase.id}.md"
                artifact.write_text(
                    "# architecture-review\n\n"
                    "verdict: blocked\n"
                    "status: failed\n\n"
                    "The DDD design artifact is missing required language-agnostic sections:\n"
                    + "".join(f"- `{term}`\n" for term in missing),
                    encoding="utf-8",
                )
                print(
                    "  [fail] architecture-review missing DDD design terms: "
                    + ", ".join(missing)
                )
                return True
        return False

    def _artifact_needs_auto_revalidation(self, phase: Phase) -> bool:
        if self.architecture != "ddd" or phase.id != "architecture-review":
            return False
        assert self.run_dir is not None
        artifact = self.run_dir / f"{phase.id}.md"
        text = artifact.read_text(encoding="utf-8") if artifact.exists() else ""
        if _route_key(text) != "blocked":
            return False
        print(f"  [recheck] {phase.id} status=blocked")
        return True

    def _missing_required_markers(self, phase: Phase) -> list[str]:
        if not phase.required_markers:
            return []
        assert self.run_dir is not None
        artifact = self.run_dir / f"{phase.id}.md"
        if not artifact.exists():
            return []
        return _missing_markers(
            artifact.read_text(encoding="utf-8"),
            phase.required_markers,
        )

    def _print_structured_status(
        self,
        *,
        status: str,
        phase: Phase | None,
        reason: str,
        required_artifact: Path | None = None,
        report: Path | None = None,
    ) -> None:
        assert self.run_dir is not None
        meta = read_meta(self.run_dir)
        required_artifact_text = str(required_artifact) if required_artifact is not None else None
        report_text = str(report) if report is not None else None
        next_command = "none" if status == "complete" else self.next_command
        payload = {
            "status": status,
            "run": f"{self.workflow_name}/{self.run_dir.name}",
            "task": meta.get("task", ""),
            "current_phase": phase.id if phase is not None else "-",
            "reason": reason,
            "required_artifact": required_artifact_text,
            "report": report_text,
            "next_command": next_command,
        }
        print(f"status: {_status_value(status)}")
        print(f"run: {_status_value(payload['run'])}")
        print(f"task: {_status_value(payload['task'])}")
        print(f"current_phase: {phase.id if phase is not None else '-'}")
        print(f"reason: {_status_value(reason)}")
        if required_artifact is not None:
            print(f"required_artifact: {_status_value(required_artifact_text)}")
        if report is not None:
            print(f"report: {_status_value(report_text)}")
        print(f"next_command: {_status_value(next_command)}")
        print(f"status_json: {json.dumps(payload, sort_keys=True)}")


def _find_kit_root() -> Path:
    """Locate the agent-flow kit root (contains workflows/ and profiles/)."""
    here = Path(__file__).resolve()
    candidates: list[Path] = []
    for parent in here.parents:
        if (parent / "workflows").is_dir() and (parent / "profiles").is_dir():
            candidates.append(parent)
    for candidate in candidates:
        if (candidate / "pyproject.toml").is_file() or (candidate / "package.json").is_file():
            return candidate
    if candidates:
        return candidates[0]
    raise RuntimeError("Could not locate agent-flow kit root from " + str(here))


def _load_workflow(kit_root: Path, name: str) -> list[Phase]:
    _validate_yaml_name(name, "workflow")
    path = kit_root / "workflows" / f"{name}.yaml"
    _ensure_child_path(kit_root / "workflows", path, "workflow")
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
            required_markers=normalize_required_markers(p.get("required_markers")),
        ))
    return out

def _route_key(text: str) -> str:
    lowered = text.lower()
    aliases = {
        "has_comments": "comments",
        "has-comments": "comments",
        "ci_failed": "ci-failed",
    }
    for raw, canonical in aliases.items():
        if f"verdict: {raw}" in lowered or f"status: {raw}" in lowered:
            return canonical
    checks = (
        "request-changes",
        "blocked",
        "ci-failed",
        "comments",
        "skipped",
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


def _missing_markers(text: str, markers: tuple[str, ...]) -> list[str]:
    return missing_markers(text, markers)


def _status_value(value: object) -> str:
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def _is_git_repo(project_root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _missing_ddd_design_terms(run_dir: Path) -> list[str]:
    candidates = [run_dir / "ddd-design.md", run_dir / "design.md"]
    text = ""
    for candidate in candidates:
        if candidate.exists():
            text = candidate.read_text(encoding="utf-8")
            break
    if not text:
        return ["ddd-design.md or design.md"]

    lowered = text.lower()
    if "service-layer refactor" in lowered:
        return ["ddd mode cannot be service-layer refactor"]

    section_titles = _design_section_titles(text)
    return [
        label
        for label, aliases in DDD_REQUIRED_DESIGN_SECTIONS
        if not any(_section_title_matches(section_titles, alias) for alias in aliases)
    ]


def _design_section_titles(text: str) -> list[str]:
    titles: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        heading = re.match(r"^#{1,6}\s+(.+)$", stripped)
        if heading:
            titles.append(_normalize_design_heading(heading.group(1)))
            continue
        label = re.match(r"^(?:[-*]\s*)?(?:\d+[.)]\s*)?([^:]{1,80}):", stripped)
        if label:
            titles.append(_normalize_design_heading(label.group(1)))
    return titles


def _normalize_design_heading(value: str) -> str:
    lowered = value.lower()
    return re.sub(r"\s+", " ", re.sub(r"[`*_#]+", " ", lowered)).strip()


def _section_title_matches(section_titles: list[str], alias: str) -> bool:
    normalized_alias = _normalize_design_heading(alias)
    return any(normalized_alias in title for title in section_titles)


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
    _validate_yaml_name(profile_id, "profile")
    explicit_fallback = os.environ.get("AGENT_FLOW_FALLBACK_GENERIC") == "1"

    profile_path = kit_root / "profiles" / f"{profile_id}.yaml"
    _ensure_child_path(kit_root / "profiles", profile_path, "profile")
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


def _validate_yaml_name(name: str, kind: str) -> None:
    validate_safe_name(name, kind)


def _ensure_child_path(root: Path, path: Path, kind: str) -> None:
    ensure_child_path(root, path, kind)


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
