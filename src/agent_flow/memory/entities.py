from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import math
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class EntityMemory:
    path: Path
    entity: str
    value: str
    valid_from: date | None
    valid_until: date | None
    confidence: float
    source: str

    def is_stale(self, today: date | None = None) -> bool:
        current = today or date.today()
        return self.valid_until is not None and self.valid_until < current


@dataclass(frozen=True)
class EntityMemoryIndex:
    entries: list[EntityMemory]
    skipped: list[tuple[Path, str]]
    stale: list[EntityMemory]
    conflicts: list[tuple[str, list[EntityMemory]]]

    @classmethod
    def load(cls, memory_dir: Path, *, today: date | None = None) -> "EntityMemoryIndex":
        entries: list[EntityMemory] = []
        skipped: list[tuple[Path, str]] = []
        if memory_dir.exists():
            for path in sorted(memory_dir.glob("*.md")):
                parsed, reason = parse_entity_memory(path)
                if parsed is None:
                    skipped.append((path, reason or "invalid entity memory"))
                else:
                    entries.append(parsed)
        stale = [entry for entry in entries if entry.is_stale(today)]
        return cls(entries=entries, skipped=skipped, stale=stale, conflicts=_find_conflicts(entries, today=today))


def parse_entity_memory(path: Path) -> tuple[EntityMemory | None, str | None]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return None, str(exc)
    frontmatter = _frontmatter(text)
    if frontmatter is None:
        return None, "missing frontmatter"
    entity = frontmatter.get("entity")
    value = frontmatter.get("value")
    if not isinstance(entity, str) or not entity.strip():
        return None, "missing entity"
    if value is None:
        return None, "missing value"
    valid_from, reason = _optional_date(frontmatter, "valid_from")
    if reason is not None:
        return None, reason
    valid_until, reason = _optional_date(frontmatter, "valid_until")
    if reason is not None:
        return None, reason
    confidence, reason = _optional_float(frontmatter, "confidence", 1.0)
    if reason is not None:
        return None, reason
    return EntityMemory(
        path=path,
        entity=entity.strip(),
        value=str(value).strip(),
        valid_from=valid_from,
        valid_until=valid_until,
        confidence=confidence,
        source=str(frontmatter.get("source", "")),
    ), None


def _find_conflicts(entries: list[EntityMemory], *, today: date | None = None) -> list[tuple[str, list[EntityMemory]]]:
    conflicts: list[tuple[str, list[EntityMemory]]] = []
    by_entity: dict[str, list[EntityMemory]] = {}
    for entry in entries:
        by_entity.setdefault(entry.entity.lower(), []).append(entry)
    for entity, group in by_entity.items():
        values = {entry.value for entry in group if not entry.is_stale(today)}
        if len(values) > 1:
            conflicts.append((entity, group))
    return conflicts


def _frontmatter(text: str) -> dict[str, Any] | None:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        return None
    end = normalized.find("\n---\n", 4)
    if end == -1:
        return None
    try:
        payload = yaml.safe_load(normalized[4:end])
    except yaml.YAMLError:
        return None
    return payload if isinstance(payload, dict) else None


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _optional_date(frontmatter: dict[str, Any], key: str) -> tuple[date | None, str | None]:
    if key not in frontmatter or frontmatter.get(key) is None:
        return None, None
    parsed = _to_date(frontmatter.get(key))
    if parsed is None:
        return None, f"invalid {key}"
    return parsed, None


def _optional_float(frontmatter: dict[str, Any], key: str, default: float) -> tuple[float, str | None]:
    if key not in frontmatter or frontmatter.get(key) is None:
        return default, None
    try:
        parsed = float(frontmatter.get(key))
    except (TypeError, ValueError):
        return default, f"invalid {key}"
    if not math.isfinite(parsed):
        return default, f"invalid {key}"
    return parsed, None
