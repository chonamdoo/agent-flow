from __future__ import annotations

import json
import os
import stat
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4


START_LOCK_VERSION = 1
START_LOCK_KEYS = (
    "version",
    "token",
    "pid",
    "runtime",
    "created_at",
    "project_root",
)


def assert_no_install_transaction(project_root: Path) -> None:
    agent_flow_root = project_root / ".agent-flow"
    transaction_root = agent_flow_root / "install-transaction"
    if not transaction_root.exists() and not transaction_root.is_symlink():
        return
    try:
        agent_flow_stat = agent_flow_root.lstat()
        transaction_stat = transaction_root.lstat()
    except OSError as exc:
        raise RuntimeError(
            "blocked: project install transaction metadata is unsafe"
        ) from exc
    if (
        stat.S_ISLNK(agent_flow_stat.st_mode)
        or not stat.S_ISDIR(agent_flow_stat.st_mode)
        or stat.S_ISLNK(transaction_stat.st_mode)
        or not stat.S_ISDIR(transaction_stat.st_mode)
    ):
        raise RuntimeError("blocked: project install transaction metadata is unsafe")
    raise RuntimeError(
        "blocked: an agent-flow install transaction is in progress or requires recovery; "
        "rerun `agent-flow install` from the leader checkout before using the workflow"
    )


def project_start_lock_path(project_root: Path) -> Path:
    root = project_root.resolve(strict=True)
    try:
        result = subprocess.run(
            ("git", "rev-parse", "--git-common-dir"),
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        common = Path(result.stdout.strip())
        common = common if common.is_absolute() else root / common
        return common.resolve(strict=True) / "agent-flow" / "start.lock"
    return root / ".agent-flow" / "state" / "start.lock"


@contextmanager
def project_start_lock(project_root: Path, *, runtime: str = "python") -> Iterator[Path]:
    root = project_root.resolve(strict=True)
    lock_path = project_start_lock_path(root)
    _ensure_plain_parent(lock_path.parent)
    payload = {
        "version": START_LOCK_VERSION,
        "token": uuid4().hex,
        "pid": os.getpid(),
        "runtime": runtime,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(root),
    }
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(_start_lock_message(lock_path)) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        lock_path.unlink(missing_ok=True)
        raise
    try:
        yield lock_path
    finally:
        _release_start_lock(lock_path, payload["token"])


def _release_start_lock(lock_path: Path, token: str) -> None:
    try:
        current_stat = lock_path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISREG(current_stat.st_mode):
        raise RuntimeError(_start_lock_message(lock_path))
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(_start_lock_message(lock_path)) from exc
    if not isinstance(payload, dict) or payload.get("token") != token:
        raise RuntimeError(_start_lock_message(lock_path))
    lock_path.unlink()


def _ensure_plain_parent(directory: Path) -> None:
    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    try:
        current_stat = current.lstat()
    except FileNotFoundError:
        current_stat = None
    if current_stat is not None and (
        stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode)
    ):
        raise RuntimeError(f"blocked: unsafe start lock parent: {current}")
    for path in reversed(missing):
        path.mkdir()


def _start_lock_message(lock_path: Path) -> str:
    return (
        f"blocked: agent-flow start lock exists at {lock_path}; another start may be running. "
        "If no start process is active, inspect the lock and remove it manually before retrying"
    )
