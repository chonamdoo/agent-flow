"""Entity memory tests — parsing, temporal staleness, conflicts."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path


KIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_ROOT / "src"))

from agent_flow.memory.entities import EntityMemoryIndex, parse_entity_memory  # noqa: E402


def test_entity_memory_tracks_temporal_stale_and_conflicts(tmp_path: Path):
    (tmp_path / "user-a.md").write_text(
        "---\n"
        "entity: customer-plan\n"
        "value: pro\n"
        "valid_from: 2026-01-01\n"
        "confidence: 0.9\n"
        "source: interview\n"
        "---\n"
        "# Customer plan\n",
        encoding="utf-8",
    )
    (tmp_path / "user-b.md").write_text(
        "---\n"
        "entity: customer-plan\n"
        "value: enterprise\n"
        "valid_from: 2026-02-01\n"
        "confidence: 0.7\n"
        "source: prd\n"
        "---\n"
        "# Customer plan\n",
        encoding="utf-8",
    )
    (tmp_path / "old.md").write_text(
        "---\n"
        "entity: deprecated-api\n"
        "value: v1\n"
        "valid_until: 2025-01-01\n"
        "confidence: 1.0\n"
        "source: adr\n"
        "---\n",
        encoding="utf-8",
    )

    index = EntityMemoryIndex.load(tmp_path, today=date(2026, 5, 1))

    assert len(index.entries) == 3
    assert len(index.stale) == 1
    assert index.conflicts


def test_entity_memory_conflicts_use_supplied_today(tmp_path: Path):
    (tmp_path / "a.md").write_text(
        "---\n"
        "entity: plan\n"
        "value: pro\n"
        "valid_until: 2025-12-31\n"
        "confidence: 0.8\n"
        "source: a\n"
        "---\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text(
        "---\n"
        "entity: plan\n"
        "value: enterprise\n"
        "valid_until: 2025-12-31\n"
        "confidence: 0.8\n"
        "source: b\n"
        "---\n",
        encoding="utf-8",
    )

    index = EntityMemoryIndex.load(tmp_path, today=date(2025, 12, 30))

    assert index.stale == []
    assert index.conflicts


def test_parse_entity_memory_requires_entity_and_value(tmp_path: Path):
    path = tmp_path / "bad.md"
    path.write_text("---\nentity: x\n---\n", encoding="utf-8")

    parsed, reason = parse_entity_memory(path)

    assert parsed is None
    assert reason == "missing value"


def test_entity_memory_malformed_yaml_is_skipped(tmp_path: Path):
    path = tmp_path / "bad-yaml.md"
    path.write_text("---\nentity: [unterminated\n---\n", encoding="utf-8")

    index = EntityMemoryIndex.load(tmp_path)

    assert index.entries == []
    assert len(index.skipped) == 1


def test_entity_memory_invalid_temporal_metadata_is_skipped(tmp_path: Path):
    (tmp_path / "bad-date.md").write_text(
        "---\n"
        "entity: plan\n"
        "value: pro\n"
        "valid_until: not-a-date\n"
        "confidence: 0.8\n"
        "---\n",
        encoding="utf-8",
    )
    (tmp_path / "bad-confidence.md").write_text(
        "---\n"
        "entity: plan\n"
        "value: enterprise\n"
        "valid_from: 2026-01-01\n"
        "confidence: high\n"
        "---\n",
        encoding="utf-8",
    )

    index = EntityMemoryIndex.load(tmp_path, today=date(2026, 5, 1))

    assert index.entries == []
    assert {reason for _, reason in index.skipped} == {"invalid valid_until", "invalid confidence"}


def test_entity_memory_non_finite_confidence_is_skipped(tmp_path: Path):
    (tmp_path / "nan.md").write_text(
        "---\n"
        "entity: plan\n"
        "value: pro\n"
        "confidence: .nan\n"
        "---\n",
        encoding="utf-8",
    )

    index = EntityMemoryIndex.load(tmp_path)

    assert index.entries == []
    assert index.skipped[0][1] == "invalid confidence"
