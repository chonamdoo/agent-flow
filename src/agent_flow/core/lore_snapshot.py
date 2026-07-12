from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from pathlib import Path
from typing import Any

from agent_flow.memory.index import LoreIndex
from agent_flow.memory.lore import Lore


LORE_SNAPSHOT_VERSION = 1


def lore_snapshot_metadata(project_root: Path, task: str) -> dict[str, Any]:
    citations = search_lore(project_root, task)
    snapshot = [_serialize_lore(lore, project_root) for lore in citations]
    return {
        "lore_snapshot_version": LORE_SNAPSHOT_VERSION,
        "lore_snapshot_hash": lore_snapshot_hash(snapshot),
        "lore_citations": snapshot,
    }


def reconcile_lore_snapshot(
    meta: dict[str, Any],
    source_root: Path,
    prompt_root: Path,
    *,
    allow_migration: bool,
) -> tuple[dict[str, Any], list[Lore], bool]:
    snapshot = meta.get("lore_citations")
    changed = False
    if snapshot is None and not any(
        key in meta for key in ("lore_snapshot_version", "lore_snapshot_hash")
    ):
        if not allow_migration:
            raise RuntimeError(
                "blocked: a legacy run already emitted a phase prompt; "
                "its original lore citations cannot be reconstructed safely"
            )
        meta = dict(meta)
        meta.update(lore_snapshot_metadata(source_root, str(meta.get("task") or "")))
        meta["lore_snapshot_migrated"] = True
        snapshot = meta["lore_citations"]
        changed = True

    if meta.get("lore_snapshot_version") != LORE_SNAPSHOT_VERSION:
        raise RuntimeError("blocked: active run has an unsupported lore snapshot version")
    if not isinstance(snapshot, list):
        raise RuntimeError("blocked: active run has an invalid lore snapshot")
    if meta.get("lore_snapshot_hash") != lore_snapshot_hash(snapshot):
        raise RuntimeError(
            "blocked: active run lore snapshot changed; restore the run state or start a new run"
        )
    return meta, [_deserialize_lore(item, prompt_root) for item in snapshot], changed


def lore_snapshot_hash(snapshot: object) -> str:
    if not isinstance(snapshot, list):
        raise RuntimeError("blocked: active run has an invalid lore snapshot")
    normalized: list[dict[str, Any]] = []
    for entry in snapshot:
        if not isinstance(entry, dict):
            raise RuntimeError("blocked: active run has an invalid lore citation")
        string_fields = ("constraint", "directive", "path", "title", "type")
        if any(not isinstance(entry.get(field), str) for field in string_fields):
            raise RuntimeError("blocked: active run has an invalid lore citation")
        normalized.append(
            {
                "constraint": entry["constraint"],
                "directive": entry["directive"],
                "path": entry["path"],
                "title": entry["title"],
                "type": entry["type"],
                "weight": _normalized_weight(entry.get("weight")),
            }
        )
    try:
        payload = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError("blocked: active run lore snapshot is not serializable") from exc
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def search_lore(project_root: Path, task: str, top_k: int = 5) -> list[Lore]:
    if not task or not task.strip():
        return []
    lore_dir = project_root / ".agent-flow" / "memory" / "lore"
    index = LoreIndex.load(lore_dir, warn=False)
    if not index.entries:
        return []
    return index.search_text(task, top_k=top_k)


def _serialize_lore(lore: Lore, project_root: Path) -> dict[str, Any]:
    try:
        relative = lore.path.relative_to(project_root)
    except ValueError as exc:
        raise RuntimeError(
            f"blocked: lore citation is outside the project snapshot: {lore.path}"
        ) from exc
    return {
        "path": relative.as_posix(),
        "title": lore.title,
        "type": lore.type,
        "weight": _normalized_weight(lore.weight),
        "constraint": lore.constraint,
        "directive": lore.directive,
    }


def _deserialize_lore(payload: object, project_root: Path) -> Lore:
    if not isinstance(payload, dict):
        raise RuntimeError("blocked: active run has an invalid lore citation")
    raw_path = payload.get("path")
    if not isinstance(raw_path, str) or not raw_path or Path(raw_path).is_absolute():
        raise RuntimeError("blocked: active run has an invalid lore citation path")
    root = project_root.resolve()
    path = (root / raw_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("blocked: active run lore citation escapes the project root") from exc
    string_fields = ("title", "type", "constraint", "directive")
    if any(not isinstance(payload.get(field), str) for field in string_fields):
        raise RuntimeError("blocked: active run has an invalid lore citation")
    return Lore(
        path=path,
        title=payload["title"],
        type=payload["type"],
        date=date.min,
        scope="",
        weight=float(_normalized_weight(payload.get("weight"))),
        tags=[],
        constraint=payload["constraint"],
        rejected=[],
        directive=payload["directive"],
        body="",
    )


def _normalized_weight(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError("blocked: active run has an invalid lore citation weight")
    number = float(value)
    if not math.isfinite(number):
        raise RuntimeError("blocked: active run has an invalid lore citation weight")
    return int(number) if number.is_integer() else number
