#!/usr/bin/env python3
"""SKILL.md 읽기를 관측해 append-only 로그에 남긴다.

이 hook은 **아무것도 차단하지 않는다.** 항상 exit 0이다. 유일한 역할은
"agent가 이 skill을 실제로 열어봤는가"에 대한 기계적 증거를 남기는 것이고,
판정은 runner의 completion gate가 한다.

로그는 한 줄 append다. O_APPEND 쓰기라 병렬 agent가 서로의 기록을 덮지 않는다.
`meta.json` read-modify-write를 쓰지 않는 이유가 이것이다.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

READ_TOOL_RE = ("read", "read_file", "view", "cat")
# Skill tool은 파일을 읽지 않는다. Claude Code 문서가 "does not re-read the skill file
# on later turns"라고 명시하고, 실측 transcript에서도 Skill tool 세션의 SKILL.md Read는
# 0건이었다. 경로만 관측하면 정상 사용이 미사용으로 판정된다.
SKILL_TOOLS = ("skill",)
# Codex에는 Read tool이 없다. skill을 읽을 때 셸로 파일을 연다.
SHELL_TOOLS = (
    "bash",
    "shell",
    "run_terminal_cmd",
    "execute_command",
    "local_shell",
    "terminal",
)
LOG_RELATIVE = Path(".agent-flow") / "skills-read.jsonl"
# `:10-40`, `:10`, `:50-`, `:10+5`, `:raw`, `:raw:2-4`, `:conflicts` 같은 읽기
# 선택자만 꼬리로 인정한다. 그 외 꼬리는 `SKILL.md.bak`처럼 다른 파일이다.
_SELECTOR_RE = re.compile(
    r"(?::(?:raw|conflicts|\d+(?:[-+]\d+)?-?)(?:,\d+(?:[-+]\d+)?-?)*)+"
)
_SKILL_URI_PREFIX = "skill://"
_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SHELL_SKILL_RE = re.compile(r"((?:~|/|\.{1,2}/)?[^\s'\"|;&<>]*SKILL\.md)")
# 파일 내용을 실제로 출력하는 커맨드만. `ls`/`stat`/`echo`/`rm`은 읽기가 아니다.
_SHELL_READER_RE = re.compile(
    r"(?<!\w)(cat|bat|head|tail|less|more|sed|awk|grep|rg|nl|fold|strings)(?!\w)"
)
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
    observed = tool in SKILL_TOOLS or tool in SHELL_TOOLS or not tool or tool in READ_TOOL_RE
    if not observed:
        return 0

    tool_input = find_first(payload, ("tool_input", "input", "parameters"))
    if not isinstance(tool_input, dict):
        return 0

    cwd = str(find_first(payload, ("cwd", "workspace", "project_root")) or os.getcwd())
    project_root = find_project_root(Path(cwd))
    if project_root is None:
        return 0
    log_path = project_root / LOG_RELATIVE

    if tool in SKILL_TOOLS:
        return append_name(log_path, first_string(tool_input, ("skill", "name")))

    raw_path = first_string(tool_input, ("file_path", "path", "filename", "target"))
    if not raw_path and tool in SHELL_TOOLS:
        raw_path = shell_skill_path(first_string(tool_input, ("command", "cmd", "script")))
    if not raw_path:
        return 0

    if raw_path.startswith(_SKILL_URI_PREFIX):
        # OMP는 `read skill://<name>`으로 연다. 파일 경로가 오지 않는다.
        return append_name(log_path, raw_path[len(_SKILL_URI_PREFIX):])

    candidate = Path(_strip_selector(raw_path))
    if candidate.name != "SKILL.md":
        return 0

    resolved = candidate if candidate.is_absolute() else (Path(cwd) / candidate)
    try:
        resolved = resolved.resolve()
    except OSError:
        return 0
    if not resolved.is_file():
        return 0

    append_entry(log_path, resolved)
    return 0


def append_name(log_path: Path, raw: str) -> int:
    """이름으로만 관측되는 경로. plugin skill은 `<plugin>:<skill>`로 스코프된다."""
    name = raw.strip().split("/", 1)[0].rsplit(":", 1)[-1]
    if not name or not _SAFE_NAME_RE.fullmatch(name):
        return 0
    append_record(log_path, {"skill": name})
    return 0


def shell_skill_path(command: str) -> str:
    """셸로 파일을 **읽은** 경우만 증거다.

    경로가 커맨드에 등장한다는 것만으로 인정하면 `ls`, `stat`, `echo`, `rm`이 게이트를
    통과시킨다 — 파일을 열지 않고 읽음 증거를 만들 수 있다.
    """
    if not _SHELL_READER_RE.search(command):
        return ""
    match = _SHELL_SKILL_RE.search(command)
    return match.group(1) if match else ""


def _strip_selector(raw: str) -> str:
    """`path/SKILL.md:10-40` 같은 **줄 범위 선택자**만 떼고 경로는 보존한다.

    앞에서부터 자르면 `skill://x`나 `C:\\...`가 통째로 망가진다. 그렇다고
    `SKILL.md` 뒤를 무조건 자르면 `SKILL.md.bak`, `SKILL.md-old/notes.txt`가
    형제 `SKILL.md`를 읽은 것으로 둔갑한다 — 게이트 위조 경로다. 그래서
    `SKILL.md` 직후가 선택자이거나 문자열 끝일 때만 자른다.
    """
    marker = "SKILL.md"
    index = raw.rfind(marker)
    if index < 0:
        return raw
    tail = raw[index + len(marker):]
    if tail and not _SELECTOR_RE.fullmatch(tail):
        return raw
    return raw[: index + len(marker)]


def append_entry(log_path: Path, skill_path: Path) -> None:
    append_record(log_path, {"path": str(skill_path)})


def append_record(log_path: Path, record: dict) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({**record, "at": time.time()}, ensure_ascii=False, sort_keys=True)
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
            ["/usr/bin/git", "rev-parse", "--git-common-dir"],
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
