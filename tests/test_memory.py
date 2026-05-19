"""Memory engine tests — parsing, indexing, search, fingerprint, compaction."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest


KIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_ROOT / "src"))

from agent_flow.memory.lore import (  # noqa: E402
    Lore, parse_lore, parse_failure_reason, write_weight,
)
from agent_flow.memory.index import LoreIndex  # noqa: E402
from agent_flow.memory.compact import compact, RETENTION_DAYS  # noqa: E402
from agent_flow.memory.entities import EntityMemoryIndex, parse_entity_memory  # noqa: E402


def _make_lore(path: Path, *, title: str = "T", type_: str = "decision",
               date_: date | None = None, scope: str = "feature/x",
               weight: float = 1.0, tags: list[str] | None = None,
               constraint: str = "C", rejected: list[str] | None = None,
               directive: str = "D") -> Path:
    date_ = date_ or date.today()
    tags = tags or ["a", "b"]
    rejected = rejected or ["alt: reason"]
    rejected_md = "\n".join(f"- {r}" for r in rejected)
    text = (
        f"---\n"
        f"title: {title}\n"
        f"type: {type_}\n"
        f"date: {date_.isoformat()}\n"
        f"scope: {scope}\n"
        f"weight: {weight}\n"
        f"tags: [{', '.join(tags)}]\n"
        f"---\n"
        f"# {title}\n\n"
        f"## Context\nctx\n\n"
        f"## Constraint\n{constraint}\n\n"
        f"## Outcome\nout\n\n"
        f"## Rejected\n{rejected_md}\n\n"
        f"## Directive\n{directive}\n"
    )
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_full_lore(tmp_path: Path):
    p = _make_lore(tmp_path / "x.md", title="StateFlow split",
                   constraint="recomposition cascades", directive="split flows")
    lore = parse_lore(p)
    assert lore is not None
    assert lore.title == "StateFlow split"
    assert lore.constraint == "recomposition cascades"
    assert lore.directive == "split flows"
    assert lore.rejected == ["alt: reason"]
    assert lore.weight == 1.0


def test_parse_no_frontmatter_returns_none(tmp_path: Path):
    p = tmp_path / "no_fm.md"
    p.write_text("# Just a heading\nNo frontmatter.")
    assert parse_lore(p) is None


def test_fingerprint_dedup_detection(tmp_path: Path):
    p1 = _make_lore(tmp_path / "a.md", constraint="X imports Y", directive="invert")
    p2 = _make_lore(tmp_path / "b.md", title="Different title",
                    constraint="x imports y", directive="invert")
    p3 = _make_lore(tmp_path / "c.md", constraint="X imports Y", directive="DIFFERENT")
    a = parse_lore(p1)
    b = parse_lore(p2)
    c = parse_lore(p3)
    assert a.fingerprint == b.fingerprint  # case + whitespace normalized
    assert a.fingerprint != c.fingerprint


def test_index_search(tmp_path: Path):
    _make_lore(tmp_path / "compose.md", title="Compose recomposition",
               tags=["compose", "performance"], constraint="recomp cascade",
               directive="split flows")
    _make_lore(tmp_path / "navigation.md", title="Navigation refactor",
               tags=["nav3"], constraint="back stack", directive="hilt scope")
    _make_lore(tmp_path / "auth.md", title="Auth flow",
               tags=["auth"], constraint="token expiry", directive="refresh")
    index = LoreIndex.load(tmp_path)
    assert len(index.entries) == 3

    results = index.search(["compose", "recomposition"])
    assert results
    assert results[0].title == "Compose recomposition"

    no_match = index.search(["nonexistent_keyword_xyz"])
    assert no_match == []


def test_compact_dedup(tmp_path: Path):
    today = date(2026, 5, 1)
    p1 = _make_lore(tmp_path / "a.md", date_=date(2026, 4, 1),
                    constraint="same", directive="same")
    p2 = _make_lore(tmp_path / "b.md", date_=date(2026, 4, 15),
                    constraint="same", directive="same")
    index = LoreIndex.load(tmp_path)
    report = compact(index, today=today, apply=True)
    assert len(report.duplicates_archived) == 1
    # Older one (a.md) archived
    assert any("a.md" in str(t) for _, t in report.duplicates_archived)
    archive = tmp_path / "archive"
    assert (archive / "a.md").exists()
    assert p2.exists()  # newer kept


def test_compact_stale_drop(tmp_path: Path):
    today = date(2026, 5, 1)
    # decision retention = 180d; created 200d ago
    old = _make_lore(tmp_path / "old.md", type_="decision",
                     date_=today - timedelta(days=200))
    fresh = _make_lore(tmp_path / "fresh.md", type_="decision",
                       date_=today - timedelta(days=30))
    index = LoreIndex.load(tmp_path)
    report = compact(index, today=today, apply=True)
    assert (tmp_path / "archive" / "old.md").exists()
    assert fresh.exists()
    assert old.exists() is False  # moved


def test_compact_dry_run(tmp_path: Path):
    today = date(2026, 5, 1)
    _make_lore(tmp_path / "old.md", type_="rollback",
               date_=today - timedelta(days=80))  # rollback retention = 60d
    index = LoreIndex.load(tmp_path)
    report = compact(index, today=today, apply=False)
    assert report.dry_run is True
    assert len(report.stale_archived) == 1
    # File NOT actually moved in dry-run
    assert (tmp_path / "old.md").exists()
    assert not (tmp_path / "archive" / "old.md").exists()


def test_weight_decay_updates_files(tmp_path: Path):
    today = date(2026, 5, 1)
    _make_lore(tmp_path / "old.md", date_=today - timedelta(days=100),
               weight=1.0)  # old → should drop to 0.3
    index = LoreIndex.load(tmp_path)
    report = compact(index, today=today, apply=True)
    assert any(
        new == 0.3 for _, _, new in report.weight_updated
    )
    # Verify on disk
    re_read = parse_lore(tmp_path / "old.md")
    assert re_read.weight == 0.3


def test_write_weight_preserves_body(tmp_path: Path):
    p = _make_lore(tmp_path / "x.md", weight=1.0, constraint="X", directive="Y")
    write_weight(p, 0.6)
    re_read = parse_lore(p)
    assert re_read.weight == 0.6
    assert re_read.constraint == "X"
    assert re_read.directive == "Y"


def test_parse_handles_crlf(tmp_path: Path):
    """A file written on Windows (CRLF) must still parse."""
    p = tmp_path / "win.md"
    body = (
        "---\r\n"
        "title: CRLF Test\r\n"
        "type: decision\r\n"
        "date: 2026-01-01\r\n"
        "scope: test\r\n"
        "weight: 1.0\r\n"
        "tags: [crlf]\r\n"
        "---\r\n"
        "# CRLF Test\r\n\r\n"
        "## Constraint\r\nC\r\n\r\n"
        "## Rejected\r\n- alt\r\n\r\n"
        "## Directive\r\nD\r\n"
    )
    p.write_bytes(body.encode("utf-8"))
    lore = parse_lore(p)
    assert lore is not None
    assert lore.title == "CRLF Test"
    assert lore.constraint == "C"
    assert lore.directive == "D"


def test_parse_handles_bom(tmp_path: Path):
    """A file with UTF-8 BOM (Notepad/Excel default) must still parse."""
    p = tmp_path / "bom.md"
    content = (
        "---\n"
        "title: BOM\n"
        "type: decision\n"
        "date: 2026-01-01\n"
        "scope: t\n"
        "weight: 1.0\n"
        "tags: []\n"
        "---\n"
        "# BOM\n\n## Constraint\nC\n\n## Rejected\n- a\n\n## Directive\nD\n"
    )
    p.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))
    lore = parse_lore(p)
    assert lore is not None
    assert lore.title == "BOM"


def test_parse_failure_reason_explains(tmp_path: Path):
    p = tmp_path / "broken.md"
    p.write_text("# Just a heading\nNo frontmatter here.\n")
    reason = parse_failure_reason(p)
    assert reason is not None
    assert "frontmatter" in reason.lower()


def test_index_load_warns_on_skip(tmp_path: Path, capsys):
    _make_lore(tmp_path / "ok.md")
    bad = tmp_path / "broken.md"
    bad.write_text("not a lore file")
    index = LoreIndex.load(tmp_path, warn=True)
    assert len(index.entries) == 1
    assert len(index.skipped) == 1
    captured = capsys.readouterr()
    assert "broken.md" in captured.err


def test_search_text_tokenizes_consistently(tmp_path: Path):
    _make_lore(tmp_path / "a.md", title="Compose recomposition",
               tags=["compose", "performance"], constraint="recomp",
               directive="split")
    _make_lore(tmp_path / "b.md", title="Auth flow",
               tags=["auth"])
    index = LoreIndex.load(tmp_path, warn=False)
    # Free-form text with punctuation, numbers, short words
    results = index.search_text("Compose recomposition, please! (v2 tracking)")
    assert results
    assert results[0].title == "Compose recomposition"


def test_compact_does_not_double_archive(tmp_path: Path):
    """Stale-drop must NOT touch already-deduplicated files (Reviewer A bug fix)."""
    today = date(2026, 5, 1)
    # Two duplicates, both old enough to be stale
    p1 = _make_lore(tmp_path / "old1.md", date_=today - timedelta(days=200),
                    constraint="same", directive="same")
    p2 = _make_lore(tmp_path / "old2.md", date_=today - timedelta(days=210),
                    constraint="same", directive="same")
    index = LoreIndex.load(tmp_path, warn=False)
    report = compact(index, today=today, apply=True)
    # Older one (old2) deduped, newer one (old1) kept then evaluated for stale.
    # Both should NOT appear in stale_archived (old2 already in duplicates;
    # old1 should be stale-archived because it's still > 180d old).
    assert len(report.duplicates_archived) == 1  # old2 archived as dup
    assert len(report.stale_archived) == 1       # old1 then archived as stale
    # No double-counting: stale source path != duplicate source path
    assert (tmp_path / "archive" / "old1.md").exists()
    assert (tmp_path / "archive" / "old2.md").exists()


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
