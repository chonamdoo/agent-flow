from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


KIT_ROOT = Path(__file__).resolve().parent.parent


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    return node


def _install(
    project: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install", *args),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def _skill(path: Path, body: str, *, hosts: str | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    host_line = f"hosts: {hosts}\n" if hosts is not None else ""
    (path / "SKILL.md").write_text(
        "---\n"
        f"name: {path.name}\n"
        "description: Use when testing custom skills.\n"
        f"{host_line}"
        "tags: [test]\n"
        "---\n"
        f"Use when testing custom skills.\n\n{body}\n",
        encoding="utf-8",
    )


def test_project_skill_links_all_hosts_and_index_omits_body(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _skill(project / "skills" / "my-skill", "BODY SHOULD NOT BE IN INDEX")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    host_roots = {
        "claude": project / ".claude" / "skills",
        "codex": project / ".Codex" / "skills",
        "omp": project / ".omp" / "skills",
    }
    for host_root in host_roots.values():
        assert (host_root / "my-skill" / "SKILL.md").exists()
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    selected = next(skill for skill in index["skills"] if skill["name"] == "my-skill")
    assert selected["source"] == "project"
    assert set(selected["hosts"]) == {"claude", "codex", "omp"}
    assert "BODY SHOULD NOT BE IN INDEX" not in json.dumps(index)


def test_bundled_workflow_skills_are_internal_and_host_skills_are_registered(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = _install(project)

    assert result.returncode == 0, result.stderr
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    host_skills = {
        "agent-flow",
        "android-appshell-error-handling",
        "comment-authoring-discipline",
        "comment-checker",
        "ios-app-shell-error-handling",
        "react-app-shell-error-handling",
        "react-native-app-shell-error-handling",
    }
    indexed = {skill["name"] for skill in index["skills"]}
    matt_skill_closure = {
        "code-review",
        "codebase-design",
        "domain-modeling",
        "grill-with-docs",
        "grilling",
        "tdd",
        "to-prd",
    }
    # bundled skill은 전부 index에 노출되어야 agent가 발견할 수 있다.
    assert host_skills <= indexed
    assert {
        "full-feature-workflow",
        "architecture-reviewer",
        "push-watch",
        "clean-architecture-core",
        "android-clean-architecture",
        "ios-clean-architecture",
        "react-clean-architecture",
        "react-native-clean-architecture",
        "python-api-clean-architecture",
    } <= indexed
    assert matt_skill_closure <= indexed
    # host 디렉토리 link는 host skill 7종으로 제한한다.
    assert {link["name"] for link in index["links"]} == host_skills
    assert (project / ".agent-flow" / "skills" / "domain-modeling" / "SKILL.md").exists()
    assert (project / ".agent-flow" / "skills" / "full-feature-workflow" / "SKILL.md").exists()
    for host_dir in (".Codex", ".claude"):
        for skill in matt_skill_closure:
            assert not (project / host_dir / "skills" / skill).exists()
    assert not (project / ".Codex" / "skills" / "full-feature-workflow").exists()


def test_clean_architecture_skills_install_core_and_platform_dependency_graph(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = _install(project)

    assert result.returncode == 0, result.stderr
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    skills = {skill["name"]: skill for skill in index["skills"]}
    platform_skills = {
        "android-clean-architecture",
        "ios-clean-architecture",
        "react-clean-architecture",
        "react-native-clean-architecture",
        "python-api-clean-architecture",
    }

    assert "clean-architecture-core" in skills
    assert "clean-architecture" in skills
    assert platform_skills <= set(skills)
    assert skills["clean-architecture"]["requires"] == ["clean-architecture-core"]
    for name in platform_skills:
        assert skills[name]["requires"] == ["clean-architecture-core"]
    assert not any("missing required skill" in warning for warning in index["warnings"])

    core = (
        project / ".agent-flow" / "skills" / "clean-architecture-core" / "SKILL.md"
    ).read_text(encoding="utf-8")
    android = (
        project / ".agent-flow" / "skills" / "android-clean-architecture" / "SKILL.md"
    ).read_text(encoding="utf-8")
    alias = (
        project / ".agent-flow" / "skills" / "clean-architecture" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "repository-impl-direct-api-service: pass|fail" in core
    assert "HomeRepositoryImpl -> HomeRemoteDataSource -> HomeApiService" in android
    assert "Compatibility Alias" in alias
    assert "Samantha" not in core + android + alias
    assert "http://" not in core + android + alias
    assert "https://" not in core + android + alias


def test_android_profile_installs_android_skills_and_common_dependencies_only(tmp_path: Path) -> None:
    project = tmp_path / "android-project"
    project.mkdir()
    (project / "settings.gradle.kts").write_text("pluginManagement {}\n", encoding="utf-8")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    names = {skill["name"] for skill in index["skills"]}
    matt_skill_closure = {
        "code-review",
        "codebase-design",
        "domain-modeling",
        "grill-with-docs",
        "grilling",
        "tdd",
        "to-prd",
    }
    assert index["selection"]["profiles"] == ["android"]
    assert "clean-architecture-core" in names
    assert matt_skill_closure <= names
    assert "android-clean-architecture" in names
    assert "android-code-review" in names
    # 설치되지 않으면 카탈로그에 안 올라가고, frontmatter가 무슨 선언을 하든
    # 자동 활성화가 통째로 죽는다. 기본 install로 닿아야 한다.
    assert "android-sdui-architecture" in names
    assert "react-native-clean-architecture" not in names
    assert "ios-clean-architecture" not in names
    assert not (project / ".agent-flow" / "skills" / "react-native-clean-architecture").exists()


def test_multi_profile_install_uses_union_and_dependency_closure(tmp_path: Path) -> None:
    project = tmp_path / "mixed-project"
    project.mkdir()

    result = _install(project, "--profile", "android", "--profile", "react-native")

    assert result.returncode == 0, result.stderr
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    names = {skill["name"] for skill in index["skills"]}
    assert index["selection"]["profiles"] == ["android", "react-native"]
    assert "clean-architecture-core" in names
    assert "android-clean-architecture" in names
    assert "react-native-clean-architecture" in names
    assert "ios-clean-architecture" not in names


def test_reinstall_preserves_previously_selected_profile_skills(tmp_path: Path) -> None:
    project = tmp_path / "mixed-project"
    project.mkdir()

    first = _install(project, "--profile", "android", "--profile", "react-native")
    assert first.returncode == 0, first.stderr
    second = _install(project, "--profile", "android")
    assert second.returncode == 0, second.stderr

    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    names = {skill["name"] for skill in index["skills"]}
    assert index["selection"]["profiles"] == ["android", "react-native"]
    assert "android-clean-architecture" in names
    assert "react-native-clean-architecture" in names


def test_plain_reinstall_preserves_filtered_profile_selection(tmp_path: Path) -> None:
    project = tmp_path / "android-project"
    project.mkdir()

    first = _install(project, "--profile", "android")
    assert first.returncode == 0, first.stderr
    second = _install(project)
    assert second.returncode == 0, second.stderr

    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    names = {skill["name"] for skill in index["skills"]}
    assert index["selection"]["mode"] == "filtered"
    assert index["selection"]["profiles"] == ["android"]
    assert "android-clean-architecture" in names
    assert "react-native-clean-architecture" not in names
    assert "ios-clean-architecture" not in names


def test_plain_reinstall_preserves_filtered_selection_over_detected_profile(tmp_path: Path) -> None:
    project = tmp_path / "rn-project"
    project.mkdir()
    (project / "package.json").write_text('{"dependencies":{"react-native":"latest"}}\n', encoding="utf-8")
    (project / "settings.gradle.kts").write_text("pluginManagement {}\n", encoding="utf-8")

    first = _install(project, "--profile", "android")
    assert first.returncode == 0, first.stderr
    second = _install(project)
    assert second.returncode == 0, second.stderr

    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    names = {skill["name"] for skill in index["skills"]}
    assert index["selection"]["mode"] == "filtered"
    assert index["selection"]["profiles"] == ["android"]
    assert "android-clean-architecture" in names
    assert "react-native-clean-architecture" not in names


def test_filtered_reinstall_after_all_install_does_not_preserve_unselected_platforms(tmp_path: Path) -> None:
    project = tmp_path / "android-project"
    project.mkdir()

    first = _install(project)
    assert first.returncode == 0, first.stderr
    second = _install(project, "--profile", "android")
    assert second.returncode == 0, second.stderr

    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    names = {skill["name"] for skill in index["skills"]}
    assert index["selection"]["profiles"] == ["android"]
    assert "android-clean-architecture" in names
    assert "react-native-clean-architecture" not in names
    assert "ios-clean-architecture" not in names
    assert not (project / ".agent-flow" / "skills" / "react-native-clean-architecture").exists()
    assert not (project / ".agent-flow" / "skills" / "ios-clean-architecture").exists()


def test_ios_project_auto_selects_ios_profile_skills(tmp_path: Path) -> None:
    project = tmp_path / "ios-project"
    project.mkdir()
    (project / "Package.swift").write_text("// swift-tools-version: 5.9\n", encoding="utf-8")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    names = {skill["name"] for skill in index["skills"]}
    assert index["selection"]["profiles"] == ["ios"]
    assert "ios-clean-architecture" in names
    assert "ios-clean-presentation-architecture" in names
    assert "android-code-review" not in names
    assert "react-native-clean-architecture" not in names


def test_react_native_project_with_gradle_auto_selects_react_native_profile(tmp_path: Path) -> None:
    project = tmp_path / "rn-project"
    project.mkdir()
    (project / "package.json").write_text('{"dependencies":{"react-native":"latest"}}\n', encoding="utf-8")
    (project / "settings.gradle.kts").write_text("", encoding="utf-8")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    names = {skill["name"] for skill in index["skills"]}
    assert index["selection"]["profiles"] == ["react-native"]
    assert "react-native-clean-architecture" in names
    assert "android-code-review" not in names


def test_skill_metadata_dependencies_are_indexed_and_auto_installed(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    dependency = project / "skills" / "dependency-skill"
    dependency.mkdir(parents=True)
    (dependency / "SKILL.md").write_text(
        "---\n"
        "name: dependency-skill\n"
        "title: Dependency Skill\n"
        "description: Use when testing dependencies.\n"
        "---\n"
        "Use when testing dependencies.\n",
        encoding="utf-8",
    )
    consumer = project / "skills" / "consumer-skill"
    consumer.mkdir(parents=True)
    (consumer / "SKILL.md").write_text(
        "---\n"
        "id: consumer-skill-id\n"
        "name: consumer-skill\n"
        "title: Consumer Skill\n"
        "description: Use when testing dependency closure.\n"
        "dependencies: [dependency-skill]\n"
        "---\n"
        "Use when testing dependency closure.\n",
        encoding="utf-8",
    )

    result = _install(project, "--skills", "consumer-skill")

    assert result.returncode == 0, result.stderr
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    skills = {skill["name"]: skill for skill in index["skills"]}
    assert {"consumer-skill", "dependency-skill"} <= set(skills)
    assert skills["consumer-skill"]["id"] == "consumer-skill-id"
    assert skills["consumer-skill"]["title"] == "Consumer Skill"
    assert skills["consumer-skill"]["dependencies"] == ["dependency-skill"]
    assert skills["consumer-skill"]["requires"] == ["dependency-skill"]
    assert (project / ".Codex" / "skills" / "dependency-skill" / "SKILL.md").exists()
    assert (project / ".claude" / "skills" / "dependency-skill" / "SKILL.md").exists()


def test_local_skill_priority_beats_project_and_bundled_conflict_is_recorded(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _skill(project / "skills" / "agent-flow", "PROJECT")
    _skill(project / ".agent-flow" / "local-skills" / "agent-flow", "LOCAL")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    selected = next(skill for skill in index["skills"] if skill["name"] == "agent-flow")
    assert selected["source"] == "local"
    conflict = next(conflict for conflict in index["conflicts"] if conflict["name"] == "agent-flow")
    assert conflict["selected"] == ".agent-flow/local-skills/agent-flow/SKILL.md"
    assert "skills/agent-flow/SKILL.md" in conflict["ignored"]


def test_host_limited_skill_links_only_requested_host(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _skill(project / "skills" / "codex-only", "CODEX", hosts="[codex]")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert (project / ".Codex" / "skills" / "codex-only" / "SKILL.md").exists()
    assert not (project / ".claude" / "skills" / "codex-only").exists()
    assert not (project / ".omp" / "skills" / "codex-only").exists()


def test_host_limited_skill_links_only_omp(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _skill(project / "skills" / "omp-only", "OMP", hosts="[omp]")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert (project / ".omp" / "skills" / "omp-only" / "SKILL.md").exists()
    assert not (project / ".Codex" / "skills" / "omp-only").exists()
    assert not (project / ".claude" / "skills" / "omp-only").exists()



def test_host_limited_skill_accepts_yaml_block_list(tmp_path: Path) -> None:
    project = tmp_path / "project"
    skill_dir = project / "skills" / "codex-block-list"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: codex-block-list\n"
        "description: Use when testing custom skills.\n"
        "hosts:\n"
        "  - codex\n"
        "tags:\n"
        "  - test\n"
        "---\n"
        "Use when testing custom skills.\n",
        encoding="utf-8",
    )

    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert (project / ".Codex" / "skills" / "codex-block-list" / "SKILL.md").exists()
    assert not (project / ".claude" / "skills" / "codex-block-list").exists()
    assert not (project / ".omp" / "skills" / "codex-block-list").exists()


def test_existing_user_modified_skill_is_not_overwritten(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _skill(project / "skills" / "my-skill", "PROJECT")
    dest = project / ".Codex" / "skills" / "my-skill"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("user modified\n", encoding="utf-8")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "user modified\n"
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    assert any(link["status"] == "skipped-user-modified" for link in index["links"])


def test_skill_hash_updates_and_local_skills_are_gitignored(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / "skills" / "my-skill"
    _skill(skill_dir, "v1")
    assert _install(project).returncode == 0
    index1 = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    hash1 = next(skill["hash"] for skill in index1["skills"] if skill["name"] == "my-skill")

    _skill(skill_dir, "v2")
    assert _install(project).returncode == 0
    index2 = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    hash2 = next(skill["hash"] for skill in index2["skills"] if skill["name"] == "my-skill")

    assert hash1 != hash2
    gitignore = (project / ".gitignore").read_text(encoding="utf-8")
    assert ".agent-flow/" in gitignore or ".agent-flow/local-skills/" in gitignore


def test_skill_frontmatter_name_cannot_escape_host_skill_directory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / "skills" / "safe-folder"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: ../../../outside/pwn\n"
        "description: Use when testing unsafe names.\n"
        "---\n"
        "Use when testing unsafe names.\n",
        encoding="utf-8",
    )

    result = _install(project)

    assert result.returncode == 0, result.stderr
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    assert any("unsafe skill name ignored" in warning for warning in index["warnings"])
    assert not (tmp_path / "outside").exists()


def test_skill_frontmatter_dotdot_name_is_sanitized_without_install_failure(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / "skills" / "safe-folder"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: ..\n"
        "description: Use when testing unsafe names.\n"
        "---\n"
        "Use when testing unsafe names.\n",
        encoding="utf-8",
    )

    result = _install(project)

    assert result.returncode == 0, result.stderr
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    assert all(skill["name"] != ".." for skill in index["skills"])


def test_stale_host_skill_link_removed_when_hosts_change(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / "skills" / "demo"
    _skill(skill_dir, "CODEX", hosts="[codex]")
    assert _install(project).returncode == 0
    assert (project / ".Codex" / "skills" / "demo" / "SKILL.md").exists()

    _skill(skill_dir, "CLAUDE", hosts="[claude]")
    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert (project / ".claude" / "skills" / "demo" / "SKILL.md").exists()
    assert not (project / ".Codex" / "skills" / "demo").exists()


def test_stale_broken_host_skill_symlink_removed_when_skill_deleted(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / "skills" / "demo"
    _skill(skill_dir, "CODEX", hosts="[codex]")
    assert _install(project).returncode == 0
    codex_link = project / ".Codex" / "skills" / "demo"
    assert codex_link.exists() or codex_link.is_symlink()

    (skill_dir / "SKILL.md").unlink()
    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert not codex_link.is_symlink()


def test_stale_copied_host_skill_dir_removed_when_skill_deleted(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / "skills" / "demo"
    _skill(skill_dir, "CODEX", hosts="[codex]")
    assert _install(project).returncode == 0
    codex_link = project / ".Codex" / "skills" / "demo"
    if codex_link.is_symlink():
        codex_link.unlink()
        codex_link.mkdir(parents=True)
        (codex_link / "SKILL.md").write_text((skill_dir / "SKILL.md").read_text(encoding="utf-8"), encoding="utf-8")

    (skill_dir / "SKILL.md").unlink()
    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert not codex_link.exists()


def test_host_skill_root_symlink_is_skipped_not_written_outside_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / ".Codex").symlink_to(outside, target_is_directory=True)
    _skill(project / "skills" / "demo", "CODEX", hosts="[codex]")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert not (outside / "skills" / "demo").exists()
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    assert any(link["status"] == "skipped-host-root-symlink" for link in index["links"])


def test_android_upstream_skills_are_not_installed_or_vendored(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "settings.gradle").write_text("pluginManagement { repositories { google() } }\n", encoding="utf-8")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert not (project / ".agent-flow" / "vendor" / "android-skills").exists()
    assert not (project / ".agent-flow" / "vendor" / "chrisbanes-skills").exists()
    assert not (project / ".Codex" / "skills" / "edge-to-edge").exists()
    assert not (project / ".claude" / "skills" / "edge-to-edge").exists()
    assert not (project / ".omp" / "skills" / "edge-to-edge").exists()
    assert not (project / ".agents" / "skills" / "edge-to-edge").exists()

    kit = json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
    assert "android_skills" not in kit
    assert "chrisbanes_skills" not in kit
    bootstrap = (project / ".agent-flow" / "bootstrap" / "AGENTS.md").read_text(encoding="utf-8")
    assert "missing local <group>: <skill>" in bootstrap
    android_profile = (project / ".agent-flow" / "profiles" / "android.yaml").read_text(encoding="utf-8")
    assert "url: https://github.com/skydoves/compose-performance-skills" in android_profile
    assert "kind: host-managed" in android_profile


def _installed_profile_yaml(project: Path, *, runtime: bool = False) -> set[str]:
    base = project / ".agent-flow"
    directory = (
        base / "runtime" / "python" / "agent_flow" / "profiles" if runtime else base / "profiles"
    )
    return {entry.name for entry in directory.iterdir() if entry.suffix == ".yaml"}


def _bundled_profile_yaml() -> set[str]:
    return {entry.name for entry in (KIT_ROOT / "src" / "agent_flow" / "profiles").glob("*.yaml")}


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_install_scopes_profiles_to_the_detected_stack(tmp_path: Path, binary: str) -> None:
    # 두 진입점이 keep-set을 각자 구하므로 둘 다 태운다. 갈라지면 install.mjs로 깐
    # 프로젝트만 profile이 전부 남는다.
    project = tmp_path / f"scoped-profiles-{binary}"
    project.mkdir()
    (project / "settings.gradle").write_text("pluginManagement { repositories { google() } }\n", encoding="utf-8")

    result = _install_with(binary, project)

    assert result.returncode == 0, result.stderr
    assert _installed_profile_yaml(project) == {"_schema.yaml", "generic.yaml", "android.yaml"}


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_install_keeps_the_full_runtime_profile_catalog(tmp_path: Path, binary: str) -> None:
    """반증: runtime 사본이 profile YAML의 실제 read path이자 override의 자원이다.

    여기를 좁히면 `gates --profile ios`가 `unknown profile: ios`로 죽고,
    `AGENT_FLOW_PROFILE=ios`는 경고만 내고 `generic`으로 조용히 내려앉는다.
    """
    project = tmp_path / f"runtime-catalog-{binary}"
    project.mkdir()
    (project / "settings.gradle").write_text("pluginManagement { repositories { google() } }\n", encoding="utf-8")

    result = _install_with(binary, project)

    assert result.returncode == 0, result.stderr
    assert _installed_profile_yaml(project, runtime=True) == _bundled_profile_yaml()


def test_installed_runtime_loads_a_profile_the_project_did_not_select(tmp_path: Path) -> None:
    """`--profile` / `AGENT_FLOW_PROFILE` override가 설치 후에도 살아 있어야 한다."""
    project = tmp_path / "override-project"
    project.mkdir()
    (project / "settings.gradle").write_text("pluginManagement { repositories { google() } }\n", encoding="utf-8")
    assert _install(project).returncode == 0

    runtime = project / ".agent-flow" / "runtime" / "python"
    probe = (
        "from pathlib import Path\n"
        "from agent_flow.core.profiles import active_profile_ids, load_profile\n"
        f"ids = active_profile_ids(Path({str(project)!r}), 'ios')\n"
        "print(ids[0], load_profile(ids[0]).profile_id)\n"
    )
    proc = subprocess.run(
        (os.environ.get("PYTHON", "python3"), "-c", probe),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(runtime)},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ios ios"


def test_react_native_carries_its_android_vocabulary_without_the_android_profile(
    tmp_path: Path,
) -> None:
    """react-native의 `android/**` 변경은 자기 어휘로 Android skill을 잡는다.

    옛 `android-native-escalation`은 android profile의 표를 지명해서 그 표의 주인
    파일까지 깔아야 했다. 표가 사라진 지금 남의 stack 정의를 끼워 넣으면 "이
    프로젝트에 걸리는 profile"이라는 구분이 다시 흐려진다 - 그래서 목록에서 빼고,
    빠져도 잃는 것이 없다는 근거를 같은 테스트가 함께 못 박는다.
    """
    project = tmp_path / "rn-project"
    project.mkdir()
    (project / "package.json").write_text(
        '{"name": "x", "dependencies": {"react-native": "0.74.0"}}\n', encoding="utf-8"
    )

    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert _installed_profile_yaml(project) == {
        "_schema.yaml",
        "generic.yaml",
        "react-native.yaml",
    }
    installed = yaml.safe_load(
        (project / ".agent-flow" / "profiles" / "react-native.yaml").read_text(encoding="utf-8")
    )
    domains = {domain["id"]: domain for domain in installed["skills"]["external"]["domains"]}
    assert domains["android-native"]["path_globs"] == ["android/**"]


def test_install_keeps_the_detected_profile_when_another_is_requested(tmp_path: Path) -> None:
    """worktree에는 kit.json이 없어 런타임이 감지 profile로 되돌아간다.

    그래서 감지 id는 선택하지 않아도 이 프로젝트에 걸린다.
    """
    project = tmp_path / "gradle-as-ios"
    project.mkdir()
    (project / "settings.gradle").write_text("pluginManagement { repositories { google() } }\n", encoding="utf-8")

    result = _install(project, "--profile", "ios")

    assert result.returncode == 0, result.stderr
    kit = json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
    assert kit["profile"] == "android"
    assert kit["profiles"] == ["ios"]
    assert _installed_profile_yaml(project) == {
        "_schema.yaml",
        "generic.yaml",
        "ios.yaml",
        "android.yaml",
    }


def test_install_scopes_profiles_to_requested_profiles(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "settings.gradle").write_text("pluginManagement { repositories { google() } }\n", encoding="utf-8")

    result = _install(project, "--profile", "android,ios")

    assert result.returncode == 0, result.stderr
    assert _installed_profile_yaml(project) == {
        "_schema.yaml",
        "generic.yaml",
        "android.yaml",
        "ios.yaml",
    }


def test_reinstall_prunes_foreign_profiles_and_keeps_custom_ones(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "settings.gradle").write_text("pluginManagement { repositories { google() } }\n", encoding="utf-8")
    assert _install(project).returncode == 0

    # 예전 설치본은 배포되는 profile을 전부 받았다. 업그레이드가 그것을 걷어내야 한다.
    bundled = KIT_ROOT / "src" / "agent_flow" / "profiles"
    installed_dir = project / ".agent-flow" / "profiles"
    for source in bundled.glob("*.yaml"):
        shutil.copyfile(source, installed_dir / source.name)
    (installed_dir / "my-stack.yaml").write_text("id: my-stack\ngates: []\n", encoding="utf-8")
    edited = installed_dir / "nextjs.yaml"
    edited.write_text(edited.read_text(encoding="utf-8") + "# local tweak\n", encoding="utf-8")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert _installed_profile_yaml(project) == {
        "_schema.yaml",
        "generic.yaml",
        "android.yaml",
        # 사용자가 만든 profile은 배포 이름이 아니므로 살아남는다.
        "my-stack.yaml",
    }
    # 손댄 흔적이 있는 파일은 지우기 전에 사본을 남기고 알린다.
    assert (installed_dir / "nextjs.yaml.removed").exists()
    assert "pruned: .agent-flow/profiles/nextjs.yaml" in result.stdout


def test_reinstall_is_quiet_when_pruning_loses_nothing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "settings.gradle").write_text("pluginManagement { repositories { google() } }\n", encoding="utf-8")
    assert _install(project).returncode == 0

    # 손대지 않은 kit 사본을 지우는 것은 잃는 것이 없다. 그때도 알리면 "정리했다"는
    # 사실만 매 재설치마다 stack 수만큼 되풀이된다.
    bundled = KIT_ROOT / "src" / "agent_flow" / "profiles"
    installed_dir = project / ".agent-flow" / "profiles"
    for source in bundled.glob("*.yaml"):
        shutil.copyfile(source, installed_dir / source.name)

    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert "pruned: " not in result.stdout


def test_no_profile_enumerates_external_skill_names() -> None:
    """upstream이 6개월에 이름 35%를 바꿨다. 이름을 적으면 우리 파일이 항상 낡는다."""
    profiles = sorted((KIT_ROOT / "src" / "agent_flow" / "profiles").glob("*.yaml"))
    assert profiles

    for path in profiles:
        text = path.read_text(encoding="utf-8")
        assert "android_skills:" not in text, path
        assert "chrisbanes_skills:" not in text, path
        assert "skills_from:" not in text, path
        assert ".agent-flow/vendor" not in text, path


def test_external_sources_declare_host_roots_without_installing() -> None:
    """설치는 사용자 소유다. 우리는 경로만 해석하고 fetch는 관리자 없는 소스에만 쓴다."""
    android = yaml.safe_load(
        (KIT_ROOT / "src" / "agent_flow" / "profiles" / "android.yaml").read_text(encoding="utf-8")
    )
    sources = {source["id"]: source for source in android["skill_sources"]}

    host_managed = sources["android-official"]
    assert host_managed["kind"] == "host-managed"
    assert "~/.claude/skills/{skill}/SKILL.md" in host_managed["roots"]
    assert "~/.codex/skills/{skill}/SKILL.md" in host_managed["roots"]
    assert sources["skydoves-compose-performance"]["kind"] == "fetch"


def test_bootstrap_and_kit_keep_the_missing_skill_wording() -> None:
    """부재를 알리는 문구는 계약이다. 사라지면 사용자가 무엇을 깔아야 하는지 알 수 없다."""
    kit_text = (KIT_ROOT / "bin" / "agent-flow-kit.mjs").read_text(encoding="utf-8")
    review_text = (KIT_ROOT / "skills" / "android-code-review" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "missing local <group>: <skill>" in kit_text
    assert "missing local <group>: <skill>" in review_text


def test_sdui_skill_is_android_only(tmp_path: Path) -> None:
    """반증: SDUI는 Android 전용이다. 다른 profile까지 따라가면 안 된다."""
    project = tmp_path / "python-project"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0"\n', encoding="utf-8")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    names = {skill["name"] for skill in index["skills"]}
    assert index["selection"]["profiles"] == ["python"]
    assert "android-sdui-architecture" not in names


def test_profile_yaml_install_list_matches_fallback_map(tmp_path: Path) -> None:
    """불변: profile YAML의 install 목록과 JS fallback 맵이 갈라지지 않는다.

    실제 설치를 정하는 것은 YAML이고 맵은 YAML이 없을 때의 대비책이다. 둘이
    어긋나면 어느 쪽을 고쳐도 절반만 반영된다.
    """
    import re

    kit = Path(__file__).resolve().parents[1]
    yaml_text = (kit / "src" / "agent_flow" / "profiles" / "android.yaml").read_text(encoding="utf-8")
    block = yaml_text.split("\nskills:\n", 1)[1].split("\n  required_review:", 1)[0]
    from_yaml = sorted(re.findall(r"^\s+- ([A-Za-z0-9._-]+)$", block, re.M))

    js = (kit / "lib" / "skill-selection.mjs").read_text(encoding="utf-8")
    android_block = js.split('["android", [', 1)[1].split("]],", 1)[0]
    from_js = sorted(re.findall(r'"([A-Za-z0-9._-]+)"', android_block))

    assert from_yaml == from_js, f"yaml={from_yaml} js={from_js}"


def _hook_state(project: Path) -> dict:
    import json as _json

    names = set()
    for rel in (".claude/settings.json", ".Codex/hooks.json", ".codex/hooks.json"):
        path = project / rel
        if not path.is_file():
            continue
        payload = _json.loads(path.read_text(encoding="utf-8"))
        for entries in (payload.get("hooks") or {}).values():
            for entry in entries:
                for hook in entry.get("hooks") or []:
                    command = hook.get("command") or ""
                    if command:
                        names.add(command.split("/")[-1].strip("'\""))
    hooks_dir = project / ".agent-flow" / "scripts" / "hooks"
    scripts = (
        {p.name for p in hooks_dir.iterdir() if p.suffix in {".sh", ".py"}}
        if hooks_dir.is_dir()
        else set()
    )
    kit = _json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
    return {
        "registered": names,
        "scripts": scripts,
        "omp": (project / ".omp" / "extensions" / "agent-flow-hooks.ts").exists(),
        "flag": kit.get("hooks"),
    }


def _install_with(
    binary: str,
    project: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / binary), "install", *args),
        cwd=project, text=True, capture_output=True, check=False, env=env,
    )


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_installer_packages_review_angles_with_the_python_runtime(
    tmp_path: Path, binary: str
) -> None:
    project = tmp_path / f"runtime-review-prompts-{binary}"
    project.mkdir()

    result = _install_with(binary, project)

    assert result.returncode == 0, result.stderr
    installed_review_root = (
        project
        / ".agent-flow"
        / "runtime"
        / "python"
        / "agent_flow"
        / "templates"
        / "_shared"
        / "review"
    )
    for name in ("architecture.md", "architecture-design.md"):
        assert (installed_review_root / name).read_text(encoding="utf-8") == (
            KIT_ROOT / "templates" / "_shared" / "review" / name
        ).read_text(encoding="utf-8")


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_user_prompt_spec_hooks_prepare_before_confirm_for_every_host(
    tmp_path: Path, binary: str
) -> None:
    project = tmp_path / f"ordered-user-prompt-{binary}"
    project.mkdir()

    result = _install_with(binary, project)
    assert result.returncode == 0, result.stderr

    expected = [
        "prepare-spec-user-prompt.py",
        "confirm-spec-user-prompt.py",
    ]
    for relative in (
        ".claude/settings.json",
        ".Codex/hooks.json",
        ".codex/hooks.json",
    ):
        payload = json.loads((project / relative).read_text(encoding="utf-8"))
        commands = [
            hook["command"]
            for entry in payload["hooks"]["UserPromptSubmit"]
            for hook in entry["hooks"]
        ]
        parsed = [shlex.split(command) for command in commands]
        assert [Path(parts[-1]).name for parts in parsed] == expected, (
            binary,
            relative,
            commands,
        )
        assert all(parts[:2] == ["/usr/bin/python3", "-I"] for parts in parsed)

    extension = (
        project / ".omp" / "extensions" / "agent-flow-hooks.ts"
    ).read_text(encoding="utf-8")
    input_handler = extension.split('pi.on("input"', 1)[1].split(
        'pi.on("context"', 1
    )[0]
    assert input_handler.index(
        'await runHook("prepare-spec-user-prompt.py"'
    ) < input_handler.index(
        'await runHook("confirm-spec-user-prompt.py"'
    )
    assert 'const command = scriptName.endsWith(".py") ? "/usr/bin/python3" : "/bin/bash";' in extension
    assert 'const args = scriptName.endsWith(".py") ? ["-I", scriptPath] : [scriptPath];' in extension


def _spec_confirmation_contract(text: str) -> str:
    start = text.index("run이 SPEC 확인 대기로 막히면")
    end = text.index("\n\n### ", start)
    return text[start:end]


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_installer_outputs_use_current_chat_spec_confirmation_contract(
    tmp_path: Path, binary: str
) -> None:
    project = tmp_path / f"spec-confirmation-{binary}"
    project.mkdir()

    result = _install_with(binary, project)
    assert result.returncode == 0, result.stderr

    canonical_bootstrap = (
        KIT_ROOT / "bootstrap" / "AGENTS.md.template"
    ).read_text(encoding="utf-8")
    expected_context_contract = _spec_confirmation_contract(canonical_bootstrap)
    legacy_confirm = (
        "agent-flow spec confirm --run-dir <run-dir> "
        "--artifact <run-dir>/artifacts/design.md"
    )
    for relative_path in (
        "AGENTS.md",
        "CLAUDE.md",
        ".agent-flow/bootstrap/AGENTS.md",
        ".agent-flow/bootstrap/CLAUDE.md",
    ):
        installed = (project / relative_path).read_text(encoding="utf-8")
        assert _spec_confirmation_contract(installed) == expected_context_contract
        assert "현재 대화의 새 turn으로 정확히 `승인`" in installed
        assert (
            "대상 worktree의 대화형 터미널에서 경로 없는 fallback "
            "`agent-flow spec confirm`"
        ) in installed
        assert (
            "`manual` verify 항목은 사용자가 "
            "`agent-flow spec approve <spec-id> --run-dir <run-dir>`"
        ) in installed
        assert (
            "agent는 fallback·manual 승인 명령이나 user-prompt hook을 "
            "대신 실행하지 않습니다."
        ) in installed
        assert legacy_confirm not in installed

    canonical_skill = (
        KIT_ROOT / "skills" / "agent-flow" / "SKILL.md"
    ).read_text(encoding="utf-8")
    installed_skill = (
        project / ".agent-flow" / "skills" / "agent-flow" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert installed_skill == canonical_skill
    assert "reply exactly `승인` in a new turn of the current chat" in installed_skill
    assert (
        "path-free fallback `agent-flow spec confirm` from the target worktree"
        in installed_skill
    )
    assert (
        "A `manual` verifier still requires the user to run "
        "`agent-flow spec approve <spec-id> --run-dir <run-dir>`."
    ) in installed_skill
    assert (
        "Never run the fallback or manual approval command, or invoke the "
        "user-prompt hook yourself."
    ) in installed_skill
    assert legacy_confirm not in installed_skill


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_no_hooks_removes_every_managed_hook_and_survives_reinstall(
    tmp_path: Path, binary: str
) -> None:
    """불변: hook을 끄면 등록·스크립트·OMP 확장이 모두 사라지고 재설치가 되살리지 않는다.

    되살아나면 끈 의미가 없다 — 사용자는 install 한 번마다 다시 꺼야 한다.
    두 진입점 모두 같은 계약이어야 한다. installer는 kit에 위임한 뒤 kit.json을
    자기 것으로 덮으므로, 한쪽만 고치면 재설치에서 조용히 되살아난다.
    """
    def _install(project: Path, *args: str):  # noqa: ANN202 - 로컬 바인딩
        return _install_with(binary, project, *args)

    project = tmp_path / "hooks-off"
    project.mkdir()

    assert _install(project).returncode == 0
    on = _hook_state(project)
    assert on["registered"], "기본 설치는 hook을 등록해야 한다"
    assert on["flag"] is True

    assert _install(project, "--no-hooks").returncode == 0
    off = _hook_state(project)
    assert off["registered"] == set()
    assert off["scripts"] == set()
    assert off["omp"] is False
    assert off["flag"] is False

    # 플래그 없이 재설치, force까지 — 둘 다 되살리면 안 된다.
    assert _install(project).returncode == 0
    assert _hook_state(project)["registered"] == set()
    assert _install(project, "--force-managed").returncode == 0
    assert _hook_state(project)["registered"] == set()

def test_standalone_no_hooks_prunes_stale_json_when_delegated_kit_fails(
    tmp_path: Path,
) -> None:
    """Standalone cleanup must not depend on delegated kit success."""
    import os

    project = tmp_path / "delegated-kit-failure"
    project.mkdir()
    stale = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": ".agent-flow/scripts/hooks/guard-protected-branch.sh",
                        },
                        {"type": "command", "command": "./custom-hook.sh"},
                    ],
                }
            ]
        }
    }
    for relative in (".claude/settings.json", ".Codex/hooks.json"):
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(stale), encoding="utf-8")

    fail_delegated_kit = tmp_path / "fail-delegated-kit.cjs"
    fail_delegated_kit.write_text(
        'if (process.argv[1]?.endsWith("agent-flow-kit.mjs")) '
        'throw new Error("delegated kit failure");\n',
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "NODE_OPTIONS": f"--require={fail_delegated_kit}",
    }
    result = _install_with(
        "agent-flow-install.mjs", project, "--no-hooks", env=env
    )

    assert result.returncode == 0, result.stderr
    assert "agent-flow-kit install skipped" in result.stderr
    for relative in (".claude/settings.json", ".Codex/hooks.json"):
        text = (project / relative).read_text(encoding="utf-8")
        assert "guard-protected-branch.sh" not in text
        assert "./custom-hook.sh" in text
    assert _hook_state(project)["scripts"] == set()



@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_hooks_flag_restores_them(tmp_path: Path, binary: str) -> None:
    """불변: 되돌릴 수 있어야 한다. 끄기가 편도면 그건 삭제다."""
    def _install(project: Path, *args: str):  # noqa: ANN202
        return _install_with(binary, project, *args)

    project = tmp_path / "hooks-back"
    project.mkdir()

    assert _install(project, "--no-hooks").returncode == 0
    assert _hook_state(project)["registered"] == set()

    assert _install(project, "--hooks").returncode == 0
    back = _hook_state(project)
    assert "record-skill-read.py" in back["registered"]
    assert "guard-protected-branch.sh" in back["registered"]
    assert back["omp"] is True
    assert back["flag"] is True


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_install_removes_broad_codex_project_trust(tmp_path: Path, binary: str) -> None:
    """Installer-generated hooks never justify a repo-wide writable trust grant."""
    import os
    import sys

    project = tmp_path / "trust"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f'[projects."{project.resolve()}"]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k != "AGENT_FLOW_SKIP_CODEX_TRUST"}
    env["HOME"] = str(home)
    # Keep workflow export independent of the temporary HOME.
    import yaml

    env["PYTHON"] = sys.executable
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(Path(yaml.__file__).resolve().parents[1]), env.get("PYTHONPATH")) if p
    )

    result = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / binary), "install"),
        cwd=project, text=True, capture_output=True, check=False, env=env,
    )
    assert result.returncode == 0, result.stderr

    config = home / ".codex" / "config.toml"
    text = config.read_text(encoding="utf-8") if config.is_file() else ""
    assert f'[projects."{project.resolve()}"]' not in text
    assert "trust_level" not in text
    assert "hooks.state" not in text
    assert "trusted_hash" not in text


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
@pytest.mark.parametrize("hooks_flag", [(), ("--no-hooks",)])
def test_install_output_satisfies_the_run_start_hook_gate(
    tmp_path: Path, binary: str, hooks_flag: tuple[str, ...]
) -> None:
    """불변: installer가 만든 상태는 런 시작 게이트를 그대로 통과한다.

    게이트의 기대값과 installer가 심는 것이 갈라지면, 정상 설치가 모든 런을
    막거나(오탐) 게이트가 아무것도 안 보게 된다(미탐). 둘 다 조용히 생긴다.
    """
    import sys

    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.core.hook_integrity import verify_managed_hooks

    project = tmp_path / f"gate-{binary}-{len(hooks_flag)}"
    project.mkdir()
    result = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / binary), "install", *hooks_flag),
        cwd=project, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr

    reports = verify_managed_hooks(project)
    assert len(reports) == 1
    assert reports[0].recorded is True
    assert reports[0].expected_enabled is (not hooks_flag)
    assert reports[0].violations == ()


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_broken_host_skill_symlink_does_not_brick_install(tmp_path: Path, binary: str) -> None:
    """반증: `existsSync`는 심링크를 따라가 끊어진 링크에 false를 준다.

    그러면 stale link 정리 분기를 못 타고 링크 생성이 `EEXIST`로 죽어 install이
    exit 1로 끝난다. profile을 좁히거나 `--skills` 선택을 바꾸면 이전 선택의 host
    링크가 끊긴 채 남으므로 실제로 밟는 경로다.
    """
    project = tmp_path / "broken-link"
    project.mkdir()
    first = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / binary), "install"),
        cwd=project, text=True, capture_output=True, check=False,
    )
    assert first.returncode == 0, first.stderr
    links = sorted(
        entry for entry in (project / ".claude" / "skills").iterdir() if entry.is_symlink()
    )
    if not links:
        pytest.skip("this platform copied instead of symlinking host skills")

    dangling = links[0]
    target = Path(os.readlink(dangling))
    dangling.unlink()
    dangling.symlink_to(project / "gone" / dangling.name)
    assert dangling.is_symlink() and not dangling.exists()

    result = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / binary), "install"),
        cwd=project, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (dangling / "SKILL.md").is_file(), f"stale link was not repaired (was {target})"


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_installers_never_probe_a_link_path_with_existssync(binary: str) -> None:
    """`existsSync`는 심링크를 따라가므로 끊어진 링크에 false를 준다.

    행위 테스트는 kit 경로만 닿는다 — installer는 kit에 위임한 뒤 이미 고쳐진
    링크를 보기 때문이다. 그래서 두 진입점 모두에 소스 계약으로 못박는다.
    """
    source = (KIT_ROOT / "bin" / binary).read_text(encoding="utf-8")
    assert "lstatIfExists(destDir)" in source
    assert "fs.existsSync(destDir)" not in source



def test_cross_tree_install_keeps_the_targets_tracked_scripts(tmp_path: Path) -> None:
    """반증: worktree의 kit으로 leader를 install하면 leader의 추적 소스가 삭제됐다.

    실측으로 `git status`에 `D scripts/*` 7개가 남았다. 관리 사본을 걷어내는 것이
    목적이지 사용자 소스를 지우는 것이 아니다.
    """
    project = tmp_path / "leader"
    project.mkdir()
    shutil.copytree(KIT_ROOT / "scripts", project / "scripts")
    for command in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "-A"],
        ["git", "commit", "-m", "track scripts"],
    ):
        subprocess.run(command, cwd=project, check=True, capture_output=True)

    result = _install(project)
    assert result.returncode == 0, result.stderr
    assert (project / "scripts").is_dir()
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", "scripts"],
        cwd=project, text=True, capture_output=True, check=True,
    ).stdout
    assert " D " not in status and not status.startswith("D "), status



@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
@pytest.mark.parametrize("script_name", ["check-context-docs.mjs", "check-context-docs.ts"])
def test_reinstall_removes_retired_context_checker(
    tmp_path: Path, binary: str, script_name: str
) -> None:
    project = tmp_path / f"retired-context-checker-{binary}-{script_name}"
    project.mkdir()
    assert _install_with(binary, project).returncode == 0

    stale = project / ".agent-flow" / "scripts" / script_name
    body = "retired checker\n"
    stale.write_text(body, encoding="utf-8")
    gitignore = project / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8") + "scripts/check-context-docs.*\n",
        encoding="utf-8",
    )

    result = _install_with(binary, project)

    assert result.returncode == 0, result.stderr
    assert not stale.exists()
    assert stale.with_name(f"{script_name}.removed").read_text(encoding="utf-8") == body
    assert "scripts/check-context-docs.*" not in gitignore.read_text(encoding="utf-8")


def _skill_index_block(project: Path, file_name: str = "AGENTS.md") -> str:
    text = (project / file_name).read_text(encoding="utf-8")
    start = text.index("<!-- agent-flow:skills:start -->")
    end = text.index("<!-- agent-flow:skills:end -->")
    return text[start:end]


def _indexed_names(block: str, group: str) -> set[str]:
    match = re.search(rf"\|{group}:\{{([^}}]*)\}}", block)
    if not match:
        return set()
    return {name.strip() for name in match.group(1).split(",") if name.strip()}


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_install_writes_the_skill_index_into_agents_md(tmp_path: Path, binary: str) -> None:
    """설치된 skill을 AGENTS.md 안에서 바로 보여야 한다.

    `index.json`을 읽으라고 안내만 하면 그건 판단 지점이고, agent는 그 판단을
    자주 건너뛴다. 목록이 문서 안에 있으면 건너뛸 판단 자체가 없다.
    """
    project = tmp_path / f"index-{binary}"
    project.mkdir()
    assert _install_with(binary, project).returncode == 0

    installed = {
        entry.name
        for entry in (project / ".agent-flow" / "skills").iterdir()
        if entry.is_dir() and (entry / "SKILL.md").is_file()
    }
    assert installed
    for file_name in ("AGENTS.md", "CLAUDE.md"):
        block = _skill_index_block(project, file_name)
        assert "[agent-flow skill index]" in block, f"{file_name} 인덱스가 채워지지 않았다"
        listed = _indexed_names(block, "always") | _indexed_names(block, "on-demand")
        assert listed == installed, f"{file_name}: {installed ^ listed}"


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_passive_delivery_lands_on_the_always_line(tmp_path: Path, binary: str) -> None:
    """반증: 전부 on-demand로 적으면 "항상 적용" 선언이 인덱스에서 사라진다."""
    project = tmp_path / f"delivery-{binary}"
    project.mkdir()
    assert _install_with(binary, project).returncode == 0

    block = _skill_index_block(project)
    always = _indexed_names(block, "always")
    assert "code-generation-discipline" in always
    assert "comment-authoring-discipline" in always
    # 선언하지 않은 skill이 always로 올라가면 안 쓰는 skill이 상시 노출된다.
    assert "tdd" in _indexed_names(block, "on-demand")
    assert "tdd" not in always


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_reinstall_does_not_duplicate_the_skill_index(tmp_path: Path, binary: str) -> None:
    """반증: 블록을 덧붙이면 재설치마다 AGENTS.md가 자란다."""
    project = tmp_path / f"reinstall-{binary}"
    project.mkdir()
    assert _install_with(binary, project).returncode == 0
    first = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert _install_with(binary, project).returncode == 0
    second = (project / "AGENTS.md").read_text(encoding="utf-8")

    assert second == first
    assert second.count("<!-- agent-flow:skills:start -->") == 1
    assert second.count("[agent-flow skill index]") == 1


def _workflow_backups(project: Path, stem: str) -> dict[str, str]:
    workflows = project / ".agent-flow" / "workflows"
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in workflows.iterdir()
        if path.name.startswith(f"{stem}.removed")
    }


def _kit_json(project: Path) -> dict:
    return json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_pruned_custom_workflow_leaves_a_backup_and_says_where(tmp_path: Path, binary: str) -> None:
    """반증: prune이 사용자가 만든 workflow를 경고도 사본도 없이 지웠다.

    알리지 않으면 사용자는 자기 workflow가 사라진 것을 다음 run이 깨질 때 안다.
    그때는 되돌릴 원본이 없다. 두 진입점이 같은 문장을 내야 한다 — 한쪽만
    알리면 어느 CLI를 썼는지에 따라 손실이 조용해진다.
    """
    project = tmp_path / f"prune-{binary}"
    project.mkdir()
    assert _install_with(binary, project).returncode == 0

    body = "id: my-custom\nphases: []\n"
    custom = project / ".agent-flow" / "workflows" / "my-custom.yaml"
    custom.write_text(body, encoding="utf-8")
    result = _install_with(binary, project)
    assert result.returncode == 0

    assert not custom.exists()
    backup = custom.with_name("my-custom.yaml.removed")
    assert backup.read_text(encoding="utf-8") == body
    # 백업이 `.yaml`로 끝나면 workflow 로더가 그것을 workflow로 되살려 읽는다.
    assert not backup.name.endswith(".yaml")

    expected = (
        "  - pruned: .agent-flow/workflows/my-custom.yaml"
        " (backup: .agent-flow/workflows/my-custom.yaml.removed)"
    )
    assert result.stdout.splitlines().count(expected) == 1, result.stdout


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_repeated_install_never_multiplies_workflow_backups(tmp_path: Path, binary: str) -> None:
    """불변: 백업은 잃은 버전당 하나다.

    `.removed`가 prune 대상에 남아 있으면 다음 install이 백업을 지운다. 반대로
    매번 새 이름을 붙이면 재설치 횟수만큼 사본이 쌓인다. 둘 다 손실이다.
    """
    project = tmp_path / f"prune-idem-{binary}"
    project.mkdir()
    assert _install_with(binary, project).returncode == 0
    custom = project / ".agent-flow" / "workflows" / "my-custom.yaml"

    first = "id: my-custom\nphases: []\n"
    for _ in range(3):
        custom.write_text(first, encoding="utf-8")
        assert _install_with(binary, project).returncode == 0
    assert _workflow_backups(project, "my-custom.yaml") == {"my-custom.yaml.removed": first}

    second = "id: my-custom\nphases: [edited]\n"
    for _ in range(2):
        custom.write_text(second, encoding="utf-8")
        assert _install_with(binary, project).returncode == 0
    digest = hashlib.sha256(second.encode("utf-8")).hexdigest()[:8]
    assert _workflow_backups(project, "my-custom.yaml") == {
        "my-custom.yaml.removed": first,
        f"my-custom.yaml.removed.{digest}": second,
    }


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_installed_at_is_the_first_install_and_updated_at_is_the_last(
    tmp_path: Path, binary: str
) -> None:
    """불변: installed_at은 최초 설치, updated_at은 마지막 설치다.

    두 진입점이 같은 필드에 다른 뜻을 담고 있었다 — kit은 보존하고 installer는
    매번 덮었다. 그러면 "언제부터 쓰던 프로젝트인가"의 답이 어느 CLI를 썼는지에
    따라 달라진다.
    """
    project = tmp_path / f"stamp-{binary}"
    project.mkdir()

    assert _install_with(binary, project).returncode == 0
    stamps = [_kit_json(project)]
    for args in ((), ("--force-managed",), ("--no-hooks",)):
        assert _install_with(binary, project, *args).returncode == 0
        stamps.append(_kit_json(project))

    first = stamps[0]["installed_at"]
    assert {stamp["installed_at"] for stamp in stamps} == {first}
    updated = [stamp["updated_at"] for stamp in stamps]
    assert updated == sorted(updated)
    assert first <= updated[0] < updated[-1]


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_legacy_kit_json_keeps_installed_at_and_gains_updated_at(
    tmp_path: Path, binary: str
) -> None:
    """반증: updated_at을 기존 installed_at으로 backfill하면 없는 기록을 지어낸다.

    updated_at이 없던 설치본은 마지막 설치 시각을 모른다. 모르는 값을 꾸미는
    대신 이번 install로 채운다. 읽을 수 없는 kit.json은 최초 설치와 같다.
    """
    project = tmp_path / f"legacy-{binary}"
    project.mkdir()
    assert _install_with(binary, project).returncode == 0

    legacy = "2020-01-01T00:00:00.000Z"
    kit_path = project / ".agent-flow" / "kit.json"
    payload = _kit_json(project)
    payload["installed_at"] = legacy
    payload.pop("updated_at", None)
    kit_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    assert _install_with(binary, project).returncode == 0
    migrated = _kit_json(project)
    assert migrated["installed_at"] == legacy
    assert migrated["updated_at"] > legacy

    kit_path.write_text("{ broken", encoding="utf-8")
    assert _install_with(binary, project).returncode == 0
    fresh = _kit_json(project)
    assert fresh["installed_at"] > legacy
    assert fresh["installed_at"] <= fresh["updated_at"]


def _hook_matchers(project: Path, script: str) -> list[str]:
    matchers: list[str] = []
    for relative in (".claude/settings.json", ".Codex/hooks.json", ".codex/hooks.json"):
        path = project / relative
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for entries in (payload.get("hooks") or {}).values():
            for entry in entries:
                commands = " ".join(
                    str(hook.get("command", "")) for hook in entry.get("hooks") or []
                )
                if script in commands:
                    matchers.append(str(entry.get("matcher", "")))
    return matchers


def _tool_names(matcher: str) -> list[str]:
    return matcher.removeprefix("^(").removesuffix(")$").split("|")


def test_skill_use_observer_records_every_tool_its_matcher_admits(tmp_path: Path) -> None:
    """등록과 소비가 갈라지면 matcher가 붙인 tool이 조용히 버려진다."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname = \"x\"\n", encoding="utf-8")
    assert _install(project).returncode == 0
    skill = project / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\n---\n", encoding="utf-8")
    hook = project / ".agent-flow" / "scripts" / "hooks" / "record-skill-read.py"

    matchers = _hook_matchers(project, "record-skill-read.py")
    assert matchers
    for matcher in matchers:
        for tool in ("Skill", "Bash", "Read"):
            assert re.match(matcher, tool), matcher

    log = project / ".agent-flow" / "skills-read.jsonl"
    for tool in _tool_names(matchers[0]):
        log.unlink(missing_ok=True)
        payload = {
            "tool_name": tool,
            "cwd": str(project),
            "tool_input": {"command": f"cat {skill}", "skill": "demo", "path": str(skill)},
        }
        subprocess.run(
            (sys.executable, str(hook)),
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=str(project),
            timeout=30,
            check=False,
        )
        assert log.is_file(), tool


def test_installers_do_not_enumerate_external_skill_names() -> None:
    """열거된 목록은 우리가 배포하지도 않는 이름을 영구히 들고 있게 된다."""
    for name in ("bin/agent-flow-kit.mjs", "bin/agent-flow-install.mjs"):
        text = (KIT_ROOT / name).read_text(encoding="utf-8")
        match = re.search(
            r"const PROFILE_MANAGED_HOST_ONLY_SKILLS = new Set\((.*?)\);", text, re.S
        )
        assert match, name
        assert match.group(1).strip() == "", name


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_plain_install_refreshes_a_managed_hook_to_match_its_recorded_digest(
    tmp_path: Path, binary: str
) -> None:
    """digest는 갱신되고 hook 파일은 안 갱신되면 run 시작이 막힌다 — 평범한 install로 복구돼야 한다."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname = \"x\"\n", encoding="utf-8")
    assert _install_with(binary, project).returncode == 0
    hook = project / ".agent-flow" / "scripts" / "hooks" / "record-skill-read.py"
    hook.write_text("# stale copy from an older kit\n", encoding="utf-8")
    os.chmod(hook, 0o755)

    assert _install_with(binary, project).returncode == 0

    recorded = json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
    digest = recorded["managed_hook_digests"]["record-skill-read.py"]
    assert hashlib.sha256(hook.read_bytes()).hexdigest() == digest
    assert os.access(hook, os.X_OK)
    # digest만 보면 백업이 남긴 실행 파일이 run 시작을 막는 것을 놓친다.
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.core.hook_integrity import verify_managed_hooks

    reports = verify_managed_hooks(project)
    assert reports and all(report.ok for report in reports), [
        list(report.violations) for report in reports
    ]


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_install_root_option_targets_another_project(tmp_path: Path, binary: str) -> None:
    """반증: `--root`가 무시되면 cwd에 깔고 성공으로 끝난다 — 어디 설치됐는지 알 길이 없다."""
    elsewhere = tmp_path / f"elsewhere-{binary}"
    elsewhere.mkdir()
    target = tmp_path / f"target-{binary}"
    target.mkdir()

    result = _install_with(binary, elsewhere, "--root", str(target))

    assert result.returncode == 0, result.stderr
    assert (target / ".agent-flow" / "kit.json").is_file()
    assert not (elsewhere / ".agent-flow").exists()
    assert not (elsewhere / "AGENTS.md").exists()


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_install_root_rejects_a_path_that_is_not_a_directory(tmp_path: Path, binary: str) -> None:
    """install은 없는 경로를 mkdir로 만들어 낸다. 오타가 빈 트리를 심고 성공하면 안 된다."""
    project = tmp_path / f"project-{binary}"
    project.mkdir()
    missing = tmp_path / f"missing-{binary}"

    result = _install_with(binary, project, "--root", str(missing))

    # 주석이 주장하는 계약은 "한 줄"이다. substring만 보면 try/catch를 통째로 지워도
    # node의 uncaught 출력에 같은 문장이 있어 통과한다.
    assert result.returncode == 1
    assert result.stderr.strip() == f"--root must be an existing directory: {missing}"
    assert not missing.exists()
    assert not (project / ".agent-flow").exists()


def test_run_install_does_not_swallow_the_root_option(tmp_path: Path) -> None:
    """`run install`은 `--root`를 읽는 runWorkflowCommand 앞에서 가로챈다. 그 분기가
    인자를 안 넘기면 같은 계열의 플래그가 조용히 죽고 cwd에 설치된다."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = tmp_path / "target"
    target.mkdir()

    result = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "run", "install", "--root", str(target)),
        cwd=elsewhere,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (target / ".agent-flow" / "kit.json").is_file()
    assert not (elsewhere / ".agent-flow").exists()


def test_relative_install_root_resolves_against_the_caller_cwd(tmp_path: Path) -> None:
    """자식 kit install은 이미 해석된 경로를 cwd로 받는다. `--root`를 그대로
    넘기면 자식이 제 cwd 기준으로 한 번 더 풀어 다른 곳을 가리킨다."""
    project = tmp_path / "workspace"
    (project / "app").mkdir(parents=True)

    result = _install_with("agent-flow-install.mjs", project, "--root", "app")

    assert result.returncode == 0, result.stderr
    # 자식 kit install이 죽어도 부모는 경고 한 줄로 낮추고 제 손으로 kit.json을 쓴다.
    # `prompts/`는 kit install만 만들므로 위임 성공의 오라클이다.
    assert "agent-flow-kit install skipped" not in result.stderr
    assert (project / "app" / ".agent-flow" / "prompts").is_dir()
    assert (project / "app" / ".agent-flow" / "kit.json").is_file()
    assert not (project / ".agent-flow").exists()


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_install_root_inside_a_git_repo_names_the_root_it_would_use(tmp_path: Path, binary: str) -> None:
    """`resolveInstallRoot`는 git root로 걸어 올라간다. 사용자가 이름을 댄 자리가
    조용히 바뀌면 지금 고치는 버그와 같은 모양이 된다."""
    repo = tmp_path / f"repo-{binary}"
    (repo / "packages" / "app").mkdir(parents=True)
    subprocess.run(("git", "init", "-q", str(repo)), check=True)
    caller = tmp_path / f"caller-{binary}"
    caller.mkdir()

    result = _install_with(binary, caller, "--root", str(repo / "packages" / "app"))

    assert result.returncode == 1
    assert "resolves to" in result.stderr
    assert not (repo / "packages" / "app" / ".agent-flow").exists()
    assert not (repo / ".agent-flow").exists()


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_symlinked_worktree_root_is_blocked_by_both_entry_points(tmp_path: Path, binary: str) -> None:
    """두 진입점의 worktree 판정은 각각 realpath와 path.resolve다. `--root`를
    정규화하지 않으면 심볼릭 링크 하나가 한쪽 guard만 통과한다."""
    leader = tmp_path / f"leader-{binary}"
    worktree = leader / ".agent-flow" / "worktrees" / "feat-x"
    worktree.mkdir(parents=True)
    alias = tmp_path / f"alias-{binary}"
    alias.symlink_to(worktree)
    caller = tmp_path / f"caller-{binary}"
    caller.mkdir()

    result = _install_with(binary, caller, "--root", str(alias))

    assert result.returncode == 1, result.stdout
    assert list(worktree.iterdir()) == []


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
@pytest.mark.parametrize("value", ["--root=", "--root:empty", "--root:flag"])
def test_install_root_requires_a_real_value(tmp_path: Path, binary: str, value: str) -> None:
    """빈 값은 cwd로 접혀 원래 버그와 같은 결과를 내고, `-`로 시작하는 값은 다음
    플래그를 root로 삼킨다 — 그 이름의 디렉터리가 있으면 실제로 거기 설치된다."""
    project = tmp_path / f"project-{binary}-{value}"
    project.mkdir()
    (project / "--force-managed").mkdir()
    args = {
        "--root=": ("--root=",),
        "--root:empty": ("--root", ""),
        "--root:flag": ("--root", "--force-managed"),
    }[value]

    result = _install_with(binary, project, *args)

    assert result.returncode == 1
    assert result.stderr.strip() == "--root requires a value"
    assert not (project / ".agent-flow").exists()
    assert not (project / "--force-managed" / ".agent-flow").exists()


def test_root_option_is_not_validated_before_the_command_dispatch(tmp_path: Path) -> None:
    """`--root` 해석이 커맨드 디스패치보다 먼저 돌면 install이 아닌 커맨드가
    받지도 않는 플래그의 오류로 죽는다."""
    project = tmp_path / "project"
    project.mkdir()

    result = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-install.mjs"), "bogus", "--root", str(tmp_path / "nope")),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stderr.strip() == "Unknown command: bogus"


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_install_root_pointing_at_the_marker_dir_is_blocked_the_same_way(tmp_path: Path, binary: str) -> None:
    """두 진입점의 root 해석이 갈리면 한쪽만 `<proj>/.agent-flow/.agent-flow`를 만든다."""
    project = tmp_path / f"proj-{binary}"
    (project / ".agent-flow").mkdir(parents=True)
    caller = tmp_path / f"caller-{binary}"
    caller.mkdir()

    result = _install_with(binary, caller, "--root", str(project / ".agent-flow"))

    assert result.returncode == 1
    assert "resolves to" in result.stderr
    assert list((project / ".agent-flow").iterdir()) == []


_UPGRADE_PROBE_SKILL = "code-generation-discipline"


def _record_installed_hash(project: Path, name: str) -> None:
    """이전 install이 쓴 그대로라고 기록한다. 사용자가 손대지 않았다는 뜻이다."""
    index_path = project / ".agent-flow" / "skills" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    body = (project / ".agent-flow" / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    found = False
    for skill in index["skills"]:
        if skill.get("name") == name:
            skill["hash"] = digest
            found = True
    # 못 찾고 지나가면 갱신 조건이 성립하지 않아, 원인과 무관한 단언이 대신 깨진다.
    assert found, (name, sorted(skill.get("name") for skill in index["skills"]))
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_reinstall_upgrades_an_untouched_bundled_skill(tmp_path: Path, binary: str) -> None:
    """반증: "내용이 다르면 사용자 편집"으로만 보호하면 kit이 고친 skill이 기존
    설치본에 영영 닿지 않는다 — 실측으로 `workflowPhases` 개정이 설치된 프로젝트
    어디에도 도달하지 못했고 그 skill은 계속 활성화되지 않았다."""
    project = tmp_path / f"project-{binary}"
    project.mkdir()
    assert _install_with(binary, project).returncode == 0
    installed = project / ".agent-flow" / "skills" / _UPGRADE_PROBE_SKILL / "SKILL.md"
    shipped = (KIT_ROOT / "skills" / _UPGRADE_PROBE_SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert installed.is_file()

    # 옛 kit이 배포했던 판본을 흉내낸다. 사용자는 손대지 않았다.
    installed.write_text("---\nname: old\n---\n\n# 옛 판본\n", encoding="utf-8")
    _record_installed_hash(project, _UPGRADE_PROBE_SKILL)

    result = _install_with(binary, project)

    assert result.returncode == 0, result.stderr
    assert installed.read_text(encoding="utf-8") == shipped


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_reinstall_keeps_a_user_edited_bundled_skill(tmp_path: Path, binary: str) -> None:
    """불변: 갱신이 사용자 편집을 덮으면 그 자리에 둔 프로젝트 규칙이 사라진다.

    한 번으로는 부족하다. index의 hash는 발견 시점의 파일에서 뽑으므로, 편집을 한 번
    건너뛰고 나면 그 편집 내용이 "우리가 쓴 것"으로 기록돼 다음 재설치에서 오라클이
    일치하고 편집이 덮인다 — 재설치 두 번이면 잃는다."""
    project = tmp_path / f"project-{binary}"
    project.mkdir()
    assert _install_with(binary, project).returncode == 0
    installed = project / ".agent-flow" / "skills" / _UPGRADE_PROBE_SKILL / "SKILL.md"
    edited = "---\nname: code-generation-discipline\n---\n\n# 우리 규칙\n"
    # index의 hash는 그대로 둔다 — 기록과 내용이 갈리는 것이 곧 사용자 편집이다.
    installed.write_text(edited, encoding="utf-8")

    for _ in range(3):
        result = _install_with(binary, project)
        assert result.returncode == 0, result.stderr
        assert installed.read_text(encoding="utf-8") == edited


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_skill_upgrade_reports_what_it_changed(tmp_path: Path, binary: str) -> None:
    """무음 교체가 원래 버그를 수개월 가렸다. install.mjs 경로는 실제 갱신을 자식이
    하므로, 자식 stdout을 걸러 내면 그 경로만 다시 조용해진다."""
    project = tmp_path / f"project-{binary}"
    project.mkdir()
    assert _install_with(binary, project).returncode == 0
    installed = project / ".agent-flow" / "skills" / _UPGRADE_PROBE_SKILL / "SKILL.md"
    installed.write_text("---\nname: old\n---\n\n# 옛 판본\n", encoding="utf-8")
    _record_installed_hash(project, _UPGRADE_PROBE_SKILL)

    result = _install_with(binary, project)

    assert result.returncode == 0, result.stderr
    assert f"upgraded skill: {_UPGRADE_PROBE_SKILL}" in result.stdout


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_skill_upgrade_does_not_revert_a_sibling_file(tmp_path: Path, binary: str) -> None:
    """오라클은 `SKILL.md` 하나다. 형제 파일까지 덮으면 근거 없는 되돌림이 된다."""
    project = tmp_path / f"project-{binary}"
    project.mkdir()
    assert _install_with(binary, project).returncode == 0
    sibling = next(
        (
            item
            for item in (project / ".agent-flow" / "skills" / "android-guides" / "references").glob("*.md")
        ),
        None,
    )
    if sibling is None:
        pytest.skip("android-guides references are not installed for this selection")
    sibling.write_text("# 우리 팀 규칙\n", encoding="utf-8")
    installed = project / ".agent-flow" / "skills" / _UPGRADE_PROBE_SKILL / "SKILL.md"
    installed.write_text("---\nname: old\n---\n\n# 옛 판본\n", encoding="utf-8")
    _record_installed_hash(project, _UPGRADE_PROBE_SKILL)

    assert _install_with(binary, project).returncode == 0

    assert sibling.read_text(encoding="utf-8") == "# 우리 팀 규칙\n"


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_an_upgraded_skill_can_still_be_dropped_by_narrowing_the_profile(tmp_path: Path, binary: str) -> None:
    """갱신이 skill 디렉터리에 여분 파일을 남기면 `dirContentsMatch`의 항목 수 비교가
    어긋나 profile을 좁혀도 그 skill이 지워지지 않는다. `--profile ios`가 실제로
    떨어뜨리는 skill이어야 하므로 android 전용 이름을 쓴다."""
    dropped = "android-clean-presentation-architecture"
    project = tmp_path / f"project-{binary}"
    project.mkdir()
    assert _install_with(binary, project).returncode == 0
    skill_dir = project / ".agent-flow" / "skills" / dropped
    (skill_dir / "SKILL.md").write_text("---\nname: old\n---\n\n# 옛 판본\n", encoding="utf-8")
    _record_installed_hash(project, dropped)
    assert _install_with(binary, project).returncode == 0

    assert _install_with(binary, project, "--profile", "ios").returncode == 0

    assert not skill_dir.exists(), sorted(item.name for item in skill_dir.iterdir())


_SIBLING_RELATIVE = Path(".agent-flow/skills/android-guides/references/architecture-rules-guide.md")
_TEMPLATE_RELATIVE = Path(".agent-flow/templates/_shared/review/sdui.md")


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
@pytest.mark.parametrize("relative", [_SIBLING_RELATIVE, _TEMPLATE_RELATIVE], ids=["sibling", "template"])
def test_kit_assets_without_a_record_are_upgraded_once(tmp_path: Path, binary: str, relative: Path) -> None:
    """반증: `SKILL.md` 밖 자산은 오라클이 없어 낡은 채로 남았다 — review 템플릿 4개가
    실제로 그랬다. 기록이 생기기 전에 깔린 프로젝트는 사본을 남기고 한 번 갱신한다."""
    project = tmp_path / f"project-{binary}-{relative.name}"
    project.mkdir()
    assert _install_with(binary, project).returncode == 0
    target = project / relative
    assert target.is_file()
    shipped = target.read_text(encoding="utf-8")
    target.write_text("# 낡은 판본\n", encoding="utf-8")
    (project / ".agent-flow" / "kit-assets.json").unlink()

    result = _install_with(binary, project)

    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8") == shipped
    assert f"upgraded: {relative.as_posix()}" in result.stdout
    backup = project / ".agent-flow" / "backups" / relative.relative_to(".agent-flow")
    assert backup.read_text(encoding="utf-8") == "# 낡은 판본\n"
    # 사본은 미러 트리 밖에 남는다. 안에 남기면 profile을 좁혀도 그 skill이 안 지워진다.
    assert not list(target.parent.glob("*.bak*"))
    assert f"backup: .agent-flow/backups/{relative.relative_to('.agent-flow').as_posix()}" in result.stdout

    again = _install_with(binary, project)
    assert f"upgraded: {relative.as_posix()}" not in again.stdout


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
@pytest.mark.parametrize("relative", [_SIBLING_RELATIVE, _TEMPLATE_RELATIVE], ids=["sibling", "template"])
def test_kit_assets_keep_a_user_edit_across_reinstalls(tmp_path: Path, binary: str, relative: Path) -> None:
    """불변: 기록과 다른 내용은 사용자 편집이다. 몇 번을 재설치해도 남아야 한다."""
    project = tmp_path / f"project-{binary}-{relative.name}"
    project.mkdir()
    assert _install_with(binary, project).returncode == 0
    target = project / relative
    target.write_text("# 우리 규칙\n", encoding="utf-8")

    for _ in range(3):
        result = _install_with(binary, project)
        assert result.returncode == 0, result.stderr
        assert target.read_text(encoding="utf-8") == "# 우리 규칙\n"
        # 복사 단계와 동기화 단계가 같은 파일을 각각 판정한다. 둘 다 알리면 잡음이 된다.
        assert result.stdout.count(f"skipped (user-modified): {relative.as_posix()}") == 1


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_install_records_the_kit_assets_it_wrote(tmp_path: Path, binary: str) -> None:
    """기록이 없으면 다음 install이 "우리가 쓴 그대로인가"를 물을 수 없다."""
    project = tmp_path / f"project-{binary}"
    project.mkdir()

    assert _install_with(binary, project).returncode == 0

    record = json.loads((project / ".agent-flow" / "kit-assets.json").read_text(encoding="utf-8"))
    assert record["version"] == 1
    assert _SIBLING_RELATIVE.as_posix() in record["files"]
    assert _TEMPLATE_RELATIVE.as_posix() in record["files"]
    # `SKILL.md`는 index hash가 오라클이다. 두 기록이 겹치면 어느 쪽이 이기는지 모른다.
    assert not any(name.endswith("/SKILL.md") for name in record["files"])


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_reinstall_reports_a_skipped_user_edited_skill(tmp_path: Path, binary: str) -> None:
    """무음 skip이 "왜 kit 개정이 안 왔는가"를 물을 자리를 없앴다."""
    project = tmp_path / f"project-{binary}"
    project.mkdir()
    assert _install_with(binary, project).returncode == 0
    installed = project / ".agent-flow" / "skills" / _UPGRADE_PROBE_SKILL / "SKILL.md"
    installed.write_text("---\nname: code-generation-discipline\n---\n\n# 우리 규칙\n", encoding="utf-8")

    result = _install_with(binary, project)

    assert result.returncode == 0, result.stderr
    assert f"skipped (user-modified): .agent-flow/skills/{_UPGRADE_PROBE_SKILL}/SKILL.md" in result.stdout


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_a_synced_sibling_asset_does_not_block_profile_pruning(tmp_path: Path, binary: str) -> None:
    """반증: 사본을 skill 디렉터리 안에 남기면 `dirContentsMatch`의 항목 수 비교가
    어긋나 profile을 좁혀도 그 skill이 다시는 지워지지 않는다."""
    project = tmp_path / f"project-{binary}"
    project.mkdir()
    assert _install_with(binary, project).returncode == 0
    (project / _SIBLING_RELATIVE).write_text("# 낡은 판본\n", encoding="utf-8")
    (project / ".agent-flow" / "kit-assets.json").unlink()
    assert _install_with(binary, project).returncode == 0

    assert _install_with(binary, project, "--profile", "ios").returncode == 0

    dropped = project / ".agent-flow" / "skills" / "android-guides"
    assert not dropped.exists(), sorted(item.name for item in dropped.rglob("*"))


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_a_corrupt_asset_record_does_not_trigger_a_bulk_overwrite(tmp_path: Path, binary: str) -> None:
    """반증: 잘린 기록을 "기록 없음"으로 읽으면 부트스트랩 분기로 떨어져 살아 있는
    사용자 편집을 한꺼번에 덮는다."""
    project = tmp_path / f"project-{binary}"
    project.mkdir()
    assert _install_with(binary, project).returncode == 0
    edited = project / _SIBLING_RELATIVE
    edited.write_text("# 우리 규칙\n", encoding="utf-8")
    (project / ".agent-flow" / "kit-assets.json").write_text('{"version": 1, "fil', encoding="utf-8")

    result = _install_with(binary, project)

    assert result.returncode == 0, result.stderr
    assert edited.read_text(encoding="utf-8") == "# 우리 규칙\n"
    assert not (project / ".agent-flow" / "backups").exists()
    assert "kit asset sync skipped" in result.stderr


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_install_banner_names_the_root_it_wrote_to(tmp_path: Path, binary: str) -> None:
    """어디에 깔렸는지 안 보이면 잘못된 checkout에 깔린 것을 알 방법이 없다."""
    project = tmp_path / "proj"
    project.mkdir()
    result = _install_with(binary, project)
    assert result.returncode == 0, result.stderr
    assert str(project.resolve()) in result.stdout
    # `.agent-flow` 디렉토리가 아니라 프로젝트 루트다 - 두 진입점이 같은 것을 낸다.
    assert f"{project.resolve()}/.agent-flow\n" not in result.stdout


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_the_worktrees_container_is_not_an_install_target(tmp_path: Path, binary: str) -> None:
    """`<marker>/worktrees`는 형제 checkout을 담는 자리다. 설치본이 생기면 안 된다."""
    container = tmp_path / "proj" / ".agent-flow" / "worktrees"
    container.mkdir(parents=True)
    result = _install_with(binary, container)
    assert result.returncode == 1, result.stdout
    assert not (container / ".agent-flow").exists()


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_an_unusable_git_reports_one_line_not_a_stack_trace(tmp_path: Path, binary: str) -> None:
    """`PROJECT`는 모듈 상수라 dispatch의 try/catch가 받지 못한다."""
    fake_bin = tmp_path / "bin"
    (fake_bin / "git").mkdir(parents=True)  # spawn이 EACCES를 낸다
    project = tmp_path / "proj"
    project.mkdir()
    env = {**os.environ, "PATH": str(fake_bin)}
    result = _install_with(binary, project, env=env)
    assert result.returncode == 1
    assert "git rev-parse" in result.stderr
    assert "at " not in result.stderr, result.stderr
    # git이 못 돌아도 install이 아닌 명령은 root를 물을 이유가 없다. kit은 예외다 -
    # 모르는 명령을 Python CLI로 넘기고 그쪽이 root를 필요로 한다(main과 같다).
    if binary == "agent-flow-install.mjs":
        help_result = subprocess.run(
            (_node(), str(KIT_ROOT / "bin" / binary), "--help"),
            cwd=project, text=True, capture_output=True, check=False, env=env,
        )
        assert help_result.returncode == 0, help_result.stderr
