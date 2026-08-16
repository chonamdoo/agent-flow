"""어휘 조인 계약.

이름을 열거하지 않으므로 이 판정이 유일한 방어선이다. 반증 대상은 두 가지다 —
관련 없는 run에서 설치된 skill이 딸려 나오는가, 그리고 관련 있는 run에서 조용히 빠지는가.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.skill_matching import (
    OFFERED,
    REQUIRED,
    match_external,
    parse_external,
    routable_names,
)
from agent_flow.core.local_skills import local_skill_prompt_block
from agent_flow.core.skill_resolver import (
    SkillCatalogEntry,
    SkillRoot,
    discover_skill_catalog,
    resolve_phase_skills,
)


def _entry(name: str, description: str = "", source: str = "host") -> SkillCatalogEntry:
    return SkillCatalogEntry(
        name=name,
        path=Path(f"/tmp/{name}/SKILL.md"),
        source=source,
        description=description,
    )


def _profile(domains: list[dict], **extra) -> dict:
    return {"skills": {"external": {"enabled": True, "domains": domains, **extra}}}


_INSETS = {
    "id": "ui-insets",
    "terms": ["edge-to-edge", "insets", "status bar"],
    "phases": ["implementation", "review"],
}


def test_external_routing_is_off_until_the_profile_enables_it(tmp_path):
    """단계 전환 안전장치. 어휘가 완벽해도 꺼져 있으면 아무것도 붙지 않는다."""
    profile = {"skills": {"external": {"enabled": False, "domains": [_INSETS]}}}

    matches = match_external(
        profile,
        (_entry("edge-to-edge", "Use when insets change."),),
        phase_id="implement",
        task_text="fix insets",
        env={},
    )

    assert matches == ()


def test_env_override_can_force_routing_on_and_off():
    profile = {"skills": {"external": {"enabled": False, "domains": [_INSETS]}}}
    catalog = (_entry("edge-to-edge", "Use when insets change."),)

    on = match_external(
        profile,
        catalog,
        phase_id="implement",
        task_text="fix insets",
        env={"AGENT_FLOW_EXTERNAL_SKILLS": "on"},
    )
    off = match_external(
        _profile([_INSETS]),
        catalog,
        phase_id="implement",
        task_text="fix insets",
        env={"AGENT_FLOW_EXTERNAL_SKILLS": "off"},
    )

    assert [item.name for item in on] == ["edge-to-edge"]
    assert off == ()


def test_a_domain_without_terms_never_activates():
    """선택자 없는 선언을 '무조건 활성화'로 읽으면 설치된 skill 전량이 딸려 나온다."""
    profile = _profile([{"id": "empty", "phases": ["implementation"]}])

    matches = match_external(
        profile,
        (_entry("edge-to-edge", "Use when insets change."),),
        phase_id="implement",
        task_text="fix insets",
        env={},
    )

    assert matches == ()


def test_task_wording_alone_offers_but_never_requires():
    """반증: 같은 뜻을 다른 언어로 적으면 required가 사라진다.

    실측으로 영어 task는 required 6개 / 26,241 B였고 한국어는 4개였다. 후보 집합도
    이 머신에 깔린 카탈로그라 host마다 갈린다. 그래서 free-form 문구는 offered까지만
    만들고, 무엇을 반드시 읽어야 하는지는 `--concern`이 정한다.
    """
    args = dict(
        phase_id="implement",
        env={},
    )
    catalog = (_entry("edge-to-edge", "Use when the status bar overlaps content."),)

    korean = match_external(
        _profile([_INSETS]), catalog, task_text="status bar가 콘텐츠를 가린다", **args
    )
    english = match_external(
        _profile([_INSETS]), catalog, task_text="the status bar overlaps content", **args
    )

    assert [(item.name, item.tier) for item in korean] == [("edge-to-edge", OFFERED)]
    assert [(item.name, item.tier) for item in english] == [("edge-to-edge", OFFERED)]


def test_a_declared_concern_promotes_its_domain_to_required():
    """concern은 열거형이라 표기와 무관하게 같은 결과를 낸다."""
    matches = match_external(
        _profile([_INSETS]),
        (_entry("edge-to-edge", "Use when the status bar overlaps content."),),
        phase_id="implement",
        task_text="status bar가 콘텐츠를 가린다",
        concerns=["ui-insets"],
        env={},
    )

    assert [(item.name, item.tier) for item in matches] == [("edge-to-edge", REQUIRED)]


def test_term_matching_only_the_skill_is_offered_not_required():
    """run이 그 일을 하고 있다는 증거가 없다. 프롬프트에는 올리되 게이트로 요구하지 않는다."""
    domain = {
        "id": "ui-insets",
        "terms": ["edge-to-edge", "insets"],
        "path_globs": ["**/*.kt"],
        "phases": ["implementation"],
    }

    matches = match_external(
        _profile([domain]),
        (_entry("edge-to-edge", "Use when insets change."),),
        phase_id="implement",
        changed_files=["app/A.kt"],
        task_text="rename a variable",
        env={},
    )

    assert [(item.name, item.tier) for item in matches] == [("edge-to-edge", OFFERED)]


def test_the_recorded_term_is_the_one_both_sides_share():
    """tier는 concern이 정하지만, 어느 어휘로 걸렸는지는 프롬프트와 진단의 근거다."""
    matches = match_external(
        _profile([_INSETS]),
        (_entry("edge-to-edge", "Use when insets or system bars change."),),
        phase_id="implement",
        task_text="insets 정리",
        concerns=["ui-insets"],
        env={},
    )

    assert matches[0].tier == REQUIRED
    assert matches[0].term == "insets"


def test_implementation_only_domain_is_absent_in_review_phases():
    """phase축은 우리 workflow의 개념이라 upstream이 바뀌어도 낡지 않는다."""
    domain = {"id": "platform-sdk", "terms": ["camerax"], "phases": ["implementation"]}
    catalog = (_entry("camerax", "Camera guidance."),)

    implement = match_external(
        _profile([domain]), catalog, phase_id="implement", task_text="camerax 도입", env={}
    )
    review = match_external(
        _profile([domain]), catalog, phase_id="final-review", task_text="camerax 리뷰", env={}
    )

    assert [item.name for item in implement] == ["camerax"]
    assert review == ()


def test_changed_files_alone_can_activate_a_domain():
    domain = {
        "id": "release-shrinker",
        "terms": ["r8", "proguard"],
        "path_globs": ["**/proguard-rules.pro"],
        "phases": ["implementation"],
    }

    matches = match_external(
        _profile([domain]),
        (_entry("r8-analyzer", "Use for R8 keep rules."),),
        phase_id="implement",
        changed_files=["app/proguard-rules.pro"],
        task_text="release build",
        env={},
    )

    assert [item.name for item in matches] == ["r8-analyzer"]


def test_required_tier_is_truncated_deterministically():
    """절단은 있어도 되지만 재현돼야 한다. 순서가 흔들리면 같은 입력이 다른 게이트를 만든다."""
    domain = {"id": "wide", "terms": ["skill"], "phases": ["implementation"]}
    catalog = tuple(_entry(f"skill-{index}", "Use when skill.") for index in range(5))
    profile = _profile([domain], required_max=2)

    first = match_external(
        profile, catalog, phase_id="implement", task_text="skill", concerns=["wide"], env={}
    )
    second = match_external(
        profile,
        tuple(reversed(catalog)),
        phase_id="implement",
        task_text="skill",
        concerns=["wide"],
        env={},
    )

    assert [item.name for item in first if item.tier == REQUIRED] == ["skill-0", "skill-1"]
    assert [item.name for item in first] == [item.name for item in second]


def test_pin_forces_a_matched_skill_into_the_required_tier():
    """upstream 메타데이터 공백을 메우는 유일한 손 데이터. 후보 집합은 여전히 디스크가 만든다."""
    domain = {
        "id": "testing",
        "terms": ["testing setup", "robolectric"],
        "phases": ["implementation"],
    }
    profile = _profile([domain], pins=["testing-setup"])

    matches = match_external(
        profile,
        (_entry("testing-setup", "Create a testing strategy."),),
        phase_id="implement",
        task_text="robolectric 추가",
        env={},
    )

    assert [(item.name, item.tier) for item in matches] == [("testing-setup", REQUIRED)]


def test_a_pin_for_an_uninstalled_skill_stays_inert():
    """열거가 아니라 보정이다. 사라진 이름이 required를 만들지 못한다."""
    domain = {"id": "testing", "terms": ["robolectric"], "phases": ["implementation"]}
    profile = _profile([domain], pins=["camera1-to-camerax"])

    matches = match_external(
        profile, (), phase_id="implement", task_text="robolectric 추가", env={}
    )

    assert matches == ()


def test_project_owned_skills_are_not_routed_by_vocabulary():
    """우리가 배포하는 skill은 이름을 우리가 소유한다. 그쪽은 기존 선언이 담당한다."""
    matches = match_external(
        _profile([_INSETS]),
        (_entry("android-code-review", "Use when insets change.", source="bundled"),),
        phase_id="implement",
        task_text="insets 수정",
        env={},
    )

    assert matches == ()


def test_defaults_are_conservative():
    config = parse_external({}, env={})

    assert config.enabled is False
    assert config.required_max == 6
    assert config.offered_max == 20


def test_resolution_puts_required_in_required_and_offered_in_optional(tmp_path, monkeypatch):
    """resolver까지 흐르는지 확인한다. 매칭만 맞고 배선이 없으면 프롬프트에 안 나온다."""
    home = tmp_path / "home"
    skills = home / ".claude" / "skills"
    for name, description in (
        ("edge-to-edge", "Use when insets change."),
        ("r8-analyzer", "Use for R8 keep rules and app size."),
    ):
        path = skills / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "app"
    project.mkdir()
    profile = _profile(
        [
            _INSETS,
            {
                "id": "release-shrinker",
                "terms": ["r8", "app size"],
                "path_globs": ["**/*.pro"],
                "phases": ["implementation"],
            },
        ]
    )

    resolution = resolve_phase_skills(
        project_root=project,
        phase_id="implement",
        profile=profile,
        changed_files=["app/proguard-rules.pro"],
        task_text="insets 정리",
        concerns=["ui-insets"],
        host="claude",
    )

    assert "edge-to-edge" in {skill.name for skill in resolution.required}
    assert "r8-analyzer" in {skill.name for skill in resolution.optional}
    # offered tier의 존재 이유는 프롬프트 노출이다. dataclass만 보면 렌더링이 사라져도 통과한다.
    block = local_skill_prompt_block(
        project,
        "implement",
        profile=profile,
        changed_files=["app/proguard-rules.pro"],
        task_text="insets 정리",
    )
    assert "r8-analyzer" in block


def test_an_exclusion_clause_does_not_pull_the_skill_in():
    """실측: `docx`가 "Do NOT use for PDFs, spreadsheets…"를 실어 `spreadsheet` 어휘에 걸렸다."""
    domain = {"id": "documents", "terms": ["spreadsheet"], "phases": ["implementation"]}
    entry = _entry(
        "docx",
        "Use this skill for Word documents. Do NOT use for PDFs, spreadsheets, or slides.",
    )

    matches = match_external(
        _profile([domain]),
        (entry,),
        phase_id="implement",
        task_text="spreadsheet 정리",
        env={},
    )

    assert matches == ()


def test_truncation_gives_every_domain_a_slot():
    """실측: recomposition 어휘에 걸린 8개가 `edge-to-edge`를 required에서 밀어냈다."""
    domains = [
        {"id": "a-perf", "terms": ["recomposition"], "phases": ["implementation"]},
        {"id": "z-insets", "terms": ["insets"], "phases": ["implementation"]},
    ]
    catalog = tuple(
        _entry(f"perf-{index}", "Use when recomposition is slow.") for index in range(8)
    ) + (_entry("edge-to-edge", "Use when insets overlap content."),)

    matches = match_external(
        _profile(domains, required_max=3),
        catalog,
        phase_id="implement",
        task_text="recomposition jank과 insets 정리",
        concerns=["a-perf", "z-insets"],
        env={},
    )

    assert "edge-to-edge" in {item.name for item in matches if item.tier == REQUIRED}


def test_a_term_does_not_match_a_longer_word():
    """실측: `chart`가 xlsx의 "charting"에 걸려 무관한 skill이 required까지 올라갔다."""
    domain = {"id": "docs", "terms": ["chart"], "phases": ["implementation"]}

    matches = match_external(
        _profile([domain]),
        (_entry("xlsx", "Charting and cleaning tabular data."),),
        phase_id="implement",
        task_text="add a chart",
        env={},
    )

    assert matches == ()


def test_a_term_still_matches_its_plural():
    """skill description은 term을 복수로 쓰는 쪽이 흔하다. 금지하면 정탐 13건이 사라졌다."""
    domain = {"id": "rn", "terms": ["turbo module"], "phases": ["implementation"]}

    matches = match_external(
        _profile([domain]),
        (_entry("optimizing-react-native", "Covers turbo modules and Hermes."),),
        phase_id="implement",
        task_text="turbo module 추가",
        env={},
    )

    assert [item.name for item in matches] == ["optimizing-react-native"]


def test_routable_names_is_empty_while_routing_is_off():
    """꺼져 있는데 도달 가능으로 세면 doctor가 실제로 죽은 skill의 보고를 지운다."""
    profile = {"skills": {"external": {"enabled": False, "domains": [_INSETS]}}}

    assert routable_names(profile, (_entry("edge-to-edge", "Use when insets change."),), env={}) == set()


def test_a_matched_skill_keeps_its_catalog_source():
    entry = _entry("shepherd", "Use when insets change.", source="shared")

    matches = match_external(
        _profile([_INSETS]), (entry,), phase_id="implement", task_text="insets 정리", env={}
    )

    assert matches[0].source == "shared"


def test_routable_names_ignores_the_run_scope():
    """doctor는 "이 skill이 어떤 run에서든 걸릴 수 있는가"를 물어야 한다."""
    profile = _profile([_INSETS])
    catalog = (
        _entry("edge-to-edge", "Use when insets overlap content."),
        _entry("handoff", "Use when handing work over."),
    )

    assert routable_names(profile, catalog, env={}) == {"edge-to-edge"}


def test_a_matched_skill_carries_its_discovered_path():
    entry = _entry("edge-to-edge", "Use when insets overlap content.")

    matches = match_external(
        _profile([_INSETS]),
        (entry,),
        phase_id="implement",
        task_text="insets 정리",
        env={},
    )

    assert matches[0].path == entry.path


def test_a_vendor_skill_installed_under_the_project_routes(tmp_path, monkeypatch):
    """전역 설치를 거부하는 벤더는 프로젝트 root에만 놓는다. 그 root를 더해 놓고
    라우팅 대상에서 빼면 더한 이유가 사라진다."""
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / "app"
    path = project / ".claude" / "skills" / "prisma-vendor" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: prisma-vendor\ndescription: Use when working with prisma schema.\n---\n",
        encoding="utf-8",
    )
    profile = _profile(
        [{"id": "orm", "terms": ["prisma schema"], "phases": ["implementation"]}]
    )

    resolution = resolve_phase_skills(
        project_root=project,
        phase_id="implement",
        profile=profile,
        task_text="prisma schema 수정",
        concerns=["orm"],
        host="claude",
    )

    matched = next(
        skill for skill in resolution.required if skill.name == "prisma-vendor"
    )
    assert matched.exists
    assert matched.source == "vendor"


def _write_skill(directory: Path, name: str, body: str) -> Path:
    path = directory / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _host_root(directory: Path) -> SkillRoot:
    return SkillRoot(source="host", template=str(directory / "{skill}" / "SKILL.md"))


def _external_profile(budget: int | None = None) -> dict:
    external = {
        "enabled": True,
        "domains": [
            {"id": "d", "terms": ["compose"], "phases": ["implementation"]},
        ],
    }
    if budget is not None:
        external["required_budget_bytes"] = budget
    return {"skills": {"external": external}}


def _sized_host_skill(directory: Path, name: str, size: int) -> Path:
    body = (
        f"---\nname: {name}\ndescription: Compose guidance for {name}.\n---\n\n"
        + "x" * size
    )
    return _write_skill(directory, name, body)


def _external_matches(tmp_path: Path, sizes: dict[str, int], budget: int | None):
    from agent_flow.core.skill_matching import match_external

    host = tmp_path / "host"
    for name, size in sizes.items():
        _sized_host_skill(host, name, size)
    catalog = discover_skill_catalog(tmp_path, (_host_root(host),))
    # 예산은 required tier에만 걸린다. required는 이제 명시된 concern에서만 나오므로
    # 예산 계약을 반증하려면 그 concern을 함께 선언해야 한다.
    profile = _external_profile(budget)
    concerns = [domain["id"] for domain in profile["skills"]["external"]["domains"]]
    return match_external(
        profile,
        catalog,
        phase_id="implement",
        task_text="compose work",
        concerns=concerns,
    )


def test_external_required_respects_byte_budget(tmp_path: Path) -> None:
    """반증: 개수만 세면 71KB 한 장이 6장 몫의 열람을 요구하고도 캡을 통과한다."""
    sizes = {"compose-a": 20_000, "compose-b": 20_000, "compose-c": 20_000}
    matches = _external_matches(tmp_path, sizes, 24_000)
    required = [item.name for item in matches if item.tier == "required"]
    offered = [item.name for item in matches if item.tier == "offered"]
    assert len(required) < len(sizes), "예산을 넘겼는데 전부 required로 남았다"
    assert set(required) | set(offered) == set(sizes), "강등된 skill이 사라졌다"


def test_external_budget_admits_the_document_that_crosses(tmp_path: Path) -> None:
    """반증: 넘긴 문서를 건너뛰면 순위가 뒤집혀 큰 정확한 skill이 작은 것에 밀린다.

    예산 아래에서 시작한 문서는 경계를 넘겨도 들어가고, 거기서 멈춘다.
    """
    sizes = {"compose-a": 20_000, "compose-b": 20_000, "compose-c": 5_000}
    matches = _external_matches(tmp_path, sizes, 24_000)
    required = [item.name for item in matches if item.tier == "required"]
    offered = [item.name for item in matches if item.tier == "offered"]
    assert required == ["compose-a", "compose-b"]
    assert offered == ["compose-c"]


def test_external_budget_applies_without_profile_configuration(tmp_path: Path) -> None:
    """반증: 기본이 꺼짐이면 아무 profile도 켜지 않아 예산이 죽은 코드가 된다."""
    sizes = {"compose-a": 30_000, "compose-b": 30_000}
    matches = _external_matches(tmp_path, sizes, None)
    required = [item.name for item in matches if item.tier == "required"]
    assert required == ["compose-a"]


def test_external_budget_zero_disables_the_ceiling(tmp_path: Path) -> None:
    """profile이 명시적으로 끌 수 있어야 한다. `0`이 그 해제 값이다."""
    sizes = {"compose-a": 30_000, "compose-b": 30_000}
    matches = _external_matches(tmp_path, sizes, 0)
    required = [item.name for item in matches if item.tier == "required"]
    assert sorted(required) == ["compose-a", "compose-b"]


def test_external_budget_admits_an_oversize_first_document(tmp_path: Path) -> None:
    """설계값 `external_budget_admit_crossing_doc: true`. 예산보다 큰 문서라도 순위가 앞이면
    넣고 멈춘다 — 건너뛰면 가장 구체적인 skill이 작고 덜 관련된 것에 밀린다."""
    sizes = {"compose-a": 71_590, "compose-b": 5_000}
    matches = _external_matches(tmp_path, sizes, 24_000)
    required = [item.name for item in matches if item.tier == "required"]
    offered = [item.name for item in matches if item.tier == "offered"]
    assert required == ["compose-a"]
    assert offered == ["compose-b"]


def test_external_budget_stops_after_the_crossing_document(tmp_path: Path) -> None:
    """초과는 마지막에 들어간 문서 하나로 끝난다. 그 뒤 매치는 전부 offered다."""
    budget = 24_000
    sizes = {f"compose-{index}": 9_000 for index in range(6)}
    matches = _external_matches(tmp_path, sizes, budget)
    required = [item for item in matches if item.tier == "required"]
    total = sum(item.path.stat().st_size for item in required if item.path is not None)
    assert len(required) == 3
    assert total - 9_000 < budget
