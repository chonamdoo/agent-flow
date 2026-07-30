#!/usr/bin/env python3
"""셸 명령 실행을 관측해 append-only 로그에 남긴다.

이 hook은 **아무것도 차단하지 않는다.** 항상 exit 0이다. 유일한 역할은
"이 phase에서 그 명령을 실제로 돌렸는가"에 대한 기계적 증거를 남기는 것이고,
판정은 runner의 completion gate가 한다. `record-skill-read.py`와 같은 계약이다.

**반드시 PostToolUse에 단다.** PreToolUse는 host가 허용/차단 판정을 기대하는
자리라, 관측용 스크립트를 거기 달면 exit code나 출력이 곧 판정이 되어 사용자
도구를 막는다. 스크립트가 없거나 죽으면 셸이 통째로 막히는 사고가 실제로 있었다.

관측이 잡는 것은 **"아예 안 돌렸다"**뿐이다. argv와 exit code만 보므로 "돌렸다"가
"그게 옳은 걸 검증했다"를 뜻하지는 않는다. 그 한계는 소비 쪽에 적어 둔다.

로그는 한 줄 append다. O_APPEND 쓰기라 병렬 agent가 서로의 기록을 덮지 않는다.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# 셸 실행 tool 이름은 host마다 다르다. comment-checker의 write matcher와 같은 방식으로 합집합을 쓴다.
COMMAND_TOOLS = (
    "bash",
    "shell",
    "run_terminal_cmd",
    "execute_command",
    "local_shell",
    "terminal",
)
LOG_RELATIVE = Path(".agent-flow") / "commands-run.jsonl"
_COMMAND_KEYS = ("command", "cmd", "script", "shell_command")
_EXIT_CODE_KEYS = ("exit_code", "exitCode", "returncode", "return_code")
# git 하위프로세스를 띄울 때 벗겨야 하는 ambient discovery 변수. 실측으로
# `GIT_COMMON_DIR=<decoy>/.git` 하나만 새어 들어와도 rev-parse가 decoy를 반환하고,
# 그러면 hook이 남의 저장소에 증거를 쓴다. core의 LEAKY_GIT_ENV_VARS와 같은 목록이다.
LEAKY_GIT_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_CEILING_DIRECTORIES",
)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    tool = str(find_first(payload, ("tool_name", "tool")) or "").lower()
    if tool and tool not in COMMAND_TOOLS:
        return 0

    tool_input = find_first(payload, ("tool_input", "input", "parameters"))
    if not isinstance(tool_input, dict):
        return 0
    command = first_string(tool_input, _COMMAND_KEYS)
    if not command:
        return 0

    cwd = str(find_first(payload, ("cwd", "workspace", "project_root")) or os.getcwd())
    project_root = find_project_root(Path(cwd))
    if project_root is None:
        return 0

    append_entry(project_root / LOG_RELATIVE, command, exit_code(payload), cwd)
    return 0


def exit_code(payload: object) -> int | None:
    """host가 결과를 실어 보낼 때만 있다. 없는 것과 0은 다르므로 섞지 않는다."""
    value = find_first(payload, _EXIT_CODE_KEYS)
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def append_entry(log_path: Path, command: str, code: int | None, cwd: str) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"command": command, "exit_code": code, "cwd": cwd, "at": time.time()},
            ensure_ascii=False,
            sort_keys=True,
        )
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        # 관측 실패가 작업을 막아서는 안 된다. 증거가 없으면 gate가 unavailable로 처리한다.
        return


def git_leader_checkout(start: Path) -> Path | None:
    """`start`가 속한 저장소의 leader checkout. git이 없거나 저장소가 아니면 None이다."""
    env = {name: value for name, value in os.environ.items() if name not in LEAKY_GIT_ENV_VARS}
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(start),
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        # hook은 관측 전용이다. git 부재·timeout은 예상 상황이라 증거 하나를 포기하고 넘어간다.
        return None
    if result.returncode != 0:
        return None
    common = Path(result.stdout.strip() or ".")
    if not common.is_absolute():
        # leader 자신에서는 `.git`처럼 상대경로가 나온다. 기준은 실행 cwd인 `start`다.
        common = start / common
    if common.name != ".git":
        return None
    return common.parent


def find_project_root(start: Path) -> Path | None:
    # leader-first. `.agent-flow/worktrees/` 밖의 linked worktree(Orca workspace 등)에는
    # `.agent-flow`가 아예 없어서 조상 탐색만으로는 증거가 통째로 사라진다. 반대로 조상
    # 탐색을 먼저 하면 `$HOME/.agent-flow/kit.json`이 실제 leader를 가려버린다.
    leader = git_leader_checkout(start)
    if leader is not None and (leader / ".agent-flow" / "kit.json").is_file():
        return leader
    for candidate in [start, *start.parents]:
        if (candidate / ".agent-flow" / "kit.json").is_file():
            return candidate
    return None


def find_first(payload: object, keys: tuple[str, ...]) -> object:
    if isinstance(payload, dict):
        for key in keys:
            if key in payload:
                return payload[key]
        for value in payload.values():
            found = find_first(value, keys)
            if found is not None:
                return found
    return None


def first_string(mapping: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
