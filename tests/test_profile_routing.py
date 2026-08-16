"""#105: profile 선언이 실제 resolver 입력인가.

선언이 되고 아무도 안 읽으면 "보여 주지 않는 것을 읽으라"가 된다. 그래서 테스트는
선언이 아니라 **routing 결과**를 반증한다: 무관한 profile에서 나오지 않는가, 변경 범위
밖에서 나오지 않는가, phase가 갈리는가.

이 모듈은 **우리가 이름을 소유한 bundled skill**의 라우팅을 지킨다. 설치된 외부 skill의
어휘 라우팅은 `test_skill_matching.py`가 지킨다 — 이름을 우리가 적지 않기 때문이다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.local_skills import (
    local_skill_prompt_block,
    merged_profile_payload,
    missing_local_skill_markers,
)
from agent_flow.core.profile_routing import (
    RoutedSkill,
    missing_routed_report,
    routable_group_skills,
    routed_profile_skills,
)
from agent_flow.core.profiles import load_profile_payload
from agent_flow.core.skill_matching import match_external, parse_external
from agent_flow.core.skill_resolver import (
    CODE_PHASES,
    IMPLEMENTATION_PHASES,
    REVIEW_PHASES,
    discover_skill_catalog,
    resolve_phase_skills,
    skill_roots,
)


def _names(routed: tuple[RoutedSkill, ...]) -> set[str]:
    return {skill.name for skill in routed}


def _route(profile_id: str, **kwargs) -> tuple[RoutedSkill, ...]:
    return routed_profile_skills(load_profile_payload(profile_id), **kwargs)


def _android_project(tmp_path: Path) -> Path:
    project = tmp_path / "app"
    project.mkdir()
    (project / "settings.gradle.kts").write_text("rootProject.name = \"x\"\n", encoding="utf-8")
    return project


def _install(home: Path, name: str, description: str) -> Path:
    path = home / ".claude" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n", encoding="utf-8")
    return path


PROFILES_DIR = REPO / "src" / "agent_flow" / "profiles"


def _profile_ids() -> list[str]:
    return sorted(
        path.stem for path in PROFILES_DIR.glob("*.yaml") if not path.stem.startswith("_")
    )


def test_section_phases_cover_exactly_the_gated_phases():
    """read gate가 없는 phase에 skill을 밀어 넣으면 프롬프트만 길어진다."""
    assert IMPLEMENTATION_PHASES | REVIEW_PHASES == set(CODE_PHASES)
    assert not IMPLEMENTATION_PHASES & REVIEW_PHASES


def test_profile_group_routes_the_bundled_skills_it_owns():
    routed = _route(
        "android",
        phase_id="implement",
        changed_files=["app/src/main/java/A.kt"],
        task_text="Fix recomposition jank in the feed",
    )

    assert "android-code-review" in _names(routed)
    assert {skill.group for skill in routed} == {"profile"}


def test_no_external_skill_name_is_routed_by_declaration():
    """반증: 표가 되살아나면 죽은 이름이 다시 required가 된다."""
    routed = _route(
        "android",
        phase_id="implement",
        changed_files=["app/src/main/java/A.kt"],
        task_text="camerax migration recomposition jank",
    )

    assert "camera1-to-camerax" not in _names(routed)
    assert "compose-recomposition-performance" not in _names(routed)


def test_non_code_phases_never_route():
    for phase_id in ("design", "prd", "slice-plan", "commit", "gates", "merge"):
        assert _route(
            "android",
            phase_id=phase_id,
            changed_files=["app/src/main/java/A.kt"],
            task_text="recomposition jank insets edge-to-edge",
        ) == ()


def test_a_group_without_selectors_never_activates():
    profile = {
        "skills": {
            "required_review": [
                {"group": "declared", "skills": ["prose-only"]},
                {"group": "scoped", "skills": ["selected"], "task_terms": ["widget"]},
            ]
        }
    }

    routed = routed_profile_skills(
        profile,
        phase_id="implement",
        changed_files=["app/A.kt"],
        task_text="widget work",
    )

    assert _names(routed) == {"selected"}


@pytest.mark.parametrize("profile_id", ["python", "nextjs", "typescript", "ios"])
def test_unrelated_profiles_never_route_android_vocabulary(profile_id, tmp_path, monkeypatch):
    """#105의 핵심 조건. Android 어휘가 다른 profile에 노출되면 안 된다."""
    home = tmp_path / "home"
    _install(home, "edge-to-edge", "Use when insets or system bars change.")
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "proj"
    project.mkdir()
    profile = load_profile_payload(profile_id)

    resolution = resolve_phase_skills(
        project_root=project,
        phase_id="implement",
        profile=profile,
        changed_files=["src/a.py", "app/page.tsx", "Sources/App.swift"],
        task_text="insets edge-to-edge status bar",
        host="claude",
    )

    assert "edge-to-edge" not in {skill.name for skill in resolution.required}
    # 이 profile의 표가 스스로 라우팅되는 것은 정상이다. 금지 조건은 android 이름이
    # 여기 섞이는 것 하나다 — `== ()`로 적으면 표가 죽어 있어야만 통과한다.
    routed = _names(_route(profile_id, phase_id="implement", changed_files=["src/a.py"], task_text=""))
    android = _names(_route("android", phase_id="implement", changed_files=["A.kt"], task_text=""))
    assert not routed & android


def test_react_native_reaches_native_vocabulary_only_through_native_paths(tmp_path, monkeypatch):
    """옛 `android-native-escalation`의 자리. 다른 profile의 표를 끌어오지 않고 경로로 좁힌다."""
    home = tmp_path / "home"
    _install(home, "agp-9-upgrade", "Upgrade a project to Android Gradle Plugin 9.")
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "rn"
    project.mkdir()
    profile = load_profile_payload("react-native")
    catalog = discover_skill_catalog(project, skill_roots(project, profile=profile, host="claude"))

    ts_only = match_external(
        profile,
        catalog,
        phase_id="implement",
        changed_files=["src/App.tsx"],
        task_text="rename a prop",
        env={},
    )
    native = match_external(
        profile,
        catalog,
        phase_id="implement",
        changed_files=["android/app/build.gradle.kts"],
        task_text="android gradle plugin 올리기",
        env={},
    )

    assert ts_only == ()
    assert [item.name for item in native] == ["agp-9-upgrade"]


def test_multi_profile_merge_keeps_both_vocabularies():
    """반증: `update`만 하면 뒤 profile이 앞 profile의 routing을 통째로 덮는다."""
    merged = merged_profile_payload(
        [load_profile_payload("react-native"), load_profile_payload("android")]
    )

    ids = {domain.id for domain in parse_external(merged, env={}).domains}
    assert {"expo-sdk-config", "android-native"} <= ids
    assert {"ui-insets-systembars", "platform-sdk"} <= ids
    assert "android_skills" not in merged


def test_runner_profile_union_routes_like_the_flat_merge(tmp_path):
    """runner가 만든 union이 resolver가 읽는 shape인가.

    반증 대상은 하나다: `skills`를 profile id로 한 겹 감싸면
    `_required_review_groups`가 `required_review` 키를 못 찾아 다중 profile run의
    routed required가 조용히 0개가 된다. 그 상태에서도 `agent-flow status`는
    `merged_profile_payload`로 평평하게 합쳐 5개를 보므로, 같은 run에 대해 두
    판정이 갈린다.
    """
    from agent_flow.core.profile_resolution import load_profile_union

    _profile_id, union = load_profile_union(
        tmp_path, ["react-native", "android"], explicit_fallback=False
    )
    flat = merged_profile_payload(
        [load_profile_payload("react-native"), load_profile_payload("android")]
    )

    changed = ["android/app/src/main/java/A.kt"]
    union_routed = _names(
        routed_profile_skills(union, phase_id="implement", changed_files=changed, task_text="")
    )
    flat_routed = _names(
        routed_profile_skills(flat, phase_id="implement", changed_files=changed, task_text="")
    )

    assert union_routed == flat_routed
    assert "android-code-review" in union_routed


def test_first_profile_owns_a_duplicated_group():
    first = {
        "skills": {
            "required_review": [
                {"group": "shared", "skills": ["a"], "path_globs": ["**/*.kt"]}
            ]
        }
    }
    second = {
        "skills": {
            "required_review": [
                {"group": "shared", "skills": ["b"], "path_globs": ["**/*.kt"]}
            ]
        }
    }

    merged = merged_profile_payload([first, second])

    assert [group["skills"] for group in merged["skills"]["required_review"]] == [["a"]]


def test_merge_keeps_each_profile_group():
    """반증: 6개 profile 전부가 `group: profile`을 쓴다. group id만으로 dedupe하면
    다중 profile 프로젝트에서 두 번째 profile의 표가 통째로 사라진다."""
    merged = merged_profile_payload(
        [load_profile_payload("android"), load_profile_payload("python")]
    )

    routed = [set(group["skills"]) for group in merged["skills"]["required_review"]]
    assert any("android-code-review" in skills for skills in routed), routed
    assert any("python-api-clean-architecture" in skills for skills in routed), routed


def test_routable_group_skills_drops_selectorless_groups():
    """반증: 이름이 선언돼 있다는 것만으로 도달 가능하다고 세면 doctor는 활성화되지
    않는 skill을 통과시킨다. selectors 없는 group은 어떤 변경에도 걸리지 않는다."""
    profile = {
        "skills": {
            "required_review": [
                {"group": "profile", "skills": ["scoped"], "path_globs": ["**/*.py"]},
                {"group": "loose", "skills": ["unscoped"]},
            ]
        }
    }

    assert routable_group_skills(profile) == {"scoped"}


def test_every_profile_group_declares_selectors():
    """selectors 없는 `required_review` group은 표가 있으나 죽어 있다 — 어떤 변경에도
    걸리지 않으므로 그 이름은 어느 phase에도 올라가지 않는다."""
    selectorless = [
        f"{profile_id}:{group.get('group', '')}"
        for profile_id in _profile_ids()
        for group in (load_profile_payload(profile_id).get("skills") or {}).get("required_review")
        or []
        if not (group.get("task_terms") or group.get("path_globs"))
    ]

    assert selectorless == []


def test_resolver_surfaces_a_vocabulary_matched_skill(tmp_path, monkeypatch):
    """반증: resolver가 어휘 결과를 안 받으면 그 skill은 프롬프트에도 게이트에도 없다."""
    home = tmp_path / "home"
    _install(home, "edge-to-edge", "Use when the status bar or insets overlap content.")
    monkeypatch.setenv("HOME", str(home))
    project = _android_project(tmp_path)
    common = dict(
        profile=load_profile_payload("android"),
        changed_files=["app/src/main/java/A.kt"],
        task_text="status bar가 콘텐츠를 가린다",
    )

    resolution = resolve_phase_skills(
        project_root=project, phase_id="implement", host="claude", **common
    )
    block = local_skill_prompt_block(project, "implement", **common)

    matched = next(skill for skill in resolution.required if skill.name == "edge-to-edge")
    assert matched.exists
    assert "edge-to-edge" in block


def test_missing_bundled_skill_must_be_named_in_the_gate(tmp_path, monkeypatch):
    """부재는 위반이 아니지만 `none`으로 덮을 수는 없다."""
    monkeypatch.setenv("HOME", str(tmp_path / "empty"))
    project = _android_project(tmp_path)
    gate = (
        "## Completion Gate\n"
        "skill-availability: degraded\n"
        "skill-use-evidence: unavailable\n"
        "project-local-skills: checked\n"
        "project-local-skills-used: n/a\n"
        "missing-required-profile-skills: none\n"
    )
    common = dict(
        profile=load_profile_payload("android"),
        changed_files=["app/src/main/java/A.kt"],
        task_text="Fix recomposition jank",
    )

    missing = missing_local_skill_markers(gate, project, "implement", **common)

    reports = [item for item in missing if item.startswith("missing-required-profile-skills:")]
    assert reports, missing
    assert "android-code-review" in reports[0]


def test_unrelated_profile_gate_stays_silent(tmp_path):
    project = tmp_path / "py"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname = \"x\"\n", encoding="utf-8")
    gate = (
        "## Completion Gate\n"
        "skill-availability: pass\n"
        "skill-use-evidence: unavailable\n"
        "project-local-skills: checked\n"
        "project-local-skills-used: n/a\n"
    )

    missing = missing_local_skill_markers(
        gate,
        project,
        "implement",
        profile=load_profile_payload("python"),
        changed_files=["src/a.py"],
        task_text="recomposition jank insets compose state",
    )

    reports = [item for item in missing if item.startswith("missing-required-profile-skills:")]
    # python 표가 자기 skill의 부재를 보고하는 것은 정상이다. 금지 조건은 compose/android
    # 어휘가 python 게이트에 android 이름을 끌어오는 것 하나다.
    assert not [item for item in reports if "android" in item], reports


def test_prompt_contract_lists_every_marker_the_gate_demands(tmp_path):
    """반증: 계약에 빠진 마커가 있으면 계약대로 쓴 artifact가 반드시 한 번 거부된다."""
    project = _android_project(tmp_path)
    common = dict(
        profile=load_profile_payload("android"),
        changed_files=["app/src/main/java/A.kt"],
        task_text="Fix recomposition jank",
    )

    block = local_skill_prompt_block(project, "implement", **common)
    contract = block.split("```text")[1].split("```")[0]
    artifact = "## Completion Gate\n" + contract.replace(
        "skill-use-evidence: verified|unavailable", "skill-use-evidence: unavailable"
    )

    assert missing_local_skill_markers(artifact, project, "implement", **common) == []


def test_matched_skill_reports_the_path_it_was_discovered_at(tmp_path, monkeypatch):
    """카탈로그는 설치된 것만 담는다. 이름으로 다시 해석하면 활성 host 필터가
    설치돼 있는 skill을 "없다"고 보고한다 — 그 거짓 진술이 게이트를 degraded로 만든다."""
    project = _android_project(tmp_path)
    home = tmp_path / "home"
    installed = _install(home, "edge-to-edge", "Use when the status bar or insets overlap content.")
    monkeypatch.setenv("HOME", str(home))
    common = dict(
        project_root=project,
        phase_id="implement",
        profile=load_profile_payload("android"),
        changed_files=["app/src/main/java/A.kt"],
        task_text="status bar overlap 수정",
    )

    def matched(host: str):
        resolution = resolve_phase_skills(host=host, **common)
        return next(
            skill for skill in resolution.required if skill.name == "edge-to-edge"
        )

    for host in ("claude", "codex"):
        skill = matched(host)
        assert skill.exists is True, host
        assert skill.path == installed, host


def test_a_declared_bundled_name_still_resolves_from_the_active_host_only(tmp_path, monkeypatch):
    """우리가 이름으로 선언한 skill은 미설치일 수 있다. 그쪽은 host 격리를 유지한다."""
    project = _android_project(tmp_path)
    home = tmp_path / "home"
    _install(home, "android-code-review", "Bundled review skill.")
    monkeypatch.setenv("HOME", str(home))
    common = dict(
        project_root=project,
        phase_id="implement",
        profile=load_profile_payload("android"),
        changed_files=["app/src/main/java/A.kt"],
        task_text="rename a variable",
    )

    def found(host: str) -> bool:
        resolution = resolve_phase_skills(host=host, **common)
        return any(
            skill.exists for skill in resolution.required if skill.name == "android-code-review"
        )

    assert found("claude") is True
    assert found("codex") is False


def test_missing_report_uses_the_declared_wording():
    skill = RoutedSkill("android-code-review", "profile", "", "missing local profile: <skill>")

    assert missing_routed_report(skill) == "missing local profile: android-code-review"
