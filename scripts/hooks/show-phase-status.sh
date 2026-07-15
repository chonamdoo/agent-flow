#!/bin/bash
# agent-flow Stop hook: 세션 종료 시 현재 워크플로우 phase 표시
INPUT=$(/bin/cat)
HOOK_HOST=""
if [ "$1" = "--host" ]; then
  HOOK_HOST="$2"
fi
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
AGENT_FLOW="$AGENT_FLOW" PROJECT_ROOT="$PROJECT_ROOT" HOOK_INPUT="$INPUT" HOOK_HOST="$HOOK_HOST" python3 - <<'PY' 2>/dev/null || exit 0
import json, os, subprocess

try:
    payload = json.loads(os.environ.get("HOOK_INPUT") or "{}")
except json.JSONDecodeError:
    payload = {}
child_env = dict(os.environ)
host = payload.get("host") or os.environ.get("HOOK_HOST")
session_id = (
    payload.get("execution_id")
    or payload.get("thread_id")
    or payload.get("session_id")
)
agent_id = payload.get("agent_id")
if host:
    child_env["AGENT_FLOW_ACTIVE_HOST"] = str(host)
if session_id:
    child_env["AGENT_FLOW_EXECUTION_ID"] = str(session_id)
if agent_id:
    child_env["AGENT_FLOW_AGENT_ID"] = str(agent_id)
try:
    result = subprocess.run(
        [os.environ["AGENT_FLOW"], "status"],
        cwd=os.environ["PROJECT_ROOT"],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
except (OSError, subprocess.TimeoutExpired):
    raise SystemExit(0)
if result.returncode != 0 or not result.stdout.strip():
    raise SystemExit(0)

fields = {}
for line in result.stdout.splitlines():
    if line.startswith("status_json: "):
        try:
            candidate = json.loads(line.removeprefix("status_json: "))
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            fields = candidate
            break
if not fields:
    allowed = {"status", "current_phase", "reason", "next_command"}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() in allowed:
            fields[key.strip()] = value.strip()
if not fields.get("status"):
    raise SystemExit(0)

def compact(value, limit):
    normalized = " ".join(str(value or "-").split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"

message = " | ".join(
    (
        f"status: {compact(fields.get('status'), 32)}",
        f"current_phase: {compact(fields.get('current_phase'), 120)}",
        f"reason: {compact(fields.get('reason'), 160)}",
        f"next_command: {compact(fields.get('next_command'), 640)}",
    )
)
print(json.dumps({"systemMessage": "[agent-flow] " + message}, ensure_ascii=False))
PY
exit 0
