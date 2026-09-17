from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from agent_flow.core.kit_digest import kit_source_digest


KIT_ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {
    ".agent-flow", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".venv", "__pycache__", "node_modules", ".DS_Store",
}


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENT_FLOW_NO_UPDATE_CHECK", "1")
    for name in (
        "AGENT_FLOW_PROFILE", "AGENT_FLOW_HOST", "AGENT_FLOW_GENERIC_MODE",
        "CLAUDECODE", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "CODEX_SANDBOX",
        "PI_CODING_AGENT_DIR", "OMP_SESSION_ID", "NODE_OPTIONS", "PYTHONPATH",
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    assert shutil.which("node") is not None, "Node is required for plugin bundle scenarios"


def _pack(output: Path, source: Path = KIT_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", str(source / "bin/agent-flow-plugin.mjs"), "pack", "--output", str(output)],
        cwd=output.parent,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )


def _bundle(output: Path, source: Path = KIT_ROOT) -> Path:
    result = _pack(output, source)
    assert result.returncode == 0, result.stdout + result.stderr
    return output


def _snapshot(root: Path) -> dict[str, tuple[str, int, int]]:
    return {
        file.relative_to(root).as_posix(): (
            hashlib.sha256(file.read_bytes()).hexdigest(),
            file.stat().st_mode & 0o777,
            file.stat().st_mtime_ns,
        )
        for file in root.rglob("*") if file.is_file()
    }


def _source_copy(destination: Path) -> Path:
    package = json.loads((KIT_ROOT / "package.json").read_text())
    destination.mkdir()
    for relative in ["package.json", *package["files"]]:
        source = KIT_ROOT / relative
        target = destination / relative
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns(*EXCLUDED, "*.pyc"))
        else:
            shutil.copy2(source, target)
    return destination


def test_bundle_is_complete_deterministic_and_entry_only(tmp_path: Path) -> None:
    first = _bundle(tmp_path / "plugin one")
    second = _bundle(tmp_path / "plugin two")
    assert _snapshot(first) == _snapshot(second)
    assert not any(file.is_symlink() for file in first.rglob("*"))
    assert kit_source_digest(first / "kit") == kit_source_digest(KIT_ROOT)
    package = json.loads((KIT_ROOT / "package.json").read_text())
    for relative in ["package.json", *package["files"]]:
        source = KIT_ROOT / relative
        files = source.rglob("*") if source.is_dir() else [source]
        for file in files:
            if not file.is_file() or any(part in EXCLUDED for part in file.relative_to(KIT_ROOT).parts) or file.suffix == ".pyc":
                continue
            assert (first / "kit" / file.relative_to(KIT_ROOT)).read_bytes() == file.read_bytes()
    portable = json.loads((first / "plugin.json").read_text())
    claude = json.loads((first / ".claude-plugin/plugin.json").read_text())
    assert portable["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert portable["name"] == claude["name"] == "agent-flow"
    assert portable["version"] == claude["version"] == package["version"]
    assert sorted(path.relative_to(first / "skills").as_posix() for path in (first / "skills").rglob("SKILL.md")) == ["agent-flow/SKILL.md"]
    assert not any((first / name).exists() for name in ("hooks", "agents", "commands", ".mcp.json", "mcp.json", ".agent-flow"))
    assert "hooks" not in claude
    assert "hooks" not in portable["extensions"]["com.openai"]
    policy = yaml.safe_load((first / "skills/agent-flow/agents/openai.yaml").read_text())
    assert policy["policy"]["allow_implicit_invocation"] is False
    frontmatter = (first / "skills/agent-flow/SKILL.md").read_text().split("---", 2)[1]
    assert yaml.safe_load(frontmatter)["disable-model-invocation"] is True


def test_pack_reads_current_canonical_entry_and_preserves_relative_payload(tmp_path: Path) -> None:
    source = _source_copy(tmp_path / "source")
    entry = source / "skills/agent-flow/SKILL.md"
    entry.write_text(entry.read_text() + "\nUse the newly selected completion contract.\n")
    reference = source / "skills/local-contract/references/team.txt"
    reference.parent.mkdir(parents=True)
    reference.write_text("Local architecture remains authoritative.\n")
    (reference.parent.parent / "SKILL.md").write_text(
        "---\nname: local-contract\ndescription: Local architecture\n---\n"
        "Read [team rules](references/team.txt).\n"
    )
    bundle = _bundle(tmp_path / "plugin", source)
    generated = (bundle / "skills/agent-flow/SKILL.md").read_text()
    assert generated.endswith(entry.read_text().split("---", 2)[2])
    installed_skill = bundle / "kit/skills/local-contract"
    assert (installed_skill / "references/team.txt").read_bytes() == reference.read_bytes()
    assert not (bundle / "skills/local-contract").exists()
    assert kit_source_digest(bundle / "kit") == kit_source_digest(source)


def test_pack_refuses_existing_output_without_mutating_it(tmp_path: Path) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    owned = output / "user-content.txt"
    owned.write_text("keep my bundle\n")
    before = _snapshot(output)
    result = _pack(output)
    assert result.returncode != 0
    assert _snapshot(output) == before


def test_pack_rejects_outward_symlinks_without_partial_output(tmp_path: Path) -> None:
    source = _source_copy(tmp_path / "source")
    private = tmp_path / "private.txt"
    private.write_text("outside payload\n")
    (source / "skills/private-link").symlink_to(private)
    output = tmp_path / "plugin"
    result = _pack(output, source)
    assert result.returncode != 0
    assert not output.exists()
    assert private.read_text() == "outside payload\n"


def test_project_runtime_survives_bundle_removal(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "plugin cache")
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    user_rules = project / "AGENTS.md"
    user_rules.write_text("Preserve project-owned instructions.\n")
    installer = (bundle / "skills/agent-flow" / "../../kit/bin/agent-flow-kit.mjs").resolve()
    result = subprocess.run(
        ["node", str(installer), "install", "--root", str(project), "--profile", "python",
         "--architecture-mode", "pending", "--no-hooks"],
        cwd=project, text=True, capture_output=True, check=False, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    launcher = project / ".agent-flow/bin/agent-flow"
    command = [str(launcher), "workflow", "export", "--workflow", "development"]
    before = subprocess.run(command, cwd=project, text=True, capture_output=True, check=False, timeout=30)
    assert before.returncode == 0, before.stdout + before.stderr
    assert json.loads(before.stdout)["id"] == "development"
    project_snapshot = _snapshot(project / ".agent-flow")
    shutil.rmtree(bundle)
    after = subprocess.run(command, cwd=project, text=True, capture_output=True, check=False, timeout=30)
    assert after.returncode == 0, after.stdout + after.stderr
    assert json.loads(after.stdout) == json.loads(before.stdout)
    assert _snapshot(project / ".agent-flow") == project_snapshot
    assert user_rules.read_text() == "Preserve project-owned instructions.\n"
