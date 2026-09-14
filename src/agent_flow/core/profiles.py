from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, cast, Literal

import yaml

from agent_flow.core.architecture_policy import ArchitectureMode, ArchitectureSelection
from agent_flow.core.reviewer_launch import (
    ReviewerLaunchError,
    validate_reviewer_launch_declaration,
)
from agent_flow.core.security import (
    ensure_child_path,
    validate_git_branch,
    validate_safe_name,
)
from agent_flow.core.worktree_isolation import LEADER_SWEEP_SCOPES


# `profiles/_schema.yaml`의 gates[].phase가 선언하는 전체 집합.
ProfileGatePhase = Literal["pre-commit", "pre-push", "post-merge"]
GatePhase = Literal["pre-commit", "pre-push", "post-merge", "all"]
GATE_PHASES: tuple[ProfileGatePhase, ...] = (
    "pre-commit",
    "pre-push",
    "post-merge",
)
GateExecution = Literal["local", "ci"]
GATE_EXECUTIONS: tuple[GateExecution, ...] = ("local", "ci")


def require_gate_execution(
    value: object,
    *,
    label: str = "gate execution",
    context: str = "",
) -> GateExecution:
    if not isinstance(value, str) or value not in GATE_EXECUTIONS:
        suffix = f": {context}" if context else ""
        raise ValueError(
            f"{label} must be one of {', '.join(GATE_EXECUTIONS)}{suffix}"
        )
    # Membership in the closed tuple establishes the Literal at runtime.
    return cast(GateExecution, value)

# workflow상 gates는 final-review → gates → commit 사이에서 돈다(`workflows/default.yaml`).
DEFAULT_GATE_PHASE: ProfileGatePhase = "pre-commit"
# phase 필터를 끄는 선택자. 실제 gate가 이 값을 phase로 선언할 수는 없다.
GATE_PHASE_ALL: GatePhase = "all"


def require_gate_phase(value: object) -> GatePhase:
    phases: tuple[GatePhase, ...] = (*GATE_PHASES, GATE_PHASE_ALL)
    if not isinstance(value, str) or value not in phases:
        raise ValueError(f"gate phase must be one of {', '.join(phases)}")
    return cast(GatePhase, value)

# 프로젝트가 배포 profile 위에 얹는 파일. install이 덮지 않는 유일한 자리다.
PROJECT_OVERRIDE_SUFFIX = ".local.yaml"
# `flutter create`가 `dependencies:`에 쓰는 Flutter SDK 의존. 이것이 pubspec을 가진
# 순수 Dart 패키지와 Flutter 저장소를 가르는 유일한 표지다. 인라인 주석을 허용한다 —
# `sdk: flutter # Flutter SDK`도 같은 매핑으로 파싱되는 유효한 pubspec이다. `#` 앞의
# 공백을 요구해야 `sdk: flutter#c`(주석이 아니라 스칼라 `flutter#c`)를 잡지 않는다.
_FLUTTER_SDK_DEPENDENCY_RE = re.compile(
    r"""^\s*sdk:\s*["']?flutter["']?(?:[ \t]+#.*)?[ \t]*$""", re.MULTILINE
)


class _UnknownProfileError(ValueError):
    pass


# override가 실제로 반영되는 키만 받는다. 근거는 `apply_project_profile_override`.
PROJECT_OVERRIDE_KEYS: tuple[str, ...] = (
    "architecture",
    "branching",
    "commit_convention",
    "execution",
    "gates",
    "pr",
    "review_angles",
)

# 배포 role이 선언했으면 동명 override role도 반드시 다시 선언해야 하는 키.
#
# 판정 기준은 "그 키를 빼면 효과가 어디서 나타나는가"다. `modules`는 role이 자기에게
# 두는 제약이 아니라 **모듈 소유권 좌표**고, `architecture_lint.role_owns_module`이
# 그것을 읽어 *다른* role의 required 규칙을 켠다. 그 규칙표
# (`REQUIRED_GRADLE_MODULES`)는 role id로 키가 잡혀 있고 override로 바뀌지 않는다.
# 그래서 `feature-api`에서 `modules`만 빼면 `feature-presentation must depend on
# :feature:<f>:api`가 조용히 꺼진다 — 지운 자리에서는 보이지 않는 곳의 규칙이
# 사라진다. 실측: 배포 profile은 이 규칙을 보고했고, modules만 뺀 override에서는
# must-depend-on이 0건이었다.
#
# `paths`도 같은 성질이다. `architecture_lint.validate_pair`가 **짝 role의 `paths`**를
# 읽어 그 자리가 디스크에 있는지 본다. 그래서 `feature-api`에서 `paths`만 빼면
# `feature-presentation requires paired role feature-api`가 조용히 꺼진다 — 지운
# 자리가 아니라 짝의 판정이 사라진다. 실측: 중첩 레이아웃에서 pair finding 4 → 2.
#
# `package_suffix`·`forbidden`·`pair_with`는 같은 성질이 아니라 넣지 않는다. 그 값
# 자체가 규칙이고 효과는 그 role 안에서 끝난다 — 빼면 "이 role은 그 제약을 두지
# 않는다"는 완결된 선언이고, 배포본이 애초에 suffix를 주지 않은 role(app-shell 등)과
# 같은 상태다. 지운 자리에서 지운 것만 사라진다.
#
# 반증 조건: role의 어떤 키가 새로 "다른 role의 규칙을 켜는 입력"이 되면 그때 여기
# 들어와야 한다. 이름을 골라 예외를 두는 목록이 아니라 그 성질의 판정이다.
ROLE_KEYS_OVERRIDE_MUST_RESTATE: tuple[str, ...] = ("modules", "paths")


@dataclass(frozen=True)
class ProfileGate:
    gate_id: str
    command: tuple[str, ...]
    required: bool = True
    phase: ProfileGatePhase = DEFAULT_GATE_PHASE
    timeout_s: int | None = None
    execution: GateExecution = "local"
    ci_check: str | None = None


@dataclass(frozen=True)
class ProjectProfile:
    profile_id: str
    gates: tuple[ProfileGate, ...]
    skills: dict[str, Any]
    architecture: dict[str, Any] | None


def package_dependencies(root: Path) -> set[str]:
    manifest = root / "package.json"
    if not manifest.is_file():
        return set()
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid package manifest {manifest}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{manifest} must contain a JSON object")
    return {
        name
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
        if isinstance(payload.get(key), dict)
        for name in payload[key]
    }




def _read_build_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _without_gradle_comments(text: str) -> str:
    return re.sub(
        r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|/\*[\s\S]*?\*/|//[^\n]*""",
        lambda match: "" if match[0].startswith("/") else match[0],
        text,
    )


def _build_directories(root: Path) -> list[Path]:
    root = root.resolve()
    directories = [root]
    seen = {root}
    for directory in directories:
        settings = "\n".join(
            _without_gradle_comments(_read_build_file(directory / name))
            for name in ("settings.gradle", "settings.gradle.kts")
        )
        children: list[str] = []
        for include in re.finditer(r"\binclude\s*(?:\(([^)]*)\)|([^\n]+))", settings):
            children.extend(
                name.lstrip(":").replace(":", "/")
                for name in re.findall(r"""["'](:?[A-Za-z0-9_.:-]+)["']""", include[1] or include[2] or "")
            )
        pom = re.sub(r"<!--[\s\S]*?-->", "", _read_build_file(directory / "pom.xml"))
        for modules in re.findall(r"<modules\b[^>]*>([\s\S]*?)</modules>", pom):
            children.extend(re.findall(r"<module\b[^>]*>\s*([^<]+?)\s*</module>", modules))
        for child in children:
            candidate = directory / child
            resolved = candidate.resolve()
            if (
                candidate.is_dir()
                and not candidate.is_symlink()
                and resolved.is_relative_to(root)
                and resolved not in seen
            ):
                seen.add(resolved)
                directories.append(resolved)
    return directories


def detect_profile(root: Path) -> str:
    dependencies = package_dependencies(root)
    candidates: set[str] = set()
    if dependencies.intersection({"react-native", "expo"}):
        candidates.add("react-native")
    if _FLUTTER_SDK_DEPENDENCY_RE.search(_read_build_file(root / "pubspec.yaml")):
        candidates.add("flutter")
    if "next" in dependencies or any(
        (root / name).exists() for name in ("next.config.js", "next.config.mjs", "next.config.ts")
    ):
        candidates.add("nextjs")
    if (root / "Package.swift").exists() or any(root.glob("*.xcodeproj")) or any(root.glob("*.xcworkspace")):
        candidates.add("ios")
    if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists():
        candidates.add("python")
    jvm = False
    for directory in _build_directories(root):
        gradle_files = [directory / name for name in (
            "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"
        )]
        jvm = jvm or any(path.exists() for path in gradle_files) or (directory / "pom.xml").exists()
        gradle = "\n".join(_without_gradle_comments(_read_build_file(path)) for path in gradle_files)
        pom = re.sub(r"<!--[\s\S]*?-->", "", _read_build_file(directory / "pom.xml"))
        if (
            re.search(r"""\bid\s*(?:\(\s*)?["']org\.springframework\.boot["']""", gradle)
            or re.search(r"""["']org\.springframework\.boot:spring-boot[^"']*["']""", gradle)
            or re.search(r"<groupId\b[^>]*>\s*org\.springframework\.boot\s*</groupId>", pom)
        ):
            candidates.add("spring")
        if (
            re.search(r"""\bid\s*(?:\(\s*)?["']io\.ktor(?:\.plugin)?["']""", gradle)
            or re.search(r"""["']io\.ktor:ktor-server-[^"']+["']""", gradle)
            or re.search(r"<(dependency|plugin)\b[^>]*>(?:(?!</\1>)[\s\S])*<groupId>\s*io\.ktor\s*</groupId>\s*<artifactId>\s*ktor-server-[^<]+</artifactId>", pom)
        ):
            candidates.add("ktor")
        if (
            re.search(r"""\bid\s*(?:\(\s*)?["']com\.android\.(?:application|library|test|dynamic-feature)["']""", gradle)
            or any((directory / name).exists() for name in (
                "src/main/AndroidManifest.xml", "app/src/main/AndroidManifest.xml"
            ))
        ):
            candidates.add("android")
    if candidates.intersection({"react-native", "flutter"}):
        candidates.discard("android")
    if len(candidates) > 1:
        raise ValueError(f"ambiguous project profiles in {root}: {', '.join(sorted(candidates))}; select an explicit profile")
    if candidates:
        return next(iter(candidates))
    if jvm:
        raise ValueError(f"JVM framework evidence is insufficient in {root}; select an explicit profile")
    if (root / "package.json").exists():
        return "typescript" if (root / "tsconfig.json").exists() else "node"
    return "generic"


def active_profile_ids(root: Path, requested: str = "auto") -> list[str]:
    if requested != "auto":
        return _dedupe_profiles(_split_profiles(requested))
    kit_profiles = kit_declared_profiles(root)
    if kit_profiles:
        return kit_profiles
    kit_profile = kit_declared_profile(root)
    if kit_profile:
        return [kit_profile]
    return [detect_profile(root)]


def load_profile(profile_id: str, root: Path | None = None) -> ProjectProfile:
    payload = load_profile_payload(profile_id, root)
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
    if payload.get("id") != profile_id:
        raise ValueError(f"profile id mismatch: {profile_id}")
    if root is None:
        return payload
    return resolve_project_profile_payload(payload, profile_id=profile_id, root=root)


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


def resolve_project_profile_payload(
    payload: dict[str, Any], *, profile_id: str, root: Path
) -> dict[str, Any]:
    """Resolve declared project capabilities and gate variants before local overrides."""
    resolved = dict(payload)
    source = project_profile_path(root, profile_id)
    capability_ids = payload.get("capabilities", [])
    if not isinstance(capability_ids, list) or not all(
        isinstance(item, str) and item.strip() for item in capability_ids
    ):
        raise ValueError(f"profile capabilities must be a list of non-empty strings: {source}")
    if capability_ids:
        dependencies = package_dependencies(root)
        for capability_id in capability_ids:
            try:
                safe_id = validate_safe_name(capability_id, "profile capability")
            except ValueError as exc:
                raise ValueError(f"invalid profile capability in {source}: {exc}") from exc
            resource = resources.files("agent_flow").joinpath("profiles", f"_{safe_id}.yaml")
            context = f"{resource} (declared in {source})"
            try:
                capability = yaml.safe_load(resource.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                raise ValueError(f"cannot read profile capability {context}: {exc}") from exc
            except yaml.YAMLError as exc:
                raise ValueError(f"invalid profile capability YAML {context}: {exc}") from exc
            _validate_capability_fields(capability, source=context)
            required = set(capability.get("requires_dependencies", []))
            excluded = set(capability.get("excludes_dependencies", []))
            if not required.issubset(dependencies) or excluded.intersection(dependencies):
                continue
            _validate_capability_fields(resolved, source=str(source))
            skills = dict(resolved.get("skills") or {})
            additions = capability.get("skills") or {}
            skills["install"] = list(dict.fromkeys([*skills.get("install", []), *additions.get("install", [])]))
            skills["required_review"] = [*skills.get("required_review", []), *additions.get("required_review", [])]
            resolved["skills"] = skills
            if "architecture" not in resolved and "architecture" in capability:
                resolved["architecture"] = capability["architecture"]
            resolved["review_angles"] = [*resolved.get("review_angles", []), *capability.get("review_angles", [])]
    variants = payload.get("gate_variants", [])
    if "gate_variants" in payload:
        override_path = project_profile_override_path(root, profile_id)
        override = yaml.safe_load(override_path.read_text(encoding="utf-8")) if override_path.is_file() else None
        if not isinstance(override, dict) or "gates" not in override:
            _validate_gate_variants(variants, profile_id=profile_id, source=source)
            matches = [
                variant for variant in variants
                if all((root / name).is_file() for name in variant.get("requires_files", []))
                and (
                    not variant.get("any_files")
                    or any((root / name).is_file() for name in variant["any_files"])
                )
            ]
            if len({variant["tool"] for variant in matches}) > 1:
                raise ValueError(f"ambiguous build tools for {profile_id}; declare gates in {override_path}")
            if matches:
                selected = max(matches, key=lambda variant: len(variant.get("requires_files", [])))
                resolved["gates"] = selected["gates"]
    return apply_project_profile_override(resolved, profile_id=profile_id, root=root)


def _validate_capability_fields(payload: object, *, source: str) -> None:
    if not isinstance(payload, dict):
        raise ValueError(f"profile capability must be a mapping: {source}")
    for field in ("requires_dependencies", "excludes_dependencies"):
        value = payload.get(field, [])
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise ValueError(f"profile {field} must be a list of non-empty strings: {source}")
    skills = payload.get("skills", {})
    if not isinstance(skills, dict):
        raise ValueError(f"profile skills must be a mapping: {source}")
    install = skills.get("install", [])
    if not isinstance(install, list) or not all(
        isinstance(item, str) and item.strip() for item in install
    ):
        raise ValueError(f"profile skills.install must be a list of non-empty strings: {source}")
    for field, value in (
        ("skills.required_review", skills.get("required_review", [])),
        ("review_angles", payload.get("review_angles", [])),
    ):
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ValueError(f"profile {field} must be a list of mappings: {source}")


def _validate_gate_variants(
    variants: object, *, profile_id: str, source: Path
) -> None:
    if not isinstance(variants, list):
        raise ValueError(f"profile gate_variants must be a list of mappings: {source}")
    for index, variant in enumerate(variants):
        context = f"{source}: gate_variants[{index}]"
        if not isinstance(variant, dict):
            raise ValueError(f"profile gate_variants entry must be a mapping: {context}")
        tool = variant.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError(f"profile gate_variants tool must be a non-empty string: {context}")
        for field in ("requires_files", "any_files"):
            value = variant.get(field, [])
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise ValueError(
                    f"profile gate_variants {field} must be a list of non-empty strings: {context}"
                )
        gates = variant.get("gates")
        if not isinstance(gates, list):
            raise ValueError(f"profile gate_variants gates must be a list: {context}")
        try:
            for gate in gates:
                _gate_from_payload(gate, profile_id=profile_id)
        except ValueError as exc:
            raise ValueError(f"invalid profile gate_variants gate: {context}: {exc}") from exc


def assert_architecture_override_compatible(
    root: Path, selection: ArchitectureSelection,
) -> None:
    """Reject an architecture override incompatible with the active profile."""
    if selection.mode is ArchitectureMode.CLEAN:
        return
    for path in (root / ".agent-flow" / "profiles").glob("*.local.yaml"):
        try:
            override = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ValueError(f"cannot read profile override {path}: {exc}") from exc
        if isinstance(override, dict) and "architecture" in override:
            raise ValueError(
                f"{path}: legacy architecture override conflicts with "
                f"architecture mode {selection.mode.value}; migrate the override "
                "to the selected project contract before proceeding"
            )


def apply_project_profile_override(
    payload: dict[str, Any], *, profile_id: str, root: Path
) -> dict[str, Any]:
    """`<root>/.agent-flow/profiles/<id>.local.yaml`을 배포 profile 위에 얹는다.

    install은 배포 profile을 덮어써야 새 필드가 기존 설치본에 닿는다. 그래서 프로젝트가
    그 파일을 직접 고치면 다음 install에 사라지고, 사라진 것을 아무도 모른 채 base와 PR
    target이 kit 기본값으로 돌아간다. 별도 파일이라야 install이 손대지 않는다 —
    `pruneUninstalledProfiles`는 kit에 같은 이름이 있는 파일만 지운다.

    branch 계약과 함께 gates/architecture도 받는다. 이 두 키를 소비하는 경로를 전수
    확인했고 전부 프로젝트 root를 넘긴다 — `architecture_lint`의 네 호출(root 또는
    `profile_root or root`), `gate_plan.profile_gate_commands`, `profile_resolution.load_single_profile`,
    `local_skills.resolved_profile`, `worktrees`의 lint 준비. 즉 "여기서 받아도 반영되지
    않는 경로가 있다"는 낡은 근거는 더 이상 성립하지 않는다. root 없이 payload를 읽는
    호출자를 새로 넣으면 그쪽에서만 override가 조용히 사라지므로, 새 호출자는 root를
    함께 넘겨야 한다.

    `skills`는 계속 거부한다. 설치 대상을 정하는 쪽은 Python이 아니라 installer이고
    (`lib/skill-selection.mjs`의 `profileSkillsFromSource`는 kit의 `<id>.yaml`만 읽는다)
    override는 Python 런타임만 통과한다. 열면 "선언한 skill 목록"과 "실제 설치된 목록"이
    갈리고, 그 어긋남은 라우팅이 빈 skill을 가리킬 때까지 보이지 않는다.
    조용히 무시하면 사용자는 선언이 걸렸다고 믿으므로, 거부는 예외로 낸다.
    """
    path = project_profile_override_path(root, profile_id)
    if not path.is_file():
        return _validate_project_profile_branch_contract(
            payload,
            source=project_profile_path(root, profile_id),
        )
    override = yaml.safe_load(path.read_text(encoding="utf-8"))
    if override is None:
        return _validate_project_profile_branch_contract(
            payload,
            source=project_profile_path(root, profile_id),
        )
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
    _validate_project_profile_override_shape(
        override, packaged=payload, profile_id=profile_id, source=path
    )
    merged = dict(payload)
    for key in PROJECT_OVERRIDE_KEYS:
        if key in override:
            merged[key] = _deep_merge(payload.get(key), override[key])
    return _validate_project_profile_branch_contract(merged, source=path)


def _validate_project_profile_branch_contract(
    payload: dict[str, Any], *, source: Path
) -> dict[str, Any]:
    branching = payload.get("branching")
    pr = payload.get("pr")
    declares_contract = (
        isinstance(branching, dict)
        and ("base" in branching or "integration" in branching)
    ) or (
        isinstance(pr, dict)
        and ("target_branch" in pr or "merge_strategy" in pr)
    )
    invalid_section = (
        "branching" in payload and not isinstance(branching, dict)
    ) or (
        "pr" in payload and not isinstance(pr, dict)
    )
    if not declares_contract and not invalid_section:
        return payload
    base = branching.get("base") if isinstance(branching, dict) else None
    integration = branching.get("integration") if isinstance(branching, dict) else None
    target = pr.get("target_branch") if isinstance(pr, dict) else None
    strategy = pr.get("merge_strategy") if isinstance(pr, dict) else None
    if (
        not isinstance(base, str)
        or not base.strip()
        or not isinstance(integration, str)
        or not integration.strip()
        or not isinstance(target, str)
        or not target.strip()
        or integration != target
        or not isinstance(strategy, str)
        or strategy not in {"merge", "squash", "rebase"}
    ):
        raise ValueError(
            "invalid project profile branch contract: branching.base and "
            "branching.integration must be non-empty strings; pr.target_branch "
            "must equal branching.integration; pr.merge_strategy must be merge, "
            f"squash, or rebase: {source}"
        )
    try:
        for branch in (base, integration, target):
            validate_git_branch(branch)
    except ValueError as exc:
        raise ValueError(f"invalid project profile branch contract: {source}: {exc}") from exc
    return payload


def _validate_project_profile_override_shape(
    override: dict[str, Any],
    *,
    packaged: dict[str, Any],
    profile_id: str,
    source: Path,
) -> None:
    """override가 연 키들이 소비자가 기대하는 모양인지 선언한 자리에서 검사한다.

    소비자는 전부 `isinstance(..., dict/list)`로 걸러내고 아니면 조용히 넘긴다 —
    `architecture_lint.lint_project`는 `architecture`가 dict가 아니면 finding 0개를
    돌려준다. 그러면 override의 오타 하나가 "lint가 통과했다"로 보인다.

    list는 교체이므로(`_deep_merge`) override가 적은 값이 그대로 최종값이다. 병합 후가
    아니라 override 자체를 검사해도 같은 것을 검사하는 것이고, 오류는 사용자가 쓴
    자리를 가리킨다.
    """
    architecture = override.get("architecture")
    if "architecture" in override and not isinstance(architecture, dict):
        raise ValueError(f"profile override architecture must be a mapping: {source}")
    has_roles = isinstance(architecture, dict) and "roles" in architecture
    roles = architecture.get("roles") if isinstance(architecture, dict) else None
    # 키가 있으면서 값이 없는 `roles:`도 거부한다. `None`을 "선언 안 함"으로 접으면
    # `_deep_merge`가 배포본 role 표를 `None`으로 갈아 끼우고, `lint_project`는
    # roles가 list가 아니면 finding 0개를 돌려준다 — 오타 한 줄이 필수 gate를 끈다.
    if has_roles and (
        not isinstance(roles, list) or not all(isinstance(role, dict) for role in roles)
    ):
        raise ValueError(
            f"profile override architecture.roles must be a list of mappings: {source}"
        )
    if isinstance(roles, list):
        for role in roles:
            _validate_override_role_fields(role, source=source)
        _assert_override_keeps_shipped_role_declarations(roles, packaged, source=source)
    _validate_override_leader_tripwire(override, source=source)
    _validate_override_execution(override, source=source)
    if "review_angles" in override:
        _validate_override_review_angles(override["review_angles"], source=source)
    if "commit_convention" in override:
        convention = override["commit_convention"]
        allowed = {"style": {"conventional", "tagged", "freeform"}, "co_author": {"include", "skip"}}
        if not isinstance(convention, dict) or convention.keys() - allowed.keys():
            raise ValueError(f"profile override commit_convention supports only style and co_author: {source}")
        for key, value in convention.items():
            if not isinstance(value, str) or value not in allowed[key]:
                raise ValueError(f"invalid profile override commit_convention.{key}: {source}")
    if "gates" not in override:
        return
    gates = override["gates"]
    if not isinstance(gates, list):
        raise ValueError(f"profile override gates must be a list: {source}")
    # gate 실행이 쓰는 파서로 검사한다. 여기서 따로 규칙을 적으면 두 벌이 갈린다.
    try:
        for item in gates:
            _gate_from_payload(item, profile_id=profile_id)
    except ValueError as exc:
        raise ValueError(f"invalid profile override gate: {source}: {exc}") from exc


def _validate_override_review_angles(angles: object, *, source: Path) -> None:
    if not isinstance(angles, list) or not all(isinstance(angle, dict) for angle in angles):
        raise ValueError(f"profile override review_angles must be a list of mappings: {source}")
    for index, angle in enumerate(angles):
        context = f"profile override review_angles[{index}]"
        for field in ("id", "prompt", "requires"):
            if field == "requires" and field not in angle:
                continue
            value = angle.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{context}.{field} must be a non-empty string: {source}")
        angle_id = angle["id"].strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", angle_id):
            raise ValueError(f"{context}.id is not a valid review angle id: {source}")
        prompt_path = Path(angle["prompt"].strip())
        if (
            prompt_path.is_absolute()
            or prompt_path.parts[:3] != ("templates", "_shared", "review")
            or len(prompt_path.parts) != 4
            or not prompt_path.parts[-1].endswith(".md")
        ):
            raise ValueError(f"{context}.prompt is not a valid review angle prompt path: {source}")
        for field in ("task_terms", "path_globs"):
            if field not in angle:
                continue
            values = angle[field]
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise ValueError(f"{context}.{field} must be a list of non-empty strings: {source}")


def _validate_override_execution(override: dict[str, Any], *, source: Path) -> None:
    """reviewer launch 선언은 그것을 해석하는 파서로 검사한다.

    파서는 `core.reviewer_launch`에 있다. 여기 규칙을 한 벌 더 적으면 gates에서
    피한 이중화가 그대로 생긴다.
    """
    if "execution" not in override:
        return
    execution = override["execution"]
    # 키가 있으면서 값이 없는 `execution:`은 거부한다. `_deep_merge`가 배포본 선언을
    # `None`으로 갈아 끼우고, 소비자는 그것을 "선언 없음"으로 읽어 기본 model로 돈다.
    if execution is None:
        raise ValueError(f"profile override execution must be a mapping: {source}")
    try:
        validate_reviewer_launch_declaration(execution)
    except ReviewerLaunchError as exc:
        raise ValueError(f"invalid profile override execution: {source}: {exc}") from exc


# role 필드가 소비자에게 어떤 모양으로 읽히는가. 이름을 고른 목록이 아니라
# `architecture_lint`가 그 값을 쓰는 방식이다 — 리스트로 순회하는 것과 문자열로
# 비교하는 것.
_ROLE_LIST_FIELDS: tuple[str, ...] = ("paths", "modules", "forbidden")
_ROLE_OPTIONAL_TEXT_FIELDS: tuple[str, ...] = ("package_suffix", "pair_with")


def _validate_override_role_fields(role: dict[str, Any], *, source: Path) -> None:
    """role 필드가 소비자가 순회할 수 있는 모양인가.

    `paths: "core/domain"`처럼 list 자리에 스칼라를 쓰면 `match_role`과
    `role_owns_module`이 `isinstance(..., list)`에서 조용히 건너뛴다. 그러면 필수
    architecture gate가 `n/a`이거나 무위반으로 보이고, 오타는 어디에도 보고되지
    않는다. 소비자가 침묵하므로 선언 자리에서 막는다.

    `id`만 **없어도** 거부한다. 나머지는 없으면 "그 제약을 두지 않는다"는 완결된
    선언이지만, id는 의미 좌표다 — 빠지면 `validate_gradle_dependencies`가
    `role_id=""`로 `FORBIDDEN_GRADLE_MODULES`/`REQUIRED_GRADLE_MODULES`를 조회해
    role id로 키가 잡힌 gradle 규칙 전부를 조용히 건너뛴다.
    """
    if "id" not in role:
        raise ValueError(
            "profile override architecture.roles entries must declare id "
            f"(gradle dependency rules are keyed by it): {source}"
        )
    for field in _ROLE_LIST_FIELDS:
        if field not in role:
            continue
        value = role[field]
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise ValueError(
                f"profile override architecture.roles[{role.get('id')!r}].{field} "
                f"must be a list of non-empty strings: {source}"
            )
    for field in ("id", *_ROLE_OPTIONAL_TEXT_FIELDS):
        if field not in role:
            continue
        value = role[field]
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"profile override architecture.roles[{role.get('id')!r}].{field} "
                f"must be a non-empty string: {source}"
            )


def _assert_override_keeps_shipped_role_declarations(
    roles: list[Any], packaged: dict[str, Any], *, source: Path
) -> None:
    """배포본이 선언한 것을 조용히 버리지는 못하게 한다.

    비교 대상은 override를 얹기 전의 배포(kit) role 표다. `_deep_merge`가 list를
    통째로 갈아 끼우므로 override가 적지 않은 키는 병합 후에도 없다 — 즉 같은 id를
    다시 쓰면서 키를 빼는 것은 "그 선언을 지운다"와 같다.

    배포본에 없는 id는 비교 대상이 없으므로 막지 않는다. 막는 것은 있던 선언을 빼는
    것 하나다. 값이 빈 목록인 것도 막지 않는다 — 빈 목록은 diff에 남는 선언이고,
    "이 저장소에는 그 모듈이 없다"를 누가 언제 정했는지 리뷰에서 보인다.
    """
    shipped_architecture = packaged.get("architecture")
    shipped_roles = (
        shipped_architecture.get("roles")
        if isinstance(shipped_architecture, dict)
        else None
    )
    if not isinstance(shipped_roles, list):
        return
    shipped_by_id = {
        role["id"]: role
        for role in shipped_roles
        if isinstance(role, dict) and isinstance(role.get("id"), str)
    }
    dropped: list[str] = []
    for role in roles:
        # id가 문자열이 아니면 배포본과 맞출 대상이 없다(unhashable일 수도 있다).
        role_id = role.get("id") if isinstance(role, dict) else None
        shipped = shipped_by_id.get(role_id) if isinstance(role_id, str) else None
        if not isinstance(shipped, dict):
            continue
        dropped.extend(
            f"{role_id}.{key}"
            for key in ROLE_KEYS_OVERRIDE_MUST_RESTATE
            if key in shipped and key not in role
        )
    if dropped:
        raise ValueError(
            "profile override must restate what the packaged role declared: "
            f"{', '.join(dropped)} missing in {source}. "
            "Rules keyed by role id stay in effect while the id does, so dropping "
            "the declaration they read turns them off silently. Declare an empty "
            "list to turn a rule off on purpose, or use a role id of your own."
        )


def _validate_override_leader_tripwire(override: dict[str, Any], *, source: Path) -> None:
    """오타를 선언한 자리에서 잡는다.

    소비 자리(`leader_tripwire.declared_leader_tripwire`)에도 같은 검사가 있지만 그것은
    `Runner.__init__`에서 돈다. run 경로는 거기서 난 예외를 실패로 보고 방금 만든
    worktree를 정리한다(`cli._cleanup_worktree_after_failure`). 오타 하나의 값이 작업
    트리 삭제일 수는 없으므로 profile을 읽는 자리에서 먼저 던진다.

    `None`을 통과시키는 것은 소비 자리와 맞춘 것이다 — 거기서 `None`은 "선언 없음"이고
    기본값 `all`로 간다. 여기서만 거부하면 `leader_tripwire: null`이 자리에 따라 다르게
    해석된다.

    값 목록은 `LEADER_SWEEP_SCOPES` 하나를 본다. 여기에 문자열을 다시 적으면 세 번째
    사본이 되고, 갈렸을 때 선언은 통과하고 sweep만 다르게 돈다.
    """
    branching = override.get("branching")
    if not isinstance(branching, dict):
        return
    scope = branching.get("leader_tripwire")
    if scope is None or scope in LEADER_SWEEP_SCOPES:
        return
    raise ValueError(
        "profile override branching.leader_tripwire must be one of "
        f"{', '.join(LEADER_SWEEP_SCOPES)}: got {scope!r} in {source}"
    )


def _deep_merge(base: object, patch: object) -> object:
    # 리스트는 합치지 않고 통째로 갈아 끼운다. 순서가 의미를 갖는 값(gate 순서, ref 후보)
    # 에서 append 병합은 선언한 적 없는 순서를 만든다. `gates`와 `architecture.roles`도
    # 같은 규칙이다 — 이어붙이면 배포본의 role 표가 남아서, 프로젝트가 자기 모듈 구조를
    # 선언해도 맞지 않는 shipped 표가 계속 미매핑 finding을 낸다. 교체가 유일하게
    # "이 저장소의 구조는 이것이다"를 표현한다.
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
    execution = _gate_execution_from_payload(
        item.get("execution"),
        profile_id=profile_id,
        gate_id=gate_id,
    )
    ci_check = _gate_ci_check_from_payload(
        item.get("ci_check"),
        execution=execution,
        profile_id=profile_id,
        gate_id=gate_id,
    )
    return ProfileGate(
        gate_id=gate_id,
        command=tuple(command),
        required=required if isinstance(required, bool) else True,
        phase=_gate_phase_from_payload(item.get("phase"), profile_id=profile_id, gate_id=gate_id),
        timeout_s=_gate_timeout_from_payload(
            item.get("timeout_s"), profile_id=profile_id, gate_id=gate_id
        ),
        execution=execution,
        ci_check=ci_check,
    )


def _gate_ci_check_from_payload(
    value: object,
    *,
    execution: GateExecution,
    profile_id: str,
    gate_id: str,
) -> str | None:
    if execution == "local":
        if value is not None:
            raise ValueError(
                f"profile local gate must not declare ci_check: "
                f"{profile_id}:{gate_id}"
            )
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"profile CI gate ci_check missing: {profile_id}:{gate_id}"
        )
    return value.strip()


def _gate_timeout_from_payload(value: object, *, profile_id: str, gate_id: str) -> int | None:
    if value is None:
        return None
    # `bool`은 `int`의 하위형이라 먼저 걸러야 `timeout_s: true`가 1초가 되지 않는다.
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(
            f"profile gate timeout_s must be a positive integer: {profile_id}:{gate_id}"
        )
    return value


def _gate_execution_from_payload(
    value: object, *, profile_id: str, gate_id: str
) -> GateExecution:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "local"
    return require_gate_execution(
        value,
        label="profile gate execution",
        context=f"{profile_id}:{gate_id}",
    )


def _gate_phase_from_payload(
    value: object,
    *,
    profile_id: str,
    gate_id: str,
) -> ProfileGatePhase:
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_GATE_PHASE
    if isinstance(value, str) and value.strip() in GATE_PHASES:
        return cast(ProfileGatePhase, value.strip())
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


def kit_declared_profiles(root: Path) -> list[str]:
    """`kit.json:profiles`. 다중 profile 선언이 정본인 자리다."""
    data = read_kit_json(root)
    profiles = data.get("profiles")
    if isinstance(profiles, list):
        return _dedupe_profiles(profile for profile in profiles if isinstance(profile, str))
    return []


def kit_declared_profile(root: Path) -> str:
    """`kit.json:profile`. 단일 선언 자리이고 없으면 빈 문자열이다."""
    data = read_kit_json(root)
    profile = data.get("profile")
    return profile if isinstance(profile, str) and profile else ""


def read_kit_json(root: Path) -> dict[str, Any]:
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
