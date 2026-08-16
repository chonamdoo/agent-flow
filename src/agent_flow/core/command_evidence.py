"""실행 관측 증거 — `record-command-run.py`가 남긴 기록을 읽는다.

`local_skills.read_skill_evidence`와 같은 모양이다. 주장자가 쓰는 마커와 달리
이 증거는 host tool 런타임이 만든다. 그래서 "안 돌렸다"는 주장자가 뒤집을 수
없다.

**이 증거가 증명하지 않는 것**을 먼저 적는다. hook은 argv와 exit code만 본다.
`pytest tests/test_x.py::test_trivial`도 exit 0이고, `assert False` 한 줄도
빨간 테스트다. 즉 관측이 확실히 잡는 것은 **"아예 안 돌렸다"** 하나뿐이며,
가짜 테스트는 관측으로 갈 수 없다. 이 층에 그 이상을 기대하면 안 된다.

hook이 없는 host에서는 로그 파일 자체가 없다. 그때는 `available=False`로
축퇴시키고 자기신고(`unavailable`)를 받는다 — 관측 불가를 위반으로 들면 hook
미지원 host에서 모든 런이 막힌다.

**skill 쪽 L2와 같은 계약이 아니다.** 관측이 가능한 host에서 이쪽은 관측으로
막는다(`missing_test_evidence_markers`: 실행이 하나도 안 잡히면 자기신고가
무엇이든 차단이다). skill 쪽 L2(`local_skills.missing_local_skill_markers`)는
자기신고 하나만 요구하고 관측은 진단에만 쓴다. 갈라진 이유는 생산자다 —
`record-command-run.py`는 argv를 실행 시점에 잡으므로 hook이 로드된 세션이면
빠짐이 없지만, skill 읽음 기록은 hook이 없는 세션에서 실제 읽기를 놓치면서도
다른 세션이 만든 로그 파일 때문에 `available=True`가 되어 그 세션을 영원히
막았다. 두 층을 같은 문장으로 요약하지 마라.
"""
from __future__ import annotations

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
#
# `default`는 red/implement-fix 어느 쪽도 갖지 않는다. 그래서 "모든 프로젝트에
# 적용된다"고 스스로 적어 둔 기본 워크플로만 테스트 실행 증거를 한 번도 요구하지
# 않는 상태였다 — bugfix에 있던 구멍과 같은 것이 기본 경로에 그대로 있었다.
# 구현이 일어나는 `implement`와 고침이 일어나는 `fix-loop`가 그 자리다.
TEST_EVIDENCE_PHASES = frozenset({"red", "implement-fix", "implement", "fix-loop"})

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
        # `errors="replace"`가 없으면 손상 바이트 하나가 UnicodeDecodeError를 낸다.
        # 그건 ValueError라 아래 `except OSError`를 그냥 통과해 `agent-flow status`를
        # 죽인다. 이 로그는 hook이 append-only로 쓰는 것이라 잘린 줄이 언제든 섞인다.
        raw = log_path.read_text(encoding="utf-8", errors="replace")
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


_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")


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
    cwd_root: Path | None = None,
) -> list[str]:
    """"테스트를 아예 안 돌렸다"만 잡는다. 그 이상은 이 층이 증명하지 못한다.

    관측이 불가능한 host에서는 자기신고(`unavailable`)로 축퇴한다. 관측 불가를
    위반으로 들면 hook 미지원 host에서 red/fix phase가 통째로 막힌다.
    """
    if phase_id not in TEST_EVIDENCE_PHASES:
        return []
    evidence = read_command_evidence(project_root, since=since, cwd_root=cwd_root)
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
