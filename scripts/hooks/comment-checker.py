#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable


CODE_EXTENSIONS = {
    ".py",
    ".kt",
    ".kts",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".swift",
}

ALLOWED_REASON_RE = re.compile(
    r"\b("
    r"why|because|reason|contract|public api|api contract|external|platform|"
    r"workaround|security|permission|privacy|performance|perf|concurrency|"
    r"thread|threading|coroutine|lifecycle|recomposition|hydration|memoization|"
    r"mainactor|availability|regex|regexp|algorithm|business rule|domain rule"
    r")\b",
    re.IGNORECASE,
)

LOW_VALUE_RE = re.compile(
    r"^\s*("
    r"initialize|initialise|set|sets|assign|assigns|update|updates|create|creates|"
    r"loop through|iterate|iterates|render|renders|handle|handles|call|calls|"
    r"return|returns|check|checks|validate|validates|parse|parses|"
    r"get|gets|fetch|fetches|build|builds|convert|converts"
    r")\b",
    re.IGNORECASE,
)
LOW_VALUE_DETAIL_RE = re.compile(
    r"\b(before|after|must|requires|required|prevent|avoid|cannot|until)\b",
    re.IGNORECASE,
)

SECTION_RE = re.compile(r"^\s*(?:[-=_*#/\s]{6,}|[-=_*#/\s]{3,}.*[-=_*#/\s]{3,})\s*$")
TODO_RE = re.compile(r"^\s*(todo|note|fixme)\b", re.IGNORECASE)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    findings = list(check_payload(payload))
    if not findings:
        return 0

    # exit 2일 때 Claude/Codex는 stderr만 모델에 전달한다. stdout은 무시된다.
    print("comment-checker: newly added low-value comments detected", file=sys.stderr)
    for file_path, line, text in findings[:20]:
        print(f"- {file_path}:{line}: {text}", file=sys.stderr)
    print("Keep WHY, constraint, workaround, security, performance, concurrency, or public API comments; remove comments that only restate code.", file=sys.stderr)
    return 2


def check_payload(payload: object) -> Iterable[tuple[str, int, str]]:
    tool = str(find_first(payload, ("tool_name", "tool")) or "")
    if tool and not re.search(r"(apply_patch|write|edit|multiedit|multi_edit)", tool, re.IGNORECASE):
        return []

    tool_input = find_first(payload, ("tool_input",))
    if isinstance(tool_input, str):
        if "*** Begin Patch" in tool_input:
            return check_patch(tool_input)
        return []
    if not isinstance(tool_input, dict):
        tool_input = {}

    patch_text = string_value(tool_input.get("command")) or string_value(tool_input.get("patch")) or ""
    if "*** Begin Patch" in patch_text:
        return check_patch(patch_text)

    edits = tool_input.get("edits")
    if isinstance(edits, list):
        findings: list[tuple[str, int, str]] = []
        for edit in edits:
            if isinstance(edit, dict):
                findings.extend(check_edit_mapping(tool_input | edit))
        return findings

    return check_edit_mapping(tool_input)


def check_edit_mapping(tool_input: dict[str, object]) -> list[tuple[str, int, str]]:
    file_path = string_value(tool_input.get("file_path")) or string_value(tool_input.get("path"))
    if not file_path or not is_code_file(file_path):
        return []

    new_text = (
        string_value(tool_input.get("content"))
        or string_value(tool_input.get("new_string"))
        or string_value(tool_input.get("newStr"))
        or ""
    )
    old_text = string_value(tool_input.get("old_string")) or string_value(tool_input.get("oldStr")) or ""
    if not new_text:
        return []

    old_counts = Counter(normalize_comment_text(text) for _, text in comment_lines(old_text, file_path))
    findings: list[tuple[str, int, str]] = []
    for line, text in comment_lines(new_text, file_path):
        normalized = normalize_comment_text(text)
        if old_counts[normalized] > 0:
            old_counts[normalized] -= 1
            continue
        if is_low_value_comment(text):
            findings.append((file_path, line, text))
    return findings


def check_patch(patch_text: str) -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    current_file = ""
    line_number = 0
    hunk_lines: list[str] = []
    hunk_added_lines: set[int] = set()
    hunk_start_line = 0

    def flush_hunk() -> None:
        nonlocal hunk_lines, hunk_added_lines, hunk_start_line
        if not current_file or not hunk_lines:
            hunk_lines = []
            hunk_added_lines = set()
            return
        hunk_text = "\n".join(hunk_lines)
        for line, comment in comment_lines(
            hunk_text,
            current_file,
            start_line=hunk_start_line,
            allowed_lines=hunk_added_lines,
        ):
            if is_low_value_comment(comment):
                findings.append((current_file, line, comment))
        hunk_lines = []
        hunk_added_lines = set()
        hunk_start_line = 0

    for raw in patch_text.splitlines():
        if raw.startswith("*** Add File: ") or raw.startswith("*** Update File: "):
            flush_hunk()
            current_file = raw.split(": ", 1)[1].strip()
            line_number = 0
            continue
        if not current_file or not is_code_file(current_file):
            continue
        if raw.startswith("@@"):
            flush_hunk()
            line_number = parse_hunk_line(raw)
            hunk_start_line = line_number + 1
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            line_number += 1
            if not hunk_lines:
                hunk_start_line = line_number
            hunk_lines.append(raw[1:])
            hunk_added_lines.add(line_number)
            continue
        if raw.startswith(" "):
            line_number += 1
            if not hunk_lines:
                hunk_start_line = line_number
            hunk_lines.append(raw[1:])
            continue
        if raw.startswith("-"):
            continue
        flush_hunk()
    flush_hunk()
    return findings


def parse_hunk_line(line: str) -> int:
    match = re.search(r"\+(\d+)", line)
    if not match:
        return 0
    return max(int(match.group(1)) - 1, 0)


def comment_lines(
    text: str,
    file_path: str = "",
    start_line: int = 1,
    allowed_lines: set[int] | None = None,
) -> Iterable[tuple[int, str]]:
    index = 0
    line = start_line
    quote = ""
    triple = ""
    escaped = False
    in_regex = False
    block_start = 0
    block_line = 0
    while index < len(text):
        char = text[index]
        if char == "\n":
            line += 1
            escaped = False
            index += 1
            continue
        if triple:
            if text.startswith(triple, index):
                index += 3
                triple = ""
            else:
                index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if in_regex:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "/":
                in_regex = False
            index += 1
            continue
        if text.startswith(("'''", '"""'), index):
            triple = text[index : index + 3]
            index += 3
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if is_regex_start(text, index, file_path):
            in_regex = True
            index += 1
            continue
        if char == "#" and supports_hash_comments(file_path):
            end = text.find("\n", index)
            if end == -1:
                end = len(text)
            if allowed_lines is None or line in allowed_lines:
                yield line, strip_comment_marker(text[index:end])
            index = end
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            if end == -1:
                end = len(text)
            if allowed_lines is None or line in allowed_lines:
                yield line, strip_comment_marker(text[index:end])
            index = end
            continue
        if text.startswith("/*", index):
            block_start = index
            block_line = line
            end = text.find("*/", index + 2)
            if end == -1:
                end = len(text)
            else:
                end += 2
            block_text = text[block_start:end]
            block_has_allowed_context = bool(ALLOWED_REASON_RE.search(normalize_comment_text(block_text)))
            for offset, block_part in enumerate(block_text.splitlines()):
                current_line = block_line + offset
                if allowed_lines is None or current_line in allowed_lines:
                    cleaned = strip_comment_marker(block_part)
                    if block_has_allowed_context and not should_check_comment_in_allowed_block(cleaned):
                        continue
                    yield current_line, cleaned
            line += block_text.count("\n")
            index = end
            continue
        index += 1


def supports_hash_comments(file_path: str) -> bool:
    return Path(file_path).suffix.lower() in {".py", ".sh", ".yaml", ".yml"} or not file_path


def is_regex_start(text: str, index: int, file_path: str) -> bool:
    if Path(file_path).suffix.lower() not in {".js", ".jsx", ".ts", ".tsx"}:
        return False
    if text[index] != "/" or text.startswith(("//", "/*"), index):
        return False
    previous = previous_significant_char(text, index)
    return previous == "" or previous in "=({[,!:?;|&"


def previous_significant_char(text: str, index: int) -> str:
    cursor = index - 1
    while cursor >= 0 and text[cursor].isspace():
        cursor -= 1
    return text[cursor] if cursor >= 0 else ""


def is_comment_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return (
        stripped.startswith("#")
        or stripped.startswith("//")
        or stripped.startswith("/*")
        or stripped.startswith("*")
    )


def strip_comment_marker(line: str) -> str:
    stripped = line.strip()
    for prefix in ("/**", "/*"):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :].strip()
            break
    if stripped.endswith("*/"):
        stripped = stripped[:-2].strip()
    cleaned: list[str] = []
    for part in stripped.splitlines():
        part = part.strip()
        for prefix in ("///", "//", "###", "##", "#", "*/", "*"):
            if part.startswith(prefix):
                part = part[len(prefix) :].strip()
                break
        cleaned.append(part.rstrip("*/").strip())
    return " ".join(part for part in cleaned if part).strip()


def is_low_value_comment(text: str) -> bool:
    normalized = normalize_comment_text(text)
    if not normalized:
        return False
    if SECTION_RE.match(normalized):
        return True
    if ALLOWED_REASON_RE.search(normalized):
        return False
    if is_unqualified_todo_comment(text):
        return True
    if LOW_VALUE_DETAIL_RE.search(normalized):
        return False
    return bool(LOW_VALUE_RE.match(normalized))


def is_generic_low_value_comment(text: str) -> bool:
    normalized = normalize_comment_text(text)
    if not normalized:
        return False
    if ALLOWED_REASON_RE.search(normalized) or LOW_VALUE_DETAIL_RE.search(normalized):
        return False
    if not LOW_VALUE_RE.match(normalized):
        return False
    return len(re.findall(r"[a-z0-9_]+", normalized)) <= 3


def should_check_comment_in_allowed_block(text: str) -> bool:
    normalized = normalize_comment_text(text)
    return bool(
        SECTION_RE.match(normalized)
        or is_unqualified_todo_comment(text)
        or is_generic_low_value_comment(text)
    )


def is_unqualified_todo_comment(text: str) -> bool:
    normalized = normalize_comment_text(text)
    if not TODO_RE.match(normalized):
        return False
    if re.match(r"^\s*(todo|note|fixme)\s*\([^)]+\)", normalized, re.IGNORECASE):
        return False
    body = re.sub(r"^\s*(todo|note|fixme)\s*:?\s*", "", normalized, flags=re.IGNORECASE)
    return not bool(
        re.search(
            r"\b(why|because|after|before|when|until|blocked|owner|requires|required|must|external|platform|workaround|security|performance|concurrency|lifecycle|public api)\b",
            body,
            re.IGNORECASE,
        )
    )


def normalize_comment_text(text: str) -> str:
    return re.sub(r"\s+", " ", strip_comment_marker(text)).strip().lower()


def is_code_file(file_path: str) -> bool:
    return Path(file_path).suffix.lower() in CODE_EXTENSIONS


def find_mapping(value: object, key: str) -> dict[str, object] | None:
    found = find_first(value, (key,))
    return found if isinstance(found, dict) else None


def find_first(value: object, keys: tuple[str, ...]) -> object | None:
    if isinstance(value, dict):
        for key in keys:
            if key in value:
                return value[key]
        for child in value.values():
            found = find_first(child, keys)
            if found is not None:
                return found
    if isinstance(value, list):
        for child in value:
            found = find_first(child, keys)
            if found is not None:
                return found
    return None


def string_value(value: object) -> str:
    return value if isinstance(value, str) else ""


if __name__ == "__main__":
    raise SystemExit(main())
