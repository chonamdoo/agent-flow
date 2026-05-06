"""Lore index — scan, search, fingerprint dedup.

The index is rebuilt fresh on each load; lore corpora are small (<200 files
typical) so caching adds complexity for no gain.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from agent_flow.memory.lore import Lore, parse_failure_reason, parse_lore


@dataclass
class LoreIndex:
    lore_dir: Path
    entries: list[Lore]
    skipped: list[tuple[Path, str]]  # (path, reason) for files that failed to parse

    @classmethod
    def load(cls, lore_dir: Path, *, warn: bool = True) -> "LoreIndex":
        """Scan `lore_dir/*.md` for parseable lore.

        Files that look like lore (`.md` under the lore dir, NOT in archive/)
        but fail to parse are recorded in `skipped` and (if `warn=True`)
        printed to stderr so the user notices typos/format issues instead of
        silently losing entries.
        """
        entries: list[Lore] = []
        skipped: list[tuple[Path, str]] = []
        if not lore_dir.exists():
            return cls(lore_dir=lore_dir, entries=[], skipped=[])
        for f in sorted(lore_dir.glob("*.md")):
            lore = parse_lore(f)
            if lore is not None:
                entries.append(lore)
                continue
            reason = parse_failure_reason(f) or "unknown parse failure"
            skipped.append((f, reason))
            if warn:
                print(
                    f"⚠️  lore skipped: {f.name} — {reason}",
                    file=sys.stderr,
                )
        return cls(lore_dir=lore_dir, entries=entries, skipped=skipped)

    def search(self, keywords: Iterable[str], top_k: int = 5) -> list[Lore]:
        """Naive keyword scoring: count occurrences of each keyword in
        title/scope/tags/constraint/directive. Tie-break by weight.

        Per-keyword score is capped to avoid spam: a haystack with 50
        occurrences of one term doesn't drown three different relevant terms.

        Good enough for ~100s of lore entries. Replace with embedding-based
        search if corpus grows beyond a few thousand.
        """
        kws = [k.lower() for k in keywords if k and k.strip()]
        if not kws:
            return []
        scored: list[tuple[int, Lore]] = []
        for lore in self.entries:
            haystack = lore.search_haystack()
            score = 0
            for kw in kws:
                wb = len(re.findall(rf"\b{re.escape(kw)}\b", haystack))
                sub = haystack.count(kw) - wb
                # Cap per-keyword: 5 word-boundary + 5 substring max
                kw_score = min(wb, 5) * 3 + min(sub, 5)
                score += kw_score
            if score > 0:
                scored.append((score, lore))
        scored.sort(key=lambda pair: (-pair[0], -pair[1].weight, pair[1].path.name))
        return [lore for _, lore in scored[:top_k]]

    def search_text(self, text: str, top_k: int = 5) -> list[Lore]:
        """Convenience wrapper: tokenize free-form text, then search.

        Splits on whitespace and common punctuation. Drops tokens shorter
        than 3 chars and pure-numeric tokens. Same tokenization is used
        from both the runner's auto-cite path and the CLI `lore search`
        command — consolidating here prevents drift.
        """
        tokens = [
            t for t in re.split(r"[\s,/.:;()\[\]{}\"'<>!?]+", text)
            if len(t) >= 3 and not t.isdigit()
        ]
        return self.search(tokens, top_k=top_k)

    def find_duplicate(self, fingerprint: str) -> Lore | None:
        for lore in self.entries:
            if lore.fingerprint == fingerprint:
                return lore
        return None

    def grouped_by_fingerprint(self) -> dict[str, list[Lore]]:
        groups: dict[str, list[Lore]] = {}
        for lore in self.entries:
            groups.setdefault(lore.fingerprint, []).append(lore)
        return groups

    def by_weight(self) -> list[Lore]:
        return sorted(self.entries, key=lambda l: (-l.weight, l.path.name))
