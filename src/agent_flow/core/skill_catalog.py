"""설치된 skill 카탈로그의 관측 기록과 드리프트 진단.

lock은 재현 핀이 아니다. 외부 skill은 우리가 설치하지 않으므로(`install_policy: never`)
어느 커밋을 받을지 우리가 정할 수 없다. lock의 유일한 일은 "지난번 스캔에 무엇이 있었나"를
남겨 다음 스캔이 신규와 삭제를 이름으로 지목하게 하는 것이다.

`version`을 첫 커밋부터 넣는다. 스키마가 바뀔 때 옛 lock을 읽고 죽는 대신 버리게 된다.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from agent_flow.core.skill_resolver import (
    SkillCatalogEntry,
    active_host,
    catalog_files,
    catalog_stamp,
    discover_skill_catalog,
    skill_roots,
)

LOCK_VERSION = 1
LOCK_RELATIVE = Path(".agent-flow") / "skills" / "catalog.lock.json"

# doctor가 내는 판정 종류. 문자열은 grep 대상이므로 계약이다.
NEW = "new"
REMOVED = "removed"
DEAD_DECLARATION = "dead-declaration"
UNROUTED = "unrouted"
SHADOWED = "shadowed"
COLLISION = "collision"


@dataclass(frozen=True)
class CatalogFinding:
    kind: str
    name: str
    detail: str = ""
    fix: str = ""


@dataclass(frozen=True)
class CatalogScan:
    stamp: str
    entries: tuple[SkillCatalogEntry, ...] = ()
    findings: tuple[CatalogFinding, ...] = ()
    shadowed: tuple[tuple[str, str], ...] = ()

    def by_source(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.source] = counts.get(entry.source, 0) + 1
        return counts


def lock_path(project_root: Path) -> Path:
    return project_root / LOCK_RELATIVE


def read_lock(project_root: Path) -> dict:
    try:
        payload = json.loads(lock_path(project_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != LOCK_VERSION:
        return {}
    return payload


def scan(
    project_root: Path,
    *,
    profile: dict | None = None,
    host: str | None = None,
    env: dict[str, str] | None = None,
) -> CatalogScan:
    resolved_host = active_host(env) if host is None else host
    roots = skill_roots(project_root, profile=profile, host=resolved_host, env=env)
    files = catalog_files(roots)
    entries = discover_skill_catalog(project_root, roots)
    shadowed = _shadowed_files(files, entries)
    findings = list(_lock_findings(read_lock(project_root), entries))
    findings.extend(_declaration_findings(profile, entries))
    findings.extend(_unrouted_findings(profile, entries))
    findings.extend(_collision_findings(files, entries))
    if shadowed:
        findings.append(
            CatalogFinding(
                SHADOWED,
                f"{len(shadowed)} file(s)",
                "같은 파일이 여러 root에 걸린다 (symlink 또는 중복 설치)",
            )
        )
    return CatalogScan(
        stamp=catalog_stamp(files),
        entries=entries,
        findings=tuple(findings),
        shadowed=shadowed,
    )


def _collision_findings(
    files: Sequence[tuple[str, Path]], entries: Sequence[SkillCatalogEntry]
) -> list[CatalogFinding]:
    """같은 이름이 서로 다른 파일로 여러 root에 있다.

    카탈로그는 우선순위 root의 것 하나만 담고 나머지를 조용히 버린다. 프로젝트가
    설치된 외부 skill과 같은 이름을 쓰면 그 그림자가 보이지 않는다.
    """
    chosen = {entry.name: os.path.realpath(entry.path) for entry in entries}
    seen: dict[str, set[str]] = {}
    for _source, skill_path in files:
        name = skill_path.parent.name
        if name not in chosen:
            continue
        seen.setdefault(name, set()).add(os.path.realpath(skill_path))
    return [
        CatalogFinding(
            COLLISION,
            name,
            f"{len(paths)}곳에 서로 다른 파일로 있다. 쓰이는 것은 {chosen[name]}",
            "이름을 바꾸거나 중복 사본을 지운다",
        )
        for name, paths in sorted(seen.items())
        if len(paths) > 1
    ]


def write_lock(project_root: Path, result: CatalogScan) -> Path:
    path = lock_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": LOCK_VERSION,
        "stamp": result.stamp,
        "skills": {
            entry.name: {
                "path": str(entry.path),
                "source": entry.source,
                "keywords": list(entry.keywords),
            }
            for entry in sorted(result.entries, key=lambda item: item.name)
        },
    }
    path.write_text(f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n", encoding="utf-8")
    return path


def declared_skill_names(profile: dict | None) -> tuple[str, ...]:
    """profile이 **이름으로** 박아 둔 skill 전부.

    `required_review`의 literal 목록, 그리고 이름을 열거하는 어떤 표든 함께 본다.
    이 집합이 디스크와 어긋나는 것이 우리가 겪은 드리프트의 정의다 — 그래서 표를
    지운 뒤에도 남는 유일한 열거 지점(우리가 소유한 bundled skill)을 계속 감시한다.
    """
    if not isinstance(profile, dict):
        return ()
    names: list[str] = []
    skills = profile.get("skills")
    if isinstance(skills, dict):
        for group in skills.get("required_review") or []:
            if isinstance(group, dict):
                names.extend(_string_list(group.get("skills")))
    for value in profile.values():
        if not isinstance(value, dict):
            continue
        for section in ("implementation", "review"):
            for item in value.get(section) or []:
                if isinstance(item, dict) and item.get("skill"):
                    names.append(str(item["skill"]).strip())
    return tuple(dict.fromkeys(name for name in names if name))


def _lock_findings(previous: dict, entries: Sequence[SkillCatalogEntry]) -> list[CatalogFinding]:
    recorded = previous.get("skills")
    if not isinstance(recorded, dict):
        return []
    current = {entry.name for entry in entries}
    findings: list[CatalogFinding] = []
    for name in sorted(current - set(recorded)):
        findings.append(CatalogFinding(NEW, name, "지난 스캔 이후 설치됨"))
    for name in sorted(set(recorded) - current):
        findings.append(CatalogFinding(REMOVED, name, "지난 스캔에는 있었고 지금은 없다"))
    return findings


def _declaration_findings(
    profile: dict | None, entries: Sequence[SkillCatalogEntry]
) -> list[CatalogFinding]:
    installed = {entry.name for entry in entries}
    return [
        CatalogFinding(
            DEAD_DECLARATION,
            name,
            "profile 선언에 있으나 디스크에 없다",
            "선언에서 지우거나 해당 skill을 설치한다",
        )
        for name in declared_skill_names(profile)
        if name not in installed
    ]


def _unrouted_findings(
    profile: dict | None, entries: Sequence[SkillCatalogEntry]
) -> list[CatalogFinding]:
    # 지연 import: skill_matching이 resolver를 되짚어 참조한다.
    from agent_flow.core.skill_matching import routable_names

    reachable = set(declared_skill_names(profile)) | routable_names(profile, entries)
    return [
        CatalogFinding(
            UNROUTED,
            entry.name,
            f"{entry.source} root에 있으나 어떤 어휘에도 걸리지 않는다",
        )
        for entry in entries
        if entry.source in {"host", "shared", "fetched", "vendor"} and entry.name not in reachable
    ]


def _shadowed_files(
    files: Sequence[tuple[str, Path]], entries: Sequence[SkillCatalogEntry]
) -> tuple[tuple[str, str], ...]:
    """카탈로그가 채택한 파일을 가리키는 별칭 경로. 이름 dedupe는 조용하므로 여기서 남긴다."""
    chosen = {os.path.realpath(entry.path) for entry in entries}
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for _source, skill_path in files:
        real = os.path.realpath(skill_path)
        if real not in chosen or str(skill_path) == real:
            continue
        key = (str(skill_path), real)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return tuple(out)


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []
