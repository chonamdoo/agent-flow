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
    """code block 바깥의 줄만 남긴다. fenced와 indented 둘 다 여기서 걷어낸다.

    CommonMark 규칙을 따른다: fence opener는 최대 3칸 들여쓰기까지, backtick
    fence의 info string에는 backtick이 올 수 없고(그래서 한 줄짜리 코드 스팬은
    fence를 열지 않는다), closer는 같은 문자를 opener 이상 길이로 쓴 줄뿐이다.
    단순 토글로 읽으면 ```` ```dp``` ```` 한 줄이 뒤 본문 전체를 삼킨다.

    indented code block은 **직전 줄이 문단 본문이 아닐 때** 열린다. 들여쓰기 폭만
    보고 자르면 리스트 항목이나 문단에 이어지는 줄까지 코드로 오인해, 마커를
    리스트 밑에 적은 artifact가 다 써 넣고도 missing으로 막힌다. 반대로 빈 줄만
    조건으로 삼으면 `## Completion Gate` 바로 다음 줄의 들여쓴 예시가 코드가 아니라
    본문으로 남아, 예시만 적어 둔 artifact가 게이트를 통과한다. CommonMark에서
    들여쓰기가 코드를 열지 못하는 것은 그것이 문단의 lazy continuation이 될 때뿐이다.
    문단 여부는 `_NOT_A_PARAGRAPH`와 `_SETEXT_UNDERLINE`의 근사로 판정한다 —
    규칙 전체를 옮긴 것이 아니므로 그 두 상수의 주석에 근사 경계를 적어 두었다.
    한 번 열린 코드는 4칸 미만으로 들여쓴 비어 있지 않은 줄이 나올 때까지 이어진다.

    두 판정을 같은 자리에 두는 이유는 모듈 docstring의 약속 때문이다 — 호출부가
    각자 들여쓰기를 자르면 fence는 여기서, indented는 저기서 갈라져 두 파서가
    같은 artifact를 다르게 읽는다.
    """
    visible: list[str] = []
    fence_char = ""
    fence_length = 0
    in_code = False
    in_paragraph = False
    for line in text.splitlines():
        if fence_char:
            if re.fullmatch(
                rf" {{0,3}}{re.escape(fence_char)}{{{fence_length},}}[ \t]*",
                line,
            ):
                fence_char = ""
                fence_length = 0
            in_paragraph = False
            continue
        opener = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if opener:
            marker, info = opener.groups()
            # opener는 3칸까지만 들여쓸 수 있으므로 이 줄은 열려 있던 indented
            # code를 반드시 끝낸다. 안 닫으면 뒤따르는 들여쓴 줄이 코드로 버려진다.
            in_code = False
            if marker[0] == "`" and "`" in info:
                # 한 줄짜리 코드 스팬은 fence를 열지 않으므로 본문으로 남고,
                # 그래서 다음 줄의 들여쓰기는 이 문단에 이어지는 것이다.
                in_paragraph = True
                visible.append(line)
                continue
            fence_char = marker[0]
            fence_length = len(marker)
            in_paragraph = False
            continue
        blank = not line.strip()
        indented = line.startswith("    ") or line.startswith("\t")
        if in_code:
            if blank or indented:
                continue
            in_code = False
        elif indented and not blank and not in_paragraph:
            in_code = True
            continue
        in_paragraph = _opens_paragraph(line, in_paragraph) if not blank else False
        visible.append(line)
    return "\n".join(visible)


# 아래 두 정규식은 "문단이 **아닌** 줄"을 가린다. 그 여집합이 문단 본문이고,
# 문단 본문 다음의 들여쓰기는 lazy continuation이라 코드 블록을 열지 못한다.
# 반대로 heading·thematic break·setext underline 다음 줄은 코드가 될 수 있다.
#
# CommonMark 전체를 옮긴 것은 아니다. blockquote, HTML block, list marker는
# 문단으로 취급한다 — 그쪽으로 틀리면 marker를 본문으로 읽어 게이트가 열리는
# 대신, 코드를 본문으로 읽어 게이트가 닫히는 쪽으로 틀린다.
_NOT_A_PARAGRAPH = re.compile(
    r"^ {0,3}(?:#{1,6}(?:[ \t]|$)|(?:\*[ \t]*){3,}$|(?:-[ \t]*){3,}$|(?:_[ \t]*){3,}$)"
)
# setext underline은 **열린 문단이 있을 때만** 성립한다. 빈 줄 뒤의 `===`는
# 그냥 문단이므로, 상태를 안 보고 무조건 문단 종료로 읽으면 그 다음의 정상
# marker를 코드로 오인한다. `---`는 이미 thematic break로 잡힌다.
_SETEXT_UNDERLINE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")


def _opens_paragraph(line: str, in_paragraph: bool) -> bool:
    if _NOT_A_PARAGRAPH.match(line):
        return False
    return not (in_paragraph and _SETEXT_UNDERLINE.match(line))


def _too_indented_for_a_heading(line: str) -> bool:
    """ATX heading은 3칸까지만 들여쓸 수 있다(CommonMark).

    그 이상 들여쓴 `## Completion Gate`는 heading이 아니라 앞 문단·리스트에 딸린
    본문이다. 이걸 heading으로 읽으면 문단 안에 적어 둔 예시가 게이트를 열어 준다.
    코드 블록 판정과는 별개다 — 들여쓴 줄이라도 코드가 아니면 본문으로는 남는다.
    """
    return line.startswith("    ") or line.startswith("\t")


def _heading_present(text: str, marker: str) -> bool:
    for line in unfenced_markdown_text(text).splitlines():
        if _too_indented_for_a_heading(line):
            continue
        lowered = line.strip().lower()
        if lowered.startswith("#") and lowered == marker:
            return True
    return False


def _completion_gate_lines(text: str) -> list[str]:
    in_gate = False
    gate_level = 0
    out: list[str] = []
    for line in unfenced_markdown_text(text).splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("#") and not _too_indented_for_a_heading(line):
            level = len(lowered) - len(lowered.lstrip("#"))
            heading = lowered.lstrip("#").strip()
            if heading == COMPLETION_GATE_HEADING:
                in_gate = True
                gate_level = level
                continue
            # 섹션을 끝내는 것은 같은 레벨 이상의 heading뿐이다. `### Notes` 같은
            # 하위 heading에서 끊으면 그 아래 마커가 문서에 있는데도 missing으로
            # 잡혀, 사용자는 다 써 넣은 artifact가 이유 없이 막히는 것을 본다.
            if in_gate and level <= gate_level:
                break
            if in_gate:
                continue
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
