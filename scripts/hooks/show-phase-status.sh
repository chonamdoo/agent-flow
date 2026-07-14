#!/bin/bash
# agent-flow Stop hook: 세션 종료 시 현재 워크플로우 phase 표시
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT=""
if [ -f ".agent-flow/kit.json" ]; then
  PROJECT_ROOT="$PWD"
elif [ -f "$SCRIPT_DIR/../../kit.json" ]; then
  PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
elif [ -f "$SCRIPT_DIR/../../.agent-flow/kit.json" ]; then
  PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
else
  exit 0
fi
AGENT_FLOW="$(command -v agent-flow 2>/dev/null)"
if [ -z "$AGENT_FLOW" ]; then
  for candidate in "$PROJECT_ROOT/.venv/bin/agent-flow" "$SCRIPT_DIR/../../.venv/bin/agent-flow" ".venv/bin/agent-flow"; do
    if [ -x "$candidate" ]; then
      AGENT_FLOW="$candidate"
      break
    fi
  done
fi
if [ -z "$AGENT_FLOW" ]; then
  exit 0
fi

# Stop hook stdout은 JSON으로 파싱된다. 평문 출력은 Claude Code의
# "invalid stop hook json output" 에러를 유발하고 사용자에게 표시되지도 않는다.
# macOS에는 GNU timeout이 없으므로 status 호출도 python subprocess timeout으로
# 감싸 hook이 세션 종료를 무기한 막지 않게 한다.
AGENT_FLOW="$AGENT_FLOW" PROJECT_ROOT="$PROJECT_ROOT" python3 - <<'PY' 2>/dev/null || exit 0
import json, os, subprocess

try:
    result = subprocess.run(
        [os.environ["AGENT_FLOW"], "status"],
        cwd=os.environ["PROJECT_ROOT"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
except (OSError, subprocess.TimeoutExpired):
    raise SystemExit(0)
status = result.stdout.strip()
if result.returncode != 0 or not status:
    raise SystemExit(0)
print(json.dumps({"systemMessage": "[agent-flow] " + status}, ensure_ascii=False))
PY
exit 0
