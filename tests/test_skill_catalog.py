"""외부 skill이 카탈로그에 들어오는가, 그리고 그것만으로 required가 되지는 않는가.

두 성질이 함께여야 한다. 하나만 지키면 각각 다른 사고가 된다 — 카탈로그가 닫혀 있으면
host에 깔린 skill을 영원히 못 보고(실측 953개 중 `workflowPhases` 보유 0개), 카탈로그가
열렸는데 활성화 가드가 없으면 깔린 skill 전량이 선택자 없는 엔트리로 required가 된다.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core import skill_catalog
from agent_flow.core.phase_workflow import declared_phase_skills
from agent_flow.core.profiles import load_profile_payload
from agent_flow.core.skill_resolver import (
    SkillRoot,
    discover_skill_catalog,
    resolve_phase_skills,
)

PROFILES_DIR = REPO / "src" / "agent_flow" / "profiles"


def _write_skill(directory: Path, name: str, body: str) -> Path:
    path = directory / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _upstream_skill(directory: Path, name: str, description: str) -> Path:
    """upstream이 실제로 쓰는 형태. `name` + `description`뿐이다."""
    return _write_skill(
        directory,
        name,
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
    )


def _host_root(directory: Path) -> SkillRoot:
    return SkillRoot(source="host", template=str(directory / "{skill}" / "SKILL.md"))


def _bundled_shipped_skill(project: Path, name: str) -> Path:
    """install이 kit skill을 앉히는 자리. source가 `bundled`라 스스로 선언해야 활성화된다."""
    return _write_skill(
        project / ".agent-flow" / "skills",
        name,
        (REPO / "skills" / name / "SKILL.md").read_text(encoding="utf-8"),
    )


def _required_for(
    project: Path, changed_files: list[str], *, profile: dict | None = None
) -> set[str]:
    return {
        skill.name
        for skill in resolve_phase_skills(
            project_root=project,
            phase_id="implement",
            changed_files=changed_files,
            host="claude",
            profile=profile,
        ).required
    }



def test_external_skill_without_workflow_phases_enters_catalog(tmp_path):
    """현행 `_catalog_entry`의 early return으로는 반드시 실패하는 계약."""
    host = tmp_path / "host"
    _upstream_skill(host, "edge-to-edge", "Use when insets or system bars change.")

    catalog = discover_skill_catalog(tmp_path, (_host_root(host),))

    entry = next(item for item in catalog if item.name == "edge-to-edge")
    assert entry.description == "Use when insets or system bars change."
    assert entry.phase_declared is False


def test_catalog_keeps_upstream_keywords_when_present(tmp_path):
    host = tmp_path / "host"
    _write_skill(
        host,
        "camerax",
        "---\nname: camerax\ndescription: Camera guidance.\n"
        "metadata:\n  keywords:\n  - CameraX\n  - Camera2\n---\n",
    )

    catalog = discover_skill_catalog(tmp_path, (_host_root(host),))

    entry = next(item for item in catalog if item.name == "camerax")
    assert entry.keywords == ("camerax", "camera2")


def test_host_skill_in_catalog_does_not_become_required(tmp_path, monkeypatch):
    """카탈로그를 여는 변경이 required 집합을 건드리지 않는다는 증명."""
    home = tmp_path / "home"
    _upstream_skill(home / ".claude" / "skills", "swiftui-pro", "Use when writing SwiftUI code.")
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "app"
    project.mkdir()

    resolution = resolve_phase_skills(
        project_root=project,
        phase_id="implement",
        task_text="write SwiftUI code",
        host="claude",
    )

    assert "swiftui-pro" not in {skill.name for skill in resolution.required}


def test_project_local_skill_without_declarations_still_activates(tmp_path):
    """`.agent-flow/local-skills/`는 거기 둔 것 자체가 선언이다. 가드가 이걸 깨서는 안 된다."""
    project = tmp_path / "app"
    _upstream_skill(project / ".agent-flow" / "local-skills", "house-rules", "Project rules.")

    resolution = resolve_phase_skills(
        project_root=project,
        phase_id="implement",
        task_text="anything",
        host="claude",
    )

    assert "house-rules" in {skill.name for skill in resolution.required}


def test_catalog_reflects_a_skill_edited_in_the_same_process(tmp_path):
    """캐시 키가 template뿐이면 갱신된 skill을 못 본다. 사용자가 `skills update`를 돌린 직후가 그 경로다."""
    host = tmp_path / "host"
    _upstream_skill(host, "diagnose", "Old description.")
    first = discover_skill_catalog(tmp_path, (_host_root(host),))
    _upstream_skill(host, "diagnose", "New description that is clearly longer.")

    second = discover_skill_catalog(tmp_path, (_host_root(host),))

    assert next(item for item in first if item.name == "diagnose").description == "Old description."
    assert (
        next(item for item in second if item.name == "diagnose").description
        == "New description that is clearly longer."
    )


def test_symlinked_duplicate_is_catalogued_once(tmp_path):
    """`npx skills`는 실파일을 `~/.agents/skills`에 두고 host 디렉터리에 symlink를 건다."""
    shared = tmp_path / "shared"
    real = _upstream_skill(shared, "shepherd", "Use when shepherding PRs.")
    host = tmp_path / "host"
    host.mkdir(parents=True, exist_ok=True)
    os.symlink(real.parent, host / "shepherd")

    catalog = discover_skill_catalog(
        tmp_path,
        (_host_root(host), SkillRoot(source="shared", template=str(shared / "{skill}" / "SKILL.md"))),
    )

    assert [item.name for item in catalog].count("shepherd") == 1


def test_lock_roundtrip_and_version_mismatch_is_discarded(tmp_path):
    """스키마가 바뀌면 옛 lock을 읽고 죽는 대신 버린다."""
    project = tmp_path / "app"
    host = tmp_path / "host"
    _upstream_skill(host, "diagnose", "Use when debugging.")
    result = skill_catalog.scan(project, profile=None, host="claude")
    skill_catalog.write_lock(project, result)

    assert skill_catalog.read_lock(project)["version"] == skill_catalog.LOCK_VERSION

    path = skill_catalog.lock_path(project)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = skill_catalog.LOCK_VERSION + 1
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert skill_catalog.read_lock(project) == {}


def test_doctor_names_a_declared_skill_that_is_not_installed(tmp_path, monkeypatch):
    """`camera1-to-camerax`처럼 upstream이 지운 이름이 선언에 남은 상태를 이름으로 지목한다."""
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / "app"
    project.mkdir()
    profile = {
        "skills": {
            "required_review": [
                {
                    "group": "profile",
                    "skills": ["camera1-to-camerax"],
                    "task_terms": ["camerax"],
                }
            ]
        }
    }

    result = skill_catalog.scan(project, profile=profile, host="claude")

    dead = [
        finding.name
        for finding in result.findings
        if finding.kind == skill_catalog.DEAD_DECLARATION
    ]
    assert dead == ["camera1-to-camerax"]


def test_doctor_reports_an_installed_skill_that_no_declaration_routes(tmp_path, monkeypatch):
    """디스크에 있고 선언에 없는 것 — 오늘 영구히 안 보이는 12개가 이 종류다."""
    home = tmp_path / "home"
    _upstream_skill(home / ".claude" / "skills", "wear-compose-m3", "Use for Wear OS Material3.")
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "app"
    project.mkdir()

    result = skill_catalog.scan(project, profile={}, host="claude")

    unrouted = [
        finding.name for finding in result.findings if finding.kind == skill_catalog.UNROUTED
    ]
    assert "wear-compose-m3" in unrouted


def test_lock_diff_reports_new_and_removed_names(tmp_path, monkeypatch):
    home = tmp_path / "home"
    skills = home / ".claude" / "skills"
    _upstream_skill(skills, "diagnose", "Use when debugging.")
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "app"
    project.mkdir()
    skill_catalog.write_lock(project, skill_catalog.scan(project, profile={}, host="claude"))

    _upstream_skill(skills, "zoom-out", "Use when stepping back.")
    (skills / "diagnose" / "SKILL.md").unlink()
    result = skill_catalog.scan(project, profile={}, host="claude")

    kinds = {(finding.kind, finding.name) for finding in result.findings}
    assert (skill_catalog.NEW, "zoom-out") in kinds
    assert (skill_catalog.REMOVED, "diagnose") in kinds


def test_doctor_does_not_report_a_vocabulary_routable_skill_as_unrouted(tmp_path, monkeypatch):
    """실측: 어휘를 안 보던 doctor가 정상 라우팅되는 skill 122개를 미라우팅으로 보고했다."""
    home = tmp_path / "home"
    _upstream_skill(home / ".claude" / "skills", "edge-to-edge", "Use when insets overlap.")
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "app"
    project.mkdir()
    profile = {
        "skills": {
            "external": {
                "enabled": True,
                "domains": [
                    {"id": "ui", "terms": ["insets"], "phases": ["implementation"]}
                ],
            }
        }
    }

    result = skill_catalog.scan(project, profile=profile, host="claude")

    unrouted = [
        finding.name for finding in result.findings if finding.kind == skill_catalog.UNROUTED
    ]
    assert "edge-to-edge" not in unrouted


def test_doctor_reports_a_project_skill_that_shadows_an_installed_one(tmp_path, monkeypatch):
    """카탈로그는 우선순위 root의 것 하나만 담는다. 그 그림자가 조용하면 사용자는
    자기 skill이 왜 안 쓰이는지 알 수 없다."""
    home = tmp_path / "home"
    _upstream_skill(home / ".claude" / "skills", "edge-to-edge", "Upstream copy.")
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "app"
    _upstream_skill(project / "skills", "edge-to-edge", "Project copy.")

    result = skill_catalog.scan(project, profile={}, host="claude")

    collisions = [
        finding.name for finding in result.findings if finding.kind == skill_catalog.COLLISION
    ]
    assert collisions == ["edge-to-edge"]


def test_doctor_reports_an_unrouted_bundled_skill(tmp_path, monkeypatch):
    """실측: `bundled`/`project`를 검사에서 빼 둔 탓에 profile이 설치하는 25개 중 21개가
    어느 phase에도 붙지 않는 상태로 방치됐다. 관측이 없으면 그 드리프트는 조용하다."""
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / "app"
    shipped = project / ".agent-flow" / "skills"
    _upstream_skill(shipped, "orphan-guide", "Use when nothing routes it.")
    _upstream_skill(shipped, "phase-declared-guide", "Use when a workflow phase declares it.")

    result = skill_catalog.scan(
        project,
        profile={},
        host="claude",
        workflow_skills=("phase-declared-guide",),
    )

    unrouted = [
        finding.name for finding in result.findings if finding.kind == skill_catalog.UNROUTED
    ]
    assert "orphan-guide" in unrouted
    assert "phase-declared-guide" not in unrouted


# 활성 profile을 함께 준다. profile 없이 판정하면 표(`required_review`)의 glob이
# skill frontmatter의 좁은 범위를 덮어쓰는 역전을 가드가 볼 수 없다.
_PRESENTATION_PROFILES = {
    "android-clean-presentation-architecture": "android",
    "react-clean-presentation-architecture": "nextjs",
    "react-native-clean-presentation-architecture": "react-native",
    "ios-clean-presentation-architecture": "ios",
}

_PRESENTATION_CHANGES = {
    "android-clean-presentation-architecture": "feature/chat/presentation/src/main/java/io/levvels/samantha/feature/chat/presentation/ChatViewModel.kt",
    "react-clean-presentation-architecture": "src/features/chat/presentation/ChatContainer.tsx",
    "react-native-clean-presentation-architecture": "src/features/chat/presentation/ChatScreen.tsx",
    "ios-clean-presentation-architecture": "Sources/Features/Chat/Presentation/ChatViewModel.swift",
}

_DATA_LAYER_CHANGES = {
    "android-clean-presentation-architecture": "core/data/chat/src/main/java/io/levvels/samantha/core/data/chat/ChatRepositoryImpl.kt",
    "react-clean-presentation-architecture": "src/core/data/chat/ChatRepositoryImpl.ts",
    "react-native-clean-presentation-architecture": "src/core/data/chat/ChatRepositoryImpl.ts",
    "ios-clean-presentation-architecture": "Sources/Core/Data/Chat/ChatRepositoryImpl.swift",
}


@pytest.mark.parametrize("name", sorted(_PRESENTATION_PROFILES))
def test_shipped_presentation_skills_activate_on_a_presentation_change(name, tmp_path, monkeypatch):
    """설치만 되고 활성화가 안 되면 상태 기반 UI 계약이 프롬프트에 영원히 안 들어온다.

    android만 frontmatter를 선언한 상태라 나머지 세 스택은 설치돼도 켜지지 않았다."""
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / name
    _bundled_shipped_skill(project, name)
    profile = load_profile_payload(_PRESENTATION_PROFILES[name])

    required = _required_for(project, [_PRESENTATION_CHANGES[name]], profile=profile)

    assert name in required


@pytest.mark.parametrize("name", sorted(_PRESENTATION_PROFILES))
def test_shipped_presentation_skills_stay_off_for_a_data_layer_change(name, tmp_path, monkeypatch):
    """반증: 표의 glob이 스택 파일 전체면 데이터 계층 변경에도 presentation skill이 붙는다.

    실측으로 ios 표가 `**/*.swift`와 함께 presentation skill 이름을 갖고 있었고, profile을
    안 넘기던 이 가드는 그 역전을 통과시켰다."""
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    project = tmp_path / name
    _bundled_shipped_skill(project, name)
    profile = load_profile_payload(_PRESENTATION_PROFILES[name])

    required = _required_for(project, [_DATA_LAYER_CHANGES[name]], profile=profile)

    assert name not in required


def _profile_ids() -> list[str]:
    return sorted(
        path.stem for path in PROFILES_DIR.glob("*.yaml") if not path.stem.startswith("_")
    )


def test_declared_phase_skills_degrades_on_an_unreadable_workflow(tmp_path):
    """반증: 깨진 workflow 하나에 예외가 새면 `skills doctor`가 traceback으로 죽는다.

    수집을 조용히 비우는 것도 안 된다 — 그러면 그 workflow만 선언한 skill이 미라우팅
    오탐으로 찍힌다. 읽은 것은 남기고 못 읽은 사유를 함께 돌려줘야 한다."""
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    shutil.copy(REPO / "src" / "agent_flow" / "workflows" / "default.yaml", workflows / "default.yaml")
    (workflows / "broken.yaml").write_text("phases: [\n", encoding="utf-8")

    declared = declared_phase_skills(tmp_path)

    assert "code-generation-discipline" in declared.names
    assert [error for error in declared.errors if error.startswith("workflow broken:")]


def test_every_profile_install_name_is_activation_reachable(tmp_path, monkeypatch):
    """설치와 활성화는 다른 층이다. 실측으로 install 25개 중 4개만 활성화 가능했고,
    나머지 21개는 어느 phase에도 붙지 않았다. 이 테스트가 그 드리프트의 재발 가드다.

    판정은 doctor와 같은 함수로 한다 — 여기서 도달 가능성을 다시 정의하면 두 기준이
    갈라져 통과하면서 죽어 있는 상태가 다시 만들어진다."""
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    declared = declared_phase_skills(REPO).names

    unreachable: dict[str, list[str]] = {}
    for profile_id in _profile_ids():
        payload = load_profile_payload(profile_id)
        install = (payload.get("skills") or {}).get("install") or []
        if not install:
            continue
        project = tmp_path / profile_id
        for name in install:
            _bundled_shipped_skill(project, name)

        result = skill_catalog.scan(
            project, profile=payload, host="claude", workflow_skills=declared
        )
        unrouted = {
            finding.name for finding in result.findings if finding.kind == skill_catalog.UNROUTED
        }
        blocked = sorted(set(install) & unrouted)
        if blocked:
            unreachable[profile_id] = blocked

    assert unreachable == {}


def test_selector_terms_match_on_word_boundaries():
    """부분문자열로 보면 무관한 task가 skill을 required로 올린다."""
    from agent_flow.core.skill_resolver import selector_matches

    def matched(term: str, text: str) -> bool:
        return selector_matches(
            task_terms=[term], path_globs=[], changed_files=[], task_text=text
        )

    # 옛 substring 규칙에서 실제로 True였던 것들이다.
    assert not matched("uistate", "guistatemachine을 고친다")
    assert not matched("chart", "charting 라이브러리를 붙인다")
    assert not matched("state", "restated 요구사항 정리")
    # 복수형은 허용한다. skill 설명이 term을 복수로 쓰는 쪽이 흔하다.
    assert matched("modifier", "compose modifiers 정리")
    # 한글 조사가 붙어도 경계다. `\\w`로 잡으면 한국어 task가 전부 미매치가 된다.
    assert matched("상태", "화면 상태를 정리한다")
    # 한글은 ASCII 경계 밖이라 경계가 없는 것과 같다. 그 오탐은 구절로 좁혀서 막는다.
    assert matched("프레젠테이션", "팀 프레젠테이션으로 정리한다")



SHIPPED_SKILLS = REPO / "skills"

MIGRATED_DEPENDENCIES = {
    "clean-architecture": ["clean-architecture-core"],
    "android-clean-architecture": ["clean-architecture-core"],
    "ios-clean-architecture": ["clean-architecture-core"],
    "react-clean-architecture": ["clean-architecture-core"],
    "react-native-clean-architecture": ["clean-architecture-core"],
    "python-api-clean-architecture": ["clean-architecture-core"],
    "grill-with-docs": ["domain-modeling", "grilling"],
    "tdd": ["codebase-design", "code-review"],
}


def _shipped_catalog():
    root = SkillRoot(source="project", template=str(SHIPPED_SKILLS / "{skill}" / "SKILL.md"))
    return discover_skill_catalog(REPO, (root,))


def test_alias_expands_to_clean_architecture_core():
    """반증: JS 표에만 있으면 Python 런타임은 alias 한 벌만 읽고 정본을 놓친다."""
    from agent_flow.core.skill_resolver import expand_dependencies

    expanded = expand_dependencies(["clean-architecture"], _shipped_catalog())
    assert "clean-architecture-core" in expanded


def test_shipped_skill_dependencies_are_declared_in_frontmatter():
    """반증: 매핑 8개 중 하나라도 frontmatter 밖에 남으면 두 언어가 갈린다."""
    from agent_flow.core.skill_resolver import expand_dependencies

    catalog = _shipped_catalog()
    missing = {
        name: [dep for dep in deps if dep not in expand_dependencies([name], catalog)]
        for name, deps in MIGRATED_DEPENDENCIES.items()
    }
    assert {name: gap for name, gap in missing.items() if gap} == {}


def test_skill_selection_has_no_dependency_table():
    """반증: 표가 남아 있으면 정본이 둘이라 다음 skill 추가에서 다시 갈린다."""
    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted((REPO / "lib").glob("*.mjs")) + sorted((REPO / "bin").glob("*.mjs"))
    }
    holders = [name for name, text in sources.items() if "SKILL_DEPENDENCIES" in text]
    assert holders == [], f"SKILL_DEPENDENCIES가 아직 남아 있다: {holders}"




def test_profile_declared_skills_ignore_the_external_budget(tmp_path: Path) -> None:
    """반증: 예산이 정본 규범까지 떨구면 read gate에서 핵심 skill이 빠진다."""
    from agent_flow.core.skill_resolver import expand_dependencies

    catalog = _shipped_catalog()
    expanded = expand_dependencies(["clean-architecture", "tdd"], catalog)
    assert "clean-architecture-core" in expanded
    assert "codebase-design" in expanded



MANAGED_ADAPTER_BODY = "// agent-flow: managed omp extension\nexport default function agentFlowHooks(pi) {}\n"


def _global_adapter(home: Path, body: str = MANAGED_ADAPTER_BODY) -> Path:
    path = home / ".omp" / "agent" / "extensions" / "agent-flow-hooks.ts"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_doctor_reports_unowned_global_omp_adapter(tmp_path: Path) -> None:
    """반증: 못 보면 오늘처럼 kit이 만들지도 관리하지도 않는 adapter가 모든 tool을 막고도
    재설치로 낫지 않는다."""
    project = tmp_path / "project"
    (project / ".agent-flow" / "skills").mkdir(parents=True)
    home = tmp_path / "home"
    adapter = _global_adapter(home)

    result = skill_catalog.scan(project, home=home)

    findings = [item for item in result.findings if item.kind == skill_catalog.UNOWNED_ADAPTER]
    assert len(findings) == 1, [item.kind for item in result.findings]
    assert str(adapter) in findings[0].name + findings[0].detail


def test_doctor_ignores_a_home_without_a_global_adapter(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".agent-flow" / "skills").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()

    result = skill_catalog.scan(project, home=home)

    assert [item for item in result.findings if item.kind == skill_catalog.UNOWNED_ADAPTER] == []


def test_doctor_ignores_a_foreign_file_without_the_kit_marker(tmp_path: Path) -> None:
    """반증: marker 없이 보고하면 남의 확장을 kit 산출물로 지목한다."""
    project = tmp_path / "project"
    (project / ".agent-flow" / "skills").mkdir(parents=True)
    home = tmp_path / "home"
    _global_adapter(home, "export default function somebodyElse() {}\n")

    result = skill_catalog.scan(project, home=home)

    assert [item for item in result.findings if item.kind == skill_catalog.UNOWNED_ADAPTER] == []


def test_doctor_adapter_scan_does_not_touch_the_file(tmp_path: Path) -> None:
    """반증: 진단이 파일을 고치면 소유를 증명하지 못한 것을 훼손한다."""
    project = tmp_path / "project"
    (project / ".agent-flow" / "skills").mkdir(parents=True)
    home = tmp_path / "home"
    adapter = _global_adapter(home)
    before = (adapter.read_bytes(), adapter.stat().st_mtime_ns)

    skill_catalog.scan(project, home=home)

    assert (adapter.read_bytes(), adapter.stat().st_mtime_ns) == before


def test_doctor_ignores_a_symlinked_global_adapter(tmp_path: Path) -> None:
    """반증: 링크를 따라가면 이 자리에 없는 남의 파일로 소유권을 판정한다."""
    project = tmp_path / "project"
    (project / ".agent-flow" / "skills").mkdir(parents=True)
    home = tmp_path / "home"
    elsewhere = tmp_path / "elsewhere.ts"
    elsewhere.write_text(MANAGED_ADAPTER_BODY, encoding="utf-8")
    link = home / ".omp" / "agent" / "extensions" / "agent-flow-hooks.ts"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(elsewhere)

    result = skill_catalog.scan(project, home=home)

    assert [item for item in result.findings if item.kind == skill_catalog.UNOWNED_ADAPTER] == []


def test_crlf_skill_enters_the_catalog_with_metadata(tmp_path: Path) -> None:
    """반증: Python이 CRLF frontmatter를 못 읽으면 그 skill은 선언 없는 엔트리로 떨어진다."""
    host = tmp_path / "host"
    _write_skill(
        host,
        "crlf-skill",
        "---\r\nname: crlf-skill\r\ndescription: Compose guidance. Second sentence.\r\n"
        "requires:\r\n  - other-skill\r\n---\r\n\r\n# crlf-skill\r\n",
    )
    catalog = discover_skill_catalog(tmp_path, (_host_root(host),))
    entry = next(item for item in catalog if item.name == "crlf-skill")
    assert entry.description == "Compose guidance. Second sentence."
    assert entry.dependencies == ("other-skill",)


def test_crlf_skill_summary_matches_the_shared_rule(tmp_path: Path) -> None:
    from agent_flow.core.skill_resolver import skill_summary

    host = tmp_path / "host"
    path = _write_skill(
        host,
        "crlf-summary",
        "---\r\nname: crlf-summary\r\ndescription: First sentence. Second sentence.\r\n---\r\n",
    )
    assert skill_summary(path) == "First sentence."
