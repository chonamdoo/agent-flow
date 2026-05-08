from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ProfileGate:
    gate_id: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class ProjectProfile:
    profile_id: str
    gates: tuple[ProfileGate, ...]


def detect_profile(root: Path) -> str:
    if (root / "package.json").exists():
        package_text = (root / "package.json").read_text(encoding="utf-8", errors="ignore")
        if "react-native" in package_text:
            return "react-native"
        if "next" in package_text:
            return "nextjs"
        return "node"
    if (root / "pyproject.toml").exists():
        return "python"
    if (root / "build.gradle.kts").exists() or (root / "settings.gradle.kts").exists():
        return "android"
    return "generic"


def load_profile(profile_id: str) -> ProjectProfile:
    payload = yaml.safe_load(_read_profile_text(profile_id))
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
    )


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
    return ProfileGate(gate_id=gate_id, command=tuple(command))


def _read_profile_text(profile_id: str) -> str:
    package_path = resources.files("agent_flow").joinpath("profiles", f"{profile_id}.yaml")
    if package_path.is_file():
        return package_path.read_text(encoding="utf-8")
    repo_path = Path(__file__).resolve().parents[3] / "profiles" / f"{profile_id}.yaml"
    return repo_path.read_text(encoding="utf-8")
