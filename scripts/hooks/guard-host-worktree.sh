#!/bin/bash
MANAGED_PYTHON="${AGENT_FLOW_MANAGED_PYTHON:-/usr/bin/python3}"
case "$MANAGED_PYTHON" in
  /*) ;;
  *)
    echo "작업을 중단했습니다: host worktree 판정 Python 경로가 안전하지 않습니다." >&2
    exit 2
    ;;
esac
if [ ! -x "$MANAGED_PYTHON" ]; then
  echo "작업을 중단했습니다: host worktree 판정 Python을 실행할 수 없습니다. host session을 다시 시작하세요." >&2
  exit 2
fi
"$MANAGED_PYTHON" -I - 3<&0 <<'PY'
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
exec(os.environ["AGENT_FLOW_VERIFIED_IMPORT_BOOTSTRAP"], globals())


def load_boundary():
    from agent_flow.core.host_write_boundary import host_write_boundary_violation

    project_root = Path(os.environ["AGENT_FLOW_PROJECT_ROOT"]).resolve()
    return project_root, host_write_boundary_violation


try:
    with os.fdopen(3, encoding="utf-8") as stream:
        payload = json.load(stream)
except (json.JSONDecodeError, OSError, UnicodeError) as exc:
    raise RuntimeError("invalid host worktree guard payload") from exc
if not isinstance(payload, dict):
    raise RuntimeError("invalid host worktree guard payload")
project_root, checker = load_boundary()
violation = checker(payload, project_root)
if violation is not None and not isinstance(violation, str):
    raise RuntimeError("host worktree boundary returned an invalid decision")
if violation:
    print(violation, file=sys.stderr)
    raise SystemExit(2)
PY
STATUS=$?
if [ "$STATUS" -ne 0 ]; then
  echo "작업을 중단했습니다: 현재 host session에 연결된 worktree 밖으로 쓰거나 실행하려 했거나 판정기를 안전하게 실행하지 못했습니다. 안내된 worktree 경로에서 agent-flow status/continue를 다시 실행한 뒤 계속하세요." >&2
  exit 2
fi
exit 0
