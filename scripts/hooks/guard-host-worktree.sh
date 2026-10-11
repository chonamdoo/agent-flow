#!/bin/sh
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
"${AGENT_FLOW_HOOK_PYTHON:-python3}" -I - "$SCRIPT_DIR" 3<&0 <<'PY'
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def load_boundary(script_dir: Path):
    install_root = script_dir.resolve().parents[1]
    for source in (
        install_root / "runtime" / "python",
        install_root / "src",
    ):
        module = source / "agent_flow" / "core" / "host_write_boundary.py"
        if module.is_file():
            sys.path.insert(0, str(source))
            break
    else:
        raise RuntimeError("trusted host worktree boundary is unavailable")
    from agent_flow.core.host_write_boundary import (
        host_write_boundary_violation,
        prepare_host_continue_input,
    )

    project_root = install_root.parent if install_root.name == ".agent-flow" else install_root
    return project_root, host_write_boundary_violation, prepare_host_continue_input


try:
    with os.fdopen(3, encoding="utf-8") as stream:
        payload = json.load(stream)
except (json.JSONDecodeError, OSError, UnicodeError) as exc:
    raise RuntimeError("invalid host worktree guard payload") from exc
if not isinstance(payload, dict):
    raise RuntimeError("invalid host worktree guard payload")
project_root, checker, prepare_continue = load_boundary(Path(sys.argv[1]))
violation = checker(payload, project_root)
if violation is not None and not isinstance(violation, str):
    raise RuntimeError("host worktree boundary returned an invalid decision")
if violation:
    print(violation, file=sys.stderr)
    raise SystemExit(2)
updated_input = prepare_continue(payload, project_root)
if updated_input is not None:
    decision = {"hookEventName": "PreToolUse", "updatedInput": updated_input}
    # Codex 0.162.1은 permissionDecision:allow 없는 updatedInput을 hook 실패로 보고
    # 버린다. Claude에서 allow는 권한 확인을 건너뛰므로 turn_id를 보내는 Codex에만 붙인다.
    if "turn_id" in payload:
        decision["permissionDecision"] = "allow"
    print(json.dumps({"hookSpecificOutput": decision}))
PY
STATUS=$?
if [ "$STATUS" -eq 2 ]; then
  echo "작업을 중단했습니다: 현재 host session에 연결된 worktree 밖으로 쓰거나 실행하려 했습니다. 안내된 worktree 경로에서 agent-flow status/continue를 다시 실행한 뒤 계속하세요." >&2
  exit 2
fi
# 판정기 자체가 죽은 것은 사용자의 위반 증거가 아니다. hook 설치·무결성은 run
# 시작 게이트(hook_integrity digest)가 이미 증명하므로, 여기서 차단 판정을
# 만들어 내면 복구 명령까지 막히는 데드락만 남는다.
exit 0
