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
import sys
import time
from pathlib import Path

READ_TOOL_RE = ("read", "read_file", "view", "cat")
LOG_RELATIVE = Path(".agent-flow") / "skills-read.jsonl"
# `:10-40`, `:10`, `:50-`, `:10+5`, `:raw`, `:raw:2-4`, `:conflicts` 같은 읽기
# 선택자만 꼬리로 인정한다. 그 외 꼬리는 `SKILL.md.bak`처럼 다른 파일이다.
_SELECTOR_RE = re.compile(
    r"(?::(?:raw|conflicts|\d+(?:[-+]\d+)?-?)(?:,\d+(?:[-+]\d+)?-?)*)+"
)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    tool = str(find_first(payload, ("tool_name", "tool")) or "").lower()
    if tool and not any(candidate == tool for candidate in READ_TOOL_RE):
        return 0

    tool_input = find_first(payload, ("tool_input", "input", "parameters"))
    if not isinstance(tool_input, dict):
        return 0
    raw_path = first_string(tool_input, ("file_path", "path", "filename", "target"))
    if not raw_path:
        return 0

    candidate = Path(_strip_selector(raw_path))
    if candidate.name != "SKILL.md":
        return 0

    cwd = str(find_first(payload, ("cwd", "workspace", "project_root")) or os.getcwd())
    project_root = find_project_root(Path(cwd))
    if project_root is None:
        return 0

    resolved = candidate if candidate.is_absolute() else (Path(cwd) / candidate)
    try:
        resolved = resolved.resolve()
    except OSError:
        return 0
    if not resolved.is_file():
        return 0

    append_entry(project_root / LOG_RELATIVE, resolved)
    return 0


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
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"path": str(skill_path), "at": time.time()}, ensure_ascii=False, sort_keys=True
        )
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        # 관측 실패가 작업을 막아서는 안 된다. 증거가 없으면 gate가 unavailable로 처리한다.
        return


def find_project_root(start: Path) -> Path | None:
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
