#!/bin/bash
# agent-flow PreToolUse hook: leader worktree 브랜치 변경 차단 → git worktree add 안내
INPUT=$(/bin/cat)
ACTION=$(/usr/bin/python3 -I -B -c "
import sys, json
import re
import shlex
import subprocess

def command_from(value):
    if isinstance(value, dict):
        tool_input = value.get('tool_input')
        if isinstance(tool_input, dict):
            for key in ('command', 'cmd'):
                if isinstance(tool_input.get(key), str):
                    return tool_input[key]
        for key in ('command', 'cmd'):
            if isinstance(value.get(key), str):
                return value[key]
        for child in value.values():
            found = command_from(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = command_from(child)
            if found:
                return found
    return ''

def split_segments(command):
    return [part for part in re.split(r'[;&|()]+', command) if part.strip()]

def skip_env(tokens):
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', token):
            index += 1
            continue
        if token in ('-i', '--ignore-environment'):
            index += 1
            continue
        if token in ('-u', '--unset') and index + 1 < len(tokens):
            index += 2
            continue
        break
    return tokens[index:]

def git_args(tokens):
    while tokens:
        if tokens[0] == 'command':
            tokens = tokens[1:]
            continue
        if tokens[0] == 'env':
            tokens = skip_env(tokens)
            continue
        break
    if not tokens or tokens[0] != 'git':
        return []
    index = 1
    options_with_value = {
        '-C', '-c', '--git-dir', '--work-tree', '--namespace', '--config-env', '--exec-path',
    }
    while index < len(tokens) and tokens[index].startswith('-') and tokens[index] != '--':
        option = tokens[index]
        index += 1
        if option in options_with_value and index < len(tokens):
            index += 1
    if index < len(tokens) and tokens[index] == '--':
        index += 1
    return tokens[index:]

def is_local_branch(name):
    # git이 멈추거나 없을 때 hook이 도구 호출을 무기한 막지 않도록 방어한다.
    try:
        result = subprocess.run(
            ['/usr/bin/git', 'show-ref', '--verify', '--quiet', 'refs/heads/' + name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0

def switch_action(args):
    if not args:
        return ''
    subcommand, rest = args[0], args[1:]
    if subcommand == 'switch':
        if any(arg in ('-c', '--create') for arg in rest):
            return 'create'
        if rest:
            return 'change'
    if subcommand == 'checkout':
        if any(arg in ('-b', '-B', '--orphan') for arg in rest):
            return 'create'
        if any(arg == '--detach' for arg in rest):
            return 'change'
        if '--' in rest:
            return ''
        if rest and not rest[0].startswith('-'):
            # 태그/SHA/파일 경로 checkout은 허용. 로컬 브랜치 전환만 차단한다.
            return 'change' if is_local_branch(rest[0]) else ''
    return ''

def classify(command):
    for segment in split_segments(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        action = switch_action(git_args(tokens))
        if action:
            return action
    return ''

d = json.load(sys.stdin)
print(classify(command_from(d)))
" 2>/dev/null <<< "$INPUT")

if [ -z "$ACTION" ]; then
  exit 0
fi

# exit 2일 때 Claude는 stderr만 모델에 전달한다. stdout은 무시된다.
if [ "$ACTION" = "create" ]; then
  echo "BLOCKED: 브랜치만 만들지 마세요. 병렬 작업 격리를 위해 git worktree add를 사용하세요." >&2
  echo "예: git worktree add -b feat/<slug> .agent-flow/worktrees/feat-<slug>/ main" >&2
  exit 2
fi

if [ "$ACTION" = "change" ]; then
  echo "BLOCKED: 기준 worktree의 현재 브랜치를 바꾸지 마세요." >&2
  echo "새 작업은 git worktree add -b feat/<slug> .agent-flow/worktrees/feat-<slug>/ main 으로 시작하세요." >&2
  exit 2
fi

exit 0
