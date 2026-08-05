from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from agent_flow.core.security import (
    ensure_child_path,
    validate_git_branch,
    validate_safe_name,
)
from agent_flow.core.worktree_isolation import LEADER_SWEEP_SCOPES


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
PROJECT_OVERRIDE_KEYS: tuple[str, ...] = ("architecture", "branching", "gates", "pr")

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

    branch 계약과 함께 gates/architecture도 받는다. 이 두 키를 소비하는 경로를 전수
    확인했고 전부 프로젝트 root를 넘긴다 — `architecture_lint`의 네 호출(root 또는
    `profile_root or root`), `cli._profile_gate_commands`, `runner._load_single_profile`,
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
