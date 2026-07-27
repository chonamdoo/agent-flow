"""Profile → phase → 변경 범위 기반 skill routing.

우리가 쓰지 않는 upstream skill(google/android, chrisbanes)은 frontmatter에
`workflowPhases` 같은 agent-flow 선언이 없다. 그 파일을 우리가 고칠 수 없으니
활성화 선언을 **profile 쪽에** 둔다. 이 모듈은 그 선언을 읽어 skill 이름을
내놓고, 실제 해석·프롬프트·read gate는 `skill_resolver`가 하던 그대로 한다.

## Routing contract

입력은 셋이다.

1. **profile** — `active_profile_ids()`가 고른 profile들의 합본. 비활성 profile의
   표는 입력에 아예 없다. Python 프로젝트에서 Android skill이 나오지 않는 이유가
   이것이고, 다른 층의 조건문이 아니다.
2. **phase_id** — 표의 섹션이 phase 집합을 정한다. `implementation:`은 코드를
   쓰는 phase, `review:`는 판정하는 phase다. 한 엔트리가 두 섹션에 다 있으면
   두 집합 모두에서 활성화된다.
3. **changed_files / task_text** — 엔트리의 `task_terms` / `path_globs`.
   `skill_resolver`의 selector matcher를 그대로 쓴다. 규칙이 둘로 갈라지면
   frontmatter로 붙은 skill과 profile로 붙은 skill이 다른 기준으로 활성화된다.

출력은 `RoutedSkill` 목록이고 `resolve_phase_skills()`가 자기 required 집합에
합친다. 그래서 프롬프트 노출, 존재 여부 판정, read-evidence 강제,
`project-local-skills-used` 마커가 전부 기존 경로를 그대로 탄다.

## 선언 형식

`profiles/<id>.yaml`의 `skills.required_review[]`가 그룹을 선언한다.

```yaml
- group: android_skills
  skills_from: android_skills.review   # <표>.<섹션>
  path_globs: ["**/*.kt", "**/*.kts"]  # 그룹 전체 게이트(선택)
  missing: "missing local android_skills: <skill>"
- group: android-native-escalation
  profiles: [android]                  # 다른 profile의 표를 끌어온다
  skills_from: android_skills.review
  path_globs: ["android/**"]
```

표의 엔트리는 사람이 읽는 `when:` 산문과 기계가 읽는 선택자를 함께 갖는다.
산문은 근거이고 판정에 쓰이지 않는다 — 판정은 선택자만 본다.

```yaml
implementation:
  - skill: edge-to-edge
    when: system bars, insets, or cutout behavior changes
    task_terms: [edge-to-edge, insets, status bar]
    path_globs: ["**/*.kt"]
```

## 선택자 없는 엔트리는 활성화되지 않는다

`skill_resolver`의 `selector_declared` 규칙과 같다. 선택자를 안 적은 엔트리를
"항상 활성화"로 읽으면 Android 파일을 한 줄도 안 건드린 run에서 표 전체가
required가 된다. 표는 크고 host 설치는 사용자 몫이라, 그 순간 모든 run이
`skill-availability: degraded`로 물든다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from agent_flow.core.profiles import load_profile_payload

# 표의 섹션이 곧 phase 집합이다. 합집합은 `skill_resolver.CODE_PHASES`와 같아야
# 한다 — read gate가 걸리지 않는 phase에 skill을 밀어 넣으면 프롬프트만 길어진다.
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
_SECTION_PHASES = {
    "implementation": IMPLEMENTATION_PHASES,
    "review": REVIEW_PHASES,
}

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
) -> tuple[RoutedSkill, ...]:
    """활성 profile 선언에서 이번 phase/변경 범위에 걸리는 skill을 고른다."""
    if not isinstance(profile, dict):
        return ()
    routed: dict[str, RoutedSkill] = {}
    for group in _required_review_groups(profile):
        if not _selectors_match(group, changed_files=changed_files, task_text=task_text):
            continue
        for skill in _group_skills(
            group,
            profile,
            phase_id=phase_id,
            changed_files=changed_files,
            task_text=task_text,
        ):
            routed.setdefault(skill.name, skill)
    return tuple(routed.values())


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
    profile: dict,
    *,
    phase_id: str,
    changed_files: Sequence[str],
    task_text: str,
) -> list[RoutedSkill]:
    group_id = str(group.get("group", "")).strip() or "profile"
    missing_report = str(group.get("missing", "")).strip()
    literal = _string_list(group.get("skills"))
    if literal:
        # 표를 안 가리키는 그룹은 엔트리 선택자가 없다. 그룹 자신이 범위를
        # 선언하지 않으면 활성화 근거가 없어 코드 phase 전체에 얹힌다 — 기존
        # profile들은 선언만 해 둔 상태라 여기서 조용히 새 게이트가 생긴다.
        if not _has_selectors(group):
            return []
        if phase_id not in IMPLEMENTATION_PHASES and phase_id not in REVIEW_PHASES:
            return []
        return [RoutedSkill(name, group_id, "", missing_report) for name in literal]
    reference = str(group.get("skills_from", "")).strip()
    if not reference:
        return []
    owner = _table_owner(group, profile)
    if owner is None:
        return []
    return _table_skills(
        owner,
        reference,
        group_id=group_id,
        missing_report=missing_report,
        phase_id=phase_id,
        changed_files=changed_files,
        task_text=task_text,
    )


def _has_selectors(declaration: dict) -> bool:
    return bool(
        _string_list(declaration.get("task_terms"))
        or _string_list(declaration.get("path_globs"))
    )


def _table_owner(group: dict, profile: dict) -> dict | None:
    """표를 소유한 payload. `profiles:`가 있으면 그 profile을 끌어온다.

    React Native의 `android/` native 변경이 Android 표를 쓰는 경로다. 이때
    끌어오는 것은 **표뿐**이고 그 profile의 gate나 install 목록은 따라오지 않는다.
    """
    escalated = _string_list(group.get("profiles"))
    if not escalated:
        return profile
    for profile_id in escalated:
        try:
            payload = load_profile_payload(profile_id)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _table_skills(
    payload: dict,
    reference: str,
    *,
    group_id: str,
    missing_report: str,
    phase_id: str,
    changed_files: Sequence[str],
    task_text: str,
) -> list[RoutedSkill]:
    table_name, separator, section = reference.partition(".")
    if not table_name:
        return []
    if separator != ".":
        # 섹션을 안 적으면 phase가 고른다. `implementation`과 `review`를 그룹 두
        # 개로 쪼개면 같은 표를 두 번 선언하게 되고 둘이 어긋난다.
        resolved = _phase_section(phase_id)
        if resolved is None:
            return []
        section = resolved
    elif not section:
        return []
    if phase_id not in _SECTION_PHASES.get(section, frozenset()):
        return []
    table = payload.get(table_name)
    if not isinstance(table, dict):
        return []
    entries = table.get(section)
    if not isinstance(entries, list):
        return []
    source = str(table.get("source", "")).strip()
    routed: list[RoutedSkill] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("skill", "")).strip()
        if not name:
            continue
        if not _selectors_match(entry, changed_files=changed_files, task_text=task_text):
            continue
        routed.append(RoutedSkill(name, group_id, source, missing_report))
    return routed


def _phase_section(phase_id: str) -> str | None:
    for section, phases in _SECTION_PHASES.items():
        if phase_id in phases:
            return section
    return None


def _selectors_match(
    declaration: dict, *, changed_files: Sequence[str], task_text: str
) -> bool:
    # 지연 import: skill_resolver가 이 모듈을 부르므로 상단 import는 순환이 된다.
    from agent_flow.core.skill_resolver import selector_matches

    task_terms = _string_list(declaration.get("task_terms"))
    path_globs = _string_list(declaration.get("path_globs"))
    if not task_terms and not path_globs:
        # 그룹 게이트는 선택자를 안 적으면 "제한 없음"이다. 엔트리 쪽에서 다시
        # 좁히므로 여기서 막으면 그룹 전체가 죽는다. 반대로 엔트리가 선택자를
        # 안 적었으면 활성화 근거가 없다.
        return "skill" not in declaration
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
