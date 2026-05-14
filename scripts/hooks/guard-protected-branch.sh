#!/bin/bash
# agent-flow PreToolUse hook: main/master/develop 브랜치에서 커밋·푸시 차단
INPUT=$(cat)
CMD=$(python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('tool_input', {}).get('command', ''))
" 2>/dev/null <<< "$INPUT")

if [ -z "$CMD" ]; then
  exit 0
fi

if echo "$CMD" | grep -qE '^\s*git\s+(commit|push)\b'; then
  BRANCH=$(git branch --show-current 2>/dev/null)
  case "$BRANCH" in
    main|master|develop)
      echo "BLOCKED: 보호 브랜치 '$BRANCH'에서 직접 커밋/푸시하지 마세요."
      echo "git worktree add .agent-flow/worktrees/feat-<slug>/ -b feat/<slug> 로 작업 브랜치를 만드세요."
      exit 2
      ;;
  esac
fi

exit 0
