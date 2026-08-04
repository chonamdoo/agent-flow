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
import shlex
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from agent_flow.adapters.auto import detect_adapter
from agent_flow.adapters.generic import STUB_SENTINEL
from agent_flow.artifact import (
    create_run,
    mark_inactive,
    read_meta,
    write_meta,
)
from agent_flow.cli_detect import CliInfo, REVIEW_CLI_NAMES, detect_available_clis
from agent_flow.core.commands import run_safe_command
from agent_flow.core.command_evidence import missing_test_evidence_markers
from agent_flow.core.design_ledger import (
    LEDGER_SOURCE_PHASES,
    capture_design_ledger,
    missing_design_value_markers,
)
from agent_flow.core.design_value_check import (
    missing_design_value_implementations,
    missing_spec_item_evidence,
)
from agent_flow.core.hook_integrity import assert_managed_hooks_registered
from agent_flow.core.worktrees import (
    CleanupBlockedError,
    complete_worktree_cleanup,
    run_worktree_cleanup_transaction,
    worktree_run_activation,
)
from agent_flow.core.worktree_isolation import (
    HOST_PHASE_LEADER_BASELINE_KEY,
    LeaderDriftError,
    LeaderSnapshot,
    WorktreeIsolationError,
    assert_leader_unchanged,
    capture_leader_snapshot,
    git_safe,
    leader_drift_message,
    leader_root_for,
    real_path,
    sanitized_worker_env,
)
from agent_flow.core.profiles import (
    GATE_PHASE_ALL,
    apply_project_profile_override,
    project_profile_path,
)
from agent_flow.core.phase_workflow import (
    package_root,
    find_kit_root,
    load_phase_workflow_definition,
    overall_review_route_key,
    unfenced_markdown_text,
)
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
# 이름이 두 벌이면 rebind 복구 명령이 runner가 읽는 키와 다른 키를 고칠 수 있다.
_HOST_PHASE_LEADER_BASELINE = HOST_PHASE_LEADER_BASELINE_KEY
PROTECTED_BRANCHES = frozenset({"main", "master", "develop"})
CONVENTIONAL_COMMIT_RE = re.compile(
    r"^(?:feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(?:\([^)]+\))?!?: \S.*$"
)
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


def _phase_available_clis(clis: list[CliInfo], phase: Phase | None) -> list[CliInfo]:
    if phase is None or not phase.multi_review:
        return clis
    return [cli for cli in clis if cli.name in REVIEW_CLI_NAMES]


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
        requested_run_id: str | None = None,
        checkout_identity: str | None = None,
        checkout_registration_identity: str | None = None,
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
        self.requested_run_id = requested_run_id
        self.checkout_identity = checkout_identity
        self.checkout_registration_identity = checkout_registration_identity
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
            activation = (
                worktree_run_activation(
                    root=self.config_root,
                    path=self.project_root,
                    registration_identity=self.checkout_registration_identity,
                )
                if self.checkout_identity
                and self.checkout_identity.startswith("worktree:")
                else nullcontext()
            )
            with activation:
                self.run_dir = create_run(
                    self.state_root,
                    self.workflow_name,
                    task,
                    architecture=self.architecture,
                    run_id=self.requested_run_id,
                    checkout_identity=self.checkout_identity,
                    checkout_registration_identity=(
                        self.checkout_registration_identity
                    ),
                )
            print(f"▶ run started : {self.run_dir.name}")
            print(f"▶ task        : {task}")
        else:
            assert self.run_dir is not None
            meta = read_meta(self.run_dir)
            stored_checkout = meta.get("checkout_identity")
            if (
                isinstance(stored_checkout, str)
                and stored_checkout.startswith("worktree:")
            ):
                with worktree_run_activation(
                    root=self.config_root,
                    path=self.project_root,
                    registration_identity=meta.get(
                        "checkout_registration_identity"
                    ),
                ):
                    pass
            print(f"▶ resuming    : {self.run_dir.name}")
            print(f"▶ task        : {meta.get('task', '')}")

        adapter = detect_adapter()
        self._adapter_name = adapter.name
        assert self.run_dir is not None
        run_meta = read_meta(self.run_dir)
        banner_index = int(run_meta.get("phase_index", 0) or 0)
        banner_phase = (
            self.phases[banner_index]
            if 0 <= banner_index < len(self.phases)
            else None
        )
        clis = _phase_available_clis(detect_available_clis(), banner_phase)
        if clis:
            cli_summary = ", ".join(c.name for c in clis)
        elif banner_phase is not None and banner_phase.multi_review:
            # review phase는 host fallback이 없다. "generic fallback"이라고 적으면
            # 없는 우회로를 안내한다.
            cli_summary = "none (review blocked: install claude or codex)"
        else:
            cli_summary = "none (generic fallback)"
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
        adapter._task_text = run_meta.get("task", "")
        adapter._changed_files = changed_files(self.project_root)

        # Auto-cite lore: search the local lore index for entries relevant
        # to the task description and inject them into the prompt envelope.
        # Empty list when memory dir is missing or no matches.
        adapter._lore_citations = _search_lore(
            self.project_root, run_meta.get("task", ""),
        )

        # 이 실행이 worktree 안이라면 뒤에 있는 leader 체크아웃이 지켜야 할
        # 대상이다. leader에서 그대로 도는 실행은 지킬 바깥 대상이 없다.
        leader_root = leader_root_for(self.project_root)

        assert self.run_dir is not None
        meta = read_meta(self.run_dir)
        phase_index = int(meta.get("phase_index", 0) or 0)
        while phase_index < len(self.phases):
            phase = self.phases[phase_index]
            leader_before = self._verify_host_phase_leader_baseline(
                meta=meta,
                phase=phase,
                phase_index=phase_index,
                leader_root=leader_root,
            )
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
            if leader_root is not None and leader_before is None:
                leader_before = capture_leader_snapshot(leader_root)
                self._persist_host_phase_leader_baseline(
                    phase=phase,
                    phase_index=phase_index,
                    leader_root=leader_root,
                    snapshot=leader_before,
                )
            completed = adapter.execute(
                phase, run_dir=self.run_dir, project_root=self.project_root,
            )
            if leader_before is not None:
                self._assert_leader_unchanged(leader_root, leader_before)
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
        cleanup_journal = read_meta(self.run_dir).get("cleanup_journal")
        if leader_root is not None or cleanup_journal:
            try:
                target_branch, integration_strategy = _cleanup_profile_contract(
                    self.profile
                )
                cleanup = run_worktree_cleanup_transaction(
                    root=self.config_root,
                    checkout_path=self.project_root,
                    run_dir=self.run_dir,
                    target_branch=target_branch,
                    integration_strategy=integration_strategy,
                )
                self.run_dir = complete_worktree_cleanup(cleanup)
                report_path = self.run_dir / report_path.name
            except CleanupBlockedError as exc:
                if exc.run_dir is not None and exc.run_dir.exists():
                    self.run_dir = exc.run_dir
                print(f"\n═══ cleanup is pending. {exc} ═══")
                self._print_structured_status(
                    status="blocked",
                    phase=None,
                    reason="cleanup_pending",
                    required_artifact=exc.journal_path,
                )
                return
        else:
            mark_inactive(self.run_dir)
        print("\n✓ run complete.")
        self._print_structured_status(
            status="complete",
            phase=None,
            reason="workflow_complete",
            report=report_path,
        )

    def _verify_host_phase_leader_baseline(
        self,
        *,
        meta: dict[str, Any],
        phase: Phase,
        phase_index: int,
        leader_root: Path | None,
    ) -> LeaderSnapshot | None:
        raw = meta.get(_HOST_PHASE_LEADER_BASELINE)
        if raw is None:
            return None
        if leader_root is None:
            raise WorktreeIsolationError(
                "durable host-phase leader baseline exists without a linked worktree"
            )
        if not isinstance(raw, dict) or set(raw) != {
            "version",
            "run_id",
            "phase_id",
            "phase_index",
            "leader_root",
            "snapshot",
        }:
            raise WorktreeIsolationError(
                "durable host-phase leader baseline is malformed"
            )
        assert self.run_dir is not None
        expected_root = str(real_path(leader_root))
        if (
            raw.get("version") != 1
            or raw.get("run_id") != self.run_dir.name
            or raw.get("phase_id") != phase.id
            or isinstance(raw.get("phase_index"), bool)
            or raw.get("phase_index") != phase_index
            or raw.get("leader_root") != expected_root
        ):
            raise WorktreeIsolationError(
                "durable host-phase leader baseline does not match the current "
                "run, phase, or leader checkout"
            )
        snapshot_raw = raw.get("snapshot")
        if not isinstance(snapshot_raw, dict) or set(snapshot_raw) != {
            "head",
            "branch",
            "status",
            "armed",
        }:
            raise WorktreeIsolationError(
                "durable host-phase leader snapshot is malformed"
            )
        head = snapshot_raw.get("head")
        branch = snapshot_raw.get("branch")
        status = snapshot_raw.get("status")
        armed = snapshot_raw.get("armed")
        if (
            not isinstance(head, str)
            or not head
            or not isinstance(branch, str)
            or not branch
            or not isinstance(status, str)
            or armed is not True
        ):
            raise WorktreeIsolationError(
                "durable host-phase leader snapshot is incomplete"
            )
        snapshot = LeaderSnapshot(
            head=head,
            branch=branch,
            status=status,
            armed=True,
        )
        self._assert_leader_unchanged(leader_root, snapshot)
        return snapshot

    def _assert_leader_unchanged(
        self, leader_root: Path, snapshot: LeaderSnapshot
    ) -> None:
        """tripwire 판정 + 이 run에 맞는 복구 명령.

        정상 `git pull` 하나로도 기준선은 stale이 된다. 그때 "commit 또는 stash"만
        안내하면 사용자가 할 수 있는 일이 없고, run은 그 자리에서 영구 정지한다.
        그래서 clean fast-forward에는 이 run의 baseline을 지목하는 정확한 명령을
        싣는다.
        """
        assert self.run_dir is not None
        try:
            assert_leader_unchanged(
                leader_root,
                snapshot,
                run_id=self.run_dir.name,
                worker_root=self.project_root,
            )
        except LeaderDriftError as exc:
            raise LeaderDriftError(
                leader_drift_message(
                    exc.drift,
                    worker_root=self.project_root,
                    recovery_command=(
                        "`agent-flow host-session rebind"
                        f" --root {shlex.quote(str(leader_root))}"
                        f" --run-dir {shlex.quote(str(self.run_dir))}"
                        f" --expected-old-head {exc.drift.before.head}"
                        f" --expected-new-head {exc.drift.after.head}`"
                    ),
                ),
                exc.drift,
            ) from exc

    def _persist_host_phase_leader_baseline(
        self,
        *,
        phase: Phase,
        phase_index: int,
        leader_root: Path,
        snapshot: LeaderSnapshot,
    ) -> None:
        assert self.run_dir is not None
        if not snapshot.armed:
            raise WorktreeIsolationError(
                "cannot persist an unarmed host-phase leader baseline"
            )
        meta = read_meta(self.run_dir)
        if meta.get(_HOST_PHASE_LEADER_BASELINE) is not None:
            raise WorktreeIsolationError(
                "host-phase leader baseline changed without phase advancement"
            )
        meta[_HOST_PHASE_LEADER_BASELINE] = {
            "version": 1,
            "run_id": self.run_dir.name,
            "phase_id": phase.id,
            "phase_index": phase_index,
            "leader_root": str(real_path(leader_root)),
            "snapshot": {
                "head": snapshot.head,
                "branch": snapshot.branch,
                "status": snapshot.status,
                "armed": snapshot.armed,
            },
        }
        write_meta(self.run_dir, meta)

    def _next_index(self, current_index: int, phase: Phase) -> tuple[int, bool]:
        # phase를 떠나는 유일한 통로다. 여기서 원장을 굳혀야 skip 경로(재개)와
        # 실행 경로 양쪽에서 같은 값이 다음 phase로 넘어간다.
        self._capture_design_ledger(phase)
        if not phase.routes:
            return current_index + 1, False
        assert self.run_dir is not None
        artifact = self._existing_artifact_path(phase)
        text = artifact.read_text(encoding="utf-8") if artifact.exists() else ""
        if phase.multi_review:
            key = _multi_review_route_key(text, phase.id)
        elif phase.id == "gates":
            key = _gates_route_key(text, nonce=str(read_meta(self.run_dir).get("gate_nonce", "")))
            if key == "default":
                # `passed: true`인 파일이 fix-loop로 되돌려지는 이유는 결과 목록에
                # 안 보인다. 말하지 않으면 같은 명령을 세 번 재시도하다 round cap에
                # 걸려 run이 영구 정지한다.
                recorded = _recorded_gate_phase(text)
                if recorded and recorded != GATE_PHASE_ALL:
                    print(
                        f"  [route] gate-results.json ran --phase {recorded}; "
                        f"build and test gates are pre-push. re-run: "
                        f"agent-flow gates --phase {GATE_PHASE_ALL} --run-dir <run-dir>"
                    )
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

    def _capture_design_ledger(self, phase: Phase) -> None:
        """설계 phase의 수치를 원장으로 굳힌다. 다음 phase는 여기서만 값을 본다."""
        if phase.id not in LEDGER_SOURCE_PHASES or self.run_dir is None:
            return
        artifact = self._existing_artifact_path(phase)
        if not artifact.exists():
            return
        capture_design_ledger(self.run_dir, phase.id, artifact.read_text(encoding="utf-8"))

    def _advance_phase(self, meta: dict[str, Any], phase_index: int, blocked: bool) -> None:
        """route 결과를 meta에 반영한다.

        `blocked`면 제자리에 멈춘 것이므로 진입이 아니다. 여기서 시각을 밀면
        방금 쓴 artifact가 진입 시각보다 과거가 되어 다음 실행이 진짜 사유
        (route_blocked) 대신 stale_artifact를 보고한다.
        """
        if blocked:
            meta["phase_blocked_reason"] = "route_blocked"
        else:
            meta.pop(_HOST_PHASE_LEADER_BASELINE, None)
            meta.pop("phase_blocked_reason", None)
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
        assert self.run_dir is not None
        artifact = self._existing_artifact_path(phase)
        if not artifact.exists():
            return []
        text = artifact.read_text(encoding="utf-8")
        # stub 모드는 host AI 없이 state machine을 돌리는 픽스처 경로다. 다만
        # **stub이 직접 쓴 artifact**에서만 마커 검사를 건너뛴다. 예전에는 환경변수
        # 하나로 사람이 쓴 artifact까지 통째로 통과해서, 마커 검사 전면 킬스위치였다.
        if self._is_stub_authored(text):
            return []
        missing = list(_missing_markers(text, phase.required_markers))
        missing.extend(
            _missing_delivery_evidence(
                self.project_root,
                phase.id,
                text,
                profile=self.profile,
            )
        )
        missing.extend(missing_design_value_markers(text, phase.id))
        meta = read_meta(self.run_dir)
        missing.extend(
            missing_test_evidence_markers(
                self.config_root,
                phase.id,
                text,
                profile=self.profile,
                since=_meta_timestamp(meta.get("phase_entered_at")),
            )
        )
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
        missing.extend(
            missing_spec_item_evidence(
                self.project_root,
                self.run_dir,
                phase.id,
                text,
                task_text=str(meta.get("task", "")),
                profile=self.profile,
                since=_meta_timestamp(meta.get("started_at")),
                evidence_root=self.config_root,
            )
        )
        missing.extend(
            missing_design_value_implementations(
                self.project_root,
                self.run_dir,
                phase.id,
                text,
                profile=self.profile,
            )
        )
        return missing

    def _is_stub_authored(self, text: str) -> bool:
        return (
            getattr(self, "_adapter_name", "") == "generic"
            and os.environ.get("AGENT_FLOW_GENERIC_MODE") == "stub-success"
            and STUB_SENTINEL in text
        )

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


def _cleanup_profile_contract(profile: dict[str, Any]) -> tuple[str, str]:
    branching = profile.get("branching")
    pr = profile.get("pr")
    if not isinstance(branching, dict) or not isinstance(pr, dict):
        raise CleanupBlockedError(
            "profile integration contract is unknown; preserving checkout"
        )
    integration = branching.get("integration")
    target = pr.get("target_branch")
    strategy = pr.get("merge_strategy")
    if (
        not isinstance(integration, str)
        or not integration
        or not isinstance(target, str)
        or target != integration
        or strategy not in {"merge", "squash", "rebase"}
    ):
        raise CleanupBlockedError(
            "profile target or merge strategy is unknown or inconsistent; "
            "preserving checkout"
        )
    return target, str(strategy)


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


def _gates_route_key(text: str, *, nonce: str = "") -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return "default"
    if not isinstance(payload, dict) or not isinstance(payload.get("passed"), bool):
        return "default"
    # timeout은 "실패"가 아니라 "판정 불가"다. optional 게이트가 상한을 다 쓰고
    # 죽어도 passed 집계는 required만 보므로 green이 된다. 그 구멍을 여기서 닫는다.
    if _gate_results_timed_out(payload.get("results")):
        return "error"
    # 통과 라우팅에만 출처를 요구한다. 실패/차단은 손으로 써도 앞으로 못 가므로
    # 막을 이유가 없고, 막으면 복구 경로만 좁아진다.
    proven = (
        _gate_results_prove_pass(payload.get("results"))
        and _gate_nonce_matches(payload, nonce)
        and _gate_phase_covers_verification(payload)
    )
    status = payload.get("status")
    if isinstance(status, str):
        normalized_status = status.strip().lower().replace("_", "-")
        if payload["passed"] is True and normalized_status in {"green", "approve"}:
            return normalized_status if proven else "default"
        if payload["passed"] is False and normalized_status in {"request-changes", "blocked", "error", "pending"}:
            return normalized_status
    if payload["passed"] is True:
        return "green" if proven else "default"
    return "request-changes"


def _gate_results_timed_out(results: object) -> bool:
    if not isinstance(results, list):
        return False
    return any(
        isinstance(result, dict) and result.get("timed_out") is True
        for result in results
    )


def _gate_nonce_matches(payload: dict[str, object], nonce: str) -> bool:
    """이 run의 `agent-flow gates`가 쓴 파일인가.

    run에 nonce가 없으면(구버전 run, CLI 직접 사용) 대조할 기준이 없으므로
    요구하지 않는다. "없으면 위반"이 아니라 "기록과 다르면 위반"이다.
    """
    if not nonce:
        return True
    produced_by = payload.get("produced_by")
    if not isinstance(produced_by, dict):
        return False
    return produced_by.get("nonce") == nonce


def _gate_phase_covers_verification(payload: dict[str, object]) -> bool:
    """QA gate가 build/test까지 돌렸는가.

    `agent-flow gates`의 기본 phase는 `pre-commit`이고 build/test는 `pre-push`다.
    workflow의 gates phase는 커밋 직전 마지막 검증이므로 둘 다 돌아야 한다.
    `--phase pre-commit`으로 돈 결과는 "전부 통과"처럼 보이지만 실제로는
    build/test가 목록에 오르지도 않은 실행이다.

    `all`만 받는다. `pre-push` 단독은 lint/type/architecture-lint를 빼먹고, 부분
    phase를 조합으로 인정하기 시작하면 "무엇이 돌았는가"를 결과 목록에서 다시
    역산해야 한다. 번들 프로필에 post-merge gate는 아직 없다 — 생기면 `all`이
    그것까지 커밋 전에 돌리므로 그때 이 규칙을 다시 봐야 한다.

    기록이 없으면(구버전 파일, CLI 직접 사용) 대조할 기준이 없으므로 요구하지
    않는다. nonce와 같은 규칙이다 — "없으면 위반"이 아니라 "기록과 다르면 위반".
    """
    produced_by = payload.get("produced_by")
    if not isinstance(produced_by, dict):
        return True
    recorded = produced_by.get("gate_phase")
    if not isinstance(recorded, str) or not recorded:
        return True
    return recorded == GATE_PHASE_ALL


def _recorded_gate_phase(text: str) -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    produced_by = payload.get("produced_by")
    if not isinstance(produced_by, dict):
        return ""
    recorded = produced_by.get("gate_phase")
    return recorded if isinstance(recorded, str) else ""


def _multi_review_route_key(text: str, phase_id: str = "") -> str:
    verdicts = _independent_reviewer_verdicts(text)
    overall = overall_review_route_key(text)
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




def _independent_reviewer_verdict_count(text: str) -> int:
    return len(_independent_reviewer_verdicts(text))


def _independent_reviewer_verdicts(text: str) -> dict[str, str]:
    reviewers: dict[str, dict[str, object]] = {}
    current_reviewer: str | None = None
    for line in unfenced_markdown_text(text).splitlines():
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
    return any(
        _line_marks_subagent_source(line.strip().lower())
        for line in unfenced_markdown_text(text).splitlines()
    )


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


def _missing_delivery_evidence(
    project_root: Path,
    phase_id: str,
    text: str,
    *,
    profile: dict[str, Any] | None = None,
) -> list[str]:
    if phase_id == "commit":
        return _missing_commit_evidence(project_root, text)
    if phase_id == "push-pr":
        target_branch = _profile_pr_target(profile)
        if target_branch is None:
            return ["delivery evidence: profile pr.target_branch is unavailable"]
        return _missing_push_pr_evidence(
            project_root,
            text,
            target_branch=target_branch,
        )
    return []


def _delivery_fields(
    text: str, names: tuple[str, ...]
) -> tuple[dict[str, str], list[str]]:
    body = unfenced_markdown_text(text)
    fields: dict[str, str] = {}
    errors: list[str] = []
    for name in names:
        values = [
            match.group(1).strip()
            for match in re.finditer(
                rf"^[ \t]*{re.escape(name)}[ \t]*:[ \t]*(.*?)[ \t]*$",
                body,
                re.MULTILINE,
            )
            if match.group(1).strip()
        ]
        if len(values) != 1:
            errors.append(
                f"delivery evidence: {name}: requires exactly one non-empty value"
            )
            continue
        fields[name] = values[0]
    return fields, errors


def _missing_commit_evidence(project_root: Path, text: str) -> list[str]:
    fields, errors = _delivery_fields(text, ("commit-oid", "commit-subject"))
    if errors:
        return errors

    head = git_safe(
        "rev-parse", "--verify", "HEAD^{commit}",
        cwd=project_root,
        optional_locks=False,
    )
    if not head.ok or not head.stdout.strip():
        return ["delivery evidence: cannot prove current git HEAD"]
    head_oid = head.stdout.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", fields["commit-oid"].lower()):
        errors.append("delivery evidence: commit-oid must be a full git object id")
    elif fields["commit-oid"].lower() != head_oid:
        errors.append("delivery evidence: commit-oid does not match current HEAD")

    branch = git_safe(
        "symbolic-ref", "--quiet", "--short", "HEAD",
        cwd=project_root,
        optional_locks=False,
    )
    if not branch.ok or not branch.stdout.strip():
        errors.append("delivery evidence: detached or unknown current branch")
    elif branch.stdout.strip() in PROTECTED_BRANCHES:
        errors.append(
            f"delivery evidence: protected branch {branch.stdout.strip()} cannot be committed"
        )

    status = git_safe(
        "status", "--porcelain=v1", "--untracked-files=normal",
        cwd=project_root,
        optional_locks=False,
    )
    if not status.ok:
        errors.append("delivery evidence: cannot prove a clean git worktree")
    elif status.stdout.strip():
        errors.append("delivery evidence: git worktree is not clean")

    subject = git_safe(
        "show", "-s", "--format=%s", head_oid,
        cwd=project_root,
        optional_locks=False,
    )
    if not subject.ok:
        errors.append("delivery evidence: cannot read the committed subject")
    else:
        actual_subject = subject.stdout.rstrip("\r\n")
        if fields["commit-subject"] != actual_subject:
            errors.append(
                "delivery evidence: commit-subject does not match current HEAD"
            )
        if not CONVENTIONAL_COMMIT_RE.fullmatch(actual_subject):
            errors.append(
                "delivery evidence: current HEAD subject is not a Conventional Commit"
            )
    return errors


def _profile_pr_target(profile: dict[str, Any] | None) -> str | None:
    pr = profile.get("pr") if isinstance(profile, dict) else None
    target = pr.get("target_branch") if isinstance(pr, dict) else None
    return target.strip() if isinstance(target, str) and target.strip() else None


def _missing_push_pr_evidence(
    project_root: Path,
    text: str,
    *,
    target_branch: str,
) -> list[str]:
    fields, errors = _delivery_fields(
        text,
        ("remote", "branch", "remote-oid", "pr-url", "pr-base"),
    )
    if errors:
        return errors
    if fields["pr-base"] != target_branch:
        errors.append(
            f"delivery evidence: pr-base must match profile target {target_branch}"
        )
    if not re.fullmatch(r"https://[^\s]+", fields["pr-url"]):
        errors.append("delivery evidence: pr-url must be an HTTPS URL")

    head = git_safe(
        "rev-parse", "--verify", "HEAD^{commit}",
        cwd=project_root,
        optional_locks=False,
    )
    branch = git_safe(
        "symbolic-ref", "--quiet", "--short", "HEAD",
        cwd=project_root,
        optional_locks=False,
    )
    if not head.ok or not head.stdout.strip():
        errors.append("delivery evidence: cannot prove current git HEAD")
    if not branch.ok or not branch.stdout.strip():
        errors.append("delivery evidence: detached or unknown current branch")
    if errors:
        return errors

    head_oid = head.stdout.strip().lower()
    branch_name = branch.stdout.strip()
    if fields["branch"] != branch_name:
        errors.append("delivery evidence: branch does not match the current branch")
    if fields["remote-oid"].lower() != head_oid:
        errors.append("delivery evidence: remote-oid does not match local HEAD")

    remotes = git_safe("remote", cwd=project_root, optional_locks=False)
    if not remotes.ok or fields["remote"] not in remotes.stdout.splitlines():
        errors.append("delivery evidence: named git remote is unavailable")
    else:
        remote_ref = f"refs/heads/{branch_name}"
        remote = git_safe(
            "ls-remote", "--heads", fields["remote"], remote_ref,
            cwd=project_root,
            timeout_s=30,
            optional_locks=False,
        )
        rows = [line.split() for line in remote.stdout.splitlines()] if remote.ok else []
        matching = [
            row[0].lower()
            for row in rows
            if len(row) == 2 and row[1] == remote_ref
        ]
        if len(matching) != 1:
            errors.append("delivery evidence: cannot prove the pushed remote branch OID")
        elif matching[0] != head_oid or matching[0] != fields["remote-oid"].lower():
            errors.append(
                "delivery evidence: local HEAD, remote-oid, and pushed branch OID differ"
            )

    pr = run_safe_command(
        (
            "gh", "pr", "view", fields["pr-url"], "--json",
            "url,baseRefName,headRefName,headRefOid",
        ),
        cwd=project_root,
        env=sanitized_worker_env(),
        timeout_s=30,
    )
    if not pr.ok:
        errors.append("delivery evidence: gh cannot prove the pull request")
        return errors
    try:
        payload = json.loads(pr.stdout)
    except json.JSONDecodeError:
        errors.append("delivery evidence: gh returned invalid pull request evidence")
        return errors
    if not isinstance(payload, dict):
        return [*errors, "delivery evidence: gh returned invalid pull request evidence"]

    actual_url = payload.get("url")
    if (
        not isinstance(actual_url, str)
        or not actual_url.startswith("https://")
        or actual_url.rstrip("/") != fields["pr-url"].rstrip("/")
    ):
        errors.append("delivery evidence: pr-url does not match the live pull request")
    if payload.get("baseRefName") != target_branch:
        errors.append(
            f"delivery evidence: live pull request base is not {target_branch}"
        )
    if payload.get("headRefName") != branch_name:
        errors.append("delivery evidence: live pull request head branch differs")
    gh_head_oid = payload.get("headRefOid")
    if (
        not isinstance(gh_head_oid, str)
        or gh_head_oid.lower() != head_oid
        or gh_head_oid.lower() != fields["remote-oid"].lower()
    ):
        errors.append(
            "delivery evidence: local HEAD, remote-oid, and PR headRefOid differ"
        )
    return errors


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
            project_root=project_root,
        )

    from_kit_profiles = _read_kit_profiles(project_root)
    if from_kit_profiles:
        return _load_profile_union(
            kit_root,
            from_kit_profiles,
            explicit_fallback=explicit_fallback,
            project_root=project_root,
        )

    from_kit = _read_kit_profile(project_root)
    profile_id = from_kit or "generic"
    return _load_single_profile(
        kit_root,
        profile_id,
        strict_missing=bool(from_kit),
        explicit_fallback=explicit_fallback,
        source=".agent-flow/kit.json:profile" if from_kit else "default",
        project_root=project_root,
    )


def _packaged_profile_path(profile_id: str) -> Path | None:
    """설치된 `agent_flow` 패키지가 싣고 있는 profile 정의."""
    package_dir = package_root()
    if package_dir is None:
        return None
    path = package_dir / "profiles" / f"{profile_id}.yaml"
    _ensure_child_path(package_dir / "profiles", path, "profile")
    return path if path.is_file() else None


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

    profile_path = kit_root / "profiles" / f"{profile_id}.yaml"
    _ensure_child_path(kit_root / "profiles", profile_path, "profile")
    if project_root is not None:
        installed_profile = project_profile_path(project_root, profile_id)
        if installed_profile.is_file():
            profile_path = installed_profile
    if not profile_path.exists():
        # 워크플로 정의와 같은 규율이다 — 정본은 패키지 자원이고 kit root 사본은
        # 설치본이 덮어쓰는 자리다. 사본이 없다고 "없는 profile"로 판정하면
        # 루트 사본을 지울 수 없다.
        packaged = _packaged_profile_path(profile_id)
        if packaged is not None:
            profile_path = packaged
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
        if project_root is not None:
            installed_generic = project_profile_path(project_root, profile_id)
            if installed_generic.is_file():
                profile_path = installed_generic
        if not profile_path.exists():
            packaged_generic = _packaged_profile_path("generic")
            if packaged_generic is not None:
                profile_path = packaged_generic

    raw = yaml.safe_load(profile_path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"profile {profile_path}: top-level must be a mapping")
    if raw.get("id") != profile_id:
        raise ValueError(f"profile id mismatch: {profile_id}")
    if project_root is not None:
        raw = apply_project_profile_override(raw, profile_id=profile_id, root=project_root)
    return profile_id, raw


def _load_profile_union(
    kit_root: Path,
    profile_ids: list[str],
    *,
    explicit_fallback: bool,
    project_root: Path | None = None,
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
            project_root=project_root,
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
