"""Profile → phase → 변경 범위 기반 skill routing.

이 모듈은 **우리가 이름을 소유한 skill**만 다룬다 — repo가 배포하는 bundled skill이다.
설치된 외부 skill은 이름을 우리가 정하지 않으므로 `skill_matching`이 어휘로 라우팅한다.

## Routing contract

입력은 셋이다.

1. **profile** — `active_profile_ids()`가 고른 profile들의 합본. 비활성 profile의
   선언은 입력에 아예 없다. Python 프로젝트에서 Android skill이 나오지 않는 이유가
   이것이고, 다른 층의 조건문이 아니다.
2. **phase_id** — 코드를 쓰는 phase(`IMPLEMENTATION_PHASES`)와 판정하는
   phase(`REVIEW_PHASES`)를 가른다.
3. **changed_files / task_text** — 그룹의 `task_terms` / `path_globs`.
   `skill_resolver`의 selector matcher를 그대로 쓴다. 규칙이 둘로 갈라지면
   frontmatter로 붙은 skill과 profile로 붙은 skill이 다른 기준으로 활성화된다.

출력은 `RoutedSkill` 목록이고 `resolve_phase_skills()`가 자기 required 집합에
합친다. 그래서 프롬프트 노출, 존재 여부 판정, read-evidence 강제,
`project-local-skills-used` 마커가 전부 기존 경로를 그대로 탄다.

## 선언 형식

```yaml
- group: profile
  skills: [android-code-review, android-clean-architecture]
  path_globs: ["**/*.kt", "**/*.kts"]
  missing: "missing local profile: <skill>"
```

## 선택자 없는 그룹은 활성화되지 않는다

`skill_resolver`의 `selector_declared` 규칙과 같다. 선택자를 안 적은 선언을
"항상 활성화"로 읽으면 그 파일을 한 줄도 안 건드린 run에서도 required가 된다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

# 합집합은 `skill_resolver.CODE_PHASES`와 같아야 한다 — read gate가 걸리지 않는 phase에
# skill을 밀어 넣으면 프롬프트만 길어진다.
IMPLEMENTATION_PHASES = frozenset(
    {
        "implement",
        "implement-fix",
        "red",
        "green",
        "refactor",
        "fix-loop",
        "pr-comment-fix",
        "pr-ci-fix",
    }
)
REVIEW_PHASES = frozenset({"final-review", "review", "multi-review", "architecture-review"})


_MISSING_PLACEHOLDER = "<skill>"


@dataclass(frozen=True)
class RoutedSkill:
    """profile 선언으로 활성화된 skill 하나."""

    name: str
    group: str
    source: str = ""
    missing_report: str = ""


def routed_profile_skills(
    profile: dict | None,
    *,
    phase_id: str,
    changed_files: Sequence[str] = (),
    task_text: str = "",
    concerns: Sequence[str] = (),
) -> tuple[RoutedSkill, ...]:
    """활성 profile 선언에서 이번 phase/변경 범위에 걸리는 skill을 고른다."""
    if not isinstance(profile, dict):
        return ()
    routed: dict[str, RoutedSkill] = {}
    for group in _required_review_groups(profile):
        if not _selectors_match(
            group, changed_files=changed_files, task_text=task_text, concerns=concerns
        ):
            continue
        for skill in _group_skills(group, phase_id=phase_id):
            routed.setdefault(skill.name, skill)
    return tuple(routed.values())


def routable_group_skills(profile: dict | None) -> frozenset[str]:
    """selectors를 선언한 `required_review` group의 skill 이름.

    doctor는 "어떤 변경에서든 켜질 수 있는가"를 묻는다. phase도 변경 파일도 그 질문의
    입력이 아니다. selectors 없는 group은 `_group_skills`가 빈 목록을 주므로 이름이
    선언돼 있어도 도달 불가다 — 그 판정을 doctor가 복제하지 않게 여기서 내보낸다.
    """
    if not isinstance(profile, dict):
        return frozenset()
    return frozenset(
        name
        for group in _required_review_groups(profile)
        if _has_selectors(group)
        for name in _string_list(group.get("skills"))
    )


def declared_group_concerns(profile: dict | None) -> set[str]:
    """`required_review` group이 선언한 concern id. `--concern`의 유효 값 목록이다."""
    if not isinstance(profile, dict):
        return set()
    return {
        value.lower()
        for group in _required_review_groups(profile)
        for value in _string_list(group.get("concerns"))
    }


def _required_review_groups(profile: dict) -> list[dict]:
    skills = profile.get("skills")
    if not isinstance(skills, dict):
        return []
    declared = skills.get("required_review")
    if not isinstance(declared, list):
        return []
    return [item for item in declared if isinstance(item, dict)]


def _group_skills(
    group: dict,
    *,
    phase_id: str,
) -> list[RoutedSkill]:
    group_id = str(group.get("group", "")).strip() or "profile"
    literal = _string_list(group.get("skills"))
    if not literal:
        return []
    # 그룹 자신이 범위를 선언하지 않으면 활성화 근거가 없어 코드 phase 전체에 얹힌다.
    if not _has_selectors(group):
        return []
    if phase_id not in IMPLEMENTATION_PHASES and phase_id not in REVIEW_PHASES:
        return []
    missing_report = str(group.get("missing", "")).strip()
    return [RoutedSkill(name, group_id, "", missing_report) for name in literal]


def _has_selectors(declaration: dict) -> bool:
    return bool(
        _string_list(declaration.get("task_terms"))
        or _string_list(declaration.get("path_globs"))
        or _string_list(declaration.get("concerns"))
    )


def _selectors_match(
    declaration: dict,
    *,
    changed_files: Sequence[str],
    task_text: str,
    concerns: Sequence[str] = (),
) -> bool:
    # 지연 import: skill_resolver가 이 모듈을 부르므로 상단 import는 순환이 된다.
    from agent_flow.core.skill_resolver import selector_matches

    task_terms = _string_list(declaration.get("task_terms"))
    path_globs = _string_list(declaration.get("path_globs"))
    declared_concerns = {value.lower() for value in _string_list(declaration.get("concerns"))}
    if declared_concerns and declared_concerns & {
        str(item).strip().lower() for item in concerns if str(item).strip()
    }:
        return True
    if not task_terms and not path_globs and not declared_concerns:
        # 그룹 게이트는 선택자를 안 적으면 "제한 없음"이다. 엔트리 쪽에서 다시
        # 좁히므로 여기서 막으면 그룹 전체가 죽는다. 반대로 엔트리가 선택자를
        # 안 적었으면 활성화 근거가 없다.
        return "skill" not in declaration
    if not task_terms and not path_globs:
        # concern만 선언한 그룹은 그 concern이 명시될 때만 켜진다.
        return False
    return selector_matches(
        task_terms=task_terms,
        path_globs=path_globs,
        changed_files=changed_files,
        task_text=task_text,
    )


def missing_routed_report(skill: RoutedSkill) -> str:
    """표가 선언한 문구로 부재를 보고한다. 없으면 그룹 이름으로 만든다."""
    template = skill.missing_report or f"missing local {skill.group}: {_MISSING_PLACEHOLDER}"
    return template.replace(_MISSING_PLACEHOLDER, skill.name)


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []
