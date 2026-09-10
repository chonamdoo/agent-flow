"""설치 시점에 1회 도는 skill 소스 동기화.

런타임은 이 모듈을 호출하지 않는다. 런 도중 사용자에게 설치를 묻지 않기 위해,
가져올 수 있는 것은 여기서 미리 가져오고 가져올 수 없는 것은 여기서 한 번만 보고한다.

정책:
  - `kind: host-managed` — 이미 설치 관리자가 있다(android CLI, host plugin marketplace).
    fetch하지 않는다. 존재 여부만 확인하고 없으면 설치 명령을 1회 출력한다.
  - `kind: fetch` — 설치 관리자가 없는 순수 git repo. 머신 공유 캐시에 pinned ref로 1회 clone한다.
    프로젝트마다도 run마다도 아니다. repo 안에는 아무것도 넣지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from agent_flow.core.atomic_io import atomic_write_text
from agent_flow.core.security import validate_safe_name
from agent_flow.core.worktree_isolation import FileLeaseUnavailable, exclusive_file_lease, git_safe


@dataclass(frozen=True)
class SkillSource:
    id: str
    kind: str
    url: str = ""
    ref: str = ""
    layout: str = ""
    install_hint: str = ""
    roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class SyncResult:
    source_id: str
    status: str  # fetched | cached | skipped | failed
    detail: str = ""


def cache_root(env: dict[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    override = environ.get("AGENT_FLOW_SKILL_CACHE")
    if override:
        return Path(override).expanduser()
    state_home = environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".agent-flow"
    return base / "skill-sources"


def parse_skill_sources(profile: dict | None) -> tuple[SkillSource, ...]:
    if not isinstance(profile, dict):
        return ()
    raw = profile.get("skill_sources")
    if not isinstance(raw, list):
        return ()
    sources: list[SkillSource] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("id") or "").strip()
        if not source_id:
            continue
        validate_safe_name(source_id, "skill source id")
        roots = tuple(
            str(root)
            for root in (item.get("roots") or [])
            if isinstance(root, str) and "{skill}" in root
        )
        sources.append(
            SkillSource(
                id=source_id,
                kind=str(item.get("kind") or "host-managed").strip(),
                url=str(item.get("url") or "").strip(),
                ref=str(item.get("ref") or "").strip(),
                layout=str(item.get("layout") or "").strip(),
                install_hint=str(item.get("install_hint") or "").strip(),
                roots=roots,
            )
        )
    return tuple(sources)


def sync_skill_sources(
    sources: Sequence[SkillSource], *, env: dict[str, str] | None = None, refresh: bool = False
) -> list[SyncResult]:
    """Fetch declared sources; refresh publishes a new checkout without mutating readers."""
    results: list[SyncResult] = []
    for source in sources:
        if source.kind != "fetch":
            results.append(
                SyncResult(source_id=source.id, status="skipped", detail=source.install_hint)
            )
            continue
        results.append(_fetch_source(source, env=env, refresh=refresh))
    return results


def cached_source_sha(source: SkillSource, *, env: dict[str, str] | None = None) -> str:
    checkout = cached_source_checkout(source, env=env)
    return checkout.name if checkout else ""


def cached_source_checkout(source: SkillSource, *, env: dict[str, str] | None = None) -> Path | None:
    """Return one fully published snapshot, never the mutable source/ref alias."""
    try:
        base = _source_cache_path(source, env=env)
        pointer = json.loads((base / "current.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(pointer, dict) or pointer.get("url") != source.url or pointer.get("ref") != source.ref:
        return None
    sha = pointer.get("sha")
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40,64}", sha) is None:
        return None
    checkout = base / "snapshots" / sha
    return checkout if checkout.is_dir() and (checkout / ".git").is_dir() else None


def fetched_source_roots(
    sources: Sequence[SkillSource], *, env: dict[str, str] | None = None
) -> list[str]:
    """캐시에 받아둔 fetch 소스를 resolver가 쓸 root 템플릿으로 바꾼다."""
    templates: list[str] = []
    for source in sources:
        if source.kind != "fetch" or not source.layout:
            continue
        checkout = cached_source_checkout(source, env=env)
        if checkout is not None:
            templates.append(str(checkout / source.layout))
    return templates


def _fetch_source(
    source: SkillSource, *, env: dict[str, str] | None, refresh: bool = False
) -> SyncResult:
    if not source.url or not source.ref:
        return SyncResult(
            source_id=source.id, status="failed", detail="fetch source needs both url and ref"
        )
    if not _is_safe_ref(source.ref):
        return SyncResult(source_id=source.id, status="failed", detail=f"unsafe ref: {source.ref}")
    try:
        base = _source_cache_path(source, env=env)
        with exclusive_file_lease(base / ".publish.lock", wait=True):
            checkout = cached_source_checkout(source, env=env)
            if not refresh and checkout is not None:
                return SyncResult(source.id, "cached", f"{checkout} {checkout.name}")
            with tempfile.TemporaryDirectory(prefix=".fetch-", dir=base) as temporary:
                staging = Path(temporary) / "checkout"
                clone = git_safe(
                    "clone", "--quiet", "--filter=blob:none", "--", source.url, str(staging),
                    cwd=base, timeout_s=180,
                )
                if not clone.ok:
                    return SyncResult(source.id, "failed", clone.stderr.strip() or clone.error or "git clone failed")
                checkout_ref = git_safe("checkout", "--quiet", source.ref, cwd=staging, timeout_s=60)
                if not checkout_ref.ok:
                    return SyncResult(source.id, "failed", checkout_ref.stderr.strip() or checkout_ref.error or "git checkout failed")
                resolved = git_safe("rev-parse", "HEAD", cwd=staging, timeout_s=30, optional_locks=False)
                sha = resolved.stdout.strip()
                if not resolved.ok or re.fullmatch(r"[0-9a-f]{40,64}", sha) is None:
                    return SyncResult(source.id, "failed", resolved.stderr.strip() or "cannot resolve fetched commit")
                snapshots = base / "snapshots"
                snapshots.mkdir(exist_ok=True)
                checkout = snapshots / sha
                if not checkout.exists():
                    staging.rename(checkout)
                pointer = {"url": source.url, "ref": source.ref, "sha": sha}
                atomic_write_text(base / "current.json", json.dumps(pointer, sort_keys=True) + "\n")
                return SyncResult(source.id, "fetched", f"{checkout} {sha}")
    except (OSError, ValueError, FileLeaseUnavailable) as exc:
        return SyncResult(source.id, "failed", str(exc))


def _is_safe_ref(ref: str) -> bool:
    # ref는 캐시 경로 컴포넌트로도 쓰인다. `..`이 들어가면 캐시 루트를 벗어난다.
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", ref)) and ".." not in ref


def _source_cache_path(source: SkillSource, *, env: dict[str, str] | None) -> Path:
    source_id = validate_safe_name(source.id, "skill source id")
    identity = hashlib.sha256(f"{source.url}\0{source.ref}".encode("utf-8")).hexdigest()
    return cache_root(env) / source_id / identity
