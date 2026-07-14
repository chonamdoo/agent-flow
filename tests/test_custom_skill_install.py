from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


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


def _skill(
    path: Path,
    body: str,
    *,
    hosts: str | None = None,
    description: str = "Use when testing custom skills.",
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    host_line = f"hosts: {hosts}\n" if hosts is not None else ""
    (path / "SKILL.md").write_text(
        "---\n"
        f"name: {path.name}\n"
        f"description: {description}\n"
        f"{host_line}"
        "tags: [test]\n"
        "---\n"
        f"Use when testing custom skills.\n\n{body}\n",
        encoding="utf-8",
    )


def test_overlong_skill_installs_and_validates_without_length_diagnostic(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    body = "\n".join(f"detail {line}" for line in range(260))
    _skill(
        project / "skills" / "long-skill",
        body,
        description="Validates unrestricted skill documents. Use when testing long skills.",
    )

    install = _install(project)
    validation = subprocess.run(
        (_node(), str(KIT_ROOT / "scripts" / "validate-skills.mjs")),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )

    assert install.returncode == 0, install.stderr
    assert validation.returncode == 0, validation.stderr
    assert "lines; consider progressive disclosure" not in validation.stdout
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    selected = next(skill for skill in index["skills"] if skill["name"] == "long-skill")
    assert len(selected["hash"]) == 64
    for host_dir in (".claude", ".Codex", ".omp"):
        installed = project / host_dir / "skills" / "long-skill" / "SKILL.md"
        assert installed.read_text(encoding="utf-8").count("detail ") == 260


def test_pinned_workspace_write_guard_is_installed_for_all_hosts(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = _install(project)

    assert result.returncode == 0, result.stderr
    guard = project / ".agent-flow" / "scripts" / "hooks" / "guard-worktree-write.py"
    assert guard.is_file()
    assert guard.stat().st_mode & 0o111
    assert (project / ".agent-flow" / "runtime" / "python" / "agent_flow" / "core" / "workspace_boundary.py").is_file()
    codex = (project / ".Codex" / "hooks.json").read_text(encoding="utf-8")
    claude = (project / ".claude" / "settings.json").read_text(encoding="utf-8")
    omp = (project / ".omp" / "extensions" / "agent-flow-hooks.ts").read_text(encoding="utf-8")
    assert "guard-worktree-write.py" in codex
    assert "guard-worktree-write.py" in claude
    assert 'runHook("guard-worktree-write.py"' in omp


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
        "diagnosing-bugs",
        "domain-modeling",
        "grill-with-docs",
        "grilling",
        "improve-codebase-architecture",
        "qa",
        "setup-matt-pocock-skills",
        "tdd",
        "to-issues",
        "to-prd",
        "triage",
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
        "diagnosing-bugs",
        "domain-modeling",
        "grill-with-docs",
        "grilling",
        "improve-codebase-architecture",
        "qa",
        "setup-matt-pocock-skills",
        "tdd",
        "to-issues",
        "to-prd",
        "triage",
    }
    assert index["selection"]["profiles"] == ["android"]
    assert "clean-architecture-core" in names
    assert matt_skill_closure <= names
    assert "android-clean-architecture" in names
    assert "android-code-review" in names
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
    assert "source: https://github.com/android/skills" in android_profile
    assert "source: https://github.com/chrisbanes/skills/tree/main/skills" in android_profile


def test_android_skill_policy_is_active_host_local_only() -> None:
    profile_paths = [
        KIT_ROOT / "profiles" / "android.yaml",
        KIT_ROOT / "src" / "agent_flow" / "profiles" / "android.yaml",
    ]
    policy_paths = [
        KIT_ROOT / "profiles" / "_schema.yaml",
        KIT_ROOT / "templates" / "_shared" / "review" / "android-skills.md",
        KIT_ROOT / "templates" / "_shared" / "review" / "android-chrisbanes.md",
        KIT_ROOT / "skills" / "android-code-review" / "SKILL.md",
    ]

    for path in profile_paths:
        text = path.read_text(encoding="utf-8")
        assert "install_policy: never" in text
        assert "active_host_only: true" in text
        assert "codex: ~/.codex/skills/{skill}/SKILL.md" in text
        assert "claude: ~/.claude/skills/{skill}/SKILL.md" in text
        assert "omp: ~/.omp/agent/skills/{skill}/SKILL.md" in text
        assert "missing local android_skills: <skill>" in text
        assert "missing local chrisbanes_skills: <skill>" in text
        assert "vendor_dir" not in text
        assert "native_loader" not in text
        assert ".agent-flow/vendor" not in text

    for path in policy_paths:
        text = path.read_text(encoding="utf-8")
        assert "~/.codex/skills/{skill}/SKILL.md" in text
        assert "~/.claude/skills/{skill}/SKILL.md" in text
        assert "~/.omp/agent/skills/{skill}/SKILL.md" in text
        assert "falling back to" not in text
        assert ".agent-flow/vendor/android-skills" not in text
        assert ".agent-flow/vendor/chrisbanes-skills" not in text

    kit_text = (KIT_ROOT / "bin" / "agent-flow-kit.mjs").read_text(encoding="utf-8")
    assert "missing local <group>: <skill>" in kit_text
