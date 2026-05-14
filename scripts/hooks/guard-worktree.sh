#!/bin/bash
# agent-flow PreToolUse hook: 브랜치 생성 명령 차단 → git worktree add 안내
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
  echo "예: git worktree add .agent-flow/worktrees/feat-<slug>/ -b feat/<slug>"
  exit 2
fi

exit 0
