"""어떤 gate가 돌지 정하는 층. 실행은 `core/gates.py`가 한다.

계획과 실행을 갈라 두는 이유는 호출자가 둘이기 때문이다. `cli.py`의 `gates`
서브커맨드(사람이 손으로 돌리는 자리)와 `runner.py`의 `gates` phase(run이 스스로
돌리는 자리)가 같은 표를 봐야 한다. 이 함수들이 `cli.py`에 있으면 runner가 쓸 수
없다 — `cli.py`가 `runner`를 import하므로(`cli.py:169`) 반대 방향 import는 순환이다.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from agent_flow.core.gates import GateCommand
from agent_flow.core.profiles import DEFAULT_GATE_PHASE, GATE_PHASE_ALL, load_profile


def profile_gate_commands(
    profile_ids: list[str],
    *,
    root: Path | None = None,
    phase: str = DEFAULT_GATE_PHASE,
) -> list[GateCommand]:
    commands: list[tuple[int, GateCommand]] = []
    seen: set[tuple[str, ...]] = set()
    multi_profile = len(profile_ids) > 1
    architecture_lint_added = False
    architecture_lint_profile = ",".join(profile_ids)
    order = 0
    for profile_id in profile_ids:
        profile = load_profile(profile_id, root)
        for gate in profile.gates:
            if phase != GATE_PHASE_ALL and gate.phase != phase:
                continue
            command = _normalize_profile_gate_command(
                profile.profile_id,
                gate.gate_id,
                gate.command,
                profile_root=root,
            )
            required = gate.required
            timeout_s = gate.timeout_s
            gate_id = f"{profile.profile_id}:{gate.gate_id}" if multi_profile else gate.gate_id
            if multi_profile and is_architecture_lint_gate(gate.gate_id, gate.command):
                if architecture_lint_added:
                    continue
                command = architecture_lint_command(
                    architecture_lint_profile,
                    profile_root=root,
                )
                gate_id = "architecture-lint"
                required = True
                # 합쳐진 명령은 union 전체를 한 번에 도는 **다른 명령**이다. 먼저
                # 순회된 profile이 선언한 상한을 그대로 들고 가면 상한이 profile
                # 나열 순서에 달라진다.
                timeout_s = None
                architecture_lint_added = True
            if command in seen:
                continue
            seen.add(command)
            commands.append(
                (order, GateCommand(gate_id, command, required=required, timeout_s=timeout_s))
            )
            order += 1
    return [
        command
        for _, command in sorted(
            commands,
            key=lambda item: (*gate_order_key(item[1]), item[0]),
        )
    ]


def is_architecture_lint_gate(gate_id: str, command: tuple[str, ...]) -> bool:
    return gate_id == "architecture-lint" or "architecture-lint" in command


def _normalize_profile_gate_command(
    profile_id: str,
    gate_id: str,
    command: tuple[str, ...],
    *,
    profile_root: Path | None = None,
) -> tuple[str, ...]:
    if is_architecture_lint_gate(gate_id, command):
        profile_index = command.index("--profile") + 1 if "--profile" in command else -1
        if profile_index > 0 and profile_index < len(command):
            return architecture_lint_command(
                command[profile_index],
                profile_root=profile_root,
            )
    if profile_id == "python" and command:
        if command[0] in {"mypy", "pytest", "ruff"}:
            return (sys.executable, "-m", command[0], *command[1:])
    return command


def architecture_lint_command(
    profile_ids: str,
    *,
    profile_root: Path | None = None,
) -> tuple[str, ...]:
    command = (
        sys.executable,
        "-m",
        "agent_flow.core.architecture_lint",
        "--profile",
        profile_ids,
    )
    if profile_root is None:
        return command
    return (*command, "--profile-root", str(profile_root))


# gate 종류는 **선언된 gate id**에서만 읽는다. 명령 문자열에는 인터프리터와
# 프로젝트의 절대 경로가 섞이고, 그 경로가 이 어휘와 부딪히면 순서가 실행 환경마다
# 달라진다. 실측: `uv run --with pytest`가 만든 인터프리터
# `/Users/…/.cache/uv/builds-v0/.tmp…/bin/python`의 "builds"가 python profile의 gate
# 셋을 모두 build 칸으로 옮겨 `type → architecture-lint → lint`가
# `architecture-lint → lint → type`으로 뒤집혔다 — BUILD → TYPECHECK → LINT 계약이
# 인터프리터 경로 하나로 깨진 것이다. id는 profile이 선언하는 값이라 그런 오염이 없다.
#
# 어휘에 없는 id(예: gradle build를 도는 `verify`)는 마지막 칸으로 간다. 답이
# 안정적이고 선언으로 고칠 수 있다 — 명령 문자열을 추측하는 쪽은 둘 다 아니다.
_GATE_KIND_VOCABULARY: tuple[tuple[int, frozenset[str]], ...] = (
    (0, frozenset({"build", "assemble", "xcodebuild"})),
    (1, frozenset({"typecheck", "type", "types", "tsc", "mypy", "pyright"})),
    (2, frozenset({"lint", "ruff", "detekt", "ktlint", "swiftlint"})),
    (3, frozenset({"test", "tests", "pytest"})),
)


def _gate_id_words(gate_id: str) -> frozenset[str]:
    """`android:ios-build` → {android, ios, build}. 부분 문자열이 아니라 낱말로 본다."""
    return frozenset(word for word in re.split(r"[^a-z0-9]+", gate_id.lower()) if word)


def gate_order_key(gate: GateCommand) -> tuple[int, int, str]:
    words = _gate_id_words(gate.gate_id)
    for rank, vocabulary in _GATE_KIND_VOCABULARY:
        if words & vocabulary:
            return (rank, _profile_gate_kind_tiebreaker(gate.gate_id), gate.gate_id)
    return (4, _profile_gate_kind_tiebreaker(gate.gate_id), gate.gate_id)


def _profile_gate_kind_tiebreaker(gate_id: str) -> int:
    """같은 칸 안의 순서. architecture 계약이 스택 lint보다 먼저 답을 준다."""
    words = _gate_id_words(gate_id)
    if "architecture" in words:
        return 0
    if "context" in words:
        return 1
    return 2
