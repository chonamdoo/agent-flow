from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ProfileGate:
    gate_id: str
    command: tuple[str, ...]
    required: bool = True


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


def load_profile_payload(profile_id: str) -> dict[str, Any]:
    payload = yaml.safe_load(_read_profile_text(profile_id))
    if not isinstance(payload, dict):
        raise ValueError(f"profile must be a mapping: {profile_id}")
    return payload


def primary_profile_id(root: Path, requested: str = "auto") -> str:
    profiles = active_profile_ids(root, requested)
    return profiles[0] if profiles else detect_profile(root)


def load_project_profile_payload(root: Path, profile_id: str) -> dict[str, Any]:
    if not profile_id or not profile_id.replace("-", "_").isalnum():
        raise ValueError(f"unsafe profile: {profile_id}")
    installed = root / ".agent-flow" / "profiles" / f"{profile_id}.yaml"
    if installed.is_file() and not installed.is_symlink():
        payload = yaml.safe_load(installed.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("id") != profile_id:
            raise ValueError(f"installed profile id mismatch: {profile_id}")
        return payload
    return load_profile_payload(profile_id)


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
    )


def _read_profile_text(profile_id: str) -> str:
    package_path = resources.files("agent_flow").joinpath("profiles", f"{profile_id}.yaml")
    if package_path.is_file():
        return package_path.read_text(encoding="utf-8")
    repo_path = Path(__file__).resolve().parents[3] / "profiles" / f"{profile_id}.yaml"
    if not repo_path.is_file():
        raise ValueError(f"unknown profile: {profile_id}")
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
