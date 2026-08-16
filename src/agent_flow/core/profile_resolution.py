"""디스크에서 활성 profile 한 벌을 정한다: `kit.json` → 파일 → 다중 profile union.

`core/profiles.py`는 **profile 하나**의 payload와 project override를 담당한다.
이 모듈은 그 위에서 "이 run은 어느 profile들로 도는가"를 정하고, 둘 이상이면
runner가 그대로 쓸 수 있는 union 한 벌로 만든다.

runner에서 떼어낸 이유는 크기가 아니라 결합이다. 여기 있던 코드는 leader
tripwire·cleanup·phase 전진 판정과 같은 파일에 있었고, 그 셋은 서로 아무 것도
공유하지 않는다. profile 해석이 잘못되면 branching·gates·PR target·skill routing이
한꺼번에 틀리므로, 그 판정은 phase 루프와 분리된 자리에서 읽혀야 한다.

union이 지켜야 하는 계약은 하나다: **resolver가 읽는 키는 평평하다.** 병합 규칙의
정본은 `local_skills.merged_profile_payload`이고 여기서 다시 구현하지 않는다.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from agent_flow.core.local_skills import merged_profile_payload
from agent_flow.core.phase_workflow import package_root
from agent_flow.core.profiles import (
    apply_project_profile_override,
    kit_declared_profile,
    kit_declared_profiles,
    project_profile_path,
)
from agent_flow.core.security import ensure_child_path, validate_safe_name


def resolve_profile(kit_root: Path, project_root: Path) -> tuple[str, dict[str, Any]]:
    """Return (profile_id, profile_dict).

    Resolution order:
      1. `AGENT_FLOW_PROFILE` env override (always wins; user opted in)
      2. `.agent-flow/kit.json:profiles` written by filtered installer
      3. `.agent-flow/kit.json:profile` written by the installer
      4. fall back to "generic"

    A typo in `kit.json:profile(s)` would otherwise run the entire workflow
    against the wrong stack (wrong branching, gates, PR target) — a
    correctness bug, not a degraded mode. So we treat that case as a hard
    error unless `AGENT_FLOW_FALLBACK_GENERIC=1` opts into silent fallback.
    Env-var override case stays lenient (the user explicitly set it; let
    them shoot their foot).
    """
    forced = os.environ.get("AGENT_FLOW_PROFILE")
    explicit_fallback = os.environ.get("AGENT_FLOW_FALLBACK_GENERIC") == "1"
    if forced:
        return load_single_profile(
            kit_root,
            forced,
            strict_missing=False,
            explicit_fallback=explicit_fallback,
            source="AGENT_FLOW_PROFILE",
            project_root=project_root,
        )

    from_kit_profiles = kit_declared_profiles(project_root)
    if from_kit_profiles:
        return load_profile_union(
            kit_root,
            from_kit_profiles,
            explicit_fallback=explicit_fallback,
            project_root=project_root,
        )

    from_kit = kit_declared_profile(project_root)
    profile_id = from_kit or "generic"
    return load_single_profile(
        kit_root,
        profile_id,
        strict_missing=bool(from_kit),
        explicit_fallback=explicit_fallback,
        source=".agent-flow/kit.json:profile" if from_kit else "default",
        project_root=project_root,
    )


def load_single_profile(
    kit_root: Path,
    profile_id: str,
    *,
    strict_missing: bool,
    explicit_fallback: bool,
    source: str,
    project_root: Path | None = None,
) -> tuple[str, dict[str, Any]]:
    validate_safe_name(profile_id, "profile")

    profile_path = kit_root / "profiles" / f"{profile_id}.yaml"
    ensure_child_path(kit_root / "profiles", profile_path, "profile")
    if project_root is not None:
        installed_profile = project_profile_path(project_root, profile_id)
        if installed_profile.is_file():
            profile_path = installed_profile
    if not profile_path.exists():
        # 워크플로 정의와 같은 규율이다 — 정본은 패키지 자원이고 kit root 사본은
        # 설치본이 덮어쓰는 자리다. 사본이 없다고 "없는 profile"로 판정하면
        # 루트 사본을 지울 수 없다.
        packaged = packaged_profile_path(profile_id)
        if packaged is not None:
            profile_path = packaged
    if not profile_path.exists():
        # Hard error when kit.json says a profile that doesn't exist (typo).
        # Lenient fallback only when explicitly requested via env var or when
        # the resolution path was already "generic" (true unknown setup).
        if strict_missing and not explicit_fallback:
            raise FileNotFoundError(
                f"profile {profile_id!r} not found at {profile_path}. "
                f"Likely a typo in `{source}`. "
                f"Set `AGENT_FLOW_FALLBACK_GENERIC=1` to fall back silently, "
                f"or fix the kit.json value."
            )
        print(
            f"⚠️  profile {profile_id!r} not found at {profile_path}; "
            f"falling back to `generic`.",
            file=sys.stderr,
        )
        profile_id = "generic"
        profile_path = kit_root / "profiles" / "generic.yaml"
        if project_root is not None:
            installed_generic = project_profile_path(project_root, profile_id)
            if installed_generic.is_file():
                profile_path = installed_generic
        if not profile_path.exists():
            packaged_generic = packaged_profile_path("generic")
            if packaged_generic is not None:
                profile_path = packaged_generic

    raw = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"profile {profile_path}: top-level must be a mapping")
    if raw.get("id") != profile_id:
        raise ValueError(f"profile id mismatch: {profile_id}")
    if project_root is not None:
        raw = apply_project_profile_override(raw, profile_id=profile_id, root=project_root)
    return profile_id, raw


def load_profile_union(
    kit_root: Path,
    profile_ids: list[str],
    *,
    explicit_fallback: bool,
    project_root: Path | None = None,
) -> tuple[str, dict[str, Any]]:
    loaded: list[tuple[str, dict[str, Any]]] = []
    for profile_id in profile_ids:
        loaded.append(
            load_single_profile(
                kit_root,
                profile_id,
                strict_missing=True,
                explicit_fallback=explicit_fallback,
                source=".agent-flow/kit.json:profiles",
                project_root=project_root,
            )
        )
    deduped = _dedupe_loaded_profiles(loaded)
    if not deduped:
        return load_single_profile(
            kit_root,
            "generic",
            strict_missing=False,
            explicit_fallback=explicit_fallback,
            source="default",
            project_root=project_root,
        )
    if len(deduped) == 1:
        return deduped[0]
    active_ids = [profile_id for profile_id, _ in deduped]
    # resolver가 읽는 skill 키는 평평하다 — `skills.required_review`,
    # `skills.external`, `skill_sources`. profile id로 한 겹 감싸면 그 키가 사라져
    # 다중 profile run의 routed required가 통째로 0개가 된다(실측: react-native +
    # android에서 `.kt` 변경에 required 5개 → 0개). 병합 규칙은
    # `merged_profile_payload` 하나뿐이다. 여기서 다시 구현하면 status와 runner가
    # 같은 run에 대해 서로 다른 required 집합을 본다.
    merged = merged_profile_payload([profile for _, profile in deduped])
    union: dict[str, Any] = {
        "id": "multi-profile",
        "active_profiles": active_ids,
        "profiles": [profile for _, profile in deduped],
        "review_angles": _merge_profile_list_field(deduped, "review_angles"),
        "gates": _merge_profile_list_field(deduped, "gates"),
        # architecture는 role 표를 profile별로 소유해야 해서 중첩을 유지한다.
        # resolver는 이 키를 읽지 않는다.
        "architecture": {
            profile_id: profile.get("architecture")
            for profile_id, profile in deduped
            if isinstance(profile.get("architecture"), dict)
        },
    }
    for key in ("skills", "skill_sources"):
        if key in merged:
            union[key] = merged[key]
    return ",".join(active_ids), union


def packaged_profile_path(profile_id: str) -> Path | None:
    """설치된 `agent_flow` 패키지가 싣고 있는 profile 정의."""
    package_dir = package_root()
    if package_dir is None:
        return None
    path = package_dir / "profiles" / f"{profile_id}.yaml"
    ensure_child_path(package_dir / "profiles", path, "profile")
    return path if path.is_file() else None


def _dedupe_loaded_profiles(
    profiles: list[tuple[str, dict[str, Any]]],
) -> list[tuple[str, dict[str, Any]]]:
    deduped: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for profile_id, profile in profiles:
        if profile_id in seen:
            continue
        seen.add(profile_id)
        deduped.append((profile_id, profile))
    return deduped


def _merge_profile_list_field(
    profiles: list[tuple[str, dict[str, Any]]], field: str
) -> list[Any]:
    merged: list[Any] = []
    seen: set[str] = set()
    for _, profile in profiles:
        values = profile.get(field)
        if not isinstance(values, list):
            continue
        for value in values:
            key = json.dumps(value, sort_keys=True, ensure_ascii=False)
            if key in seen:
                continue
            seen.add(key)
            merged.append(value)
    return merged
