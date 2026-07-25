from __future__ import annotations

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
)

_HOST_TEMPLATES = {
    "claude": ("~/.claude/skills/{skill}/SKILL.md",),
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


@dataclass(frozen=True)
class SkillRoot:
    """하나의 skill 탐색 위치. template은 `{skill}` 자리표시자와 glob을 허용한다."""

    source: str
    template: str
    install_hint: str = ""


@dataclass(frozen=True)
class ResolvedSkill:
    name: str
    path: Path | None
    source: str
    exists: bool
    install_hint: str = ""

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
            SkillRoot(source="host", template=template) for template in _HOST_TEMPLATES[name]
        )
    roots.extend(SkillRoot(source="shared", template=template) for template in _SHARED_TEMPLATES)
    roots.extend(_profile_skill_source_roots(profile))
    return tuple(_dedupe_roots(roots))


def resolve_skill(name: str, roots: Sequence[SkillRoot]) -> ResolvedSkill:
    """첫 매치가 이긴다. 어디에도 없으면 exists=False와 설치 안내를 담아 돌려준다."""
    if not _is_safe_skill_name(name):
        return ResolvedSkill(name=name, path=None, source="", exists=False, install_hint="")
    hints: list[str] = []
    for root in roots:
        match = _match_template(root.template, name)
        if match is not None:
            return ResolvedSkill(name=name, path=match, source=root.source, exists=True)
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
    """phase가 선언한 skill + 스스로 이 phase에 걸린다고 선언한 skill을 합쳐 해석한다."""
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

    required_names = _expand_dependencies(required_names, catalog)
    optional_names = [name for name in optional_names if name not in required_names]

    return SkillResolution(
        required=tuple(resolve_skill(name, roots) for name in _stable_unique(required_names)),
        optional=tuple(resolve_skill(name, roots) for name in _stable_unique(optional_names)),
    )


_CATALOG_CACHE: dict[tuple[str, ...], tuple["SkillCatalogEntry", ...]] = {}


def discover_skill_catalog(
    project_root: Path, roots: Sequence[SkillRoot]
) -> tuple[SkillCatalogEntry, ...]:
    """root들을 훑어 frontmatter 활성화 선언이 있는 skill만 카탈로그로 만든다.

    한 번의 marker 검증에서 여러 번 불리고 매번 모든 SKILL.md를 읽는다.
    프로세스 수명 동안만 캐시한다 — CLI는 단명이라 stale 위험이 없다.
    """
    key = (str(project_root.resolve()),) + tuple(root.template for root in roots)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached
    entries: dict[str, SkillCatalogEntry] = {}
    for root in roots:
        for skill_path in _iter_root_skills(root.template):
            name = skill_path.parent.name
            if name in entries or not _is_safe_skill_name(name):
                continue
            entry = _catalog_entry(name, skill_path, root.source)
            if entry is not None:
                entries[name] = entry
    result = tuple(entries.values())
    _CATALOG_CACHE[key] = result
    return result


def skill_prompt_block(project_root: Path, resolution: SkillResolution) -> str:
    """resolver 결과만 프롬프트에 넣는다. profile YAML 전량 dump를 대체한다."""
    if not resolution.required and not resolution.optional:
        return ""
    lines = ["\n## Required skills for this phase", ""]
    if resolution.required:
        lines.append("Read every one of these before writing or judging code:")
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
    if absolute and absolute != relative:
        return f"- `{skill.name}` ({skill.source}) — `{relative}` — `{absolute}`"
    return f"- `{skill.name}` ({skill.source}) — `{relative}`"


def _profile_skill_source_roots(profile: dict | None) -> list[SkillRoot]:
    # 지연 import: skill_sync가 core.commands/security를 끌어와 import 그래프를 넓힌다.
    from agent_flow.core.skill_sync import cache_root, parse_skill_sources

    roots: list[SkillRoot] = []
    for source in parse_skill_sources(profile):
        label = "fetched" if source.kind == "fetch" else "host"
        hint = source.install_hint or source.id
        roots.extend(
            SkillRoot(source=label, template=template, install_hint=hint)
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


def _catalog_entry(name: str, skill_path: Path, source: str) -> SkillCatalogEntry | None:
    frontmatter = _read_frontmatter(skill_path) or {}
    phases = _string_tuple(frontmatter.get("workflowPhases"))
    terms = tuple(term.lower() for term in _string_tuple(frontmatter.get("taskTerms")))
    globs = _string_tuple(frontmatter.get("pathGlobs"))
    deps = _string_tuple(frontmatter.get("dependencies")) + _string_tuple(
        frontmatter.get("requires")
    )
    if not phases:
        if source != "project-local":
            return None
        phases = CODE_PHASES
    return SkillCatalogEntry(
        name=name,
        path=skill_path,
        source=source,
        workflow_phases=phases,
        task_terms=terms,
        path_globs=globs,
        dependencies=deps,
        selector_declared=(
            "taskTerms" in frontmatter or "pathGlobs" in frontmatter
        ),
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


def _entry_activates(
    entry: SkillCatalogEntry, phase_id: str, changed_files: Sequence[str], task_text: str
) -> bool:
    if phase_id not in entry.workflow_phases:
        return False
    if entry.selector_declared:
        # 선언했는데 전부 빈 값이면 "무조건 활성화"가 아니라 "아무것도 안 걸림"이다.
        # 그렇지 않으면 `taskTerms: ""` 하나로 모든 phase에 조용히 얹힌다.
        if not entry.task_terms and not entry.path_globs:
            return False
    elif not entry.task_terms and not entry.path_globs:
        return True
    haystack = task_text.lower()
    if any(term in haystack for term in entry.task_terms):
        return True
    normalized = [str(path).replace("\\", "/") for path in changed_files]
    return any(
        _glob_matches(pattern, candidate)
        for pattern in entry.path_globs
        for candidate in normalized
    )


def _glob_matches(pattern: str, candidate: str) -> bool:
    if fnmatch(candidate, pattern):
        return True
    # `**/x` 는 최상위 경로에도 걸려야 한다. fnmatch는 이를 처리하지 않는다.
    if pattern.startswith("**/"):
        return fnmatch(candidate, pattern[3:])
    return False


def _expand_dependencies(names: Sequence[str], catalog: Sequence[SkillCatalogEntry]) -> list[str]:
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
