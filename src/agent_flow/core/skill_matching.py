"""설치된 외부 skill을 이름 열거 없이 라우팅한다.

이름을 우리 YAML에 적으면 우리 파일은 upstream보다 항상 낡는다 — `android/skills`는
디렉터리를 지우고 zip을 통째로 재추출하므로 rename 이력이 upstream 자신에게도 남지 않고,
실측으로 6개월에 이름 35%가 바뀌었다. 그래서 소비자는 **어휘와 범위**만 선언하고,
판정은 upstream이 스스로 쓴 `name` + `description`(+ 있으면 `metadata.keywords`)에 대해
런의 task 문구와 변경 경로를 조인해서 한다.

두 축이 모두 필요하다. 어휘가 skill 쪽에만 걸리면 그 skill은 이 저장소와 무관한 것이고,
런 쪽에만 걸리면 그 일을 도와줄 skill이 안 깔린 것이다. 둘 다 걸릴 때만 required가 된다.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from agent_flow.core.profile_routing import IMPLEMENTATION_PHASES, REVIEW_PHASES
from agent_flow.core.skill_resolver import SkillCatalogEntry, selector_matches

REQUIRED = "required"
OFFERED = "offered"

# 어휘로 라우팅되는 것은 설치된 외부 skill뿐이다. 우리가 배포하는 것은 이름을
# 우리가 소유하므로 `profile_routing`이 담당한다.
_EXTERNAL_SOURCES = frozenset({"host", "shared", "fetched", "vendor"})

_SECTION_PHASES = {
    "implementation": IMPLEMENTATION_PHASES,
    "review": REVIEW_PHASES,
}

# `AGENT_FLOW_EXTERNAL_SKILLS`로 profile 값을 덮는다. 단계 전환과 사고 시 되돌리기 지점이다.
_ENV_KEY = "AGENT_FLOW_EXTERNAL_SKILLS"


@dataclass(frozen=True)
class ExternalDomain:
    id: str
    terms: tuple[str, ...] = ()
    path_globs: tuple[str, ...] = ()
    phases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExternalConfig:
    enabled: bool = False
    required_max: int = 6
    offered_max: int = 20
    pins: tuple[str, ...] = ()
    domains: tuple[ExternalDomain, ...] = ()


@dataclass(frozen=True)
class ExternalMatch:
    name: str
    domain: str
    tier: str
    term: str
    # 카탈로그가 이미 아는 실제 경로. 이름으로 다시 해석하면 활성 host 필터에 걸려
    # 설치돼 있는 skill을 "없다"고 보고한다.
    path: Path | None = None
    source: str = "host"


def parse_external(profile: dict | None, *, env: dict[str, str] | None = None) -> ExternalConfig:
    block = None
    if isinstance(profile, dict):
        skills = profile.get("skills")
        if isinstance(skills, dict) and isinstance(skills.get("external"), dict):
            block = skills["external"]
    if block is None:
        block = {}
    return ExternalConfig(
        enabled=_enabled(bool(block.get("enabled", False)), env),
        required_max=_positive_int(block.get("required_max"), 6),
        offered_max=_positive_int(block.get("offered_max"), 20),
        pins=tuple(_string_list(block.get("pins"))),
        domains=tuple(_domains(block.get("domains"))),
    )


def match_external(
    profile: dict | None,
    catalog: Sequence[SkillCatalogEntry],
    *,
    phase_id: str,
    changed_files: Sequence[str] = (),
    task_text: str = "",
    env: dict[str, str] | None = None,
) -> tuple[ExternalMatch, ...]:
    """이번 phase/범위에서 활성화되는 설치 skill. 순서까지 결정론이다."""
    config = parse_external(profile, env=env)
    if not config.enabled:
        return ()
    haystack = task_text.lower()
    matches: dict[str, ExternalMatch] = {}
    for domain in config.domains:
        if not _domain_active(domain, phase_id, changed_files, task_text):
            continue
        for entry in catalog:
            if entry.source not in _EXTERNAL_SOURCES:
                continue
            terms = _entry_terms(entry, domain.terms)
            if not terms:
                continue
            # tier는 "어느 term이 양쪽에 다 걸렸나"로 정한다. 첫 매치만 보면 skill 쪽에서
            # 먼저 걸린 term이 task 쪽에 없다는 이유로 required가 offered로 떨어진다.
            shared = next((term for term in terms if _term_in(term, haystack)), None)
            tier = REQUIRED if shared or entry.name in config.pins else OFFERED
            existing = matches.get(entry.name)
            if existing is not None and (existing.tier == REQUIRED or tier == OFFERED):
                continue
            matches[entry.name] = ExternalMatch(
                entry.name, domain.id, tier, shared or terms[0], entry.path, entry.source
            )
    return _truncate(matches.values(), config)


def routable_names(
    profile: dict | None,
    catalog: Sequence[SkillCatalogEntry],
    *,
    env: dict[str, str] | None = None,
) -> set[str]:
    """어느 domain의 어휘에든 걸릴 수 있는 skill 이름.

    `match_external`은 이번 run의 task/경로까지 본다. doctor는 그것과 무관하게
    "이 skill이 어떤 run에서든 라우팅될 수 있는가"를 물어야 한다 — 안 그러면 정상
    라우팅되는 skill을 매번 미라우팅으로 보고한다.
    """
    config = parse_external(profile, env=env)
    if not config.enabled:
        # routing이 꺼져 있으면 도달 가능한 이름도 없다. 아니면 doctor가 유령 이름을
        # 도달 가능으로 세어 실제로 죽어 있는 skill의 보고를 지운다.
        return set()
    names: set[str] = set()
    for domain in config.domains:
        for entry in catalog:
            if entry.source not in _EXTERNAL_SOURCES or entry.name in names:
                continue
            if _entry_terms(entry, domain.terms):
                names.add(entry.name)
    return names


def _truncate(
    matches: Sequence[ExternalMatch] | tuple[ExternalMatch, ...], config: ExternalConfig
) -> tuple[ExternalMatch, ...]:
    ordered = sorted(matches, key=lambda item: (item.tier != REQUIRED, item.domain, item.name))
    required = _round_robin([item for item in ordered if item.tier == REQUIRED], config.required_max)
    offered = _round_robin([item for item in ordered if item.tier == OFFERED], config.offered_max)
    return tuple(required + offered)


def _round_robin(matches: Sequence[ExternalMatch], limit: int) -> list[ExternalMatch]:
    """domain마다 한 자리씩 먼저 준다.

    이름 순으로 그냥 자르면 매치가 많은 domain 하나가 나머지를 굶긴다 — 실측으로
    recomposition 어휘에 걸린 8개가 `edge-to-edge`를 required에서 밀어냈다.
    """
    buckets: dict[str, list[ExternalMatch]] = {}
    for match in matches:
        buckets.setdefault(match.domain, []).append(match)
    out: list[ExternalMatch] = []
    while len(out) < limit and any(buckets.values()):
        for queue in buckets.values():
            if len(out) >= limit:
                break
            if queue:
                out.append(queue.pop(0))
    return out


def _domain_active(
    domain: ExternalDomain,
    phase_id: str,
    changed_files: Sequence[str],
    task_text: str,
) -> bool:
    if not domain.terms:
        # 어휘 없는 domain은 활성화 근거가 없다. profile 표의 선택자 없는 엔트리와 같은 규칙이다.
        return False
    if not any(phase_id in _SECTION_PHASES.get(section, frozenset()) for section in domain.phases):
        return False
    return selector_matches(
        task_terms=domain.terms,
        path_globs=domain.path_globs,
        changed_files=changed_files,
        task_text=task_text,
    )


# skill이 자기 description에 배제 조항을 쓴다. 실측: `docx`가 "Do NOT use for PDFs,
# spreadsheets…"를 실어서 `spreadsheet` 어휘에 걸렸다. 긍정 매칭만 하므로 배제 조항은
# 신호에서 빼야 한다 — 아니면 "쓰지 말라"는 문장이 그 skill을 불러온다.
_NEGATION_MARKERS = ("do not use", "do not trigger", "do not apply", "don't use")
_TERM_CACHE: dict[str, "re.Pattern[str]"] = {}


def _entry_terms(entry: SkillCatalogEntry, terms: Sequence[str]) -> tuple[str, ...]:
    signal = " ".join(
        (
            entry.name.replace("-", " "),
            entry.name,
            _positive_description(entry.description),
            " ".join(entry.keywords),
        )
    ).lower()
    return tuple(term for term in terms if term and _term_in(term, signal))


def _term_in(term: str, text: str) -> bool:
    r"""단어 경계로 본다.

    부분문자열로 보면 `chart`가 `charting`에 걸려 무관한 플랫폼 skill이 required까지
    올라간다(실측). 복수형은 허용하므로 `modifier`는 `modifiers`에 계속 걸린다 —
    그 오탐은 어휘를 구절로 좁혀서 막는다(`compose modifier`, `modifier chain`).

    경계는 ASCII 영숫자로만 잡는다. `\w`를 쓰면 한글이 word 문자라서
    `status bar가`처럼 조사가 붙은 한국어 task 문구가 전부 미매치가 된다.

    복수 접미사는 허용한다. skill description은 term을 복수로 쓰는 쪽이 흔해서
    (`turbo modules`, `slide decks`, `mcp servers`) 금지하면 정탐 13건이 사라졌다.
    `charting`처럼 복수형이 아닌 확장은 계속 배제된다.
    """
    pattern = _TERM_CACHE.get(term)
    if pattern is None:
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?:e?s)?(?![A-Za-z0-9_])")
        _TERM_CACHE[term] = pattern
    return pattern.search(text) is not None


def _positive_description(description: str) -> str:
    lowered = description.lower()
    cut = len(description)
    for marker in _NEGATION_MARKERS:
        index = lowered.find(marker)
        if index != -1:
            cut = min(cut, index)
    return description[:cut]


def _domains(value: object) -> list[ExternalDomain]:
    if not isinstance(value, list):
        return []
    domains: list[ExternalDomain] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        domain_id = str(item.get("id", "")).strip()
        if not domain_id:
            continue
        domains.append(
            ExternalDomain(
                id=domain_id,
                terms=tuple(term.lower() for term in _string_list(item.get("terms"))),
                path_globs=tuple(_string_list(item.get("path_globs"))),
                phases=tuple(_string_list(item.get("phases"))) or ("implementation", "review"),
            )
        )
    return domains


def _enabled(declared: bool, env: dict[str, str] | None) -> bool:
    environ = os.environ if env is None else env
    override = (environ.get(_ENV_KEY) or "").strip().lower()
    if override in {"on", "1", "true"}:
        return True
    if override in {"off", "0", "false"}:
        return False
    return declared


def _positive_int(value: object, default: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []
