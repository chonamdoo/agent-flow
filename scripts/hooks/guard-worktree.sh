#!/bin/bash
# agent-flow PreToolUse hook: leader worktree 브랜치 변경 차단 → git worktree add 안내
INPUT=$(cat)
CMD=$(python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('tool_input', {}).get('command', ''))
" 2>/dev/null <<< "$INPUT")

if [ -z "$CMD" ]; then
  exit 0
fi

if echo "$CMD" | grep -qE '^\s*git\s+(checkout\s+-b|switch\s+(-c|--create)\s)'; then
  echo "BLOCKED: 브랜치만 만들지 마세요. 병렬 작업 격리를 위해 git worktree add를 사용하세요."
  echo "예: git worktree add -b feat/<slug> .agent-flow/worktrees/feat-<slug>/ main"
  exit 2
fi

if echo "$CMD" | grep -qE '(^|[;&|])\s*git\s+switch\s+(-[^[:space:]]+\s+)*[^-[:space:]]' || \
   echo "$CMD" | grep -qE '(^|[;&|])\s*git\s+checkout\s+[^-[:space:]][^[:space:]]*'; then
  echo "BLOCKED: 기준 worktree의 현재 브랜치를 바꾸지 마세요."
  echo "새 작업은 git worktree add -b feat/<slug> .agent-flow/worktrees/feat-<slug>/ main 으로 시작하세요."
  exit 2
fi

exit 0
