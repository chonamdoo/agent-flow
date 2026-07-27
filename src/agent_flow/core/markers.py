"""Completion Gate 마커 파서.

Markdown fence 판정은 이 모듈 하나만 갖는다. 파서마다 규칙이 다르면 같은
artifact가 gate에서는 통과하고 원장에서는 막히는 일이 생긴다.
"""
from __future__ import annotations

import re


COMPLETION_GATE_HEADING = "completion gate"


def normalize_required_markers(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(marker) for marker in value)
    return ()


def missing_markers(text: str, markers: tuple[str, ...]) -> list[str]:
    if not markers:
        return []
    lines = _completion_gate_lines(text)
    return [
        marker
        for marker in markers
        if not _marker_present(text, lines, marker.strip().lower())
    ]


def has_failure_markers(text: str) -> bool:
    return any(_line_has_failure_marker(line) for line in _completion_gate_lines(text))


def completion_gate_marker_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _completion_gate_lines(text):
        key, separator, value = line.partition(":")
        if separator == ":":
            values[key.strip()] = value.strip()
    return values


def _marker_present(text: str, gate_lines: list[str], marker: str) -> bool:
    if marker.startswith("#"):
        return _heading_present(text, marker)
    return any(_line_matches_marker(line, marker) for line in gate_lines)


def unfenced_markdown_text(text: str) -> str:
    """유효한 fenced code block 바깥의 줄만 남긴다.

    CommonMark 규칙을 따른다: opener는 최대 3칸 들여쓰기까지, backtick fence의
    info string에는 backtick이 올 수 없고(그래서 한 줄짜리 코드 스팬은 fence를
    열지 않는다), closer는 같은 문자를 opener 이상 길이로 쓴 줄뿐이다. 단순
    토글로 읽으면 ```` ```dp``` ```` 한 줄이 뒤 본문 전체를 삼킨다.
    """
    visible: list[str] = []
    fence_char = ""
    fence_length = 0
    for line in text.splitlines():
        if fence_char:
            if re.fullmatch(
                rf" {{0,3}}{re.escape(fence_char)}{{{fence_length},}}[ \t]*",
                line,
            ):
                fence_char = ""
                fence_length = 0
            continue
        opener = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if opener:
            marker, info = opener.groups()
            if marker[0] == "`" and "`" in info:
                visible.append(line)
                continue
            fence_char = marker[0]
            fence_length = len(marker)
            continue
        visible.append(line)
    return "\n".join(visible)


def _heading_present(text: str, marker: str) -> bool:
    for line in unfenced_markdown_text(text).splitlines():
        if line.startswith("    ") or line.startswith("\t"):
            continue
        lowered = line.strip().lower()
        if lowered.startswith("#") and lowered == marker:
            return True
    return False


def _completion_gate_lines(text: str) -> list[str]:
    in_gate = False
    out: list[str] = []
    for line in unfenced_markdown_text(text).splitlines():
        if line.startswith("    ") or line.startswith("\t"):
            continue
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("#"):
            heading = lowered.lstrip("#").strip()
            if heading == COMPLETION_GATE_HEADING:
                in_gate = True
                continue
            if in_gate:
                break
        if in_gate:
            out.append(_normalize_marker_line(stripped).lower())
    return out


def _normalize_marker_line(line: str) -> str:
    # Completion Gate 마커는 체크리스트나 diff 추가 줄에서도 같은 값으로 비교한다.
    candidate = line.strip()
    if candidate.startswith("+"):
        candidate = candidate[1:].strip()
    lowered = candidate.lower()
    for prefix in ("- [x] ", "- [ ] ", "- ", "* "):
        if lowered.startswith(prefix):
            return candidate[len(prefix):].strip()
    return candidate


def _line_has_failure_marker(line: str) -> bool:
    _key, separator, value = line.partition(":")
    if separator != ":":
        return False
    key = _key.strip()
    normalized_value = value.strip()
    if normalized_value == "fail":
        return True
    if key == "missing-required-profile-skills" and normalized_value not in {"", "none", "n/a"}:
        return True
    return False


def _line_matches_marker(line: str, marker: str) -> bool:
    if marker.endswith(":"):
        return line.startswith(marker) and _is_concrete_value(line[len(marker):])
    key, separator, value = marker.partition(":")
    if separator and "|" in value:
        line_key, line_separator, line_value = line.partition(":")
        if line_separator != ":" or line_key.strip() != key.strip():
            return False
        allowed = {part.strip() for part in value.split("|") if part.strip()}
        # n/a 마커는 artifact에서 optional로 써도 같은 비적용 상태로 인정한다.
        if "n/a" in allowed:
            allowed.add("optional")
        return line_value.strip() in allowed
    return line == marker


def _is_concrete_value(value: str) -> bool:
    """빈 값과 `<...>` 자리표시자를 거른다.

    프롬프트는 `cache-invalidation-policy: <policy or n/a>` 같은 틀을 그대로
    보여준다. 그걸 복사해 붙이면 게이트는 통과하고 값은 없다. 빈 값 검사만
    있으면 자리표시자가 그 구멍을 그대로 대신한다.
    """
    stripped = value.strip()
    if not stripped:
        return False
    return not (stripped.startswith("<") and stripped.endswith(">"))
