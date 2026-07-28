#!/bin/bash
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
/usr/bin/python3 -I - "$SCRIPT_DIR" 3<&0 <<'PY'
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROTECTED_STATE_NAMES = {
    "spec-user-confirmation.json",
    "spec-user-confirmation.pending.json",
    "spec-user-confirmation.lock",
    "spec-manual-approvals.json",
    "commands-run.jsonl",
}
PROTECTED_STATE_PARTS = {"spec-hook-capabilities"}
COMMAND_KEYS = ("command", "cmd", "script", "shell_command")
PATH_KEYS = {"path", "file", "file_path", "target", "destination", "dest"}


def commands_from(value: object) -> tuple[str, ...]:
    if not isinstance(value, dict):
        return ()
    commands = tuple(
        command
        for key in COMMAND_KEYS
        if isinstance((command := value.get(key)), str)
    )
    nested = tuple(
        command
        for key, child in value.items()
        if key not in COMMAND_KEYS
        for command in commands_from(child)
    )
    return commands + nested


def targets_protected_state(value: object) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in PATH_KEYS and isinstance(child, str):
                name = Path(child).name.lstrip(".")
                parts = {part.lstrip(".") for part in Path(child).parts}
                if (
                    name in PROTECTED_STATE_NAMES
                    or name.startswith("spec-user-confirmation.pending.")
                    or bool(parts & PROTECTED_STATE_PARTS)
                ):
                    return True
            if targets_protected_state(child):
                return True
    elif isinstance(value, list):
        return any(targets_protected_state(child) for child in value)
    return False


def approval_checker(script_dir: Path):
    script_base = script_dir.resolve().parents[1]
    for source in (
        script_base / "runtime" / "python",
        script_base / "src",
    ):
        module = source / "agent_flow" / "core" / "command_evidence.py"
        if module.is_file():
            sys.path.insert(0, str(source))
            break
    else:
        raise RuntimeError("trusted approval checker is unavailable")
    from agent_flow.core.command_evidence import (
        command_executes_agent_spec_approval,
    )

    return command_executes_agent_spec_approval


try:
    with os.fdopen(3, encoding="utf-8") as stream:
        payload = json.load(stream)
except (json.JSONDecodeError, OSError, UnicodeError) as exc:
    raise RuntimeError("invalid approval guard payload") from exc
if not isinstance(payload, dict):
    raise RuntimeError("invalid approval guard payload")
commands = commands_from(payload)
protected_state = targets_protected_state(payload)
if not commands and not protected_state:
    raise SystemExit(0)
checker = approval_checker(Path(sys.argv[1]))
blocked = protected_state
for command in commands:
    decision = checker(command)
    if not isinstance(decision, bool):
        raise RuntimeError("approval checker returned a non-boolean decision")
    blocked = blocked or decision
raise SystemExit(2 if blocked else 0)
PY
STATUS=$?
if [ "$STATUS" -eq 0 ]; then
  exit 0
fi
if [ "$STATUS" -eq 2 ]; then
  echo "작업을 중단했습니다: agent가 사용자 전용 SPEC 승인 명령 또는 hook을 실행하려 했습니다. 채팅에서 정확히 '승인'이라고 답하거나 안내된 짧은 fallback 명령을 사용자 터미널에서 직접 실행하세요." >&2
else
  echo "작업을 중단했습니다: 사용자 전용 SPEC 승인 검사기가 안전한 허용 결정을 내리지 못했습니다." >&2
fi
exit 2
