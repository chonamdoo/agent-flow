"""4-tier compaction for the lore index (vuddy lore_system.md ported).

Tier 1: Format normalization — frontmatter fields, date format. Implemented
        as `write_weight` calls when weight drift exceeds threshold.

Tier 2: Dedup — same fingerprint → keep most recent, archive others.

Tier 3: Pattern generalization — surfaces candidate clusters for the user
        to manually consolidate. The tool does NOT auto-merge prose.

Tier 4: Stale drop — entries older than retention by type → archive/.

Retention table (vuddy lore_system.md):
    tradeoff: 180d
    decision: 180d
    failure : 90d
    learning: 90d
    rollback: 60d

Weight decay (used after Tier 2/4):
    age ≤ 30d           → 1.0
    30 < age ≤ 90d      → 0.6
    age > 90d (kept)    → 0.3
    archived            → 0.0 (file moved out)

Compaction is *conservative*: only Tier 2 dedup and Tier 4 stale-drop move
files. Tier 3 reports candidates only. The user runs `agent-flow lore
compact --apply` to commit; `--dry-run` (default) just reports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from agent_flow.memory.index import LoreIndex
from agent_flow.memory.lore import Lore, write_weight


RETENTION_DAYS: dict[str, int] = {
    "tradeoff": 180,
    "decision": 180,
    "failure": 90,
    "learning": 90,
    "rollback": 60,
}
DEFAULT_RETENTION_DAYS = 90  # for unknown types


@dataclass
class CompactReport:
    duplicates_archived: list[tuple[Path, Path]] = field(default_factory=list)
    stale_archived: list[Path] = field(default_factory=list)
    weight_updated: list[tuple[Path, float, float]] = field(default_factory=list)
    generalization_candidates: list[list[Lore]] = field(default_factory=list)
    dry_run: bool = True

    def summary(self) -> str:
        prefix = "[dry-run] " if self.dry_run else ""
        lines = [
            f"{prefix}Duplicates archived: {len(self.duplicates_archived)}",
            f"{prefix}Stale archived    : {len(self.stale_archived)}",
            f"{prefix}Weight updated    : {len(self.weight_updated)}",
            f"{prefix}Generalization candidates: "
            f"{sum(len(g) for g in self.generalization_candidates)} entries "
            f"in {len(self.generalization_candidates)} cluster(s)",
        ]
        return "\n".join(lines)


def compact(index: LoreIndex, today: date | None = None,
            apply: bool = False) -> CompactReport:
    today = today or date.today()
    archive_dir = index.lore_dir / "archive"
    if apply:
        archive_dir.mkdir(parents=True, exist_ok=True)

    report = CompactReport(dry_run=not apply)

    # Tier 2: dedup by fingerprint
    for _fingerprint, group in index.grouped_by_fingerprint().items():
        if len(group) <= 1:
            continue
        group.sort(key=lambda lore: (lore.date, lore.path.name), reverse=True)
        kept = group[0]
        for archived in group[1:]:
            target = _archive_target(archive_dir, archived.path)
            if apply and archived.path.exists():
                archived.path.rename(target)
            report.duplicates_archived.append((kept.path, target))

    # Tier 4: stale drop (skip files already moved by Tier 2)
    # Track *source* paths separately so Tier 1 can correctly skip them.
    # report.stale_archived stores the destination (archive/) paths for the
    # user-facing report; for the survivors check we need the originals.
    duplicate_source_paths = {
        # Group above iterated `archived in group[1:]`; track those originals.
        original_archived.path
        for _fingerprint, group in index.grouped_by_fingerprint().items() if len(group) > 1
        for original_archived in sorted(group, key=lambda lore: (lore.date, lore.path.name), reverse=True)[1:]
    }
    stale_source_paths: set[Path] = set()
    for lore in index.entries:
        if lore.path in duplicate_source_paths or not lore.path.exists():
            continue
        retention = RETENTION_DAYS.get(lore.type, DEFAULT_RETENTION_DAYS)
        age = (today - lore.date).days
        if age > retention:
            target = _archive_target(archive_dir, lore.path)
            if apply:
                lore.path.rename(target)
            report.stale_archived.append(target)
            stale_source_paths.add(lore.path)

    # Tier 1: weight decay (only for survivors — exclude both dedup-archived
    # and stale-archived source paths)
    just_archived_sources = duplicate_source_paths | stale_source_paths
    for lore in index.entries:
        if lore.path in just_archived_sources or not lore.path.exists():
            continue
        new_weight = _calc_weight((today - lore.date).days)
        if abs(new_weight - lore.weight) > 0.05:
            if apply:
                write_weight(lore.path, new_weight)
            report.weight_updated.append((lore.path, lore.weight, new_weight))

    # Tier 3: surface generalization candidates (clusters with similar tags)
    report.generalization_candidates = _cluster_candidates(index)

    return report


def _calc_weight(age_days: int) -> float:
    if age_days <= 30:
        return 1.0
    if age_days <= 90:
        return 0.6
    return 0.3


def _archive_target(archive_dir: Path, source: Path) -> Path:
    target = archive_dir / source.name
    if not target.exists():
        return target
    stem = source.stem
    suffix = source.suffix
    counter = 1
    while True:
        candidate = archive_dir / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _cluster_candidates(index: LoreIndex,
                        min_cluster_size: int = 3) -> list[list[Lore]]:
    """Group lore by shared tag. Clusters of `min_cluster_size`+ entries
    are flagged as generalization candidates.
    """
    by_tag: dict[str, list[Lore]] = {}
    for lore in index.entries:
        for tag in lore.tags:
            by_tag.setdefault(tag.lower(), []).append(lore)
    clusters: list[list[Lore]] = []
    seen_keys: set[tuple[str, ...]] = set()
    for group in by_tag.values():
        if len(group) < min_cluster_size:
            continue
        # Deduplicate by lore-set (a cluster shared by 2 tags is reported once)
        key = tuple(sorted(lore.path.name for lore in group))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        clusters.append(group)
    return clusters
