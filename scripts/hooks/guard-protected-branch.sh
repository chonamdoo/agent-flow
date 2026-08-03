#!/bin/bash
# agent-flow PreToolUse hook: main/master/develop 브랜치에서 커밋·푸시 차단
INPUT=$(/bin/cat) || {
  echo "BLOCKED: 보호 브랜치 판정 입력을 읽지 못했습니다." >&2
  exit 2
}
MANAGED_PYTHON="${AGENT_FLOW_MANAGED_PYTHON:-/usr/bin/python3}"
case "$MANAGED_PYTHON" in
  /*) ;;
  *)
    echo "BLOCKED: 보호 브랜치 판정 Python 경로가 안전하지 않습니다." >&2
    exit 2
    ;;
esac
if [ ! -x "$MANAGED_PYTHON" ]; then
  echo "BLOCKED: 보호 브랜치 판정 Python을 실행할 수 없습니다. host session을 다시 시작하세요." >&2
  exit 2
fi
if ! PROTECTED_BRANCH=$("$MANAGED_PYTHON" -I -c "
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

def git_parts(tokens):
    while tokens:
        if tokens[0] == 'command':
            tokens = tokens[1:]
            continue
        if tokens[0] == 'env':
            tokens = skip_env(tokens)
            continue
        break
    if not tokens or tokens[0] != 'git':
        return [], []
    index = 1
    global_args = []
    options_with_value = {
        '-C', '-c', '--git-dir', '--work-tree', '--namespace', '--config-env', '--exec-path',
    }
    while index < len(tokens) and tokens[index].startswith('-') and tokens[index] != '--':
        option = tokens[index]
        global_args.append(option)
        index += 1
        if option in options_with_value and index < len(tokens):
            global_args.append(tokens[index])
            index += 1
    if index < len(tokens) and tokens[index] == '--':
        index += 1
    return tokens[index:], global_args

def current_branch(global_args, cwd):
    # git이 멈추거나 없을 때 hook이 도구 호출을 무기한 막지 않도록 방어한다.
    try:
        result = subprocess.run(
            ['/usr/bin/git', *global_args, 'branch', '--show-current'],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ''
    if result.returncode != 0:
        return ''
    return result.stdout.strip()

def shell_tokens(command):
    # shlex는 ValueError 전에 일부 토큰을 이미 내보내므로, 부분 토큰으로
    # 오판하지 않도록 전체 파싱이 성공한 경우에만 토큰을 반환한다.
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|()')
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return
    for token in tokens:
        if token and all(char in ';&|()' for char in token):
            for char in token:
                yield char
        else:
            yield token

def inspect_tokens(tokens, cwd):
    if not tokens:
        return '', cwd
    if tokens[0] == 'cd':
        target = tokens[1] if len(tokens) > 1 else os.path.expanduser('~')
        next_cwd = os.path.abspath(os.path.join(cwd, target)) if not os.path.isabs(target) else target
        return '', next_cwd if os.path.isdir(next_cwd) else cwd
    args, global_args = git_parts(tokens)
    if args and args[0] in ('commit', 'push'):
        branch = current_branch(global_args, cwd)
        if branch in ('main', 'master', 'develop'):
            return branch, cwd
    return '', cwd

def classify(command, initial_cwd):
    cwd_stack = [initial_cwd]
    current = []
    for token in shell_tokens(command):
        if token in ';&|':
            branch, cwd_stack[-1] = inspect_tokens(current, cwd_stack[-1])
            if branch:
                return branch
            current = []
            continue
        if token == '(':
            branch, cwd_stack[-1] = inspect_tokens(current, cwd_stack[-1])
            if branch:
                return branch
            current = []
            cwd_stack.append(cwd_stack[-1])
            continue
        if token == ')':
            branch, cwd_stack[-1] = inspect_tokens(current, cwd_stack[-1])
            if branch:
                return branch
            current = []
            if len(cwd_stack) > 1:
                cwd_stack.pop()
            continue
        current.append(token)
    branch, cwd_stack[-1] = inspect_tokens(current, cwd_stack[-1])
    if branch:
        return branch
    return ''

d = json.load(sys.stdin)
initial_cwd = d.get('cwd') if isinstance(d.get('cwd'), str) else ''
if not initial_cwd or not os.path.isdir(initial_cwd):
    initial_cwd = os.environ.get('AGENT_FLOW_PROJECT_ROOT', '')
if not initial_cwd or not os.path.isdir(initial_cwd):
    initial_cwd = os.getcwd()
print(classify(command_from(d), initial_cwd))
" 2>/dev/null <<< "$INPUT"); then
  echo "BLOCKED: 보호 브랜치 판정기를 실행하지 못했습니다." >&2
  exit 2
fi

if [ -z "$PROTECTED_BRANCH" ]; then
  exit 0
fi

# exit 2일 때 Claude는 stderr만 모델에 전달한다. stdout은 무시된다.
echo "BLOCKED: 보호 브랜치 '$PROTECTED_BRANCH'에서 직접 커밋/푸시하지 마세요." >&2
echo "agent-flow worktree create --name feat-<slug> 로 작업 worktree를 만드세요." >&2
exit 2
