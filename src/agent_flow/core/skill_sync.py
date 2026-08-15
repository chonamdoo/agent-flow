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

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from agent_flow.core.atomic_io import atomic_write_text
from agent_flow.core.security import validate_safe_name
from agent_flow.core.worktree_isolation import git_safe

_READY_MARKER = ".agent-flow-sync-ok"


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
    """fetch 종류만 실제로 가져온다. 이미 핀이 맞으면 네트워크를 쓰지 않는다.

    `refresh=True`면 캐시를 버리고 다시 받는다. 이게 없으면 `main` 같은 움직이는
    ref가 최초 1회 받은 커밋에 영구히 굳는다 — 머신마다 다른, 보이지 않는 핀이
    된다. 실측으로 캐시가 upstream보다 뒤처진 상태가 그대로 남아 있었다.
    """
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
    """캐시가 어느 커밋에 굳어 있는지. 기록이 없으면 빈 문자열이다."""
    try:
        marker = (_checkout_path(source, env=env) / _READY_MARKER).read_text(encoding="utf-8")
    except OSError:
        return ""
    parts = marker.split()
    return parts[1] if len(parts) > 1 else ""


def fetched_source_roots(
    sources: Sequence[SkillSource], *, env: dict[str, str] | None = None
) -> list[str]:
    """캐시에 받아둔 fetch 소스를 resolver가 쓸 root 템플릿으로 바꾼다."""
    templates: list[str] = []
    for source in sources:
        if source.kind != "fetch" or not source.layout:
            continue
        checkout = _checkout_path(source, env=env)
        if checkout.is_dir():
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
    checkout = _checkout_path(source, env=env)
    # 완료 표식이 있어야 cached다. clone은 됐는데 checkout이 실패한 디렉터리를
    # `.git` 존재만으로 정상 취급하면 잘못된 트리가 영구히 굳는다.
    if not refresh and (checkout / ".git").exists() and (checkout / _READY_MARKER).exists():
        return SyncResult(
            source_id=source.id,
            status="cached",
            detail=f"{checkout} {cached_source_sha(source, env=env)}".rstrip(),
        )
    if checkout.exists():
        shutil.rmtree(checkout, ignore_errors=True)
    checkout.parent.mkdir(parents=True, exist_ok=True)
    # `--`가 없으면 profile YAML이 준 url이 `-`로 시작하는 순간 git 옵션으로 읽힌다.
    clone = git_safe(
        "clone",
        "--quiet",
        "--filter=blob:none",
        "--",
        source.url,
        str(checkout),
        cwd=checkout.parent,
        timeout_s=180,
    )
    if not clone.ok:
        return SyncResult(source_id=source.id, status="failed", detail=clone.stderr.strip())
    checkout_ref = git_safe("checkout", "--quiet", source.ref, cwd=checkout, timeout_s=60)
    if not checkout_ref.ok:
        shutil.rmtree(checkout, ignore_errors=True)
        return SyncResult(
            source_id=source.id, status="failed", detail=checkout_ref.stderr.strip()
        )
    # 어느 커밋을 받았는지 기록한다. ref만 적으면 `main`이 움직여도 캐시가
    # 무엇에 굳었는지 알 길이 없어서 stale을 눈으로도 확인하지 못한다.
    resolved = git_safe("rev-parse", "HEAD", cwd=checkout, timeout_s=30, optional_locks=False)
    sha = resolved.stdout.strip() if resolved.ok else ""
    # 캐시 적중 판정은 이 표식의 **존재**만 본다. 쓰다 죽으면 그 캐시는 영원히
    # "완성"으로 굳고 `cached_source_sha`는 빈 문자열을 돌려준다.
    atomic_write_text(checkout / _READY_MARKER, f"{source.ref} {sha}\n".rstrip() + "\n")
    return SyncResult(source_id=source.id, status="fetched", detail=f"{checkout} {sha}".rstrip())


def _is_safe_ref(ref: str) -> bool:
    # ref는 캐시 경로 컴포넌트로도 쓰인다. `..`이 들어가면 캐시 루트를 벗어난다.
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", ref)) and ".." not in ref


def _checkout_path(source: SkillSource, *, env: dict[str, str] | None) -> Path:
    # ref별로 디렉터리를 나눠 핀을 올려도 이전 체크아웃이 살아 있게 한다.
    return cache_root(env) / source.id / (source.ref or "HEAD")
