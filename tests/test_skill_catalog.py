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

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core import skill_catalog
from agent_flow.core.skill_resolver import (
    SkillRoot,
    discover_skill_catalog,
    resolve_phase_skills,
)


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


def _bundled_shipped_skill(project: Path, name: str) -> None:
    """install이 kit skill을 앉히는 자리. source가 `bundled`라 스스로 선언해야 활성화된다."""
    destination = project / ".agent-flow" / "skills" / name
    destination.mkdir(parents=True)
    shutil.copyfile(REPO / "skills" / name / "SKILL.md", destination / "SKILL.md")


def _required_for(project: Path, changed_files: list[str]) -> set[str]:
    return {
        skill.name
        for skill in resolve_phase_skills(
            project_root=project,
            phase_id="implement",
            changed_files=changed_files,
            host="claude",
        ).required
    }


def test_shipped_presentation_skill_activates_on_a_presentation_change(tmp_path):
    """설치만 되고 활성화가 안 되면 UDF·use case 규칙이 프롬프트에 영원히 안 들어온다."""
    project = tmp_path / "app"
    _bundled_shipped_skill(project, "android-clean-presentation-architecture")

    required = _required_for(
        project,
        ["feature/chat/presentation/src/main/java/io/levvels/samantha/feature/chat/presentation/ChatViewModel.kt"],
    )

    assert "android-clean-presentation-architecture" in required


def test_shipped_presentation_skill_stays_off_for_a_data_layer_change(tmp_path):
    project = tmp_path / "app"
    _bundled_shipped_skill(project, "android-clean-presentation-architecture")

    required = _required_for(
        project,
        ["core/data/chat/src/main/java/io/levvels/samantha/core/data/chat/ChatRepositoryImpl.kt"],
    )

    assert "android-clean-presentation-architecture" not in required
