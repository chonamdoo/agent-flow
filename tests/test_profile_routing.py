"""#105: profile 선언이 실제 resolver 입력인가.

선언이 되고 아무도 안 읽으면 "보여 주지 않는 것을 읽으라"가 된다. 그래서 테스트는
선언이 아니라 **routing 결과**를 반증한다: 무관한 profile에서 나오지 않는가, 변경 범위
밖에서 나오지 않는가, phase가 갈리는가.

이 모듈은 **우리가 이름을 소유한 bundled skill**의 라우팅을 지킨다. 설치된 외부 skill의
어휘 라우팅은 `test_skill_matching.py`가 지킨다 — 이름을 우리가 적지 않기 때문이다.
"""
from __future__ import annotations

import json
import re
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
    PhaseSkills,
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
    (project / "build.gradle.kts").write_text('plugins { id("com.android.application") }\n', encoding="utf-8")
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
    # `app/src/main/java/A.kt`는 `app-shell` 경로다. 배선 자리는 architecture group에서
    # 빼기로 했으므로 baseline만 걸린다.
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

    override = tmp_path / ".agent-flow/profiles/android.local.yaml"
    override.parent.mkdir(parents=True)
    override.write_text(
        "pr:\n  merge_strategy: squash\n"
        "commit_convention:\n  style: conventional\n",
        encoding="utf-8",
    )
    _profile_id, union = load_profile_union(
        REPO, ["react-native", "android"], explicit_fallback=False, project_root=tmp_path
    )
    flat = merged_profile_payload(
        [load_profile_payload("react-native", tmp_path), load_profile_payload("android", tmp_path)]
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
        # required tier는 명시된 concern에서만 나온다. 어휘 매칭만으로 required가
        # 되면 같은 변경이 host의 카탈로그 구성에 따라 다른 게이트를 만든다.
        concerns=["ui-insets-systembars"],
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
    # 계약은 허용 값을 `a|b`로 적는다. artifact는 그중 하나를 골라야 하므로 첫 번째
    # 대안으로 구체화한다 — 특정 marker 이름을 손으로 적으면 새 marker가 이 가드를
    # 빠져나간다.
    artifact = "## Completion Gate\n" + re.sub(
        r"^([a-z0-9-]+): ([^|\n]+)\|\S+$",
        r"\1: \2",
        contract,
        flags=re.M,
    )

    assert missing_local_skill_markers(artifact, project, "implement", **common) == []


def test_matched_skill_reports_only_the_path_its_host_can_open(tmp_path, monkeypatch):
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
        concerns=["ui-insets-systembars"],
    )

    claude = resolve_phase_skills(host="claude", **common)
    codex = resolve_phase_skills(host="codex", **common)
    found = next(skill for skill in claude.required if skill.name == "edge-to-edge")

    assert found.exists is True
    assert found.path == installed
    assert all(skill.name != "edge-to-edge" for skill in (*codex.required, *codex.optional))


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
    skill = RoutedSkill(
        "android-code-review", "profile", "missing local profile: <skill>"
    )

    assert missing_routed_report(skill) == "missing local profile: android-code-review"


def test_a_copy_only_component_change_does_not_require_architecture_docs():
    """반증: baseline과 architecture가 한 group이면 문구 변경에도 계층 문서가 붙는다.

    실측 근거: `.tsx` 한 줄 변경의 required가 4개 / 15,746 B였고, 그중
    `react-clean-architecture`와 그 dependency `clean-architecture-core`가
    10,526 B였다. 버튼 문구를 바꾸는 작업이 계층 계약을 읽어야 할 이유는 없다.
    """
    for changed in ("src/components/Button.tsx", "app/globals.css", "app/page.tsx"):
        routed = _names(
            _route(
                "nextjs",
                phase_id="implement",
                changed_files=[changed],
                task_text="버튼 문구와 색상만 수정",
            )
        )

        assert "react-clean-architecture" not in routed, changed
    # baseline은 그대로 붙는다. 축소는 계층 문서에만 적용된다.
    assert "react-development-guide" in _names(
        _route(
            "nextjs",
            phase_id="implement",
            changed_files=["src/components/Button.tsx"],
            task_text="버튼 문구와 색상만 수정",
        )
    )


# 배선 자리다. `app/**`는 App Router 프로젝트 소스 대부분이라 architecture group에서
# 빼기로 했고, 그 결정을 여기서도 같은 이름으로 적어 둔다.
_COMPOSITION_ROOT_ROLES = {"app-shell", "android-native"}


def _role_sample_paths(profile_id: str) -> list[str]:
    """선언된 role path마다 표본 두 개: layer 루트의 파일과 자리표시자 아래의 파일.

    자리표시자를 항상 채우면 `**/<layer>/*/**` 같은 glob이 layer 루트 파일을 놓치는
    것을 못 본다 — 첫 구현이 정확히 그 상태였다.
    """
    payload = load_profile_payload(profile_id)
    roles = (payload.get("architecture") or {}).get("roles") or []
    samples: list[str] = []
    for role in roles:
        if str(role.get("id")) in _COMPOSITION_ROOT_ROLES:
            continue
        for path in role.get("paths") or []:
            filled = re.sub(r"<[^>]+>", "sample", str(path)).strip("/")
            root = str(path).split("<", 1)[0].strip("/")
            if filled:
                samples.append(f"{filled}/Probe.txt")
            if root:
                samples.append(f"{root}/Probe.txt")
    return sorted(dict.fromkeys(samples))


@pytest.mark.parametrize("profile_id", ["android", "flutter", "ios", "nextjs", "python", "react-native"])
def test_every_declared_role_path_requires_the_architecture_doc(profile_id):
    """불변: 축소가 계층 경계 변경까지 덮으면 그건 축소가 아니라 게이트 제거다.

    두 경로만 확인하면 나머지 role path가 빠져도 통과한다 — 실제로 첫 구현에서
    `app-shell`과 `core-ui` role이 전부 빠졌고 두 테스트 모두 초록이었다. 그래서
    표본은 profile이 선언한 `architecture.roles`에서 전부 뽑는다.
    """
    architecture_skills = {
        name
        for group in load_profile_payload(profile_id)["skills"]["required_review"]
        if group.get("group") == "architecture"
        for name in group.get("skills") or ()
    }
    assert architecture_skills, profile_id

    uncovered = [
        sample
        for sample in _role_sample_paths(profile_id)
        if not architecture_skills
        <= _names(
            _route(profile_id, phase_id="implement", changed_files=[sample], task_text="")
        )
    ]

    assert uncovered == []


@pytest.mark.parametrize("profile_id", ["android", "flutter", "ios", "nextjs", "python", "react-native"])
def test_architecture_groups_never_activate_on_a_bare_extension(profile_id):
    """architecture group이 언어 확장자만으로 켜지면 split이 무의미하다.

    이름을 고른 목록이 아니라 성질 판정이다: `**/*.<ext>` 형태의 glob 하나라도
    architecture group에 있으면 그 스택의 모든 소스 변경이 계층 문서를 요구한다.
    """
    profile = load_profile_payload(profile_id)
    groups = profile["skills"]["required_review"]

    bare = [
        glob
        for group in groups
        if group.get("group") == "architecture"
        for glob in group.get("path_globs") or ()
        if re.fullmatch(r"\*\*/\*\.[A-Za-z0-9]+", glob)
    ]

    assert bare == []


def test_the_same_change_requires_the_same_skills_in_either_language(tmp_path, monkeypatch):
    """반증: 판정이 task 문구에 걸리면 한국어로 쓴 작업이 다른 게이트를 받는다.

    실측 근거: 영어 문구는 required 6개 / 26,241 B, 같은 뜻의 한국어는 4개였다.
    `term_in`이 ASCII 단어 경계로 맞추고 domain `terms`가 영어 전용이기 때문인데,
    그 차이가 required를 만들면 안 된다.
    """
    home = tmp_path / "home"
    _install(home, "frontend-design", "Guidance for typography and color palette.")
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "web"
    project.mkdir()
    profile = load_profile_payload("nextjs")

    def required(task_text: str) -> set[str]:
        return {
            skill.name
            for skill in resolve_phase_skills(
                project_root=project,
                phase_id="implement",
                profile=profile,
                changed_files=["src/components/Button.tsx"],
                task_text=task_text,
                host="claude",
            ).required
        }

    assert required("버튼 색상 팔레트와 타이포그래피만 조정") == required(
        "adjust button color palette and typography only"
    )


def test_a_concern_only_group_waits_for_the_concern():
    """경로가 드러내지 못하는 것을 위한 자리다. 명시되지 않으면 켜지지 않는다."""
    profile = {
        "skills": {
            "required_review": [
                {"group": "security", "skills": ["threat-model"], "concerns": ["security"]}
            ]
        }
    }
    args = dict(phase_id="implement", changed_files=["src/components/Button.tsx"], task_text="")

    assert _names(routed_profile_skills(profile, **args)) == set()
    assert _names(routed_profile_skills(profile, concerns=["security"], **args)) == {
        "threat-model"
    }


def test_a_rename_keeps_both_paths_in_the_change_set(tmp_path, monkeypatch):
    """반증: rename 레코드의 원본 경로는 상태 접두사가 없다.

    레코드마다 앞 3글자를 자르면 그 필드에서 실제 문자 3개가 사라져
    `core/domain/A.kt`가 `e/domain/A.kt`가 되고, 파일을 옮기는 변경은 경로 기반
    라우팅에서 조용히 빠진다.
    """
    from agent_flow.core.local_skills import _porcelain_paths

    stdout = "R  core/domain/order/New.kt\0core/domain/order/Old.kt\0 D core/data/order/Gone.kt\0"

    paths = _porcelain_paths(stdout)

    assert paths == (
        "core/domain/order/New.kt",
        "core/domain/order/Old.kt",
        "core/data/order/Gone.kt",
    )


def test_an_uppercase_extension_still_matches_its_glob():
    """확장자 표기 하나로 skill 강제가 사라지면 안 된다. macOS에서 실제로 그랬다."""
    from agent_flow.core.skill_resolver import selector_matches

    assert selector_matches(
        task_terms=[],
        path_globs=["**/*.tsx"],
        changed_files=["src/components/Button.TSX"],
        task_text="",
    )


def test_every_architecture_group_declares_the_opt_in_concern():
    """반증: 이 group의 주석은 배제된 배선 경로의 계층 검토를 `--concern`으로
    지목하라고 안내한다. id가 선언돼 있지 않으면 그 안내는 거짓이고, 명시 요청은
    unknown concern으로 **거부된다** — 축소만 남고 탈출구가 0이 된다.
    """
    from agent_flow.core.local_skills import declared_concern_ids

    for profile_id in ("android", "flutter", "ios", "nextjs", "python", "react-native"):
        payload = load_profile_payload(profile_id)
        groups = [
            group
            for group in payload["skills"]["required_review"]
            if group.get("group") == "architecture"
        ]
        assert groups, profile_id
        for group in groups:
            assert "architecture" in (group.get("concerns") or []), profile_id
        assert "architecture" in declared_concern_ids(payload), profile_id


def test_the_opt_in_concern_reaches_a_path_the_globs_exclude():
    """배선 자리(`app/**`)는 architecture glob에서 빼기로 했다. 그 변경에서 계층
    문서를 요구하려면 concern 하나로 되어야 한다 — 되지 않으면 축소가 되돌릴 수
    없는 축소가 된다."""
    wiring = ["app/providers.tsx"]

    without = _names(_route("nextjs", phase_id="implement", changed_files=wiring))
    with_concern = _names(
        _route(
            "nextjs",
            phase_id="implement",
            changed_files=wiring,
            concerns=["architecture"],
        )
    )

    assert "react-clean-architecture" not in without
    assert "react-clean-architecture" in with_concern


@pytest.mark.parametrize("profile_id", ["typescript", "node", "nextjs"])
@pytest.mark.parametrize(
    "task,concerns,expected",
    [
        ("Fix React Hook Form dirty reset", [], "react-hook-form-zod"),
        ("RHF 폼 초기화 정책 수정", [], "react-hook-form-zod"),
        ("Update form adapter", ["react-hook-form-zod"], "react-hook-form-zod"),
        ("TanStack Form 폼 초기화", [], "react-tanstack-form"),
        ("폼 구독 및 폼 초기화 정책 수정", [], None),
        ("Update Zod form schema", [], None),
        ("검색 색인 개선", [], "react-web-seo"),
        ("Fix Storybook focus", [], "react-storybook"),
        ("툴콜 승인 인자 검증", [], "llm-tool-development"),
    ],
)
def test_react_capabilities_route_by_intent_not_all_tsx(
    tmp_path: Path, profile_id: str, task: str, concerns: list[str], expected: str | None,
) -> None:
    (tmp_path / "package.json").write_text('{"dependencies":{"react":"19"}}', encoding="utf-8")
    payload = load_profile_payload(profile_id, tmp_path)
    baseline = _names(routed_profile_skills(
        payload, phase_id="implement", changed_files=["src/components/Label.tsx"],
        task_text="Change label color",
    ))
    optional = {"react-hook-form-zod", "react-tanstack-form", "react-web-seo", "react-storybook", "llm-tool-development"}
    assert baseline.isdisjoint(optional)
    activated = _names(routed_profile_skills(
        payload, phase_id="implement", changed_files=["src/components/Label.tsx"],
        task_text=task, concerns=concerns,
    ))
    assert activated & optional == ({expected} if expected else set())


@pytest.mark.parametrize(
    "dependencies,task,concerns,expected",
    [
        ({"react": "19"}, "TanStack Form 폼 초기화", [], {"react-tanstack-form"}),
        ({"react": "19"}, "Update form adapter", ["react-tanstack-form"], {"react-tanstack-form"}),
        ({"react": "19"}, "Fix RHF dirty reset", [], {"react-hook-form-zod"}),
        ({"react": "19"}, "Change label color", [], set()),
        ({}, "TanStack Form validation", ["react-tanstack-form"], set()),
        ({"react": "19", "react-native": "0.80"}, "TanStack Form validation", ["react-tanstack-form"], set()),
    ],
)
def test_tanstack_form_capability_routes_only_for_react_web(
    tmp_path: Path, dependencies: dict, task: str, concerns: list[str], expected: set[str],
) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": dependencies}), encoding="utf-8")
    names = _names(routed_profile_skills(
        load_profile_payload("typescript", tmp_path), phase_id="implement",
        changed_files=["src/components/Form.tsx"], task_text=task, concerns=concerns,
    ))
    assert names & {"react-tanstack-form", "react-hook-form-zod"} == expected


@pytest.mark.parametrize("profile_id,adapter", [("spring", "spring-boot-development-guide"), ("ktor", "ktor-development-guide")])
def test_kotlin_backend_routes_its_adapter_without_android(profile_id: str, adapter: str) -> None:
    names = _names(_route(
        profile_id, phase_id="implement", changed_files=["src/main/kotlin/api/Route.kt"], task_text="Fix authorization",
    ))
    assert names == {"kotlin-backend-development-guide", adapter}


@pytest.mark.parametrize("phase", ["implement", "review"])
def test_python_api_contract_routes_server_intent(phase: str) -> None:
    names = _names(_route(
        "python", phase_id=phase, changed_files=["app/routes.py"],
        task_text="Implement a FastAPI endpoint",
    ))
    assert "backend-api-contract" in names


@pytest.mark.parametrize(
    "task,path",
    [
        ("Fix CLI argument parsing", "cli.py"),
        ("Retry an HTTP client call with httpx", "client.py"),
    ],
)
def test_python_api_contract_does_not_infer_a_server_from_python_or_http(task: str, path: str) -> None:
    names = _names(_route(
        "python", phase_id="implement", changed_files=[path], task_text=task,
    ))
    assert "backend-api-contract" not in names


def test_python_api_contract_routes_an_explicit_concern_without_server_named_files() -> None:
    names = _names(_route(
        "python", phase_id="review", changed_files=["policy.py"],
        task_text="Preserve checkout authorization", concerns=["backend-api"],
    ))
    assert "backend-api-contract" in names


@pytest.mark.parametrize("profile_id", ["spring", "ktor"])
@pytest.mark.parametrize(
    "task,concerns,expected",
    [
        ("Update Java service dependencies", [], False),
        ("Prepare Java-to-Kotlin server migration", [], True),
        ("Prepare mixed JVM server compilation", [], True),
        ("Configure incremental language migration", ["kotlin-backend"], True),
    ],
)
def test_kotlin_backend_build_only_selection_requires_server_intent(
    profile_id: str, task: str, concerns: list[str], expected: bool,
) -> None:
    names = _names(_route(
        profile_id, phase_id="implement", changed_files=["build.gradle.kts"],
        task_text=task, concerns=concerns,
    ))
    assert ("kotlin-backend-development-guide" in names) is expected


@pytest.mark.parametrize("dependencies", [{}, {"react-native": "0.80", "react": "19"}, {"expo": "53", "react": "19"}])
@pytest.mark.parametrize("skill", ["react-hook-form-zod", "react-tanstack-form"])
def test_react_web_concern_cannot_activate_without_web_dependencies(tmp_path: Path, dependencies: dict, skill: str) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": dependencies}), encoding="utf-8")
    names = _names(routed_profile_skills(
        load_profile_payload("typescript", tmp_path), phase_id="review", changed_files=["Screen.tsx"],
        task_text=skill, concerns=[skill],
    ))
    assert skill not in names


_PORTFOLIO_WEB = {
    "react-scroll-restoration", "react-runtime-i18n",
    "ga4-ecommerce-events", "datadog-rum-sourcemaps",
    "react-tanstack-form",
}
_PORTFOLIO = _PORTFOLIO_WEB | {"nextjs-auth-session", "webview-json-rpc-bridge"}


def _portfolio_resolution(tmp_path, monkeypatch, profile_id, dependencies, task, concerns=(), changed_files=(), *, phase_id="implement"):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": dependencies}), encoding="utf-8",
    )
    bundled = tmp_path / ".agent-flow" / "skills"
    bundled.parent.mkdir(exist_ok=True)
    bundled.symlink_to(REPO / "skills", target_is_directory=True)
    return resolve_phase_skills(
        project_root=tmp_path, phase_id=phase_id, host="claude",
        profile=load_profile_payload(profile_id, tmp_path),
        task_text=task, concerns=concerns, changed_files=changed_files,
    )


@pytest.mark.parametrize(
    "profile_id,dependencies,task,expected",
    [
        ("typescript", {"react": "19"}, "Restore scroll restoration after history traversal", "react-scroll-restoration"),
        ("node", {"react": "19"}, "뒤로가기 위치 복원 수정", "react-scroll-restoration"),
        ("typescript", {"react": "19"}, "Fix runtime translation request isolation", "react-runtime-i18n"),
        ("nextjs", {"react": "19", "next": "16"}, "런타임 번역 로케일 변경", "react-runtime-i18n"),
        ("node", {"react": "19"}, "Fix GA4 ecommerce purchase identity", "ga4-ecommerce-events"),
        ("typescript", {"react": "19"}, "GA4 전자상거래 이벤트 중복 방지", "ga4-ecommerce-events"),
        ("nextjs", {"react": "19", "next": "16"}, "Match Datadog sourcemap release", "datadog-rum-sourcemaps"),
        ("node", {"react": "19"}, "Datadog 소스맵 업로드 릴리스 확인", "datadog-rum-sourcemaps"),
        ("nextjs", {"react": "19", "next": "16"}, "Fix Next.js authentication authorization", "nextjs-auth-session"),
        ("nextjs", {"react": "19", "next": "16"}, "Next.js 세션 쿠키 경계 수정", "nextjs-auth-session"),
        ("typescript", {"react": "19"}, "Fix @tanstack/react-form validation", "react-tanstack-form"),
        ("node", {"react": "19"}, "탄스택 폼 제출 오류 수정", "react-tanstack-form"),
        ("nextjs", {"react": "19", "next": "16"}, "Fix tanstack-form server validation", "react-tanstack-form"),
    ],
)
def test_portfolio_full_resolver_scopes_explicit_tasks(
    tmp_path, monkeypatch, profile_id, dependencies, task, expected,
):
    resolution = _portfolio_resolution(tmp_path, monkeypatch, profile_id, dependencies, task)
    required = {skill.name for skill in resolution.required}
    assert required & _PORTFOLIO == {expected}
    assert all(skill.exists for skill in resolution.required)


@pytest.mark.parametrize("profile_id", ["android", "ios", "react-native", "node", "typescript", "nextjs"])
@pytest.mark.parametrize("task,concerns", [
    ("Implement WebView JSON-RPC request correlation", []),
    ("웹뷰 JSON-RPC 응답 검증", []),
    ("Review document transport boundary", ["webview-json-rpc-bridge"]),
])
def test_bridge_full_resolver_reaches_native_and_web_hosts(tmp_path, monkeypatch, profile_id, task, concerns):
    resolution = _portfolio_resolution(tmp_path, monkeypatch, profile_id, {}, task, concerns)
    assert {skill.name for skill in resolution.required} & _PORTFOLIO == {"webview-json-rpc-bridge"}
    assert all(skill.exists for skill in resolution.required)


@pytest.mark.parametrize("profile_id,dependencies,task,concerns", [
    ("typescript", {}, "Fix runtime translation and GA4 ecommerce", list(_PORTFOLIO_WEB)),
    ("typescript", {"react": "19", "react-native": "0.80"}, "스크롤 복원, 런타임 번역, GA4 전자상거래, Datadog 소스맵", list(_PORTFOLIO_WEB)),
    ("typescript", {"react": "19", "expo": "53"}, "scroll restoration runtime i18n ga4 ecommerce datadog sourcemap", list(_PORTFOLIO_WEB)),
    ("node", {"react": "19"}, "Fix Next.js authentication", ["nextjs-auth-session"]),
    ("react-native", {"react": "19", "react-native": "0.80"}, "Implement TurboModule JSI native bridge", []),
    ("android", {}, "Adjust WebView insets and keyboard layout", []),
    ("ios", {}, "Fix native bridge module registration", []),
    ("python", {}, "Implement WebView JSON-RPC", ["webview-json-rpc-bridge"]),
    ("nextjs", {"react": "19", "next": "16"}, "Change label color", []),
    ("typescript", {"react": "19"}, "Fix TanStack Query cache invalidation", []),
])
def test_portfolio_full_resolver_rejects_nearby_non_targets(tmp_path, monkeypatch, profile_id, dependencies, task, concerns):
    resolution = _portfolio_resolution(
        tmp_path, monkeypatch, profile_id, dependencies, task, concerns,
        changed_files=["src/components/Label.tsx"],
    )
    assert {skill.name for skill in resolution.required}.isdisjoint(_PORTFOLIO)


@pytest.mark.parametrize("name", sorted(_PORTFOLIO_WEB | {"nextjs-auth-session"}))
def test_portfolio_concerns_reach_tasks_without_keyword_hints(tmp_path, monkeypatch, name):
    resolution = _portfolio_resolution(
        tmp_path, monkeypatch, "nextjs", {"react": "19", "next": "16"},
        "Review changed boundary", [name],
    )
    assert {skill.name for skill in resolution.required} & _PORTFOLIO == {name}


@pytest.mark.parametrize("profile_id,dependencies,task,expected,excluded", [
    ("android", {}, "공통 에러 처리", {"android-appshell-error-handling", "app-shell-error-contract"}, {"ios-app-shell-error-handling", "react-app-shell-error-handling"}),
    ("android", {}, "화면 상태 변경", {"android-clean-presentation-architecture"}, {"react-clean-presentation-architecture"}),
    ("android", {}, "무한 로딩 원인 분석", {"android-debugging"}, {"react-native-development-guide"}),
    ("android", {}, "피처 슬라이스 모듈 생성", {"android-module-creator"}, {"kotlin-backend-development-guide"}),
    ("android", {}, "서버 주도 ui 컴포넌트 카탈로그", {"android-sdui-architecture"}, {"react-clean-presentation-architecture"}),
    ("ios", {}, "세션 만료 처리", {"ios-app-shell-error-handling", "app-shell-error-contract"}, {"android-appshell-error-handling"}),
    ("ios", {}, "상태 홀더 변경", {"ios-clean-presentation-architecture"}, {"android-clean-presentation-architecture"}),
    ("flutter", {}, "프레젠테이션 계층 수정", {"flutter-clean-presentation-architecture"}, {"react-clean-presentation-architecture"}),
    ("flutter", {}, "공통 에러 처리", {"app-shell-error-contract"}, {"android-appshell-error-handling", "ios-app-shell-error-handling", "react-app-shell-error-handling", "react-native-app-shell-error-handling"}),
    ("typescript", {"react": "19"}, "전역 에러 처리", {"react-app-shell-error-handling", "app-shell-error-contract"}, {"react-native-app-shell-error-handling"}),
    ("typescript", {"react": "19"}, "화면 상태 변경", {"react-clean-presentation-architecture"}, {"react-native-clean-presentation-architecture"}),
    ("typescript", {"react": "19"}, "RHF 폼 수정", {"react-hook-form-zod"}, {"react-native-development-guide"}),
    ("react-native", {"react-native": "0.80"}, "공통 에러 처리", {"react-native-app-shell-error-handling", "app-shell-error-contract"}, {"react-app-shell-error-handling"}),
    ("react-native", {"expo": "53"}, "화면 상태 변경", {"react-native-clean-presentation-architecture"}, {"react-clean-presentation-architecture"}),
    ("react-native", {"react-native": "0.80"}, "내비게이션 구조 변경", {"react-native-operational-adoption"}, {"react-clean-architecture"}),
    ("spring", {}, "코틀린 백엔드 수정", {"kotlin-backend-development-guide"}, {"android-code-review"}),
    ("spring", {}, "스프링 트랜잭션 수정", {"spring-boot-development-guide"}, {"ktor-development-guide"}),
    ("ktor", {}, "코토 서버 수정", {"ktor-development-guide"}, {"spring-boot-development-guide"}),
    ("python", {}, "도구 호출 구현 검토", {"llm-tool-development"}, {"android-code-review"}),
    ("python", {}, "공통 에러와 화면 상태 변경", set(), {"app-shell-error-contract", "android-appshell-error-handling", "react-clean-presentation-architecture"}),
    ("typescript", {"react": "19", "expo": "53"}, "전역 에러와 화면 상태 변경", set(), {"react-app-shell-error-handling", "react-clean-presentation-architecture"}),
])
def test_localized_aliases_keep_platform_boundaries_in_full_resolver(
    tmp_path, monkeypatch, profile_id, dependencies, task, expected, excluded,
):
    resolution = _portfolio_resolution(tmp_path, monkeypatch, profile_id, dependencies, task)
    required = {skill.name for skill in resolution.required}
    assert expected <= required
    assert required.isdisjoint(excluded)


@pytest.mark.parametrize("phase_id,expected", [("design", True), ("handoff", False)])
def test_localized_aliases_preserve_declared_phase_boundaries(
    tmp_path, monkeypatch, phase_id, expected,
):
    resolution = _portfolio_resolution(
        tmp_path, monkeypatch, "nextjs", {"react": "19", "next": "16"},
        "화면 상태 변경, runtime i18n and Next.js authentication",
        phase_id=phase_id,
    )
    required = {skill.name for skill in resolution.required}
    assert ("react-clean-presentation-architecture" in required) is expected
    assert required.isdisjoint(_PORTFOLIO)


@pytest.mark.parametrize("task,skill,phase_id", [
    ("크래시", "android-debugging", "review"),
    ("새 모듈", "android-module-creator", "red"),
])
def test_localized_aliases_respect_excluded_code_phases(
    tmp_path, monkeypatch, task, skill, phase_id,
):
    resolution = _portfolio_resolution(
        tmp_path, monkeypatch, "android", {}, task, phase_id=phase_id,
    )
    assert skill not in {entry.name for entry in resolution.required}


@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("profile_id,name,task,phase_id,expected", [
    ("android", "android-debugging", "크래시", "review", False),
    ("android", "android-debugging", "크래시", "implement", True),
    ("android", "android-module-creator", "새 모듈", "red", False),
    ("nextjs", "react-clean-presentation-architecture", "화면 상태", "design", True),
])
def test_removed_profile_skill_preserves_phase_scope(
    tmp_path, monkeypatch, indexed, profile_id, name, task, phase_id, expected,
):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"react": "19", "next": "16"}}), encoding="utf-8",
    )
    installed = tmp_path / ".agent-flow" / "skills" / name / "SKILL.md"
    installed.parent.mkdir(parents=True)
    installed.write_text((REPO / "skills" / name / "SKILL.md").read_text(encoding="utf-8"), encoding="utf-8")
    profile = load_profile_payload(profile_id, tmp_path)
    kwargs = dict(project_root=tmp_path, phase_id=phase_id, profile=profile, task_text=task, host="codex")
    before = resolve_phase_skills(**kwargs)
    assert (name in {skill.name for skill in before.available_required}) is expected
    if indexed:
        entry = next(
            entry for entry in discover_skill_catalog(tmp_path, skill_roots(tmp_path, host="codex"))
            if entry.name == name
        )
        (installed.parent.parent / "index.json").write_text(
            json.dumps({"version": 1, "skills": [{"id": f"{name}-id", "name": name, "workflowPhases": entry.workflow_phases}]}),
            encoding="utf-8",
        )
    installed.unlink()
    after = resolve_phase_skills(**kwargs)
    assert (name in {skill.name for skill in after.missing}) is expected
    assert name not in {skill.name for skill in after.available_required}
    assert (name in local_skill_prompt_block(**kwargs)) is expected


def test_installed_phase_metadata_yields_to_live_skill_and_explicit_workflow(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    bundled = tmp_path / ".agent-flow" / "skills"
    bundled.mkdir(parents=True)
    (bundled / "index.json").write_text(
        json.dumps({"version": 1, "skills": [{"id": "scoped-check-id", "name": "scoped-check", "workflowPhases": ["design"]}]}),
        encoding="utf-8",
    )
    profile = {"skills": {"required_review": [
        {"group": "scoped", "skills": ["scoped-check"], "task_terms": ["boundary"]},
    ]}}
    kwargs = dict(project_root=tmp_path, profile=profile, task_text="boundary", host="codex")
    assert {skill.name for skill in resolve_phase_skills(phase_id="design", **kwargs).missing} == {"scoped-check"}
    assert not resolve_phase_skills(phase_id="review", **kwargs).required
    explicit = resolve_phase_skills(
        phase_id="review", phase_skills=PhaseSkills(required=("scoped-check",)), **kwargs,
    )
    assert {skill.name for skill in explicit.missing} == {"scoped-check"}
    live = tmp_path / "skills" / "scoped-check" / "SKILL.md"
    live.parent.mkdir(parents=True)
    live.write_text("---\nname: scoped-check\nworkflowPhases: [review]\ntaskTerms: [boundary]\n---\n", encoding="utf-8")
    assert {skill.name for skill in resolve_phase_skills(phase_id="review", **kwargs).available_required} == {"scoped-check"}
    assert not resolve_phase_skills(phase_id="design", **kwargs).required
