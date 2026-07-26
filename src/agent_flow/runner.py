"""Phase loop runner.

Reads a workflow YAML, iterates phases, delegates execution to the detected
adapter, and treats artifact files as the state machine. The chain itself is
the enforcement mechanism: a phase only advances when the previous artifact
exists, and the run only ends when all phases complete.

Profile awareness:
  - The active profile selection is detected at install time
    (`kit.json:profile` or `kit.json:profiles`) and re-read on each run.
  - Profile YAML (branching / gates / review_angles / vocabulary / etc) is
    parsed and injected into the prompt envelope so the host AI sees the
    stack-specific knobs as real data, not as text instructions to "look it
    up somewhere". Multi-profile installs inject a composite snapshot with
    each selected profile preserved.

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
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from agent_flow.adapters.auto import detect_adapter
from agent_flow.artifact import (
    create_run,
    mark_inactive,
    read_meta,
    write_meta,
)
from agent_flow.cli_detect import detect_available_clis
from agent_flow.core.commands import run_safe_command
from agent_flow.core.worktree_isolation import (
    assert_leader_unchanged,
    capture_leader_snapshot,
    leader_root_for,
)
from agent_flow.core.phase_workflow import find_kit_root, load_phase_workflow_definition
from agent_flow.core.report import write_run_report
from agent_flow.core.security import ensure_child_path, validate_safe_name
from agent_flow.core.markers import has_failure_markers, missing_markers
from agent_flow.core.local_skills import changed_files, missing_local_skill_markers
from agent_flow.core.skill_resolver import PhaseSkills
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
FIX_LOOP_MAX_ROUNDS = 3
DDD_REQUIRED_DESIGN_SECTIONS = (
    ("bounded context", ("bounded context", "bounded contexts", "context map", "컨텍스트")),
    ("ubiquitous language", ("ubiquitous language", "ubiquitous language terms", "domain language", "보편 언어", "유비쿼터스 언어")),
    ("aggregate", ("aggregate", "aggregates", "aggregate root", "애그리거트")),
    ("entity", ("entity", "entities", "엔티티")),
    ("value object", ("value object", "value objects", "vo", "값 객체")),
    ("domain event", ("domain event", "domain events", "도메인 이벤트")),
    ("domain invariant", ("domain invariant", "domain invariants", "invariant", "invariants", "도메인 불변식", "불변식")),
    ("domain flow", ("domain flow", "domain flows", "domain workflow", "domain workflows", "도메인 흐름")),
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
    artifact: str = ""
    skills: PhaseSkills | None = None


class Runner:
    def __init__(
        self,
        project_root: Path,
        state_root: Path | None = None,
        config_root: Path | None = None,
        workflow: str = "default",
        run_dir: Path | None = None,
        architecture: str = "default",
        next_command: str = "agent-flow continue",
    ) -> None:
        if architecture not in ARCHITECTURE_MODES:
            raise ValueError(f"invalid architecture mode: {architecture!r}")
        self.project_root = project_root
        self.state_root = state_root or project_root
        self.config_root = config_root or project_root
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
        self.profile_id, self.profile = _load_profile(self.kit_root, self.config_root)

    def run(self, mode: ResumeMode, task: str = "") -> None:
        # 첫 `capture_leader_snapshot`보다 먼저 돈다. 뒤에서 돌면 이미 오염된
        # 상태를 tripwire 기준선으로 굳혀 격리 검증 전체가 무의미해진다.
        assert_managed_hooks_registered(self.project_root, self.config_root)
        if mode == ResumeMode.START:
            self.run_dir = create_run(
                self.state_root,
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
        self._adapter_name = adapter.name
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
        adapter._config_root = self.config_root
        adapter._task_text = read_meta(self.run_dir).get("task", "") if self.run_dir else ""
        adapter._changed_files = changed_files(self.project_root)

        # Auto-cite lore: search the local lore index for entries relevant
        # to the task description and inject them into the prompt envelope.
        # Empty list when memory dir is missing or no matches.
        meta_for_lore = read_meta(self.run_dir) if self.run_dir else {}
        adapter._lore_citations = _search_lore(
            self.project_root, meta_for_lore.get("task", ""),
        )

        # 이 실행이 worktree 안이라면 뒤에 있는 leader 체크아웃이 지켜야 할
        # 대상이다. leader에서 그대로 도는 실행은 지킬 바깥 대상이 없다.
        leader_root = leader_root_for(self.project_root)

        assert self.run_dir is not None
        meta = read_meta(self.run_dir)
        phase_index = int(meta.get("phase_index", 0) or 0)
        while phase_index < len(self.phases):
            phase = self.phases[phase_index]
            # 진입 시각은 phase를 **시작할 때** 찍는다. 실행 뒤에 찍으면 방금 쓴
            # artifact가 진입 시각보다 과거가 되어 stale로 오판된다.
            if not meta.get("phase_entered_at"):
                self._stamp_phase(meta, phase_index)
                write_meta(self.run_dir, meta)
            if self._has_artifact(phase):
                artifact = self._existing_artifact_path(phase)
                blocked_reason = (
                    self._stale_artifact_block_reason(artifact, meta)
                    or self._artifact_block_reason(artifact)
                )
                if blocked_reason:
                    print(
                        f"\n═══ phase '{phase.id}' is blocked. "
                        f"{blocked_reason}. Update the artifact, then "
                        f"`{self.next_command}`. ═══"
                    )
                    self._print_structured_status(
                        status="blocked",
                        phase=phase,
                        reason=blocked_reason,
                        required_artifact=artifact,
                    )
                    return
                missing_markers = self._missing_required_markers(phase)
                if missing_markers:
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
                    self._existing_artifact_path(phase).unlink()
                else:
                    print(f"  [skip] {phase.id}")
                    phase_index, blocked = self._next_index(phase_index, phase)
                    meta = read_meta(self.run_dir)
                    self._advance_phase(meta, phase_index, blocked)
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
                self._advance_phase(meta, phase_index, blocked)
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
            leader_before = (
                capture_leader_snapshot(leader_root) if leader_root is not None else None
            )
            completed = adapter.execute(
                phase, run_dir=self.run_dir, project_root=self.project_root,
            )
            if leader_before is not None:
                assert_leader_unchanged(
                    leader_root, leader_before, run_id=self.run_dir.name
                )
            meta = read_meta(self.run_dir)
            self._stamp_phase(meta, phase_index)
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
                    required_artifact=self._artifact_path(phase),
                )
                return
            artifact = self._existing_artifact_path(phase)
            blocked_reason = (
                self._stale_artifact_block_reason(artifact, meta)
                or self._artifact_block_reason(artifact)
            )
            if blocked_reason:
                print(
                    f"\n═══ phase '{phase.id}' is blocked. "
                    f"{blocked_reason}. Update the artifact, then "
                    f"`{self.next_command}`. ═══"
                )
                self._print_structured_status(
                    status="blocked",
                    phase=phase,
                    reason=blocked_reason,
                    required_artifact=artifact,
                )
                return
            missing_markers = self._missing_required_markers(phase)
            if missing_markers:
                artifact = self._existing_artifact_path(phase)
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
                    required_artifact=self._artifact_path(phase),
                )
                return
            phase_index, blocked = self._next_index(phase_index, phase)
            meta = read_meta(self.run_dir)
            self._advance_phase(meta, phase_index, blocked)
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
        artifact = self._existing_artifact_path(phase)
        text = artifact.read_text(encoding="utf-8") if artifact.exists() else ""
        if phase.multi_review:
            key = _multi_review_route_key(text, phase.id)
        elif phase.id == "gates":
            key = _gates_route_key(text)
        else:
            key = _route_key(text)
        if key == "approve" and phase.routes.get("request-changes") and has_failure_markers(text):
            print("  [route] approve overridden to request-changes: Completion Gate has failure markers")
            key = "request-changes"
        target = phase.routes.get(key)
        if target is None:
            target = phase.routes.get("default")
        if phase.multi_review:
            if key == "missing-reviewer":
                print("  [block] multi-review requires 1+ independent sub-agent reviewer verdict")
                return current_index, True
            if key == "insufficient-reviewers":
                print("  [block] multi-review requires 2+ independent sub-agent reviewer verdicts")
                return current_index, True
            if key == "invalid-verdict":
                print("  [block] multi-review requires overall verdict approve or request-changes")
                return current_index, True
            if key == "default":
                print(
                    "  [block] multi-review requires ## Overall with exactly one "
                    "verdict: approve or verdict: request-changes line"
                )
                return current_index, True
        # gates뿐 아니라 final-review/multi-review 등 fix-loop로 라우팅하는
        # 모든 phase에 같은 상한을 적용해야 review ↔ fix-loop가 무한 루프하지 않는다.
        if target == "fix-loop":
            rounds = self._increment_fix_loop_rounds()
            if rounds > FIX_LOOP_MAX_ROUNDS:
                print(f"  [block] fix-loop exceeded {FIX_LOOP_MAX_ROUNDS} rounds")
                return current_index, True
        elif phase.id == "gates" and target and target != "block" and "fix-loop" in phase.routes.values():
            # reviewer approve 이후 QA가 최종 통과할 때만 reset한다. review 재실행
            # 단계에서 reset하면 QA/review loop cap이 무력화된다.
            self._reset_fix_loop_rounds()
        if target == "block":
            print(f"  [block] {phase.id} status={key}")
            return current_index, True
        if target is None:
            print(f"  [block] {phase.id} status={key} has no route")
            return current_index, True
        if target:
            for i, candidate in enumerate(self.phases):
                if candidate.id == target:
                    if i <= current_index:
                        for stale_phase in self.phases[i:current_index + 1]:
                            stale_artifact = self._existing_artifact_path(stale_phase)
                            if stale_artifact.exists():
                                stale_artifact.unlink()
                    elif i > current_index + 1:
                        for skipped in self.phases[current_index + 1:i]:
                            skipped_artifact = self._artifact_path(skipped)
                            if not skipped_artifact.exists():
                                skipped_artifact.parent.mkdir(parents=True, exist_ok=True)
                                skipped_artifact.write_text(
                                    f"# {skipped.id}\n\nstatus: skipped\nreason: route_to_{target}\n",
                                    encoding="utf-8",
                                )
                    return i, False
            raise ValueError(f"phase {phase.id}: route target not found: {target}")
        return current_index + 1, False

    def _advance_phase(self, meta: dict[str, Any], phase_index: int, blocked: bool) -> None:
        """route 결과를 meta에 반영한다.

        `blocked`면 제자리에 멈춘 것이므로 진입이 아니다. 여기서 시각을 밀면
        방금 쓴 artifact가 진입 시각보다 과거가 되어 다음 실행이 진짜 사유
        (route_blocked) 대신 stale_artifact를 보고한다.
        """
        self._stamp_phase(meta, phase_index, entering=not blocked)

    def _stamp_phase(
        self, meta: dict[str, Any], phase_index: int, *, entering: bool = False
    ) -> None:
        """meta에 현재 phase와 **진입 시각**을 박는다.

        `phase_entered_at`이 없으면 읽음 증거를 과거 기록까지 소급 인정하게 되어
        L2 강제가 통째로 무력해진다. 그래서 phase를 실제로 **새로 시작**할 때는
        갱신한다 — 같은 phase로 되도는 self-loop도 새 라운드이므로 지난 라운드의
        읽음 기록을 물려받으면 안 된다.

        반대로 route가 막혀(`blocked`) 제자리에 멈춘 경우는 진입이 아니다.
        여기서 시각을 밀면 방금 쓴 artifact가 진입 시각보다 과거가 되어 다음
        실행이 진짜 사유(route_blocked) 대신 stale_artifact를 보고한다.
        """
        phase_id = (
            self.phases[phase_index].id if phase_index < len(self.phases) else None
        )
        if entering or meta.get("current_phase") != phase_id or not meta.get("phase_entered_at"):
            meta["phase_entered_at"] = datetime.now(timezone.utc).isoformat()
        meta["phase_index"] = phase_index
        meta["current_phase"] = phase_id

    def _increment_fix_loop_rounds(self) -> int:
        assert self.run_dir is not None
        meta = read_meta(self.run_dir)
        rounds = _fix_loop_rounds(meta) + 1
        if rounds > FIX_LOOP_MAX_ROUNDS:
            return rounds
        # gates 실패 루프는 run meta에 저장해서 재시작 후에도 상한을 유지한다.
        meta["fix_loop_rounds"] = rounds
        write_meta(self.run_dir, meta)
        return rounds

    def _reset_fix_loop_rounds(self) -> None:
        assert self.run_dir is not None
        meta = read_meta(self.run_dir)
        if "fix_loop_rounds" not in meta:
            return
        meta.pop("fix_loop_rounds", None)
        write_meta(self.run_dir, meta)

    def _write_automatic_artifact(self, phase: Phase) -> bool:
        assert self.run_dir is not None
        if phase.id in GIT_DEPENDENT_PHASES and not _is_git_repo(self.project_root):
            artifact = self._artifact_path(phase)
            artifact.parent.mkdir(parents=True, exist_ok=True)
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
                artifact = self._artifact_path(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
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
        artifact = self._existing_artifact_path(phase)
        text = artifact.read_text(encoding="utf-8") if artifact.exists() else ""
        if _route_key(text) != "blocked":
            return False
        print(f"  [recheck] {phase.id} status=blocked")
        return True

    def _missing_required_markers(self, phase: Phase) -> list[str]:
        if (
            getattr(self, "_adapter_name", "") == "generic"
            and os.environ.get("AGENT_FLOW_GENERIC_MODE") == "stub-success"
        ):
            return []
        assert self.run_dir is not None
        artifact = self._existing_artifact_path(phase)
        if not artifact.exists():
            return []
        text = artifact.read_text(encoding="utf-8")
        missing = list(_missing_markers(text, phase.required_markers))
        meta = read_meta(self.run_dir)
        missing.extend(
            missing_local_skill_markers(
                text,
                self.config_root,
                phase.id,
                phase_skills=phase.skills,
                profile=self.profile,
                changed_files=changed_files(self.project_root),
                task_text=str(meta.get("task", "")),
                since=_meta_timestamp(meta.get("phase_entered_at")),
            )
        )
        return missing

    def _artifact_path(self, phase: Phase) -> Path:
        assert self.run_dir is not None
        return self.run_dir / (phase.artifact or f"{phase.id}.md")

    def _legacy_artifact_path(self, phase: Phase) -> Path:
        assert self.run_dir is not None
        return self.run_dir / f"{phase.id}.md"

    def _existing_artifact_path(self, phase: Phase) -> Path:
        artifact = self._artifact_path(phase)
        if artifact.exists():
            return artifact
        legacy_artifact = self._legacy_artifact_path(phase)
        if legacy_artifact.exists():
            return legacy_artifact
        return artifact

    def _artifact_block_reason(self, artifact: Path) -> str | None:
        if not artifact.exists():
            return None
        text = artifact.read_text(encoding="utf-8")
        if (
            "_stub artifact written by GenericAdapter (stub mode)._" in text
            and os.environ.get("AGENT_FLOW_GENERIC_MODE") != "stub-success"
        ):
            return "generic_stub_artifact"
        return None

    def _stale_artifact_block_reason(self, artifact: Path, meta: dict[str, Any]) -> str | None:
        entered_at = _meta_timestamp(
            meta.get("phase_entered_at")
            or meta.get("updated_at")
            or meta.get("started_at")
        )
        if entered_at is None:
            return None
        try:
            artifact_mtime = artifact.stat().st_mtime
        except FileNotFoundError:
            return None
        if artifact_mtime < entered_at:
            return "stale_artifact"
        return None

    def _has_artifact(self, phase: Phase) -> bool:
        return self._artifact_path(phase).exists() or self._legacy_artifact_path(phase).exists()

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
    # artifact.py의 marker 검증과 같은 kit root를 봐야 routing/검증 YAML이 갈라지지 않는다.
    return find_kit_root()


def _load_workflow(kit_root: Path, name: str) -> list[Phase]:
    definition = load_phase_workflow_definition(kit_root, name)
    return [
        Phase(
            id=phase.id,
            description=phase.description,
            prompt=phase.prompt,
            pause_after=phase.pause_after,
            optional=phase.optional,
            multi_review=phase.multi_review,
            cite_lore=phase.cite_lore,
            routes=phase.routes,
            required_markers=phase.required_markers,
            artifact=phase.artifact,
            skills=phase.skills,
        )
        for phase in definition.phases
    ]


def _fix_loop_rounds(meta: dict[str, Any]) -> int:
    raw = meta.get("fix_loop_rounds", 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


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


def _route_key(text: str) -> str:
    lowered = text.lower()
    # gates 결과 JSON은 nested result가 아니라 top-level passed만 route source로 본다.
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("passed"), bool):
        results = payload.get("results")
        if payload["passed"] is True:
            if _gate_results_prove_pass(results):
                return "green"
            return "default"
        return "request-changes"
    checks = (
        "blocked",
        "request-changes",
        "ci-failed",
        "ci_failed",
        "comments",
        "has_comments",
        "skipped",
        "pending",
        "green",
        "approve",
        "merged",
        "closed",
        "error",
    )
    for line in lowered.splitlines():
        match = re.match(r"^(?:verdict|status):\s*([a-z_-]+)\s*$", line)
        if not match:
            continue
        key = match.group(1)
        if key in checks:
            return key
    return "default"


def _gates_route_key(text: str) -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return "default"
    if not isinstance(payload, dict) or not isinstance(payload.get("passed"), bool):
        return "default"
    status = payload.get("status")
    if isinstance(status, str):
        normalized_status = status.strip().lower().replace("_", "-")
        if payload["passed"] is True and normalized_status in {"green", "approve"}:
            return normalized_status if _gate_results_prove_pass(payload.get("results")) else "default"
        if payload["passed"] is False and normalized_status in {"request-changes", "blocked", "error", "pending"}:
            return normalized_status
    if payload["passed"] is True:
        return "green" if _gate_results_prove_pass(payload.get("results")) else "default"
    return "request-changes"


def _multi_review_route_key(text: str, phase_id: str = "") -> str:
    verdicts = _independent_reviewer_verdicts(text)
    overall = _multi_review_overall_route_key(text)
    if not verdicts:
        return "missing-reviewer"
    if overall == "invalid-verdict":
        return "invalid-verdict"
    if "request-changes" in verdicts.values() or overall == "request-changes":
        return "request-changes"
    if len(verdicts) < 2:
        return "insufficient-reviewers"
    if overall == "default":
        return "default"
    has_subagent = _has_subagent_reviewer(text)
    if overall == "approve" and has_subagent and len(verdicts) >= 2:
        return "approve"
    return "invalid-verdict"


def _gate_results_prove_pass(results: object) -> bool:
    if not isinstance(results, list) or not results:
        return False
    required_seen = False
    for result in results:
        if not isinstance(result, dict):
            return False
        if result.get("required") is False:
            continue
        required_seen = True
        command = result.get("command")
        if not isinstance(command, str) or not command.strip():
            return False
        if not _gate_result_has_evidence(result):
            return False
        if not (result.get("passed") is True or result.get("status") in {"pass", "ok"}):
            return False
    return required_seen


def _gate_result_has_evidence(result: dict[str, object]) -> bool:
    for key in ("output", "stdout", "stderr", "artifact", "path"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return True
    for key in ("exit_code", "exitCode"):
        value = result.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value == 0:
            return True
    return False


def _multi_review_overall_route_key(text: str) -> str:
    in_overall_section = False
    verdicts: list[str] = []
    overall_sections = 0
    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        heading = re.match(r"^(#{1,6})\s+(.+)$", lowered)
        if heading:
            title = heading.group(2)
            # reviewer 파서와 같은 heading alias(overall/final [verdict])를 인정한다.
            in_overall_section = bool(
                heading.group(1) == "##"
                and re.fullmatch(r"(?:overall|final)(?:\s+verdict)?", title.strip())
            )
            if in_overall_section:
                overall_sections += 1
            continue
        if not in_overall_section:
            continue
        match = re.match(r"^verdict:\s*(approve|request-changes)\s*$", stripped)
        if not match:
            continue
        verdicts.append(match.group(1))
    if overall_sections > 1:
        return "invalid-verdict"
    if not verdicts:
        return "default"
    if len(verdicts) != 1:
        return "invalid-verdict"
    return verdicts[0]


def _independent_reviewer_verdict_count(text: str) -> int:
    return len(_independent_reviewer_verdicts(text))


def _independent_reviewer_verdicts(text: str) -> dict[str, str]:
    reviewers: dict[str, dict[str, object]] = {}
    current_reviewer: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if re.match(r"^##\s*(?:overall|final)(?:\s+verdict)?\s*$", lowered):
                current_reviewer = None
                continue
            heading = re.match(r"^##\s*reviewer\s+(.+)$", lowered)
            if heading:
                key = _normalized_reviewer_heading_id(heading.group(1))
                current_reviewer = key or None
            continue
        source_match = re.match(
            r"^(reviewer[-_ ]?[a-z0-9-]+)\s+reviewer[-_ ]?source:\s*(.+)$",
            lowered,
        )
        if source_match:
            key = _normalized_reviewer_id(source_match.group(1))
            if key:
                state = reviewers.setdefault(key, {"has_source": False, "subagent": False, "verdict": None})
                state["has_source"] = True
                if _is_subagent_source(source_match.group(2)):
                    state["subagent"] = True
            continue
        if current_reviewer is not None and _line_marks_subagent_source(lowered):
            state = reviewers.setdefault(current_reviewer, {"has_source": False, "subagent": False, "verdict": None})
            state["has_source"] = True
            state["subagent"] = True
            continue
        if current_reviewer is not None and _line_marks_non_subagent_source(lowered):
            reviewers.setdefault(current_reviewer, {"has_source": True, "subagent": False, "verdict": None})
            continue
        if "verdict:" not in lowered:
            continue
        verdict_match = re.match(r"^(.*?)verdict:\s*(approve|request-changes)\s*$", stripped)
        if not verdict_match:
            continue
        prefix = verdict_match.group(1).strip(" -").lower()
        verdict = verdict_match.group(2)
        if prefix in {"overall", "overall verdict", "final", "final verdict"}:
            continue
        if prefix:
            key = _normalized_reviewer_id(prefix)
            if key:
                reviewers.setdefault(key, {"has_source": False, "subagent": False, "verdict": None})["verdict"] = verdict
        elif current_reviewer is not None:
            reviewers.setdefault(current_reviewer, {"has_source": False, "subagent": False, "verdict": None})["verdict"] = verdict
    return {
        reviewer: str(state["verdict"])
        for reviewer, state in reviewers.items()
        if state["verdict"] and state["subagent"]
    }


def _line_marks_subagent_source(value: str) -> bool:
    source_match = re.search(r"reviewer[-_ ]?source\s*:\s*(.+)$", value)
    return bool(source_match and _is_subagent_source(source_match.group(1)))


def _line_marks_non_subagent_source(value: str) -> bool:
    source_match = re.search(r"reviewer[-_ ]?source\s*:\s*(.+)$", value)
    return bool(source_match and not _is_subagent_source(source_match.group(1)))


def _has_subagent_reviewer(text: str) -> bool:
    return any(_line_marks_subagent_source(line.strip().lower()) for line in text.splitlines())


def _is_subagent_source(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return normalized in {
        "sub agent",
        "subagent",
        "host sub agent",
        "host subagent",
        "active host sub agent",
        "active host subagent",
    }


def _reviewer_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return normalized or value


def _normalized_reviewer_id(value: str) -> str:
    # 섹션 라벨과 종합 verdict는 독립 reviewer id로 세지 않는다.
    key = _reviewer_key(value)
    key = re.sub(r"^reviewer\b", "", key).strip()
    generic_labels = {
        "verdict",
        "verdicts",
        "overall",
        "final",
        "summary",
        "review",
        "reviews",
        "feedback",
        "report",
        "reports",
        "assessment",
        "assessments",
        "analysis",
        "analyses",
        "decision",
        "decisions",
        "conclusion",
        "conclusions",
        "status",
        "statuses",
        "approval",
        "approvals",
        "note",
        "notes",
        "finding",
        "findings",
        "comment",
        "comments",
        "output",
        "outputs",
        "result",
        "results",
        "scope",
        "check",
        "checks",
        "checklist",
        "details",
        "detail",
    }
    if not key or any(part in generic_labels for part in key.split()):
        return ""
    return key


def _normalized_reviewer_heading_id(value: str) -> str:
    # Reviewer heading은 1-2 단어 id(claude, agent 1 등)만 독립 id로 인정한다.
    # 긴 서술형 heading은 reviewer가 아니라 prose일 가능성이 높아 제외한다.
    key = _normalized_reviewer_id(value)
    if re.fullmatch(r"[a-z0-9]+(?: [a-z0-9]+)?", key):
        return key
    return ""


def _missing_markers(text: str, markers: tuple[str, ...]) -> list[str]:
    return missing_markers(text, markers)


def _status_value(value: object) -> str:
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def _is_git_repo(project_root: Path) -> bool:
    result = run_safe_command(["git", "rev-parse", "--is-inside-work-tree"], cwd=project_root, timeout_s=5)
    return result.ok and result.stdout.strip() == "true"


def _missing_ddd_design_terms(run_dir: Path) -> list[str]:
    candidates = [run_dir / "ddd-design.md", run_dir / "design.md"]
    text = ""
    for candidate in candidates:
        if candidate.exists():
            text = candidate.read_text(encoding="utf-8")
            break
    if not text:
        return ["ddd-design.md or design.md"]

    section_titles = _design_section_titles(text)
    if any(_section_title_matches(section_titles, alias) for alias in ("service-layer refactor", "service layer refactor")):
        return ["ddd mode cannot be service-layer refactor"]
    return [
        label
        for label, aliases in DDD_REQUIRED_DESIGN_SECTIONS
        if not any(_section_title_matches(section_titles, alias) for alias in aliases)
    ]


def _design_section_titles(text: str) -> list[str]:
    titles: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```") or line.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or line.startswith("    ") or line.startswith("\t"):
            continue
        stripped = line.strip()
        # DDD 판정은 Markdown heading과 list label만 본다. 본문 문장은 relay를 막지 않는다.
        heading = re.match(r"^#{1,6}\s+(.+)$", stripped)
        if heading:
            titles.append(_normalize_design_heading(heading.group(1)))
            continue
        label = re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)(?:\*\*)?([^:]{1,80}?)(?:\*\*)?\s*:", stripped)
        if label:
            titles.append(_normalize_design_heading(label.group(1)))
    return titles


def _normalize_design_heading(value: str) -> str:
    lowered = value.lower()
    cleaned = re.sub(r"[`*_#]+", " ", lowered)
    cleaned = re.sub(r"^\s*\d+(?:[.)]|\s+-)\s*", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" :-")


def _section_title_matches(section_titles: list[str], alias: str) -> bool:
    normalized_alias = _normalize_design_heading(alias)
    return any(title == normalized_alias or title.startswith(f"{normalized_alias} ") for title in section_titles)


def _load_profile(kit_root: Path, project_root: Path) -> tuple[str, dict[str, Any]]:
    """Return (profile_id, profile_dict).

    Resolution order:
      1. `AGENT_FLOW_PROFILE` env override (always wins; user opted in)
      2. `.agent-flow/kit.json:profiles` written by filtered installer
      3. `.agent-flow/kit.json:profile` written by the installer
      4. fall back to "generic"

    A typo in `kit.json:profile(s)` would otherwise run the entire workflow
    against the wrong stack (wrong branching, gates, PR target) — a
    correctness bug, not a degraded mode. So we treat that case as a hard
    error unless `AGENT_FLOW_FALLBACK_GENERIC=1` opts into silent fallback.
    Env-var override case stays lenient (the user explicitly set it; let
    them shoot their foot).
    """
    import os
    forced = os.environ.get("AGENT_FLOW_PROFILE")
    explicit_fallback = os.environ.get("AGENT_FLOW_FALLBACK_GENERIC") == "1"
    if forced:
        return _load_single_profile(
            kit_root,
            forced,
            strict_missing=False,
            explicit_fallback=explicit_fallback,
            source="AGENT_FLOW_PROFILE",
        )

    from_kit_profiles = _read_kit_profiles(project_root)
    if from_kit_profiles:
        return _load_profile_union(
            kit_root,
            from_kit_profiles,
            explicit_fallback=explicit_fallback,
        )

    from_kit = _read_kit_profile(project_root)
    profile_id = from_kit or "generic"
    return _load_single_profile(
        kit_root,
        profile_id,
        strict_missing=bool(from_kit),
        explicit_fallback=explicit_fallback,
        source=".agent-flow/kit.json:profile" if from_kit else "default",
    )


def _load_single_profile(
    kit_root: Path,
    profile_id: str,
    *,
    strict_missing: bool,
    explicit_fallback: bool,
    source: str,
) -> tuple[str, dict[str, Any]]:
    _validate_yaml_name(profile_id, "profile")

    profile_path = kit_root / "profiles" / f"{profile_id}.yaml"
    _ensure_child_path(kit_root / "profiles", profile_path, "profile")
    if not profile_path.exists():
        # Hard error when kit.json says a profile that doesn't exist (typo).
        # Lenient fallback only when explicitly requested via env var or when
        # the resolution path was already "generic" (true unknown setup).
        if strict_missing and not explicit_fallback:
            raise FileNotFoundError(
                f"profile {profile_id!r} not found at {profile_path}. "
                f"Likely a typo in `{source}`. "
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


def _load_profile_union(
    kit_root: Path,
    profile_ids: list[str],
    *,
    explicit_fallback: bool,
) -> tuple[str, dict[str, Any]]:
    loaded: list[tuple[str, dict[str, Any]]] = []
    for profile_id in profile_ids:
        loaded.append(
            _load_single_profile(
                kit_root,
                profile_id,
                strict_missing=True,
                explicit_fallback=explicit_fallback,
                source=".agent-flow/kit.json:profiles",
            )
        )
    deduped = _dedupe_loaded_profiles(loaded)
    if not deduped:
        return _load_single_profile(
            kit_root,
            "generic",
            strict_missing=False,
            explicit_fallback=explicit_fallback,
            source="default",
        )
    if len(deduped) == 1:
        return deduped[0]
    active_ids = [profile_id for profile_id, _ in deduped]
    return ",".join(active_ids), {
        "id": "multi-profile",
        "active_profiles": active_ids,
        "profiles": [profile for _, profile in deduped],
        "review_angles": _merge_profile_list_field(deduped, "review_angles"),
        "gates": _merge_profile_list_field(deduped, "gates"),
        "skills": {
            profile_id: profile.get("skills", {})
            for profile_id, profile in deduped
            if isinstance(profile.get("skills"), dict)
        },
        "architecture": {
            profile_id: profile.get("architecture")
            for profile_id, profile in deduped
            if isinstance(profile.get("architecture"), dict)
        },
    }


def _dedupe_loaded_profiles(profiles: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    deduped: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for profile_id, profile in profiles:
        if profile_id in seen:
            continue
        seen.add(profile_id)
        deduped.append((profile_id, profile))
    return deduped


def _merge_profile_list_field(profiles: list[tuple[str, dict[str, Any]]], field: str) -> list[Any]:
    merged: list[Any] = []
    seen: set[str] = set()
    for _, profile in profiles:
        values = profile.get(field)
        if not isinstance(values, list):
            continue
        for value in values:
            key = json.dumps(value, sort_keys=True, ensure_ascii=False)
            if key in seen:
                continue
            seen.add(key)
            merged.append(value)
    return merged


def _read_kit_profile(project_root: Path) -> str | None:
    data = _read_kit_json(project_root)
    p = data.get("profile")
    return p if isinstance(p, str) else None


def _read_kit_profiles(project_root: Path) -> list[str]:
    data = _read_kit_json(project_root)
    profiles = data.get("profiles")
    if not isinstance(profiles, list):
        return []
    deduped: list[str] = []
    seen: set[str] = set()
    for profile in profiles:
        if isinstance(profile, str) and profile and profile not in seen:
            deduped.append(profile)
            seen.add(profile)
    return deduped


def _read_kit_json(project_root: Path) -> dict[str, Any]:
    kit_json = project_root / ".agent-flow" / "kit.json"
    if not kit_json.exists():
        return {}
    try:
        data = json.loads(kit_json.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


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
