"""실행 관측 증거 — `record-command-run.py`가 남긴 기록을 읽는다.

`local_skills.read_skill_evidence`와 같은 모양이다. 주장자가 쓰는 마커와 달리
이 증거는 host tool 런타임이 만든다. 그래서 "안 돌렸다"는 주장자가 뒤집을 수
없다.

**이 증거가 증명하지 않는 것**을 먼저 적는다. hook은 argv와 exit code만 본다.
`pytest tests/test_x.py::test_trivial`도 exit 0이고, `assert False` 한 줄도
빨간 테스트다. 즉 관측이 확실히 잡는 것은 **"아예 안 돌렸다"** 하나뿐이며,
가짜 테스트는 관측으로 갈 수 없다. 이 층에 그 이상을 기대하면 안 된다.

hook이 없는 host에서는 로그 파일 자체가 없다. 그때는 `available=False`로
축퇴시키고 자기신고(`unavailable`)를 받는다 — L2와 같은 계약이다. 관측 불가를
위반으로 들면 hook 미지원 host에서 모든 런이 막힌다.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from agent_flow.core.markers import completion_gate_marker_values

COMMANDS_RUN_LOG = Path(".agent-flow") / "commands-run.jsonl"

# 재현 테스트를 요구하는 phase. `bugfix`에는 red phase 자체가 없어서
# "같은 버그 10번 재현"의 직접 원인이 여기였다.
TEST_EVIDENCE_PHASES = frozenset({"red", "implement-fix"})

TEST_RUN_EVIDENCE_MARKER = "test-run-evidence: verified|unavailable"

# profile에 test gate가 없을 때의 대비책(`generic`이 그렇다). 넓게 잡아도
# 잡으려는 것은 "아무 테스트도 안 돌렸다" 하나다.
FALLBACK_TEST_TOKENS = (
    "pytest",
    "unittest",
    "tox",
    "nox",
    "jest",
    "vitest",
    "mocha",
    "gradlew",
    "mvn",
    "cargo",
    "rspec",
    "phpunit",
    "ctest",
    "xcodebuild",
)


@dataclass(frozen=True)
class CommandRun:
    command: str
    exit_code: int | None
    at: float
    cwd: str = ""


@dataclass(frozen=True)
class CommandRunEvidence:
    available: bool
    runs: tuple[CommandRun, ...]

    def matching(self, *needles: str) -> tuple[CommandRun, ...]:
        patterns = [_needle_pattern(needle) for needle in needles if needle.strip()]
        if not patterns:
            return ()
        return tuple(
            run for run in self.runs if any(pattern.search(run.command) for pattern in patterns)
        )

    def ran(self, *needles: str) -> bool:
        return bool(self.matching(*needles))

    def failed(self, *needles: str) -> bool:
        """관측된 실패가 하나라도 있는가. exit code를 안 실어 보내는 host면 False다."""
        return any(run.exit_code not in (None, 0) for run in self.matching(*needles))

    def matching_all(self, tokens: Sequence[str]) -> tuple[CommandRun, ...]:
        """토큰 전부가 한 명령 안에 있는 실행. gate 명령을 통째로 대조할 때 쓴다."""
        patterns = [_needle_pattern(token) for token in tokens if token.strip()]
        if not patterns:
            return ()
        return tuple(
            run for run in self.runs if all(pattern.search(run.command) for pattern in patterns)
        )


def read_command_evidence(
    project_root: Path,
    *,
    since: float | None = None,
    cwd_root: Path | None = None,
) -> CommandRunEvidence:
    """관측 로그를 읽는다. 파일이 없으면 hook 미등록/미지원 host로 본다."""
    log_path = project_root / COMMANDS_RUN_LOG
    try:
        raw = log_path.read_text(encoding="utf-8")
    except OSError:
        return CommandRunEvidence(available=False, runs=())
    runs: list[CommandRun] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        command = entry.get("command")
        if not isinstance(command, str) or not command:
            continue
        at = entry.get("at")
        stamp = float(at) if isinstance(at, (int, float)) else 0.0
        if since is not None and stamp < since:
            continue
        cwd = entry.get("cwd")
        if cwd_root is not None and (
            not isinstance(cwd, str) or not _path_is_within(Path(cwd), cwd_root)
        ):
            continue
        code = entry.get("exit_code")
        runs.append(
            CommandRun(
                command=command,
                exit_code=code if isinstance(code, int) and not isinstance(code, bool) else None,
                at=stamp,
                cwd=cwd if isinstance(cwd, str) else "",
            )
        )
    return CommandRunEvidence(available=True, runs=tuple(runs))


def _needle_pattern(needle: str) -> re.Pattern[str]:
    """토큰 경계로 맞춘다. 부분 문자열로 맞추면 `mypytest`가 `pytest`로 통과한다."""
    return re.compile(rf"(?<![\w.-]){re.escape(needle.strip())}(?![\w.-])")


# 사용자만 돌려야 하는 명령. agent의 셸에서 관측되면 그 record는 사용자 확인이
# 아니다. 관측이 "안 돌렸다"만 잡는 다른 gate와 방향이 반대다 - 여기서는 로그에
# 남았다는 사실 자체가 위반의 증거다.
_SPEC_APPROVAL_CLI_TOKENS = (
    "agent-flow",
    "agent_flow.cli",
    "agent-flow-kit",
    "agent-flow-kit.mjs",
)
_SPEC_APPROVAL_SUBCOMMANDS = ("confirm", "approve", "prepare-confirmation")
_SPEC_USER_PROMPT_HOOKS = frozenset(
    {
        "confirm-spec-user-prompt.py",
        "prepare-spec-user-prompt.py",
    }
)
_SPEC_PROTECTED_STATE_TOKENS = (
    "spec-user-confirmation.json",
    "spec-user-confirmation.pending.",
    "spec-user-confirmation.lock",
    "spec-manual-approvals.json",
    "commands-run.jsonl",
)
_SPEC_PROTECTED_API_TOKENS = (
    "record_spec_set_confirmation",
    "prepare_user_spec_confirmation",
    "prepare_and_attest_user_spec_confirmation",
    "attest_user_spec_confirmation",
    "record_manual_spec_approval",
)

# `node <script>` 형태는 실행 파일이 인터프리터다. 실제로 무엇을 실행하는지는
# 첫 비플래그 인자에 있다.
_SCRIPT_RUNNERS = frozenset({"node", "bun"})
_SHELL_RUNNERS = frozenset({"bash", "dash", "sh", "zsh"})
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_SIMPLE_SHELL_ASSIGNMENT = re.compile(
    r"(?:^|[\n;&|])\s*([A-Za-z_][A-Za-z0-9_]*)="
    r"(?:'([^']*)'|\"([^\"]*)\"|([^\s;&|]+))"
)
_SHELL_VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")

# 승인 기록을 만들 수 없는 호출. 도움말은 인자만 읽고 끝나므로 승인 시도가 아니다.
# 이걸 위반으로 세면 명령 형태를 확인한 것만으로 그 런은 영영 못 푼다 — 증거 창이
# `since = started_at`이라 런 안에서는 되돌릴 방법이 없다.
_SPEC_APPROVAL_INERT_FLAGS = frozenset({"--help", "-h"})


def agent_run_spec_approvals(evidence: CommandRunEvidence) -> tuple[CommandRun, ...]:
    """agent 셸에서 관측된 사용자 전용 SPEC 승인 상태 진입."""
    if not evidence.available:
        return ()
    return tuple(
        run
        for run in evidence.runs
        if command_executes_agent_spec_approval(run.command)
    )


def command_executes_agent_spec_approval(command: str) -> bool:
    expanded = _expand_simple_shell_variables(command)
    if any(
        command_executes_agent_spec_approval(nested)
        for nested in _shell_command_substitutions(expanded)
    ):
        return True
    segments = _shell_segments(expanded)
    if segments is None:
        return False
    return any(_argv_executes_agent_spec_approval(argv) for argv in segments)


def _python_entrypoint(
    argv: Sequence[str],
) -> tuple[str, str, tuple[str, ...]] | None:
    index = 1
    while index < len(argv):
        argument = argv[index]
        lowered = argument.lower()
        if argument == "--":
            index += 1
            break
        if lowered in {"-c", "-m"}:
            if index + 1 >= len(argv):
                return None
            kind = "code" if lowered == "-c" else "module"
            return kind, argv[index + 1], tuple(argv[index + 2 :])
        if lowered.startswith("-c") and lowered != "-c":
            return "code", argument[2:], tuple(argv[index + 1 :])
        if lowered.startswith("-m") and lowered != "-m":
            return "module", argument[2:], tuple(argv[index + 1 :])
        if lowered in {"-w", "-x", "--check-hash-based-pycs"}:
            if index + 1 >= len(argv):
                return None
            index += 2
            continue
        if argument.startswith("-"):
            index += 1
            continue
        break
    if index >= len(argv):
        return None
    return "script", argv[index], tuple(argv[index + 1 :])


def _argv_executes_agent_spec_approval(argv: Sequence[str]) -> bool:
    if not argv:
        return False
    raw = tuple(argv)
    normalized = _strip_env_prefix(raw)
    if normalized != raw:
        return _argv_executes_agent_spec_approval(normalized)
    names = tuple(os.path.basename(part).lower() for part in raw)
    first = names[0]
    if first in {"command", "exec"}:
        nested = _unwrap_execution_wrapper(raw)
        return bool(nested) and _argv_executes_agent_spec_approval(nested)
    if first == "eval":
        arguments = raw[1:]
        if arguments[:1] == ("--",):
            arguments = arguments[1:]
        return bool(arguments) and command_executes_agent_spec_approval(" ".join(arguments))
    if first in _SHELL_RUNNERS:
        for index, part in enumerate(names[1:], start=1):
            if part.startswith("-") and part.endswith("c") and index + 1 < len(raw):
                return command_executes_agent_spec_approval(raw[index + 1])
        return False
    if first in {"uv", "poetry", "pipenv"} and len(raw) >= 3 and names[1] == "run":
        return _argv_executes_agent_spec_approval(raw[2:])
    if first == "bundle" and len(raw) >= 3 and names[1] == "exec":
        return _argv_executes_agent_spec_approval(raw[2:])
    if first in {"npx", "pnpx"} and len(raw) >= 2:
        return _argv_executes_agent_spec_approval(raw[1:])
    if _argv_touches_protected_state(raw):
        return True
    if _argv_executes_spec_user_prompt_hook(raw):
        return True
    if first.startswith("python"):
        if _python_argv_calls_protected_api(raw):
            return True
        entrypoint = _python_entrypoint(raw)
        if entrypoint is None:
            return False
        kind, target, arguments = entrypoint
        if kind not in {"module", "script"}:
            return False
        return _approval_cli_arguments(
            os.path.basename(target).lower(),
            tuple(os.path.basename(part).lower() for part in arguments),
        )
    if first in _SCRIPT_RUNNERS:
        for index, name in enumerate(names[1:], start=1):
            if name.startswith("-"):
                continue
            return _approval_cli_arguments(name, names[index + 1 :])
        return False
    return _approval_cli_arguments(first, names[1:])


def _python_argv_calls_protected_api(argv: Sequence[str]) -> bool:
    entrypoint = _python_entrypoint(argv)
    if entrypoint is None or entrypoint[0] != "code":
        return False
    code = entrypoint[1]
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    protected_names = set(_SPEC_PROTECTED_API_TOKENS)
    protected_aliases: set[str] = set()
    module_aliases: dict[str, str] = {}
    execution_aliases: set[str] = set()
    execution_calls = {
        "subprocess": {
            "call",
            "check_call",
            "check_output",
            "popen",
            "run",
        },
        "os": {
            "execl",
            "execle",
            "execlp",
            "execlpe",
            "execv",
            "execve",
            "execvp",
            "execvpe",
            "popen",
            "posix_spawn",
            "posix_spawnp",
            "spawnl",
            "spawnle",
            "spawnlp",
            "spawnlpe",
            "spawnv",
            "spawnve",
            "spawnvp",
            "spawnvpe",
            "system",
        },
        "pty": {"spawn"},
        "runpy": {"run_module", "run_path"},
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in execution_calls:
                    module_aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                local_name = alias.asname or alias.name
                if alias.name in protected_names:
                    protected_aliases.add(local_name)
                if alias.name in execution_calls.get(module, set()):
                    execution_aliases.add(local_name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if not isinstance(value, ast.Name) or value.id not in module_aliases:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    module_aliases[target.id] = module_aliases[value.id]
    if protected_aliases:
        return True
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            if node.func.id in protected_names | protected_aliases | execution_aliases:
                return True
            if node.func.id in {"eval", "exec", "compile", "__import__"}:
                return True
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in protected_names:
            return True
        owner = node.func.value
        if (
            isinstance(owner, ast.Name)
            and node.func.attr in execution_calls.get(module_aliases.get(owner.id, ""), set())
        ):
            return True
    return False


def _argv_touches_protected_state(argv: Sequence[str]) -> bool:
    protected_indexes = [
        index
        for index, part in enumerate(argv)
        if any(token in part for token in _SPEC_PROTECTED_STATE_TOKENS)
    ]
    if not protected_indexes:
        return False
    for index in protected_indexes:
        if index > 0 and set(argv[index - 1]) <= {"<", ">"}:
            return True
    executable = os.path.basename(argv[0]).lower()
    return executable not in {"echo", "printf"}


def _unwrap_execution_wrapper(argv: Sequence[str]) -> tuple[str, ...]:
    first = os.path.basename(argv[0]).lower()
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            index += 1
            break
        if not token.startswith("-") or token == "-":
            break
        if first == "command":
            if token in {"-v", "-V", "--help"}:
                return ()
            if token == "-p":
                index += 1
                continue
            return ()
        if token == "-a":
            index += 2
            continue
        if token in {"-c", "-l"}:
            index += 1
            continue
        return ()
    return tuple(argv[index:])


def _approval_cli_arguments(executable: str, arguments: Sequence[str]) -> bool:
    if executable not in _SPEC_APPROVAL_CLI_TOKENS:
        return False
    if any(flag in _SPEC_APPROVAL_INERT_FLAGS for flag in arguments):
        return False
    return any(
        arguments[index] == "spec"
        and arguments[index + 1] in _SPEC_APPROVAL_SUBCOMMANDS
        for index in range(len(arguments) - 1)
    )


def _expand_simple_shell_variables(command: str) -> str:
    values: dict[str, list[tuple[int, str]]] = {}
    for assignment in _SIMPLE_SHELL_ASSIGNMENT.finditer(command):
        value = next(
            candidate
            for candidate in assignment.groups()[1:]
            if candidate is not None
        )
        values.setdefault(assignment.group(1), []).append((assignment.end(), value))
    if not values:
        return command

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        for assignment_end, value in reversed(values.get(name, ())):
            if assignment_end <= match.start():
                return value
        return match.group(0)

    return _SHELL_VARIABLE.sub(replace, command)


def _shell_command_substitutions(command: str) -> tuple[str, ...]:
    """Extract executable command substitutions while respecting shell quotes."""
    substitutions: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        character = command[index]
        if character == "\\" and quote != "'":
            index += 2
            continue
        if character == "'" and quote != '"':
            quote = None if quote == "'" else "'"
            index += 1
            continue
        if character == '"' and quote != "'":
            quote = None if quote == '"' else '"'
            index += 1
            continue
        if quote != "'" and character == "`":
            end = _closing_backtick(command, index + 1)
            if end is None:
                break
            substitutions.append(command[index + 1 : end])
            index = end + 1
            continue
        if (
            quote != "'"
            and command.startswith("$(", index)
            and not command.startswith("$((", index)
        ):
            end = _closing_command_substitution(command, index + 2)
            if end is None:
                break
            substitutions.append(command[index + 2 : end])
            index = end + 1
            continue
        index += 1
    return tuple(substitutions)


def _closing_backtick(command: str, start: int) -> int | None:
    index = start
    while index < len(command):
        if command[index] == "\\":
            index += 2
            continue
        if command[index] == "`":
            return index
        index += 1
    return None


def _closing_command_substitution(command: str, start: int) -> int | None:
    depth = 1
    quote: str | None = None
    index = start
    while index < len(command):
        character = command[index]
        if character == "\\" and quote != "'":
            index += 2
            continue
        if character == "'" and quote != '"':
            quote = None if quote == "'" else "'"
        elif character == '"' and quote != "'":
            quote = None if quote == '"' else '"'
        elif quote is None and character == "(":
            depth += 1
        elif quote is None and character == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _shell_lines_without_heredocs(command: str) -> tuple[str, ...] | None:
    executable: list[str] = []
    pending_delimiters: list[str] = []
    for line in command.splitlines():
        if pending_delimiters:
            if line.strip() == pending_delimiters[0]:
                pending_delimiters.pop(0)
            continue
        executable.append(line)
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars="<>")
            lexer.whitespace_split = True
            tokens = tuple(lexer)
        except ValueError:
            return None
        for index, token in enumerate(tokens[:-1]):
            if token == "<<":
                delimiter = tokens[index + 1].removeprefix("-")
                if delimiter:
                    pending_delimiters.append(delimiter)
    return tuple(executable)


def _shell_segments(command: str) -> tuple[tuple[str, ...], ...] | None:
    """Return only top-level commands, excluding quoted data and heredoc bodies."""
    lines = _shell_lines_without_heredocs(command)
    if lines is None:
        return None
    segments: list[tuple[str, ...]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
            lexer.whitespace_split = True
            tokens = tuple(lexer)
        except ValueError:
            return None
        current: list[str] = []
        for token in tokens:
            if token == "!" or (token and set(token) <= set(";&|")):
                if current:
                    segments.append(_strip_env_prefix(tuple(current)))
                current = []
                continue
            current.append(token)
        if current:
            segments.append(_strip_env_prefix(tuple(current)))
    return tuple(segment for segment in segments if segment)


def command_is_unmasked(command: str) -> bool:
    return bool(_single_command_argv(command))


def is_concrete_test_selector(value: str) -> bool:
    selector = value.strip()
    return bool(
        len(selector) >= 3
        and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:/#\[\]-]*", selector)
    )


def is_test_command_execution(
    command: str,
    expected_tokens: Sequence[str],
    test_name: str,
) -> bool:
    argv = _single_command_argv(command)
    if not argv or not is_concrete_test_selector(test_name):
        return False
    lowered = tuple(part.lower() for part in argv)
    if any(
        flag in lowered
        for flag in (
            "--collect-only",
            "--list-tests",
            "--listtests",
            "--dry-run",
            "--help",
            "--version",
        )
    ):
        return False
    normalized = tuple(os.path.basename(part).lower() for part in argv)
    required = tuple(
        os.path.basename(str(token)).lower()
        for token in expected_tokens
        if str(token).strip() and not str(token).startswith("-")
    )
    if not required or not all(token in normalized for token in required):
        return False
    executable = _effective_executable(argv)
    allowed = {required[0], *FALLBACK_TEST_TOKENS, "npm", "pnpm", "yarn", "bun"}
    if executable not in allowed:
        return False
    return _command_selects_exact_test(argv, test_name)


def _executes_spec_user_prompt_hook(command: str) -> bool:
    segments = _shell_segments(command)
    if not segments:
        return False
    return any(_argv_executes_spec_user_prompt_hook(argv) for argv in segments)


def _argv_executes_spec_user_prompt_hook(argv: Sequence[str]) -> bool:
    if not argv:
        return False
    names = tuple(os.path.basename(part).lower() for part in argv)
    if names[0] in _SHELL_RUNNERS:
        for index, part in enumerate(names[1:], start=1):
            if part.startswith("-") and part.endswith("c") and index + 1 < len(argv):
                return _executes_spec_user_prompt_hook(argv[index + 1])
        return False
    if names[0] in {"uv", "poetry", "pipenv"} and len(names) >= 3 and names[1] == "run":
        names = names[2:]
    if not names:
        return False
    if names[0] in _SPEC_USER_PROMPT_HOOKS:
        return True
    if not names[0].startswith("python"):
        return False
    entrypoint = _python_entrypoint(argv)
    return (
        entrypoint is not None
        and entrypoint[0] == "script"
        and os.path.basename(entrypoint[1]).lower() in _SPEC_USER_PROMPT_HOOKS
    )


def _command_selects_exact_test(argv: Sequence[str], test_name: str) -> bool:
    escaped = re.escape(test_name)
    node_selector = re.compile(rf"[:/#.]{escaped}(?:$|[\[])")
    anchored_selector = re.compile(
        rf"^(?:--test-name-pattern|--testNamePattern|--filter)=\^{escaped}\$$"
    )
    return any(
        node_selector.search(part) or anchored_selector.fullmatch(part)
        for part in argv[1:]
    )


def _single_command_argv(command: str) -> tuple[str, ...]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        argv = tuple(lexer)
    except ValueError:
        return ()
    if not argv or any(
        token == "!" or (token and set(token) <= set(";&|<>()"))
        for token in argv
    ):
        return ()
    return _strip_env_prefix(argv)


def _strip_env_prefix(argv: tuple[str, ...]) -> tuple[str, ...]:
    """`env` 래퍼와 앞쪽 환경 변수 할당을 걷어낸다."""
    index = 0
    while index < len(argv) and _ENV_ASSIGNMENT.fullmatch(argv[index]):
        index += 1
    if index >= len(argv) or os.path.basename(argv[index]).lower() != "env":
        return argv[index:]

    index += 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            index += 1
            break
        if _ENV_ASSIGNMENT.fullmatch(token) or token in {"-i", "--ignore-environment"}:
            index += 1
            continue
        if token in {"-u", "--unset"}:
            index += 2
            continue
        if token.startswith("--unset=") or (
            token.startswith("-u") and token != "-u"
        ):
            index += 1
            continue
        break
    while index < len(argv) and _ENV_ASSIGNMENT.fullmatch(argv[index]):
        index += 1
    return argv[index:]


def _effective_executable(argv: Sequence[str]) -> str:
    if not argv:
        return ""
    names = tuple(os.path.basename(part).lower() for part in argv)
    first = names[0]
    if first.startswith("python") and len(names) >= 3 and names[1] == "-m":
        return names[2]
    if first in {"uv", "poetry", "pipenv"} and len(names) >= 3 and names[1] == "run":
        return names[2]
    if first == "bundle" and len(names) >= 3 and names[1] == "exec":
        return names[2]
    if first in {"npx", "pnpx"} and len(names) >= 2:
        return names[1]
    return first


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def resolve_test_command_tokens(profile: dict | None) -> tuple[tuple[str, ...], ...]:
    """profile의 test gate 명령을 토큰 집합으로 만든다.

    플래그(`-q`)는 뺀다. 같은 gate라도 agent가 옵션을 바꿔 돌리는 것은 정상이고,
    옵션까지 요구하면 관측이 문자열 일치 게임이 된다.
    """
    sets: list[tuple[str, ...]] = []
    for gate in (profile or {}).get("gates") or []:
        if not isinstance(gate, dict) or "test" not in str(gate.get("id", "")).lower():
            continue
        command = gate.get("command")
        if isinstance(command, str):
            command = command.split()
        if not isinstance(command, list):
            continue
        tokens = tuple(str(part) for part in command if not str(part).startswith("-"))
        if tokens:
            sets.append(tokens)
    if sets:
        return tuple(sets)
    return tuple((token,) for token in FALLBACK_TEST_TOKENS)


def missing_test_evidence_markers(
    project_root: Path,
    phase_id: str,
    text: str,
    *,
    profile: dict | None = None,
    since: float | None = None,
) -> list[str]:
    """"테스트를 아예 안 돌렸다"만 잡는다. 그 이상은 이 층이 증명하지 못한다.

    관측이 불가능한 host에서는 자기신고(`unavailable`)로 축퇴한다. 관측 불가를
    위반으로 들면 hook 미지원 host에서 red/fix phase가 통째로 막힌다.
    """
    if phase_id not in TEST_EVIDENCE_PHASES:
        return []
    evidence = read_command_evidence(project_root, since=since)
    values = completion_gate_marker_values(text)
    if not evidence.available:
        if values.get("test-run-evidence") not in {"verified", "unavailable"}:
            return [TEST_RUN_EVIDENCE_MARKER]
        return []
    token_sets = resolve_test_command_tokens(profile)
    observed = [run for tokens in token_sets for run in evidence.matching_all(tokens)]
    if not observed:
        return [
            "test-run-evidence: verified (no test command was observed during this "
            "phase; the regression test has to actually run)"
        ]
    reported = [run.exit_code for run in observed if run.exit_code is not None]
    if reported and all(code == 0 for code in reported):
        # `implement-fix`도 같은 요구를 받는다. 고친 뒤 한 번만 돌리면 초록이라
        # 통과하는데, 그러면 회귀 테스트가 그 버그를 정말 잡는지 아무도 안 봤다는
        # 뜻이다 - `red-observed`가 자유 서술이 된다. bugfix workflow의 prompt도
        # 수정 전 실패와 수정 후 통과를 같은 phase에서 요구한다.
        detail = (
            "a red phase has to leave a failing test behind"
            if phase_id == "red"
            else "run the regression test before the fix and watch it fail"
        )
        return [f"red-observed: <failing exit code> (every observed test command exited 0; {detail})"]
    return []
