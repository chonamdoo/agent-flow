from __future__ import annotations

import json
import re
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


def init_team(*, root: Path, name: str, description: str) -> TeamConfig:
    team_name = safe_team_name(name)
    config = TeamConfig(name=team_name, description=description, created_at=_now())
    _write_json(_team_root(root, team_name) / "config.json", asdict(config))
    (_team_root(root, team_name) / "tasks").mkdir(parents=True, exist_ok=True)
    (_team_root(root, team_name) / "workers").mkdir(parents=True, exist_ok=True)
    (_team_root(root, team_name) / "mailbox").mkdir(parents=True, exist_ok=True)
    return config


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


def team_status(*, root: Path, team_name: str) -> dict[str, object]:
    safe_team = safe_team_name(team_name)
    root_dir = _team_root(root, safe_team)
    tasks = sorted((root_dir / "tasks").glob("*.json")) if (root_dir / "tasks").exists() else []
    workers = sorted((root_dir / "workers").glob("*/identity.json")) if (root_dir / "workers").exists() else []
    return {
        "team": safe_team,
        "exists": root_dir.exists(),
        "task_count": len(tasks),
        "worker_count": len(workers),
        "path": str(root_dir),
    }


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


def _team_root(root: Path, team_name: str) -> Path:
    return root / ".agent-flow" / "state" / "team" / team_name


def _mailbox_path(*, root: Path, team_name: str, worker_name: str) -> Path:
    return _team_root(root, team_name) / "mailbox" / f"{worker_name}.json"


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


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")


def _write_json_create(path: Path, payload: object, *, exists_message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as file:
            file.write(f"{json.dumps(payload, indent=2, sort_keys=True)}\n")
    except FileExistsError as exc:
        raise FileExistsError(exists_message) from exc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
