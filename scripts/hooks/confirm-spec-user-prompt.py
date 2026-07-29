#!/usr/bin/python3
"""현재 사용자 chat의 exact 승인을 대기 중인 SPEC 집합에 기록한다."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path


def _prompt(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("prompt", "text", "message"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def _agent_flow_command() -> tuple[str, ...] | None:
    candidate = Path(__file__).resolve().parents[2] / "bin" / "agent-flow"
    try:
        identity = candidate.lstat()
    except OSError:
        return None
    if (
        not stat.S_ISREG(identity.st_mode)
        or identity.st_uid != os.getuid()
        or identity.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not os.access(candidate, os.X_OK)
    ):
        return None
    return (str(candidate),)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if (
        not isinstance(payload, dict)
        or payload.get("hook_event_name") != "UserPromptSubmit"
    ):
        return 0
    session_id = payload.get("session_id") or payload.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        return 0
    cwd = payload.get("cwd")
    root = str(cwd) if isinstance(cwd, str) and cwd else str(
        os.environ.get("PROJECT_ROOT") or os.getcwd()
    )
    command = _agent_flow_command()
    if command is None:
        return 0
    capability = secrets.token_hex(32)
    capability_hash = hashlib.sha256(capability.encode("ascii")).hexdigest()
    try:
        prepared = subprocess.run(
            (
                *command,
                "spec",
                "prepare-confirmation",
                "--root",
                root,
                "--session-id",
                session_id,
                "--hook-capability-hash",
                capability_hash,
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        if prepared.returncode != 0 or _prompt(payload) != "승인":
            return 0
        subprocess.run(
            (
                *command,
                "spec",
                "confirm",
                "--root",
                root,
                "--from-user-prompt",
                "--session-id",
                session_id,
                "--hook-capability",
                capability,
            ),
            input="승인",
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
