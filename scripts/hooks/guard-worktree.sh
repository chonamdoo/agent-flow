#!/bin/bash
# agent-flow PreToolUse hook: leader worktree 브랜치 변경 차단 → git worktree add 안내
TRUSTED_PYTHON=/usr/bin/python3
if [ ! -f "$TRUSTED_PYTHON" ] || [ ! -x "$TRUSTED_PYTHON" ]; then
  echo "BLOCKED: trusted Python interpreter is unavailable or unsafe." >&2
  exit 2
fi
INPUT=$(/bin/cat)
if ! ACTION=$("$TRUSTED_PYTHON" -c "
import sys, json
import os
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

def command_segments(command):
    try:
        lexer = shlex.shlex(
            command.replace('\r', '\n').replace('\n', ';'),
            posix=True,
            punctuation_chars=';&|()',
        )
        lexer.whitespace_split = True
        lexer.commenters = ''
        tokens = list(lexer)
    except ValueError:
        return []
    segments = [[]]
    for token in tokens:
        if token and all(char in ';&|()' for char in token):
            for _ in token:
                if segments[-1]:
                    segments.append([])
            continue
        segments[-1].append(token)
    return [segment for segment in segments if segment]

def nested_shell_command(tokens):
    while tokens and tokens[0] == 'command':
        tokens = tokens[1:]
    if tokens and tokens[0] == 'env':
        tokens = skip_env(tokens)
    if not tokens or os.path.basename(tokens[0]) not in ('bash', 'sh', 'zsh'):
        return ''
    for index, token in enumerate(tokens[1:], start=1):
        if token == '-c' or (
            token.startswith('-') and not token.startswith('--') and 'c' in token[1:]
        ):
            return tokens[index + 1] if index + 1 < len(tokens) else ''
    return ''

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

def unwrap_command(tokens):
    remaining = list(tokens)
    while remaining:
        while remaining and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', remaining[0]):
            remaining = remaining[1:]
        if not remaining:
            return remaining, False
        wrapper = os.path.basename(remaining[0])
        if wrapper in ('doas', 'parallel', 'sudo', 'xargs'):
            return [], True
        if wrapper in ('command', 'builtin'):
            index = 1
            while index < len(remaining):
                token = remaining[index]
                if token in ('-v', '-V'):
                    return [], False
                if token == '-p':
                    index += 1
                    continue
                if token == '--':
                    index += 1
                    break
                if token.startswith('-'):
                    return [], True
                break
            remaining = remaining[index:]
            continue
        if wrapper == 'env':
            index = 1
            while index < len(remaining):
                token = remaining[index]
                if re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', token):
                    index += 1
                    continue
                if token in ('-i', '--ignore-environment'):
                    index += 1
                    continue
                if token in ('-u', '--unset'):
                    if index + 1 >= len(remaining):
                        return [], True
                    index += 2
                    continue
                if token.startswith('--unset='):
                    index += 1
                    continue
                if token == '--':
                    index += 1
                    break
                if token.startswith('-'):
                    return [], True
                break
            remaining = remaining[index:]
            continue
        if wrapper == 'exec':
            index = 1
            while index < len(remaining):
                token = remaining[index]
                if token == '--':
                    index += 1
                    break
                if token in ('-c', '-l') or (
                    token.startswith('-')
                    and not token.startswith('--')
                    and set(token[1:]) <= {'c', 'l'}
                ):
                    index += 1
                    continue
                if token == '-a':
                    if index + 1 >= len(remaining):
                        return [], True
                    index += 2
                    continue
                if token.startswith('-'):
                    return [], True
                break
            remaining = remaining[index:]
            continue
        return remaining, False
    return remaining, False

def git_args(tokens):
    if not tokens:
        return []
    executable = os.path.basename(tokens[0])
    if executable in ('git-checkout', 'git-switch'):
        return [executable.removeprefix('git-'), *tokens[1:]]
    if executable != 'git':
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
        return None
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
            return 'change' if is_local_branch(rest[0]) is not False else ''
    return ''

def classify(command, depth=0):
    if depth > 4:
        return 'change'
    for tokens in command_segments(command):
        tokens, dynamic = unwrap_command(tokens)
        if dynamic:
            return 'change'
        nested = nested_shell_command(tokens)
        if nested:
            action = classify(nested, depth + 1)
            if action:
                return action
        action = switch_action(git_args(tokens))
        if action:
            return action
    return ''

d = json.load(sys.stdin)
print(classify(command_from(d)))
" 2>/dev/null <<< "$INPUT"); then
  echo "BLOCKED: worktree hook input could not be validated." >&2
  exit 2
fi

if [ -z "$ACTION" ]; then
  exit 0
fi

# exit 2일 때 Claude는 stderr만 모델에 전달한다. stdout은 무시된다.
if [ "$ACTION" = "create" ]; then
  echo "BLOCKED: 브랜치만 만들지 마세요. 병렬 작업 격리를 위해 git worktree add를 사용하세요." >&2
  echo "새 작업은 ./.agent-flow/bin/agent-flow run \"<task>\"으로 시작해 profile 기준 worktree를 생성하세요." >&2
  exit 2
fi

if [ "$ACTION" = "change" ]; then
  echo "BLOCKED: 기준 worktree의 현재 브랜치를 바꾸지 마세요." >&2
  echo "현재 run의 pinned worktree에서 계속하거나 ./.agent-flow/bin/agent-flow run \"<task>\"으로 새 작업을 시작하세요." >&2
  exit 2
fi

exit 0
