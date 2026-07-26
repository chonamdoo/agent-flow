from __future__ import annotations


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


def _heading_present(text: str, marker: str) -> bool:
    in_fence = False
    for line in text.splitlines():
        if line.startswith("    ") or line.startswith("\t"):
            continue
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("```") or lowered.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if lowered.startswith("#") and lowered == marker:
            return True
    return False


def _completion_gate_lines(text: str) -> list[str]:
    lines = text.splitlines()
    in_gate = False
    in_fence = False
    out: list[str] = []
    for line in lines:
        if line.startswith("    ") or line.startswith("\t"):
            continue
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("```") or lowered.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
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
