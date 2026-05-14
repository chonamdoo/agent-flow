#!/bin/bash
# agent-flow Stop hook: 세션 종료 시 현재 워크플로우 phase 표시
if [ ! -f ".agent-flow/kit.json" ]; then
  exit 0
fi

STATUS=$(agent-flow status 2>/dev/null)
if [ $? -eq 0 ] && [ -n "$STATUS" ]; then
  echo "[agent-flow] $STATUS"
fi
exit 0
