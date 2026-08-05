#!/bin/bash
# agent-flow PreToolUse hook: main/master/develop 브랜치에서 커밋·푸시 차단
INPUT=$(cat)
PROTECTED_BRANCH=$(python3 -c "
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

_CWD_KEYS = ('cwd', 'workdir', 'working_directory')

def tool_cwd_from(value):
    # command_from과 달리 payload 전체를 뒤지지 않는다. 깊은 중첩은 RecursionError를
    # 내는데 이 python의 stderr는 버려지므로 판정이 빈 문자열이 되고 가드가 통째로
    # 사라진다. 게다가 무관한 하위 dict(tool_input.env.cwd 등)의 값은 실행 자리를
    # 옮기지 않으면서 판정만 옮긴다. 못 찾으면 세션 cwd로 접히므로 차단 쪽으로 실패한다.
    if not isinstance(value, dict):
        return ''
    tool_input = value.get('tool_input')
    if not isinstance(tool_input, dict):
        return ''
    for key in _CWD_KEYS:
        if isinstance(tool_input.get(key), str) and tool_input[key].strip():
            return tool_input[key].strip()
    return ''

def payload_cwd(value):
    if isinstance(value, dict):
        raw = value.get('cwd')
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ''

def resolve_cwd(raw, base):
    # 실재하지 않는 선언은 자리를 옮기지 못한다. 그 경로에서 브랜치를 물으면
    # git이 실패하고, 실패는 미판정이라 보호 브랜치 명령이 그대로 통과한다.
    if not raw:
        return base
    candidate = os.path.expanduser(raw)
    if not os.path.isabs(candidate):
        candidate = os.path.join(base, candidate)
    candidate = os.path.abspath(candidate)
    return candidate if os.path.isdir(candidate) else base

def declared_cwd(value):
    # 세션 프로세스의 cwd는 host가 세션을 연 자리(대개 leader)다. 도구 호출이
    # 선언한 자리와 다르면 worktree에서 도는 명령을 leader의 브랜치로 판정한다.
    # host_write_boundary._declared_command_cwd와 같은 증거·순서를 쓴다: payload
    # cwd를 기준으로 tool cwd를 풀고, 명령 앞머리의 cd는 inspect_tokens가 얹는다.
    return resolve_cwd(tool_cwd_from(value), resolve_cwd(payload_cwd(value), os.getcwd()))

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
            ['git', *global_args, 'branch', '--show-current'],
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

def classify(command, cwd):
    cwd_stack = [cwd]
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
print(classify(command_from(d), declared_cwd(d)))
" 2>/dev/null <<< "$INPUT")

if [ -z "$PROTECTED_BRANCH" ]; then
  exit 0
fi

# exit 2일 때 Claude는 stderr만 모델에 전달한다. stdout은 무시된다.
echo "BLOCKED: 보호 브랜치 '$PROTECTED_BRANCH'에서 직접 커밋/푸시하지 마세요." >&2
echo "agent-flow worktree create --name feat-<slug> 로 작업 worktree를 만드세요." >&2
exit 2
