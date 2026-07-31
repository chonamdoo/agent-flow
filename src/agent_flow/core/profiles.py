from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from agent_flow.core.security import ensure_child_path, validate_safe_name


# `profiles/_schema.yaml`의 gates[].phase가 선언하는 전체 집합.
GATE_PHASES: tuple[str, ...] = ("pre-commit", "pre-push", "post-merge")
# workflow상 gates는 final-review → gates → commit 사이에서 돈다(`workflows/default.yaml`).
DEFAULT_GATE_PHASE = "pre-commit"
# phase 필터를 끄는 선택자. 실제 gate가 이 값을 phase로 선언할 수는 없다.
GATE_PHASE_ALL = "all"
# 프로젝트가 배포 profile 위에 얹는 파일. install이 덮지 않는 유일한 자리다.
PROJECT_OVERRIDE_SUFFIX = ".local.yaml"


class _UnknownProfileError(ValueError):
    pass


# override가 실제로 반영되는 키만 받는다. 근거는 `apply_project_profile_override`.
PROJECT_OVERRIDE_KEYS: tuple[str, ...] = ("branching", "pr")


@dataclass(frozen=True)
class ProfileGate:
    gate_id: str
    command: tuple[str, ...]
    required: bool = True
    phase: str = DEFAULT_GATE_PHASE


@dataclass(frozen=True)
class ProjectProfile:
    profile_id: str
    gates: tuple[ProfileGate, ...]
    skills: dict[str, Any]
    architecture: dict[str, Any] | None


def detect_profile(root: Path) -> str:
    # 설치 스크립트(install.mjs/kit.mjs)와 동일한 우선순위를 유지해야
    # 설치 배너와 런타임 gate/skill routing이 같은 profile을 본다.
    if (
        (root / "next.config.js").exists()
        or (root / "next.config.mjs").exists()
        or (root / "next.config.ts").exists()
    ):
        return "nextjs"
    if (
        (root / "Package.swift").exists()
        or any(root.glob("*.xcodeproj"))
        or any(root.glob("*.xcworkspace"))
    ):
        return "ios"
    if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists():
        return "python"
    package_path = root / "package.json"
    if package_path.exists():
        package_text = package_path.read_text(encoding="utf-8", errors="ignore")
        if "react-native" in package_text:
            return "react-native"
    if (
        (root / "build.gradle").exists()
        or (root / "settings.gradle").exists()
        or (root / "build.gradle.kts").exists()
        or (root / "settings.gradle.kts").exists()
    ):
        return "android"
    if package_path.exists():
        package_text = package_path.read_text(encoding="utf-8", errors="ignore")
        if "react-native" in package_text:
            return "react-native"
        if '"next"' in package_text:
            return "nextjs"
        # 일반 TypeScript 프로젝트는 node보다 좁은 profile을 써야 gate와 skill routing이 맞다.
        if (root / "tsconfig.json").exists():
            return "typescript"
        return "node"
    # npm gate를 실행할 수 없는 tsconfig 단독 프로젝트는 generic으로 둔다.
    return "generic"


def active_profile_ids(root: Path, requested: str = "auto") -> list[str]:
    if requested != "auto":
        return _dedupe_profiles(_split_profiles(requested))
    kit_profiles = _read_kit_profiles(root)
    if kit_profiles:
        return kit_profiles
    kit_profile = _read_kit_profile(root)
    if kit_profile:
        return [kit_profile]
    return [detect_profile(root)]


def load_profile(profile_id: str) -> ProjectProfile:
    payload = load_profile_payload(profile_id)
    if not isinstance(payload, dict):
        raise ValueError(f"profile must be a mapping: {profile_id}")
    if payload.get("id") != profile_id:
        raise ValueError(f"profile id mismatch: {profile_id}")
    gates = payload.get("gates", [])
    if not isinstance(gates, list):
        raise ValueError(f"profile gates must be a list: {profile_id}")
    return ProjectProfile(
        profile_id=profile_id,
        gates=tuple(_gate_from_payload(item, profile_id=profile_id) for item in gates),
        skills=payload.get("skills") if isinstance(payload.get("skills"), dict) else {},
        architecture=payload.get("architecture") if isinstance(payload.get("architecture"), dict) else None,
    )


def load_profile_payload(
    profile_id: str,
    root: Path | None = None,
    *,
    fallback_unknown_to_generic: bool = False,
) -> dict[str, Any]:
    try:
        text = (
            _read_profile_text(profile_id)
            if root is None
            else _read_project_profile_text(root, profile_id)
        )
    except _UnknownProfileError:
        if not fallback_unknown_to_generic:
            raise
        profile_id = "generic"
        text = (
            _read_profile_text(profile_id)
            if root is None
            else _read_project_profile_text(root, profile_id)
        )
    payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise ValueError(f"profile must be a mapping: {profile_id}")
    if root is None:
        return payload
    return apply_project_profile_override(payload, profile_id=profile_id, root=root)


def project_profile_path(root: Path, profile_id: str) -> Path:
    return _project_profile_path(root, profile_id, suffix=".yaml")


def project_profile_override_path(root: Path, profile_id: str) -> Path:
    return _project_profile_path(root, profile_id, suffix=PROJECT_OVERRIDE_SUFFIX)


def _read_project_profile_text(root: Path, profile_id: str) -> str:
    path = project_profile_path(root, profile_id)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return _read_profile_text(profile_id)


def _project_profile_path(root: Path, profile_id: str, *, suffix: str) -> Path:
    profiles_root = root / ".agent-flow" / "profiles"
    safe_id = validate_safe_name(profile_id, "profile")
    return ensure_child_path(
        profiles_root,
        profiles_root / f"{safe_id}{suffix}",
        "profile",
    )


def apply_project_profile_override(
    payload: dict[str, Any], *, profile_id: str, root: Path
) -> dict[str, Any]:
    """`<root>/.agent-flow/profiles/<id>.local.yaml`을 배포 profile 위에 얹는다.

    install은 배포 profile을 덮어써야 새 필드가 기존 설치본에 닿는다. 그래서 프로젝트가
    그 파일을 직접 고치면 다음 install에 사라지고, 사라진 것을 아무도 모른 채 base와 PR
    target이 kit 기본값으로 돌아간다. 별도 파일이라야 install이 손대지 않는다 —
    `pruneUninstalledProfiles`는 kit에 같은 이름이 있는 파일만 지운다.

    branch 계약만 받는다. gates/architecture/skills까지 열면 override가 반영되는 경로와
    무시되는 경로가 갈리므로(그쪽 호출자는 root를 넘기지 않는다), 그 키는 거부한다.
    조용히 무시하면 사용자는 선언이 걸렸다고 믿는다.
    """
    path = project_profile_override_path(root, profile_id)
    if not path.is_file():
        return payload
    override = yaml.safe_load(path.read_text(encoding="utf-8"))
    if override is None:
        return payload
    if not isinstance(override, dict):
        raise ValueError(f"profile override must be a mapping: {path}")
    declared_id = override.get("id")
    if declared_id is not None and declared_id != profile_id:
        raise ValueError(f"profile override id mismatch: {path} declares {declared_id!r}")
    unsupported = sorted(
        key for key in override if key != "id" and key not in PROJECT_OVERRIDE_KEYS
    )
    if unsupported:
        raise ValueError(
            f"profile override supports only {', '.join(PROJECT_OVERRIDE_KEYS)}: "
            f"remove {', '.join(unsupported)} from {path}"
        )
    merged = dict(payload)
    for key in PROJECT_OVERRIDE_KEYS:
        if key in override:
            merged[key] = _deep_merge(payload.get(key), override[key])
    branching = merged.get("branching")
    pr = merged.get("pr")
    if isinstance(branching, dict) and isinstance(pr, dict):
        integration = branching.get("integration")
        target = pr.get("target_branch")
        if (
            isinstance(integration, str)
            and isinstance(target, str)
            and integration != target
        ):
            raise ValueError(
                "profile override must keep branching.integration and "
                f"pr.target_branch equal: {path}"
            )
    return merged


def _deep_merge(base: object, patch: object) -> object:
    # 리스트는 합치지 않고 통째로 갈아 끼운다. 순서가 의미를 갖는 값(gate 순서, ref 후보)
    # 에서 append 병합은 선언한 적 없는 순서를 만든다.
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return patch
    merged = dict(base)
    for key, value in patch.items():
        merged[key] = _deep_merge(base.get(key), value)
    return merged


def _gate_from_payload(item: object, *, profile_id: str) -> ProfileGate:
    if not isinstance(item, dict):
        raise ValueError(f"profile gate must be a mapping: {profile_id}")
    gate_id = item.get("id")
    command = item.get("command")
    if not isinstance(gate_id, str) or not gate_id:
        raise ValueError(f"profile gate id missing: {profile_id}")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        raise ValueError(f"profile gate command must be a non-empty string list: {profile_id}:{gate_id}")
    required = item.get("required")
    return ProfileGate(
        gate_id=gate_id,
        command=tuple(command),
        required=required if isinstance(required, bool) else True,
        phase=_gate_phase_from_payload(item.get("phase"), profile_id=profile_id, gate_id=gate_id),
    )


def _gate_phase_from_payload(value: object, *, profile_id: str, gate_id: str) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_GATE_PHASE
    if isinstance(value, str) and value.strip() in GATE_PHASES:
        return value.strip()
    # 오타를 기본값으로 접으면 pre-push 게이트가 조용히 pre-commit에서 돈다.
    # 죽은 설정으로 돌아가는 경로라 거부한다.
    raise ValueError(
        f"profile gate phase must be one of {'|'.join(GATE_PHASES)}: {profile_id}:{gate_id}"
    )


def _read_profile_text(profile_id: str) -> str:
    safe_id = validate_safe_name(profile_id, "profile")
    package_path = resources.files("agent_flow").joinpath("profiles", f"{safe_id}.yaml")
    if package_path.is_file():
        return package_path.read_text(encoding="utf-8")
    repo_path = Path(__file__).resolve().parents[3] / "profiles" / f"{safe_id}.yaml"
    if not repo_path.is_file():
        raise _UnknownProfileError(f"unknown profile: {profile_id}")
    return repo_path.read_text(encoding="utf-8")


def _read_kit_profiles(root: Path) -> list[str]:
    data = _read_kit_json(root)
    profiles = data.get("profiles")
    if isinstance(profiles, list):
        return _dedupe_profiles(profile for profile in profiles if isinstance(profile, str))
    return []


def _read_kit_profile(root: Path) -> str:
    data = _read_kit_json(root)
    profile = data.get("profile")
    return profile if isinstance(profile, str) and profile else ""


def _read_kit_json(root: Path) -> dict[str, Any]:
    path = root / ".agent-flow" / "kit.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _split_profiles(value: str) -> list[str]:
    return [profile.strip() for profile in value.split(",") if profile.strip()]


def _dedupe_profiles(values: object) -> list[str]:
    profiles: list[str] = []
    seen: set[str] = set()
    try:
        iterator = iter(values)  # type: ignore[arg-type]
    except TypeError:
        return profiles
    for value in iterator:
        if isinstance(value, str) and value and value not in seen:
            profiles.append(value)
            seen.add(value)
    return profiles
