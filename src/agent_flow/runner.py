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
    (emit prompt, exit, wait for `agent-flow-python continue`).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from agent_flow.adapters.auto import detect_adapter
from agent_flow.artifact import (
    _pause_approval_command,
    create_run,
    ensure_run_skill_plan_pinned,
    mark_inactive,
    read_meta,
    write_meta,
)
from agent_flow.cli_detect import detect_available_clis
from agent_flow.core.commands import run_safe_command
from agent_flow.core.execution_state_ledger import (
    capture_execution_state,
    initialize_execution_state_ledger,
    observe_execution_state_injection,
    resolve_ledger_mode,
)
from agent_flow.core.phase_workflow import find_kit_root, load_phase_workflow_definition
from agent_flow.core.profiles import (
    active_profile_ids,
    load_project_profile_payload,
    merge_profile_payloads,
    primary_profile_id,
)
from agent_flow.core.report import write_run_report
from agent_flow.core.security import ensure_child_path, validate_safe_name
from agent_flow.core.markers import has_failure_markers, missing_markers
from agent_flow.core.local_skills import missing_local_skill_markers
from agent_flow.core.skill_plan import runtime_changed_files
from agent_flow.core.start_lock import assert_no_install_transaction
from agent_flow.core.lore_snapshot import (
    lore_snapshot_metadata,
    reconcile_lore_snapshot,
    search_lore,
)
from agent_flow.memory.lore import Lore


ARCHITECTURE_MODES = {"default", "ddd", "service-layer"}
FIX_LOOP_MAX_ROUNDS = 3
PENDING_TRANSITION_VERSION = 2
PENDING_TRANSITION_FILE = "transition-pending.json"


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


@dataclass(frozen=True)
class _TransitionPlan:
    current_index: int
    next_index: int
    prospective_fix_loop_rounds: int
    route_key: str
    routed_to: str
    capture_round: int
    capture_fix_loop_rounds: int
    set_fix_loop_rounds: bool = False
    reset_fix_loop_rounds: bool = False


class Runner:
    def __init__(
        self,
        project_root: Path,
        state_root: Path | None = None,
        config_root: Path | None = None,
        workflow: str = "full-feature",
        run_dir: Path | None = None,
        architecture: str = "default",
        next_command: str = "agent-flow-python continue",
        worktree_mode: str = "required",
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
        self.worktree_mode = worktree_mode
        self._pending_transition_plan: _TransitionPlan | None = None
        self._prepared_ledger_prompt: dict[str, object] | None = None
        self.kit_root = _find_kit_root()
        if run_dir is not None:
            meta = read_meta(run_dir)
            self.workflow_name = meta.get("workflow", workflow)
            self.architecture = meta.get("architecture", architecture)
            self.worktree_mode = meta.get("worktree_mode", worktree_mode)
        self.phases = _load_workflow(self.kit_root, self.workflow_name)
        if run_dir is not None:
            _assert_runner_workflow_pinned(meta, self.workflow_name, self.phases)
        self.profile_id, self.profile = _load_profile(self.kit_root, self.config_root)

    def run(
        self,
        mode: ResumeMode,
        task: str = "",
        *,
        approve_pause: bool = False,
    ) -> None:
        if approve_pause and mode == ResumeMode.START:
            raise RuntimeError("blocked: --approve-pause requires an existing paused run")
        if mode == ResumeMode.START:
            raw_ledger_mode = os.environ.get("AGENT_FLOW_LEDGER_MODE")
            experiment_enabled = bool(raw_ledger_mode and raw_ledger_mode.strip())
            ledger_mode = resolve_ledger_mode(raw_ledger_mode)
            ledger_experiment = _ledger_experiment_controls_from_environment()
            lore_meta = _lore_snapshot_metadata(self.config_root, task)
            lore_meta.update(_run_base_snapshot(self.profile, self.project_root))
            lore_meta.update(
                {
                    "ledger_mode": ledger_mode,
                    "experiment_enabled": experiment_enabled,
                    "ledger_experiment": ledger_experiment,
                    "runner_workflow_hash": _runner_workflow_hash(
                        self.workflow_name, self.phases
                    ),
                    "runner_workflow_hash_version": 1,
                    "worktree_mode": self.worktree_mode,
                    **({"phase_revision": 0} if experiment_enabled else {}),
                }
            )
            self.run_dir = create_run(
                self.state_root,
                self.workflow_name,
                task,
                architecture=self.architecture,
                config_root=self.config_root,
                initial_meta=lore_meta,
            )
            try:
                assert_no_install_transaction(self.config_root)
            except RuntimeError:
                shutil.rmtree(self.run_dir)
                self.run_dir = None
                raise
            started_meta = read_meta(self.run_dir)
            try:
                initialized_ledger = initialize_execution_state_ledger(
                    run_dir=self.run_dir,
                    run_id=str(started_meta.get("run_id") or self.run_dir.name),
                    mode=ledger_mode,
                    experiment_enabled=experiment_enabled,
                    task=task,
                    workflow_id=self.workflow_name,
                    workflow_phases=_ledger_workflow_phases(self.phases),
                    base_commit=_optional_meta_string(started_meta.get("base_commit")),
                    experiment=ledger_experiment,
                    run_snapshot=_ledger_observed_run_snapshot(
                        started_meta,
                        self.profile_id,
                        self.profile,
                        self.phases,
                    ),
                )
                if experiment_enabled and initialized_ledger.get("ok") is not True:
                    raise RuntimeError(
                        "execution ledger pilot initialization failed: "
                        + str(initialized_ledger.get("error") or "unknown error")
                    )
            except Exception:
                shutil.rmtree(self.run_dir)
                self.run_dir = None
                raise
            print(f"▶ run started : {self.run_dir.name}")
            print(f"▶ task        : {task}")
        else:
            assert self.run_dir is not None
            meta = ensure_run_skill_plan_pinned(self.run_dir, self.config_root)
            if approve_pause:
                self._validate_pause_approval_request(
                    meta,
                    int(meta.get("phase_index", 0) or 0),
                )
            print(f"▶ resuming    : {self.run_dir.name}")
            print(f"▶ task        : {meta.get('task', '')}")

        assert self.run_dir is not None
        pinned_meta = ensure_run_skill_plan_pinned(self.run_dir, self.config_root)
        _assert_runner_workflow_pinned(
            pinned_meta, self.workflow_name, self.phases
        )

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

        # Auto-cite lore: search the local lore index for entries relevant
        # to the task description and inject them into the prompt envelope.
        # Empty list when memory dir is missing or no matches.
        meta_for_lore, lore_citations = _ensure_lore_snapshot(
            self.run_dir,
            self.config_root,
            self.project_root,
        )
        adapter._task_scope = str(meta_for_lore.get("task") or "")
        pinned_base = meta_for_lore.get("base_commit")
        adapter._base_commit = pinned_base if isinstance(pinned_base, str) and pinned_base else None
        adapter._lore_citations = lore_citations
        adapter._ledger_run_dir = self.run_dir
        adapter._ledger_run_id = str(meta_for_lore.get("run_id") or self.run_dir.name)
        adapter._ledger_mode = _pinned_ledger_mode(meta_for_lore)
        adapter._ledger_experiment_enabled = meta_for_lore.get("experiment_enabled") is True

        if mode == ResumeMode.START and adapter._ledger_experiment_enabled is True:
            initial_phase = self.phases[0]
            try:
                observation = observe_execution_state_injection(
                    run_dir=self.run_dir,
                    run_id=adapter._ledger_run_id,
                    mode=adapter._ledger_mode,
                    experiment_enabled=True,
                    phase=initial_phase,
                    project_root=self.project_root,
                    round=_ledger_prompt_round(meta_for_lore),
                    generated_at=_ledger_now_iso(),
                    prompt_bytes=len(_phase_instruction(initial_phase).encode("utf-8")),
                )
                block = _committed_observation_block(
                    observation,
                    experiment_enabled=True,
                )
            except Exception:
                shutil.rmtree(self.run_dir)
                self.run_dir = None
                raise
            self._prepared_ledger_prompt = {
                "phase_id": initial_phase.id,
                "phase_revision": _phase_revision(meta_for_lore),
                "block": block,
            }

        meta = self._recover_pending_transition(adapter) or read_meta(self.run_dir)
        phase_index = int(meta.get("phase_index", 0) or 0)
        if phase_index < len(self.phases):
            meta = self._record_phase_entry(phase_index)
        while phase_index < len(self.phases):
            phase = self.phases[phase_index]
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
                    if self._pause_after_blocks(
                        phase,
                        artifact,
                        approve_pause=approve_pause,
                    ):
                        return
                    print(f"  [skip] {phase.id}")
                    pause_digest = (
                        hashlib.sha256(artifact.read_bytes()).hexdigest()
                        if phase.pause_after
                        else None
                    )
                    next_index, blocked = self._next_index(
                        phase_index,
                        phase,
                        defer_commit=True,
                    )
                    if not blocked:
                        meta = self._observe_and_commit_pending_transition(
                            adapter
                        )
                        self._clear_pause_after_pending(
                            phase,
                            artifact,
                            artifact_sha256=pause_digest,
                        )
                    phase_index = next_index
                    if blocked:
                        meta = self._record_phase_entry(phase_index)
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
                phase_index, blocked = self._next_index(
                    phase_index,
                    phase,
                    defer_commit=True,
                )
                if blocked:
                    meta = self._record_phase_entry(phase_index)
                else:
                    meta = self._observe_and_commit_pending_transition(adapter)
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
            ensure_run_skill_plan_pinned(self.run_dir, self.config_root)
            prompt_meta = read_meta(self.run_dir)
            adapter._ledger_round = _ledger_prompt_round(prompt_meta)
            prepared_prompt = self._take_prepared_ledger_prompt(
                phase,
                prompt_meta,
            )
            if prepared_prompt is None:
                observation = observe_execution_state_injection(
                    run_dir=self.run_dir,
                    run_id=adapter._ledger_run_id,
                    mode=adapter._ledger_mode,
                    experiment_enabled=adapter._ledger_experiment_enabled,
                    phase=phase,
                    project_root=self.project_root,
                    round=adapter._ledger_round,
                    generated_at=_ledger_now_iso(),
                    prompt_bytes=len(_phase_instruction(phase).encode("utf-8")),
                )
                adapter._ledger_prompt_block = _committed_observation_block(
                    observation,
                    experiment_enabled=adapter._ledger_experiment_enabled,
                )
            else:
                adapter._ledger_prompt_block = prepared_prompt
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
            if self._pause_after_blocks(
                phase,
                artifact,
                approve_pause=approve_pause,
            ):
                return
            pause_digest = (
                hashlib.sha256(artifact.read_bytes()).hexdigest()
                if phase.pause_after
                else None
            )
            next_index, blocked = self._next_index(
                phase_index,
                phase,
                defer_commit=True,
            )
            if not blocked:
                meta = self._observe_and_commit_pending_transition(adapter)
                self._clear_pause_after_pending(
                    phase,
                    artifact,
                    artifact_sha256=pause_digest,
                )
            phase_index = next_index
            if blocked:
                meta = self._record_phase_entry(phase_index)
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

    def _record_phase_entry(
        self,
        phase_index: int,
        *,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert self.run_dir is not None
        next_meta = read_meta(self.run_dir) if meta is None else meta
        next_meta = self._phase_entry_meta(phase_index, next_meta)
        write_meta(self.run_dir, next_meta)
        return next_meta

    def _phase_entry_meta(
        self,
        phase_index: int,
        meta: dict[str, Any],
    ) -> dict[str, Any]:
        next_meta = dict(meta)
        next_phase = self.phases[phase_index].id if phase_index < len(self.phases) else None
        previous_index = int(next_meta.get("phase_index", 0) or 0)
        if previous_index != phase_index or next_meta.get("current_phase") != next_phase:
            next_meta["phase_entered_at"] = _utc_now_iso()
        next_meta["phase_index"] = phase_index
        next_meta["current_phase"] = next_phase
        return next_meta

    def _next_index(
        self,
        current_index: int,
        phase: Phase,
        *,
        defer_commit: bool = False,
    ) -> tuple[int, bool]:
        if defer_commit:
            self._pending_transition_plan = None
        if not phase.routes:
            if phase.multi_review or phase.id == "gates":
                print(f"  [block] {phase.id} requires non-empty routes")
                return current_index, True
            next_index = current_index + 1
            routed_to = (
                self.phases[next_index].id
                if next_index < len(self.phases)
                else "complete"
            )
            meta = read_meta(self.run_dir) if self.run_dir is not None else {}
            self._stage_or_apply_transition(
                _TransitionPlan(
                    current_index=current_index,
                    next_index=next_index,
                    prospective_fix_loop_rounds=_fix_loop_rounds(meta),
                    route_key="sequential",
                    routed_to=routed_to,
                    capture_round=_ledger_capture_round(meta, routed_to),
                    capture_fix_loop_rounds=_fix_loop_rounds(meta),
                ),
                defer_commit=defer_commit,
            )
            return next_index, False
        assert self.run_dir is not None
        artifact = self._existing_artifact_path(phase)
        text = artifact.read_text(encoding="utf-8") if artifact.exists() else ""
        if phase.multi_review:
            key = _multi_review_route_key(text, phase.id)
        elif phase.id == "gates":
            key = _gates_route_key(text)
        else:
            key = _route_key(text, phase.id)
        if key == "invalid-route":
            print(f"  [block] {phase.id} artifact has contradictory or multiple route fields")
            return current_index, True
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
                print("  [block] multi-review has an invalid or ambiguous overall route field")
                return current_index, True
            if key == "default":
                print(
                    "  [block] multi-review requires ## Overall with exactly one "
                    "verdict: approve or verdict: request-changes line"
                )
                return current_index, True
        # gates뿐 아니라 final-review/multi-review 등 fix-loop로 라우팅하는
        # 모든 phase에 같은 상한을 적용해야 review ↔ fix-loop가 무한 루프하지 않는다.
        transition_meta = read_meta(self.run_dir)
        current_fix_loop_rounds = _fix_loop_rounds(transition_meta)
        prospective_fix_loop_rounds = current_fix_loop_rounds
        set_fix_loop_rounds = False
        reset_fix_loop_rounds = False
        if target == "fix-loop":
            rounds = current_fix_loop_rounds + 1
            if rounds > FIX_LOOP_MAX_ROUNDS:
                print(f"  [block] fix-loop exceeded {FIX_LOOP_MAX_ROUNDS} rounds")
                return current_index, True
            prospective_fix_loop_rounds = rounds
            set_fix_loop_rounds = True
        elif phase.id == "gates" and target and target != "block" and "fix-loop" in phase.routes.values():
            # reviewer approve 이후 QA가 최종 통과할 때만 reset한다. review 재실행
            # 단계에서 reset하면 QA/review loop cap이 무력화된다.
            prospective_fix_loop_rounds = 0
            reset_fix_loop_rounds = True
        if target == "block":
            print(f"  [block] {phase.id} status={key}")
            return current_index, True
        if target is None:
            print(f"  [block] {phase.id} status={key} has no route")
            return current_index, True
        next_index = next(
            (index for index, candidate in enumerate(self.phases) if candidate.id == target),
            None,
        )
        if next_index is None:
            raise ValueError(f"phase {phase.id}: route target not found: {target}")
        self._stage_or_apply_transition(
            _TransitionPlan(
                current_index=current_index,
                next_index=next_index,
                prospective_fix_loop_rounds=prospective_fix_loop_rounds,
                route_key=key,
                routed_to=target,
                capture_round=_ledger_capture_round(transition_meta, target),
                capture_fix_loop_rounds=prospective_fix_loop_rounds,
                set_fix_loop_rounds=set_fix_loop_rounds,
                reset_fix_loop_rounds=reset_fix_loop_rounds,
            ),
            defer_commit=defer_commit,
        )
        return next_index, False

    def _stage_or_apply_transition(
        self,
        plan: _TransitionPlan,
        *,
        defer_commit: bool,
    ) -> None:
        if defer_commit:
            self._pending_transition_plan = plan
            return
        self._capture_transition(
            self.phases[plan.current_index],
            current_index=plan.current_index,
            route_key=plan.route_key,
            routed_to=plan.routed_to,
            round_number=plan.capture_round,
            fix_loop_rounds=plan.capture_fix_loop_rounds,
        )
        self._apply_transition_plan(plan, update_phase=False)

    def _transition_meta(
        self,
        plan: _TransitionPlan,
    ) -> dict[str, Any]:
        assert self.run_dir is not None
        meta = read_meta(self.run_dir)
        if plan.reset_fix_loop_rounds:
            meta.pop("fix_loop_rounds", None)
        elif plan.set_fix_loop_rounds:
            meta["fix_loop_rounds"] = plan.prospective_fix_loop_rounds
        if meta.get("experiment_enabled") is True:
            meta["phase_revision"] = _phase_revision(meta) + 1
        return meta

    def _apply_transition_artifacts(self, plan: _TransitionPlan) -> None:
        if plan.next_index <= plan.current_index:
            for stale_phase in self.phases[
                plan.next_index : plan.current_index + 1
            ]:
                stale_artifact = self._existing_artifact_path(stale_phase)
                if stale_artifact.exists():
                    stale_artifact.unlink()
        elif plan.next_index > plan.current_index + 1:
            target = (
                self.phases[plan.next_index].id
                if plan.next_index < len(self.phases)
                else "complete"
            )
            for skipped in self.phases[
                plan.current_index + 1 : plan.next_index
            ]:
                skipped_artifact = self._artifact_path(skipped)
                if not skipped_artifact.exists():
                    skipped_artifact.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )
                    skipped_artifact.write_text(
                        f"# {skipped.id}\n\nstatus: skipped\nreason: route_to_{target}\n",
                        encoding="utf-8",
                    )

    def _apply_transition_plan(
        self,
        plan: _TransitionPlan,
        *,
        update_phase: bool,
    ) -> dict[str, Any]:
        assert self.run_dir is not None
        meta = self._transition_meta(plan)
        self._apply_transition_artifacts(plan)
        if update_phase:
            return self._record_phase_entry(plan.next_index, meta=meta)
        if (
            plan.set_fix_loop_rounds
            or plan.reset_fix_loop_rounds
            or meta.get("experiment_enabled") is True
        ):
            write_meta(self.run_dir, meta)
        return meta

    def _transition_target_requires_prompt(
        self,
        plan: _TransitionPlan,
    ) -> bool:
        if plan.next_index >= len(self.phases):
            return False
        if plan.next_index <= plan.current_index:
            return True
        next_phase = self.phases[plan.next_index]
        return (
            not self._has_artifact(next_phase)
            or self._artifact_needs_auto_revalidation(next_phase)
        )

    def _observe_and_commit_pending_transition(
        self,
        adapter: Any,
    ) -> dict[str, Any]:
        plan = self._pending_transition_plan
        if plan is None:
            raise RuntimeError("missing pending workflow transition")
        if adapter._ledger_experiment_enabled is True:
            pending = self._create_pending_transition(plan)
            assert self.run_dir is not None
            _write_pending_transition(self.run_dir, pending)
            return self._finish_pending_transition(adapter, pending)
        committed = self._apply_transition_plan(plan, update_phase=True)
        self._pending_transition_plan = None
        return committed

    def _create_pending_transition(
        self,
        plan: _TransitionPlan,
    ) -> dict[str, object]:
        assert self.run_dir is not None
        from_meta = read_meta(self.run_dir)
        next_meta = self._phase_entry_meta(
            plan.next_index,
            self._transition_meta(plan),
        )
        next_phase = (
            self.phases[plan.next_index]
            if plan.next_index < len(self.phases)
            else None
        )
        observation = (
            {
                "phase_id": next_phase.id,
                "round": _ledger_prompt_round(next_meta),
                "generated_at": _ledger_now_iso(),
                "prompt_bytes": len(_phase_instruction(next_phase).encode("utf-8")),
            }
            if next_phase is not None
            and self._transition_target_requires_prompt(plan)
            else None
        )
        capture = {
            "phase_id": self.phases[plan.current_index].id,
            "route_key": plan.route_key,
            "routed_to": plan.routed_to,
            "round": plan.capture_round,
            "fix_loop_rounds": plan.capture_fix_loop_rounds,
            "generated_at": _ledger_now_iso(),
            "transition_occurrence_id": _transition_occurrence_id(
                from_meta,
                self.phases[plan.current_index],
                plan.current_index,
            ),
            "committed": False,
        }
        base: dict[str, object] = {
            "schema_version": PENDING_TRANSITION_VERSION,
            "runtime": "python",
            "run_id": from_meta.get("run_id"),
            "workflow_id": self.workflow_name,
            "current_index": plan.current_index,
            "next_index": plan.next_index,
            "prospective_fix_loop_rounds": plan.prospective_fix_loop_rounds,
            "set_fix_loop_rounds": plan.set_fix_loop_rounds,
            "reset_fix_loop_rounds": plan.reset_fix_loop_rounds,
            "from_meta": from_meta,
            "next_meta": next_meta,
            "from_meta_sha256": _ledger_control_sha256(from_meta),
            "next_meta_sha256": _ledger_control_sha256(next_meta),
            "capture": capture,
            "observation": observation,
        }
        return {**base, "content_sha256": _ledger_control_sha256(base)}

    def _recover_pending_transition(self, adapter: Any) -> dict[str, Any] | None:
        assert self.run_dir is not None
        pending = _read_pending_transition(self.run_dir)
        if pending is None:
            return None
        return self._finish_pending_transition(adapter, pending)

    def _finish_pending_transition(
        self,
        adapter: Any,
        pending: dict[str, object],
    ) -> dict[str, Any]:
        assert self.run_dir is not None
        plan, next_meta, capture, observation = self._validate_pending_transition(
            pending
        )
        if capture["committed"] is not True:
            from_meta = pending["from_meta"]
            assert isinstance(from_meta, dict)
            self._capture_transition(
                self.phases[plan.current_index],
                current_index=plan.current_index,
                route_key=capture["route_key"],
                routed_to=capture["routed_to"],
                round_number=capture["round"],
                fix_loop_rounds=capture["fix_loop_rounds"],
                meta=from_meta,
                generated_at=capture["generated_at"],
                transition_occurrence_id=capture["transition_occurrence_id"],
            )
            pending = _pending_transition_with_committed_capture(pending)
            _replace_pending_transition(self.run_dir, pending)
        if observation is not None:
            next_phase = self.phases[plan.next_index]
            observed = observe_execution_state_injection(
                run_dir=self.run_dir,
                run_id=adapter._ledger_run_id,
                mode=adapter._ledger_mode,
                experiment_enabled=True,
                phase=next_phase,
                project_root=self.project_root,
                round=observation["round"],
                generated_at=observation["generated_at"],
                prompt_bytes=observation["prompt_bytes"],
            )
            block = _committed_observation_block(
                observed,
                experiment_enabled=True,
            )
            self._prepared_ledger_prompt = {
                "phase_id": next_phase.id,
                "phase_revision": _phase_revision(next_meta),
                "block": block,
            }
        self._apply_transition_artifacts(plan)
        write_meta(self.run_dir, next_meta)
        _remove_pending_transition(self.run_dir)
        self._pending_transition_plan = None
        return next_meta

    def _validate_pending_transition(
        self,
        pending: dict[str, object],
    ) -> tuple[
        _TransitionPlan,
        dict[str, Any],
        dict[str, Any],
        dict[str, Any] | None,
    ]:
        assert self.run_dir is not None
        content_sha256 = pending.get("content_sha256")
        base = dict(pending)
        base.pop("content_sha256", None)
        from_meta = pending.get("from_meta")
        next_meta = pending.get("next_meta")
        if (
            pending.get("schema_version") != PENDING_TRANSITION_VERSION
            or pending.get("runtime") != "python"
            or pending.get("workflow_id") != self.workflow_name
            or not isinstance(from_meta, dict)
            or not isinstance(next_meta, dict)
            or content_sha256 != _ledger_control_sha256(base)
            or pending.get("from_meta_sha256") != _ledger_control_sha256(from_meta)
            or pending.get("next_meta_sha256") != _ledger_control_sha256(next_meta)
        ):
            raise RuntimeError("blocked: invalid execution ledger pending transition")
        current_meta_sha256 = _ledger_control_sha256(read_meta(self.run_dir))
        if current_meta_sha256 not in {
            pending.get("from_meta_sha256"),
            pending.get("next_meta_sha256"),
        }:
            raise RuntimeError("blocked: execution ledger pending transition state mismatch")
        current_index = pending.get("current_index")
        next_index = pending.get("next_index")
        rounds = pending.get("prospective_fix_loop_rounds")
        if (
            not isinstance(current_index, int)
            or isinstance(current_index, bool)
            or not isinstance(next_index, int)
            or isinstance(next_index, bool)
            or not isinstance(rounds, int)
            or isinstance(rounds, bool)
            or not 0 <= current_index < len(self.phases)
            or not 0 <= next_index <= len(self.phases)
        ):
            raise RuntimeError("blocked: invalid execution ledger pending route")
        current_phase = self.phases[current_index]
        next_phase = self.phases[next_index] if next_index < len(self.phases) else None
        if (
            pending.get("run_id") != from_meta.get("run_id")
            or next_meta.get("run_id") != from_meta.get("run_id")
            or from_meta.get("workflow") != self.workflow_name
            or next_meta.get("workflow") != self.workflow_name
            or from_meta.get("phase_index") != current_index
            or from_meta.get("current_phase") != current_phase.id
            or next_meta.get("phase_index") != next_index
            or next_meta.get("current_phase") != (next_phase.id if next_phase else None)
            or from_meta.get("experiment_enabled") is not True
            or next_meta.get("experiment_enabled") is not True
            or _phase_revision(next_meta) != _phase_revision(from_meta) + 1
        ):
            raise RuntimeError("blocked: execution ledger pending transition route mismatch")
        capture = pending.get("capture")
        expected_routed_to = next_phase.id if next_phase is not None else "complete"
        route_key = capture.get("route_key") if isinstance(capture, dict) else None
        expected_route_target = (
            (
                current_phase.routes.get(route_key)
                or current_phase.routes.get("default")
            )
            if current_phase.routes is not None and isinstance(route_key, str)
            else (
                self.phases[current_index + 1].id
                if current_phase.routes is None
                and current_index + 1 < len(self.phases)
                else "complete"
            )
        )
        if (
            not isinstance(capture, dict)
            or capture.get("phase_id") != current_phase.id
            or not isinstance(capture.get("route_key"), str)
            or (
                current_phase.routes is None
                and capture.get("route_key") != "sequential"
            )
            or expected_route_target != expected_routed_to
            or capture.get("routed_to") != expected_routed_to
            or not isinstance(capture.get("round"), int)
            or isinstance(capture.get("round"), bool)
            or not 1 <= capture["round"] <= FIX_LOOP_MAX_ROUNDS
            or not isinstance(capture.get("fix_loop_rounds"), int)
            or isinstance(capture.get("fix_loop_rounds"), bool)
            or capture["fix_loop_rounds"] < 0
            or capture["fix_loop_rounds"] != rounds
            or not isinstance(capture.get("generated_at"), str)
            or capture.get("transition_occurrence_id")
            != _transition_occurrence_id(from_meta, current_phase, current_index)
            or not isinstance(capture.get("committed"), bool)
        ):
            raise RuntimeError("blocked: invalid execution ledger pending capture")
        plan = _TransitionPlan(
            current_index=current_index,
            next_index=next_index,
            prospective_fix_loop_rounds=rounds,
            route_key=capture["route_key"],
            routed_to=capture["routed_to"],
            capture_round=capture["round"],
            capture_fix_loop_rounds=capture["fix_loop_rounds"],
            set_fix_loop_rounds=pending.get("set_fix_loop_rounds") is True,
            reset_fix_loop_rounds=pending.get("reset_fix_loop_rounds") is True,
        )
        observation = pending.get("observation")
        if observation is not None:
            if (
                next_phase is None
                or not isinstance(observation, dict)
                or observation.get("phase_id") != next_phase.id
                or not isinstance(observation.get("round"), int)
                or isinstance(observation.get("round"), bool)
                or not isinstance(observation.get("generated_at"), str)
                or not isinstance(observation.get("prompt_bytes"), int)
                or isinstance(observation.get("prompt_bytes"), bool)
                or observation["prompt_bytes"] < 0
            ):
                raise RuntimeError("blocked: invalid execution ledger pending observation")
        return plan, dict(next_meta), capture, observation

    def _take_prepared_ledger_prompt(
        self,
        phase: Phase,
        meta: dict[str, Any],
    ) -> str | None:
        prepared = self._prepared_ledger_prompt
        if prepared is None:
            return None
        self._prepared_ledger_prompt = None
        if (
            prepared.get("phase_id") != phase.id
            or prepared.get("phase_revision") != _phase_revision(meta)
            or not isinstance(prepared.get("block"), str)
        ):
            return None
        return prepared["block"]

    def _capture_transition(
        self,
        phase: Phase,
        *,
        current_index: int,
        route_key: str,
        routed_to: str,
        round_number: int,
        fix_loop_rounds: int,
        meta: dict[str, Any] | None = None,
        generated_at: str | None = None,
        transition_occurrence_id: str | None = None,
    ) -> dict[str, object]:
        if self.run_dir is None:
            return {"ok": True, "enabled": False, "captured": False}
        capture_meta = read_meta(self.run_dir) if meta is None else meta
        capture = capture_execution_state(
            run_dir=self.run_dir,
            run_id=str(capture_meta.get("run_id") or self.run_dir.name),
            mode=_pinned_ledger_mode(capture_meta),
            experiment_enabled=capture_meta.get("experiment_enabled") is True,
            phase=phase,
            artifact_path=self._existing_artifact_path(phase),
            project_root=getattr(self, "project_root", self.run_dir.parent),
            round=round_number,
            fix_loop_rounds=fix_loop_rounds,
            generated_at=generated_at or _ledger_now_iso(),
            workflow_id=str(capture_meta.get("workflow") or getattr(self, "workflow_name", "unknown")),
            route_key=route_key,
            routed_to=routed_to,
            transition_occurrence_id=(
                transition_occurrence_id
                or _transition_occurrence_id(capture_meta, phase, current_index)
            ),
        )
        if (
            capture_meta.get("experiment_enabled") is True
            and capture.get("ok") is not True
        ):
            raise RuntimeError(
                "execution ledger pilot transition capture failed: "
                f"{capture.get('error') or 'unknown error'}; "
                "retry the same workflow command"
            )
        return capture

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
        return False

    def _artifact_needs_auto_revalidation(self, phase: Phase) -> bool:
        return False

    def _missing_required_markers(self, phase: Phase) -> list[str]:
        assert self.run_dir is not None
        artifact = self._existing_artifact_path(phase)
        if not artifact.exists():
            return []
        text = artifact.read_text(encoding="utf-8")
        missing = list(_missing_markers(text, phase.required_markers))
        config_root = getattr(self, "config_root", getattr(self, "project_root", self.run_dir.parent))
        project_root = getattr(self, "project_root", config_root)
        meta = read_meta(self.run_dir)
        task_scope = str(meta.get("task") or "")
        base_commit = meta.get("base_commit")
        missing.extend(
            missing_local_skill_markers(
                text,
                config_root,
                phase.id,
                task_scope,
                runtime_changed_files(
                    config_root,
                    project_root,
                    base_commit if isinstance(base_commit, str) else None,
                ),
            )
        )
        return missing

    def _artifact_path(self, phase: Phase) -> Path:
        assert self.run_dir is not None
        return self.run_dir / (phase.artifact or f"{phase.id}.md")

    def _existing_artifact_path(self, phase: Phase) -> Path:
        return self._artifact_path(phase)

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
        return self._artifact_path(phase).exists()

    def _validate_pause_approval_request(
        self,
        meta: dict[str, Any],
        phase_index: int,
    ) -> None:
        if phase_index >= len(self.phases):
            raise RuntimeError("blocked: --approve-pause requires an active pause_after phase")
        phase = self.phases[phase_index]
        if not phase.pause_after:
            raise RuntimeError(
                f"blocked: --approve-pause is invalid for non-pause phase {phase.id}"
            )
        artifact = self._artifact_path(phase)
        if not artifact.exists():
            raise RuntimeError(
                f"blocked: --approve-pause requires the current artifact: {artifact}"
            )
        pending = meta.get("pause_after_pending")
        if not isinstance(pending, dict) or pending.get("phase") != phase.id:
            raise RuntimeError(
                f"blocked: --approve-pause requires an existing pause request for {phase.id}"
            )

    def _pause_after_blocks(
        self,
        phase: Phase,
        artifact: Path,
        *,
        approve_pause: bool = False,
    ) -> bool:
        if not phase.pause_after:
            return False
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        meta = read_meta(self.run_dir)
        pending = meta.get("pause_after_pending")
        pending_matches = bool(
            isinstance(pending, dict)
            and pending.get("phase") == phase.id
            and pending.get("artifact_sha256") == digest
        )
        approval = meta.get("pause_after_approval")
        approval_matches = bool(
            isinstance(approval, dict)
            and approval.get("phase") == phase.id
            and approval.get("artifact_sha256") == digest
        )
        if approval_matches:
            return False
        if approve_pause and pending_matches:
            meta["pause_after_approval"] = {
                "phase": phase.id,
                "artifact_sha256": digest,
                "approved_at": _utc_now_iso(),
            }
            write_meta(self.run_dir, meta)
            return False
        requested_at = (
            pending.get("requested_at")
            if (
                pending_matches
                and isinstance(pending.get("requested_at"), str)
                and pending.get("requested_at")
            )
            else _utc_now_iso()
        )
        next_pending = {
            "phase": phase.id,
            "artifact_sha256": digest,
            "requested_at": requested_at,
        }
        if pending != next_pending:
            meta["pause_after_pending"] = next_pending
            write_meta(self.run_dir, meta)
        print(
            f"\n═══ pause: '{phase.id}' 결과 검토 후 "
            f"`{_pause_approval_command(self.next_command)}` ═══"
        )
        self._print_structured_status(
            status="blocked",
            phase=phase,
            reason="pause_after",
            required_artifact=artifact,
            next_command=_pause_approval_command(self.next_command),
        )
        return True

    def _clear_pause_after_pending(
        self,
        phase: Phase,
        artifact: Path,
        *,
        artifact_sha256: str | None = None,
    ) -> None:
        if not phase.pause_after:
            return
        digest = artifact_sha256 or hashlib.sha256(artifact.read_bytes()).hexdigest()
        meta = read_meta(self.run_dir)
        pending = meta.get("pause_after_pending")
        approval = meta.get("pause_after_approval")
        if not (
            isinstance(pending, dict)
            and pending.get("phase") == phase.id
            and pending.get("artifact_sha256") == digest
            and isinstance(approval, dict)
            and approval.get("phase") == phase.id
            and approval.get("artifact_sha256") == digest
        ):
            return
        meta.pop("pause_after_pending", None)
        write_meta(self.run_dir, meta)

    def _print_structured_status(
        self,
        *,
        status: str,
        phase: Phase | None,
        reason: str,
        required_artifact: Path | None = None,
        report: Path | None = None,
        next_command: str | None = None,
    ) -> None:
        assert self.run_dir is not None
        meta = read_meta(self.run_dir)
        required_artifact_text = str(required_artifact) if required_artifact is not None else None
        report_text = str(report) if report is not None else None
        resolved_next_command = (
            "none"
            if status == "complete"
            else next_command or self.next_command
        )
        payload = {
            "status": status,
            "run": f"{self.workflow_name}/{self.run_dir.name}",
            "task": meta.get("task", ""),
            "current_phase": phase.id if phase is not None else "-",
            "reason": reason,
            "required_artifact": required_artifact_text,
            "report": report_text,
            "next_command": resolved_next_command,
            "workspace_root": str(self.project_root.resolve()),
        }
        print(f"status: {_status_value(status)}")
        print(f"run: {_status_value(payload['run'])}")
        print(f"task: {_status_value(payload['task'])}")
        print(f"current_phase: {phase.id if phase is not None else '-'}")
        print(f"workspace_root: {_status_value(self.project_root.resolve())}")
        print(
            "work_cwd_policy: keep every source, build, test, and write tool call "
            f"in {self.project_root.resolve()}; transition commands may run from the leader checkout"
        )
        print(f"reason: {_status_value(reason)}")
        if required_artifact is not None:
            print(f"required_artifact: {_status_value(required_artifact_text)}")
        if report is not None:
            print(f"report: {_status_value(report_text)}")
        print(f"next_command: {_status_value(resolved_next_command)}")
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
        )
        for phase in definition.phases
    ]


def _phase_instruction(phase: Phase) -> str:
    return phase.prompt or phase.description


def _ledger_workflow_phases(phases: list[Phase]) -> list[dict[str, object]]:
    return [
        {
            "id": phase.id,
            "artifact": phase.artifact,
            "description": phase.description,
            "instruction": _phase_instruction(phase),
            "required_markers": list(phase.required_markers),
            "pause_after": phase.pause_after,
            "optional": phase.optional,
            "multi_review": phase.multi_review,
            "cite_lore": phase.cite_lore,
            "routes": dict(phase.routes) if phase.routes is not None else None,
        }
        for phase in phases
    ]


def _runner_workflow_hash(workflow_id: str, phases: list[Phase]) -> str:
    return _ledger_control_sha256(
        {
            "workflow_id": workflow_id,
            "workflow_phases": _ledger_workflow_phases(phases),
        }
    )


def _assert_runner_workflow_pinned(
    meta: dict[str, Any], workflow_id: str, phases: list[Phase]
) -> None:
    if meta.get("experiment_enabled") is not True:
        return
    workflow_hash = meta.get("runner_workflow_hash")
    if (
        meta.get("runner_workflow_hash_version") != 1
        or not isinstance(workflow_hash, str)
        or re.fullmatch(r"[a-f0-9]{64}", workflow_hash) is None
    ):
        raise RuntimeError(
            "blocked: active pilot run has an invalid runner workflow snapshot"
        )
    if workflow_hash != _runner_workflow_hash(workflow_id, phases):
        raise RuntimeError(
            "blocked: active pilot runner workflow snapshot changed; "
            "restore the installed workflow or start a new run"
        )


def _committed_observation_block(
    observation: object,
    *,
    experiment_enabled: bool = False,
) -> str:
    if experiment_enabled and (
        not isinstance(observation, dict) or observation.get("ok") is not True
    ):
        detail = (
            observation.get("error")
            if isinstance(observation, dict)
            else "unknown error"
        )
        raise RuntimeError(
            "execution ledger pilot prompt observation failed: "
            f"{detail or 'unknown error'}; retry the same workflow command"
        )
    if not isinstance(observation, dict) or observation.get("ok") is not True:
        return ""
    block = observation.get("block")
    return block if isinstance(block, str) else ""


def _pinned_ledger_mode(meta: dict[str, Any]) -> str:
    try:
        return resolve_ledger_mode(meta.get("ledger_mode"))
    except ValueError:
        return "artifacts-only"


def _ledger_prompt_round(meta: dict[str, Any]) -> int:
    return min(FIX_LOOP_MAX_ROUNDS, max(1, _fix_loop_rounds(meta)))


def _ledger_capture_round(meta: dict[str, Any], routed_to: str) -> int:
    current = max(0, _fix_loop_rounds(meta))
    prospective = current + 1 if routed_to == "fix-loop" else max(1, current)
    return min(FIX_LOOP_MAX_ROUNDS, max(1, prospective))


def _phase_revision(meta: dict[str, Any]) -> int:
    raw = meta.get("phase_revision", 0)
    if isinstance(raw, bool):
        return 0
    try:
        revision = int(raw)
    except (TypeError, ValueError):
        return 0
    return revision if revision >= 0 else 0


def _transition_occurrence_id(
    meta: dict[str, Any],
    phase: Phase,
    current_index: int,
) -> str:
    return _ledger_control_sha256(
        {
            "schema_version": 1,
            "run_id": str(meta.get("run_id") or ""),
            "workflow_id": str(meta.get("workflow") or ""),
            "phase_id": phase.id,
            "phase_index": current_index,
            "phase_revision": _phase_revision(meta),
        }
    )


def _ledger_experiment_controls_from_environment() -> dict[str, object | None]:
    return {
        "experiment_id": os.environ.get("AGENT_FLOW_EXPERIMENT_ID"),
        "model_id": os.environ.get("AGENT_FLOW_EXPERIMENT_MODEL_ID"),
        "tool_permissions_sha256": os.environ.get(
            "AGENT_FLOW_EXPERIMENT_TOOL_PERMISSIONS_SHA256"
        ),
        "system_prompt_sha256": os.environ.get(
            "AGENT_FLOW_EXPERIMENT_SYSTEM_PROMPT_SHA256"
        ),
        "caps_sha256": os.environ.get("AGENT_FLOW_EXPERIMENT_CAPS_SHA256"),
        "provider_retry_policy_sha256": os.environ.get(
            "AGENT_FLOW_EXPERIMENT_PROVIDER_RETRY_POLICY_SHA256"
        ),
        "provider_max_retries": _ledger_environment_integer(
            "AGENT_FLOW_EXPERIMENT_PROVIDER_MAX_RETRIES"
        ),
        "pricing_snapshot": _ledger_environment_object(
            "AGENT_FLOW_EXPERIMENT_PRICING_JSON"
        ),
        "provider_attestation_key_id": os.environ.get(
            "AGENT_FLOW_EXPERIMENT_PROVIDER_ATTESTATION_KEY_ID"
        ),
        "provider_attestation_public_key": _ledger_environment_object(
            "AGENT_FLOW_EXPERIMENT_PROVIDER_ATTESTATION_PUBLIC_KEY_JWK"
        ),
    }


def _ledger_environment_integer(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    if not raw.isascii() or not raw.isdecimal():
        raise ValueError(f"invalid {name}: expected a non-negative integer")
    value = int(raw)
    if value > 9_007_199_254_740_991:
        raise ValueError(f"invalid {name}: integer exceeds the safe range")
    return value


def _ledger_environment_object(name: str) -> dict[str, object] | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid {name}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid {name}: expected a JSON object")
    return value


def _ledger_observed_run_snapshot(
    meta: dict[str, Any],
    profile_id: str,
    profile: dict[str, Any],
    phases: list[Phase],
) -> dict[str, str]:
    profile_projection = {"profile_id": profile_id, "profile": profile}
    profile_snapshot_sha256 = _ledger_control_sha256(profile_projection)
    installed_skill_plan = _ledger_control_commitment_or_absent(
        meta.get("skill_plan_hash"), "installed skill plan"
    )
    local_skill_plan = _ledger_control_commitment_or_absent(
        meta.get("local_skill_plan_hash"), "local skill plan"
    )
    lore_snapshot = _ledger_control_commitment_or_absent(
        meta.get("lore_snapshot_hash"), "lore snapshot"
    )
    return {
        "runtime_id": "python",
        "profile_snapshot_sha256": profile_snapshot_sha256,
        "installed_skill_plan_sha256": installed_skill_plan,
        "local_skill_plan_sha256": local_skill_plan,
        "lore_snapshot_sha256": lore_snapshot,
        "prompt_controls_sha256": _ledger_control_sha256(
            {
                "workflow_phases": _ledger_workflow_phases(phases),
                "profile_snapshot_sha256": profile_snapshot_sha256,
                "installed_skill_plan_sha256": installed_skill_plan,
                "local_skill_plan_sha256": local_skill_plan,
                "lore_snapshot_sha256": lore_snapshot,
                "worktree_mode": meta.get("worktree_mode"),
            }
        ),
    }


def _ledger_control_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _pending_transition_path(run_dir: Path) -> Path:
    return run_dir / PENDING_TRANSITION_FILE


def _read_pending_transition(run_dir: Path) -> dict[str, object] | None:
    target = _pending_transition_path(run_dir)
    if not target.exists() and not target.is_symlink():
        return None
    if target.is_symlink() or not target.is_file():
        raise RuntimeError(f"blocked: unsafe execution ledger pending transition: {target}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"blocked: unreadable execution ledger pending transition: {target}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("blocked: invalid execution ledger pending transition")
    return payload


def _write_pending_transition(run_dir: Path, payload: dict[str, object]) -> None:
    target = _pending_transition_path(run_dir)
    if target.exists() or target.is_symlink():
        raise RuntimeError(f"blocked: unresolved execution ledger transition: {target}")
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, target)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _pending_transition_with_committed_capture(
    pending: dict[str, object],
) -> dict[str, object]:
    capture = pending.get("capture")
    if not isinstance(capture, dict) or capture.get("committed") is not False:
        raise RuntimeError("blocked: invalid execution ledger pending capture state")
    base = dict(pending)
    base.pop("content_sha256", None)
    base["capture"] = {**capture, "committed": True}
    return {**base, "content_sha256": _ledger_control_sha256(base)}


def _replace_pending_transition(
    run_dir: Path,
    payload: dict[str, object],
) -> None:
    target = _pending_transition_path(run_dir)
    if target.is_symlink() or not target.is_file():
        raise RuntimeError(f"blocked: unsafe execution ledger pending transition: {target}")
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, target)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _remove_pending_transition(run_dir: Path) -> None:
    target = _pending_transition_path(run_dir)
    if target.is_symlink() or not target.is_file():
        raise RuntimeError(f"blocked: unsafe execution ledger pending transition: {target}")
    target.unlink()


def _ledger_control_commitment_or_absent(value: object, label: str) -> str:
    if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value):
        return value
    if value in (None, ""):
        return _ledger_control_sha256({"state": "absent", "label": label})
    raise RuntimeError(f"blocked: invalid {label} commitment for ledger experiment")


def _optional_meta_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _route_key(text: str, phase_id: str = "") -> str:
    checks = {
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
    }
    route_fields = _route_fields(
        text,
        include_malformed=phase_id
        in {"plan-review", "architecture-review", "merge-approval"},
    )
    if len(route_fields) > 1:
        return "invalid-route"
    if not route_fields:
        return "default"
    field, key = route_fields[0]
    if phase_id == "pr-watch" and field != "status":
        return "invalid-route"
    if phase_id in {"plan-review", "merge-approval"} and field != "verdict":
        return "invalid-route"
    if phase_id == "architecture-review":
        if field == "status" and key != "blocked":
            return "invalid-route"
        if field not in {"status", "verdict"}:
            return "invalid-route"
    if key in checks:
        return key
    return "default"


def _gates_route_key(text: str) -> str:
    try:
        payload = json.loads(text, object_pairs_hook=_json_object_without_duplicate_keys)
    except json.JSONDecodeError:
        return "default"
    except ValueError:
        return "invalid-route"
    if not isinstance(payload, dict) or not isinstance(payload.get("passed"), bool):
        return "default"
    status = payload.get("status")
    if "status" in payload and not isinstance(status, str):
        return "invalid-route"
    if isinstance(status, str):
        normalized_status = status.strip().lower().replace("_", "-")
        if payload["passed"] is True and normalized_status in {"green", "approve"}:
            return normalized_status if _gate_results_prove_pass(payload.get("results")) else "default"
        if payload["passed"] is False and normalized_status in {"request-changes", "blocked", "error", "pending"}:
            return normalized_status
        return "invalid-route"
    if payload["passed"] is True:
        return "green" if _gate_results_prove_pass(payload.get("results")) else "default"
    return "request-changes"


def _multi_review_route_key(text: str, phase_id: str = "") -> str:
    verdicts = _independent_reviewer_verdicts(text)
    overall = _multi_review_overall_route_key(text, phase_id)
    if overall == "blocked" and phase_id == "architecture-review":
        return "blocked"
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


def _multi_review_overall_route_key(text: str, phase_id: str = "") -> str:
    in_overall_section = False
    route_fields: list[tuple[str, str]] = []
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
        match = re.match(r"^(verdict|status):\s*([a-z_-]+)\s*$", stripped, re.IGNORECASE)
        if not match:
            continue
        route_fields.append((match.group(1).lower(), match.group(2).lower()))
    if overall_sections > 1:
        return "invalid-verdict"
    if overall_sections == 0 and phase_id == "architecture-review":
        blocked_fields = [
            match.groups()
            for line in text.splitlines()
            if (
                match := re.match(
                    r"^(verdict|status):\s*(blocked)\s*$",
                    line,
                    re.IGNORECASE,
                )
            )
        ]
        if len(blocked_fields) == 1:
            return "blocked"
        if len(blocked_fields) > 1:
            return "invalid-verdict"
    if not route_fields:
        return "default"
    if len(route_fields) != 1:
        return "invalid-verdict"
    field, value = route_fields[0]
    if phase_id == "architecture-review":
        if field == "status":
            return "blocked" if value == "blocked" else "invalid-verdict"
        return value if value in {"approve", "request-changes", "blocked"} else "invalid-verdict"
    if field != "verdict" or value not in {"approve", "request-changes"}:
        return "invalid-verdict"
    return value


def _route_fields(
    text: str,
    *,
    include_malformed: bool = False,
) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    for line in text.splitlines():
        pattern = (
            r"^(verdict|status):\s*([^\r\n]*)$"
            if include_malformed
            else r"^(verdict|status):\s*([a-z_-]+)\s*$"
        )
        match = re.match(pattern, line, re.IGNORECASE)
        if match:
            fields.append((match.group(1).lower(), match.group(2).strip().lower()))
    return fields


def _json_object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _independent_reviewer_verdict_count(text: str) -> int:
    return len(_independent_reviewer_verdicts(text))


def _independent_reviewer_verdicts(text: str) -> dict[str, str]:
    reviewers: dict[str, dict[str, object]] = {}
    current_reviewer: str | None = None

    def set_verdict(reviewer: str, verdict: str) -> None:
        state = reviewers.setdefault(
            reviewer,
            {"has_source": False, "subagent": False, "verdict": None},
        )
        if state["verdict"] is not None:
            raise RuntimeError(
                f"blocked: multi-review reviewer {reviewer} has multiple verdict lines"
            )
        state["verdict"] = verdict

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
                set_verdict(key, verdict)
        elif current_reviewer is not None:
            set_verdict(current_reviewer, verdict)
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


def _run_base_snapshot(profile: dict[str, Any], project_root: Path) -> dict[str, str | None]:
    primary = profile
    if profile.get("id") == "multi-profile":
        profiles = profile.get("profiles")
        primary_id = profile.get("primary_profile")
        if isinstance(profiles, list):
            selected = next(
                (
                    item
                    for item in profiles
                    if isinstance(item, dict) and item.get("id") == primary_id
                ),
                None,
            )
            if selected is not None:
                primary = selected
    branching = primary.get("branching") if isinstance(primary, dict) else None
    configured = branching.get("base") if isinstance(branching, dict) else None
    base_ref = configured if isinstance(configured, str) and configured else "HEAD"
    _validate_git_base_ref(base_ref)
    commands = (
        (["git", "rev-parse", "HEAD"],)
        if base_ref == "HEAD"
        else (
            ["git", "merge-base", "HEAD", base_ref],
            ["git", "rev-parse", f"{base_ref}^{{commit}}"],
            ["git", "rev-parse", "HEAD"],
        )
    )
    base_commit: str | None = None
    for command in commands:
        result = run_safe_command(command, cwd=project_root, timeout_s=5)
        if result.ok and result.stdout.strip():
            base_commit = result.stdout.strip()
            break
    return {"base_ref": base_ref, "base_commit": base_commit}


def _load_profile(kit_root: Path, project_root: Path) -> tuple[str, dict[str, Any]]:
    """Return (profile_id, profile_dict).

    Installed projects use only the pinned kit/profile snapshot. The
    `AGENT_FLOW_PROFILE` compatibility override is accepted only before the
    project has installed metadata.

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
    kit_path = project_root / ".agent-flow" / "kit.json"
    installed = kit_path.exists() or kit_path.is_symlink()
    if forced and not installed:
        return _load_single_profile(
            kit_root,
            forced,
            strict_missing=False,
            explicit_fallback=explicit_fallback,
            source="AGENT_FLOW_PROFILE",
        )

    from_kit_profiles = active_profile_ids(project_root) if installed else []
    if from_kit_profiles:
        return _load_profile_union(
            kit_root,
            from_kit_profiles,
            explicit_fallback=explicit_fallback,
            project_root=project_root,
            primary_profile=primary_profile_id(project_root),
        )

    from_kit = _read_kit_profile(project_root)
    detected_profiles = active_profile_ids(project_root)
    profile_id = from_kit or (detected_profiles[0] if detected_profiles else "generic")
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
    project_root: Path | None = None,
) -> tuple[str, dict[str, Any]]:
    _validate_yaml_name(profile_id, "profile")

    if project_root is not None:
        return profile_id, load_project_profile_payload(project_root, profile_id)

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
    project_root: Path | None = None,
    primary_profile: str | None = None,
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
                project_root=project_root,
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
    return merge_profile_payloads(deduped, primary_profile)


def _dedupe_loaded_profiles(profiles: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    deduped: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for profile_id, profile in profiles:
        if profile_id in seen:
            continue
        seen.add(profile_id)
        deduped.append((profile_id, profile))
    return deduped


def _read_kit_profile(project_root: Path) -> str | None:
    data = _read_kit_json(project_root)
    p = data.get("profile")
    if p is None:
        return None
    if not isinstance(p, str) or not p:
        raise ValueError(".agent-flow/kit.json:profile must be a non-empty string")
    _validate_yaml_name(p, "profile")
    return p


def _read_kit_profiles(project_root: Path) -> list[str]:
    data = _read_kit_json(project_root)
    profiles = data.get("profiles")
    if profiles is None:
        return []
    if not isinstance(profiles, list):
        raise ValueError(".agent-flow/kit.json:profiles must be a list")
    deduped: list[str] = []
    seen: set[str] = set()
    for profile in profiles:
        if not isinstance(profile, str) or not profile:
            raise ValueError("invalid profile name in .agent-flow/kit.json:profiles")
        _validate_yaml_name(profile, "profile")
        if profile not in seen:
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


def _validate_git_base_ref(value: str) -> str:
    if value.startswith("-") or any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError(f"invalid profile base ref: {value!r}")
    return value


def _lore_snapshot_metadata(project_root: Path, task: str) -> dict[str, Any]:
    return lore_snapshot_metadata(project_root, task)


def _ensure_lore_snapshot(
    run_dir: Path,
    source_root: Path,
    prompt_root: Path,
) -> tuple[dict[str, Any], list[Lore]]:
    meta = read_meta(run_dir)
    if not all(isinstance(meta.get(key), str) and meta[key] for key in ("run_id", "workflow")):
        raise RuntimeError(
            "blocked: legacy run metadata is missing its identity; "
            "restore meta.json before migrating lore citations"
        )
    phase_started = (
        meta.get("current_phase") not in (None, "")
        or int(meta.get("phase_index", 0) or 0) != 0
    )
    updated, citations, changed = reconcile_lore_snapshot(
        meta,
        source_root,
        prompt_root,
        allow_migration=not phase_started,
    )
    if changed:
        write_meta(run_dir, updated)
    return updated, citations


def _search_lore(project_root: Path, task: str, top_k: int = 5) -> list[Lore]:
    return search_lore(project_root, task, top_k=top_k)
