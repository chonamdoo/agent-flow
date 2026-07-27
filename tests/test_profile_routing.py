"""#105: profile 표가 실제 resolver 입력인가.

표가 선언만 되고 아무도 안 읽으면 "보여 주지 않는 표를 읽으라"가 된다. 그래서
테스트는 선언이 아니라 **routing 결과**를 반증한다: 무관한 profile에서 나오지
않는가, 변경 범위 밖에서 나오지 않는가, phase 섹션이 갈리는가.
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
    routed_profile_skills,
)
from agent_flow.core.profiles import load_profile_payload
from agent_flow.core.skill_resolver import (
    CODE_PHASES,
    IMPLEMENTATION_PHASES,
    REVIEW_PHASES,
    resolve_phase_skills,
)


def _names(routed: tuple[RoutedSkill, ...]) -> set[str]:
    return {skill.name for skill in routed}


def _route(profile_id: str, **kwargs) -> tuple[RoutedSkill, ...]:
    return routed_profile_skills(load_profile_payload(profile_id), **kwargs)


def test_section_phases_cover_exactly_the_gated_phases():
    """read gate가 없는 phase에 skill을 밀어 넣으면 프롬프트만 길어진다."""
    assert IMPLEMENTATION_PHASES | REVIEW_PHASES == set(CODE_PHASES)
    assert not IMPLEMENTATION_PHASES & REVIEW_PHASES


def test_android_table_routes_on_matching_task_scope():
    routed = _route(
        "android",
        phase_id="implement",
        changed_files=["app/src/main/java/A.kt"],
        task_text="Fix recomposition jank in the feed",
    )

    assert "compose-recomposition-performance" in _names(routed)
    groups = {skill.group for skill in routed}
    assert "chrisbanes_skills" in groups


def test_kotlin_change_alone_does_not_pull_the_whole_table():
    """반증: 선택자 없이 파일 확장자만으로 붙이면 32개가 통째로 required가 된다."""
    routed = _route(
        "android",
        phase_id="implement",
        changed_files=["app/src/main/java/A.kt"],
        task_text="",
    )

    assert not {skill.name for skill in routed if skill.group != "profile"}


def test_table_sections_split_by_phase_kind():
    common = dict(changed_files=["app/src/main/java/A.kt"], task_text="camerax migration")

    implementation = _names(_route("android", phase_id="implement", **common))
    review = _names(_route("android", phase_id="final-review", **common))

    # camera1-to-camerax는 implementation 섹션에만 있다.
    assert "camera1-to-camerax" in implementation
    assert "camera1-to-camerax" not in review


def test_non_code_phases_never_route():
    for phase_id in ("design", "prd", "slice-plan", "commit", "gates", "merge"):
        assert _route(
            "android",
            phase_id=phase_id,
            changed_files=["app/src/main/java/A.kt"],
            task_text="recomposition jank insets edge-to-edge",
        ) == ()


@pytest.mark.parametrize("profile_id", ["python", "nextjs", "typescript", "ios"])
def test_unrelated_profiles_never_route_android_skills(profile_id):
    """#105의 핵심 조건. Android 표가 다른 profile에 노출되면 안 된다."""
    routed = _route(
        profile_id,
        phase_id="implement",
        changed_files=["src/a.py", "app/page.tsx", "Sources/App.swift"],
        task_text="recomposition jank insets edge-to-edge compose state perfetto",
    )

    assert routed == ()


def test_react_native_escalates_only_for_android_native_changes():
    ts_only = _route(
        "react-native",
        phase_id="implement",
        changed_files=["src/App.tsx"],
        task_text="perfetto trace jank",
    )
    native = _route(
        "react-native",
        phase_id="implement",
        changed_files=["android/app/src/main/java/A.kt"],
        task_text="perfetto trace jank",
    )

    assert ts_only == ()
    assert "perfetto-trace-analysis" in _names(native)
    assert {skill.group for skill in native} == {"android-native-escalation"}


def test_escalated_group_reports_the_declared_missing_line():
    native = _route(
        "react-native",
        phase_id="implement",
        changed_files=["android/app/src/main/java/A.kt"],
        task_text="perfetto trace",
    )

    reports = {missing_routed_report(skill) for skill in native}
    assert "missing local android_skills: perfetto-trace-analysis" in reports


def test_multi_profile_merge_keeps_every_declared_group():
    """반증: `update`만 하면 뒤 profile이 앞 profile의 routing을 통째로 덮는다."""
    merged = merged_profile_payload(
        [load_profile_payload("react-native"), load_profile_payload("android")]
    )

    groups = [group["group"] for group in merged["skills"]["required_review"]]
    assert "android-native-escalation" in groups
    assert "chrisbanes_skills" in groups
    assert merged["android_skills"]["source"] == "https://github.com/android/skills"

    routed = routed_profile_skills(
        merged,
        phase_id="implement",
        changed_files=["android/app/src/main/java/A.kt"],
        task_text="recomposition jank",
    )
    assert "compose-recomposition-performance" in _names(routed)


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


def test_entry_without_selectors_never_activates():
    profile = {
        "skills": {
            "required_review": [
                {"group": "t", "skills_from": "t_skills", "path_globs": ["**/*.kt"]}
            ]
        },
        "t_skills": {
            "source": "https://example.test",
            "implementation": [
                {"skill": "declared", "when": "prose only"},
                {"skill": "selected", "task_terms": ["widget"]},
            ],
        },
    }

    routed = routed_profile_skills(
        profile,
        phase_id="implement",
        changed_files=["app/A.kt"],
        task_text="widget work",
    )

    assert _names(routed) == {"selected"}


def _android_project(tmp_path: Path) -> Path:
    project = tmp_path / "app"
    project.mkdir()
    (project / "settings.gradle.kts").write_text("rootProject.name = \"x\"\n", encoding="utf-8")
    return project


def test_resolver_surfaces_routed_skills_with_the_table_source(tmp_path):
    """반증: resolver가 표를 안 읽으면 이 skill은 프롬프트에도 게이트에도 없다."""
    project = _android_project(tmp_path)

    resolution = resolve_phase_skills(
        project_root=project,
        phase_id="implement",
        profile=load_profile_payload("android"),
        changed_files=["app/src/main/java/A.kt"],
        task_text="Fix recomposition jank",
        host="codex",
        env={},
    )

    routed = next(
        skill
        for skill in resolution.required
        if skill.name == "compose-recomposition-performance"
    )
    if not routed.exists:
        assert "chrisbanes/skills" in routed.install_hint


def test_prompt_block_lists_the_routed_skill(tmp_path):
    project = _android_project(tmp_path)

    block = local_skill_prompt_block(
        project,
        "implement",
        profile=load_profile_payload("android"),
        changed_files=["app/src/main/java/A.kt"],
        task_text="Fix recomposition jank",
    )

    assert "compose-recomposition-performance" in block


def test_missing_routed_skill_must_be_named_in_the_gate(tmp_path):
    """부재는 위반이 아니지만 `none`으로 덮을 수는 없다."""
    project = _android_project(tmp_path)
    gate = (
        "## Completion Gate\n"
        "skill-availability: degraded\n"
        "skill-read-evidence: unavailable\n"
        "project-local-skills: checked\n"
        "project-local-skills-used: n/a\n"
        "missing-required-profile-skills: none\n"
    )

    missing = missing_local_skill_markers(
        gate,
        project,
        "implement",
        profile=load_profile_payload("android"),
        changed_files=["app/src/main/java/A.kt"],
        task_text="Fix recomposition jank",
    )

    routed_reports = [item for item in missing if item.startswith("missing-required-profile-skills:")]
    resolution = resolve_phase_skills(
        project_root=project,
        phase_id="implement",
        profile=load_profile_payload("android"),
        changed_files=["app/src/main/java/A.kt"],
        task_text="Fix recomposition jank",
    )
    absent = [skill.name for skill in resolution.missing]
    if absent:
        assert routed_reports, missing
        assert "compose-recomposition-performance" in routed_reports[0]
    else:
        assert not routed_reports


def test_unrelated_profile_gate_stays_silent(tmp_path):
    project = tmp_path / "py"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname = \"x\"\n", encoding="utf-8")
    gate = (
        "## Completion Gate\n"
        "skill-availability: pass\n"
        "skill-read-evidence: unavailable\n"
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

    assert not [item for item in missing if item.startswith("missing-required-profile-skills:")]
