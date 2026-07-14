from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from unittest import mock

import pytest

from agent_flow.artifact import find_active_run
from agent_flow.cli import main
from agent_flow.core.skill_plan import (
    SkillPlanSnapshotError,
    installed_skill_plan_pin,
    resolve_runtime_skill_plan,
)


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
    process_env = dict(os.environ)
    process_env["HOME"] = str(project.parent / "test-home")
    process_env["AGENT_FLOW_AUTO_EXTERNAL_SKILLS"] = "1"
    if env is not None:
        process_env.update(env)
    return subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install", *args),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        env=process_env,
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


def _skill_with_metadata(path: Path, metadata: str, body: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        "---\n"
        f"name: {path.name}\n"
        f"description: Use when testing {path.name}.\n"
        f"{metadata}"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )


def _command(
    project: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process_env = dict(os.environ)
    process_env["AGENT_FLOW_AUTO_EXTERNAL_SKILLS"] = "1"
    if env is not None:
        process_env.update(env)
    return subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), *args),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        env=process_env,
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


def test_node_run_rejects_skill_index_tamper(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    started = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "run", "start", "--task", "pin"),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )
    assert started.returncode == 0, started.stderr
    index_path = project / ".agent-flow" / "skills" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["warnings"].append("tampered")
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")

    result = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "run", "status"),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "commitment" in result.stderr


def test_reinstall_commits_transaction_without_residue(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first = _install(project)
    second = _install(project)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert not (project / ".agent-flow" / "install-transaction").exists()
    assert not (project / ".agent-flow" / "install.lock").exists()
    assert (project / ".agent-flow" / "skills" / "index.json").is_file()


def test_unverified_existing_host_skill_is_preserved(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _skill(project / "skills" / "demo", "managed", hosts="[codex]")
    destination = project / ".Codex" / "skills" / "demo"
    destination.mkdir(parents=True)
    marker = destination / "SKILL.md"
    marker.write_text("user-owned\n", encoding="utf-8")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "user-owned\n"
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    assert any(link["status"] == "skipped-unverified-existing" for link in index["links"])


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
    assert "AGENT_FLOW_MANAGED_HOOK_SOURCE_PATH" in codex
    assert "AGENT_FLOW_MANAGED_HOOK_SOURCE_PATH" in claude
    assert 'runHook("guard-worktree-write.py"' in omp
    for reviewer in (
        project / ".Codex" / "agents" / "code-reviewer.md",
        project / ".claude" / "agents" / "code-reviewer.md",
        project / ".omp" / "agents" / "code-reviewer.md",
    ):
        assert reviewer.is_file()
        assert reviewer.read_text(encoding="utf-8").strip()
    kit = json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
    assert set(kit["managed_host_files"]["files"]) >= {
        ".Codex/agents/code-reviewer.md",
        ".claude/agents/code-reviewer.md",
        ".omp/agents/code-reviewer.md",
        ".omp/extensions/agent-flow-hooks.ts",
    }
    assert installed_skill_plan_pin(project)


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
    assert any(link["status"] == "skipped-unverified-existing" for link in index["links"])


def test_copied_host_skill_with_modified_auxiliary_file_is_not_overwritten(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "skills" / "demo"
    _skill(source, "v1", hosts="[codex]")
    assert _install(project, env={"AGENT_FLOW_TEST_FORCE_COPY_HOST_SKILLS": "1"}).returncode == 0
    destination = project / ".Codex" / "skills" / "demo"
    assert destination.is_dir() and not destination.is_symlink()
    (destination / "notes.md").write_text("user note\n", encoding="utf-8")
    _skill(source, "v2", hosts="[codex]")

    result = _install(project, "--force-managed")

    assert result.returncode == 0, result.stderr
    assert "v1" in (destination / "SKILL.md").read_text(encoding="utf-8")
    assert (destination / "notes.md").read_text(encoding="utf-8") == "user note\n"
    index_path = project / ".agent-flow" / "skills" / "index.json"
    updated = json.loads(index_path.read_text(encoding="utf-8"))
    assert any(link["status"] == "skipped-user-modified" for link in updated["links"])


def test_unmanaged_snapshot_matching_bundled_catalog_name_is_preserved(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    unmanaged = project / ".agent-flow" / "skills" / "adaptive"
    unmanaged.mkdir()
    (unmanaged / "SKILL.md").write_text(
        "---\nname: adaptive\ndescription: user snapshot\n---\nuser-owned\n",
        encoding="utf-8",
    )

    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert "user-owned" in (unmanaged / "SKILL.md").read_text(encoding="utf-8")
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    assert "adaptive" not in {skill["name"] for skill in index["skills"]}
    assert any("adaptive: preserved unmanaged skill entry" in warning for warning in index["warnings"])


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


def test_previous_explicit_selection_stays_fail_closed_after_drift(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    active = home / ".codex" / "skills" / "external"
    shared = home / ".agents" / "skills" / "external"
    _skill(active, "active")
    _skill(shared, "shared")
    env = {"HOME": str(home), "AGENT_FLOW_HOST": "codex"}
    first = _command(project, "install", "--skills", "external", env=env)
    assert first.returncode == 0, first.stderr
    index_path = project / ".agent-flow" / "skills" / "index.json"
    authenticated = index_path.read_bytes()
    installed = project / ".agent-flow" / "skills" / "external" / "SKILL.md"
    original = installed.read_bytes()
    (active / "SKILL.md").write_text(
        "---\nname: wrong-name\ndescription: invalid\n---\ninvalid\n",
        encoding="utf-8",
    )

    result = _command(project, "run", "status", env=env)

    assert result.returncode != 0
    assert "external" in result.stderr
    assert str(active) in result.stderr
    assert "name" in result.stderr
    assert index_path.read_bytes() == authenticated
    assert installed.read_bytes() == original


def test_filtered_install_exposes_new_project_catalog_skills_on_demand(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _skill(project / ".agent-flow" / "local-skills" / "private-demo", "private")
    _skill(project / "skills" / "project-demo", "project")

    result = _command(project, "install", "--profile", "python")

    assert result.returncode == 0, result.stderr
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    by_name = {skill["name"]: skill for skill in index["skills"]}
    assert by_name["private-demo"]["source"] == "local"
    assert by_name["project-demo"]["source"] == "project"
    runtime = {
        skill["name"]
        for skill in resolve_runtime_skill_plan(index, phase_id="implement", task_scope="unrelated")["skills"]
    }
    assert "private-demo" not in runtime
    assert "project-demo" not in runtime


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


def test_stale_cleanup_preserves_linked_to_directory_replacement(tmp_path: Path) -> None:
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
    assert codex_link.is_dir()
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    assert any(link["status"] == "preserved-kind-mismatch" for link in index["links"])


def test_stale_cleanup_preserves_directory_to_symlink_replacement(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "user-skill"
    project.mkdir()
    outside.mkdir()
    skill_dir = project / "skills" / "demo"
    _skill(skill_dir, "CODEX", hosts="[codex]")
    assert _install(project, env={"AGENT_FLOW_TEST_FORCE_COPY_HOST_SKILLS": "1"}).returncode == 0
    codex_link = project / ".Codex" / "skills" / "demo"
    index_path = project / ".agent-flow" / "skills" / "index.json"
    assert codex_link.is_dir() and not codex_link.is_symlink()
    shutil.rmtree(codex_link)
    codex_link.symlink_to(outside, target_is_directory=True)
    (skill_dir / "SKILL.md").unlink()

    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert codex_link.is_symlink()
    updated = json.loads(index_path.read_text(encoding="utf-8"))
    assert any(link["status"] == "preserved-unverified-ownership" for link in updated["links"])


def test_identical_unmanaged_directory_is_not_adopted(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / "skills" / "demo"
    _skill(skill_dir, "CODEX", hosts="[codex]")
    destination = project / ".Codex" / "skills" / "demo"
    destination.mkdir(parents=True)
    shutil.copy2(skill_dir / "SKILL.md", destination / "SKILL.md")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert destination.is_dir() and not destination.is_symlink()
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    assert any(link["status"] == "skipped-unverified-existing" for link in index["links"])


def test_unmanaged_skill_snapshot_is_preserved_without_adopting_ownership(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    unmanaged = project / ".agent-flow" / "skills" / "user-owned"
    unmanaged.mkdir()
    (unmanaged / "SKILL.md").write_text(
        "---\nname: user-owned\ndescription: User owned.\n---\nkeep me\n",
        encoding="utf-8",
    )

    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert "keep me" in (unmanaged / "SKILL.md").read_text(encoding="utf-8")
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    assert "user-owned" not in {skill["name"] for skill in index["skills"]}
    assert any("preserved unmanaged skill entry" in warning for warning in index["warnings"])


def test_skill_drift_reloads_add_change_rename_move_and_delete_at_command_boundary(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    active = home / ".codex" / "skills"
    shared = home / ".agents" / "skills"
    env = {"HOME": str(home), "AGENT_FLOW_HOST": "codex"}
    _skill(active / "alpha", "alpha-v1")
    first = _install(project, env=env)
    assert first.returncode == 0, first.stderr
    index_path = project / ".agent-flow" / "skills" / "index.json"
    first_index = json.loads(index_path.read_text(encoding="utf-8"))
    first_alpha = next(skill for skill in first_index["skills"] if skill["name"] == "alpha")
    assert first_alpha["source"] == "host-bootstrap"
    started = _command(
        project,
        "run",
        "start",
        "--task",
        "active drift",
        "--run-id",
        "active-drift",
        env=env,
    )
    assert started.returncode == 0, started.stderr
    state_path = project / ".agent-flow" / "state" / "current-run.json"
    first_state = json.loads(state_path.read_text(encoding="utf-8"))

    _skill(active / "alpha", "alpha-v2")
    _skill(active / "beta", "beta-v1")
    boundary = _command(project, "run", "status", env=env)
    assert boundary.returncode == 0, boundary.stderr
    assert "agent-flow installed" in boundary.stdout
    changed_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert changed_state["skill_plan_hash"] != first_state["skill_plan_hash"]
    changed = json.loads(index_path.read_text(encoding="utf-8"))
    changed_alpha = next(skill for skill in changed["skills"] if skill["name"] == "alpha")
    assert changed_alpha["tree_hash"] != first_alpha["tree_hash"]
    assert {"alpha", "beta"} <= {skill["name"] for skill in changed["skills"]}
    assert "alpha-v2" in (project / ".agent-flow" / "skills" / "alpha" / "SKILL.md").read_text(encoding="utf-8")

    (active / "alpha").rename(active / "renamed")
    renamed_skill = active / "renamed" / "SKILL.md"
    renamed_skill.write_text(renamed_skill.read_text(encoding="utf-8").replace("name: alpha", "name: renamed"), encoding="utf-8")
    assert _command(project, "run", "status", env=env).returncode == 0
    renamed = json.loads(index_path.read_text(encoding="utf-8"))
    names = {skill["name"] for skill in renamed["skills"]}
    assert "alpha" not in names and "renamed" in names

    shared.mkdir(parents=True)
    shutil.move(str(active / "renamed"), str(shared / "renamed"))
    assert _command(project, "run", "status", env=env).returncode == 0
    moved = json.loads(index_path.read_text(encoding="utf-8"))
    moved_skill = next(skill for skill in moved["skills"] if skill["name"] == "renamed")
    assert moved_skill["source"] == "shared"

    shutil.rmtree(shared / "renamed")
    assert _command(project, "run", "status", env=env).returncode == 0
    deleted = json.loads(index_path.read_text(encoding="utf-8"))
    assert "renamed" not in {skill["name"] for skill in deleted["skills"]}
    assert not (project / ".agent-flow" / "skills" / "renamed").exists()


def test_metadata_selector_drift_recalculates_the_next_runtime_plan(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    source = home / ".codex" / "skills" / "selector"
    env = {"HOME": str(home), "AGENT_FLOW_HOST": "codex"}
    _skill_with_metadata(
        source,
        "activation: conditional\nworkflowPhases: [implement]\ntaskTerms: [alpha]\n",
        "v1",
    )
    assert _install(project, env=env).returncode == 0
    index_path = project / ".agent-flow" / "skills" / "index.json"
    before = json.loads(index_path.read_text(encoding="utf-8"))
    assert "selector" in {
        skill["name"]
        for skill in resolve_runtime_skill_plan(before, phase_id="implement", task_scope="alpha")["skills"]
    }

    _skill_with_metadata(
        source,
        "activation: conditional\nworkflowPhases: [implement]\ntaskTerms: [beta]\n",
        "v2",
    )
    assert _command(project, "run", "status", env=env).returncode in {0, 1}
    after = json.loads(index_path.read_text(encoding="utf-8"))

    assert after["revision"] != before["revision"]
    assert "selector" not in {
        skill["name"]
        for skill in resolve_runtime_skill_plan(after, phase_id="implement", task_scope="alpha")["skills"]
    }
    assert "selector" in {
        skill["name"]
        for skill in resolve_runtime_skill_plan(after, phase_id="implement", task_scope="beta")["skills"]
    }


def test_python_active_run_pin_reloads_at_the_next_command_boundary(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    source = home / ".codex" / "skills" / "alpha"
    project.mkdir()
    env = {"HOME": str(home), "AGENT_FLOW_HOST": "codex"}
    _skill(source, "alpha-v1")
    assert _install(project, env=env).returncode == 0
    runtime_env = {
        **os.environ,
        **env,
        "AGENT_FLOW_ADAPTER": "generic",
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "1",
        "AGENT_FLOW_GENERIC_MODE": "emit",
        "PYTHONPATH": str(KIT_ROOT / "src"),
    }
    with mock.patch.dict(os.environ, runtime_env, clear=True):
        assert main(["run", "active drift", "--root", str(project)]) == 0
    active = find_active_run(project)
    assert active is not None
    previous = json.loads((active.path / "meta.json").read_text(encoding="utf-8"))

    _skill(source, "alpha-v2")
    assert _install(project, env=env).returncode == 0
    with mock.patch.dict(os.environ, runtime_env, clear=True):
        assert main(["continue", "--root", str(project)]) == 0
    reconciled = json.loads((active.path / "meta.json").read_text(encoding="utf-8"))

    assert reconciled["skill_plan_hash"] != previous["skill_plan_hash"]
    assert reconciled["skill_plan_repin_from"] == previous["skill_plan_hash"]


def test_dependency_drift_updates_transitive_runtime_closure(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    root = home / ".codex" / "skills"
    env = {"HOME": str(home), "AGENT_FLOW_HOST": "codex"}
    _skill(root / "dependency", "dependency")
    _skill_with_metadata(root / "consumer", "activation: always\ndependencies: [dependency]\n", "with dependency")
    assert _install(project, env=env).returncode == 0
    index_path = project / ".agent-flow" / "skills" / "index.json"
    before = json.loads(index_path.read_text(encoding="utf-8"))
    before_plan = resolve_runtime_skill_plan(before, phase_id="implement")
    assert {"consumer", "dependency"} <= {skill["name"] for skill in before_plan["skills"]}

    _skill_with_metadata(root / "consumer", "activation: always\n", "without dependency")
    assert _command(project, "run", "status", env=env).returncode in {0, 1}
    after = json.loads(index_path.read_text(encoding="utf-8"))
    after_plan = resolve_runtime_skill_plan(after, phase_id="implement")

    assert "consumer" in {skill["name"] for skill in after_plan["skills"]}
    assert "dependency" not in {skill["name"] for skill in after_plan["skills"]}


def test_install_lock_serializes_concurrent_installers(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["AGENT_FLOW_TEST_HOLD_INSTALL_LOCK_MS"] = "1200"
    first = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    lock_path = project / ".agent-flow" / "install.lock"
    for _ in range(100):
        if lock_path.exists():
            break
        time.sleep(0.01)
    second = _command(project, "install", env={"HOME": str(tmp_path / "home")})
    stdout, stderr = first.communicate(timeout=10)

    assert first.returncode == 0, stdout + stderr
    assert second.returncode != 0
    assert "project install lock is held" in second.stderr


def test_unindexed_existing_skills_failure_leaves_no_transaction_residue(tmp_path: Path) -> None:
    project = tmp_path / "project"
    user_skills = project / ".agent-flow" / "skills"
    user_skills.mkdir(parents=True)
    marker = user_skills / "user-owned.txt"
    marker.write_text("keep\n", encoding="utf-8")

    failed = _install(project)

    assert failed.returncode != 0
    assert "existing skills directory has no authenticated index" in failed.stderr
    assert marker.read_text(encoding="utf-8") == "keep\n"
    assert not (project / ".agent-flow" / "install-transaction").exists()

    shutil.rmtree(user_skills)
    retry = _install(project)
    assert retry.returncode == 0, retry.stderr


def test_recovery_survives_crash_after_moving_skills_directory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first = _install(project)
    assert first.returncode == 0, first.stderr
    index_path = project / ".agent-flow" / "skills" / "index.json"
    original = index_path.read_bytes()

    crashed = _command(
        project,
        "install",
        env={
            "HOME": str(tmp_path / "test-home"),
            "AGENT_FLOW_TEST_CRASH_AFTER_SKILLS_MOVE": "1",
        },
    )
    assert crashed.returncode == 86
    assert not index_path.exists()
    assert (project / ".agent-flow" / "install-transaction" / "skills-backup" / "index.json").read_bytes() == original

    recovered = _install(project)
    assert recovered.returncode == 0, recovered.stderr
    assert index_path.exists()
    assert not (project / ".agent-flow" / "install-transaction").exists()
    again = _install(project)
    assert again.returncode == 0, again.stderr


def test_recovery_survives_crash_between_skills_rename_and_journal_update(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    index_path = project / ".agent-flow" / "skills" / "index.json"
    authenticated = index_path.read_bytes()

    crashed = _command(
        project,
        "install",
        env={"AGENT_FLOW_TEST_CRASH_AFTER_SKILLS_RENAME": "1"},
    )

    assert crashed.returncode == 88
    transaction = project / ".agent-flow" / "install-transaction"
    journal = json.loads((transaction / "journal.json").read_text(encoding="utf-8"))
    assert journal["stage"] == "moving-skills"
    assert not index_path.exists()
    assert (transaction / "skills-backup" / "index.json").read_bytes() == authenticated

    recovered = _install(project)

    assert recovered.returncode == 0, recovered.stderr
    assert index_path.exists()
    assert not transaction.exists()


def test_recovery_rolls_back_initial_install_host_mutations_after_crash(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    env = {"HOME": str(home), "AGENT_FLOW_HOST": "codex"}
    source = home / ".codex" / "skills" / "external"
    _skill(source, "v1")

    crashed = _command(
        project,
        "install",
        env={**env, "AGENT_FLOW_TEST_CRASH_AFTER_SKILL_INDEX": "1"},
    )

    assert crashed.returncode == 87
    host_link = project / ".Codex" / "skills" / "external"
    assert host_link.is_symlink()
    assert (project / ".agent-flow" / "install-transaction").exists()
    shutil.rmtree(source)

    recovered = _install(project, env=env)

    assert recovered.returncode == 0, recovered.stderr
    assert not host_link.exists() and not host_link.is_symlink()
    assert not (project / ".agent-flow" / "install-transaction").exists()


def test_late_failure_restores_only_authenticated_previous_skill_index(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    env = {"HOME": str(home), "AGENT_FLOW_HOST": "codex"}
    _skill(home / ".codex" / "skills" / "external", "v1")
    assert _install(project, env=env).returncode == 0
    index_path = project / ".agent-flow" / "skills" / "index.json"
    authenticated = index_path.read_bytes()
    _skill(home / ".codex" / "skills" / "external", "v2")
    _skill(home / ".codex" / "skills" / "added", "new")

    failed = _command(
        project,
        "install",
        env={**env, "AGENT_FLOW_TEST_FAIL_AFTER_SKILL_INDEX": "1"},
    )

    assert failed.returncode != 0
    assert "injected failure after skill index" in failed.stderr
    assert index_path.read_bytes() == authenticated
    restored = (project / ".agent-flow" / "skills" / "external" / "SKILL.md").read_text(encoding="utf-8")
    assert "v1" in restored and "v2" not in restored
    assert not (project / ".Codex" / "skills" / "added").exists()


def test_late_failure_restores_all_managed_install_outputs(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    workflow_contract = project / ".agent-flow" / "rules" / "workflow-contract.md"
    workflow_contract.write_text("user baseline\n", encoding="utf-8")
    agents = project / "AGENTS.md"
    agents.write_text("user agents baseline\n", encoding="utf-8")
    watched = [
        project / ".agent-flow" / "skills" / "index.json",
        project / ".agent-flow" / "kit.json",
        workflow_contract,
        project / ".agent-flow" / "scripts" / "hooks" / "guard-worktree-write.py",
        project / ".Codex" / "hooks.json",
        project / ".claude" / "settings.json",
        project / ".omp" / "extensions" / "agent-flow-hooks.ts",
        project / ".gitignore",
        agents,
        project / "CLAUDE.md",
    ]
    before = {path: path.read_bytes() for path in watched}

    failed = _command(
        project,
        "install",
        "--force-managed",
        env={"AGENT_FLOW_TEST_FAIL_AFTER_MANAGED_INSTALL": "1"},
    )

    assert failed.returncode != 0
    assert "injected failure after managed install" in failed.stderr
    assert {path: path.read_bytes() for path in watched} == before
    assert not (project / ".agent-flow" / "install-transaction").exists()


def test_external_managed_output_replacement_is_preserved_on_rollback(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    agents = project / "AGENTS.md"
    replacement = b"external replacement\n"
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["AGENT_FLOW_TEST_HOLD_AFTER_MANAGED_INSTALL_SEAL_MS"] = "1500"
    env["AGENT_FLOW_TEST_FAIL_AFTER_MANAGED_INSTALL"] = "1"
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stderr is not None
    marker = process.stderr.readline()
    assert "agent-flow:test-managed-install-sealed" in marker
    agents.write_bytes(replacement)
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode != 0, stdout
    assert "changed outside transaction" in stderr
    assert agents.read_bytes() == replacement


def test_external_change_to_untouched_managed_path_is_rejected_before_seal(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    external = project / "scripts" / "external.txt"
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["AGENT_FLOW_TEST_HOLD_BEFORE_MANAGED_INSTALL_SEAL_MS"] = "1500"
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stderr is not None
    marker = process.stderr.readline()
    assert "agent-flow:test-managed-install-before-seal" in marker
    external.parent.mkdir()
    external.write_text("external\n", encoding="utf-8")
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode != 0, stdout
    assert "changed outside transaction: scripts" in stderr
    assert external.read_text(encoding="utf-8") == "external\n"


def test_failed_install_does_not_mutate_codex_trust_config(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    config = home / ".codex" / "config.toml"
    project.mkdir()
    config.parent.mkdir(parents=True)
    config.write_text("model = \"test\"\n", encoding="utf-8")

    failed = _install(
        project,
        env={
            "HOME": str(home),
            "AGENT_FLOW_TEST_FAIL_AFTER_MANAGED_INSTALL": "1",
        },
    )

    assert failed.returncode != 0
    assert config.read_text(encoding="utf-8") == "model = \"test\"\n"


def test_omp_reviewer_tamper_invalidates_installed_plan(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    reviewer = project / ".omp" / "agents" / "code-reviewer.md"
    reviewer.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(SkillPlanSnapshotError, match="managed host file changed"):
        installed_skill_plan_pin(project)


def test_late_failure_restores_stale_host_link_removed_by_transaction(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    env = {"HOME": str(home), "AGENT_FLOW_HOST": "codex"}
    source = home / ".codex" / "skills" / "external"
    _skill(source, "v1")
    assert _install(project, env=env).returncode == 0
    host_link = project / ".Codex" / "skills" / "external"
    assert host_link.is_symlink()
    shutil.rmtree(source)

    failed = _command(
        project,
        "install",
        env={**env, "AGENT_FLOW_TEST_FAIL_AFTER_SKILL_INDEX": "1"},
    )

    assert failed.returncode != 0
    assert host_link.is_symlink()
    assert (host_link / "SKILL.md").exists()


def test_index_replacement_after_auth_is_not_backed_up_or_restored_as_trusted(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    index_path = project / ".agent-flow" / "skills" / "index.json"
    authenticated = index_path.read_bytes()
    tampered_payload = json.loads(authenticated)
    tampered_payload["warnings"].append("untrusted replacement")
    tampered = (json.dumps(tampered_payload, indent=2) + "\n").encode()
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "test-home")
    env["AGENT_FLOW_TEST_HOLD_AFTER_INDEX_AUTH_MS"] = "1500"
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stderr is not None
    marker = process.stderr.readline()
    assert "agent-flow:test-index-authenticated" in marker
    index_path.write_bytes(tampered)
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode != 0, stdout
    assert "backup was not adopted" in stderr
    transaction = project / ".agent-flow" / "install-transaction"
    backup_index = transaction / "skills-backup" / "index.json"
    journal = json.loads((transaction / "journal.json").read_text(encoding="utf-8"))
    assert backup_index.read_bytes() == tampered
    assert base64.b64decode(journal["previous_index_bytes"]) == authenticated
    assert not index_path.exists()

    retry = _install(project)
    assert retry.returncode != 0
    assert "backup is not authenticated" in retry.stderr
    assert backup_index.read_bytes() == tampered


def test_claude_codex_and_omp_share_index_revision_and_real_host_exposure_paths(tmp_path: Path) -> None:
    revisions: set[str] = set()
    for host in ("claude", "codex", "omp"):
        project = tmp_path / f"project-{host}"
        home = tmp_path / f"home-{host}"
        project.mkdir()
        host_root = {
            "claude": home / ".claude" / "skills",
            "codex": home / ".codex" / "skills",
            "omp": home / ".omp" / "agent" / "skills",
        }[host]
        _skill(host_root / "parity-skill", "identical bytes")
        result = _install(
            project,
            "--skills",
            "parity-skill",
            env={"HOME": str(home), "AGENT_FLOW_HOST": host},
        )
        assert result.returncode == 0, result.stderr
        index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
        revisions.add(index["revision"])
        selected = next(skill for skill in index["skills"] if skill["name"] == "parity-skill")
        assert selected["source"] == "host-bootstrap"
        assert selected["source_host"] == host
        assert (project / ".Codex" / "skills" / "parity-skill" / "SKILL.md").exists()
        assert (project / ".claude" / "skills" / "parity-skill" / "SKILL.md").exists()
        assert (project / ".omp" / "skills" / "parity-skill" / "SKILL.md").exists()

    assert len(revisions) == 1


def test_installed_skill_plan_is_committed_in_kit_and_tamper_fails(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    kit = json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))

    pin = installed_skill_plan_pin(project)

    assert pin["skill_plan_hash"] == kit["skill_plan_hash"]
    index_path = project / ".agent-flow" / "skills" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["selection"]["explicit_skills"] = ["forged"]
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(SkillPlanSnapshotError, match="matches kit.json"):
        installed_skill_plan_pin(project)


def test_forged_prior_link_cannot_claim_or_delete_user_owned_target(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    user_target = project / ".Codex" / "skills" / "user-owned"
    user_target.mkdir(parents=True)
    marker = user_target / "notes.txt"
    marker.write_text("user data\n", encoding="utf-8")

    index_path = project / ".agent-flow" / "skills" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["links"].append(
        {
            "name": "forged-owner",
            "host": "codex",
            "path": ".Codex/skills/user-owned",
            "status": "copied",
            "filesystem_kind": "directory",
            "tree_hash": "0" * 64,
        }
    )
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")

    result = _install(project)

    assert result.returncode == 1
    assert "previous skill index does not match kit commitment" in result.stderr
    assert marker.read_text(encoding="utf-8") == "user data\n"
    assert user_target.is_dir()


def test_host_skill_root_symlink_fails_without_writing_outside_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / ".Codex").symlink_to(outside, target_is_directory=True)
    _skill(project / "skills" / "demo", "CODEX", hosts="[codex]")

    result = _install(project)

    assert result.returncode != 0
    assert "managed host file is missing or unsafe" in result.stderr
    assert not (outside / "skills" / "demo").exists()
    assert not (outside / "agents" / "code-reviewer.md").exists()


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
    code_generation_skill = (
        project / ".agent-flow" / "skills" / "code-generation-discipline" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "missing local <group>: <skill>" in code_generation_skill
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

    workflow_text = (KIT_ROOT / "workflows" / "full-feature.yaml").read_text(encoding="utf-8")
    assert "missing local <group>: <skill>" in workflow_text
