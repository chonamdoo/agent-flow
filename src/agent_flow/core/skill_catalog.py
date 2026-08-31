"""설치된 skill 카탈로그의 관측 기록과 드리프트 진단.

lock은 재현 핀이나 설치 소유권 기록이 아니다. host/profile별로 마지막 관측 위치,
콘텐츠, 거버넌스, upstream ref와 실제 해석 SHA를 남겨 다음 진단에서 드리프트를
지목한다. 설치 인덱스의 ``hash`` 소유권 판정과는 별개다.
"""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import AbstractSet, Sequence

from agent_flow.core.atomic_io import atomic_write_text, read_bounded_regular_file
from agent_flow.core.profile_routing import routable_group_skills
from agent_flow.core.skill_matching import routable_names
from agent_flow.core.skill_resolver import (
    INVALID_GOVERNANCE_SCALAR,
    INVALID_FRONTMATTER,
    OWNED_SOURCES,
    SkillCatalogEntry,
    active_host,
    active_host_roots,
    catalog_files,
    catalog_stamp,
    discover_skill_catalog,
    entry_can_activate,
    expand_dependencies,
    skill_observed_content_digest,
    skill_roots,
)
from agent_flow.core.skill_sync import cached_source_sha, parse_skill_sources
from agent_flow.core.worktree_isolation import (
    FileLeaseUnavailable,
    exclusive_file_lease,
)

LOCK_VERSION = 2
LOCK_RELATIVE = Path(".agent-flow") / "skills" / "catalog.lock.json"
LOCK_WRITE_LEASE_RELATIVE = Path(".agent-flow") / "skills" / "catalog.lock.write"

# doctor가 내는 판정 종류. 문자열은 grep 대상이므로 계약이다.
NEW = "new"
REMOVED = "removed"
NEW_VIEW = "new-view"
CONTENT_CHANGED = "content-changed"
CONTENT_UNREADABLE = "content-unreadable"
LOCATION_CHANGED = "location-changed"
GOVERNANCE_CHANGED = "governance-changed"
SOURCE_CHANGED = "source-changed"
LOCK_INVALID = "lock-invalid"
LOCK_UNREADABLE = "lock-unreadable"
LOCK_STALE = "lock-stale"
INVALID_GOVERNANCE = "invalid-governance"
RETIRED_ROUTED = "retired-routed"
UNAPPROVED_ROUTED = "unapproved-routed"
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
    strict: bool = True


@dataclass(frozen=True)
class CatalogScan:
    stamp: str
    entries: tuple[SkillCatalogEntry, ...] = ()
    findings: tuple[CatalogFinding, ...] = ()
    shadowed: tuple[tuple[str, str], ...] = ()
    host: str = ""
    profile_ids: tuple[str, ...] = ()
    sources: dict[str, dict[str, str]] = field(default_factory=dict)
    skills: dict[str, dict[str, object]] = field(default_factory=dict)

    @property
    def view_id(self) -> str:
        return _view_id(self.host, self.profile_ids)

    def by_source(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.source] = counts.get(entry.source, 0) + 1
        return counts


@dataclass(frozen=True)
class _LockState:
    status: str
    payload: dict[str, object] = field(default_factory=dict)


def lock_path(project_root: Path) -> Path:
    return project_root / LOCK_RELATIVE


def read_lock(project_root: Path) -> dict:
    state = _load_lock(project_root)
    return state.payload if state.status == "valid" else {}


def _load_lock(project_root: Path) -> _LockState:
    try:
        raw, _size = read_bounded_regular_file(
            lock_path(project_root),
            max_bytes=8 * 1024 * 1024,
        )
    except FileNotFoundError:
        return _LockState("absent")
    except OSError:
        return _LockState("unreadable")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _LockState("corrupt")
    if not isinstance(payload, dict):
        return _LockState("corrupt")
    if payload.get("version") != LOCK_VERSION:
        return _LockState("stale")
    if not _valid_lock_payload(payload):
        return _LockState("corrupt")
    return _LockState("valid", payload)


def _valid_lock_payload(payload: dict[str, object]) -> bool:
    views = payload.get("views")
    if not isinstance(views, dict):
        return False
    for view_id, view in views.items():
        if not isinstance(view_id, str) or not isinstance(view, dict):
            return False
        host = view.get("host")
        profiles = view.get("profiles")
        if not isinstance(host, str):
            return False
        if not isinstance(profiles, list) or not all(
            isinstance(item, str) for item in profiles
        ):
            return False
        if view_id != _view_id(host, profiles):
            return False
        if not isinstance(view.get("stamp"), str):
            return False
        sources = view.get("sources")
        skills = view.get("skills")
        if not isinstance(sources, dict) or not all(
            isinstance(name, str) and _valid_source_record(record)
            for name, record in sources.items()
        ):
            return False
        if not isinstance(skills, dict) or not all(
            isinstance(name, str) and _valid_skill_record(record)
            for name, record in skills.items()
        ):
            return False
    return True


def _valid_source_record(record: object) -> bool:
    return isinstance(record, dict) and all(
        isinstance(record.get(field), str)
        for field in ("kind", "url", "ref", "resolvedSha")
    )


def _valid_skill_record(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    if not all(
        isinstance(record.get(field), str)
        for field in ("path", "source", "observedContentDigest")
    ):
        return False
    governance = record.get("governance")
    return isinstance(governance, dict) and all(
        isinstance(governance.get(field), str)
        for field in ("version", "owner", "lifecycle", "approval", "provenance")
    )


def scan(
    project_root: Path,
    *,
    profile: dict | None = None,
    profile_ids: Sequence[str] = (),
    host: str | None = None,
    env: dict[str, str] | None = None,
    workflow_skills: Sequence[str] = (),
    home: Path | None = None,
) -> CatalogScan:
    resolved_host = active_host(env) if host is None else host
    resolved_profiles = _resolved_profile_ids(profile, profile_ids)
    roots = skill_roots(project_root, profile=profile, host=resolved_host, env=env)
    view_roots = active_host_roots(roots, resolved_host)
    files = catalog_files(roots)
    view_files = catalog_files(view_roots)
    entries = discover_skill_catalog(project_root, roots)
    view_entries = discover_skill_catalog(project_root, view_roots)
    stamp = catalog_stamp(files)
    sources = _source_observations(profile, env)
    skills, content_findings = _skill_observations(project_root, view_entries)
    unreadable_names = {
        finding.name
        for finding in content_findings
        if finding.kind == CONTENT_UNREADABLE
    }
    shadowed = _shadowed_files(files, entries)
    lock_state = _load_lock(project_root)
    findings = list(content_findings)
    findings.extend(
        _lock_findings(
            lock_state,
            _view_id(resolved_host, resolved_profiles),
            skills,
            sources,
            unreadable_names=unreadable_names,
        )
    )
    reachable = _reachable_names(profile, entries, workflow_skills)
    view_reachable = _reachable_names(profile, view_entries, workflow_skills)
    findings.extend(_declaration_findings(profile, view_entries))
    findings.extend(_unrouted_findings(entries, reachable))
    findings.extend(_governance_findings(view_entries, view_reachable))
    findings.extend(_collision_findings(view_files, view_entries))
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
        stamp=stamp,
        entries=entries,
        findings=tuple(findings),
        shadowed=shadowed,
        host=resolved_host,
        profile_ids=resolved_profiles,
        sources=sources,
        skills=skills,
    )


def _resolved_profile_ids(
    profile: dict | None, profile_ids: Sequence[str]
) -> tuple[str, ...]:
    raw_profile_ids = (
        [profile_ids] if isinstance(profile_ids, str) else profile_ids
    )
    resolved = [str(item).strip() for item in raw_profile_ids if str(item).strip()]
    if not resolved and isinstance(profile, dict):
        profile_id = str(profile.get("id") or "").strip()
        if profile_id:
            resolved.append(profile_id)
    return tuple(dict.fromkeys(resolved))


def _view_id(host: str, profile_ids: Sequence[str]) -> str:
    return json.dumps(
        [host or "unknown", *profile_ids],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _source_observations(
    profile: dict | None, env: dict[str, str] | None
) -> dict[str, dict[str, str]]:
    return {
        source.id: {
            "kind": source.kind,
            "url": source.url,
            "ref": source.ref,
            "resolvedSha": cached_source_sha(source, env=env),
        }
        for source in parse_skill_sources(profile)
    }


def _skill_observations(
    project_root: Path, entries: Sequence[SkillCatalogEntry]
) -> tuple[dict[str, dict[str, object]], list[CatalogFinding]]:
    observations: dict[str, dict[str, object]] = {}
    findings: list[CatalogFinding] = []
    for entry in sorted(entries, key=lambda item: item.name):
        try:
            content_digest = skill_observed_content_digest(entry.path.parent)
        except OSError as exc:
            findings.append(
                CatalogFinding(
                    CONTENT_UNREADABLE,
                    entry.name,
                    str(exc),
                    "skill 디렉터리와 배포 파일의 읽기 권한 및 동시 수정을 확인한다",
                )
            )
            continue
        observations[entry.name] = {
            "path": _recorded_path(project_root, entry.path),
            "source": entry.source,
            "observedContentDigest": content_digest,
            "governance": {
                "version": entry.version,
                "owner": entry.owner,
                "lifecycle": entry.lifecycle,
                "approval": entry.approval,
                "provenance": entry.provenance,
            },
        }
    return observations, findings


def _recorded_path(project_root: Path, skill_path: Path) -> str:
    try:
        return skill_path.absolute().relative_to(
            project_root.absolute()
        ).as_posix()
    except ValueError:
        return str(skill_path)


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
    if any(finding.kind == CONTENT_UNREADABLE for finding in result.findings):
        raise ValueError("catalog observations include unreadable skill content")
    lease_path = project_root / LOCK_WRITE_LEASE_RELATIVE
    try:
        with exclusive_file_lease(lease_path, wait=True):
            state = _load_lock(project_root)
            if state.status in {"corrupt", "unreadable"}:
                raise ValueError(
                    f"catalog lock is {state.status}: {lock_path(project_root)}"
                )
            views: dict[str, object] = {}
            if state.status == "valid":
                recorded = state.payload.get("views")
                if isinstance(recorded, dict):
                    views.update(recorded)
            views[result.view_id] = {
                "host": result.host,
                "profiles": list(result.profile_ids),
                "stamp": result.stamp,
                "sources": result.sources,
                "skills": result.skills,
            }
            payload = {"version": LOCK_VERSION, "views": views}
            path = lock_path(project_root)
            atomic_write_text(
                path,
                f"{json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)}\n",
            )
            return path
    except FileLeaseUnavailable as exc:
        raise ValueError(f"catalog lock lease unavailable: {lease_path}") from exc


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


def _lock_findings(
    previous: _LockState,
    view_id: str,
    current: dict[str, dict[str, object]],
    current_sources: dict[str, dict[str, str]],
    *,
    unreadable_names: AbstractSet[str] = frozenset(),
) -> list[CatalogFinding]:
    if previous.status in {"corrupt", "unreadable"}:
        kind = LOCK_INVALID if previous.status == "corrupt" else LOCK_UNREADABLE
        return [
            CatalogFinding(
                kind,
                str(view_id),
                "catalog lock을 안전하게 읽을 수 없다",
                "파일 출처와 읽기 권한을 확인한 뒤 다시 진단한다",
            )
        ]
    if previous.status == "stale":
        return [
            CatalogFinding(
                LOCK_STALE,
                str(view_id),
                "catalog lock schema가 현재 버전과 다르다",
                "skills scan으로 현재 schema 관측을 기록한다",
            )
        ]
    if previous.status != "valid":
        return []
    views = previous.payload["views"]
    assert isinstance(views, dict)
    recorded_view = views.get(view_id)
    if not isinstance(recorded_view, dict):
        return [
            CatalogFinding(
                NEW_VIEW,
                view_id,
                "이 host/profile 조합은 이전 관측 기록에 없다",
                "skills scan으로 현재 관측을 승인한다",
            )
        ]
    recorded = recorded_view["skills"]
    recorded_sources = recorded_view["sources"]
    assert isinstance(recorded, dict)
    assert isinstance(recorded_sources, dict)
    findings: list[CatalogFinding] = []
    for name in sorted(set(current) - set(recorded)):
        findings.append(CatalogFinding(NEW, name, "지난 스캔 이후 설치됨"))
    for name in sorted(set(recorded) - set(current) - unreadable_names):
        findings.append(
            CatalogFinding(REMOVED, name, "지난 스캔에는 있었고 지금은 없다")
        )
    for name in sorted(set(recorded) & set(current)):
        before = recorded[name]
        after = current[name]
        if not isinstance(before, dict):
            findings.append(
                CatalogFinding(LOCK_INVALID, name, "skill 관측 레코드가 손상됨")
            )
            continue
        if before.get("path") != after.get("path"):
            findings.append(
                CatalogFinding(
                    LOCATION_CHANGED,
                    name,
                    f"{before.get('path', '')} -> {after.get('path', '')}",
                )
            )
        if before.get("source") != after.get("source"):
            findings.append(
                CatalogFinding(
                    SOURCE_CHANGED,
                    name,
                    f"{before.get('source', '')} -> {after.get('source', '')}",
                )
            )
        if before.get("observedContentDigest") != after.get(
            "observedContentDigest"
        ):
            findings.append(
                CatalogFinding(
                    CONTENT_CHANGED,
                    name,
                    "SKILL.md 또는 함께 배포되는 참조 파일이 바뀜",
                )
            )
        if before.get("governance") != after.get("governance"):
            findings.append(
                CatalogFinding(
                    GOVERNANCE_CHANGED,
                    name,
                    "version/owner/lifecycle/approval/provenance가 바뀜",
                )
            )
    for source_id in sorted(set(current_sources) | set(recorded_sources)):
        if recorded_sources.get(source_id) != current_sources.get(source_id):
            findings.append(
                CatalogFinding(
                    SOURCE_CHANGED,
                    source_id,
                    "upstream source ref 또는 resolved SHA가 바뀜",
                )
            )
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


_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_LIFECYCLES = frozenset({"active", "experimental", "deprecated", "retired"})
_APPROVALS = frozenset({"approved", "pending", "rejected", "unattested"})


def _valid_semver(value: str) -> bool:
    match = _SEMVER.fullmatch(value)
    if match is None:
        return False
    prerelease = match.group(4)
    return prerelease is None or all(
        not (part.isdigit() and len(part) > 1 and part.startswith("0"))
        for part in prerelease.split(".")
    )


def _governance_findings(
    entries: Sequence[SkillCatalogEntry],
    reachable: set[str],
) -> list[CatalogFinding]:
    findings: list[CatalogFinding] = []
    for entry in entries:
        invalid: list[str] = []
        for field_name in ("version", "owner", "lifecycle", "approval", "provenance"):
            value = getattr(entry, field_name)
            if value == INVALID_GOVERNANCE_SCALAR:
                invalid.append(f"{field_name}=structured")
            elif value == INVALID_FRONTMATTER:
                invalid.append(f"{field_name}=frontmatter-invalid")
        invalid_values = (INVALID_GOVERNANCE_SCALAR, INVALID_FRONTMATTER)
        if (
            entry.version not in invalid_values
            and entry.version
            and not _valid_semver(entry.version)
        ):
            invalid.append(f"version={entry.version}")
        if (
            entry.lifecycle not in invalid_values
            and entry.lifecycle not in _LIFECYCLES
        ):
            invalid.append(f"lifecycle={entry.lifecycle}")
        if (
            entry.approval not in invalid_values
            and entry.approval not in _APPROVALS
        ):
            invalid.append(f"approval={entry.approval}")
        if invalid:
            findings.append(
                CatalogFinding(
                    INVALID_GOVERNANCE,
                    entry.name,
                    ", ".join(invalid),
                    "frontmatter 거버넌스 값을 수정한다",
                    strict=entry.source in OWNED_SOURCES,
                )
            )
        if entry.name not in reachable:
            continue
        if entry.lifecycle == "retired":
            findings.append(
                CatalogFinding(
                    RETIRED_ROUTED,
                    entry.name,
                    "retired skill이 활성화 경로에 연결되어 있다",
                    "라우팅에서 제거하거나 lifecycle을 복구한다",
                    strict=entry.source in OWNED_SOURCES,
                )
            )
        if entry.approval in {"pending", "rejected"}:
            findings.append(
                CatalogFinding(
                    UNAPPROVED_ROUTED,
                    entry.name,
                    f"approval={entry.approval} skill이 활성화 경로에 연결되어 있다",
                    "승인 전까지 라우팅에서 제거한다",
                    strict=entry.source in OWNED_SOURCES,
                )
            )
    return findings


def _unrouted_findings(
    entries: Sequence[SkillCatalogEntry],
    reachable: set[str],
) -> list[CatalogFinding]:
    """어떤 활성화 경로에도 걸리지 않는 설치본.

    축은 네 개다: profile 표(selectors 있는 group), 어휘 라우팅, workflow phase 선언,
    그리고 이 셋에 걸린 skill이 끌어오는 dependencies. 축을 조건식 분기로 두면
    축이 늘 때마다 분기가 늘어난다 — 그래서 합집합 하나로 표현한다.

    `bundled`/`project`를 검사에서 빼 두는 동안 profile이 설치하는 25개 중 21개가
    어느 phase에도 붙지 않은 채로 남았다. source는 보고 문구에만 쓴다.
    """
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


def _reachable_names(
    profile: dict | None,
    entries: Sequence[SkillCatalogEntry],
    workflow_skills: Sequence[str] = (),
) -> set[str]:
    # str 하나를 넘긴 호출을 문자 단위로 순회하면 모든 이름이 미라우팅으로 뒤집힌다.
    declared_raw = (
        [workflow_skills]
        if isinstance(workflow_skills, str)
        else list(workflow_skills)
    )
    declared = set(_string_list(declared_raw))
    return set(
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


_STRICT_KINDS = frozenset(
    {
        NEW,
        REMOVED,
        NEW_VIEW,
        CONTENT_CHANGED,
        LOCATION_CHANGED,
        GOVERNANCE_CHANGED,
        SOURCE_CHANGED,
        LOCK_INVALID,
        LOCK_UNREADABLE,
        LOCK_STALE,
        INVALID_GOVERNANCE,
        CONTENT_UNREADABLE,
        RETIRED_ROUTED,
        UNAPPROVED_ROUTED,
        DEAD_DECLARATION,
        UNOWNED_ADAPTER,
        COLLISION,
    }
)


def strict_findings(
    findings: Sequence[CatalogFinding],
) -> tuple[CatalogFinding, ...]:
    return tuple(
        finding
        for finding in findings
        if finding.kind in _STRICT_KINDS and finding.strict
    )


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
