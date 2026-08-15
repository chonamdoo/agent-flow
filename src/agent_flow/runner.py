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
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from agent_flow.adapters.auto import detect_adapter
from agent_flow.adapters.generic import STUB_SENTINEL
from agent_flow.artifact import (
    ACTIVE_LOCK,
    META_FILE,
    create_run,
    mark_inactive,
    read_meta,
    write_meta,
)
from agent_flow.cli_detect import CliInfo, REVIEW_CLI_NAMES, detect_available_clis
from agent_flow.core.commands import run_safe_command
from agent_flow.core.command_evidence import missing_test_evidence_markers
from agent_flow.core.context_contract import run_relative_path
from agent_flow.core.observation import (
    PHASE_ENTERED as OBS_PHASE_ENTERED,
    PHASE_EXITED as OBS_PHASE_EXITED,
    PROMPT_RENDERED as OBS_PROMPT_RENDERED,
    VALIDATION_FAILED as OBS_VALIDATION_FAILED,
    PhaseObservation,
    record_observation,
)
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
from agent_flow.core.leader_tripwire import leader_sweep_include_ignored
from agent_flow.core.worktrees import (
    CleanupBlockedError,
    complete_worktree_cleanup,
    run_worktree_cleanup_transaction,
    worktree_run_activation,
)
from agent_flow.core.worktree_isolation import (
    HOST_PHASE_LEADER_BASELINE_KEY,
    LEADER_SNAPSHOT_VERSION,
    STATUS_DRIFT_KIND,
    LeaderDrift,
    LeaderDriftError,
    LeaderSnapshot,
    WorktreeIsolationError,
    assert_leader_unchanged,
    capture_leader_snapshot,
    exclusive_file_lease,
    git_safe,
    leader_drift_paths,
    leader_root_for,
    leader_snapshot_payload,
    leader_sweep_includes_ignored,
    leader_sweep_scope,
    real_path,
    recorded_snapshot_scope,
    recorded_snapshot_version,
    resolve_run_subpath,
    sanitized_worker_env,
    write_run_subpath_text,
)
from agent_flow.core.atomic_io import fsync_directory
from agent_flow.core.profiles import (
    GATE_PHASE_ALL,
    apply_project_profile_override,
    project_profile_path,
)
from agent_flow.core.phase_workflow import (
    ACCEPT_WORKFLOW_DRIFT_FLAG,
    CorruptRunCursorError,
    CursorScope,
    PhaseWorkflowDefinition,
    RunCursor,
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
# fix collector 판정에 쓰는 rejection verdict 키. review/gate가 "다시 해라"라고
# 되돌려 보내는 route만 상한 대상이다 — 정상 진행(default·approve·green)과 PR
# 이벤트 루프(comments·ci-failed)는 여기 없어서 상한에서 빠진다.
_FIX_COLLECTOR_ROUTE_KEYS = frozenset({"request-changes", "blocked", "error", "fail"})
# 전이 원장. run_dir 안에 두되 **phase artifact가 아니다** — backward route의 무효화
# 대상은 workflow가 선언한 artifact뿐이고, 이 파일이 그 범위에 들면 복구 근거가
# 복구 대상과 함께 사라진다.
TRANSITIONS_FILE = "transitions.jsonl"
# 전이 lease는 새 lock을 만들지 않고 run 생성이 이미 쓰는 lifecycle lease를 그대로
# 잡는다. run 하나의 생성과 전이는 시간상 겹치지 않고, 같은 state root에서 동시에
# 도는 run은 활성 run 가드가 이미 막는다. run_dir 안에 lock 파일을 새로 만들면
# cleanup의 run 트리 검사와 산출물 목록에 그 파일이 섞인다.
# 키 이름은 baseline을 쓰는 쪽과 읽는 쪽이 공유해야 한다.
_HOST_PHASE_LEADER_BASELINE = HOST_PHASE_LEADER_BASELINE_KEY
# baseline **레코드 자체**의 필드 구성 버전. 그 안에 담기는 스냅샷의 형식 버전은
# 별개 축이고 `LeaderSnapshot.version`이 들고 있다. 이 값은 `run_id`/`phase_id`/
# `leader_root`/`snapshot`의 의미가 바뀔 때만 올린다.
#
# v2는 v1의 `phase_index`를 뺐다. cursor의 index는 phase 이름에서 나오는 파생값이라
# 승인된 drift가 정의를 재배치하면 이름이 그대로여도 움직인다. 그 값을 기록에 남겨
# 등호로 대조하면 baseline이 두 번째 권위가 되어 정당한 재개를 죽인다.
_HOST_PHASE_BASELINE_RECORD_VERSION = 2
# 사용자가 확인한 leader 변경을 담는 자리. baseline과 **다른 키**여야 한다 —
# baseline 레코드는 필드 집합을 등호로 검사하므로 여기에 얹으면 malformed가 된다.
_HOST_PHASE_LEADER_DRIFT = "host_phase_leader_drift"
_LEADER_DRIFT_ACKNOWLEDGEMENTS = "leader_drift_acknowledgements"
_HOST_PHASE_DRIFT_RECORD_VERSION = 1
ACCEPT_LEADER_DRIFT_FLAG = "--accept-leader-drift"
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


class RouteDecision(NamedTuple):
    """route 판정 한 벌: 어디로, 막혔는지, 그리고 그렇게 판정한 key.

    key를 인스턴스 속성으로 흘려보내던 동안은 `_plan_transition`이 `_next_index`
    **바로 다음에** 불려야만 원장의 `route_key`가 실제 판정과 같았다. 원장의
    route_key는 재개가 왜 되돌아갔는지를 말하는 유일한 근거이므로, 호출 순서가
    아니라 반환값으로 건넨다.
    """

    to_index: int
    blocked: bool
    route_key: str


@dataclass(frozen=True)
class PhaseTransition:
    """phase를 떠나는 한 번의 이동. 계산만 담고 디스크는 건드리지 않는다.

    계산과 커밋을 갈라 두어야 원장(journal)을 **먼저** 적을 수 있다. 예전처럼
    무효화가 계산 도중 일어나면 route 근거가 지워진 뒤 cursor를 쓰기 전에 죽었을 때
    남는 것이 없다.
    """

    from_index: int
    from_phase: str
    route_key: str
    to_index: int
    to_phase: str
    blocked: bool
    # run_dir 기준 상대 경로. backward route가 지울 artifact.
    invalidated: tuple[str, ...] = ()
    # 앞으로 건너뛴 phase에 남길 skip 표식. (run_dir 기준 상대 경로, 내용).
    skipped: tuple[tuple[str, str], ...] = ()

    def journal_record(self, at: str) -> dict[str, Any]:
        # skip 표식은 경로만이 아니라 내용까지 적는다. 원장 한 줄만 보고 전이를
        # 다시 끝낼 수 있어야 재개가 성립하고, 내용을 workflow에서 되계산하면
        # 그 사이 workflow가 바뀐 경우 재개가 다른 파일을 쓴다.
        return {
            "at": at,
            "from_index": self.from_index,
            "from_phase": self.from_phase,
            "route_key": self.route_key,
            "to_index": self.to_index,
            "to_phase": self.to_phase,
            "blocked": self.blocked,
            "invalidated": list(self.invalidated),
            "skipped": [
                {"path": path, "content": content} for path, content in self.skipped
            ],
        }

    @classmethod
    def from_record(cls, record: object) -> PhaseTransition | None:
        """원장 한 줄을 전이로 되읽는다. 형식이 어긋나면 ``None``.

        여기서 예외를 올리면 손상된 원장 한 줄이 run 전체를 재개 불가로 만든다.
        meta는 여전히 정본이므로, 못 읽은 줄은 "재개할 전이 없음"으로 접는다.
        """
        if not isinstance(record, dict):
            return None
        try:
            from_index = record["from_index"]
            to_index = record["to_index"]
            to_phase = record["to_phase"]
        except KeyError:
            return None
        if (
            isinstance(from_index, bool)
            or isinstance(to_index, bool)
            or not isinstance(from_index, int)
            or not isinstance(to_index, int)
            or not isinstance(to_phase, str)
        ):
            return None
        invalidated = record.get("invalidated")
        if not isinstance(invalidated, list) or any(
            not isinstance(item, str) for item in invalidated
        ):
            return None
        skipped: list[tuple[str, str]] = []
        raw_skipped = record.get("skipped") or []
        if not isinstance(raw_skipped, list):
            return None
        for item in raw_skipped:
            if not isinstance(item, dict):
                return None
            path = item.get("path")
            content = item.get("content")
            if not isinstance(path, str) or not isinstance(content, str):
                return None
            skipped.append((path, content))
        return cls(
            from_index=from_index,
            from_phase=str(record.get("from_phase", "")),
            route_key=str(record.get("route_key", "")),
            to_index=to_index,
            to_phase=to_phase,
            blocked=bool(record.get("blocked", False)),
            invalidated=tuple(invalidated),
            skipped=tuple(skipped),
        )


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
        accept_leader_drift: bool = False,
        accept_workflow_drift: bool = False,
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
        self.accept_leader_drift = accept_leader_drift
        self.accept_workflow_drift = accept_workflow_drift
        self.kit_root = _find_kit_root()
        if run_dir is not None:
            meta = read_meta(run_dir)
            self.workflow_name = meta.get("workflow", workflow)
            self.architecture = meta.get("architecture", architecture)
        self.workflow = load_phase_workflow_definition(
            self.kit_root, self.workflow_name
        )
        self.phases = _phases_from_definition(self.workflow)
        self.profile_id, self.profile = _load_profile(self.kit_root, self.config_root)
        # run 한 번 안에서 범위가 바뀌면 baseline과 관측이 서로 다른 범위가 된다.
        # 여기서 한 번 정해 굳힌다. 병합된 `self.profile`이 아니라 leader의 선언
        # 파일을 직접 보는 해석기를 쓴다 — 병합은 어느 파일이 선언했는지를 지우고,
        # 좁힘 판정에는 바로 그 provenance가 필요하다.
        self._leader_include_ignored = leader_sweep_include_ignored(
            leader_root_for(self.project_root)
        )
        self._leader_scope = leader_sweep_scope(self._leader_include_ignored)

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
        # 커서를 읽기 **전에** 끝나지 않은 전이를 마무리한다. 그 상태의 meta는
        # 원장보다 한 걸음 뒤에 있고, 그대로 커서를 세우면 방금 무효화하기로 한
        # phase를 다시 실행한다.
        self._resume_pending_transition()
        run_meta = read_meta(self.run_dir)
        cursor = self._run_cursor(run_meta)
        banner_phase = (
            self.phases[cursor.phase_index]
            if cursor.phase_index < len(self.phases)
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
        # 정확히 무엇을 주입했는지는 envelope를 만드는 자리에서만 알 수 있다.
        # 여기서 잡지 않으면 run이 끝난 뒤 프롬프트를 되살릴 방법이 없다.
        # multi-review phase는 envelope를 두 번 만든다(host, reviewer base).
        # 이름을 phase_id로만 지으면 둘이 같은 payload 이름을 써서 trace 독자가
        # 호스트가 실제로 받은 쪽을 구분할 수 없다. 이름은 만든 자리가 정한다.
        adapter._observer = lambda phase_id, payload_name, envelope: self._emit_observation(
            OBS_PROMPT_RENDERED,
            phase_id,
            payload=envelope,
            payload_name=payload_name,
        )

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
        phase_index = self._run_cursor(meta).phase_index
        while phase_index < len(self.phases):
            phase = self.phases[phase_index]
            leader_before = self._verify_host_phase_leader_baseline(
                meta=meta,
                phase=phase,
                leader_root=leader_root,
            )
            # 진입 시각은 phase를 **시작할 때** 찍는다. 실행 뒤에 찍으면 방금 쓴
            # artifact가 진입 시각보다 과거가 되어 stale로 오판된다. 전이가 다음
            # phase를 미리 stamp해 두었으면 다시 찍을 것이 없다.
            if (
                not meta.get("phase_entered_at")
                or meta.get("current_phase") != phase.id
            ):
                self._stamp_phase(meta, phase_index)
                write_meta(self.run_dir, meta)
            # 방출은 stamp 여부와 무관하다. stamp에 묶어 두면 정상 전이한 두 번째
            # 이후 phase는 `phase_entered`가 아예 없고, 약속한 trace 순서
            # (entered → rendered → exited)가 첫 phase에서만 참이 된다.
            self._emit_observation(
                OBS_PHASE_ENTERED,
                phase.id,
                details={"phase_index": phase_index},
            )
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
                    transition = self._plan_transition(phase_index, phase)
                    meta = self._commit_transition(transition)
                    phase_index, blocked = transition.to_index, transition.blocked
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
                transition = self._plan_transition(phase_index, phase)
                meta = self._commit_transition(transition)
                phase_index, blocked = transition.to_index, transition.blocked
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
                leader_before = capture_leader_snapshot(
                    leader_root, include_ignored=self._leader_include_ignored
                )
                self._persist_host_phase_leader_baseline(
                    phase=phase,
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
            transition = self._plan_transition(phase_index, phase)
            meta = self._commit_transition(transition)
            phase_index, blocked = transition.to_index, transition.blocked
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
        leader_root: Path | None,
    ) -> LeaderSnapshot | None:
        raw = meta.get(_HOST_PHASE_LEADER_BASELINE)
        if raw is None:
            return None
        if leader_root is None:
            raise WorktreeIsolationError(
                "durable host-phase leader baseline exists without a linked worktree"
            )
        if not isinstance(raw, dict):
            raise WorktreeIsolationError(
                "durable host-phase leader baseline is malformed"
            )
        assert self.run_dir is not None
        expected_root = str(real_path(leader_root))
        # 버전을 필드 집합보다 **먼저** 본다. 순서가 반대면 예전 형식의 기록은
        # 필드가 다르다는 이유로 malformed 하드 raise에 걸려, 아래 재캡처 경로에
        # 도달하지 못한 채 업그레이드를 걸친 run이 재개 불가가 된다.
        record_version = recorded_snapshot_version(raw.get("version"))
        if record_version != _HOST_PHASE_BASELINE_RECORD_VERSION:
            # 레코드 필드 구성이 다르면 그 안의 값들을 현재 의미로 읽을 수 없다.
            # 스냅샷 형식과 같은 처리를 한다 — 하드 raise로 막으면 업그레이드를
            # 걸친 run이 재개 불가가 되고, 그건 이 경로가 없애려던 교착이다.
            self._migrate_stale_host_phase_baseline(
                meta,
                f"record format v{record_version}",
                f"v{_HOST_PHASE_BASELINE_RECORD_VERSION}",
            )
            return None
        if set(raw) != {
            "version",
            "run_id",
            "phase_id",
            "leader_root",
            "snapshot",
        }:
            raise WorktreeIsolationError(
                "durable host-phase leader baseline is malformed"
            )
        # 동일성은 run·phase 이름·leader 체크아웃이 진다. index는 여기 없다 —
        # 승인된 drift가 정의를 재배치하면 같은 phase가 다른 자리에 앉고, index를
        # 대조하면 그 정당한 재개가 오염 보고로 죽는다.
        if (
            raw.get("run_id") != self.run_dir.name
            or raw.get("phase_id") != phase.id
            or raw.get("leader_root") != expected_root
        ):
            raise WorktreeIsolationError(
                "durable host-phase leader baseline does not match the current "
                "run, phase, or leader checkout"
            )
        snapshot_raw = raw.get("snapshot")
        if not isinstance(snapshot_raw, dict) or not {
            "head",
            "branch",
            "status",
            "armed",
        } <= set(snapshot_raw) <= {
            "head",
            "branch",
            "status",
            "armed",
            "version",
            "scope",
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
            version=recorded_snapshot_version(snapshot_raw.get("version")),
            scope=recorded_snapshot_scope(snapshot_raw.get("scope")),
        )
        if not snapshot.comparable:
            self._migrate_stale_host_phase_baseline(
                meta,
                f"snapshot format v{snapshot.version}",
                f"v{LEADER_SNAPSHOT_VERSION}",
            )
            return None
        if not snapshot.comparable_within(self._leader_scope):
            # profile이 sweep 범위를 바꿨다. 범위가 다른 두 기록은 leader를 아무도
            # 건드리지 않아도 항상 다르므로 새 범위로 다시 찍어야 한다.
            #
            # 다만 형식 전환과 달리 여기서는 **기록된 범위로 먼저 대조한다**. 형식은
            # kit 업그레이드가 바꾸지만 범위는 파일 한 줄이 바꾸고, 그 파일은 워커가
            # 닿을 수 있는 자리에 있다. 대조 없이 버리면 범위를 좁히는 그 phase 하나가
            # 검사 없는 통과가 되고, 그 창이 정확히 워커가 노릴 자리다.
            #
            # 승인 경로를 대조보다 **앞에** 둔다. 뒤에 두면 도달하지 못한다 — 대조가
            # raise하면 그 아래가 실행되지 않고 baseline도 그대로 남아, 다음 재개가
            # 같은 지점에서 다시 막힌다. 그러면 이 knob이 존재하는 이유인 "leader가
            # 계속 움직이는 상황"에서 광고된 해제 명령이 무효가 된다.
            recorded_include_ignored = leader_sweep_includes_ignored(snapshot.scope)
            accepted = self._accept_recorded_leader_drift(
                meta=meta,
                raw=raw,
                leader_root=leader_root,
                include_ignored=recorded_include_ignored,
            )
            if accepted is None:
                self._assert_leader_unchanged(
                    leader_root, snapshot, include_ignored=recorded_include_ignored
                )
            self._migrate_stale_host_phase_baseline(
                meta,
                f"sweep scope {snapshot.scope}",
                self._leader_scope,
            )
            return None
        accepted = self._accept_recorded_leader_drift(
            meta=meta, raw=raw, leader_root=leader_root
        )
        if accepted is not None:
            return accepted
        self._assert_leader_unchanged(leader_root, snapshot)
        if meta.pop(_HOST_PHASE_LEADER_DRIFT, None) is not None:
            # leader가 스스로 돌아왔다. 남은 보고 기록은 나중에 같은 상태가 우연히
            # 재현될 때 승인을 대신 서 줄 수 있으므로 여기서 버린다.
            assert self.run_dir is not None
            write_meta(self.run_dir, meta)
        return snapshot

    def _accept_recorded_leader_drift(
        self,
        *,
        meta: dict[str, Any],
        raw: dict[str, Any],
        leader_root: Path,
        include_ignored: bool | None = None,
    ) -> LeaderSnapshot | None:
        """사용자가 확인한 leader 변경만 새 기준선으로 굳힌다. 아니면 ``None``.

        경로를 비교에서 빼지 않는 이유: ignored 경로에는 빌드 산출물만 있는 게
        아니라 host가 **실행하는** 것도 있다(`.agent-flow/scripts/hooks/`,
        `.claude/hooks/`, `.venv/bin`). 예외 목록을 만들면 그 자리의 변조가 영원히
        조용해진다. 그래서 탐지 범위는 그대로 두고 응답만 바꾼다 — 무엇이 바뀌었는지
        전부 보여주고, 사람이 받아들인 그 상태에서 다시 시작한다.

        승인은 **보여준 그 상태**에만 붙는다. 보고 이후 leader가 또 움직였으면
        기록을 버리고 통과시키지 않는다. 그러지 않으면 승인 한 번이 이후의 모든
        변경까지 함께 덮어, 사람이 본 적 없는 오염이 기준선이 된다.
        """
        assert self.run_dir is not None
        if not self.accept_leader_drift:
            return None
        recorded = meta.get(_HOST_PHASE_LEADER_DRIFT)
        if (
            not isinstance(recorded, dict)
            or recorded.get("version") != _HOST_PHASE_DRIFT_RECORD_VERSION
            or recorded.get("run_id") != self.run_dir.name
            or recorded.get("kind") != STATUS_DRIFT_KIND
            or not isinstance(recorded.get("observed"), dict)
        ):
            return None
        # 보고했던 그 범위로 다시 찍는다. 범위 전환 중이라면 기록은 옛 범위로
        # 만들어졌으므로, 새 범위로 찍어 비교하면 leader가 그대로여도 절대 일치하지
        # 않아 승인이 영원히 성립하지 않는다.
        observed = capture_leader_snapshot(
            leader_root,
            include_ignored=(
                self._leader_include_ignored
                if include_ignored is None
                else include_ignored
            ),
        )
        if leader_snapshot_payload(observed) != recorded["observed"]:
            # 보고한 상태가 아니다. 기록을 버려 다음 검사가 현재 차이를 새로 보고한다.
            meta.pop(_HOST_PHASE_LEADER_DRIFT, None)
            write_meta(self.run_dir, meta)
            return None
        paths = [str(path) for path in recorded.get("paths") or ()]
        meta[_HOST_PHASE_LEADER_BASELINE] = {
            **raw,
            "snapshot": leader_snapshot_payload(observed),
        }
        meta.setdefault(_LEADER_DRIFT_ACKNOWLEDGEMENTS, []).append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "phase_id": raw.get("phase_id"),
                "paths": paths,
            }
        )
        meta.pop(_HOST_PHASE_LEADER_DRIFT, None)
        write_meta(self.run_dir, meta)
        print(
            f"  [accepted] leader drift acknowledged ({len(paths)} paths); "
            "re-baselined to the state reported above"
        )
        return observed

    def _migrate_stale_host_phase_baseline(
        self, meta: dict[str, Any], recorded: str, expected: str
    ) -> None:
        """비교할 수 없는 기록을 버린다. 이 phase가 새 형식으로 다시 찍는다.

        형식이 다른 기록을 그대로 대조하면 **항상** 차이가 나온다 — leader를 아무도
        건드리지 않아도 그렇다. 그 오탐은 진행 중인 run 전부를 막고 근거가 없으므로
        사용자가 풀 방법도 없다. 하드 raise도 같은 결과다(run 재개 불가).
        """
        assert self.run_dir is not None
        print(
            f"  [migrate] host-phase leader baseline was recorded in "
            f"{recorded}; re-capturing as {expected}"
        )
        meta.pop(_HOST_PHASE_LEADER_BASELINE, None)
        write_meta(self.run_dir, meta)

    def _assert_leader_unchanged(
        self,
        leader_root: Path,
        snapshot: LeaderSnapshot,
        *,
        include_ignored: bool | None = None,
    ) -> None:
        assert self.run_dir is not None
        try:
            assert_leader_unchanged(
                leader_root,
                snapshot,
                run_id=self.run_dir.name,
                worker_root=self.project_root,
                include_ignored=(
                    self._leader_include_ignored
                    if include_ignored is None
                    else include_ignored
                ),
            )
        except LeaderDriftError as exc:
            paths = self._record_leader_drift(exc.drift)
            if exc.drift.kind != STATUS_DRIFT_KIND:
                # HEAD 축은 "Investigate before continuing"이 안내다. 여기에 해제
                # 명령을 광고하면 `reset --hard`로 사라진 커밋까지 승인된다.
                raise
            # 잘리지 않은 전체 목록. 승인은 관측 전체에 붙으므로 공개도 전체여야
            # 한다 — 데코이 뒤에 숨긴 경로가 안 보인 채 기준선이 되면 안 된다.
            print(f"  [leader-drift] {len(paths)} changed paths:")
            for path in paths:
                print(f"    {path}")
            raise LeaderDriftError(
                f"{exc} All {len(paths)} changed paths are listed above. "
                "Yours and not the worker's? "
                f"`{self.next_command} {ACCEPT_LEADER_DRIFT_FLAG}` re-baselines to "
                "exactly that state and records the acknowledgement.",
                exc.drift,
            ) from None

    def _record_leader_drift(self, drift: LeaderDrift) -> tuple[str, ...]:
        """보고한 상태를 남기고 공개할 경로를 돌려준다.

        승인은 이 기록과 대조해서만 통한다. `reasons`가 아니라 `paths`를 남기는
        이유는 `reasons`가 8개에서 잘리기 때문이다.
        """
        assert self.run_dir is not None
        paths = leader_drift_paths(drift)
        meta = read_meta(self.run_dir)
        meta[_HOST_PHASE_LEADER_DRIFT] = {
            "version": _HOST_PHASE_DRIFT_RECORD_VERSION,
            "run_id": self.run_dir.name,
            "kind": drift.kind,
            "paths": list(paths),
            "observed": leader_snapshot_payload(drift.after),
        }
        write_meta(self.run_dir, meta)
        return paths

    def _persist_host_phase_leader_baseline(
        self,
        *,
        phase: Phase,
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
            "version": _HOST_PHASE_BASELINE_RECORD_VERSION,
            "run_id": self.run_dir.name,
            "phase_id": phase.id,
            "leader_root": str(real_path(leader_root)),
            "snapshot": leader_snapshot_payload(snapshot),
        }
        write_meta(self.run_dir, meta)

    def _next_index(self, current_index: int, phase: Phase) -> RouteDecision:
        """route가 가리키는 다음 자리와 그렇게 판정한 key.

        **phase artifact는 건드리지 않는다.** 무효화까지 여기서 하면 route 근거가
        cursor보다 먼저 사라져, 그 사이에 죽은 실행은 왜 되돌아갔는지를 잃은 채
        이전 phase를 다시 돈다. 무효화는 ``_commit_transition``이 원장을 적은 뒤에 한다.

        디스크를 아예 안 만지는 것은 아니다. 설계 원장과 fix-loop 라운드 카운터는
        여기서 굳힌다 — 둘 다 phase를 **떠나는 순간**의 사실이고 key로 멱등해서,
        같은 판정을 두 번 계산해도 값이 늘거나 갈리지 않는다.
        """
        # 설계 원장은 phase를 떠나는 이 자리에서 굳혀야 skip 경로(재개)와 실행 경로
        # 양쪽에서 같은 값이 다음 phase로 넘어간다.
        self._capture_design_ledger(phase)
        # route가 없는 phase는 판정 자체가 없다.
        if not phase.routes:
            return RouteDecision(current_index + 1, False, "none")
        assert self.run_dir is not None
        artifact = self._existing_artifact_path(phase)
        try:
            # phase artifact는 agent가 쓴 입력이다. decode 오류는 `OSError`가
            # 아니라 `ValueError`라 여기서 올리면 잘못된 바이트 하나가 사유 없는
            # 예외로 run을 세운다. 읽은 만큼으로 판정하고, 아예 못 읽으면 빈
            # 문서로 본다 — gates는 그 결과 malformed-results로 막힌다.
            text = artifact.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if phase.multi_review:
            key = _multi_review_route_key(text, phase.id)
        elif phase.id == "gates":
            key = _gates_route_key(text, nonce=str(read_meta(self.run_dir).get("gate_nonce", "")))
            if key == GATE_MALFORMED:
                detail = _gate_parse_error(text)
                print(f"  [block] gate-results.json is unreadable: {detail}")
                self._emit_observation(
                    OBS_VALIDATION_FAILED,
                    phase.id,
                    details={"status": "blocked", "reason": "malformed_gate_results", "detail": detail},
                )
                return RouteDecision(current_index, True, key)
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
                return RouteDecision(current_index, True, key)
            if key == "insufficient-reviewers":
                print("  [block] multi-review requires 2+ independent sub-agent reviewer verdicts")
                return RouteDecision(current_index, True, key)
            if key == "invalid-verdict":
                print("  [block] multi-review requires overall verdict approve or request-changes")
                return RouteDecision(current_index, True, key)
            if key == "default":
                print(
                    "  [block] multi-review requires ## Overall with exactly one "
                    "verdict: approve or verdict: request-changes line"
                )
                return RouteDecision(current_index, True, key)
        if target == "block":
            print(f"  [block] {phase.id} status={key}")
            return RouteDecision(current_index, True, key)
        if target is None:
            print(f"  [block] {phase.id} status={key} has no route")
            return RouteDecision(current_index, True, key)
        if not target:
            return RouteDecision(current_index + 1, False, key)
        fix_collectors = self._fix_collector_targets()
        for i, candidate in enumerate(self.phases):
            if candidate.id == target:
                # 상한은 리터럴 이름("fix-loop")이 아니라 "fix collector"로 판정한다.
                # collector = 어떤 phase가 rejection verdict(request-changes·blocked·
                # error·fail)로 되돌려 보내는 target이다. 이름은 workflow마다 달라도
                # (fix-loop·refactor·implement-fix·slice-plan) 모두 이 집합에 든다.
                # 일단 collector면 이후 어떤 key로 그리 보내도(gates의 default/blocked/
                # error 포함) 카운트해 gate 재시도 루프가 상한에 걸린다. pr-watch는
                # rejection route의 target이 아니라(pr-comment-fix/pr-ci-fix가 default로
                # 되돌릴 뿐) collector가 아니므로, 정당하게 여러 번 도는 PR 이벤트
                # 루프는 상한에서 빠진다. 카운트는 target별로 나눠 서로 다른 순환이
                # 예산을 공유하지 않게 한다.
                if target in fix_collectors:
                    rounds = self._increment_fix_loop_rounds(target)
                    if rounds > FIX_LOOP_MAX_ROUNDS:
                        print(
                            f"  [block] fix-loop exceeded {FIX_LOOP_MAX_ROUNDS} "
                            f"rounds routing {phase.id} -> {target}"
                        )
                        return RouteDecision(current_index, True, key)
                return RouteDecision(i, False, key)
        raise ValueError(f"phase {phase.id}: route target not found: {target}")

    def _artifact_rel(self, phase: Phase) -> str:
        return phase.artifact or f"{phase.id}.md"

    def _existing_artifact_rel(self, phase: Phase) -> str | None:
        """지금 디스크에 있는 이 phase의 artifact, run_dir 기준 상대 경로."""
        if self._artifact_path(phase).exists():
            return self._artifact_rel(phase)
        if self._legacy_artifact_path(phase).exists():
            return f"{phase.id}.md"
        return None

    def _plan_transition(self, current_index: int, phase: Phase) -> PhaseTransition:
        """이 phase를 떠나는 이동 전체를 값으로 계산한다. phase artifact는 그대로 둔다.

        ``_next_index``가 굳히는 설계 원장·라운드 카운터는 예외다. 그 이유는
        ``_next_index``의 docstring에 있다.
        """
        decision = self._next_index(current_index, phase)
        to_index, blocked = decision.to_index, decision.blocked
        to_phase = self.phases[to_index].id if to_index < len(self.phases) else ""
        invalidated: list[str] = []
        skipped: list[tuple[str, str]] = []
        if not blocked and to_index <= current_index:
            # 되돌아가는 route는 목적지부터 현재 phase까지의 결과를 무효로 만든다.
            # 그 결과를 남겨 두면 재개가 곧바로 skip으로 지나쳐 되돌린 의미가 없다.
            for stale_phase in self.phases[to_index:current_index + 1]:
                relative = self._existing_artifact_rel(stale_phase)
                if relative is not None and relative != TRANSITIONS_FILE:
                    invalidated.append(relative)
        elif not blocked and to_index > current_index + 1:
            for skipped_phase in self.phases[current_index + 1:to_index]:
                if self._artifact_path(skipped_phase).exists():
                    continue
                skipped.append(
                    (
                        self._artifact_rel(skipped_phase),
                        f"# {skipped_phase.id}\n\nstatus: skipped\n"
                        f"reason: route_to_{to_phase}\n",
                    )
                )
        return PhaseTransition(
            from_index=current_index,
            from_phase=phase.id,
            route_key=decision.route_key,
            to_index=to_index,
            to_phase=to_phase,
            blocked=blocked,
            invalidated=tuple(invalidated),
            skipped=tuple(skipped),
        )

    def _commit_transition(self, transition: PhaseTransition) -> dict[str, Any]:
        """전이를 한 번에 굳힌다: 원장 → 무효화 → cursor. 반환값은 갱신된 meta.

        순서가 계약이다. 원장이 먼저 있어야 중간에서 죽은 실행을 다음 실행이
        같은 결론으로 마칠 수 있다. 반대로 무효화가 먼저면 근거가 사라진 채
        이전 phase를 다시 돈다.
        """
        assert self.run_dir is not None
        with exclusive_file_lease(self._transition_lock_path()):
            self._append_transition_journal(transition)
            if not self._apply_transition(transition):
                # 여기서 걸리는 것은 workflow가 선언한 artifact 경로가 run 밖을
                # 가리킨다는 뜻이다. cursor를 옮기면 그 phase의 산출물이 없는데
                # 다음 phase로 나아간다.
                raise WorktreeIsolationError(
                    f"transition {transition.from_phase} -> "
                    f"{transition.to_phase or 'complete'} names an artifact path "
                    f"outside the run directory"
                )
            meta = read_meta(self.run_dir)
            self._advance_phase(meta, transition.to_index, transition.blocked)
            write_meta(self.run_dir, meta)
        self._emit_observation(
            OBS_PHASE_EXITED,
            transition.from_phase,
            details={
                "route_key": transition.route_key,
                "to_phase": transition.to_phase or "complete",
                "blocked": transition.blocked,
                "invalidated": list(transition.invalidated),
            },
        )
        return meta

    def _transition_lock_path(self) -> Path:
        assert self.run_dir is not None
        return self.run_dir.parent / ACTIVE_LOCK

    def _transitions_path(self) -> Path:
        assert self.run_dir is not None
        return self.run_dir / TRANSITIONS_FILE

    def _append_transition_journal(self, transition: PhaseTransition) -> None:
        record = transition.journal_record(datetime.now(timezone.utc).isoformat())
        path = self._transitions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{json.dumps(record, sort_keys=True)}\n")
            handle.flush()
            os.fsync(handle.fileno())
        # 첫 append는 파일 **생성**이다. 부모 디렉터리를 내려보내지 않으면 그
        # 생성이 디스크에 닿지 않아, 전원이 끊긴 뒤 원장 자체가 사라진 채 무효화만
        # 남을 수 있다 — "원장 → 무효화 → cursor" 순서가 지키려던 바로 그 근거다.
        fsync_directory(path.parent)

    def _apply_transition(self, transition: PhaseTransition) -> bool:
        """원장이 선언한 부수 효과를 낸다. 두 번 적용해도 결과가 같아야 한다.

        원장은 run 디렉터리 안에 있고 phase agent가 쓴다. 거기서 온 경로는
        입력이므로 봉쇄를 거친다 — `run_dir / "/etc/passwd"`는 `run_dir`을 통째로
        버리고 그 절대 경로가 된다. 한 항목이라도 run 밖을 가리키면 레코드
        전체를 적용하지 않고 ``False``를 돌려준다. 절반만 적용하면 남은 절반이
        무엇이었는지 아무도 모른다.
        """
        assert self.run_dir is not None
        removals: list[Path] = []
        writes: list[tuple[Path, str]] = []
        for relative in transition.invalidated:
            if relative == TRANSITIONS_FILE:
                continue
            target = self._attested_run_target(relative)
            if target is None:
                return False
            removals.append(target)
        for relative, content in transition.skipped:
            target = self._attested_run_target(relative)
            if target is None:
                return False
            writes.append((target, content))
        for target in removals:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
        for target, content in writes:
            if target.exists():
                continue
            # 표식은 그 phase의 결과로 읽힌다. 제자리 쓰기 중에 죽으면 찢어진
            # 표식이 남고, `exists()`로 건너뛰는 재개는 그것을 영영 고치지 않는다.
            # 봉쇄는 `run_dir` 기준으로 여기서 한 번 더 걸린다.
            write_run_subpath_text(self.run_dir, target, content)
        return True

    def _attested_run_target(self, relative: str) -> Path | None:
        """원장이 말한 상대 경로를 run 안의 실제 target으로 해소한다. 밖이면 ``None``.

        봉쇄 규칙은 `worktree_isolation.resolve_run_subpath` 한 벌이다. 여기서
        규칙을 다시 적으면 두 판정이 갈리고, 갈린 쪽이 곧 우회로가 된다.
        """
        assert self.run_dir is not None
        try:
            return resolve_run_subpath(self.run_dir, Path(relative))
        except (OSError, ValueError, WorktreeIsolationError):
            return None

    def _last_transition(self) -> PhaseTransition | None:
        """마지막으로 **읽히는** 원장 줄. 없으면 ``None``.

        찢어진 마지막 줄은 흔하다 — append 중에 죽으면 그렇게 된다. 그 한 줄로
        "재개할 전이 없음"으로 접으면, 무효화는 이미 끝났고 cursor만 남았던 run은
        근거를 잃는다. 그래서 뒤에서부터 읽히는 줄을 찾는다.
        """
        path = self._transitions_path()
        if not path.exists():
            return None
        try:
            # decode 오류는 `OSError`가 아니다. 여기서 올리면 손상된 바이트 하나가
            # runner 시작 자체를 막는다 — 관측이 아니라 진행을 죽이는 실패다.
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            transition = PhaseTransition.from_record(record)
            if transition is not None:
                return transition
        return None

    def _transition_target_index(self, transition: PhaseTransition) -> int | None:
        """원장 줄이 가리키는 자리를 **현재** 정의에서 찾는다. 없으면 ``None``.

        기록된 `to_index`는 원장을 적을 때의 정의에서 나온 값이다. 그 뒤 승인된
        drift가 phase를 끼워 넣거나 순서를 바꿨으면 같은 숫자가 다른 phase를 연다.
        이름이 정본이므로 이름으로 찾는다 — 멱등성 검사가 이미 `to_phase`를 쓰고
        있어서, 놓는 쪽만 숫자를 보면 두 권위가 갈린다.
        """
        if not transition.to_phase:
            # 이름이 없는 목적지는 마지막 phase를 지난 완료 커서 하나뿐이다.
            return len(self.phases)
        for index, phase in enumerate(self.phases):
            if phase.id == transition.to_phase:
                return index
        return None

    def _resume_pending_transition(self) -> None:
        """원장은 적혔지만 cursor가 아직 안 쓰인 전이를 마저 끝낸다.

        meta가 이미 마지막 원장 줄과 같은 자리를 가리키면 할 일이 없다 — 그래서
        같은 원장 줄을 몇 번 적용해도 결과가 같다.
        """
        assert self.run_dir is not None
        transition = self._last_transition()
        if transition is None:
            return
        with exclusive_file_lease(self._transition_lock_path()):
            meta = read_meta(self.run_dir)
            recorded_phase = meta.get("current_phase") or ""
            target_index = self._transition_target_index(transition)
            if target_index is None:
                # 놓을 자리가 없다. 기록된 index를 믿고 놓으면 이 원장 줄과 아무
                # 관계 없는 phase로 run을 옮긴다 — 적용하지 않고 사람이 원장을
                # 보게 둔다.
                print(
                    f"  [reject] transition journal targets phase "
                    f"{transition.to_phase!r}, which workflow {self.workflow.id} no "
                    f"longer defines; not applying it"
                )
                return
            # blocked 전이는 cursor를 옮기지 않는다. index만 보면 "이미 반영됨"으로
            # 오인해 사유가 빠진 meta를 그대로 둔다.
            blocked_recorded = (
                meta.get("phase_blocked_reason") == "route_blocked"
            ) == transition.blocked
            if (
                meta.get("phase_index") == target_index
                and recorded_phase == transition.to_phase
                and blocked_recorded
            ):
                return
            print(
                f"  [resume] completing interrupted transition "
                f"{transition.from_phase} -> {transition.to_phase or 'complete'}"
            )
            if not self._apply_transition(transition):
                # 원장 줄이 run 밖 경로를 담고 있다. 적용하지 않고 cursor도 두면
                # 사람이 원장을 보고 판단할 수 있다 — 여기서 지우거나 쓰면 그
                # 레코드가 지목한 호스트 파일을 손댄다.
                print(
                    "  [reject] transition journal names a path outside the run "
                    "directory; not applying it"
                )
                return
            self._advance_phase(meta, target_index, transition.blocked)
            write_meta(self.run_dir, meta)

    def _cursor_scope(self) -> CursorScope:
        """커서 검증의 기준. index로 여는 목록(`self.phases`)이 정본이다.

        정의를 합성해 넘기면 그 합성본의 `digest`는 더 이상 원문 바이트의
        sha256이 아니다. 검증에 필요한 것은 phase id 순서와 digest뿐이므로 그
        둘만 담아 넘긴다.
        """
        return CursorScope.of(self.workflow, [phase.id for phase in self.phases])

    def _run_cursor(self, meta: dict[str, Any]) -> RunCursor:
        assert self.run_dir is not None
        cursor = RunCursor.from_meta(
            meta,
            self._cursor_scope(),
            accept_workflow_drift=self.accept_workflow_drift,
        )
        if cursor.reanchored_from is not None:
            # 승인된 drift가 run을 몇 phase 앞뒤로 옮겼다. 훨씬 작은 상태 변화인
            # 중단된 전이 재개도 한 줄을 찍는다 — 이쪽이 조용하면 사용자는 재개가
            # 어디서 다시 시작했는지 알 방법이 없다.
            print(
                f"  [re-anchor] workflow drift moved phase '{cursor.phase_id}' "
                f"{cursor.reanchored_from} -> {cursor.phase_index}"
            )
        # 승인된 drift는 기록된 phase 이름으로 index를 다시 잡는다. 다시 잡은 값을
        # 남기지 않으면 digest만 새로 찍힌 채 옛 index가 meta에 남고, 다음 실행은
        # drift가 사라진 자리에서 index와 이름이 어긋나 `CorruptRunCursorError`로
        # 막힌다. 재배치 여부는 커서가 직접 들고 온다 — 여기서 `기록된 index !=
        # 커서 index`로 추론하면 digest가 어긋난 안에서만 참이 되는 비교를 결정적
        # 분기로 쓰는 것이고, 그건 근거가 될 수 없다.
        if (
            meta.get("workflow_digest") != cursor.workflow_digest
            or cursor.reanchored_from is not None
        ):
            # digest 기록이 없던 예전 run이거나, 사용자가 drift를 승인한 run이다.
            # 어느 쪽이든 지금 정의로 다시 찍어야 다음 실행이 같은 기준을 본다.
            #
            # 단 `write_meta`는 **원자적 교체**다. `read_meta`는 손상·OSError·
            # decode 실패를 stderr로 알리고 빈 dict를 돌려주므로, 읽히지 않은
            # meta에 backfill을 걸면 run_id·task·task_digest·gate_nonce·checkout
            # identity가 첫 `continue`에서 통째로 사라진다. `run_id`는 `create_run`이
            # 항상 박는 값이라, 없다는 것은 이 dict가 meta.json의 내용이 아니라는
            # 뜻이다 — 조용히 덮어쓰지 않고 여기서 멈춘다.
            if not meta.get("run_id"):
                raise CorruptRunCursorError(
                    f"run meta at {self.run_dir / META_FILE} yielded no run_id; "
                    f"refusing to rewrite it. Restore the file from backup, or clear "
                    f"the run with `agent-flow abort`."
                )
            meta["workflow_digest"] = cursor.workflow_digest
            meta["phase_index"] = cursor.phase_index
            write_meta(self.run_dir, meta)
        return cursor

    def _fix_collector_targets(self) -> set[str]:
        """rejection verdict가 되돌려 보내는 target phase들. 이 집합으로 가는 route는
        어떤 key로 가든 한 번의 fix 라운드로 센다. 리터럴 이름에 묶지 않으므로
        fix-loop·refactor·implement-fix·slice-plan을 workflow별로 자동 인식하고,
        pr-watch처럼 rejection route의 target이 아닌 phase(PR 이벤트 루프)는
        제외한다."""
        collectors: set[str] = set()
        for candidate in self.phases:
            routes = getattr(candidate, "routes", None) or {}
            for route_key, route_target in routes.items():
                if route_key in _FIX_COLLECTOR_ROUTE_KEYS and isinstance(route_target, str):
                    collectors.add(route_target)
        return collectors

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

    def _increment_fix_loop_rounds(self, target: str) -> int:
        assert self.run_dir is not None
        meta = read_meta(self.run_dir)
        counts = _fix_loop_round_counts(meta)
        rounds = counts.get(target, 0) + 1
        if rounds > FIX_LOOP_MAX_ROUNDS:
            return rounds
        # 재시작 후에도 상한을 유지하도록 target별 카운트를 run meta에 저장한다.
        counts[target] = rounds
        meta["fix_loop_rounds"] = counts
        write_meta(self.run_dir, meta)
        return rounds

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
                # 관측 로그는 저장소 전체가 공유한다. cwd를 좁히지 않으면 형제
                # worktree에서 돈 테스트가 이 run의 증거로 잡힌다.
                cwd_root=self.project_root,
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

    def _run_relative(self, path: Path | None) -> str | None:
        if path is None or self.run_dir is None:
            return None
        return run_relative_path(path, self.run_dir)

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
        # 사람이 읽는 blocker는 stdout으로만 나가고 사라진다. 같은 판정을
        # trace에도 남겨야 실패한 run을 나중에 재현하거나 eval로 옮길 수 있다.
        if status != "complete":
            self._emit_observation(
                OBS_VALIDATION_FAILED,
                phase.id if phase is not None else "-",
                details={
                    "status": status,
                    "reason": reason,
                    # stdout은 사람이 열 수 있게 절대 경로를 유지한다. trace는
                    # archive와 PR로 나가므로 호스트 레이아웃을 싣지 않는다.
                    "required_artifact": self._run_relative(required_artifact),
                },
            )
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

    def _emit_observation(
        self,
        kind: str,
        phase_id: str,
        *,
        details: dict[str, Any] | None = None,
        payload: str | None = None,
        payload_name: str | None = None,
    ) -> None:
        if self.run_dir is None:
            return
        record_observation(
            run_dir=self.run_dir,
            observation=PhaseObservation(
                kind=kind,
                phase_id=phase_id,
                details=details or {},
                payload=payload,
                payload_name=payload_name,
            ),
        )


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


def _phases_from_definition(definition: PhaseWorkflowDefinition) -> list[Phase]:
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


def _fix_loop_round_counts(meta: dict[str, Any]) -> dict[str, int]:
    raw = meta.get("fix_loop_rounds")
    # 구버전 형식: 정수는 리터럴 "fix-loop" 진입 횟수만 셌다. 업그레이드 중이던
    # run이 상한을 잃지 않도록 그 값을 "fix-loop" target의 초기 카운트로 옮긴다.
    # (bool은 int의 하위형이라 먼저 걸러 오분류를 막는다.)
    if isinstance(raw, bool):
        return {}
    if isinstance(raw, int):
        return {"fix-loop": raw} if raw > 0 else {}
    if not isinstance(raw, dict):
        return {}
    counts: dict[str, int] = {}
    for target, value in raw.items():
        try:
            counts[str(target)] = int(value)
        except (TypeError, ValueError):
            continue
    return counts


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


# route key가 아니라 "판정 불가" 표식이다. workflow가 이 key로 갈 target을
# 선언할 수 없게 이름을 route 어휘 밖에 둔다.
GATE_MALFORMED = "malformed-results"


def _gate_parse_error(text: str) -> str:
    """왜 못 읽었는지 한 줄로. 사유가 없으면 사용자는 무엇을 고칠지 모른다."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return f"invalid JSON at line {exc.lineno} column {exc.colno}"
    if not isinstance(payload, dict):
        return f"top-level value is {type(payload).__name__}, expected an object"
    return "`passed` is missing or not a boolean"


def _gates_route_key(text: str, *, nonce: str = "") -> str:
    # 읽을 수 없는 gate 결과는 "게이트가 실패했다"가 아니라 "판정 자체가 없다"다.
    # 예전처럼 `default`로 접으면 fix-loop가 근거 없이 코드를 고치기 시작하고,
    # 세 라운드를 태운 뒤 상한에 걸려 run이 영구 정지한다.
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return GATE_MALFORMED
    if not isinstance(payload, dict) or not isinstance(payload.get("passed"), bool):
        return GATE_MALFORMED
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
    # ambient `GIT_DIR`/`GIT_WORK_TREE`가 있으면 cwd 대신 그 저장소가 답한다.
    # 여기서 참이 나오면 뒤따르는 phase 전부가 남의 checkout을 대상으로 돈다.
    result = git_safe(
        "rev-parse", "--is-inside-work-tree",
        cwd=project_root,
        timeout_s=5,
        optional_locks=False,
    )
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

    raw = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
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
        data = json.loads(kit_json.read_text(encoding="utf-8"))
    # `UnicodeDecodeError`는 `OSError`가 아니라 `ValueError`다. 좁게 잡으면 손상된
    # kit.json 하나가 여기서 그대로 튀어나온다.
    except (ValueError, OSError):
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
