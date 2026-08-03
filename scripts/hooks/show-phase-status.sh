#!/bin/bash
# agent-flow Stop hook: 세션 종료 시 현재 워크플로우 phase 표시
HOOK_SOURCE="${AGENT_FLOW_MANAGED_HOOK_PATH:-${BASH_SOURCE[0]}}"
SCRIPT_DIR="$(cd "$(/usr/bin/dirname "$HOOK_SOURCE")" && pwd)"
PROJECT_ROOT="${AGENT_FLOW_PROJECT_ROOT:-}"
if [ -z "$PROJECT_ROOT" ]; then
  if [ -f ".agent-flow/kit.json" ]; then
    PROJECT_ROOT="$PWD"
  elif [ -f "$SCRIPT_DIR/../../kit.json" ]; then
    PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
  elif [ -f "$SCRIPT_DIR/../../.agent-flow/kit.json" ]; then
    PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
  else
    exit 0
  fi
fi
MANAGED_PYTHON="${AGENT_FLOW_MANAGED_PYTHON:-}"
case "$MANAGED_PYTHON" in
  /*) ;;
  *) exit 0 ;;
esac
if [ ! -x "$MANAGED_PYTHON" ]; then
  exit 0
fi

# Stop hook stdout은 JSON으로 파싱된다. 평문 출력은 Claude Code의
# "invalid stop hook json output" 에러를 유발하고 사용자에게 표시되지도 않는다.
PROJECT_ROOT="$PROJECT_ROOT" "$MANAGED_PYTHON" -I - <<'PY' 2>/dev/null || exit 0
import json
import os
from pathlib import Path

root = Path(os.environ["PROJECT_ROOT"])

current_meta = list((root / ".agent-flow" / "runs").glob("*/meta.json"))
private_runs = root / ".git" / "agent-flow" / "worktrees"
current_meta.extend(private_runs.glob("*/.agent-flow/runs/*/meta.json"))
current = []
for meta in current_meta:
    active = meta.with_name("active")
    if not active.is_file():
        continue
    try:
        payload = json.loads(meta.read_text(encoding="utf-8"))
        modified = max(meta.stat().st_mtime_ns, active.stat().st_mtime_ns)
    except (OSError, ValueError, TypeError):
        continue
    if not isinstance(payload, dict):
        continue
    run_id = payload.get("run_id") or meta.parent.name
    workflow = payload.get("workflow") or payload.get("workflow_id")
    if not run_id or not workflow:
        continue
    current.append(
        (
            modified,
            {
                **payload,
                "run_id": run_id,
                "workflow_id": workflow,
                "status": payload.get("status") or "running",
            },
        )
    )

legacy_manifests = list((root / ".agent-flow" / "runs").glob("*/*/manifest.json"))
legacy_manifests.extend(
    private_runs.glob("*/.agent-flow/runs/*/*/manifest.json")
)
legacy = []
for manifest in legacy_manifests:
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        modified = manifest.stat().st_mtime_ns
    except (OSError, ValueError, TypeError):
        continue
    if isinstance(payload, dict) and payload.get("run_id") and payload.get("status"):
        legacy.append((modified, payload))

valid = current or legacy
if not valid:
    status = "no runs"
else:
    payload = max(valid, key=lambda item: item[0])[1]
    status = "\n".join(
        (
            f"Run id     : {payload['run_id']}",
            f"Workflow   : {payload.get('workflow_id', '-')}",
            f"Status     : {payload['status']}",
            f"Phase      : {payload.get('current_phase') or payload.get('phase') or '-'}",
            f"Task       : {payload.get('task', '')}",
        )
    )
print(json.dumps({"systemMessage": "[agent-flow] " + status}, ensure_ascii=False))
PY
exit 0
