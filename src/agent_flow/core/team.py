from __future__ import annotations

import json
import re
import shutil
import time
from contextlib import contextmanager
from uuid import uuid4
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

TaskStatus = Literal["pending", "blocked", "in_progress", "completed", "failed"]


@dataclass(frozen=True)
class TeamConfig:
    name: str
    description: str
    created_at: str


@dataclass(frozen=True)
class TeamTask:
    task_id: str
    subject: str
    description: str
    status: TaskStatus = "pending"
    owner: str | None = None
    claim_token: str | None = None
    result: str | None = None


@dataclass(frozen=True)
class TeamWorker:
    name: str
    role: str
    status: str = "idle"


@dataclass(frozen=True)
class WorkerHeartbeat:
    worker: str
    status: str
    alive: bool
    updated_at: str


@dataclass(frozen=True)
class MailboxMessage:
    message_id: str
    from_actor: str
    to_worker: str
    body: str
    created_at: str
    read: bool = False


@dataclass(frozen=True)
class ShutdownSignal:
    signal_id: str
    worker: str
    reason: str
    requested_at: str
    acknowledged: bool = False
    acknowledged_at: str | None = None


@dataclass(frozen=True)
class TeamSummary:
    name: str
    description: str
    task_count: int
    worker_count: int
    path: str


@dataclass(frozen=True)
class TeamArchive:
    name: str
    source_path: str
    archive_path: str
    archived_at: str
    reason: str


def init_team(*, root: Path, name: str, description: str) -> TeamConfig:
    team_name = safe_team_name(name)
    config = TeamConfig(name=team_name, description=description, created_at=_now())
    _write_json(_team_root(root, team_name) / "config.json", asdict(config))
    (_team_root(root, team_name) / "tasks").mkdir(parents=True, exist_ok=True)
    (_team_root(root, team_name) / "workers").mkdir(parents=True, exist_ok=True)
    (_team_root(root, team_name) / "mailbox").mkdir(parents=True, exist_ok=True)
    (_team_root(root, team_name) / "shutdown").mkdir(parents=True, exist_ok=True)
    return config


def list_teams(*, root: Path) -> list[TeamSummary]:
    team_root = root / ".agent-flow" / "state" / "team"
    if not team_root.exists():
        return []
    summaries = []
    for config_path in sorted(team_root.glob("*/config.json")):
        team_dir = config_path.parent
        config = TeamConfig(**json.loads(config_path.read_text(encoding="utf-8")))
        task_count = len(list((team_dir / "tasks").glob("*.json"))) if (team_dir / "tasks").exists() else 0
        worker_count = (
            len(list((team_dir / "workers").glob("*/identity.json"))) if (team_dir / "workers").exists() else 0
        )
        summaries.append(
            TeamSummary(
                name=config.name,
                description=config.description,
                task_count=task_count,
                worker_count=worker_count,
                path=str(team_dir),
            )
        )
    return summaries


def archive_team(*, root: Path, team_name: str, reason: str) -> TeamArchive:
    safe_team = safe_team_name(team_name)
    source_dir = _team_root(root, safe_team)
    _require_team(root=root, team_name=safe_team)
    archived_at = _now()
    archive_name = f"{safe_team}-{_archive_timestamp(archived_at)}"
    archive_dir = root / ".agent-flow" / "archive" / "team" / archive_name
    archive_dir.parent.mkdir(parents=True, exist_ok=True)
    if archive_dir.exists():
        raise FileExistsError(f"team archive already exists: {archive_name}")
    archive = TeamArchive(
        name=safe_team,
        source_path=str(source_dir),
        archive_path=str(archive_dir),
        archived_at=archived_at,
        reason=reason,
    )
    _write_json(source_dir / "archive.json", asdict(archive))
    source_dir.rename(archive_dir)
    return archive


def add_task(
    *,
    root: Path,
    team_name: str,
    task_id: str,
    subject: str,
    description: str,
) -> TeamTask:
    safe_team = safe_team_name(team_name)
    _require_team(root=root, team_name=safe_team)
    safe_id = safe_task_id(task_id)
    task_path = _team_root(root, safe_team) / "tasks" / f"{safe_id}.json"
    task = TeamTask(task_id=safe_id, subject=subject, description=description)
    _write_json_create(task_path, asdict(task), exists_message=f"task already exists: {safe_id}")
    return task


def claim_task(*, root: Path, team_name: str, task_id: str, worker_name: str) -> TeamTask:
    safe_team = safe_team_name(team_name)
    _require_team(root=root, team_name=safe_team)
    safe_worker = safe_worker_name(worker_name)
    _require_worker(root=root, team_name=safe_team, worker_name=safe_worker)
    with _task_lock(root=root, team_name=safe_team, task_id=task_id):
        task = _read_task(root=root, team_name=safe_team, task_id=task_id)
        if task.status not in {"pending", "blocked"}:
            raise RuntimeError(f"task is not claimable: {task.task_id}")
        updated = TeamTask(
            task_id=task.task_id,
            subject=task.subject,
            description=task.description,
            status="in_progress",
            owner=safe_worker,
            claim_token=uuid4().hex,
            result=task.result,
        )
        _write_task(root=root, team_name=safe_team, task=updated)
        return updated


def complete_task(
    *,
    root: Path,
    team_name: str,
    task_id: str,
    claim_token: str,
    result: str,
) -> TeamTask:
    return _finish_task(
        root=root,
        team_name=team_name,
        task_id=task_id,
        claim_token=claim_token,
        result=result,
        status="completed",
    )


def fail_task(
    *,
    root: Path,
    team_name: str,
    task_id: str,
    claim_token: str,
    result: str,
) -> TeamTask:
    return _finish_task(
        root=root,
        team_name=team_name,
        task_id=task_id,
        claim_token=claim_token,
        result=result,
        status="failed",
    )


def add_worker(*, root: Path, team_name: str, worker_name: str, role: str) -> TeamWorker:
    safe_team = safe_team_name(team_name)
    _require_team(root=root, team_name=safe_team)
    safe_worker = safe_worker_name(worker_name)
    worker = TeamWorker(name=safe_worker, role=role)
    worker_dir = _team_root(root, safe_team) / "workers" / safe_worker
    with _mailbox_lock(root=root, team_name=safe_team, worker_name=safe_worker):
        mailbox_path = _mailbox_path(root=root, team_name=safe_team, worker_name=safe_worker)
        if not mailbox_path.exists():
            _write_json(mailbox_path, [])
    _write_json(worker_dir / "identity.json", asdict(worker))
    _write_json(
        worker_dir / "heartbeat.json",
        asdict(WorkerHeartbeat(worker=safe_worker, status=worker.status, alive=True, updated_at=_now())),
    )
    (worker_dir / "inbox.md").write_text("", encoding="utf-8")
    return worker


def update_worker_heartbeat(
    *,
    root: Path,
    team_name: str,
    worker_name: str,
    status: str,
    alive: bool,
) -> WorkerHeartbeat:
    safe_team = safe_team_name(team_name)
    _require_team(root=root, team_name=safe_team)
    safe_worker = safe_worker_name(worker_name)
    _require_worker(root=root, team_name=safe_team, worker_name=safe_worker)
    heartbeat = WorkerHeartbeat(worker=safe_worker, status=status, alive=alive, updated_at=_now())
    _write_json(_heartbeat_path(root=root, team_name=safe_team, worker_name=safe_worker), asdict(heartbeat))
    return heartbeat


def send_message(
    *,
    root: Path,
    team_name: str,
    from_actor: str,
    to_worker: str,
    body: str,
) -> MailboxMessage:
    safe_team = safe_team_name(team_name)
    _require_team(root=root, team_name=safe_team)
    safe_to_worker = safe_worker_name(to_worker)
    _require_worker(root=root, team_name=safe_team, worker_name=safe_to_worker)
    safe_from = _safe_actor(from_actor)
    if not body.strip():
        raise ValueError("message body must not be empty")
    message = MailboxMessage(
        message_id=uuid4().hex,
        from_actor=safe_from,
        to_worker=safe_to_worker,
        body=body,
        created_at=_now(),
    )
    with _mailbox_lock(root=root, team_name=safe_team, worker_name=safe_to_worker):
        messages = _read_mailbox(root=root, team_name=safe_team, worker_name=safe_to_worker)
        messages.append(message)
        _write_mailbox(root=root, team_name=safe_team, worker_name=safe_to_worker, messages=messages)
    return message


def list_messages(*, root: Path, team_name: str, worker_name: str, unread_only: bool = False) -> list[MailboxMessage]:
    safe_team = safe_team_name(team_name)
    _require_team(root=root, team_name=safe_team)
    safe_worker = safe_worker_name(worker_name)
    _require_worker(root=root, team_name=safe_team, worker_name=safe_worker)
    with _mailbox_lock(root=root, team_name=safe_team, worker_name=safe_worker):
        messages = _read_mailbox(root=root, team_name=safe_team, worker_name=safe_worker)
    if unread_only:
        return [message for message in messages if not message.read]
    return messages


def mark_message_read(*, root: Path, team_name: str, worker_name: str, message_id: str) -> MailboxMessage:
    safe_team = safe_team_name(team_name)
    _require_team(root=root, team_name=safe_team)
    safe_worker = safe_worker_name(worker_name)
    _require_worker(root=root, team_name=safe_team, worker_name=safe_worker)
    with _mailbox_lock(root=root, team_name=safe_team, worker_name=safe_worker):
        messages = _read_mailbox(root=root, team_name=safe_team, worker_name=safe_worker)
        updated: list[MailboxMessage] = []
        selected: MailboxMessage | None = None
        for message in messages:
            if message.message_id == message_id:
                selected = MailboxMessage(
                    message_id=message.message_id,
                    from_actor=message.from_actor,
                    to_worker=message.to_worker,
                    body=message.body,
                    created_at=message.created_at,
                    read=True,
                )
                updated.append(selected)
            else:
                updated.append(message)
        if selected is None:
            raise FileNotFoundError(f"message does not exist: {message_id}")
        _write_mailbox(root=root, team_name=safe_team, worker_name=safe_worker, messages=updated)
        return selected


def request_shutdown(*, root: Path, team_name: str, worker_name: str, reason: str) -> ShutdownSignal:
    safe_team = safe_team_name(team_name)
    _require_team(root=root, team_name=safe_team)
    safe_worker = safe_worker_name(worker_name)
    _require_worker(root=root, team_name=safe_team, worker_name=safe_worker)
    if not reason.strip():
        raise ValueError("shutdown reason must not be empty")
    signal = ShutdownSignal(
        signal_id=uuid4().hex,
        worker=safe_worker,
        reason=reason,
        requested_at=_now(),
    )
    _write_shutdown(root=root, team_name=safe_team, signal=signal)
    return signal


def acknowledge_shutdown(*, root: Path, team_name: str, worker_name: str, signal_id: str) -> ShutdownSignal:
    safe_team = safe_team_name(team_name)
    _require_team(root=root, team_name=safe_team)
    safe_worker = safe_worker_name(worker_name)
    _require_worker(root=root, team_name=safe_team, worker_name=safe_worker)
    signal = _read_shutdown(root=root, team_name=safe_team, worker_name=safe_worker, signal_id=signal_id)
    if signal.worker != safe_worker:
        raise ValueError(f"shutdown signal worker mismatch: {signal.signal_id}")
    acknowledged = ShutdownSignal(
        signal_id=signal.signal_id,
        worker=safe_worker,
        reason=signal.reason,
        requested_at=signal.requested_at,
        acknowledged=True,
        acknowledged_at=_now(),
    )
    _write_shutdown(root=root, team_name=safe_team, signal=acknowledged)
    return acknowledged


def team_status(*, root: Path, team_name: str, detail: bool = False) -> dict[str, object]:
    safe_team = safe_team_name(team_name)
    root_dir = _team_root(root, safe_team)
    task_paths = sorted((root_dir / "tasks").glob("*.json")) if (root_dir / "tasks").exists() else []
    worker_paths = sorted((root_dir / "workers").glob("*/identity.json")) if (root_dir / "workers").exists() else []
    tasks = []
    workers = []
    heartbeats = []
    for worker_path in worker_paths:
        worker_name = worker_path.parent.name
        heartbeat_path = worker_path.parent / "heartbeat.json"
        if heartbeat_path.is_file():
            heartbeats.append(WorkerHeartbeat(**json.loads(heartbeat_path.read_text(encoding="utf-8"))))
    unread_counts: dict[str, int] = {}
    shutdowns = []
    if detail:
        tasks = [TeamTask(**json.loads(path.read_text(encoding="utf-8"))) for path in task_paths]
        workers = [TeamWorker(**json.loads(path.read_text(encoding="utf-8"))) for path in worker_paths]
        for worker_path in worker_paths:
            worker_name = worker_path.parent.name
            with _mailbox_lock(root=root, team_name=safe_team, worker_name=worker_name):
                unread_counts[worker_name] = len(
                    [
                        message
                        for message in _read_mailbox(root=root, team_name=safe_team, worker_name=worker_name)
                        if not message.read
                    ]
                )
        shutdowns = _read_shutdowns(root=root, team_name=safe_team)
    return {
        "team": safe_team,
        "exists": root_dir.exists(),
        "task_count": len(task_paths),
        "worker_count": len(worker_paths),
        "tasks": tasks,
        "workers": workers,
        "heartbeats": heartbeats,
        "unread_counts": unread_counts,
        "shutdowns": shutdowns,
        "path": str(root_dir),
    }


def export_team_state(*, root: Path, team_name: str) -> dict[str, object]:
    safe_team = safe_team_name(team_name)
    _require_team(root=root, team_name=safe_team)
    root_dir = _team_root(root, safe_team)
    task_paths = sorted((root_dir / "tasks").glob("*.json")) if (root_dir / "tasks").exists() else []
    worker_paths = sorted((root_dir / "workers").glob("*/identity.json")) if (root_dir / "workers").exists() else []
    heartbeats = []
    mailboxes: dict[str, list[dict[str, object]]] = {}
    workers = []
    for worker_path in worker_paths:
        worker = _read_json(worker_path)
        workers.append(worker)
        worker_name = safe_worker_name(str(worker["name"]))
        heartbeat_path = _heartbeat_path(root=root, team_name=safe_team, worker_name=worker_name)
        if heartbeat_path.is_file():
            heartbeats.append(_read_json(heartbeat_path))
        mailbox_path = _mailbox_path(root=root, team_name=safe_team, worker_name=worker_name)
        mailboxes[worker_name] = _read_json(mailbox_path) if mailbox_path.is_file() else []
    return {
        "team": _read_json(root_dir / "config.json"),
        "tasks": [_read_json(path) for path in task_paths],
        "workers": workers,
        "heartbeats": heartbeats,
        "mailboxes": mailboxes,
        "shutdowns": _read_shutdown_payloads(root=root, team_name=safe_team),
    }


def validate_team_state_import(payload: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload must be a JSON object"]
    team = payload.get("team")
    if not isinstance(team, dict):
        errors.append("team must be an object")
    else:
        _validate_safe_field(errors, team, "name", "team.name", safe_team_name)
        _validate_string_field(errors, team, "description", "team.description")
        _validate_string_field(errors, team, "created_at", "team.created_at")
    for key in ("tasks", "workers", "heartbeats", "mailboxes", "shutdowns"):
        if key not in payload:
            errors.append(f"{key} is required")
    tasks = _list_field(errors, payload, "tasks")
    workers = _list_field(errors, payload, "workers")
    heartbeats = _list_field(errors, payload, "heartbeats")
    shutdowns = _list_field(errors, payload, "shutdowns")
    mailboxes = payload.get("mailboxes", {})
    if not isinstance(mailboxes, dict):
        errors.append("mailboxes must be an object")
        mailboxes = {}

    task_ids: set[str] = set()
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append(f"tasks[{index}] must be an object")
            continue
        task_id = _validate_safe_field(errors, task, "task_id", f"tasks[{index}].task_id", safe_task_id)
        if task_id is not None:
            if task_id in task_ids:
                errors.append(f"duplicate task id: {task_id}")
            task_ids.add(task_id)
        _validate_string_field(errors, task, "subject", f"tasks[{index}].subject")
        _validate_string_field(errors, task, "description", f"tasks[{index}].description")
        status = task.get("status", "pending")
        if not isinstance(status, str):
            errors.append(f"tasks[{index}].status must be a string")
        elif status not in {"pending", "blocked", "in_progress", "completed", "failed"}:
            errors.append(f"invalid task status: {status}")

    worker_names: set[str] = set()
    for index, worker in enumerate(workers):
        if not isinstance(worker, dict):
            errors.append(f"workers[{index}] must be an object")
            continue
        worker_name = _validate_safe_field(errors, worker, "name", f"workers[{index}].name", safe_worker_name)
        if worker_name is not None:
            if worker_name in worker_names:
                errors.append(f"duplicate worker name: {worker_name}")
            worker_names.add(worker_name)
        _validate_string_field(errors, worker, "role", f"workers[{index}].role")
        _validate_string_field(errors, worker, "status", f"workers[{index}].status")

    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        owner = task.get("owner")
        if owner is None:
            continue
        if not isinstance(owner, str):
            errors.append(f"tasks[{index}].owner must be a string")
            continue
        try:
            safe_owner = safe_worker_name(owner)
        except ValueError:
            errors.append(f"tasks[{index}].owner is unsafe: {owner}")
            continue
        if safe_owner not in worker_names:
            errors.append(f"task owner references unknown worker: {safe_owner}")

    heartbeat_workers: set[str] = set()
    for index, heartbeat in enumerate(heartbeats):
        if not isinstance(heartbeat, dict):
            errors.append(f"heartbeats[{index}] must be an object")
            continue
        worker_name = _validate_safe_field(errors, heartbeat, "worker", f"heartbeats[{index}].worker", safe_worker_name)
        if worker_name is not None:
            if worker_name in heartbeat_workers:
                errors.append(f"duplicate heartbeat worker: {worker_name}")
            heartbeat_workers.add(worker_name)
            if worker_name not in worker_names:
                errors.append(f"heartbeat references unknown worker: {worker_name}")
        _validate_string_field(errors, heartbeat, "status", f"heartbeats[{index}].status")
        _validate_bool_field(errors, heartbeat, "alive", f"heartbeats[{index}].alive")
        _validate_string_field(errors, heartbeat, "updated_at", f"heartbeats[{index}].updated_at")

    mailbox_workers: set[str] = set()
    for worker_name, messages in mailboxes.items():
        try:
            safe_worker = safe_worker_name(str(worker_name))
        except ValueError:
            errors.append(f"unsafe mailbox worker: {worker_name}")
            continue
        if safe_worker in mailbox_workers:
            errors.append(f"duplicate mailbox worker: {safe_worker}")
        mailbox_workers.add(safe_worker)
        if safe_worker not in worker_names:
            errors.append(f"mailbox references unknown worker: {safe_worker}")
        if not isinstance(messages, list):
            errors.append(f"mailboxes.{safe_worker} must be a list")
            continue
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                errors.append(f"mailboxes.{safe_worker}[{index}] must be an object")
                continue
            _validate_string_field(errors, message, "message_id", f"mailboxes.{safe_worker}[{index}].message_id")
            _validate_string_field(errors, message, "from_actor", f"mailboxes.{safe_worker}[{index}].from_actor")
            to_worker = _validate_safe_field(
                errors,
                message,
                "to_worker",
                f"mailboxes.{safe_worker}[{index}].to_worker",
                safe_worker_name,
            )
            if to_worker is not None and to_worker != safe_worker:
                errors.append(f"mailbox message worker mismatch: {safe_worker} != {to_worker}")
            _validate_string_field(errors, message, "body", f"mailboxes.{safe_worker}[{index}].body")
            _validate_string_field(errors, message, "created_at", f"mailboxes.{safe_worker}[{index}].created_at")
            _validate_bool_field(errors, message, "read", f"mailboxes.{safe_worker}[{index}].read")

    shutdown_ids: set[tuple[str, str]] = set()
    for index, signal in enumerate(shutdowns):
        if not isinstance(signal, dict):
            errors.append(f"shutdowns[{index}] must be an object")
            continue
        signal_id = _validate_safe_field(errors, signal, "signal_id", f"shutdowns[{index}].signal_id", safe_signal_id)
        worker_name = _validate_safe_field(errors, signal, "worker", f"shutdowns[{index}].worker", safe_worker_name)
        if signal_id is not None and worker_name is not None:
            shutdown_key = (worker_name, signal_id)
            if shutdown_key in shutdown_ids:
                errors.append(f"duplicate shutdown signal: {worker_name}/{signal_id}")
            shutdown_ids.add(shutdown_key)
            if worker_name not in worker_names:
                errors.append(f"shutdown references unknown worker: {worker_name}")
        _validate_string_field(errors, signal, "reason", f"shutdowns[{index}].reason")
        _validate_string_field(errors, signal, "requested_at", f"shutdowns[{index}].requested_at")
        _validate_bool_field(errors, signal, "acknowledged", f"shutdowns[{index}].acknowledged")
        acknowledged_at = signal.get("acknowledged_at")
        if acknowledged_at is not None and not isinstance(acknowledged_at, str):
            errors.append(f"shutdowns[{index}].acknowledged_at must be a string or null")
    return errors


def summarize_team_state_import(payload: object) -> dict[str, object]:
    errors = validate_team_state_import(payload)
    if errors:
        return {"valid": False, "errors": errors}
    if not isinstance(payload, dict):
        return {"valid": False, "errors": ["payload must be a JSON object"]}
    team = payload["team"]
    tasks = payload["tasks"]
    workers = payload["workers"]
    heartbeats = payload["heartbeats"]
    mailboxes = payload["mailboxes"]
    shutdowns = payload["shutdowns"]
    message_count = sum(len(messages) for messages in mailboxes.values())
    return {
        "valid": True,
        "team": team["name"],
        "task_count": len(tasks),
        "worker_count": len(workers),
        "heartbeat_count": len(heartbeats),
        "mailbox_count": len(mailboxes),
        "message_count": message_count,
        "shutdown_count": len(shutdowns),
    }


def apply_team_state_import(*, root: Path, payload: object) -> dict[str, object]:
    summary = summarize_team_state_import(payload)
    if not summary["valid"]:
        return summary
    if not isinstance(payload, dict):
        return {"valid": False, "errors": ["payload must be a JSON object"]}
    team = payload["team"]
    team_name = safe_team_name(str(team["name"]))
    root_dir = _team_root(root, team_name)
    root_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        root_dir.mkdir()
    except FileExistsError:
        return {"valid": False, "errors": [f"team already exists: {team_name}"]}
    try:
        _write_json(root_dir / "config.json", team)
        (root_dir / "tasks").mkdir(parents=True, exist_ok=True)
        for task in payload["tasks"]:
            task_id = safe_task_id(str(task["task_id"]))
            _write_json(root_dir / "tasks" / f"{task_id}.json", task)
        (root_dir / "workers").mkdir(parents=True, exist_ok=True)
        for worker in payload["workers"]:
            worker_name = safe_worker_name(str(worker["name"]))
            worker_dir = root_dir / "workers" / worker_name
            _write_json(worker_dir / "identity.json", worker)
            (worker_dir / "inbox.md").write_text("", encoding="utf-8")
        for heartbeat in payload["heartbeats"]:
            worker_name = safe_worker_name(str(heartbeat["worker"]))
            _write_json(root_dir / "workers" / worker_name / "heartbeat.json", heartbeat)
        (root_dir / "mailbox").mkdir(parents=True, exist_ok=True)
        for worker_name, messages in payload["mailboxes"].items():
            safe_worker = safe_worker_name(str(worker_name))
            _write_json(root_dir / "mailbox" / f"{safe_worker}.json", messages)
        (root_dir / "shutdown").mkdir(parents=True, exist_ok=True)
        for signal in payload["shutdowns"]:
            worker_name = safe_worker_name(str(signal["worker"]))
            signal_id = safe_signal_id(str(signal["signal_id"]))
            _write_json(root_dir / "shutdown" / worker_name / f"{signal_id}.json", signal)
    except Exception as exc:
        errors = [f"cannot apply team import: {exc}"]
        if root_dir.exists():
            try:
                shutil.rmtree(root_dir)
            except Exception as cleanup_exc:
                errors.append(f"cannot clean failed team import: {cleanup_exc}")
        return {"valid": False, "team": team_name, "errors": errors}
    return summary


def _finish_task(
    *,
    root: Path,
    team_name: str,
    task_id: str,
    claim_token: str,
    result: str,
    status: TaskStatus,
) -> TeamTask:
    safe_team = safe_team_name(team_name)
    _require_team(root=root, team_name=safe_team)
    with _task_lock(root=root, team_name=safe_team, task_id=task_id):
        task = _read_task(root=root, team_name=safe_team, task_id=task_id)
        if task.status != "in_progress":
            raise RuntimeError(f"task is not in progress: {task.task_id}")
        if task.claim_token != claim_token:
            raise PermissionError(f"claim token mismatch for task: {task.task_id}")
        updated = TeamTask(
            task_id=task.task_id,
            subject=task.subject,
            description=task.description,
            status=status,
            owner=task.owner,
            claim_token=None,
            result=result,
        )
        _write_task(root=root, team_name=safe_team, task=updated)
        return updated


def safe_team_name(value: str) -> str:
    return _safe_name(value, max_length=30, label="team")


def safe_worker_name(value: str) -> str:
    return _safe_name(value, max_length=63, label="worker")


def safe_task_id(value: str) -> str:
    stripped = value.strip()
    if not re.fullmatch(r"[a-zA-Z0-9._-]{1,64}", stripped):
        raise ValueError(f"unsafe task id: {value}")
    return stripped


def safe_signal_id(value: str) -> str:
    stripped = value.strip()
    if not re.fullmatch(r"[a-f0-9]{32}", stripped):
        raise ValueError(f"unsafe signal id: {value}")
    return stripped


def _team_root(root: Path, team_name: str) -> Path:
    return root / ".agent-flow" / "state" / "team" / team_name


def _mailbox_path(*, root: Path, team_name: str, worker_name: str) -> Path:
    return _team_root(root, team_name) / "mailbox" / f"{worker_name}.json"


def _heartbeat_path(*, root: Path, team_name: str, worker_name: str) -> Path:
    return _team_root(root, team_name) / "workers" / worker_name / "heartbeat.json"


def _shutdown_path(*, root: Path, team_name: str, worker_name: str, signal_id: str) -> Path:
    safe_signal = safe_signal_id(signal_id)
    return _team_root(root, team_name) / "shutdown" / worker_name / f"{safe_signal}.json"


def _require_team(*, root: Path, team_name: str) -> None:
    if not (_team_root(root, team_name) / "config.json").is_file():
        raise FileNotFoundError(f"team is not initialized: {team_name}")


def _require_worker(*, root: Path, team_name: str, worker_name: str) -> None:
    if not (_team_root(root, team_name) / "workers" / worker_name / "identity.json").is_file():
        raise FileNotFoundError(f"worker is not registered: {worker_name}")


def _read_task(*, root: Path, team_name: str, task_id: str) -> TeamTask:
    safe_id = safe_task_id(task_id)
    path = _team_root(root, team_name) / "tasks" / f"{safe_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"task does not exist: {safe_id}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return TeamTask(**payload)


def _write_task(*, root: Path, team_name: str, task: TeamTask) -> None:
    _write_json(_team_root(root, team_name) / "tasks" / f"{task.task_id}.json", asdict(task))


def _read_mailbox(*, root: Path, team_name: str, worker_name: str) -> list[MailboxMessage]:
    path = _mailbox_path(root=root, team_name=team_name, worker_name=worker_name)
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [MailboxMessage(**item) for item in payload]


def _write_mailbox(
    *,
    root: Path,
    team_name: str,
    worker_name: str,
    messages: list[MailboxMessage],
) -> None:
    _write_json(
        _mailbox_path(root=root, team_name=team_name, worker_name=worker_name),
        [asdict(message) for message in messages],
    )


def _read_shutdown(*, root: Path, team_name: str, worker_name: str, signal_id: str) -> ShutdownSignal:
    path = _shutdown_path(root=root, team_name=team_name, worker_name=worker_name, signal_id=signal_id)
    if not path.is_file():
        raise FileNotFoundError(f"shutdown signal does not exist: {signal_id}")
    return ShutdownSignal(**json.loads(path.read_text(encoding="utf-8")))


def _read_shutdowns(*, root: Path, team_name: str) -> list[ShutdownSignal]:
    shutdown_dir = _team_root(root, team_name) / "shutdown"
    if not shutdown_dir.exists():
        return []
    signals = []
    for path in sorted(shutdown_dir.glob("*/*.json")):
        signals.append(ShutdownSignal(**json.loads(path.read_text(encoding="utf-8"))))
    return signals


def _read_shutdown_payloads(*, root: Path, team_name: str) -> list[dict[str, object]]:
    shutdown_dir = _team_root(root, team_name) / "shutdown"
    if not shutdown_dir.exists():
        return []
    return [_read_json(path) for path in sorted(shutdown_dir.glob("*/*.json"))]


def _write_shutdown(*, root: Path, team_name: str, signal: ShutdownSignal) -> None:
    _write_json(
        _shutdown_path(root=root, team_name=team_name, worker_name=signal.worker, signal_id=signal.signal_id),
        asdict(signal),
    )


@contextmanager
def _task_lock(*, root: Path, team_name: str, task_id: str):
    safe_id = safe_task_id(task_id)
    lock_dir = _team_root(root, team_name) / "tasks" / f".lock-{safe_id}"
    deadline = time.monotonic() + 5
    acquired = False
    while time.monotonic() < deadline:
        try:
            lock_dir.mkdir()
            acquired = True
            break
        except FileExistsError:
            time.sleep(0.01)
    if not acquired:
        raise TimeoutError(f"timed out waiting for task lock: {safe_id}")
    try:
        yield
    finally:
        lock_dir.rmdir()


@contextmanager
def _mailbox_lock(*, root: Path, team_name: str, worker_name: str):
    mailbox_dir = _team_root(root, team_name) / "mailbox"
    mailbox_dir.mkdir(parents=True, exist_ok=True)
    lock_dir = mailbox_dir / f".lock-{worker_name}"
    deadline = time.monotonic() + 5
    acquired = False
    while time.monotonic() < deadline:
        try:
            lock_dir.mkdir()
            acquired = True
            break
        except FileExistsError:
            time.sleep(0.01)
    if not acquired:
        raise TimeoutError(f"timed out waiting for mailbox lock: {worker_name}")
    try:
        yield
    finally:
        lock_dir.rmdir()


def _safe_name(value: str, *, max_length: int, label: str) -> str:
    lowered = value.strip().lower()
    safe = re.sub(r"[^a-z0-9-]+", "-", lowered).strip("-")
    if not safe:
        raise ValueError(f"{label} name must contain at least one safe character")
    if len(safe) > max_length:
        raise ValueError(f"{label} name must be at most {max_length} characters")
    return safe


def _safe_actor(value: str) -> str:
    if value == "lead":
        return value
    return safe_worker_name(value)


def _archive_timestamp(value: str) -> str:
    timestamp = datetime.fromisoformat(value).astimezone(timezone.utc)
    return timestamp.strftime("%Y%m%dT%H%M%S%fZ")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    tmp_path.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
    tmp_path.replace(path)


def _read_json(path: Path) -> dict[str, object] | list[dict[str, object]]:
    return json.loads(path.read_text(encoding="utf-8"))


def _list_field(errors: list[str], payload: dict[str, object], key: str) -> list[object]:
    value = payload.get(key, [])
    if not isinstance(value, list):
        errors.append(f"{key} must be a list")
        return []
    return value


def _validate_safe_field(errors: list[str], payload: dict[str, object], key: str, label: str, validator) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str):
        errors.append(f"{label} must be a string")
        return None
    try:
        return validator(value)
    except ValueError:
        errors.append(f"{label} is unsafe: {value}")
        return None


def _validate_string_field(errors: list[str], payload: dict[str, object], key: str, label: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str):
        errors.append(f"{label} must be a string")
        return None
    return value


def _validate_bool_field(errors: list[str], payload: dict[str, object], key: str, label: str) -> bool | None:
    value = payload.get(key)
    if not isinstance(value, bool):
        errors.append(f"{label} must be a boolean")
        return None
    return value


def _write_json_create(path: Path, payload: object, *, exists_message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as file:
            file.write(f"{json.dumps(payload, indent=2, sort_keys=True)}\n")
    except FileExistsError as exc:
        raise FileExistsError(exists_message) from exc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
