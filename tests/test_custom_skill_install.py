from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agent_flow.core.skill_plan import installed_skill_plan_pin, resolve_runtime_skill_plan


KIT_ROOT = Path(__file__).resolve().parent.parent
HOST_SKILL_DIRS = {
    "claude": ".claude",
    "codex": ".agents",
    "omp": ".omp",
}


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(path for path in root.rglob("*") if path.is_file()):
        digest.update(file.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(file.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


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


def _host_skill(project: Path, host: str, name: str) -> Path:
    return project / HOST_SKILL_DIRS[host] / "skills" / name


def test_project_skill_links_all_hosts_and_index_omits_body(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _skill(project / "skills" / "my-skill", "BODY SHOULD NOT BE IN INDEX")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    host_roots = {
        "claude": project / ".claude" / "skills",
        "codex": project / ".agents" / "skills",
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
        "comment-authoring-discipline",
        "comment-checker",
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
    assert host_skills <= indexed
    assert {
        "full-feature-workflow",
        "architecture-reviewer",
        "push-watch",
        "clean-architecture-core",
    } <= indexed
    assert {
        "android-clean-architecture",
        "ios-clean-architecture",
        "react-clean-architecture",
        "react-native-clean-architecture",
        "python-api-clean-architecture",
    }.isdisjoint(indexed)
    assert matt_skill_closure <= indexed
    assert {link["name"] for link in index["links"]} == host_skills
    assert (project / ".agent-flow" / "skills" / "domain-modeling" / "SKILL.md").exists()
    assert (project / ".agent-flow" / "skills" / "full-feature-workflow" / "SKILL.md").exists()
    for host_dir in (".agents", ".claude"):
        for skill in matt_skill_closure:
            assert not (project / host_dir / "skills" / skill).exists()
    assert not (project / ".agents" / "skills" / "full-feature-workflow").exists()


def test_generic_profile_installs_clean_architecture_core_without_platform_adapters(tmp_path: Path) -> None:
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
    assert platform_skills.isdisjoint(skills)
    assert skills["clean-architecture"]["requires"] == ["clean-architecture-core"]
    assert not any("missing required skill" in warning for warning in index["warnings"])

    core = (
        project / ".agent-flow" / "skills" / "clean-architecture-core" / "SKILL.md"
    ).read_text(encoding="utf-8")
    alias = (
        project / ".agent-flow" / "skills" / "clean-architecture" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "repository-impl-direct-api-service: pass|fail" in core
    assert "Compatibility Alias" in alias
    assert "Samantha" not in core + alias
    assert "http://" not in core + alias
    assert "https://" not in core + alias


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
    assert index["selection"]["profile_routing"]["version"] == 1
    assert index["selection"]["required_review"]["android"] == [
        "android-clean-architecture",
        "android-code-review",
    ]
    assert "compose-state-authoring" in index["selection"]["conditional_skills"]["android"]["review"]
    for name in ("agp-9-upgrade", "android-intent-security", "play-policy-insights"):
        assert name in index["selection"]["conditional_skills"]["android"]["implementation"]
        assert name in index["selection"]["conditional_skills"]["android"]["review"]
    assert "clean-architecture-core" in names
    assert matt_skill_closure <= names
    assert "android-clean-architecture" in names
    assert "android-code-review" in names
    assert {"agp-9-upgrade", "android-intent-security", "play-policy-insights"} <= names
    assert "jetpack-compose-m3" in names
    assert "react-native-clean-architecture" not in names
    assert "ios-clean-architecture" not in names
    assert not (project / ".agent-flow" / "skills" / "react-native-clean-architecture").exists()

    lock_path = project / ".agent-flow" / "skills" / "upstream-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    del lock["android_official"]
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    status = subprocess.run(
        (
            str(project / ".agent-flow" / "bin" / "agent-flow"),
            "architecture-lint",
            "--profile",
            "android",
        ),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "AGENT_FLOW_HOST": "claude"},
    )
    assert status.returncode != 0
    assert "installed Android official skill lock is missing" in status.stderr


def test_overlong_active_host_skill_uses_self_contained_bundled_fallback(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    _skill(
        home / ".codex" / "skills" / "adaptive",
        "\n".join(f"host line {index}" for index in range(201)),
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
    }

    result = _install(project, "--skill", "adaptive", env=env)

    assert result.returncode == 0, result.stderr
    index = json.loads(
        (project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8")
    )
    adaptive = next(skill for skill in index["skills"] if skill["name"] == "adaptive")
    assert adaptive["source"] == "bundled"
    assert adaptive["source_host"] is None
    installed = project / ".agent-flow" / "skills" / "adaptive"
    main = (installed / "SKILL.md").read_text(encoding="utf-8")
    assert len(main.splitlines()) <= 200
    assert "/Users/" not in main
    assert "http://" not in main
    assert "https://" not in main
    assert {
        path.name for path in (installed / "references").iterdir() if path.is_file()
    } >= {
        "workflow-and-navigation.md",
        "navigation3-multi-pane.md",
        "layouts-and-capabilities.md",
        "app-bars.md",
    }


def test_missing_pinned_snapshot_cannot_switch_to_another_host(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    codex_source = home / ".codex" / "skills" / "host-only"
    _skill(codex_source, "CODEX SOURCE")
    codex_env = {
        **os.environ,
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "AGENT_FLOW_HOST": "codex",
        "AGENT_FLOW_SKIP_CODEX_TRUST": "1",
    }
    first = _install(project, "--skill", "host-only", env=codex_env)
    assert first.returncode == 0, first.stderr

    snapshot = project / ".agent-flow" / "skills" / "host-only"
    shutil.rmtree(snapshot)
    shutil.rmtree(codex_source)
    _skill(home / ".claude" / "skills" / "host-only", "CLAUDE REPLACEMENT")
    claude_env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "AGENT_FLOW_HOST": "claude",
        "AGENT_FLOW_SKIP_CODEX_TRUST": "1",
    }

    result = _install(project, "--skill", "host-only", env=claude_env)

    assert result.returncode != 0
    assert "skill snapshot replacement from another host is denied: host-only" in result.stderr
    index = json.loads(
        (project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8")
    )
    selected = next(skill for skill in index["skills"] if skill["name"] == "host-only")
    assert selected["source_host"] == "codex"


def test_missing_pinned_snapshot_recovers_only_exact_original_host_bytes(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    codex_source = home / ".codex" / "skills" / "host-only"
    _skill(codex_source, "CODEX SOURCE")
    env = {
        **os.environ,
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "AGENT_FLOW_HOST": "codex",
        "AGENT_FLOW_SKIP_CODEX_TRUST": "1",
    }
    first = _install(project, "--skill", "host-only", env=env)
    assert first.returncode == 0, first.stderr
    snapshot = project / ".agent-flow" / "skills" / "host-only"
    expected_hash = _tree_hash(snapshot)

    shutil.rmtree(snapshot)
    recovered = _install(project, "--skill", "host-only", env=env)

    assert recovered.returncode == 0, recovered.stderr
    assert _tree_hash(snapshot) == expected_hash
    kit = json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
    assert installed_skill_plan_pin(project)["skill_plan_hash"] == kit["skill_plan_hash"]

    shutil.rmtree(snapshot)
    _skill(codex_source, "CHANGED CODEX SOURCE")
    changed = _install(project, "--skill", "host-only", env=env)

    assert changed.returncode != 0
    assert "pinned host skill snapshot changed: host-only" in changed.stderr
    assert not snapshot.exists()


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


def test_explicit_reinstall_replaces_previously_selected_profiles(tmp_path: Path) -> None:
    project = tmp_path / "mixed-project"
    project.mkdir()

    first = _install(project, "--profile", "android", "--profile", "react-native")
    assert first.returncode == 0, first.stderr
    second = _install(project, "--profile", "android")
    assert second.returncode == 0, second.stderr

    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    names = {skill["name"] for skill in index["skills"]}
    assert index["selection"]["profiles"] == ["android"]
    assert "android-clean-architecture" in names
    assert "react-native-clean-architecture" not in names


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


def test_react_project_auto_selects_react_profile_skills(tmp_path: Path) -> None:
    project = tmp_path / "react-project"
    project.mkdir()
    (project / "package.json").write_text('{"dependencies":{"react":"latest"}}\n', encoding="utf-8")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    names = {skill["name"] for skill in index["skills"]}
    assert index["selection"]["profiles"] == ["react"]
    assert "react-development-guide" in names
    assert "react-clean-presentation-architecture" in names
    assert "react-native-development-guide" not in names
    assert "android-code-review" not in names


@pytest.mark.parametrize(
    "dependency",
    ("express", "@nestjs/core", "fastify", "koa", "@hapi/hapi", "hapi"),
)
def test_typescript_node_backend_installs_and_routes_node_guide(
    tmp_path: Path,
    dependency: str,
) -> None:
    project = tmp_path / "node-typescript-project"
    project.mkdir()
    (project / "package.json").write_text(
        json.dumps({"dependencies": {dependency: "latest"}}),
        encoding="utf-8",
    )
    (project / "tsconfig.json").write_text("{}\n", encoding="utf-8")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    index = json.loads(
        (project / ".agent-flow" / "skills" / "index.json").read_text(
            encoding="utf-8"
        )
    )
    assert index["selection"]["profiles"] == ["node"]
    assert "node-development-guide" in {
        skill["name"] for skill in index["skills"]
    }
    plan = resolve_runtime_skill_plan(
        index,
        "review",
        ["src/server.ts"],
        "Fix the Node.js API",
    )
    assert plan["touched_profiles"] == ["node"]
    assert "node-development-guide" in {
        skill["name"] for skill in plan["skills"]
    }


def test_plain_typescript_install_stays_typescript(tmp_path: Path) -> None:
    project = tmp_path / "typescript-project"
    project.mkdir()
    (project / "package.json").write_text(
        json.dumps({"devDependencies": {"typescript": "latest"}}),
        encoding="utf-8",
    )
    (project / "tsconfig.json").write_text("{}\n", encoding="utf-8")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    index = json.loads(
        (project / ".agent-flow" / "skills" / "index.json").read_text(
            encoding="utf-8"
        )
    )
    assert index["selection"]["profiles"] == ["typescript"]
    assert "typescript-development-guide" in {
        skill["name"] for skill in index["skills"]
    }
    assert "node-development-guide" not in {
        skill["name"] for skill in index["skills"]
    }


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
    for host_root in HOST_SKILL_DIRS.values():
        assert (project / host_root / "skills" / "dependency-skill" / "SKILL.md").exists()


def test_external_skill_snapshot_preserves_binary_assets(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    shared = home / ".agents" / "skills" / "binary-skill"
    _skill(shared, "binary asset")
    asset = bytes([0, 255, 128, 10, 13, 42])
    (shared / "asset.bin").write_bytes(asset)

    result = _install(
        project,
        "--skill",
        "binary-skill",
        env={**os.environ, "HOME": str(home)},
    )

    assert result.returncode == 0, result.stderr
    snapshot = project / ".agent-flow" / "skills" / "binary-skill"
    assert (snapshot / "asset.bin").read_bytes() == asset
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    selected = next(skill for skill in index["skills"] if skill["name"] == "binary-skill")
    assert selected["source"] == "shared"
    assert len(selected["tree_hash"]) == 64
    first_hash = json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))["skill_plan_hash"]

    reinstall = _install(
        project,
        "--skill",
        "binary-skill",
        env={**os.environ, "HOME": str(home)},
    )

    assert reinstall.returncode == 0, reinstall.stderr
    second_kit = json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
    second_index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    assert second_kit["skill_plan_hash"] == first_hash
    assert next(skill for skill in second_index["skills"] if skill["name"] == "binary-skill")["source"] == "shared"


def test_reinstall_atomically_upgrades_unchanged_managed_bundled_skill(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    old_kit = tmp_path / "old-kit"
    shutil.copytree(
        KIT_ROOT,
        old_kit,
        ignore=shutil.ignore_patterns(
            ".agent-flow",
            ".git",
            ".venv",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
        ),
    )
    old_skill = old_kit / "skills" / "grilling" / "SKILL.md"
    old_skill.write_text(
        "---\nname: grilling\ndescription: Old managed copy. Use when testing upgrades.\n---\nold\n",
        encoding="utf-8",
    )
    old_lock_path = old_kit / "skills" / "upstream-lock.json"
    old_lock = json.loads(old_lock_path.read_text(encoding="utf-8"))
    old_lock["project_tree_hashes"]["grilling"] = _tree_hash(old_skill.parent)
    old_lock_path.write_text(
        json.dumps(old_lock, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "AGENT_FLOW_HOST": "codex",
        "AGENT_FLOW_SKIP_CODEX_TRUST": "1",
        "PYTHON": sys.executable,
        "PYTHONPATH": os.pathsep.join(
            [str(Path(yaml.__file__).resolve().parent.parent), str(old_kit / "src")]
        ),
    }
    first = subprocess.run(
        (_node(), str(old_kit / "bin" / "agent-flow-kit.mjs"), "install", "--skill", "grilling"),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert first.returncode == 0, first.stderr

    snapshot = project / ".agent-flow" / "skills" / "grilling"
    old_hash = _tree_hash(snapshot)

    reinstall = _install(project, "--skill", "grilling", env=env)

    assert reinstall.returncode == 0, reinstall.stderr
    assert (snapshot / "SKILL.md").read_bytes() == (
        KIT_ROOT / "skills" / "grilling" / "SKILL.md"
    ).read_bytes()
    index_path = project / ".agent-flow" / "skills" / "index.json"
    next_index = json.loads(index_path.read_text(encoding="utf-8"))
    next_selected = next(skill for skill in next_index["skills"] if skill["name"] == "grilling")
    assert next_selected["source"] == "bundled"
    assert next_selected["tree_hash"] == _tree_hash(snapshot)
    assert next_selected["tree_hash"] != old_hash
    assert json.loads(
        (project / ".agent-flow" / "skills" / "upstream-lock.json").read_text(encoding="utf-8")
    )["commit"] == json.loads((KIT_ROOT / "skills" / "upstream-lock.json").read_text(encoding="utf-8"))["commit"]


def test_react_native_android_directory_installs_conditional_android_profile(tmp_path: Path) -> None:
    project = tmp_path / "rn-project"
    project.mkdir()
    (project / "package.json").write_text('{"dependencies":{"react-native":"latest"}}\n', encoding="utf-8")
    (project / "android").mkdir()

    result = _install(project)

    assert result.returncode == 0, result.stderr
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    names = {skill["name"] for skill in index["skills"]}
    assert index["selection"]["profiles"] == ["react-native"]
    assert index["selection"]["skill_profiles"] == ["android", "react-native"]
    assert "react-native-development-guide" in names
    assert "android-code-review" in names


def test_react_native_ios_directory_installs_conditional_ios_profile(tmp_path: Path) -> None:
    project = tmp_path / "rn-project"
    project.mkdir()
    (project / "package.json").write_text('{"dependencies":{"react-native":"latest"}}\n', encoding="utf-8")
    (project / "ios").mkdir()

    result = _install(project)

    assert result.returncode == 0, result.stderr
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    names = {skill["name"] for skill in index["skills"]}
    assert index["selection"]["profiles"] == ["react-native"]
    assert index["selection"]["skill_profiles"] == ["ios", "react-native"]
    assert "react-native-development-guide" in names
    assert "ios-clean-architecture" in names


def test_cross_priority_local_skill_authority_beats_project_and_bundled(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    project_skill = project / "skills" / "node-development-guide"
    _skill(project_skill, "PROJECT")
    project_skill_path = project_skill / "SKILL.md"
    project_skill_path.write_text(
        project_skill_path.read_text(encoding="utf-8").replace(
            "name: node-development-guide",
            "name: Node-Development-Guide",
            1,
        ),
        encoding="utf-8",
    )
    _skill(project / ".agent-flow" / "local-skills" / "node-development-guide", "LOCAL")

    result = _install(project, "--skill", "node-development-guide")

    assert result.returncode == 0, result.stderr
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    selected = next(skill for skill in index["skills"] if skill["name"] == "node-development-guide")
    assert selected["source"] == "local"
    assert selected["path"] == ".agent-flow/local-skills/node-development-guide/SKILL.md"
    conflict = next(
        conflict for conflict in index["conflicts"] if conflict["name"] == "node-development-guide"
    )
    assert conflict["selected"] == ".agent-flow/local-skills/node-development-guide/SKILL.md"
    assert "skills/node-development-guide/SKILL.md" in conflict["ignored"]


@pytest.mark.parametrize(
    ("local_body", "project_body", "expected_source"),
    (("LOCAL", "PROJECT", "local"), (None, "PROJECT", "project"), (None, None, "bundled")),
)
def test_content_skill_follows_local_project_bundled_authority(
    tmp_path: Path,
    local_body: str | None,
    project_body: str | None,
    expected_source: str,
) -> None:
    project = tmp_path / expected_source
    project.mkdir()
    if local_body is not None:
        _skill(project / ".agent-flow" / "local-skills" / "domain-modeling", local_body)
    if project_body is not None:
        _skill(project / "skills" / "domain-modeling", project_body)
    home = tmp_path / f"home-{expected_source}"
    home.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "AGENT_FLOW_HOST",
            "CLAUDECODE",
            "CLAUDE_CLI",
            "CLAUDE_CONFIG_DIR",
            "CODEX_CLI",
            "CODEX_HOME",
            "CODEX_SHELL",
            "CODEX_THREAD_ID",
            "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
            "OMP_PROFILE",
            "PI_CODING_AGENT_DIR",
        }
    }
    env["HOME"] = str(home)
    env["AGENT_FLOW_SKIP_CODEX_TRUST"] = "1"

    result = _install(project, "--skill", "domain-modeling", env=env)

    assert result.returncode == 0, result.stderr
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    selected = next(skill for skill in index["skills"] if skill["name"] == "domain-modeling")
    assert selected["source"] == expected_source
    selected_text = (project / selected["path"]).read_text(encoding="utf-8")
    if expected_source == "local":
        assert "LOCAL" in selected_text
    elif expected_source == "project":
        assert "PROJECT" in selected_text
    else:
        assert selected_text == (KIT_ROOT / "skills" / "domain-modeling" / "SKILL.md").read_text(
            encoding="utf-8"
        )


def test_external_skill_layers_cannot_override_agent_flow_control_skill(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    project.mkdir()
    home.mkdir()
    codex_home.mkdir()
    _skill(home / ".agents" / "skills" / "agent-flow", "SHARED OVERRIDE")
    _skill(codex_home / "skills" / "agent-flow", "HOST OVERRIDE")
    env = {
        **os.environ,
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "AGENT_FLOW_SKIP_CODEX_TRUST": "1",
    }

    result = _install(project, env=env)

    assert result.returncode == 0, result.stderr
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    selected = next(skill for skill in index["skills"] if skill["name"] == "agent-flow")
    assert selected["source"] == "bundled"
    installed = (project / selected["path"]).read_text(encoding="utf-8")
    assert "SHARED OVERRIDE" not in installed
    assert "HOST OVERRIDE" not in installed


def test_user_host_copy_cannot_shadow_agent_flow_control_skill(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    user_copy = project / ".agents" / "skills" / "agent-flow" / "SKILL.md"
    user_copy.parent.mkdir(parents=True)
    user_bytes = b"user-owned agent-flow host copy\n"
    user_copy.write_bytes(user_bytes)

    normal = _install(project)
    assert normal.returncode != 0
    assert "user-modified project skill host tree differs" in normal.stderr
    assert user_copy.read_bytes() == user_bytes

    forced = _install(project, "--force-managed")
    assert forced.returncode != 0
    assert "user-modified project skill host tree differs" in forced.stderr
    assert user_copy.read_bytes() == user_bytes
    assert not _host_skill(project, "claude", "agent-flow").exists()
    assert not _host_skill(project, "omp", "agent-flow").exists()


@pytest.mark.parametrize("installer", ("agent-flow-kit.mjs", "agent-flow-install.mjs"))
@pytest.mark.parametrize("reserved_name", ("agent-flow", "full-feature-workflow"))
def test_installers_reject_project_overrides_of_reserved_workflow_skills(
    tmp_path: Path,
    installer: str,
    reserved_name: str,
) -> None:
    project = tmp_path / f"reserved-{reserved_name}-{installer.removesuffix('.mjs')}"
    project.mkdir()
    skill = project / ".agent-flow" / "local-skills" / "workflow-alias"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        f"name: {reserved_name}\n"
        "description: Must not replace the workflow control skill.\n"
        "---\n"
        "\n# Replacement\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / installer), "install"),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "reserved workflow skill name cannot be overridden" in result.stderr
    assert not (project / ".agent-flow" / "workflows").exists()
    assert not (project / ".agent-flow" / "skills" / "index.json").exists()
    assert not (project / ".agent-flow" / "kit.json").exists()


@pytest.mark.parametrize("installer", ("agent-flow-kit.mjs", "agent-flow-install.mjs"))
def test_installers_reject_same_priority_casefolded_local_skill_names(
    tmp_path: Path,
    installer: str,
) -> None:
    project = tmp_path / installer.removesuffix(".mjs")
    project.mkdir()
    for directory, name in (("first", "canonical-policy"), ("second", "Canonical-Policy")):
        skill = project / ".agent-flow" / "local-skills" / directory
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            "description: Use when testing canonical local skill names.\n"
            "---\n"
            "\n# Policy\n",
            encoding="utf-8",
        )

    result = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / installer), "install"),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "conflicting project-local skill paths: canonical-policy" in result.stderr
    assert not (project / ".agent-flow" / "workflows").exists()
    assert not (project / ".agent-flow" / "skills" / "index.json").exists()
    assert not (project / ".agent-flow" / "kit.json").exists()


def test_host_limited_project_skill_is_exposed_to_every_host(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _skill(project / "skills" / "codex-only", "CODEX", hosts="[codex]")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    for host_root in HOST_SKILL_DIRS.values():
        assert (project / host_root / "skills" / "codex-only" / "SKILL.md").exists()


def test_omp_limited_project_skill_is_exposed_to_every_host(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _skill(project / "skills" / "omp-only", "OMP", hosts="[omp]")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    for host_root in HOST_SKILL_DIRS.values():
        assert (project / host_root / "skills" / "omp-only" / "SKILL.md").exists()



def test_host_limited_yaml_block_list_is_exposed_to_every_host(tmp_path: Path) -> None:
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
    for host_root in HOST_SKILL_DIRS.values():
        assert (project / host_root / "skills" / "codex-block-list" / "SKILL.md").exists()


@pytest.mark.parametrize("host", tuple(HOST_SKILL_DIRS))
def test_existing_user_modified_skill_blocks_all_host_exposure(
    tmp_path: Path,
    host: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _skill(project / "skills" / "my-skill", "PROJECT")
    dest = _host_skill(project, host, "my-skill")
    dest.mkdir(parents=True)
    user_bytes = b"user modified\n"
    (dest / "SKILL.md").write_bytes(user_bytes)

    for args in ((), ("--force-managed",)):
        result = _install(project, *args)

        assert result.returncode != 0
        assert "user-modified project skill host tree differs" in result.stderr
        assert (dest / "SKILL.md").read_bytes() == user_bytes
        for other_host in HOST_SKILL_DIRS:
            if other_host != host:
                assert not _host_skill(project, other_host, "my-skill").exists()


@pytest.mark.parametrize("host", tuple(HOST_SKILL_DIRS))
def test_noncanonical_host_symlink_blocks_all_host_exposure(
    tmp_path: Path,
    host: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "skills" / "my-skill"
    _skill(source, "PROJECT")
    dest = _host_skill(project, host, "my-skill")
    dest.parent.mkdir(parents=True)
    dest.symlink_to(source, target_is_directory=True)
    original_target = os.readlink(dest)

    result = _install(project)

    assert result.returncode != 0
    assert "noncanonical or user-modified project skill host symlink" in result.stderr
    assert dest.is_symlink()
    assert os.readlink(dest) == original_target
    for other_host in HOST_SKILL_DIRS:
        if other_host != host:
            assert not _host_skill(project, other_host, "my-skill").exists()


def test_host_directory_without_skill_blocks_all_host_exposure(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _skill(project / "skills" / "my-skill", "PROJECT")
    dest = _host_skill(project, "omp", "my-skill")
    dest.mkdir(parents=True)
    user_file = dest / "notes.txt"
    user_bytes = b"user-owned directory\n"
    user_file.write_bytes(user_bytes)

    result = _install(project)

    assert result.returncode != 0
    assert "host directory has no regular SKILL.md" in result.stderr
    assert user_file.read_bytes() == user_bytes
    assert not _host_skill(project, "claude", "my-skill").exists()
    assert not _host_skill(project, "codex", "my-skill").exists()


def test_canonical_host_links_and_whole_tree_copies_reinstall_idempotently(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "skills" / "my-skill"
    _skill(source, "PROJECT")
    references = source / "references"
    references.mkdir()
    (references / "policy.md").write_text("nested policy\n", encoding="utf-8")
    first = _install(project)
    assert first.returncode == 0, first.stderr

    claude = _host_skill(project, "claude", "my-skill")
    omp = _host_skill(project, "omp", "my-skill")
    claude_target = os.readlink(claude)
    omp_target = os.readlink(omp)
    codex = _host_skill(project, "codex", "my-skill")
    codex.unlink()
    shutil.copytree(source, codex)

    second = _install(project)

    assert second.returncode == 0, second.stderr
    assert claude.is_symlink() and os.readlink(claude) == claude_target
    assert omp.is_symlink() and os.readlink(omp) == omp_target
    assert codex.is_dir() and not codex.is_symlink()
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    selected = next(skill for skill in index["skills"] if skill["name"] == "my-skill")
    assert selected["tree_hash"] == _tree_hash(source)
    for host in HOST_SKILL_DIRS:
        exposed = _host_skill(project, host, "my-skill")
        assert _tree_hash(exposed.resolve()) == selected["tree_hash"]


def test_nested_host_copy_modification_blocks_reinstall_without_partial_updates(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "skills" / "my-skill"
    _skill(source, "PROJECT")
    (source / "references").mkdir()
    (source / "references" / "policy.md").write_text("canonical\n", encoding="utf-8")
    first = _install(project)
    assert first.returncode == 0, first.stderr

    codex = _host_skill(project, "codex", "my-skill")
    codex.unlink()
    shutil.copytree(source, codex)
    nested = codex / "references" / "policy.md"
    user_bytes = b"user nested edit\n"
    nested.write_bytes(user_bytes)
    other_targets = {
        host: os.readlink(_host_skill(project, host, "my-skill"))
        for host in ("claude", "omp")
    }

    result = _install(project)

    assert result.returncode != 0
    assert "user-modified project skill host tree differs" in result.stderr
    assert nested.read_bytes() == user_bytes
    for host, target in other_targets.items():
        exposed = _host_skill(project, host, "my-skill")
        assert exposed.is_symlink()
        assert os.readlink(exposed) == target


def test_tampered_previous_tree_hash_cannot_authorize_host_overwrite(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "skills" / "my-skill"
    _skill(source, "PROJECT")
    first = _install(project)
    assert first.returncode == 0, first.stderr

    codex = _host_skill(project, "codex", "my-skill")
    codex.unlink()
    shutil.copytree(source, codex)
    user_bytes = b"user bytes claimed by a tampered index\n"
    (codex / "SKILL.md").write_bytes(user_bytes)
    index_path = project / ".agent-flow" / "skills" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    selected = next(skill for skill in index["skills"] if skill["name"] == "my-skill")
    selected["tree_hash"] = _tree_hash(codex)
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")

    result = _install(project, "--force-managed")

    assert result.returncode != 0
    assert "previous skill index does not match kit provenance" in result.stderr
    assert (codex / "SKILL.md").read_bytes() == user_bytes


@pytest.mark.parametrize(
    ("relative", "payload", "message"),
    (
        (".agent-flow/kit.json", "{invalid", "installed kit metadata is invalid"),
        (".agent-flow/kit.json", "[]\n", "installed kit metadata is invalid"),
        (".agent-flow/skills/index.json", "{invalid", "previous skill index is invalid"),
        (".agent-flow/skills/index.json", "[]\n", "previous skill index is invalid"),
    ),
)
def test_reinstall_rejects_invalid_provenance_metadata_without_overwriting_host_bytes(
    tmp_path: Path,
    relative: str,
    payload: str,
    message: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "skills" / "my-skill"
    _skill(source, "PROJECT")
    first = _install(project)
    assert first.returncode == 0, first.stderr
    codex = _host_skill(project, "codex", "my-skill")
    codex.unlink()
    shutil.copytree(source, codex)
    user_bytes = b"preserve through invalid metadata\n"
    (codex / "SKILL.md").write_bytes(user_bytes)
    (project / relative).write_text(payload, encoding="utf-8")

    result = _install(project, "--force-managed")

    assert result.returncode != 0
    assert message in result.stderr
    assert (codex / "SKILL.md").read_bytes() == user_bytes


@pytest.mark.parametrize(
    ("removed", "message"),
    (
        (".agent-flow/skills/index.json", "required by kit provenance is missing"),
        (".agent-flow/kit.json", "previous skill index exists without kit provenance"),
    ),
)
def test_partial_provenance_install_is_not_treated_as_fresh(
    tmp_path: Path,
    removed: str,
    message: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "skills" / "my-skill"
    _skill(source, "PROJECT")
    first = _install(project)
    assert first.returncode == 0, first.stderr
    codex = _host_skill(project, "codex", "my-skill")
    codex.unlink()
    shutil.copytree(source, codex)
    user_bytes = b"preserve through partial provenance\n"
    (codex / "SKILL.md").write_bytes(user_bytes)
    (project / removed).unlink()

    result = _install(project, "--force-managed")

    assert result.returncode != 0
    assert message in result.stderr
    assert (codex / "SKILL.md").read_bytes() == user_bytes


def test_legacy_canonical_snapshot_migrates_without_becoming_authority(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "AGENT_FLOW_SKIP_CODEX_TRUST": "1"}
    first = _install(project, "--skill", "grilling", env=env)
    assert first.returncode == 0, first.stderr
    kit_path = project / ".agent-flow" / "kit.json"
    kit = json.loads(kit_path.read_text(encoding="utf-8"))
    kit.pop("skill_plan_hash", None)
    kit.pop("skill_plan_hash_version", None)
    kit.pop("managed_host_files_commitment", None)
    kit.pop("managed_host_files_commitment_version", None)
    kit_path.write_text(json.dumps(kit, indent=2) + "\n", encoding="utf-8")

    result = _install(project, "--profile", "generic", "--skill", "grilling", env=env)

    assert result.returncode == 0, result.stderr
    migrated = json.loads(kit_path.read_text(encoding="utf-8"))
    assert migrated["skill_plan_hash_version"] == 2
    assert len(migrated["skill_plan_hash"]) == 64


def test_legacy_modified_snapshot_is_not_promoted_to_authority(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "AGENT_FLOW_SKIP_CODEX_TRUST": "1"}
    first = _install(project, "--skill", "grilling", env=env)
    assert first.returncode == 0, first.stderr
    kit_path = project / ".agent-flow" / "kit.json"
    kit = json.loads(kit_path.read_text(encoding="utf-8"))
    kit.pop("skill_plan_hash", None)
    kit.pop("skill_plan_hash_version", None)
    kit.pop("managed_host_files_commitment", None)
    kit.pop("managed_host_files_commitment_version", None)
    kit_path.write_text(json.dumps(kit, indent=2) + "\n", encoding="utf-8")
    snapshot = project / ".agent-flow" / "skills" / "grilling" / "SKILL.md"
    user_bytes = b"untrusted legacy snapshot\n"
    snapshot.write_bytes(user_bytes)

    result = _install(project, "--profile", "generic", "--skill", "grilling", env=env)

    assert result.returncode != 0
    assert "untrusted existing skill snapshot differs: grilling" in result.stderr
    assert snapshot.read_bytes() == user_bytes


def test_legacy_migration_requires_explicit_profile_and_retained_skills(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "AGENT_FLOW_SKIP_CODEX_TRUST": "1"}
    first = _install(project, "--profile", "node", "--skill", "grilling", env=env)
    assert first.returncode == 0, first.stderr
    kit_path = project / ".agent-flow" / "kit.json"
    kit = json.loads(kit_path.read_text(encoding="utf-8"))
    kit.pop("skill_plan_hash", None)
    kit.pop("skill_plan_hash_version", None)
    kit.pop("managed_host_files_commitment", None)
    kit.pop("managed_host_files_commitment_version", None)
    kit_path.write_text(json.dumps(kit, indent=2) + "\n", encoding="utf-8")

    implicit = _install(project, env=env)
    assert implicit.returncode != 0
    assert "legacy install migration requires an explicit --profile" in implicit.stderr

    dropped_skill = _install(project, "--profile", "node", env=env)
    assert dropped_skill.returncode != 0
    assert "must explicitly retain skills: grilling" in dropped_skill.stderr

    explicit = _install(project, "--profile", "node", "--skill", "grilling", env=env)
    assert explicit.returncode == 0, explicit.stderr


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


def test_skill_frontmatter_path_name_fails_closed_before_install(tmp_path: Path) -> None:
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

    assert result.returncode != 0
    assert "unsafe project-local skill name" in result.stderr
    assert not (tmp_path / "outside").exists()
    assert not (project / ".agent-flow" / "skills" / "index.json").exists()


def test_skill_frontmatter_dotdot_name_fails_closed_before_install(tmp_path: Path) -> None:
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

    assert result.returncode != 0
    assert "unsafe project-local skill name" in result.stderr
    assert not (project / ".agent-flow" / "skills" / "index.json").exists()


def test_stale_host_skill_link_removed_when_hosts_change(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / "skills" / "demo"
    _skill(skill_dir, "CODEX", hosts="[codex]")
    assert _install(project).returncode == 0
    assert (project / ".agents" / "skills" / "demo" / "SKILL.md").exists()

    _skill(skill_dir, "CLAUDE", hosts="[claude]")
    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert (project / ".claude" / "skills" / "demo" / "SKILL.md").exists()
    assert not (project / ".agents" / "skills" / "demo").exists()


def test_stale_broken_host_skill_symlink_removed_when_skill_deleted(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / "skills" / "demo"
    _skill(skill_dir, "CODEX", hosts="[codex]")
    assert _install(project).returncode == 0
    codex_link = project / ".agents" / "skills" / "demo"
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
    codex_link = project / ".agents" / "skills" / "demo"
    if codex_link.is_symlink():
        codex_link.unlink()
        codex_link.mkdir(parents=True)
        (codex_link / "SKILL.md").write_text((skill_dir / "SKILL.md").read_text(encoding="utf-8"), encoding="utf-8")

    (skill_dir / "SKILL.md").unlink()
    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert not codex_link.exists()


def test_stale_nested_modified_host_copy_is_preserved_and_blocks_cleanup(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / "skills" / "demo"
    _skill(skill_dir, "CODEX", hosts="[codex]")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "policy.md").write_text("canonical\n", encoding="utf-8")
    first = _install(project)
    assert first.returncode == 0, first.stderr
    codex = _host_skill(project, "codex", "demo")
    codex.unlink()
    shutil.copytree(skill_dir, codex)
    nested = codex / "references" / "policy.md"
    user_bytes = b"user stale nested edit\n"
    nested.write_bytes(user_bytes)
    (skill_dir / "SKILL.md").unlink()

    result = _install(project)

    assert result.returncode != 0
    assert "user-modified stale project skill host tree differs" in result.stderr
    assert nested.read_bytes() == user_bytes


def test_host_skill_root_symlink_fails_closed_without_writing_outside_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / ".agents").symlink_to(outside, target_is_directory=True)
    _skill(project / "skills" / "demo", "CODEX", hosts="[codex]")

    result = _install(project)

    assert result.returncode == 1
    assert "project skill host root may not use symlinks" in result.stderr
    assert not (outside / "skills" / "demo").exists()


def test_android_official_skills_use_indexed_project_snapshots_without_host_links(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    (home / ".codex").mkdir(parents=True)
    (project / "settings.gradle").write_text("pluginManagement { repositories { google() } }\n", encoding="utf-8")

    result = _install(
        project,
        "--profile",
        "generic",
        "--skill",
        "agp-9-upgrade",
        "--skill",
        "android-intent-security",
        "--skill",
        "play-policy-insights",
        env={
            **os.environ,
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "AGENT_FLOW_HOST": "codex",
            "AGENT_FLOW_SKIP_CODEX_TRUST": "1",
        },
    )

    assert result.returncode == 0, result.stderr
    assert not (project / ".agent-flow" / "vendor" / "android-skills").exists()
    assert not (project / ".agent-flow" / "vendor" / "chrisbanes-skills").exists()
    assert not (project / ".agents" / "skills" / "edge-to-edge").exists()

    index = json.loads(
        (project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8")
    )
    by_name = {skill["name"]: skill for skill in index["skills"]}
    for name in ("agp-9-upgrade", "android-intent-security", "play-policy-insights"):
        snapshot = project / ".agent-flow" / "skills" / name / "SKILL.md"
        assert snapshot.exists()
        assert by_name[name]["source"] == "bundled"
        assert by_name[name]["profiles"] == ["generic"]
        assert not _host_skill(project, "claude", name).exists()
        assert not _host_skill(project, "codex", name).exists()
        assert not _host_skill(project, "omp", name).exists()

    installed_lock = json.loads(
        (project / ".agent-flow" / "skills" / "upstream-lock.json").read_text(
            encoding="utf-8"
        )
    )
    assert installed_lock["android_official"]["commit"] == (
        "57ff3c7d02a53781954f9ce2df92f14b7fbb2ded"
    )
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


def test_android_skill_policy_uses_one_indexed_project_snapshot() -> None:
    profile_paths = [
        KIT_ROOT / "profiles" / "android.yaml",
        KIT_ROOT / "src" / "agent_flow" / "profiles" / "android.yaml",
    ]
    schema_paths = [
        KIT_ROOT / "profiles" / "_schema.yaml",
        KIT_ROOT / "src" / "agent_flow" / "profiles" / "_schema.yaml",
    ]
    runtime_paths = [
        KIT_ROOT / "templates" / "_shared" / "review" / "android-skills.md",
        KIT_ROOT / "templates" / "_shared" / "review" / "android-chrisbanes.md",
        KIT_ROOT / "skills" / "android-code-review" / "SKILL.md",
    ]

    for path in profile_paths:
        text = path.read_text(encoding="utf-8")
        assert "install_policy: project_snapshot" in text
        assert "active_host_only: false" in text
        assert "load_mode: indexed_snapshot" in text
        assert "bootstrap_hosts:" in text
        assert "codex: ~/.codex/skills/{skill}/SKILL.md" in text
        assert "claude: ~/.claude/skills/{skill}/SKILL.md" in text
        assert "omp: ~/.omp/agent/skills/{skill}/SKILL.md" in text
        assert ".agent-flow/skills/index.json" in text
        assert "missing local android_skills: <skill>" in text
        assert "missing local chrisbanes_skills: <skill>" in text
        assert "vendor_dir" not in text
        assert "native_loader" not in text
        assert ".agent-flow/vendor" not in text

    for path in schema_paths:
        text = path.read_text(encoding="utf-8")
        assert 'install_policy: "project_snapshot"' in text
        assert "bootstrap_hosts:" in text
        assert "read only that snapshot at runtime" in text

    for path in runtime_paths:
        text = path.read_text(encoding="utf-8")
        assert ".agent-flow/skills/index.json" in text
        assert "~/.codex/skills/{skill}/SKILL.md" not in text
        assert "host-global" in text

    kit_text = (KIT_ROOT / "bin" / "agent-flow-kit.mjs").read_text(encoding="utf-8")
    assert "missing local <group>: <skill>" in kit_text
