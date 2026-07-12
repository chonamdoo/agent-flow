from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from agent_flow.core.security import ensure_child_path, validate_safe_name


_PYTHON_PROJECT_MARKERS = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "Pipfile",
    "poetry.lock",
    "uv.lock",
)
PROFILE_ARCHITECTURE_PHASES = frozenset(
    {"design", "ddd-design", "architecture-review", "slice-plan", "plan-review"}
)
_PROFILE_PROMPT_EXCLUDED_FIELDS = frozenset(
    {"skills", "android_skills", "android_ecosystem_skills", "chrisbanes_skills"}
)


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
    if _has_spring_markers(root):
        return "spring"
    if any((root / marker).exists() for marker in _PYTHON_PROJECT_MARKERS):
        return "python"
    package_path = root / "package.json"
    package_text = ""
    dependency_names: set[str] = set()
    if package_path.exists():
        package_text = package_path.read_text(encoding="utf-8", errors="ignore")
        dependency_names = _package_dependency_names(package_text)
        if dependency_names & {"react-native", "expo"}:
            return "react-native"
    if (
        (root / "build.gradle").exists()
        or (root / "settings.gradle").exists()
        or (root / "build.gradle.kts").exists()
        or (root / "settings.gradle.kts").exists()
    ):
        return "android"
    if package_path.exists():
        if "next" in dependency_names:
            return "nextjs"
        if "react" in dependency_names:
            return "react"
        if _has_node_backend_markers(package_text):
            return "node"
        # 일반 TypeScript 프로젝트는 node보다 좁은 profile을 써야 gate와 skill routing이 맞다.
        if (root / "tsconfig.json").exists():
            return "typescript"
        return "node"
    # npm gate를 실행할 수 없는 tsconfig 단독 프로젝트는 generic으로 둔다.
    return "generic"


def _has_node_backend_markers(package_text: str) -> bool:
    dependency_names = _package_dependency_names(package_text)
    return bool(
        dependency_names
        & {"express", "@nestjs/core", "fastify", "koa", "@hapi/hapi", "hapi"}
    )


def _package_dependency_names(package_text: str) -> set[str]:
    try:
        payload = json.loads(package_text)
    except json.JSONDecodeError:
        return set()
    if not isinstance(payload, dict):
        return set()
    dependency_names: set[str] = set()
    for section in (
        "dependencies",
        "devDependencies",
        "peerDependencies",
        "optionalDependencies",
    ):
        dependencies = payload.get(section)
        if isinstance(dependencies, dict):
            dependency_names.update(str(name) for name in dependencies)
    return dependency_names


def _has_spring_markers(root: Path) -> bool:
    build_files = [
        root / "pom.xml",
        root / "build.gradle",
        root / "build.gradle.kts",
        root / "settings.gradle",
        root / "settings.gradle.kts",
        root / "gradle" / "libs.versions.toml",
    ]
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        if (
            child.is_symlink()
            or not child.is_dir()
            or child.name.startswith(".")
            or child.name == "node_modules"
        ):
            continue
        build_files.extend(
            child / name
            for name in ("pom.xml", "build.gradle", "build.gradle.kts")
        )
    markers = (
        "org.springframework",
        "spring-boot",
    )
    for build_file in dict.fromkeys(build_files):
        if not build_file.is_file():
            continue
        text = build_file.read_text(encoding="utf-8", errors="ignore").lower()
        if any(marker in text for marker in markers):
            return True
    return False


def active_profile_ids(root: Path, requested: str = "auto") -> list[str]:
    if requested != "auto":
        profiles = _validated_profile_ids(_dedupe_profiles(_split_profiles(requested)))
        for profile in profiles:
            load_project_profile_payload(root, profile)
        return profiles
    kit_profiles = _read_kit_profiles(root)
    if kit_profiles:
        for profile in kit_profiles:
            load_project_profile_payload(root, profile)
        return kit_profiles
    kit_profile = _read_kit_profile(root)
    if kit_profile:
        load_project_profile_payload(root, kit_profile)
        return [kit_profile]
    profile = detect_profile(root)
    load_project_profile_payload(root, profile)
    return [profile]


def primary_profile_id(root: Path, requested: str = "auto") -> str:
    if requested != "auto":
        profiles = active_profile_ids(root, requested)
        profile = profiles[0] if profiles else detect_profile(root)
        load_project_profile_payload(root, profile)
        return profile

    data = _read_kit_json(root)
    kit_path = root / ".agent-flow" / "kit.json"
    if not data and not kit_path.exists() and not kit_path.is_symlink():
        profile = detect_profile(root)
        load_project_profile_payload(root, profile)
        return profile

    profiles = _kit_profiles(data)
    legacy_profile = _kit_profile(data)
    for profile in profiles:
        load_project_profile_payload(root, profile)

    configured = data.get("primary_profile")
    if configured is not None:
        if not isinstance(configured, str) or not configured:
            raise ValueError(
                ".agent-flow/kit.json:primary_profile must be a non-empty string"
            )
        primary = validate_safe_name(configured, "primary_profile")
        if profiles and primary not in profiles:
            raise ValueError(
                ".agent-flow/kit.json:primary_profile must be contained in profiles"
            )
    elif legacy_profile != "generic" and legacy_profile in profiles:
        primary = legacy_profile
    elif profiles:
        primary = profiles[0]
    elif legacy_profile:
        primary = legacy_profile
    else:
        primary = "generic"

    load_project_profile_payload(root, primary)
    return primary


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
    validate_safe_name(profile_id, "profile")
    payload = yaml.safe_load(_read_profile_text(profile_id))
    if not isinstance(payload, dict):
        raise ValueError(f"profile must be a mapping: {profile_id}")
    return payload


def load_project_profile_payload(root: Path, profile_id: str) -> dict[str, Any]:
    validate_safe_name(profile_id, "profile")
    kit_path = root / ".agent-flow" / "kit.json"
    if not kit_path.exists() and not kit_path.is_symlink():
        return load_profile_payload(profile_id)
    return load_installed_profile_payload(root, profile_id)


def load_installed_profile_payload(root: Path, profile_id: str) -> dict[str, Any]:
    """Load only the project-pinned profile snapshot."""
    validate_safe_name(profile_id, "profile")
    profile_path = root / ".agent-flow" / "profiles" / f"{profile_id}.yaml"
    _require_installed_regular_file(root, profile_path, f"installed profile {profile_id}")
    try:
        payload = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"unreadable installed profile: {profile_path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"installed profile must be a mapping: {profile_id}")
    if payload.get("id") != profile_id:
        raise ValueError(f"installed profile id mismatch: {profile_id}")
    _validate_installed_profile_shape(payload, profile_id)
    return payload


def merge_profile_payloads(
    profiles: list[tuple[str, dict[str, Any]]],
    primary_profile: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build the deterministic profile union shared by runners and prompts."""
    deduped: list[tuple[str, dict[str, Any]]] = []
    seen_profiles: set[str] = set()
    for profile_id, payload in profiles:
        safe_id = validate_safe_name(profile_id, "profile")
        if safe_id in seen_profiles:
            continue
        if not isinstance(payload, dict) or payload.get("id") != safe_id:
            raise ValueError(f"profile id mismatch: {safe_id}")
        seen_profiles.add(safe_id)
        deduped.append((safe_id, payload))
    if not deduped:
        raise ValueError("at least one installed profile is required")
    if len(deduped) == 1:
        return deduped[0]

    active_ids = [profile_id for profile_id, _ in deduped]
    primary = validate_safe_name(primary_profile or active_ids[0], "primary_profile")
    if primary not in active_ids:
        raise ValueError("primary_profile must be contained in profiles")
    return ",".join(active_ids), {
        "id": "multi-profile",
        "primary_profile": primary,
        "active_profiles": active_ids,
        "profiles": [profile for _, profile in deduped],
        "review_angles": _merge_profile_list_field(deduped, "review_angles"),
        "gates": _merge_profile_list_field(deduped, "gates"),
        "skills": {
            profile_id: profile.get("skills", {})
            for profile_id, profile in deduped
            if isinstance(profile.get("skills"), dict)
        },
        "architecture": {
            profile_id: profile.get("architecture")
            for profile_id, profile in deduped
            if isinstance(profile.get("architecture"), dict)
        },
    }


def load_installed_profile_snapshot(
    root: Path,
    profile_ids: list[str],
    primary_profile: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Load and merge an explicit installed profile selection."""
    loaded = [
        (profile_id, load_installed_profile_payload(root, profile_id))
        for profile_id in profile_ids
    ]
    return merge_profile_payloads(loaded, primary_profile)


def profile_prompt_projection(
    profile: dict[str, Any],
    phase_id: str,
) -> dict[str, Any]:
    """Return a compact, deterministic profile view for one phase prompt."""
    if not profile:
        return {}
    projection = {
        key: value
        for key, value in profile.items()
        if key not in _PROFILE_PROMPT_EXCLUDED_FIELDS
    }
    nested_profiles = projection.get("profiles")
    if isinstance(nested_profiles, list):
        projection["profiles"] = [
            profile_prompt_projection(value, phase_id)
            if isinstance(value, dict)
            else value
            for value in nested_profiles
        ]
    architecture = projection.get("architecture")
    if (
        isinstance(architecture, dict)
        and phase_id not in PROFILE_ARCHITECTURE_PHASES
    ):
        projection["architecture"] = _trim_prompt_architecture(architecture)
    return projection


def render_profile_prompt_block(
    profile_id: str,
    profile: dict[str, Any],
    phase_id: str,
) -> str:
    """Render the canonical profile prompt block used by every host."""
    snapshot = profile_prompt_projection(profile, phase_id)
    if not snapshot:
        return ""
    yaml_dump = yaml.safe_dump(
        snapshot,
        sort_keys=False,
        allow_unicode=True,
    ).rstrip()
    return (
        f"\n## Active profile: `{profile_id}`\n\n"
        "This is the parsed profile YAML — use these values directly, "
        "don't ask the user to look them up:\n\n"
        f"```yaml\n{yaml_dump}\n```\n"
    )


def _trim_prompt_architecture(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _trim_prompt_architecture(child)
        if isinstance(child, dict)
        else child
        for key, child in value.items()
        if key not in {"roles", "forbidden_dependencies"}
    }


def _merge_profile_list_field(
    profiles: list[tuple[str, dict[str, Any]]],
    field: str,
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


def _validate_installed_profile_shape(
    payload: dict[str, Any],
    profile_id: str,
) -> None:
    if "branching" in payload:
        branching = payload.get("branching")
        if not isinstance(branching, dict):
            raise ValueError(
                f"installed profile branching must be a mapping: {profile_id}"
            )
        worktree = branching.get("worktree")
        if "worktree" in branching and (
            not isinstance(worktree, str)
            or worktree not in {"required", "optional", "disabled"}
        ):
            raise ValueError(
                "installed profile branching.worktree must be "
                f"required, optional, or disabled: {profile_id}"
            )
        base = branching.get("base")
        if "base" in branching:
            _validate_profile_base_ref(base, profile_id)
        naming = branching.get("naming")
        if "naming" in branching:
            if not isinstance(naming, dict):
                raise ValueError(
                    f"installed profile branching.naming must be a mapping: {profile_id}"
                )
            prefix = naming.get("prefix")
            if "prefix" in naming and prefix != "feat/":
                raise ValueError(
                    f"installed profile branching.naming.prefix must be feat/: {profile_id}"
                )
            max_slug_length = naming.get("max_slug_length")
            if "max_slug_length" in naming and (
                isinstance(max_slug_length, bool)
                or not isinstance(max_slug_length, int)
                or not 12 <= max_slug_length <= 100
            ):
                raise ValueError(
                    "installed profile branching.naming.max_slug_length must be "
                    f"an integer from 12 to 100: {profile_id}"
                )

    expected_containers: tuple[tuple[str, type[object]], ...] = (
        ("gates", list),
        ("review_angles", list),
        ("skills", dict),
        ("architecture", dict),
        ("artifacts", dict),
        ("vocabulary", dict),
        ("commit_convention", dict),
        ("pr", dict),
    )
    for field, expected_type in expected_containers:
        value = payload.get(field)
        if field in payload and not isinstance(value, expected_type):
            expected_name = "a list" if expected_type is list else "a mapping"
            raise ValueError(
                f"installed profile {field} must be {expected_name}: {profile_id}"
            )
    for angle in payload.get("review_angles", []):
        if not isinstance(angle, dict):
            raise ValueError(
                f"installed profile review angle must be a mapping: {profile_id}"
            )
        angle_id = angle.get("id")
        if not isinstance(angle_id, str):
            raise ValueError(
                f"installed profile review angle id must be a string: {profile_id}"
            )
        validate_safe_name(angle_id, "review angle")


def _validate_profile_base_ref(value: object, profile_id: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"installed profile branching.base must be a non-empty string: {profile_id}"
        )
    invalid = (
        value.startswith("-")
        or value.startswith("/")
        or value.endswith(("/", ".", ".lock"))
        or ".." in value
        or "@{" in value
        or "//" in value
        or any(character.isspace() or ord(character) < 32 for character in value)
        or any(character in "~^:?*[\\" for character in value)
    )
    if invalid:
        raise ValueError(
            "invalid profile base ref; installed profile branching.base is unsafe: "
            f"{profile_id}"
        )
    return value


def _require_installed_regular_file(root: Path, path: Path, label: str) -> None:
    lexical_root = Path(os.path.abspath(root))
    lexical_path = Path(os.path.abspath(path))
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes the project: {path}") from exc
    cursor = lexical_root
    for index, part in enumerate(relative.parts):
        cursor /= part
        try:
            mode = cursor.lstat().st_mode
        except OSError as exc:
            raise ValueError(f"{label} is missing or unreadable: {path}") from exc
        if stat.S_ISLNK(mode):
            raise ValueError(f"{label} uses a symlink: {cursor}")
        is_leaf = index == len(relative.parts) - 1
        if is_leaf and not stat.S_ISREG(mode):
            raise ValueError(f"{label} is not a regular file: {path}")
        if not is_leaf and not stat.S_ISDIR(mode):
            raise ValueError(f"{label} has a non-directory ancestor: {cursor}")
    try:
        Path(os.path.realpath(lexical_path)).relative_to(
            Path(os.path.realpath(lexical_root))
        )
    except ValueError as exc:
        raise ValueError(f"{label} escapes the project: {path}") from exc


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
    validate_safe_name(profile_id, "profile")
    package_path = resources.files("agent_flow").joinpath("profiles", f"{profile_id}.yaml")
    if package_path.is_file():
        return package_path.read_text(encoding="utf-8")
    profile_root = Path(__file__).resolve().parents[3] / "profiles"
    repo_path = profile_root / f"{profile_id}.yaml"
    ensure_child_path(profile_root, repo_path, "profile")
    if not repo_path.is_file():
        raise ValueError(f"unknown profile: {profile_id}")
    return repo_path.read_text(encoding="utf-8")


def _read_kit_profiles(root: Path) -> list[str]:
    return _kit_profiles(_read_kit_json(root))


def _kit_profiles(data: dict[str, Any]) -> list[str]:
    profiles = data.get("profiles")
    if profiles is None:
        return []
    if isinstance(profiles, list):
        if not all(isinstance(profile, str) and profile for profile in profiles):
            raise ValueError("invalid profile names in .agent-flow/kit.json:profiles")
        return _validated_profile_ids(
            _dedupe_profiles(profiles)
        )
    raise ValueError(".agent-flow/kit.json:profiles must be a list")


def _read_kit_profile(root: Path) -> str:
    return _kit_profile(_read_kit_json(root))


def _kit_profile(data: dict[str, Any]) -> str:
    profile = data.get("profile")
    if profile is None:
        return ""
    if not isinstance(profile, str) or not profile:
        raise ValueError(".agent-flow/kit.json:profile must be a non-empty string")
    return validate_safe_name(profile, "profile")


def _read_kit_json(root: Path) -> dict[str, Any]:
    path = root / ".agent-flow" / "kit.json"
    if not path.exists() and not path.is_symlink():
        return {}
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"invalid installed kit metadata path: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unreadable installed kit metadata: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"installed kit metadata must be an object: {path}")
    return data


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
        logical = validate_safe_name(value, "profile").lower()
        if logical not in seen:
            profiles.append(logical)
            seen.add(logical)
    return profiles


def _validated_profile_ids(profiles: list[str]) -> list[str]:
    return [validate_safe_name(profile, "profile") for profile in profiles]
