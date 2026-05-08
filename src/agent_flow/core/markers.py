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
        if not any(_line_matches_marker(line, marker.strip().lower()) for line in lines)
    ]


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
            out.append(stripped.lower())
    return out


def _line_matches_marker(line: str, marker: str) -> bool:
    if marker.endswith(":"):
        return line.startswith(marker) and bool(line[len(marker):].strip())
    key, separator, value = marker.partition(":")
    if separator and "|" in value:
        line_key, line_separator, line_value = line.partition(":")
        if line_separator != ":" or line_key.strip() != key.strip():
            return False
        allowed = {part.strip() for part in value.split("|") if part.strip()}
        return line_value.strip() in allowed
    return line == marker
