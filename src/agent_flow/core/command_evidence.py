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
_SPEC_APPROVAL_CLI_TOKENS = ("agent-flow", "agent_flow.cli")
_SPEC_APPROVAL_SUBCOMMANDS = ("confirm", "approve")


def agent_run_spec_approvals(evidence: CommandRunEvidence) -> tuple[CommandRun, ...]:
    """agent 셸에서 관측된 `agent-flow spec confirm|approve` 실행."""
    if not evidence.available:
        return ()
    observed: list[CommandRun] = []
    for cli in _SPEC_APPROVAL_CLI_TOKENS:
        for subcommand in _SPEC_APPROVAL_SUBCOMMANDS:
            observed.extend(evidence.matching_all((cli, "spec", subcommand)))
    return tuple(dict.fromkeys(observed))


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
    index = 0
    if argv[0] == "env":
        index = 1
    while index < len(argv) and re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*=.*",
        argv[index],
    ):
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
