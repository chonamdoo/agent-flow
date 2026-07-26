"""수치 원장 테스트.

원장이 지키는 것은 **전달**뿐이다. 준수는 지키지 않는다 — 16dp를 보고 12dp를
써도 여기서는 안 잡힌다. 그래서 테스트도 전달만 반증한다: 원장이 있으면 모든
phase 엔벨로프에 실리고, agent가 그것을 끌 수 없다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.adapters.generic import GenericAdapter
from agent_flow.core.design_ledger import (
    LEDGER_FILE,
    capture_design_ledger,
    ledger_prompt_block,
    missing_design_value_markers,
    parse_design_values,
    read_ledger,
)
from agent_flow.runner import Phase

ARTIFACT = """# prd

## Design Values

horizontal-padding: 16dp
brand-primary: #FF6B00
list-row-height: 56dp

## Completion Gate

design-values: horizontal-padding, brand-primary, list-row-height
design-values-confirmed: yes
"""


def test_parses_only_the_design_values_section():
    """반증: 본문 아무 콜론 줄이나 값이 되면 원장이 쓰레기로 찬다."""
    text = "# prd\n\nowner: someone\n\n## Design Values\n\ngap: 8dp\n\n## Risks\n\nlatency: high\n"
    assert parse_design_values(text) == (("gap", "8dp"),)


def test_fenced_and_indented_lines_are_not_values():
    text = "## Design Values\n\n```\nfake: 1dp\n```\n\n    also-fake: 2dp\nreal: 3dp\n"
    assert parse_design_values(text) == (("real", "3dp"),)


def test_bullet_values_are_accepted():
    text = "## Design Values\n\n- gap: 8dp\n* radius: 4dp\n"
    assert parse_design_values(text) == (("gap", "8dp"), ("radius", "4dp"))


def test_none_placeholder_is_not_a_value():
    assert parse_design_values("## Design Values\n\ngap: none\n") == ()


def test_capture_writes_the_ledger_file(tmp_path):
    ledger = capture_design_ledger(tmp_path, "prd", ARTIFACT)
    assert ledger is not None
    assert (tmp_path / LEDGER_FILE).is_file()
    assert read_ledger(tmp_path).values == (
        ("horizontal-padding", "16dp"),
        ("brand-primary", "#FF6B00"),
        ("list-row-height", "56dp"),
    )


def test_capture_ignores_non_source_phases(tmp_path):
    assert capture_design_ledger(tmp_path, "implement", ARTIFACT) is None
    assert not (tmp_path / LEDGER_FILE).exists()


def test_no_ledger_yields_no_block(tmp_path):
    assert ledger_prompt_block(tmp_path) == ""


def test_empty_ledger_still_injects(tmp_path):
    """반증: 빈 원장을 침묵으로 접으면 '수치가 없다'와 '기록을 안 했다'가 같아진다."""
    capture_design_ledger(tmp_path, "design", "## Design Values\n\n")
    block = ledger_prompt_block(tmp_path)
    assert "No design values were recorded" in block


def _envelope(tmp_path: Path, phase_id: str) -> str:
    adapter = GenericAdapter()
    adapter._task_text = "add the login screen"
    return adapter.render_envelope(
        Phase(id=phase_id, description="d", prompt="p"),
        run_dir=tmp_path,
        project_root=tmp_path,
    )


@pytest.mark.parametrize("phase_id", ["red", "green", "refactor", "multi-review", "gates", "commit"])
def test_every_phase_envelope_carries_the_ledger(tmp_path, phase_id):
    """#102 F2의 핵심. 수치가 살아남는 phase가 0개였다."""
    capture_design_ledger(tmp_path, "prd", ARTIFACT)
    envelope = _envelope(tmp_path, phase_id)
    assert "horizontal-padding" in envelope
    assert "16dp" in envelope
    assert "#FF6B00" in envelope


def test_envelope_carries_the_task_text(tmp_path):
    """hosted 어댑터 엔벨로프에는 task 텍스트조차 없었다."""
    assert "add the login screen" in _envelope(tmp_path, "red")


def test_none_marker_contradicting_recorded_values_is_blocked():
    """반증: `design-values: none` 한 줄로 수치 기록 전체를 건너뛸 수 있으면 안 된다."""
    text = "## Design Values\n\ngap: 8dp\n\n## Completion Gate\n\ndesign-values: none\n"
    missing = missing_design_value_markers(text, "prd")
    assert any(v.startswith("design-values: gap") for v in missing)


def test_values_marker_without_a_section_is_blocked():
    text = "## Design Values\n\n\n## Completion Gate\n\ndesign-values: gap\n"
    assert missing_design_value_markers(text, "prd") == [
        "design-values: none (the ## Design Values section is empty)"
    ]


def test_confirmation_escape_hatch_is_blocked_when_values_exist():
    """반증: `n/a`가 즉시 기본값이 되면 사람 관측자가 소멸한다."""
    text = (
        "## Design Values\n\ngap: 8dp\n\n## Completion Gate\n\n"
        "design-values: gap\ndesign-values-confirmed: n/a\n"
    )
    missing = missing_design_value_markers(text, "prd")
    assert any("design-values-confirmed: yes" in v for v in missing)


def test_confirmation_na_is_fine_with_no_values():
    text = "## Design Values\n\n\n## Completion Gate\n\ndesign-values: none\ndesign-values-confirmed: n/a\n"
    assert missing_design_value_markers(text, "prd") == []


def test_consistent_artifact_passes():
    assert missing_design_value_markers(ARTIFACT, "prd") == []


def test_non_source_phases_are_not_checked():
    text = "## Completion Gate\n\ndesign-values: none\n"
    assert missing_design_value_markers(text, "implement") == []


@pytest.mark.parametrize("copy", ["workflows", "src/agent_flow/workflows"])
def test_prd_pauses_for_the_read_back(copy):
    """관측자가 사람인 유일한 지점이다. pause가 없으면 확인 자리가 없다."""
    import yaml

    workflow = yaml.safe_load((REPO / copy / "full-feature.yaml").read_text(encoding="utf-8"))
    prd = next(phase for phase in workflow["phases"] if phase["id"] == "prd")
    assert prd["pause_after"] is True
    assert "design-values-confirmed: yes|n/a" in prd["required_markers"]
