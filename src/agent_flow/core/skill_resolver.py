from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable, Sequence

import yaml


from agent_flow.core.architecture_policy import (
    MAX_ARCHITECTURE_DOCUMENT_BYTES,
    ArchitectureContractError,
    ArchitectureMode,
    ArchitectureSnapshot,
    ContractDocument,
    architecture_snapshot,
    contract_names_in,
    contract_skill_name,
    is_clean_architecture_skill,
)
from agent_flow.core.atomic_io import read_bounded_regular_file
from agent_flow.core.installation import assert_install_complete
from agent_flow.core.skill_metadata import (
    GOVERNANCE_KEYS,
    InvalidSkillFrontmatter,
    is_safe_skill_name,
    parse_skill_metadata,
)

# 우선순위 순서다. 앞쪽 root가 이기고, 같은 skill을 두 host에서 중복 로드하지 않는다.
_DEFAULT_PROJECT_TEMPLATES = (
    ("project-local", ".agent-flow/local-skills/{skill}/SKILL.md"),
    ("project", "skills/{skill}/SKILL.md"),
    ("bundled", ".agent-flow/skills/{skill}/SKILL.md"),
    # `npx skills add` 는 프로젝트 설치를 이 두 곳에 앉힌다. 전역 설치를 거부하는
    # 벤더(Prisma)는 여기밖에 안 놓으므로 project root도 봐야 한다. source를 `project`와
    # 가르는 이유는 이름 소유권이다 — `skills/`는 우리 것이고 여기는 남의 것이라
    # 어휘 라우팅 대상이다.
    ("vendor", ".claude/skills/{skill}/SKILL.md"),
    ("vendor", ".agents/skills/{skill}/SKILL.md"),
)

_HOST_TEMPLATES = {
    # plugin skill은 marketplace 레이아웃이 네 가지다. 단일 패턴만 두면 이 머신
    # 실측으로 plugins 아래 SKILL.md 53개 중 17개만 잡힌다.
    "claude": (
        "~/.claude/skills/{skill}/SKILL.md",
        "~/.claude/plugins/marketplaces/*/skills/{skill}/SKILL.md",
        "~/.claude/plugins/marketplaces/*/plugins/*/skills/{skill}/SKILL.md",
        "~/.claude/plugins/external_plugins/*/skills/{skill}/SKILL.md",
        "~/.claude/plugins/cache/*/*/*/skills/{skill}/SKILL.md",
    ),
    "codex": ("~/.codex/skills/{skill}/SKILL.md",),
    "omp": ("~/.omp/agent/skills/{skill}/SKILL.md", "~/.omp/skills/{skill}/SKILL.md"),
}

_SHARED_TEMPLATES = ("~/.agents/skills/{skill}/SKILL.md",)

_HOST_ORDER = ("claude", "codex", "omp")

_GLOB_CHARS = "*?["


# `.agent-flow/local-skills/`는 사용자가 직접 넣는 private drop-box다. frontmatter 선언이 없어도
# 코드 생성/리뷰 phase에는 붙는다 — 거기에 둔 것 자체가 "이 프로젝트에 적용하라"는 선언이다.
# 반면 bundled/host skill은 반드시 스스로 선언해야 활성화된다.
CODE_PHASES = (
    "implement",
    "implement-fix",
    "red",
    "green",
    "refactor",
    "diagnosis-cleanup",
    "fix-loop",
    "final-review",
    "review",
    "pr-comment-fix",
    "pr-ci-fix",
    "multi-review",
    "architecture-review",
)

# 표 섹션이 가리키는 phase 집합. `profile_routing`이 정의하고 여기서 다시 노출한다 —
# 소비자가 CODE_PHASES와 함께 한 곳에서 보게 한다.
from agent_flow.core.profile_routing import (  # noqa: E402  (CODE_PHASES 정의 뒤여야 한다)
    IMPLEMENTATION_PHASES,
    REVIEW_PHASES,
)


@dataclass(frozen=True)
class SkillRoot:
    """하나의 skill 탐색 위치. template은 `{skill}` 자리표시자와 glob을 허용한다."""

    source: str
    template: str
    install_hint: str = ""
    # 이 root가 특정 host 소유면 그 이름. profile 표는 `active_host_only`를 선언하므로
    # 표로 붙은 skill은 다른 host의 사본으로 충족시키면 안 된다.
    host: str = ""


@dataclass(frozen=True)
class ResolvedSkill:
    name: str
    path: Path | None
    source: str
    exists: bool
    install_hint: str = ""
    # frontmatter `description`의 첫 문장. optional 목록은 "scope가 걸리면 읽어라"인데
    # 이름만 주면 scope를 판단할 재료가 없다 — 정보 없는 판단 지점은 안 읽힌다.
    summary: str = ""

    def display_path(self, project_root: Path) -> str:
        if self.path is None:
            return "(not found)"
        try:
            return self.path.relative_to(project_root).as_posix()
        except ValueError:
            return str(self.path)


@dataclass(frozen=True)
class SkillRoute:
    skill: str
    kind: str
    detail: str


@dataclass(frozen=True)
class NormativeDocument:
    document: ContractDocument
    content: bytes
    routes: tuple[SkillRoute, ...] = ()
    inline: bool = False


@dataclass(frozen=True)
class NormativeDelivery:
    content: bytes
    documents: tuple[NormativeDocument, ...]

    @property
    def inline(self) -> bool:
        return any(document.inline for document in self.documents)


@dataclass(frozen=True)
class SkillResolution:
    required: tuple[ResolvedSkill, ...] = ()
    optional: tuple[ResolvedSkill, ...] = ()
    # 이 phase에서 프로젝트의 구조 계약으로 인정되는 required 이름. 작성자 게이트와
    # reviewer angle이 같은 판정을 쓰도록, 판정 결과를 해석 결과에 함께 싣는다.
    architecture_contract: tuple[str, ...] = ()
    architecture_snapshot: ArchitectureSnapshot | None = None
    architecture_documents: tuple[tuple[ContractDocument, str], ...] = ()
    architecture_norms: tuple[ContractDocument, ...] = ()
    normative_documents: tuple[NormativeDocument, ...] = ()
    routes: tuple[SkillRoute, ...] = ()

    @property
    def delivery(self) -> tuple[NormativeDelivery, ...]:
        groups: dict[bytes, list[NormativeDocument]] = {}
        for document in self.normative_documents:
            groups.setdefault(document.content, []).append(document)
        return tuple(
            NormativeDelivery(content, tuple(documents))
            for content, documents in groups.items()
        )

    @property
    def delivered_normative_bytes(self) -> int:
        return sum(len(group.content) for group in self.delivery if group.inline)

    @property
    def duplicated_normative_bytes(self) -> int:
        return sum(
            len(group.content) * (sum(item.inline for item in group.documents) - 1)
            for group in self.delivery if group.inline
        )

    @property
    def missing(self) -> tuple[ResolvedSkill, ...]:
        return tuple(skill for skill in self.required if not skill.exists)

    @property
    def available_required(self) -> tuple[ResolvedSkill, ...]:
        return tuple(skill for skill in self.required if skill.exists)


@dataclass(frozen=True)
class PhaseSkills:
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    replaceable_architecture: bool = False
    pinned_legacy: bool = False

    def is_empty(self) -> bool:
        return not self.required and not self.optional


@dataclass(frozen=True)
class SkillCatalogEntry:
    """discover된 skill 하나의 활성화 선언. 전부 frontmatter에서 온다."""

    name: str
    path: Path
    source: str
    workflow_phases: tuple[str, ...] = ()
    task_terms: tuple[str, ...] = ()
    path_globs: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    # taskTerms/pathGlobs를 **선언했는지**. 선언 여부와 비어 있음은 다른 뜻이다.
    selector_declared: bool = False
    # `workflowPhases`를 스스로 선언했는지. upstream SKILL.md는 이 필드를 쓰지 않으므로
    # (실측 953개 중 0개) 이것이 곧 "자동 활성화 근거가 있는가"다.
    phase_declared: bool = False
    description: str = ""
    keywords: tuple[str, ...] = ()
    version: str = ""
    owner: str = ""
    lifecycle: str = "active"
    approval: str = "unattested"
    provenance: str = ""
    architecture_modes: tuple[str, ...] = ()
    architecture_dependencies: tuple[tuple[str, tuple[str, ...]], ...] = ()
    read_error: str = ""



class ResolutionContext:
    def __init__(self) -> None:
        self.catalog: dict[tuple[str, str, str, bytes], SkillCatalogEntry] = {}
        self.resolutions: dict[str, tuple[object, SkillResolution]] = {}
        self.snapshots: dict[Path, ArchitectureSnapshot] = {}

    def clear(self) -> None:
        self.catalog.clear()
        self.resolutions.clear()
        self.snapshots.clear()

    def snapshot(self, root: Path) -> ArchitectureSnapshot:
        assert_install_complete(root)
        snapshot = architecture_snapshot(root)
        assert_install_complete(root)
        previous = self.snapshots.get(root.resolve())
        if previous == snapshot:
            return previous
        self.snapshots[root.resolve()] = snapshot
        return snapshot

INVALID_GOVERNANCE_SCALAR = "<invalid-structured-value>"
INVALID_FRONTMATTER = "<invalid-frontmatter>"

_CONTENT_EXCLUDED_NAMES = {
    ".agent-flow",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}


def skill_observed_content_digest(skill_directory: Path) -> str:
    """SKILL.md와 함께 배포되는 일반 파일 전체의 관측 digest."""
    root = skill_directory.resolve(strict=True)
    pending = [root]
    files: list[Path] = []
    while pending:
        directory = pending.pop()
        for child in directory.iterdir():
            if child.name in _CONTENT_EXCLUDED_NAMES or child.name.endswith(".pyc"):
                continue
            if child.is_symlink():
                files.append(child)
                continue
            if child.is_dir():
                pending.append(child)
            elif child.is_file():
                files.append(child)

    digest = hashlib.sha256()
    for file_path in sorted(
        files,
        key=lambda item: item.relative_to(root).as_posix().encode("utf-8"),
    ):
        digest.update(file_path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        file_digest = hashlib.sha256()
        if file_path.is_symlink():
            file_digest.update(b"symlink\0")
            target = os.readlink(file_path)
            file_digest.update(target.encode("utf-8", errors="surrogateescape"))
        else:
            with file_path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    file_digest.update(chunk)
        digest.update(file_digest.hexdigest().encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def active_host(env: dict[str, str] | None = None) -> str:
    """현재 실행 host. 명시 override가 없으면 환경변수로 추정하고, 실패하면 빈 문자열."""
    environ = os.environ if env is None else env
    explicit = (environ.get("AGENT_FLOW_HOST") or "").strip().lower()
    if explicit in _HOST_TEMPLATES:
        return explicit
    if environ.get("CLAUDECODE") or environ.get("CLAUDE_CONFIG_DIR"):
        return "claude"
    if environ.get("CODEX_HOME") or environ.get("CODEX_SANDBOX"):
        return "codex"
    if environ.get("PI_CODING_AGENT_DIR") or environ.get("OMP_SESSION_ID"):
        return "omp"
    return ""


def skill_roots(
    project_root: Path,
    *,
    profile: dict | None = None,
    host: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[SkillRoot, ...]:
    """탐색 순서대로 정렬된 root 목록. project → active host → 나머지 host → shared → profile 선언."""
    resolved_host = active_host(env) if host is None else host
    roots = [
        SkillRoot(source=source, template=str(project_root / template))
        for source, template in _DEFAULT_PROJECT_TEMPLATES
    ]
    ordered_hosts = [resolved_host] if resolved_host in _HOST_TEMPLATES else []
    ordered_hosts.extend(name for name in _HOST_ORDER if name != resolved_host)
    for name in ordered_hosts:
        roots.extend(
            SkillRoot(source="host", template=template, host=name)
            for template in _HOST_TEMPLATES[name]
        )
    roots.extend(SkillRoot(source="shared", template=template) for template in _SHARED_TEMPLATES)
    roots.extend(_profile_skill_source_roots(profile, env=env))
    return tuple(_dedupe_roots(roots))


def active_host_roots(roots: Sequence[SkillRoot], host: str) -> tuple[SkillRoot, ...]:
    """다른 host 소유 root를 뺀 목록.

    host를 못 알아내면(`""`) 거를 기준이 없다. 그때 전부 빼면 CI처럼 host 표식이
    없는 환경에서 모든 skill이 사라지므로, 알 수 없을 때는 거르지 않는다.
    """
    if not host:
        return tuple(roots)
    return tuple(root for root in roots if not root.host or root.host == host)


def resolve_skill(name: str, roots: Sequence[SkillRoot]) -> ResolvedSkill:
    """첫 매치가 이긴다. 어디에도 없으면 exists=False와 설치 안내를 담아 돌려준다."""
    if not is_safe_skill_name(name):
        return ResolvedSkill(name=name, path=None, source="", exists=False, install_hint="")
    hints: list[str] = []
    for root in roots:
        match = _match_template(root.template, name)
        if match is not None:
            return ResolvedSkill(
                name=name,
                path=match,
                source=root.source,
                exists=True,
                summary=skill_summary(match),
            )
        if root.install_hint:
            hints.append(root.install_hint)
    return ResolvedSkill(
        name=name,
        path=None,
        source="",
        exists=False,
        install_hint="; ".join(dict.fromkeys(hints)),
    )


def assert_architecture_selection_skills(
    project_root: Path,
    snapshot: ArchitectureSnapshot,
    *,
    profile: dict | None = None,
    architecture_root: Path | None = None,
) -> None:
    """Validate that selected architecture skills and dependencies are installed."""
    from agent_flow.core.profile_routing import routable_group_skills

    selection = snapshot.selection
    if selection.mode is ArchitectureMode.PENDING:
        return
    contract_root = architecture_root or project_root
    roots = active_host_roots(skill_roots(project_root, profile=profile), active_host())
    contract_name = contract_skill_name(selection)
    catalog = discover_skill_catalog(
        project_root, roots, exclude_names=(contract_name,) if contract_name else (),
    )
    if contract_name is not None:
        assert snapshot.contract is not None
        contract_path = contract_root / snapshot.contract.root.path
        catalog += (
            _catalog_entry(
                contract_name, contract_path, "architecture-contract",
                frontmatter=parse_skill_metadata(
                    snapshot.contract.contents[0], source=str(contract_path),
                ) or {},
            ),
        )
        names = [contract_name]
    else:
        names = ["clean-architecture-core", *sorted(
            name for name in routable_group_skills(profile) if is_clean_architecture_skill(name)
        )]
    required_names = expand_dependencies(names, catalog, architecture_mode=selection.mode)
    by_name = {entry.name: entry for entry in catalog}
    required: list[ResolvedSkill] = []
    for name in required_names:
        entry = by_name.get(name)
        if entry is not None and entry.lifecycle == INVALID_FRONTMATTER:
            raise ArchitectureContractError(
                f"required architecture skill {name!r} has invalid frontmatter metadata at {entry.path}"
            )
        if (
            selection.mode is not ArchitectureMode.CLEAN and is_clean_architecture_skill(name)
        ) or (
            name != contract_name and entry is not None and entry.architecture_modes
            and selection.mode.value not in entry.architecture_modes
        ):
            raise ArchitectureContractError(
                f"architecture mode {selection.mode.value!r} is incompatible with required "
                f"skill dependency: {name}"
            )
        if name != contract_name:
            required.append(resolve_skill(name, roots))
    _architecture_norms(contract_root, snapshot, required)


def resolve_phase_skills(
    *,
    project_root: Path,
    phase_id: str,
    phase_skills: PhaseSkills | None = None,
    profile: dict | None = None,
    changed_files: Sequence[str] = (),
    task_text: str = "",
    concerns: Sequence[str] = (),
    host: str | None = None,
    env: dict[str, str] | None = None,
    architecture_root: Path | None = None,
    context: ResolutionContext | None = None,
    provider_authority: str = "",
) -> SkillResolution:
    """phase 선언 + frontmatter 선언 + profile 표/어휘를 합쳐 해석한다.

    required를 만들 수 있는 출처는 넷이다: workflow phase 선언, profile 표,
    project-local drop-box, 그리고 명시된 concern. `task_text`는 offered까지만
    만든다 — 같은 변경을 다른 언어로 적으면 결과가 달라지고, 그러면 작성자와
    reviewer가 같은 기준을 본다는 보장이 문구 표기에 걸린다.
    """
    # 지연 import: 두 모듈이 이 모듈을 되짚어 참조한다.
    from agent_flow.core.profile_routing import routed_profile_skills
    from agent_flow.core.profiles import assert_architecture_override_compatible
    from agent_flow.core.skill_matching import REQUIRED as EXTERNAL_REQUIRED
    from agent_flow.core.skill_matching import match_external

    contract_root = architecture_root or project_root
    context = context or ResolutionContext()
    assert_install_complete(project_root)
    snapshot = context.snapshot(contract_root)
    selection = snapshot.selection
    if snapshot.declared:
        assert_architecture_override_compatible(project_root, selection)
    contract_name = contract_skill_name(selection)
    contract_path = contract_root / selection.contract_path if selection.contract_path else None

    def applicable(name: str) -> bool:
        """Return whether a catalog entry applies to the active phase."""
        if name == contract_name:
            return True
        if selection.mode is not ArchitectureMode.CLEAN and is_clean_architecture_skill(name):
            return False
        entry = catalog_by_name.get(name)
        return entry is None or not entry.architecture_modes or selection.mode.value in entry.architecture_modes

    roots = skill_roots(project_root, profile=profile, host=host, env=env)
    resolved_host = active_host(env) if host is None else host
    resolved_roots = active_host_roots(roots, resolved_host)
    declared = phase_skills or PhaseSkills()
    required_names: list[str] = list(declared.required)
    optional_names: list[str] = list(declared.optional)
    routes = [SkillRoute(name, "phase-required", phase_id) for name in declared.required]
    routes.extend(SkillRoute(name, "phase-optional", phase_id) for name in declared.optional)
    contents: dict[Path, bytes] = {}

    catalog = discover_skill_catalog(
        project_root, resolved_roots, exclude_names=(contract_name,) if contract_name else (),
        contents=contents, context=context, retain_aliases=True,
    )
    if contract_name is not None and contract_path is not None:
        assert snapshot.contract is not None
        contents[contract_path] = snapshot.contract.contents[0].encode("utf-8")
        catalog += (
            _catalog_entry(
                contract_name, contract_path, "architecture-contract",
                frontmatter=parse_skill_metadata(
                    snapshot.contract.contents[0], source=str(contract_path),
                ) or {},
            ),
        )
    catalog_by_name = {entry.name: entry for entry in catalog}
    declared_skill_phases = _profile_skill_phases(project_root, profile, catalog)
    capture = (
        str(project_root.resolve()), str(contract_root.resolve()), phase_id, declared,
        tuple(changed_files), task_text, tuple(concerns),
        yaml.safe_dump(profile, sort_keys=True, allow_unicode=True),
        resolved_host, provider_authority, resolved_roots, snapshot, catalog,
        tuple((str(path), content) for path, content in contents.items()),
        tuple(sorted(declared_skill_phases.items())),
        (os.environ if env is None else env).get("AGENT_FLOW_EXTERNAL_SKILLS", ""),
    )
    previous = context.resolutions.get(resolved_host)
    if previous is not None and previous[0] == capture:
        cached = previous[1]
        norm_names = {document.path for document in cached.architecture_norms}
        current_norms = _architecture_norms(
            contract_root, snapshot,
            (skill for skill in cached.required
             if skill.name != contract_name and skill.path is not None
             and str(skill.path.absolute()) in norm_names),
            contents=contents,
        )
        assert_install_complete(project_root)
        assert_install_complete(contract_root)
        if current_norms == cached.architecture_norms:
            return cached
    if "clean-architecture" in required_names and not declared.pinned_legacy:
        raise ArchitectureContractError(
            "obsolete required skill 'clean-architecture'; migrate the custom requirement "
            "to 'clean-architecture-core' (existing runs require a verified workflow pin)"
        )
    if not declared.replaceable_architecture:
        incompatible_required = [
            name
            for name in required_names
            if (
                selection.mode is not ArchitectureMode.CLEAN
                and is_clean_architecture_skill(name)
            )
            or not applicable(name)
        ]
        if incompatible_required:
            raise ArchitectureContractError(
                f"architecture mode {selection.mode.value!r} is incompatible with explicit "
                f"required workflow skills: {', '.join(incompatible_required)}; migrate "
                "the custom workflow requirements or select a compatible architecture contract"
            )
    required_names = [name for name in required_names if applicable(name)]
    optional_names = [name for name in optional_names if applicable(name)]
    # task 문구로만 켜져 offered로 내려간 이름. 명시된 concern이 그것을 다시 required로
    # 올릴 수 있어야 한다 — 그러지 않으면 강등을 되돌리라고 만든 탈출구가 정확히
    # 강등 대상에만 듣지 않는다.
    demoted: set[str] = set()
    for entry in catalog:
        if not applicable(entry.name):
            continue
        activation = entry_activation(entry, phase_id, changed_files, task_text)
        if activation is None:
            continue
        routes.append(SkillRoute(entry.name, "activation", f"{entry.source}:{activation}"))
        routes.extend(
            SkillRoute(entry.name, "path-selector", f"{pattern}:{path}")
            for pattern in entry.path_globs for path in changed_files
            if _glob_matches(pattern, path)
        )
        routes.extend(
            SkillRoute(entry.name, "task-selector", term)
            for term in entry.task_terms if term_in(term, task_text)
        )
        if entry.name in required_names or entry.name in optional_names:
            continue
        if activation == ACTIVATED_BY_TASK_TERMS and entry.source not in OWNED_SOURCES:
            # 설치된 남의 skill을 task 문구로 required로 올리면 후보 집합이 이 머신의
            # 카탈로그가 되고, 같은 변경이 host마다 다른 required를 만든다. 우리가
            # 배포하는 skill의 `taskTerms`는 저장소가 소유한 어휘라 그 문제가 없다.
            optional_names.append(entry.name)
            demoted.add(entry.name)
        else:
            required_names.append(entry.name)

    # profile 표로 붙는 skill은 upstream 파일이라 frontmatter 선언이 없어도 required다.
    # Profile task aliases must preserve the same phase allowlists as frontmatter triggers.
    for routed in routed_profile_skills(
        profile,
        phase_id=phase_id,
        changed_files=changed_files,
        task_text=task_text,
        concerns=concerns,
        declared_skill_phases=declared_skill_phases,
        preserve_routes=True,
    ):
        if not applicable(routed.name):
            continue
        routes.append(SkillRoute(routed.name, "profile", str(routed)))
        if routed.name not in required_names:
            required_names.append(routed.name)

    matched: dict[str, ResolvedSkill] = {}
    for match in match_external(
        profile,
        catalog,
        phase_id=phase_id,
        changed_files=changed_files,
        task_text=task_text,
        concerns=concerns,
        env=env,
        preserve_routes=True,
    ):
        if not applicable(match.name):
            continue
        routes.append(SkillRoute(match.name, "external", str(match)))
        if match.name in required_names:
            continue
        if match.name in optional_names:
            if match.tier != EXTERNAL_REQUIRED or match.name not in demoted:
                continue
            optional_names.remove(match.name)
        matched[match.name] = ResolvedSkill(
            name=match.name,
            path=match.path,
            source=match.source,
            exists=match.path is not None,
            summary=_description_summary(catalog_by_name[match.name].description)
            if match.name in catalog_by_name else "",
        )
        if match.tier == EXTERNAL_REQUIRED:
            required_names.append(match.name)
        else:
            optional_names.append(match.name)

    if contract_name is not None and contract_name not in required_names:
        required_names.append(contract_name)
    if contract_name is not None:
        routes.append(SkillRoute(contract_name, "architecture-selection", selection.mode.value))
    required_names = expand_dependencies(required_names, catalog, architecture_mode=selection.mode)
    for name in _stable_unique(required_names):
        entry = catalog_by_name.get(name)
        if entry is None:
            continue
        routes.extend(SkillRoute(dependency, "dependency", name) for dependency in entry.dependencies)
        routes.extend(
            SkillRoute(dependency, "architecture-dependency", f"{name}:{selection.mode.value}")
            for dependency in dict(entry.architecture_dependencies).get(selection.mode.value, ())
        )
    if "clean-architecture" in required_names and not declared.pinned_legacy:
        raise ArchitectureContractError(
            "obsolete required skill 'clean-architecture'; migrate its requiring profile "
            "or skill dependency to 'clean-architecture-core'"
        )
    for name in required_names:
        entry = catalog_by_name.get(name)
        if entry is not None and entry.read_error:
            raise ArchitectureContractError(
                f"cannot read required skill {name!r} at {entry.path}: {entry.read_error}"
            )
        if entry is not None and entry.lifecycle == INVALID_FRONTMATTER:
            raise ValueError(
                f"required skill {name!r} ({entry.source}) has invalid frontmatter metadata "
                f"at {entry.path}; repair its YAML before resolving required dependencies"
            )
    incompatible = [name for name in required_names if not applicable(name)]
    if incompatible:
        raise ArchitectureContractError(
            f"architecture mode {selection.mode.value!r} is incompatible with required "
            f"skill dependencies: {', '.join(incompatible)}; declare architecture "
            "applicability at the requiring skill instead of dropping its dependencies"
        )
    optional_names = [name for name in optional_names if name not in required_names]

    # 명시 선택 계약은 이름이 아니라 선언된 경로로 고정한다. 일반 skill은 drop-box가
    # 저장소 파일보다 우선하는데, 그 우선순위를 계약에도 적용하면 같은 이름의 사설
    # 문서가 규범을 대체하고 digest는 저장소 파일을 가리켜 둘이 갈린다.

    def resolve(name: str) -> ResolvedSkill:
        """Resolve one skill and its transitive dependencies."""
        if name == contract_name and contract_path is not None:
            return ResolvedSkill(
                name=name,
                path=contract_path,
                source="architecture-contract",
                exists=True,
                summary=_description_summary(catalog_by_name[name].description),
            )
        found = matched.get(name)
        if found is not None:
            return found
        entry = catalog_by_name.get(name)
        if entry is not None:
            return ResolvedSkill(
                name=name, path=entry.path, source=entry.source, exists=True,
                summary=_description_summary(entry.description),
            )
        return resolve_skill(name, resolved_roots)

    unique_required = _stable_unique(required_names)
    required = tuple(resolve(name) for name in unique_required)
    if declared.pinned_legacy and any(
        skill.name == "clean-architecture" and not skill.exists for skill in required
    ):
        raise ArchitectureContractError(
            "verified legacy workflow requires its pinned 'clean-architecture' skill; "
            "restore the pinned installation rather than silently replacing its norm"
        )
    norm_names = set(expand_dependencies(
        contract_names_in(unique_required, selection), catalog, architecture_mode=selection.mode,
    ))
    norm_skills = (
        skill for skill in required
        if skill.name != contract_name and skill.name in norm_names
    )
    norms = _architecture_norms(contract_root, snapshot, norm_skills, contents=contents)
    normative_documents: list[NormativeDocument] = []
    for skill in required:
        if not skill.exists or skill.path is None:
            continue
        content = contents.get(skill.path)
        if content is None:
            raise ArchitectureContractError(f"required skill was not captured: {skill.path}")
        routes.extend(
            SkillRoute(skill.name, "root", f"{root.source}:{root.host}:{root.template}:{root.install_hint}")
            for root in resolved_roots
            if _match_template(root.template, skill.name) == skill.path
        )
        document = ContractDocument(str(skill.path), hashlib.sha256(content).hexdigest(), len(content))
        normative_documents.append(NormativeDocument(
            document, content, tuple(route for route in routes if route.skill == skill.name),
            inline=skill.name == contract_name,
        ))
    if snapshot.contract is not None:
        for document, text in zip(snapshot.contract.documents[1:], snapshot.contract.contents[1:], strict=True):
            normative_documents.append(NormativeDocument(
                document, text.encode("utf-8"),
                (SkillRoute(contract_name or "", "architecture-reference", document.path),),
                inline=True,
            ))
    resolution = SkillResolution(
        required=required,
        optional=tuple(resolve(name) for name in _stable_unique(optional_names)),
        architecture_contract=contract_names_in(unique_required, selection),
        architecture_snapshot=snapshot,
        architecture_documents=tuple(zip(snapshot.contract.documents, snapshot.contract.contents, strict=True))
        if snapshot.contract else (),
        architecture_norms=norms,
        normative_documents=tuple(normative_documents),
        routes=tuple(routes),
    )
    assert_install_complete(contract_root)
    assert_install_complete(project_root)
    context.resolutions[resolved_host] = (capture, resolution)
    return resolution


def _architecture_norms(
    root: Path, snapshot: ArchitectureSnapshot, required: Iterable[ResolvedSkill],
    *, contents: dict[Path, bytes] | None = None,
) -> tuple[ContractDocument, ...]:
    """Collect pinned normative documents for the selected architecture."""
    documents: dict[str, ContractDocument] = {}
    if snapshot.contract is not None:
        for document in snapshot.contract.documents:
            absolute = str(root / document.path)
            documents[absolute] = ContractDocument(absolute, document.sha256, document.bytes)
    for skill in required:
        if not skill.exists or skill.path is None:
            if snapshot.declared:
                raise ArchitectureContractError(
                    f"missing required architecture skill: {skill.name}; provision the selected "
                    "architecture skills before selecting or using this contract"
                )
            continue
        for path in sorted({skill.path, *skill.path.parent.rglob("*.md")}):
            try:
                content, _ = read_bounded_regular_file(
                    path, max_bytes=MAX_ARCHITECTURE_DOCUMENT_BYTES
                )
            except OSError as exc:
                raise ArchitectureContractError(f"cannot read architecture norm {path}: {exc}") from exc
            if contents is not None:
                captured = contents.get(path)
                if captured is not None and captured != content:
                    raise ArchitectureContractError(f"architecture norm changed during capture: {path}")
                contents[path] = content
            absolute = str(path.absolute())
            documents[absolute] = ContractDocument(
                absolute, hashlib.sha256(content).hexdigest(), len(content)
            )
    return tuple(documents.values())




def _profile_skill_phases(
    project_root: Path, profile: dict | None, catalog: Sequence[SkillCatalogEntry]
) -> dict[str, tuple[str, ...]]:
    """Return phase selectors declared by the active profiles."""
    from agent_flow.core.phase_workflow import find_kit_root
    from agent_flow.core.profile_routing import routable_group_skills

    phases = {
        entry.name: entry.workflow_phases for entry in catalog if entry.phase_declared
    }
    missing = routable_group_skills(profile) - {entry.name for entry in catalog}
    if not missing:
        return phases

    # 파일 부재는 설치 당시의 단계 계약을 지우지 않으며, index는 가용성 증거가 아니다.
    index_path = project_root / ".agent-flow" / "skills" / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        index = {}
    entries = index.get("skills") if isinstance(index, dict) else None
    indexed: set[str] = set()
    for entry in entries if isinstance(entries, list) else ():
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or name not in missing or name in indexed:
            continue
        phases[name] = _string_tuple(entry.get("workflowPhases")) or CODE_PHASES
        indexed.add(name)

    # 아직 설치되지 않은 bundled skill도 같은 정본을 쓰되 읽을 수 있다고 표시하지 않는다.
    missing = missing - indexed
    if missing:
        bundled = find_kit_root() / "skills"
        for name in missing:
            if not is_safe_skill_name(name):
                continue
            metadata = _read_frontmatter(bundled / name / "SKILL.md") or {}
            declared = _string_tuple(metadata.get("workflowPhases"))
            if declared:
                phases[name] = declared
    return phases


def discover_skill_catalog(
    project_root: Path, roots: Sequence[SkillRoot], *, exclude_names: Sequence[str] = (),
    contents: dict[Path, bytes] | None = None,
    retain_aliases: bool = False,
    context: ResolutionContext | None = None,
) -> tuple[SkillCatalogEntry, ...]:
    """Capture document bytes before parsing metadata; reuse only immutable entries."""
    files = tuple(
        (source, path) for source, path in catalog_files(roots)
        if path.parent.name not in exclude_names
    )
    entries: dict[str, SkillCatalogEntry] = {}
    seen_files: set[str] = set()
    for source, skill_path in files:
        name = skill_path.parent.name
        if name in entries or not is_safe_skill_name(name):
            continue
        # 같은 파일이 여러 root에 걸린다(`~/.claude/skills/<n>` → `~/.agents/skills/<n>`
        # symlink가 이 머신 25개 중 15개). 이름이 달라도 같은 파일이면 한 번만 담는다.
        real = os.path.realpath(skill_path)
        if real in seen_files and not retain_aliases:
            continue
        seen_files.add(real)
        try:
            if is_clean_architecture_skill(name):
                content, _ = read_bounded_regular_file(
                    skill_path, max_bytes=MAX_ARCHITECTURE_DOCUMENT_BYTES,
                )
            else:
                content = skill_path.read_bytes()
        except OSError as exc:
            entries[name] = SkillCatalogEntry(
                name=name, path=skill_path, source=source, read_error=str(exc),
            )
            continue
        if contents is not None:
            contents[skill_path] = content
        key = (name, str(skill_path), source, content)
        entry = context.catalog.get(key) if context is not None else None
        if entry is None:
            try:
                metadata = parse_skill_metadata(content.decode("utf-8"), source=str(skill_path)) or {}
            except InvalidSkillFrontmatter:
                metadata = {field: INVALID_FRONTMATTER for field in GOVERNANCE_KEYS}
            entry = _catalog_entry(name, skill_path, source, frontmatter=metadata)
            if context is not None:
                context.catalog[key] = entry
        entries[name] = entry
    if context is not None:
        current = {(entry.name, str(entry.path), entry.source, contents[entry.path])
                   for entry in entries.values() if entry.path in contents} if contents is not None else set()
        context.catalog = {key: entry for key, entry in context.catalog.items() if key in current}
    return tuple(entries.values())


def catalog_files(roots: Sequence[SkillRoot]) -> tuple[tuple[str, Path], ...]:
    return tuple(
        (root.source, skill_path)
        for root in roots
        for skill_path in _iter_root_skills(root.template)
    )


def catalog_stamp(files: Sequence[tuple[str, Path]]) -> str:
    parts = []
    for source, skill_path in files:
        try:
            parts.append((source, str(skill_path), hashlib.sha256(skill_path.read_bytes()).hexdigest()))
        except OSError:
            continue
    return hashlib.sha256(json.dumps(parts).encode("utf-8")).hexdigest()


def skill_prompt_block(
    project_root: Path, resolution: SkillResolution, *, enforced: bool = True,
    role: str = "author",
) -> str:
    """resolver 결과만 프롬프트에 넣는다. profile YAML 전량 dump를 대체한다.

    `enforced`는 이 phase에 실제로 완료 게이트가 걸리는지다. 게이트 없는 phase에서
    "Read every one of these"는 거짓 약속이고, 거짓 약속은 지켜지는 다른 게이트의
    신뢰까지 깎는다. 강제를 늘리는 대신 문구를 사실대로 적는다.

    강제되는 phase에서도 게이트가 하는 일은 **자기신고 요구**다. 읽음 관측은
    진단에만 쓰이고 차단하지 않는다(`local_skills.missing_local_skill_markers`의 L2).
    그러니 여기서 관측 기반 강제를 약속하면 안 된다.

    문구 순서는 취향이 아니다. Vercel의 Next.js 16 eval에서 같은 skill·같은 문서로
    "먼저 skill을 호출하라"는 문서 패턴에 앵커링돼 프로젝트 컨텍스트를 놓쳤고,
    "프로젝트를 먼저 훑고 그 다음 호출하라"가 더 나은 결과를 냈다. 게이트는 순서도
    읽음 여부도 보지 않으므로, 강제는 그대로 두고 순서만 사실대로 권한다.
    """
    if role not in {"author", "reviewer"}:
        raise ValueError(f"unknown envelope role: {role}")
    if not resolution.required and not resolution.optional and resolution.architecture_snapshot is None:
        return ""
    lines = ["\n## Required skills for this phase", ""]
    # 훈련 데이터의 일반 통념과 이 파일이 갈리면 파일이 이긴다. 우리 skill은
    # 일반 개념이 아니라 **이 프로젝트의 규범**이라, 기억으로 대체하면 조용히 틀린다.
    lines.append(
        "Prefer what these files say over what you already know. Where a skill and "
        "your prior knowledge disagree, the skill wins — it is this project's norm, "
        "not the general one."
    )
    lines.append("")
    if resolution.required:
        lines.append(
            "Skim the actual change, then apply every required document below in your "
            "review. Verify the author's completion evidence without writing it; a read "
            "marker alone does not establish rule compliance:"
            if role == "reviewer" else
            "First skim the files this phase actually changes, then read every one of "
            "these before you write or judge code. The completion gate takes your word "
            "for it — it blocks this phase until the artifact records a "
            "`skill-use-evidence` value, and it does not check which files you actually "
            "opened; when nothing was recorded during this phase the block message says "
            "so, and nothing more:"
            if enforced
            else "This phase has no skill read gate. Nothing below is machine-checked "
            "— skim the change first, then read the ones that actually apply:"
        )
        lines.append("")
        if resolution.normative_documents:
            lines.extend((
                "Use the required-read plan below once per exact body. Selected skills "
                "here are provenance only, not additional read instructions:",
                "",
            ))
        for skill in resolution.required:
            if skill.exists:
                lines.append(_skill_prompt_line(project_root, skill))
            else:
                hint = f" — install: {skill.install_hint}" if skill.install_hint else ""
                lines.append(f"- `{skill.name}` — **MISSING**{hint}")
        lines.append("")
    if resolution.optional:
        lines.append("Optional — read only if the change touches their scope:")
        lines.append("")
        for skill in resolution.optional:
            if skill.exists:
                lines.append(_skill_prompt_line(project_root, skill))
        lines.append("")
    if resolution.missing:
        missing = ", ".join(skill.name for skill in resolution.missing)
        lines.append(
            f"Not installed for this host: {missing}. This is not a violation — "
            + ("Report this coverage gap in your review; " if role == "reviewer" else
               "record `skill-availability: degraded` and continue with the skills you do have. ")
            +
            "If you are reviewing, that absence is a coverage gap, not a finding: never make "
            "it a verdict. Do not ask the user to install anything mid-run; "
            "`agent-flow skills sync` owns that."
        )
        lines.append("")
    if resolution.architecture_snapshot is not None:
        snapshot = resolution.architecture_snapshot
        lines.extend((
            "## Selected architecture contract",
            "",
            f"Mode: `{snapshot.selection.mode.value}`. Snapshot SHA-256: `{snapshot.digest}`.",
            "Use this selection for project architecture; shared security, correctness, "
            "review independence, and workflow completion duties still apply.",
        ))
        if snapshot.selection.mode is ArchitectureMode.PENDING:
            lines.append(
                "Architecture is undecided. Continue only work that needs no architecture "
                "decision; a missing standard is not permission to approve structural work."
            )
        lines.append("")
    deliveries = resolution.delivery
    if deliveries:
        lines.extend(("## Required-read plan", ""))
        for index, delivery in enumerate(deliveries, 1):
            if delivery.inline:
                lines.append(f"- Apply inline body {index} below; no file read is required for this body.")
            else:
                lines.append(f"- Read `{delivery.documents[0].document.path}`.")
        lines.extend((
            "",
            "## Normative provenance and inline bodies",
            "",
            "All paths, digests, and routes below are provenance only, not additional "
            "read instructions. Each plan entry satisfies every selection route for "
            "that exact body; all selected obligations still apply.",
            "",
        ))
    for index, delivery in enumerate(deliveries, 1):
        lines.append(f"### Normative body {index}")
        for item in delivery.documents:
            document = item.document
            lines.append(f"- `{document.path}` — SHA-256: `{document.sha256}`. Bytes: {document.bytes}.")
            lines.extend(f"  - route: {route.kind} / {route.skill} / {route.detail}" for route in item.routes)
        if delivery.inline:
            lines.extend(("", delivery.content.decode("utf-8"), ""))
    return "\n".join(lines)


def _skill_prompt_line(project_root: Path, skill: ResolvedSkill) -> str:
    # worktree에서 실행되는 agent는 leader 상대경로를 열 수 없다. 절대경로를 함께 준다.
    relative = skill.display_path(project_root)
    absolute = str(skill.path) if skill.path is not None else ""
    location = f"`{relative}` — `{absolute}`" if absolute and absolute != relative else f"`{relative}`"
    summary = f" — {skill.summary}" if skill.summary else ""
    return f"- `{skill.name}` ({skill.source}) — {location}{summary}"


_SUMMARY_MAX_CHARS = 140


def skill_summary(skill_path: Path) -> str:
    return _description_summary((_read_frontmatter(skill_path) or {}).get("description"))


def _description_summary(raw: object) -> str:
    """Extract a concise summary from a skill description."""
    if not isinstance(raw, str):
        return ""
    text = " ".join(raw.split())
    head = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0] if text else ""
    return f"{head[: _SUMMARY_MAX_CHARS - 1].rstrip()}…" if len(head) > _SUMMARY_MAX_CHARS else head


def _profile_skill_source_roots(
    profile: dict | None, *, env: dict[str, str] | None = None
) -> list[SkillRoot]:
    # 지연 import: skill_sync가 core.commands/security를 끌어와 import 그래프를 넓힌다.
    from agent_flow.core.skill_sync import cached_source_checkout, parse_skill_sources

    roots: list[SkillRoot] = []
    for source in parse_skill_sources(profile):
        label = "fetched" if source.kind == "fetch" else "host"
        hint = source.install_hint or source.id
        roots.extend(
            SkillRoot(
                source=label,
                template=template,
                install_hint=hint,
                host=_template_host(template),
            )
            for template in source.roots
        )
        if source.kind == "fetch" and source.layout:
            checkout = cached_source_checkout(source, env=env)
            if checkout is None:
                continue
            roots.append(
                SkillRoot(
                    source="fetched",
                    template=str(checkout / source.layout),
                    install_hint="agent-flow skills sync",
                )
            )
    return roots


_HOST_TEMPLATE_PREFIXES = {
    "claude": ("~/.claude/",),
    "codex": ("~/.codex/",),
    "omp": ("~/.omp/",),
}


def _template_host(template: str) -> str:
    """profile이 선언한 root가 특정 host 홈에 있으면 그 host 이름.

    profile은 세 host 경로를 나란히 적지만 표는 `active_host_only`를 선언한다.
    태그가 없으면 Codex로 돌면서 Claude 경로의 사본을 읽고도 통과한다.
    """
    normalized = template.replace(str(Path.home()), "~", 1)
    for host, prefixes in _HOST_TEMPLATE_PREFIXES.items():
        if any(normalized.startswith(prefix) for prefix in prefixes):
            return host
    return ""


def _dedupe_roots(roots: Iterable[SkillRoot]) -> list[SkillRoot]:
    seen: set[SkillRoot] = set()
    out: list[SkillRoot] = []
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        out.append(root)
    return out


def _match_template(template: str, name: str) -> Path | None:
    """Match a skill template against the active context."""
    expanded = os.path.expanduser(template.replace("{skill}", name))
    if not any(char in expanded for char in _GLOB_CHARS):
        candidate = Path(expanded)
        if is_clean_architecture_skill(name) and (candidate.exists() or candidate.is_symlink()):
            return candidate
        return candidate if candidate.is_file() else None
    base, pattern = _split_glob(expanded)
    if base is None or not base.is_dir():
        return None
    for match in sorted(base.glob(pattern)):
        if match.is_file() or (
            is_clean_architecture_skill(name) and (match.exists() or match.is_symlink())
        ):
            return match
    return None


def _iter_root_skills(template: str) -> list[Path]:
    """template의 `{skill}`을 와일드카드로 바꿔 해당 root의 모든 SKILL.md를 찾는다."""
    expanded = os.path.expanduser(template.replace("{skill}", "*"))
    base, pattern = _split_glob(expanded)
    if base is None or not base.is_dir():
        return []
    return [match for match in sorted(base.glob(pattern)) if match.is_file()]


def _split_glob(expanded: str) -> tuple[Path | None, str]:
    """Split an expanded glob into its fixed root and relative pattern."""
    parts = Path(expanded).parts
    static: list[str] = []
    for part in parts:
        if any(char in part for char in _GLOB_CHARS):
            break
        static.append(part)
    if not static:
        return None, ""
    base = Path(*static)
    remainder = parts[len(static) :]
    if not remainder:
        return None, ""
    return base, str(Path(*remainder))


def _catalog_entry(
    name: str, skill_path: Path, source: str, *, frontmatter: dict | None = None,
) -> SkillCatalogEntry:
    """Build a catalog entry for an installed skill."""
    if frontmatter is None:
        frontmatter = _read_frontmatter(skill_path) or {}
    phases = _string_tuple(frontmatter.get("workflowPhases"))
    terms = tuple(term.lower() for term in _string_tuple(frontmatter.get("taskTerms")))
    globs = _string_tuple(frontmatter.get("pathGlobs"))
    deps = tuple(frontmatter.get("dependencies", ())) + tuple(frontmatter.get("requires", ()))
    metadata = frontmatter.get("metadata")
    keywords = (
        tuple(word.lower() for word in _string_tuple(metadata.get("keywords")))
        if isinstance(metadata, dict)
        else ()
    )
    return SkillCatalogEntry(
        name=name,
        path=skill_path,
        source=source,
        workflow_phases=phases or CODE_PHASES,
        task_terms=terms,
        path_globs=globs,
        dependencies=deps,
        architecture_modes=tuple(frontmatter.get("architecture_modes", ())),
        architecture_dependencies=tuple(
            (mode, tuple(names))
            for mode, names in frontmatter.get("requires_by_architecture", {}).items()
        ),
        selector_declared=(
            "taskTerms" in frontmatter or "pathGlobs" in frontmatter
        ),
        phase_declared=bool(phases),
        description=str(frontmatter.get("description") or "").strip(),
        keywords=keywords,
        version=_governance_scalar(frontmatter.get("version"), ""),
        owner=_governance_scalar(frontmatter.get("owner"), ""),
        lifecycle=_governance_scalar(
            frontmatter.get("lifecycle"), "active"
        ).lower(),
        approval=_governance_scalar(
            frontmatter.get("approval"), "unattested"
        ).lower(),
        provenance=_governance_scalar(
            frontmatter.get("provenance"), _canonical_provenance(source)
        ),
    )




def _governance_scalar(value: object, fallback: str) -> str:
    """Normalize a governance value while preserving its configured fallback."""
    if value is None:
        return fallback
    if not isinstance(value, str):
        return INVALID_GOVERNANCE_SCALAR
    return value.strip() or fallback


def _canonical_provenance(source: str) -> str:
    """Return the canonical provenance label for a skill source."""
    return "local" if source == "project-local" else source


def _read_frontmatter(skill_path: Path) -> dict | None:
    """Read and validate a skill document's frontmatter."""
    try:
        if is_clean_architecture_skill(skill_path.parent.name):
            payload, _ = read_bounded_regular_file(
                skill_path, max_bytes=MAX_ARCHITECTURE_DOCUMENT_BYTES
            )
            text = payload.decode("utf-8")
        else:
            text = skill_path.read_text(encoding="utf-8")
    except OSError as exc:
        if is_clean_architecture_skill(skill_path.parent.name):
            raise ArchitectureContractError(f"cannot read architecture norm {skill_path}: {exc}") from exc
        return None
    try:
        return parse_skill_metadata(text, source=str(skill_path))
    except InvalidSkillFrontmatter:
        return {key: INVALID_FRONTMATTER for key in GOVERNANCE_KEYS}




def selector_matches(
    *,
    task_terms: Sequence[str],
    path_globs: Sequence[str],
    changed_files: Sequence[str],
    task_text: str,
) -> bool:
    """task 문구나 변경 경로가 선택자에 걸리는가.

    frontmatter 선언과 profile 표가 같은 판정을 쓰도록 여기 하나만 둔다.
    """
    haystack = task_text.lower()
    if any(term_in(term.lower(), haystack) for term in task_terms if term):
        return True
    normalized = [str(path).replace("\\", "/") for path in changed_files]
    return any(
        _glob_matches(pattern, candidate)
        for pattern in path_globs
        for candidate in normalized
    )


_TERM_PATTERNS: dict[str, re.Pattern[str]] = {}


def term_in(term: str, text: str) -> bool:
    r"""단어 경계로 본다.

    부분문자열로 보면 `uistate`가 `GUIStateMachine`에, `chart`가 `charting`에 걸려
    무관한 task가 skill을 required로 올린다.

    경계는 ASCII 영숫자로만 잡는다. `\w`를 쓰면 한글이 word 문자라서 `화면 상태를`
    처럼 조사가 붙은 한국어 task 문구가 전부 미매치가 된다. 그래서 한글 term은
    경계가 없는 것과 같다 - `프레젠테이션`은 "팀 프레젠테이션으로"에 계속 걸린다.
    그 오탐은 어휘를 구절로 좁혀서 막는다.

    복수 접미사는 허용한다. skill description이 term을 복수로 쓰는 쪽이 흔하다 —
    `skill_matching._term_in`과 같은 규칙이고, 그쪽이 이 함수를 쓴다.
    """
    pattern = _TERM_PATTERNS.get(term)
    if pattern is None:
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?:e?s)?(?![A-Za-z0-9_])")
        _TERM_PATTERNS[term] = pattern
    return pattern.search(text) is not None


def entry_can_activate(entry: SkillCatalogEntry) -> bool:
    """phase와 변경 범위를 빼고, 이 엔트리가 **켜질 수 있는가**.

    `_entry_activates`의 phase/선택자 대조 앞부분이 곧 이 질문이고, doctor의 도달
    가능성 판정도 같은 질문을 한다. 두 곳이 각자 조건을 적으면 조용히 갈라진다.
    """
    if entry.selector_declared and not (entry.task_terms or entry.path_globs):
        # 선언했는데 전부 빈 값이면 "무조건 활성화"가 아니라 "아무것도 안 걸림"이다.
        # 그렇지 않으면 `taskTerms: ""` 하나로 모든 phase에 조용히 얹힌다.
        return False
    if entry.source == "project-local":
        # drop-box는 사용자가 넣은 것 자체가 근거다. 선언을 요구하지 않는다.
        return True
    # upstream SKILL.md는 `workflowPhases`를 선언하지 않는다. 카탈로그에는 담되
    # 자동 활성화는 하지 않는다 — 이 가드가 없으면 host에 깔린 skill 전량이
    # 선택자 없는 엔트리로 required가 된다.
    return bool(entry.phase_declared and entry.workflow_phases)


# 이름과 어휘를 저장소가 소유하는 소스. 이쪽 `taskTerms`는 머신 구성에 따라 달라지지
# 않으므로 required를 만들 수 있다.
OWNED_SOURCES = frozenset({"project-local", "project", "bundled"})

ACTIVATED_BY_PLACEMENT = "placement"
ACTIVATED_BY_PATH = "path-globs"
ACTIVATED_BY_TASK_TERMS = "task-terms"
ACTIVATED_BY_DECLARATION = "declaration"


def entry_activation(
    entry: SkillCatalogEntry, phase_id: str, changed_files: Sequence[str], task_text: str
) -> str | None:
    """이 엔트리가 왜 켜졌는가. 안 켜지면 ``None``.

    tier가 이유에 걸린다. 경로와 배치는 결정론적이라 required를 만들 수 있고,
    task 문구는 표기에 따라 달라지므로 offered까지만 만든다.
    """
    if phase_id not in entry.workflow_phases:
        return None
    if not entry_can_activate(entry):
        return None
    if not entry.selector_declared and not entry.task_terms and not entry.path_globs:
        # drop-box는 거기 둔 것 자체가 선언이다. 그 밖의 소스는
        # `entry_can_activate`가 이미 `workflowPhases` 선언을 요구했다.
        return (
            ACTIVATED_BY_PLACEMENT
            if entry.source == "project-local"
            else ACTIVATED_BY_DECLARATION
        )
    if selector_matches(
        task_terms=(),
        path_globs=entry.path_globs,
        changed_files=changed_files,
        task_text="",
    ):
        return ACTIVATED_BY_PATH
    if selector_matches(
        task_terms=entry.task_terms,
        path_globs=(),
        changed_files=(),
        task_text=task_text,
    ):
        return ACTIVATED_BY_TASK_TERMS
    return None


def _glob_matches(pattern: str, candidate: str) -> bool:
    """Return whether a path matches a configured skill glob."""
    # 대소문자를 접어서 본다. `fnmatch`는 POSIX에서 대소문자를 구분하므로
    # `Button.TSX`가 `**/*.tsx`에 안 걸린다. 확장자 표기 하나로 skill 강제가
    # 사라지는 쪽이 문서 한 장을 더 읽는 쪽보다 나쁘다.
    folded_pattern = pattern.lower()
    folded_candidate = candidate.lower()
    if fnmatch(folded_candidate, folded_pattern):
        return True
    # `**/x` 는 최상위 경로에도 걸려야 한다. fnmatch는 이를 처리하지 않는다.
    if folded_pattern.startswith("**/"):
        return fnmatch(folded_candidate, folded_pattern[3:])
    return False


def expand_dependencies(
    names: Sequence[str],
    catalog: Sequence[SkillCatalogEntry],
    *,
    architecture_mode: ArchitectureMode = ArchitectureMode.CLEAN,
) -> list[str]:
    """Expand required skill dependencies in stable order."""
    by_name = {entry.name: entry for entry in catalog}
    out = list(names)
    queue = list(names)
    while queue:
        entry = by_name.get(queue.pop())
        if entry is None:
            continue
        conditional = dict(entry.architecture_dependencies).get(architecture_mode.value, ())
        for dependency in (*entry.dependencies, *conditional):
            if dependency not in out:
                out.append(dependency)
                queue.append(dependency)
    return out


def _stable_unique(names: Iterable[str]) -> list[str]:
    """Return values in first-seen order without duplicates."""
    return list(dict.fromkeys(name for name in names if is_safe_skill_name(name)))


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        # 빈/공백 문자열은 `"" in haystack`이 항상 참이라 무조건 활성화된다.
        return (value,) if value.strip() else ()
    if isinstance(value, list):
        return tuple(str(item) for item in value if str(item).strip())
    return ()

