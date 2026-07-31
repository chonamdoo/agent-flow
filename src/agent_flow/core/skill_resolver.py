from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable, Sequence

import yaml

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
class SkillResolution:
    required: tuple[ResolvedSkill, ...] = ()
    optional: tuple[ResolvedSkill, ...] = ()

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

    def is_empty(self) -> bool:
        return not self.required and not self.optional


@dataclass
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
    roots.extend(_profile_skill_source_roots(profile))
    return tuple(_dedupe_roots(roots))


def active_host_roots(roots: Sequence[SkillRoot], host: str) -> tuple[SkillRoot, ...]:
    """다른 host 소유 root를 뺀 목록.

    host를 못 알아내면(`""`) 거를 기준이 없다. 그때 전부 빼면 CI처럼 host 표식이
    없는 환경에서 모든 skill이 사라지므로, 알 수 없을 때는 거르지 않는다.
    """
    if not host:
        return tuple(roots)
    return tuple(root for root in roots if not root.host or root.host == host)


def resolve_skill(
    name: str, roots: Sequence[SkillRoot], *, install_hint: str = ""
) -> ResolvedSkill:
    """첫 매치가 이긴다. 어디에도 없으면 exists=False와 설치 안내를 담아 돌려준다."""
    if not _is_safe_skill_name(name):
        return ResolvedSkill(name=name, path=None, source="", exists=False, install_hint="")
    hints: list[str] = [install_hint] if install_hint else []
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


def resolve_phase_skills(
    *,
    project_root: Path,
    phase_id: str,
    phase_skills: PhaseSkills | None = None,
    profile: dict | None = None,
    changed_files: Sequence[str] = (),
    task_text: str = "",
    host: str | None = None,
    env: dict[str, str] | None = None,
) -> SkillResolution:
    """phase 선언 + frontmatter 선언 + profile 표/어휘를 합쳐 해석한다."""
    # 지연 import: 두 모듈이 이 모듈을 되짚어 참조한다.
    from agent_flow.core.profile_routing import routed_profile_skills
    from agent_flow.core.skill_matching import REQUIRED as EXTERNAL_REQUIRED
    from agent_flow.core.skill_matching import match_external

    roots = skill_roots(project_root, profile=profile, host=host, env=env)
    declared = phase_skills or PhaseSkills()
    required_names: list[str] = list(declared.required)
    optional_names: list[str] = list(declared.optional)

    catalog = discover_skill_catalog(project_root, roots)
    for entry in catalog:
        if entry.name in required_names or entry.name in optional_names:
            continue
        if _entry_activates(entry, phase_id, changed_files, task_text):
            required_names.append(entry.name)

    # profile 표로 붙는 skill은 upstream 파일이라 frontmatter 선언이 없다.
    # 부재 안내는 표의 source URL로 한다 — root의 일반 install hint로는
    # 어느 저장소에서 받아야 하는지 알 수 없다.
    routed_hints: dict[str, str] = {}
    routed_only: set[str] = set()
    for routed in routed_profile_skills(
        profile, phase_id=phase_id, changed_files=changed_files, task_text=task_text
    ):
        if routed.source:
            routed_hints.setdefault(routed.name, routed.source)
        if routed.name not in required_names:
            required_names.append(routed.name)
            routed_only.add(routed.name)

    # 어휘 조인으로 붙는 설치 skill. 카탈로그가 이미 실제 경로를 아는 것만 담으므로
    # 이름으로 다시 해석하지 않는다 — host 필터로 재해석하면 다른 host 홈에 설치된
    # skill을 매칭해 required로 올린 뒤 "설치돼 있지 않다"고 보고한다.
    matched: dict[str, ResolvedSkill] = {}
    for match in match_external(
        profile,
        catalog,
        phase_id=phase_id,
        changed_files=changed_files,
        task_text=task_text,
        env=env,
    ):
        if match.name in required_names or match.name in optional_names:
            continue
        matched[match.name] = ResolvedSkill(
            name=match.name,
            path=match.path,
            source=match.source,
            exists=match.path is not None,
            summary=skill_summary(match.path) if match.path is not None else "",
        )
        if match.tier == EXTERNAL_REQUIRED:
            required_names.append(match.name)
        else:
            optional_names.append(match.name)

    required_names = expand_dependencies(required_names, catalog)
    optional_names = [name for name in optional_names if name not in required_names]

    # 우리가 이름으로 선언한 skill만 활성 host로 좁힌다. 그 이름은 미설치일 수 있고,
    # 다른 host의 사본으로 충족시키면 프롬프트가 엉뚱한 파일을 가리킨다.
    resolved_host = active_host(env) if host is None else host
    routed_roots = active_host_roots(roots, resolved_host)

    def resolve(name: str) -> ResolvedSkill:
        found = matched.get(name)
        if found is not None:
            return found
        return resolve_skill(
            name,
            routed_roots if name in routed_only else roots,
            install_hint=routed_hints.get(name, ""),
        )

    return SkillResolution(
        required=tuple(resolve(name) for name in _stable_unique(required_names)),
        optional=tuple(resolve(name) for name in _stable_unique(optional_names)),
    )


_CATALOG_CACHE: dict[tuple[tuple[str, ...], str], tuple["SkillCatalogEntry", ...]] = {}


def discover_skill_catalog(
    project_root: Path, roots: Sequence[SkillRoot]
) -> tuple[SkillCatalogEntry, ...]:
    """root들을 훑어 발견한 모든 SKILL.md를 카탈로그로 만든다.

    한 번의 marker 검증에서 여러 번 불리고 매번 모든 SKILL.md를 읽는다.
    디스크 스탬프를 캐시 키에 넣는다 — template만 키로 쓰면 같은 프로세스 안에서
    skill이 갱신되거나 새로 깔려도 낡은 카탈로그를 계속 쓴다.
    """
    files = catalog_files(roots)
    key = (tuple(root.template for root in roots), catalog_stamp(files))
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached
    entries: dict[str, SkillCatalogEntry] = {}
    seen_files: set[str] = set()
    for source, skill_path in files:
        name = skill_path.parent.name
        if name in entries or not _is_safe_skill_name(name):
            continue
        # 같은 파일이 여러 root에 걸린다(`~/.claude/skills/<n>` → `~/.agents/skills/<n>`
        # symlink가 이 머신 25개 중 15개). 이름이 달라도 같은 파일이면 한 번만 담는다.
        real = os.path.realpath(skill_path)
        if real in seen_files:
            continue
        seen_files.add(real)
        entries[name] = _catalog_entry(name, skill_path, source)
    result = tuple(entries.values())
    _CATALOG_CACHE[key] = result
    return result


def catalog_files(roots: Sequence[SkillRoot]) -> tuple[tuple[str, Path], ...]:
    return tuple(
        (root.source, skill_path)
        for root in roots
        for skill_path in _iter_root_skills(root.template)
    )


def catalog_stamp(files: Sequence[tuple[str, Path]]) -> str:
    """카탈로그 무효화 스탬프. 내용 해시가 아니라 stat이다 — 실측 131개에 0.4ms."""
    parts: list[str] = []
    for _source, skill_path in files:
        try:
            info = skill_path.stat()
        except OSError:
            continue
        parts.append(f"{skill_path}:{info.st_mtime_ns}:{info.st_size}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def skill_prompt_block(
    project_root: Path, resolution: SkillResolution, *, enforced: bool = True
) -> str:
    """resolver 결과만 프롬프트에 넣는다. profile YAML 전량 dump를 대체한다.

    `enforced`는 이 phase에 실제로 read gate가 걸리는지다. 게이트 없는 phase에서
    "Read every one of these"는 거짓 약속이고, 거짓 약속은 지켜지는 다른 게이트의
    신뢰까지 깎는다. 강제를 늘리는 대신 문구를 사실대로 적는다.

    문구 순서는 취향이 아니다. Vercel의 Next.js 16 eval에서 같은 skill·같은 문서로
    "먼저 skill을 호출하라"는 문서 패턴에 앵커링돼 프로젝트 컨텍스트를 놓쳤고,
    "프로젝트를 먼저 훑고 그 다음 호출하라"가 더 나은 결과를 냈다. 게이트는 읽음
    여부만 보고 순서는 보지 않으므로, 강제는 그대로 두고 순서만 사실대로 권한다.
    """
    if not resolution.required and not resolution.optional:
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
            "First skim the files this phase actually changes, then read every one of "
            "these before you write or judge code. The completion gate blocks this "
            "phase when a listed skill exists on disk and was never opened during it:"
            if enforced
            else "This phase has no skill read gate. Nothing below is machine-checked "
            "— skim the change first, then read the ones that actually apply:"
        )
        lines.append("")
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
            f"Not installed on this machine: {missing}. This is not a violation — "
            "record `skill-availability: degraded` and continue with the skills you do have. "
            "Do not ask the user to install anything mid-run; `agent-flow skills sync` owns that."
        )
        lines.append("")
    return "\n".join(lines)


def _skill_prompt_line(project_root: Path, skill: ResolvedSkill) -> str:
    # worktree에서 실행되는 agent는 leader 상대경로를 열 수 없다. 절대경로를 함께 준다.
    relative = skill.display_path(project_root)
    absolute = str(skill.path) if skill.path is not None else ""
    location = f"`{relative}` — `{absolute}`" if absolute and absolute != relative else f"`{relative}`"
    summary = f" — {skill.summary}" if skill.summary else ""
    return f"- `{skill.name}` ({skill.source}) — {location}{summary}"


_SUMMARY_CACHE: dict[str, str] = {}
_SUMMARY_MAX_CHARS = 140


def skill_summary(skill_path: Path) -> str:
    """frontmatter `description`의 첫 문장. 없으면 빈 문자열.

    전문을 넣으면 목록이 곧 문서가 된다 — 압축이 목적이다. 한 문장이면 "이 변경이
    이 skill의 scope에 걸리는가"를 판단하기에 충분하고, 아니면 파일을 열면 된다.
    프로세스 수명 동안만 캐시한다. CLI는 단명이라 stale 위험이 없다.
    """
    key = str(skill_path)
    cached = _SUMMARY_CACHE.get(key)
    if cached is not None:
        return cached
    raw = (_read_frontmatter(skill_path) or {}).get("description")
    summary = ""
    if isinstance(raw, str):
        text = " ".join(raw.split())
        head = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0] if text else ""
        if len(head) > _SUMMARY_MAX_CHARS:
            head = f"{head[: _SUMMARY_MAX_CHARS - 1].rstrip()}…"
        summary = head
    _SUMMARY_CACHE[key] = summary
    return summary


def _profile_skill_source_roots(profile: dict | None) -> list[SkillRoot]:
    # 지연 import: skill_sync가 core.commands/security를 끌어와 import 그래프를 넓힌다.
    from agent_flow.core.skill_sync import cache_root, parse_skill_sources

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
            checkout = cache_root() / source.id / (source.ref or "HEAD")
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
    seen: set[str] = set()
    out: list[SkillRoot] = []
    for root in roots:
        if root.template in seen:
            continue
        seen.add(root.template)
        out.append(root)
    return out


def _match_template(template: str, name: str) -> Path | None:
    expanded = os.path.expanduser(template.replace("{skill}", name))
    if not any(char in expanded for char in _GLOB_CHARS):
        candidate = Path(expanded)
        return candidate if candidate.is_file() else None
    base, pattern = _split_glob(expanded)
    if base is None or not base.is_dir():
        return None
    for match in sorted(base.glob(pattern)):
        if match.is_file():
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


def _catalog_entry(name: str, skill_path: Path, source: str) -> SkillCatalogEntry:
    frontmatter = _read_frontmatter(skill_path) or {}
    phases = _string_tuple(frontmatter.get("workflowPhases"))
    terms = tuple(term.lower() for term in _string_tuple(frontmatter.get("taskTerms")))
    globs = _string_tuple(frontmatter.get("pathGlobs"))
    deps = _string_tuple(frontmatter.get("dependencies")) + _string_tuple(
        frontmatter.get("requires")
    )
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
        selector_declared=(
            "taskTerms" in frontmatter or "pathGlobs" in frontmatter
        ),
        phase_declared=bool(phases),
        description=str(frontmatter.get("description") or "").strip(),
        keywords=keywords,
    )


def _read_frontmatter(skill_path: Path) -> dict | None:
    try:
        text = skill_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.match(r"\A---\n(?P<body>[\s\S]*?)\n---", text)
    if not match:
        return None
    try:
        parsed = yaml.safe_load(match.group("body"))
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None


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
    """단어 경계로 본다.

    부분문자열로 보면 `프레젠테이션`이 "팀 프레젠테이션으로 정리한다"에 걸리고
    `uistate`가 `GUIStateMachine`에 걸려, 무관한 task가 skill을 required로 올린다.

    경계는 ASCII 영숫자로만 잡는다. `\w`를 쓰면 한글이 word 문자라서 `화면 상태를`
    처럼 조사가 붙은 한국어 task 문구가 전부 미매치가 된다. 복수 접미사는 허용한다 —
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


def _entry_activates(
    entry: SkillCatalogEntry, phase_id: str, changed_files: Sequence[str], task_text: str
) -> bool:
    if phase_id not in entry.workflow_phases:
        return False
    if not entry_can_activate(entry):
        return False
    if not entry.selector_declared and not entry.task_terms and not entry.path_globs:
        return True
    return selector_matches(
        task_terms=entry.task_terms,
        path_globs=entry.path_globs,
        changed_files=changed_files,
        task_text=task_text,
    )


def _glob_matches(pattern: str, candidate: str) -> bool:
    if fnmatch(candidate, pattern):
        return True
    # `**/x` 는 최상위 경로에도 걸려야 한다. fnmatch는 이를 처리하지 않는다.
    if pattern.startswith("**/"):
        return fnmatch(candidate, pattern[3:])
    return False


def expand_dependencies(names: Sequence[str], catalog: Sequence[SkillCatalogEntry]) -> list[str]:
    by_name = {entry.name: entry for entry in catalog}
    out = list(names)
    queue = list(names)
    while queue:
        entry = by_name.get(queue.pop())
        if entry is None:
            continue
        for dependency in entry.dependencies:
            if dependency not in out:
                out.append(dependency)
                queue.append(dependency)
    return out


def _stable_unique(names: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(name for name in names if _is_safe_skill_name(name)))


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        # 빈/공백 문자열은 `"" in haystack`이 항상 참이라 무조건 활성화된다.
        return (value,) if value.strip() else ()
    if isinstance(value, list):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def _is_safe_skill_name(value: object) -> bool:
    name = str(value)
    # `.`/`..`은 위 정규식을 통과하면서 경로를 한 단계 올린다. 이름으로 취급하지 않는다.
    if name in {".", ".."} or set(name) <= {"."}:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._-]+", name))
