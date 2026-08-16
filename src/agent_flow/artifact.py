"""Artifact directory management.

Each run lives under the active state root's `.agent-flow/runs/<run-id>/`.
Git worktree runs use a git-private state root so runtime files do not dirty
the worktree checkout. Phases write a single .md artifact when they complete;
the runner uses these files as the source of truth for "what has been done."
An `active` marker file distinguishes the in-flight run.

Robustness fixes applied (post-review):
  - read_meta tolerates missing / malformed JSON (returns {} with warning)
  - write_meta is atomic (tmpfile + os.replace) so Ctrl-C mid-write cannot
    leave a half-written meta.json that crashes the next invocation
  - create_run refuses if an active run already exists (concurrent-run race)
  - create_run produces unique run_ids with a counter suffix when timestamp
    collision occurs (sub-second double-invocation)
"""
from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agent_flow.core.atomic_io import atomic_write_text
from agent_flow.core.design_value_check import missing_spec_item_evidence
from agent_flow.core.local_skills import (
    changed_files,
    missing_local_skill_markers,
    resolved_profile,
)
from agent_flow.core.markers import missing_markers, normalize_required_markers
from agent_flow.core.phase_workflow import find_kit_root, load_phase_workflow_definition
from agent_flow.core.security import validate_safe_name
from agent_flow.core.skill_resolver import PhaseSkills
from agent_flow.core.worktree_isolation import (
    FileLeaseUnavailable,
    exclusive_file_lease,
    write_run_artifact_text,
)


RUNS_DIRNAME = ".agent-flow/runs"
ACTIVE_MARKER = "active"
META_FILE = "meta.json"
ACTIVE_LOCK = "active.lock"


class ActiveRunExists(RuntimeError):
    """Raised when create_run is called but an active run already exists."""


@dataclass
class ActiveRun:
    path: Path
    run_id: str
    workflow: str
    task: str
    started_at: str

    def print_status(
        self,
        *,
        next_command: str = "agent-flow continue",
        config_root: Path | None = None,
        project_root: Path | None = None,
    ) -> None:
        artifacts = sorted(str(p.relative_to(self.path)) for p in self.path.rglob("*") if p.is_file())
        meta = read_meta(self.path)
        current_phase = meta.get("current_phase") or "-"
        artifact_rel, _markers, _skills = _phase_contract(self.path, self.workflow, current_phase)
        required_artifact = (
            _existing_phase_artifact(self.path, current_phase, artifact_rel)
            if current_phase != "-" and artifact_rel is not None
            else None
        )
        structured_status = "running"
        reason = "in_progress"
        missing_markers: list[str] = []
        if required_artifact is not None and not required_artifact.exists():
            structured_status = "awaiting_host"
            reason = "missing_phase_artifact"
        elif required_artifact is not None:
            structured_status = "blocked"
            persisted_reason = meta.get("phase_blocked_reason")
            stale_reason = _stale_artifact_block_reason(self.path, required_artifact)
            stub_reason = _artifact_block_reason(required_artifact)
            if stale_reason:
                reason = stale_reason
            elif stub_reason:
                reason = stub_reason
            elif persisted_reason == "route_blocked":
                reason = persisted_reason
            else:
                missing_markers = _missing_completion_markers(
                    self.path,
                    self.workflow,
                    current_phase,
                    config_root=config_root,
                    project_root=project_root,
                )
                reason = (
                    "missing_completion_markers"
                    if missing_markers
                    else "phase_artifact_written_continue_required"
                )
        payload = {
            "status": structured_status,
            "run": f"{self.workflow}/{self.run_id}",
            "task": self.task,
            "current_phase": current_phase,
            "reason": reason,
            "required_artifact": str(required_artifact) if required_artifact is not None else None,
            "next_command": next_command,
            "missing_completion_markers": missing_markers,
        }
        print(f"Run id     : {self.run_id}")
        print(f"Workflow   : {self.workflow}")
        print(f"Task       : {_status_value(self.task)}")
        print(f"Started at : {self.started_at}")
        print(f"Phase      : {current_phase}")
        print(f"Artifacts  : {len(artifacts)} written")
        for a in artifacts:
            print(f"  - {a}")
        print(f"status: {_status_value(structured_status)}")
        print(f"run: {_status_value(payload['run'])}")
        print(f"task: {_status_value(self.task)}")
        print(f"current_phase: {_status_value(current_phase)}")
        print(f"reason: {_status_value(reason)}")
        if required_artifact is not None:
            print(f"required_artifact: {_status_value(required_artifact)}")
        if missing_markers:
            print(
                "missing_completion_markers: "
                f"{json.dumps(missing_markers, ensure_ascii=False)}"
            )
        print(f"next_command: {_status_value(next_command)}")
        print(f"status_json: {json.dumps(payload, sort_keys=True)}")


def find_active_runs(project_root: Path) -> tuple[ActiveRun, ...]:
    runs_dir = project_root / RUNS_DIRNAME
    if not runs_dir.exists():
        return ()
    paths = sorted(
        (path for path in runs_dir.iterdir() if (path / ACTIVE_MARKER).exists()),
        key=lambda path: path.name,
    )
    return tuple(_active_run(path) for path in paths)


def find_active_run(project_root: Path) -> ActiveRun | None:
    actives = find_active_runs(project_root)
    if not actives:
        return None
    if len(actives) > 1:
        names = ", ".join(active.run_id for active in actives)
        print(
            f"⚠️  multiple active runs detected: {names}. "
            f"Resuming the most recent; abort the others with `agent-flow abort` "
            f"after switching directories or by hand-deleting the marker file.",
            file=sys.stderr,
        )
    return max(actives, key=lambda active: active.run_id)


def _active_run(path: Path) -> ActiveRun:
    meta = read_meta(path)
    return ActiveRun(
        path=path,
        run_id=path.name,
        workflow=meta.get("workflow", "unknown"),
        task=meta.get("task", ""),
        started_at=meta.get("started_at", ""),
    )


def run_concerns_value(concerns: Sequence[str]) -> tuple[str, ...]:
    """concern 목록 정규화. 소문자, 공백 제거, 순서 유지, 중복 제거."""
    seen: list[str] = []
    for item in concerns or ():
        value = str(item).strip().lower()
        if value and value not in seen:
            seen.append(value)
    return tuple(seen)


def run_concerns(meta: Mapping[str, Any]) -> tuple[str, ...]:
    """run meta가 기록한 concern. 이 값이 resolver 입력의 정본이다."""
    raw = meta.get("concerns")
    return run_concerns_value(raw if isinstance(raw, list) else ())


def create_run(
    project_root: Path,
    workflow: str,
    task: str,
    *,
    architecture: str | None = None,
    run_id: str | None = None,
    checkout_identity: str | None = None,
    checkout_registration_identity: str | None = None,
    concerns: Sequence[str] = (),
) -> Path:
    """Create a new run directory. Refuses if an active run exists."""
    if run_id is not None:
        validate_safe_name(run_id, "run id")
    if checkout_identity is not None:
        _validate_checkout_identity(checkout_identity)
        if (
            checkout_identity.startswith("worktree:")
            and checkout_registration_identity is None
        ):
            raise ValueError(
                "worktree checkout registration identity is required"
            )
    if checkout_registration_identity is not None:
        if (
            checkout_identity is None
            or not checkout_identity.startswith("worktree:")
            or len(checkout_registration_identity) != 64
        ):
            raise ValueError("invalid checkout registration identity")
        try:
            int(checkout_registration_identity, 16)
        except ValueError as exc:
            raise ValueError("invalid checkout registration identity") from exc
    runs_dir = project_root / RUNS_DIRNAME
    runs_dir.mkdir(parents=True, exist_ok=True)
    try:
        with exclusive_file_lease(runs_dir / ACTIVE_LOCK):
            existing = find_active_run(project_root)
            if existing is not None:
                raise ActiveRunExists(
                    f"active run already exists: {existing.run_id} "
                    f"(task: {existing.task!r}). Use `agent-flow continue` to resume "
                    f"or `agent-flow abort` to clear."
                )

            now = datetime.now(timezone.utc)
            if run_id is None:
                base_id = now.strftime("%Y%m%d-%H%M%S")
                run_id = base_id
                suffix = 1
                while (runs_dir / run_id).exists():
                    run_id = f"{base_id}-{suffix}"
                    suffix += 1
            elif (runs_dir / run_id).exists():
                raise FileExistsError(f"run already exists: {run_id}")

            run_path = runs_dir / run_id
            run_path.mkdir()
            try:
                meta = {
                    "run_id": run_id,
                    "workflow": workflow,
                    "task": task,
                    # 명시된 concern. free-form task 문구와 달리 열거형이라
                    # 같은 입력이 항상 같은 required 집합을 만든다.
                    "concerns": list(run_concerns_value(concerns)),
                    # design-spec.md의 task digest와 대조되는 값. 런 도중 task를
                    # 바꿔치기하면 원장이 가리키는 사용자 지시와 어긋나므로 gate가 막는다.
                    "task_digest": hashlib.sha256(
                        task.strip().encode("utf-8")
                    ).hexdigest(),
                    "started_at": now.isoformat(),
                    "current_phase": None,
                    # gate-results.json의 출처 표식. `agent-flow gates`만 이 값을 찍는다.
                    # 손으로 쓴 JSON은 값을 모르므로 green으로 라우팅되지 않는다.
                    # 파일에 있는 값이라 복사는 가능하다 — 적대적 위조가 아니라
                    # "실수로 손으로 쓰는 것"을 막는 층이다.
                    "gate_nonce": secrets.token_hex(16),
                }
                digest = _workflow_digest(workflow)
                if digest is not None:
                    meta["workflow_digest"] = digest
                if architecture:
                    meta["architecture"] = architecture
                if checkout_identity is not None:
                    meta["checkout_identity"] = checkout_identity
                if checkout_registration_identity is not None:
                    meta["checkout_registration_identity"] = (
                        checkout_registration_identity
                    )
                write_meta(run_path, meta)
                atomic_write_text(run_path / ACTIVE_MARKER, "")
            except Exception:
                # meta도 active 표식도 없는 디렉터리는 run이 아니다. 남겨 두면
                # 다음 시도가 `run already exists`로 막히고, 그 자리를 치울 명령이
                # 없다.
                shutil.rmtree(run_path, ignore_errors=True)
                raise
            return run_path
    except FileLeaseUnavailable as exc:
        raise ActiveRunExists(
            "another agent-flow run is starting or its lifecycle lock is unsafe; "
            "retry after the current process exits"
        ) from exc


def _workflow_digest(workflow: str) -> str | None:
    """이 run이 실행하기로 한 workflow 원문의 sha256. 읽을 수 없으면 ``None``.

    읽지 못한 경우를 실패로 올리지 않는다 — run 생성은 workflow 해석보다 앞선
    단계이고, 여기서 막으면 정의를 고치려는 사용자가 run조차 열 수 없다. 값이
    비면 cursor 검증이 drift 대신 "기록 없음"으로 취급하고 그때 채워 넣는다.
    """
    try:
        return load_phase_workflow_definition(find_kit_root(), workflow).digest
    except (OSError, RuntimeError, ValueError, yaml.YAMLError):
        return None


def _validate_checkout_identity(value: str) -> None:
    if value == "leader":
        return
    prefix = "worktree:"
    if not value.startswith(prefix):
        raise ValueError(f"invalid checkout identity: {value!r}")
    name = value[len(prefix):]
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or not name.isprintable()
        or Path(name).name != name
    ):
        raise ValueError(f"invalid checkout identity name: {name!r}")


def read_meta(run_path: Path) -> dict[str, Any]:
    """Read meta.json tolerantly. Returns {} on missing/malformed.

    The runner depends on this for status / resume; a parse error must not
    block recovery. Surface the corruption to stderr so the user sees it.
    """
    meta_path = run_path / META_FILE
    if not meta_path.exists():
        return {}
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("top-level meta.json value is not an object")
        return payload
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, TypeError) as e:
        print(
            f"⚠️  meta.json at {meta_path} is unreadable ({e}); "
            f"treating as empty. Use `agent-flow abort` to clear if needed.",
            file=sys.stderr,
        )
        return {}




def write_meta(run_path: Path, meta: Mapping[str, Any]) -> None:
    """Atomically replace ``meta.json`` without exposing partial state."""
    write_run_artifact_text(
        run_path,
        run_path.resolve() / META_FILE,
        json.dumps(meta, indent=2),
    )


def mark_inactive(run_path: Path) -> None:
    marker = run_path / ACTIVE_MARKER
    if marker.exists():
        marker.unlink()


def has_artifact(run_path: Path, phase_id: str) -> bool:
    return (run_path / f"{phase_id}.md").exists()


def _missing_completion_markers(
    run_path: Path,
    workflow: str,
    phase_id: str,
    *,
    config_root: Path | None = None,
    project_root: Path | None = None,
) -> list[str]:
    artifact_rel, markers, skills = _phase_contract(run_path, workflow, phase_id)
    artifact = _existing_phase_artifact(run_path, phase_id, artifact_rel)
    if not artifact.exists():
        return []
    text = artifact.read_text(encoding="utf-8")
    missing = _missing_markers(text, markers) if markers else []
    config = config_root or run_path
    project = project_root or config
    meta = read_meta(run_path)
    phase_since = _phase_entered_at(run_path)
    profile = resolved_profile(config)
    # 컨텍스트를 안 넘기면 `status`와 runner가 서로 다른 required 집합을 본다.
    # 도출은 local_skills가 단일 소스다.
    missing.extend(
        missing_local_skill_markers(
            text,
            config,
            phase_id,
            phase_skills=skills,
            profile=profile,
            changed_files=changed_files(project),
            task_text=str(meta.get("task", "")),
            concerns=run_concerns(meta),
            since=phase_since,
        )
    )
    missing.extend(
        missing_spec_item_evidence(
            project,
            run_path,
            phase_id,
            text,
            task_text=str(meta.get("task", "")),
            profile=profile,
            since=_parse_timestamp(meta.get("started_at")),
            evidence_root=config,
        )
    )
    return missing


def _phase_entered_at(run_path: Path) -> float | None:
    """읽음 증거는 현재 phase 진입 이후 것만 인정한다. 없으면 None이라 과거 기록까지 통과한다."""
    meta = read_meta(run_path)
    for key in ("phase_entered_at", "updated_at", "started_at"):
        parsed = _parse_timestamp(meta.get(key))
        if parsed is not None:
            return parsed
    return None


def _parse_timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _required_markers(run_path: Path, workflow: str, phase_id: str) -> tuple[str, ...]:
    _artifact_rel, markers, _skills = _phase_contract(run_path, workflow, phase_id)
    return markers


def _phase_contract(
    run_path: Path, workflow: str, phase_id: str
) -> tuple[Path | None, tuple[str, ...], PhaseSkills | None]:
    project_root = run_path.parents[2] if len(run_path.parents) >= 3 else None
    candidates: list[Path] = []
    if project_root is not None:
        candidates.append(project_root / ".agent-flow" / "workflows" / f"{workflow}.yaml")
    # runner와 같은 kit root를 우선해 phase routing과 marker 검증이 같은 YAML을 본다.
    try:
        candidates.append(find_kit_root() / "workflows" / f"{workflow}.yaml")
    except RuntimeError:
        pass
    candidates.append(Path(__file__).resolve().parent / "workflows" / f"{workflow}.yaml")
    candidates.append(Path(__file__).resolve().parents[2] / "workflows" / f"{workflow}.yaml")
    for path in candidates:
        if not path.exists():
            continue
        try:
            definition = load_phase_workflow_definition(path.parent.parent, workflow)
        except (OSError, ValueError):
            definition = None
        if definition is not None:
            for phase in definition.phases:
                if phase.id == phase_id:
                    return Path(phase.artifact), phase.required_markers, phase.skills
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        phases = raw.get("phases") if isinstance(raw, dict) else None
        if not isinstance(phases, list):
            continue
        for phase in phases:
            if not isinstance(phase, dict) or str(phase.get("id")) != phase_id:
                continue
            return (
                Path(f"{phase_id}.md"),
                normalize_required_markers(phase.get("required_markers")),
                None,
            )
    return (Path(f"{phase_id}.md") if phase_id != "-" else None, (), None)


def _existing_phase_artifact(run_path: Path, phase_id: str, artifact_rel: Path | None) -> Path:
    artifact = run_path / artifact_rel if artifact_rel is not None else run_path / f"{phase_id}.md"
    legacy_artifact = run_path / f"{phase_id}.md"
    if not artifact.exists() and legacy_artifact.exists():
        return legacy_artifact
    return artifact


def _missing_markers(text: str, markers: tuple[str, ...]) -> list[str]:
    return missing_markers(text, markers)


def _status_value(value: object) -> str:
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def _stale_artifact_block_reason(run_path: Path, artifact: Path) -> str | None:
    entered_at = _phase_entered_at(run_path)
    if entered_at is None:
        return None
    try:
        artifact_mtime = artifact.stat().st_mtime
    except FileNotFoundError:
        return None
    return "stale_artifact" if artifact_mtime < entered_at else None


def _artifact_block_reason(artifact: Path) -> str | None:
    if not artifact.exists():
        return None
    text = artifact.read_text(encoding="utf-8")
    if "_stub artifact written by GenericAdapter (stub mode)._" in text:
        return "generic_stub_artifact"
    return None
