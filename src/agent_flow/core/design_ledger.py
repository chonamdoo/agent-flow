"""수치 원장 — 설계 phase가 남긴 값을 runner가 전 phase에 강제 주입한다.

phase 간 유일한 운반체가 "압축되는 host 대화 컨텍스트"였다. `render_envelope`가
이전 artifact를 하나도 넣지 않아서, 매 phase마다 수치가 기억에서 다시 꺼내졌다.
그래서 같은 입력에서도 결과가 매번 달라졌다.

관측자는 **runner 렌더러**다. 값을 쓰는 것은 agent지만, 그 값이 다음 phase의
프롬프트에 실리는지는 agent가 통제하지 못한다. 주입 여부만 보장한다.

**이 층이 증명하지 않는 것**: 준수. 16dp를 보고도 12dp를 쓰면 여기서는 아무도
안 잡는다. 원장의 정확성은 사람 확인(V3)이, 구현 대조는 수치 gate(V6)가 본다.
빈 원장도 주입은 성공한다 — 그때는 "기록된 값이 없다"가 그대로 실린다.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_flow.core.markers import completion_gate_marker_values

LEDGER_FILE = "design-spec.md"
LEDGER_SECTION = "design values"
LEDGER_MARKER = "design-values:"

# 원장을 만드는 phase. 여기서만 값이 들어오고, 나머지 phase는 읽기만 한다.
LEDGER_SOURCE_PHASES = frozenset({"design", "prd"})

_NONE_VALUES = frozenset({"none", "n/a", "na", "-"})


@dataclass(frozen=True)
class DesignLedger:
    exists: bool
    source_phase: str
    values: tuple[tuple[str, str], ...]


def capture_design_ledger(run_dir: Path, phase_id: str, artifact_text: str) -> DesignLedger | None:
    """설계 artifact에서 값을 뽑아 원장 파일로 굳힌다. 원장 phase가 아니면 None."""
    if phase_id not in LEDGER_SOURCE_PHASES:
        return None
    values = parse_design_values(artifact_text)
    ledger = DesignLedger(exists=True, source_phase=phase_id, values=values)
    write_ledger(run_dir, ledger)
    return ledger


def parse_design_values(text: str) -> tuple[tuple[str, str], ...]:
    """`## Design Values` 아래의 `key: value` 줄만 모은다.

    Completion Gate 파서와 같은 규율이다 — 들여쓴 줄과 코드 펜스는 건너뛰고,
    다음 `#` 헤딩에서 끊는다. 그러지 않으면 본문의 아무 콜론 줄이나 값으로
    둔갑한다.
    """
    values: list[tuple[str, str]] = []
    in_section = False
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
        if lowered.startswith("#"):
            heading = lowered.lstrip("#").strip()
            if heading == LEDGER_SECTION:
                in_section = True
                continue
            if in_section:
                break
            continue
        if not in_section:
            continue
        candidate = _strip_bullet(stripped)
        key, separator, value = candidate.partition(":")
        key = key.strip()
        value = value.strip()
        if separator != ":" or not key or not value:
            continue
        if value.lower() in _NONE_VALUES:
            continue
        values.append((key, value))
    return tuple(values)


def write_ledger(run_dir: Path, ledger: DesignLedger) -> Path:
    path = run_dir / LEDGER_FILE
    lines = [
        "# Design values ledger",
        "",
        f"Source phase: {ledger.source_phase}",
        "",
        f"## {LEDGER_SECTION.title()}",
        "",
    ]
    lines.extend(f"{key}: {value}" for key, value in ledger.values)
    if not ledger.values:
        lines.append("(none recorded)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def read_ledger(run_dir: Path) -> DesignLedger:
    path = run_dir / LEDGER_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return DesignLedger(exists=False, source_phase="", values=())
    source = ""
    for line in text.splitlines():
        if line.lower().startswith("source phase:"):
            source = line.partition(":")[2].strip()
            break
    return DesignLedger(exists=True, source_phase=source, values=parse_design_values(text))


def ledger_prompt_block(run_dir: Path) -> str:
    """전 phase 엔벨로프에 실리는 블록. agent가 끌 수 없다."""
    ledger = read_ledger(run_dir)
    if not ledger.exists:
        return ""
    header = (
        "\n## Design values (ledger — injected by the runner)\n\n"
        f"Recorded in the `{ledger.source_phase}` phase and carried into every "
        "phase of this run. You did not opt in to this block and cannot suppress "
        "it; it is the only thing that survives a compacted conversation.\n\n"
    )
    if not ledger.values:
        return header + (
            "No design values were recorded for this run. Do not invent numbers, "
            "colors, or tokens that are not in the task or the codebase.\n"
        )
    body = "\n".join(f"- `{key}`: `{value}`" for key, value in ledger.values)
    return header + body + (
        "\n\nUse these exact values. If one of them is wrong, say so and stop — "
        "do not silently substitute a different number.\n"
    )


def _strip_bullet(line: str) -> str:
    candidate = line.strip()
    if candidate.startswith("+"):
        candidate = candidate[1:].strip()
    for prefix in ("- [x] ", "- [ ] ", "- ", "* ", "| "):
        if candidate.lower().startswith(prefix):
            return candidate[len(prefix):].strip().strip("|").strip()
    return candidate


def missing_design_value_markers(text: str, phase_id: str) -> list[str]:
    """원장 마커가 본문과 어긋나면 막는다.

    마커만 있으면 `design-values: none` 한 줄로 수치 기록 전체를 건너뛸 수
    있고, `design-values-confirmed: n/a`는 사람 확인을 건너뛰는 기본값이 된다.
    본문과 대조해야 escape hatch가 기본값이 되지 않는다.
    """
    if phase_id not in LEDGER_SOURCE_PHASES:
        return []
    values = parse_design_values(text)
    recorded = completion_gate_marker_values(text)
    missing: list[str] = []
    declared = recorded.get("design-values", "").strip().lower()
    if values and declared in _NONE_VALUES:
        keys = ", ".join(key for key, _ in values)
        missing.append(f"design-values: {keys} (the section records values; 'none' contradicts it)")
    if not values and declared and declared not in _NONE_VALUES:
        missing.append("design-values: none (the ## Design Values section is empty)")
    confirmed = recorded.get("design-values-confirmed", "").strip().lower()
    if confirmed and values and confirmed not in {"yes", "y"}:
        missing.append(
            "design-values-confirmed: yes (values were recorded, so the user has to "
            "confirm your reading; 'n/a' is only for a task with no supplied values)"
        )
    return missing
