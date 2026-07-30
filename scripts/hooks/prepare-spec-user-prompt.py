#!/usr/bin/python3
"""현재 대기 중인 SPEC 집합에 session 결합 challenge를 준비한다."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path


def _agent_flow_command() -> tuple[str, ...] | None:
    install_root = Path(__file__).resolve().parents[2]
    candidate = install_root / "bin" / "agent-flow"
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
    if not _launcher_digest_matches(install_root, candidate):
        return None
    return (str(candidate),)


def _launcher_digest_matches(install_root: Path, launcher: Path) -> bool:
    """kit.json이 기록한 내용과 같은 launcher만 실행한다.

    이 검사가 없으면 launcher를 갈아끼운 쪽이 다음 사용자 입력 때 승인 capability를
    그대로 받는다. 런 시작 게이트(`hook_integrity`)는 런당 1회지만 이 hook은 모든
    프롬프트에서 돌기 때문에, 실행 직전 대조가 없으면 노출 창이 반대로 넓다.
    """
    try:
        recorded = json.loads(
            (install_root / "kit.json").read_text(encoding="utf-8")
        ).get("project_launcher_digest")
        if not isinstance(recorded, str) or len(recorded) != 64:
            return False
        return hashlib.sha256(launcher.read_bytes()).hexdigest() == recorded
    except (OSError, ValueError):
        return False


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    if payload.get("hook_event_name") == "UserPromptSubmit":
        # 승인 경로의 challenge 회전 주체는 confirm hook 하나다. 같은 이벤트에
        # 병렬로 등록된 두 hook이 각자 회전하면 정당한 승인이 조용히 유실된다.
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
    try:
        subprocess.run(
            (
                *command,
                "spec",
                "prepare-confirmation",
                "--root",
                root,
                "--session-id",
                session_id,
                "--hook-capability-hash",
                hashlib.sha256(capability.encode("ascii")).hexdigest(),
            ),
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
