"""Lore file model + parser.

A lore file is markdown with YAML frontmatter:

    ---
    title: 짧은 제목
    type: decision | failure | learning | rollback | tradeoff
    date: YYYY-MM-DD
    scope: feature/<module>
    weight: 1.0
    tags: [keyword, ...]
    ---
    # title

    ## Context
    ...

    ## Constraint
    ...

    ## Outcome
    ...

    ## Rejected
    - alt-A: reason
    - alt-B: reason

    ## Directive
    ...

The 3 load-bearing fields are Constraint / Rejected / Directive. They drive
the dedup fingerprint, search relevance, and the rule extracted for future
similar situations.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Lore:
    path: Path
    title: str
    type: str
    date: date
    scope: str
    weight: float
    tags: list[str]
    constraint: str
    rejected: list[str]
    directive: str
    body: str = field(repr=False)

    @property
    def fingerprint(self) -> str:
        """16-char sha256 of normalized (Constraint + Rejected + Directive).

        Used for dedup: two lore entries with the same fingerprint are
        candidates to merge / archive.
        """
        normalized = "\n".join(
            [self.constraint, *self.rejected, self.directive]
        ).strip()
        normalized = re.sub(r"\s+", " ", normalized.lower())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    def search_haystack(self) -> str:
        """All searchable text concatenated, lowercased."""
        return " ".join([
            self.title,
            self.scope,
            " ".join(self.tags),
            self.constraint,
            " ".join(self.rejected),
            self.directive,
        ]).lower()


def parse_lore(path: Path) -> Lore | None:
    """Read a lore file. Returns None if frontmatter is missing/invalid.

    Tolerant of file-system quirks:
      - utf-8-sig handles BOM (Excel/Notepad default)
      - CRLF line endings normalized to LF before frontmatter detection
      - missing optional fields default sensibly

    Section headers `## Constraint` / `## Rejected` / `## Directive` are
    required for fingerprint/search to be meaningful but missing sections
    degrade rather than crash (empty string / empty list).
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    fm, body = _split_frontmatter(text)
    if fm is None:
        return None

    return Lore(
        path=path,
        title=str(fm.get("title", path.stem)),
        type=str(fm.get("type", "decision")),
        date=_to_date(fm.get("date")),
        scope=str(fm.get("scope", "")),
        weight=_to_float(fm.get("weight", 1.0)),
        tags=_to_str_list(fm.get("tags", [])),
        constraint=_section(body, "Constraint"),
        rejected=_list_section(body, "Rejected"),
        directive=_section(body, "Directive"),
        body=body,
    )


def parse_failure_reason(path: Path) -> str | None:
    """Diagnose why parse_lore would return None for `path`.

    Returns a one-line human-readable reason, or None if parse_lore would
    actually succeed. Used by the index loader to print actionable warnings
    when files in the lore directory are silently skipped.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        return f"unreadable: {e}"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        return "missing frontmatter (file must start with `---` on line 1)"
    end = text.find("\n---\n", 4)
    if end == -1:
        return "frontmatter not closed (need `---` on its own line)"
    try:
        fm = yaml.safe_load(text[4:end])
    except yaml.YAMLError as e:
        return f"frontmatter YAML invalid: {e}"
    if not isinstance(fm, dict):
        return "frontmatter is not a YAML mapping"
    return None


def write_weight(path: Path, new_weight: float) -> None:
    """Rewrite the `weight:` field in a lore file's frontmatter in place.

    Preserves the rest of the frontmatter and body verbatim.
    """
    text = path.read_text(encoding="utf-8")
    fm, _body = _split_frontmatter(text)
    if fm is None:
        return
    end = text.find("\n---\n", 4)
    frontmatter = text[:end]
    rest = text[end:]
    pattern = re.compile(r"^weight:\s*[\d.]+\s*$", re.MULTILINE)
    if pattern.search(frontmatter):
        out = pattern.sub(
            f"weight: {new_weight:.1f}", frontmatter, count=1
        ) + rest
    else:
        out = frontmatter + f"\nweight: {new_weight:.1f}" + rest
    path.write_text(out, encoding="utf-8")


# ─────────────────────────── helpers ──────────────────────────────


def _split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    try:
        fm = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None, text
    body = text[end + 5:]
    return (fm if isinstance(fm, dict) else None), body


def _section(body: str, name: str) -> str:
    """Extract content under '## <name>' until next '## ' header or EOF."""
    pattern = rf"^##\s+{re.escape(name)}\s*\n(.*?)(?=^##\s+|\Z)"
    m = re.search(pattern, body, re.DOTALL | re.MULTILINE)
    return m.group(1).strip() if m else ""


def _list_section(body: str, name: str) -> list[str]:
    section = _section(body, name)
    items = re.findall(r"^[-*]\s+(.+?)$", section, re.MULTILINE)
    return [i.strip() for i in items]


def _to_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            pass
    return date.today()


def _to_float(value: Any, default: float = 1.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        return [value]
    return []
