"""설치된 외부 skill을 이름 열거 없이 라우팅한다.

이름을 우리 YAML에 적으면 우리 파일은 upstream보다 항상 낡는다 — `android/skills`는
디렉터리를 지우고 zip을 통째로 재추출하므로 rename 이력이 upstream 자신에게도 남지 않고,
실측으로 6개월에 이름 35%가 바뀌었다. 그래서 소비자는 **어휘와 범위**만 선언하고,
판정은 upstream이 스스로 쓴 `name` + `description`(+ 있으면 `metadata.keywords`)에 대해
런의 task 문구와 변경 경로를 조인해서 한다.

두 축이 모두 필요하다. 어휘가 skill 쪽에만 걸리면 그 skill은 이 저장소와 무관한 것이고,
런 쪽에만 걸리면 그 일을 도와줄 skill이 안 깔린 것이다.

**tier는 그 조인이 정하지 않는다.** 설치된 skill은 `--concern <domain id>`가 그 domain을
지목하거나 `pins`에 이름이 있을 때만 required가 되고, 그 밖에는 offered다. task 문구로
required를 만들면 같은 변경이 표기와 머신 구성에 따라 다른 게이트를 받는다(실측: 영어
문구 required 6개 / 26,241 B, 같은 뜻의 한국어 4개). 그래서 이 모듈만으로는 required가
나오지 않는다 — 그것이 의도다. 어느 concern이 선언돼 있는지는
`local_skills.declared_concern_ids`가 답하고, 오타는 run 시작 시 거부된다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from agent_flow.core.profile_routing import IMPLEMENTATION_PHASES, REVIEW_PHASES
from agent_flow.core.skill_resolver import SkillCatalogEntry, selector_matches, term_in

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
    # 개수는 열람 비용을 대표하지 못한다. 실측으로 host skill 하나가 71,590 B였고
    # 상위 6개 합이 214,501 B였다 — 개수 캡만으로는 required 6장이 그대로 통과한다.
    # profile이 `0`으로 끌 수 있다.
    required_budget_bytes: int = 24_000
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

    def demoted(self) -> "ExternalMatch":
        return replace(self, tier=OFFERED)


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
        required_budget_bytes=_external_budget(block.get("required_budget_bytes")),
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
    concerns: Sequence[str] = (),
    env: dict[str, str] | None = None,
) -> tuple[ExternalMatch, ...]:
    """이번 phase/범위에서 활성화되는 설치 skill. 순서까지 결정론이다.

    tier는 **선언된 concern**과 `pins`만 required로 만든다. 예전에는 task 문구와
    skill 텍스트에 같은 term이 걸리면 required였는데, 그 경로는 두 가지를 동시에
    깬다. 첫째, 같은 뜻을 한국어로 적으면 term이 안 걸려 required가 사라진다
    (실측: 영어 task 6개 / 26,241 B, 한국어 4개). 둘째, 후보 집합이 이 머신에 깔린
    카탈로그라서 같은 변경이 host마다 다른 required를 만든다.

    그래서 free-form task 문구는 offered까지만 만들고, 무엇을 반드시 읽어야 하는지는
    호출자가 `--concern <domain-id>`로 열거해서 정한다.
    """
    config = parse_external(profile, env=env)
    if not config.enabled:
        return ()
    haystack = task_text.lower()
    declared_concerns = {str(item).strip().lower() for item in concerns if str(item).strip()}
    matches: dict[str, ExternalMatch] = {}
    for domain in config.domains:
        if not _domain_active(
            domain, phase_id, changed_files, task_text, declared_concerns
        ):
            continue
        for entry in catalog:
            if entry.source not in _EXTERNAL_SOURCES:
                continue
            terms = _entry_terms(entry, domain.terms)
            if not terms:
                continue
            # term 기록은 남긴다 — 어느 어휘로 걸렸는지가 프롬프트와 진단의 근거다.
            shared = next((term for term in terms if _term_in(term, haystack)), None)
            required = domain.id.lower() in declared_concerns or entry.name in config.pins
            tier = REQUIRED if required else OFFERED
            existing = matches.get(entry.name)
            if existing is not None and (existing.tier == REQUIRED or tier == OFFERED):
                continue
            matches[entry.name] = ExternalMatch(
                entry.name, domain.id, tier, shared or terms[0], entry.path, entry.source
            )
    return _truncate(matches.values(), config)


def declared_domain_ids(profile: dict | None, *, env: dict[str, str] | None = None) -> set[str]:
    """이 profile이 선언한 external domain id. `--concern`의 유효 값 목록이다."""
    config = parse_external(profile, env=env)
    return {domain.id.lower() for domain in config.domains if domain.id}


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
    required, demoted = _within_budget(required, config.required_budget_bytes)
    offered_candidates = [item for item in ordered if item.tier == OFFERED]
    offered = _round_robin(
        list(demoted) + offered_candidates,
        config.offered_max,
    )
    return tuple(required + offered)



def _within_budget(
    matches: Sequence[ExternalMatch], budget: int
) -> tuple[list[ExternalMatch], list[ExternalMatch]]:
    """required 합계를 바이트로 자르고 초과분은 offered로 넘긴다.

    경계를 넘긴 문서는 넣고 멈춘다. 건너뛰면 순위가 뒤집혀 가장 구체적인(= 대체로
    가장 큰) skill이 작고 덜 관련된 것에 밀린다. 그래서 상한은 `budget`이 아니라
    `budget + 마지막으로 들어간 문서`다. profile이 이름으로 선언한 skill과 dependency
    확장분은 여기 오지 않는다 — 예산은 어휘 휴리스틱 tier에만 건다.
    """
    if budget <= 0:
        return list(matches), []
    kept: list[ExternalMatch] = []
    demoted: list[ExternalMatch] = []
    used = 0
    for index, match in enumerate(matches):
        if used >= budget:
            demoted.extend(item.demoted() for item in matches[index:])
            break
        kept.append(match)
        used += _match_bytes(match)
    return kept, demoted


def _match_bytes(match: ExternalMatch) -> int:
    if match.path is None:
        return 0
    try:
        return match.path.stat().st_size
    except OSError:
        return 0


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
    concerns: set[str] = frozenset(),  # type: ignore[assignment]
) -> bool:
    if not domain.terms:
        # 어휘 없는 domain은 활성화 근거가 없다. profile 표의 선택자 없는 엔트리와 같은 규칙이다.
        return False
    if not any(phase_id in _SECTION_PHASES.get(section, frozenset()) for section in domain.phases):
        return False
    if domain.id.lower() in concerns:
        # 선언된 concern은 **단독 활성화 근거**다. tier만 올리고 활성화를 문구에 맡기면
        # `path_globs` 없는 domain(스타일링·보안처럼 의도가 경로에 없는 것들)은 같은 뜻을
        # 한국어로 적었을 때 concern이 조용한 no-op이 된다 — 실측으로 영어 문구에서만
        # `frontend-design`이 붙었다. `profile_routing._selectors_match`와 같은 규칙이다.
        return True
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
    """단어 경계 판정은 `skill_resolver.term_in` 하나다. frontmatter 선언과 어휘
    라우팅이 다른 규칙으로 갈리면 같은 문구가 경로에 따라 다르게 붙는다."""
    return term_in(term, text)


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


def _external_budget(value: object) -> int:
    """`0`은 명시적 해제다. `_positive_int`는 0을 버리고 기본값으로 되돌린다."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 24_000
    return max(0, value)


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
