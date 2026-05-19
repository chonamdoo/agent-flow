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


def _install(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install", "--without-graphify"),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
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
    for host in ("claude", "codex", "gemini"):
        assert (project / f".{host}" / "skills" / "my-skill" / "SKILL.md").exists()
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    selected = next(skill for skill in index["skills"] if skill["name"] == "my-skill")
    assert selected["source"] == "project"
    assert "BODY SHOULD NOT BE IN INDEX" not in json.dumps(index)


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
    assert (project / ".codex" / "skills" / "codex-only" / "SKILL.md").exists()
    assert not (project / ".claude" / "skills" / "codex-only").exists()
    assert not (project / ".gemini" / "skills" / "codex-only").exists()


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
    assert (project / ".codex" / "skills" / "codex-block-list" / "SKILL.md").exists()
    assert not (project / ".claude" / "skills" / "codex-block-list").exists()
    assert not (project / ".gemini" / "skills" / "codex-block-list").exists()


def test_existing_user_modified_skill_is_not_overwritten(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _skill(project / "skills" / "my-skill", "PROJECT")
    dest = project / ".codex" / "skills" / "my-skill"
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
    assert (project / ".codex" / "skills" / "demo" / "SKILL.md").exists()

    _skill(skill_dir, "CLAUDE", hosts="[claude]")
    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert (project / ".claude" / "skills" / "demo" / "SKILL.md").exists()
    assert not (project / ".codex" / "skills" / "demo").exists()


def test_stale_broken_host_skill_symlink_removed_when_skill_deleted(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / "skills" / "demo"
    _skill(skill_dir, "CODEX", hosts="[codex]")
    assert _install(project).returncode == 0
    codex_link = project / ".codex" / "skills" / "demo"
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
    codex_link = project / ".codex" / "skills" / "demo"
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
    (project / ".codex").symlink_to(outside, target_is_directory=True)
    _skill(project / "skills" / "demo", "CODEX", hosts="[codex]")

    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert not (outside / "skills" / "demo").exists()
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    assert any(link["status"] == "skipped-host-root-symlink" for link in index["links"])
