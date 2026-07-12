#!/bin/bash
# agent-flow PreToolUse hook: main/master/develop 브랜치에서 커밋·푸시 차단
TRUSTED_PYTHON=/usr/bin/python3
if [ ! -f "$TRUSTED_PYTHON" ] || [ ! -x "$TRUSTED_PYTHON" ]; then
  echo "BLOCKED: trusted Python interpreter is unavailable or unsafe." >&2
  exit 2
fi
INPUT=$(/bin/cat)
if ! PROTECTED_BRANCH=$("$TRUSTED_PYTHON" -c "
import sys, json
import fnmatch
import os
import re
import shlex
import subprocess

SHARED_REF_MUTATORS = {
    'fast-import', 'filter-branch', 'filter-repo', 'receive-pack',
    'symbolic-ref', 'update-ref',
}
STANDALONE_SHARED_REF_MUTATORS = {
    'git-fast-import', 'git-filter-branch', 'git-receive-pack',
    'git-send-pack', 'git-symbolic-ref', 'git-update-ref',
}

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

def static_process_wrapper_command(wrapper, args):
    wrappers = {'ionice', 'nice', 'nohup', 'setsid', 'stdbuf', 'time', 'timeout'}
    if wrapper not in wrappers:
        return None
    index = 0
    options_with_values = set()
    if wrapper == 'nice':
        options_with_values = {'--adjustment', '-n'}
    elif wrapper == 'timeout':
        options_with_values = {'--kill-after', '--signal', '-k', '-s'}
    elif wrapper == 'time':
        options_with_values = {'--format', '--output', '-f', '-o'}
    elif wrapper == 'stdbuf':
        options_with_values = {'--error', '--input', '--output', '-e', '-i', '-o'}
    elif wrapper == 'ionice':
        options_with_values = {
            '--class', '--classdata', '--pgid', '--pid', '--uid',
            '-c', '-n', '-P', '-p', '-u',
        }
    while index < len(args):
        value = args[index]
        if value == '--':
            index += 1
            break
        if value in options_with_values:
            if index + 1 >= len(args):
                return []
            index += 2
            continue
        if value.startswith('--') and '=' in value:
            index += 1
            continue
        if value.startswith('-') and value != '-':
            index += 1
            continue
        break
    if wrapper == 'timeout':
        if index >= len(args):
            return []
        index += 1
    return args[index:]

def unwrap_command(tokens):
    remaining = list(tokens)
    while remaining:
        while remaining and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', remaining[0]):
            remaining = remaining[1:]
        if not remaining:
            return remaining, ''
        wrapper = os.path.basename(remaining[0])
        wrapped = static_process_wrapper_command(wrapper, remaining[1:])
        if wrapped is not None:
            if not wrapped:
                return [], 'dynamic process wrapper'
            remaining = wrapped
            continue
        if wrapper in ('command', 'builtin'):
            index = 1
            while index < len(remaining):
                token = remaining[index]
                if token in ('-v', '-V'):
                    return [], ''
                if token == '-p':
                    index += 1
                    continue
                if token == '--':
                    index += 1
                    break
                if token.startswith('-'):
                    return [], 'dynamic command wrapper'
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
                        return [], 'dynamic environment wrapper'
                    index += 2
                    continue
                if token.startswith('--unset='):
                    index += 1
                    continue
                if token == '--':
                    index += 1
                    break
                if token.startswith('-'):
                    return [], 'dynamic environment wrapper'
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
                    and token != '-'
                    and set(token[1:]) <= {'c', 'l'}
                ):
                    index += 1
                    continue
                if token == '-a':
                    if index + 1 >= len(remaining):
                        return [], 'dynamic exec wrapper'
                    index += 2
                    continue
                if token.startswith('-'):
                    return [], 'dynamic exec wrapper'
                break
            remaining = remaining[index:]
            continue
        return remaining, ''
    return remaining, ''

def git_parts(tokens):
    if not tokens:
        return [], []
    executable = os.path.basename(tokens[0])
    if executable == 'git-branch':
        return ['branch', *tokens[1:]], []
    if executable != 'git':
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
        lexer = shlex.shlex(command.replace('\r', '\n').replace('\n', ';'), posix=True, punctuation_chars=';&|()')
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
    unsafe_config_environment = {
        'GIT_CONFIG_COUNT', 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM',
        'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_SYSTEM', 'GIT_EXEC_PATH',
        'HOME', 'PATH', 'XDG_CONFIG_HOME',
    }
    for token in tokens:
        match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=', token)
        if match and (
            match.group(1) in unsafe_config_environment
            or re.fullmatch(r'GIT_CONFIG_(?:KEY|VALUE)_\d+', match.group(1))
        ):
            return 'git config source override', cwd
    tokens, wrapper_error = unwrap_command(tokens)
    if wrapper_error:
        return wrapper_error, cwd
    if not tokens:
        return '', cwd
    command = os.path.basename(tokens[0])
    if command in {
        '!', '{', '}', 'case', 'do', 'done', 'elif', 'else', 'esac', 'fi',
        'for', 'function', 'if', 'select', 'then', 'until', 'while',
    }:
        return 'dynamic shell control', cwd
    if command in {'doas', 'parallel', 'sudo', 'xargs'}:
        return 'dynamic process executor', cwd
    if command == 'find' and any(
        value in {'-delete', '-exec', '-execdir', '-ok', '-okdir'}
        for value in tokens[1:]
    ):
        return 'dynamic find action', cwd
    if command == 'eval':
        return 'dynamic shell evaluation', cwd
    if command in ('.', 'source'):
        return 'dynamic shell source', cwd
    if command in STANDALONE_SHARED_REF_MUTATORS:
        return 'shared git ref state', cwd
    if command == 'cd':
        target = tokens[1] if len(tokens) > 1 else os.path.expanduser('~')
        next_cwd = os.path.abspath(os.path.join(cwd, target)) if not os.path.isabs(target) else target
        return '', next_cwd if os.path.isdir(next_cwd) else cwd
    nested = nested_shell_command(tokens)
    if nested:
        return classify(nested, cwd), cwd
    if command in ('bash', 'sh', 'zsh'):
        script = next((value for value in tokens[1:] if not value.startswith('-')), '')
        if script in ('', '-', '/dev/stdin', '/proc/self/fd/0'):
            return 'dynamic shell input', cwd
    args, global_args = git_parts(tokens)
    if any(
        value == '--exec-path' or value.startswith('--exec-path=')
        for value in global_args
    ):
        return 'git executable source override', cwd
    if args:
        aliases, aliases_valid = config_values(global_args, cwd, f'alias.{args[0]}')
        if not aliases_valid or aliases:
            return 'git alias execution', cwd
    if args and args[0] in SHARED_REF_MUTATORS:
        return 'shared git ref state', cwd
    if args and args[0] == 'commit':
        branch = current_branch(global_args, cwd)
        if branch in ('main', 'master', 'develop'):
            return branch, cwd
    if args and args[0] == 'branch':
        protected_target = protected_branch_mutation_target(args[1:])
        if protected_target:
            return protected_target, cwd
    if args and args[0] == 'push':
        branch = current_branch(global_args, cwd)
        if branch in ('main', 'master', 'develop'):
            return branch, cwd
        protected_target = protected_push_target(args[1:], global_args, cwd, branch)
        if protected_target:
            return protected_target, cwd
    if args and args[0] == 'send-pack':
        branch = current_branch(global_args, cwd)
        if branch in ('main', 'master', 'develop'):
            return branch, cwd
        protected_target = protected_send_pack_target(args[1:])
        if protected_target:
            return protected_target, cwd
    if args and args[0] in ('fetch', 'pull'):
        branch = current_branch(global_args, cwd)
        if args[0] == 'pull' and branch in ('main', 'master', 'develop'):
            return branch, cwd
        protected_target = protected_fetch_target(args[1:], global_args, cwd, branch)
        if protected_target:
            return protected_target, cwd
    if args and args[0] == 'config' and not config_args_read_only(args[1:]):
        return 'shared git config', cwd
    if args and args[0] == 'remote' and not remote_args_read_only(args[1:]):
        return 'shared git remote state', cwd
    if args and args[0] == 'submodule' and not submodule_args_read_only(args[1:]):
        return 'shared git submodule state', cwd
    if args and args[0] == 'worktree' and not (
        len(args) > 1 and args[1] == 'list'
    ):
        return 'shared git worktree state', cwd
    return '', cwd

def config_values(global_args, cwd, key):
    try:
        result = subprocess.run(
            ['/usr/bin/git', *global_args, 'config', '--get-all', key],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [], False
    if result.returncode == 1:
        return [], True
    if result.returncode != 0:
        return [], False
    return result.stdout.splitlines(), True

def config_args_read_only(args):
    mutating = {
        '--add', '--edit', '--remove-section', '--rename-section', '--replace-all',
        '--unset', '--unset-all', '-e',
    }
    if any(value in mutating for value in args):
        return False
    queries = {'--get', '--get-all', '--get-regexp', '--get-urlmatch', '--list', '-l'}
    if any(value in queries for value in args):
        return True
    operands = [value for value in args if value != '--' and not value.startswith('-')]
    if operands and operands[0] in ('get', 'get-all', 'get-regexp', 'get-urlmatch', 'list'):
        return True
    return len(operands) == 1

def protected_branch_mutation_target(args):
    delete = any(
        value in ('--delete', '-d', '-D')
        or (
            value.startswith('-')
            and not value.startswith('--')
            and any(flag in value[1:] for flag in ('d', 'D'))
        )
        for value in args
    )
    force = any(
        value in ('--force', '-f')
        or (
            value.startswith('-')
            and not value.startswith('--')
            and 'f' in value[1:]
        )
        for value in args
    )
    if not delete and not force:
        return ''
    operands = [
        value.removeprefix('refs/heads/').rstrip('/')
        for value in args
        if value != '--' and not value.startswith('-')
    ]
    targets = operands if delete else operands[:1]
    if not targets:
        return 'unverified protected branch mutation'
    for target in targets:
        for branch in ('main', 'master', 'develop'):
            if fnmatch.fnmatchcase(branch, target):
                return branch
    return ''

def remote_args_read_only(args):
    operands = [value for value in args if value != '--' and not value.startswith('-')]
    return not operands or operands[0] in ('get-url', 'show')

def submodule_args_read_only(args):
    operands = [value for value in args if value != '--' and not value.startswith('-')]
    return not operands or operands[0] in ('status', 'summary')

def push_operands(args):
    operands = []
    repository = None
    options_with_value = {'--exec', '--push-option', '--receive-pack', '--repo', '-o'}
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == '--':
            operands.extend(args[index + 1:])
            break
        if arg in options_with_value:
            if index + 1 >= len(args):
                return None, [], False
            if arg == '--repo':
                repository = args[index + 1]
            index += 2
            continue
        if arg.startswith('--repo='):
            repository = arg.split('=', 1)[1]
            index += 1
            continue
        if arg.startswith('-'):
            index += 1
            continue
        operands.append(arg)
        index += 1
    if repository is not None:
        return repository, operands, bool(repository)
    if not operands:
        return None, [], True
    return operands[0], operands[1:], True

def refspec_protected_target(refspec, delete=False):
    protected = {'main', 'master', 'develop'}
    if refspec.startswith('^'):
        return ''
    normalized = refspec.lstrip('+')
    if normalized == ':':
        return 'all protected branches'
    source, separator, target = normalized.partition(':')
    target = target if separator else source
    target = target.removeprefix('refs/heads/').rstrip('/')
    source = source.removeprefix('refs/heads/').rstrip('/')
    if separator and not target:
        for branch in protected:
            if fnmatch.fnmatchcase(branch, source):
                return branch
    for branch in protected:
        if fnmatch.fnmatchcase(branch, target):
            return branch
    return ''

def fetch_refspec_protected_target(refspec):
    normalized = refspec.lstrip('+')
    if normalized.startswith('^'):
        return ''
    _, separator, target = normalized.partition(':')
    if not separator or not target:
        return ''
    target = target.removeprefix('refs/heads/').rstrip('/')
    for branch in ('main', 'master', 'develop'):
        if fnmatch.fnmatchcase(branch, target):
            return branch
    return ''

def protected_send_pack_target(args):
    if any(arg in ('--all', '--mirror', '--stdin') for arg in args):
        return 'unverified send-pack target'
    _, refspecs, valid = push_operands(args)
    if not valid or not refspecs:
        return 'unverified send-pack target'
    for refspec in refspecs:
        target = refspec_protected_target(refspec)
        if target:
            return target
    return ''

def fetch_operands(args):
    options_with_value = {
        '--cleanup', '--deepen', '--depth', '--filter', '--jobs',
        '--negotiation-tip', '--refmap', '--server-option', '--shallow-exclude',
        '--shallow-since', '--strategy', '--strategy-option', '--upload-pack',
        '-j', '-o',
    }
    operands = []
    refmaps = []
    index = 0
    while index < len(args):
        value = args[index]
        if value == '--':
            operands.extend(args[index + 1:])
            break
        if value in options_with_value:
            if index + 1 >= len(args):
                return None, [], [], False
            if value == '--refmap':
                refmaps.append(args[index + 1])
            index += 2
            continue
        if value.startswith('--refmap='):
            refmaps.append(value.split('=', 1)[1])
            index += 1
            continue
        if value.startswith('-'):
            index += 1
            continue
        operands.append(value)
        index += 1
    if not operands:
        return None, [], refmaps, True
    return operands[0], operands[1:], refmaps, True

def configured_fetch_remote(global_args, cwd, branch):
    if branch:
        values, valid = config_values(global_args, cwd, f'branch.{branch}.remote')
        if not valid:
            return None, False
        if values and values[-1]:
            return values[-1], True
    return 'origin', True

def protected_fetch_target(args, global_args, cwd, branch):
    if any(arg in ('--all', '--multiple', '--stdin') for arg in args):
        return 'unverified fetch target'
    repository, refspecs, refmaps, valid = fetch_operands(args)
    if not valid:
        return 'unverified fetch target'
    for refspec in [*refspecs, *refmaps]:
        target = fetch_refspec_protected_target(refspec)
        if target:
            return target
    if refspecs:
        return ''
    if repository is None:
        repository, valid = configured_fetch_remote(global_args, cwd, branch)
        if not valid:
            return 'unverified fetch target'
    if not re.fullmatch(r'[A-Za-z0-9._/-]+', repository):
        return ''
    configured, configured_valid = config_values(
        global_args,
        cwd,
        f'remote.{repository}.fetch',
    )
    if not configured_valid:
        return 'unverified fetch target'
    for refspec in configured:
        target = fetch_refspec_protected_target(refspec)
        if target:
            return target
    return ''

def configured_push_remote(global_args, cwd, branch):
    for key in (
        f'branch.{branch}.pushRemote',
        'remote.pushDefault',
        f'branch.{branch}.remote',
    ):
        values, valid = config_values(global_args, cwd, key)
        if not valid:
            return None, False
        if values and values[-1]:
            return values[-1], True
    return 'origin', True

def protected_push_target(args, global_args, cwd, branch):
    if any(arg in ('--all', '--mirror') for arg in args):
        return 'all protected branches'
    delete = '--delete' in args or '-d' in args
    repository, refspecs, valid = push_operands(args)
    if not valid:
        return 'unverified push target'
    for refspec in refspecs:
        target = refspec_protected_target(refspec, delete)
        if target:
            return target
    if refspecs:
        return ''
    if not branch:
        return 'unverified push target'
    if repository is None:
        repository, valid = configured_push_remote(global_args, cwd, branch)
        if not valid:
            return 'unverified push target'
    if re.fullmatch(r'[A-Za-z0-9._/-]+', repository):
        mirror, mirror_valid = config_values(global_args, cwd, f'remote.{repository}.mirror')
        configured, configured_valid = config_values(global_args, cwd, f'remote.{repository}.push')
        if not mirror_valid or not configured_valid:
            return 'unverified push target'
        if any(value.lower() == 'true' for value in mirror):
            return 'all protected branches'
        if configured:
            for refspec in configured:
                target = refspec_protected_target(refspec)
                if target:
                    return target
            return ''
    defaults, defaults_valid = config_values(global_args, cwd, 'push.default')
    if not defaults_valid:
        return 'unverified push target'
    push_default = defaults[-1].lower() if defaults else 'simple'
    if push_default == 'matching':
        return 'all protected branches'
    if push_default in ('tracking', 'upstream'):
        upstream, upstream_valid = config_values(global_args, cwd, f'branch.{branch}.merge')
        if not upstream_valid:
            return 'unverified push target'
        for refspec in upstream:
            target = refspec_protected_target(refspec)
            if target:
                return target
    return ''

def nested_shell_command(tokens):
    if not tokens or os.path.basename(tokens[0]) not in ('bash', 'sh', 'zsh'):
        return ''
    for index, token in enumerate(tokens[1:], start=1):
        if token == '-c' or (
            token.startswith('-') and not token.startswith('--') and 'c' in token[1:]
        ):
            return tokens[index + 1] if index + 1 < len(tokens) else ''
    return ''

def classify(command, start_cwd=None):
    cwd_stack = [start_cwd or os.getcwd()]
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
print(classify(command_from(d)))
" 2>/dev/null <<< "$INPUT"); then
  echo "BLOCKED: protected-branch hook input could not be validated." >&2
  exit 2
fi

if [ -z "$PROTECTED_BRANCH" ]; then
  exit 0
fi

# exit 2일 때 Claude는 stderr만 모델에 전달한다. stdout은 무시된다.
echo "BLOCKED: 보호 브랜치 '$PROTECTED_BRANCH'에서 직접 커밋/푸시하지 마세요." >&2
echo "./.agent-flow/bin/agent-flow run \"<task>\"으로 profile 기준 worktree를 만든 뒤 커밋/푸시하세요." >&2
exit 2
