"""설치된 skill 카탈로그의 관측 기록과 드리프트 진단.

lock은 재현 핀이 아니다. 외부 skill은 우리가 설치하지 않으므로(`install_policy: never`)
어느 커밋을 받을지 우리가 정할 수 없다. lock의 유일한 일은 "지난번 스캔에 무엇이 있었나"를
남겨 다음 스캔이 신규와 삭제를 이름으로 지목하게 하는 것이다.

`version`을 첫 커밋부터 넣는다. 스키마가 바뀔 때 옛 lock을 읽고 죽는 대신 버리게 된다.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from agent_flow.core.profile_routing import routable_group_skills
from agent_flow.core.skill_matching import routable_names
from agent_flow.core.skill_resolver import (
    SkillCatalogEntry,
    active_host,
    catalog_files,
    catalog_stamp,
    discover_skill_catalog,
    entry_can_activate,
    expand_dependencies,
    skill_roots,
)

LOCK_VERSION = 1
LOCK_RELATIVE = Path(".agent-flow") / "skills" / "catalog.lock.json"

# doctor가 내는 판정 종류. 문자열은 grep 대상이므로 계약이다.
NEW = "new"
REMOVED = "removed"
DEAD_DECLARATION = "dead-declaration"
UNROUTED = "unrouted"
UNOWNED_ADAPTER = "unowned-adapter"
SHADOWED = "shadowed"
COLLISION = "collision"

# 스스로 "reference-only, not a standalone workflow skill"이라고 선언한 이름. 다른 skill이
# 파일 경로로 인용해 쓰므로 phase 활성화 대상이 아니고, 미라우팅 보고에서 제외한다.
_ROUTING_EXEMPT = frozenset({"android-guides"})

# kit이 설치하는 자리는 `<root>/.omp/extensions/`뿐이다. 아래는 kit이 만들지도
# 갱신하지도 않는 전역 자리인데, 여기에 kit 표지를 단 파일이 있으면 재설치로도
# 낫지 않는 상태가 된다 — 실측으로 미병합 브랜치가 남긴 전역 adapter 하나가
# 저장소의 모든 tool 호출을 막았고, kit은 그 경로를 아예 몰라 진단조차 못 했다.
# 소유를 증명할 수 없으므로 지우지 않는다. 이름으로 지목하는 것까지가 이 진단이다.
_UNMANAGED_GLOBAL_ADAPTERS = (Path(".omp") / "agent" / "extensions" / "agent-flow-hooks.ts",)
_OMP_EXTENSION_MARKER = "agent-flow: managed omp extension"


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
    workflow_skills: Sequence[str] = (),
    home: Path | None = None,
) -> CatalogScan:
    resolved_host = active_host(env) if host is None else host
    roots = skill_roots(project_root, profile=profile, host=resolved_host, env=env)
    files = catalog_files(roots)
    entries = discover_skill_catalog(project_root, roots)
    shadowed = _shadowed_files(files, entries)
    findings = list(_lock_findings(read_lock(project_root), entries))
    findings.extend(_declaration_findings(profile, entries))
    findings.extend(_unrouted_findings(profile, entries, workflow_skills))
    findings.extend(_collision_findings(files, entries))
    findings.extend(_unowned_adapter_findings(Path.home() if home is None else home))
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



# marker는 파일 첫 줄에 있다. 전체를 읽을 이유가 없고, 상한이 없으면 진단이 임의 크기
# 파일에 끌려간다.
_ADAPTER_HEAD_BYTES = 64 * 1024


def _read_plain_file_head(path: Path) -> str | None:
    """열어 둔 descriptor로 검증하고 앞부분만 읽는다.

    `lstat` 후 경로를 다시 열면 그 사이에 symlink나 FIFO로 바뀔 수 있다. 그러면 이
    자리에 없는 남의 파일로 소유권을 판정하거나 열기에서 멈춘다. `O_NOFOLLOW`로 링크를
    거부하고 `O_NONBLOCK`으로 FIFO 대기를 끊은 뒤, 같은 descriptor를 `fstat`으로 본다.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        return os.read(descriptor, _ADAPTER_HEAD_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _unowned_adapter_findings(home: Path) -> list[CatalogFinding]:
    """kit 표지를 달았지만 kit이 관리하지 않는 자리에 있는 host adapter."""
    findings: list[CatalogFinding] = []
    for relative in _UNMANAGED_GLOBAL_ADAPTERS:
        path = home / relative
        text = _read_plain_file_head(path)
        if text is None:
            continue
        if _OMP_EXTENSION_MARKER not in text:
            continue
        findings.append(
            CatalogFinding(
                UNOWNED_ADAPTER,
                str(path),
                "kit이 설치하지 않는 자리에 kit 표지를 단 adapter가 있다. "
                "install이 갱신하지 못하므로 kit과 어긋난 채로 tool 호출을 막을 수 있다",
                "이 파일의 출처를 확인하고 직접 옮기거나 지운다. install은 이 경로를 건드리지 않는다",
            )
        )
    return findings


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
    profile: dict | None,
    entries: Sequence[SkillCatalogEntry],
    workflow_skills: Sequence[str] = (),
) -> list[CatalogFinding]:
    """어떤 활성화 경로에도 걸리지 않는 설치본.

    축은 네 개다: profile 표(selectors 있는 group), 어휘 라우팅, workflow phase 선언,
    그리고 이 셋에 걸린 skill이 끌어오는 dependencies. 축을 조건식 분기로 두면
    축이 늘 때마다 분기가 늘어난다 — 그래서 합집합 하나로 표현한다.

    `bundled`/`project`를 검사에서 빼 두는 동안 profile이 설치하는 25개 중 21개가
    어느 phase에도 붙지 않은 채로 남았다. source는 보고 문구에만 쓴다.
    """
    # str 하나를 넘긴 호출을 문자 단위로 순회하면 모든 이름이 미라우팅으로 뒤집힌다.
    declared_raw = [workflow_skills] if isinstance(workflow_skills, str) else list(workflow_skills)
    declared = set(_string_list(declared_raw))
    reachable = set(
        expand_dependencies(
            sorted(
                routable_group_skills(profile)
                | routable_names(profile, entries)
                | declared
                | {entry.name for entry in entries if entry_can_activate(entry)}
            ),
            entries,
        )
    )
    return [
        CatalogFinding(
            UNROUTED,
            entry.name,
            f"{entry.source} root에 있으나 어떤 활성화 경로에도 걸리지 않는다",
            "표에 selectors를 주거나 SKILL.md frontmatter에 workflowPhases/pathGlobs를 선언한다",
        )
        for entry in entries
        if entry.name not in reachable and entry.name not in _ROUTING_EXEMPT
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
