from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest
import yaml

from agent_flow.artifact import find_active_run
from agent_flow.cli import main
from agent_flow.core.skill_plan import (
    SkillPlanSnapshotError,
    build_resolved_skill_lock,
    installed_skill_plan_pin,
    resolve_runtime_skill_plan,
)
from agent_flow.core.workspace_boundary import (
    ExecutionIdentity,
    acquire_workspace_start_claim,
    capture_workspace_identity,
    release_workspace_start_claim,
    resolve_execution_finalizer_workspace,
)


KIT_ROOT = Path(__file__).resolve().parent.parent


def _runtime_tree_integrity(root: Path) -> str:
    entries: list[dict[str, object]] = []

    def visit(current: Path, relative: str) -> None:
        stat = current.lstat()
        entries.append({"path": relative, "type": "directory", "mode": stat.st_mode & 0o777})
        for child in sorted(current.iterdir(), key=lambda path: path.name):
            child_relative = f"{relative}/{child.name}" if relative else child.name
            child_stat = child.lstat()
            if child.is_dir():
                visit(child, child_relative)
            else:
                entries.append(
                    {
                        "path": child_relative,
                        "type": "file",
                        "mode": child_stat.st_mode & 0o777,
                        "sha256": hashlib.sha256(child.read_bytes()).hexdigest(),
                    }
                )

    visit(root, "")
    entries.sort(key=lambda entry: str(entry["path"]))
    payload = json.dumps(
        {"version": 1, "entries": entries},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _runtime_contract_commitment(contract: dict[str, object]) -> str:
    payload = [
        contract["version"],
        contract["launcher"]["path"],
        contract["launcher"]["sha256"],
        contract["node"]["path"],
        contract["node"]["sha256"],
        contract["node"]["device"],
        contract["node"]["inode"],
        contract["node"]["links"],
        contract["node"]["mode"],
        json.dumps(contract["node"].get("dependencies", []), separators=(",", ":"), sort_keys=True),
        contract["git"]["path"],
        contract["git"]["sha256"],
        contract["git"]["device"],
        contract["git"]["inode"],
        contract["git"]["links"],
        contract["git"]["mode"],
        json.dumps(contract["git"].get("dependencies", []), separators=(",", ":"), sort_keys=True),
        contract["python"]["path"],
        contract["python"]["resolved_path"],
        contract["python"]["sha256"],
        contract["python"]["device"],
        contract["python"]["inode"],
        contract["python"]["links"],
        contract["python"]["mode"],
        json.dumps(contract["python"].get("dependencies", []), separators=(",", ":"), sort_keys=True),
        contract["runtime"]["path"],
        contract["runtime"]["integrity"],
        contract["python_runtime"]["path"],
        contract["python_runtime"]["integrity"],
    ]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    return node


def _install(
    project: Path,
    *args: str,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    process_env = dict(os.environ)
    provided_env = env or {}
    test_home = Path(provided_env.get("HOME", project.parent / "test-home"))
    process_env["HOME"] = str(test_home)
    process_env["CODEX_HOME"] = provided_env.get(
        "CODEX_HOME",
        str(test_home / ".codex"),
    )
    process_env["CLAUDE_CONFIG_DIR"] = provided_env.get(
        "CLAUDE_CONFIG_DIR",
        str(test_home / ".claude"),
    )
    process_env["PI_CODING_AGENT_DIR"] = provided_env.get(
        "PI_CODING_AGENT_DIR",
        str(test_home / ".omp" / "agent"),
    )
    process_env["AGENT_FLOW_AUTO_EXTERNAL_SKILLS"] = "1"
    process_env.update(provided_env)
    return subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install", *args),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
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


def _authoritative_current_run_state_path(project: Path) -> Path:
    scoped = [
        *project.glob(".git/agent-flow/current-runs/*.json"),
        *project.glob(".agent-flow/state/current-runs/*.json"),
    ]
    assert len(scoped) <= 1
    return scoped[0] if scoped else project / ".agent-flow" / "state" / "current-run.json"


def test_install_materializes_authenticated_project_launcher(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    install = _install(project)

    assert install.returncode == 0, install.stderr
    launcher = project / ".agent-flow" / "bin" / "agent-flow"
    runtime = project / ".agent-flow" / "runtime" / "node"
    kit = json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
    contract = kit["project_runtime_contract"]
    assert launcher.is_file()
    assert os.access(launcher, os.X_OK)
    assert (runtime / "bin" / "agent-flow-kit.mjs").is_file()
    assert contract["version"] == 3
    assert contract["launcher"]["sha256"] == hashlib.sha256(launcher.read_bytes()).hexdigest()
    assert contract["python_runtime"]["path"] == ".agent-flow/runtime/python"
    assert kit["project_runtime_contract_commitment_version"] == 1
    assert str(launcher) in (project / ".agent-flow" / "skills" / "agent-flow" / "SKILL.md").read_text(encoding="utf-8")
    assert str(launcher) in (
        project / ".agent-flow" / "skills" / "full-feature-workflow" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert str(launcher) in (project / ".agent-flow" / "prompts" / "pr-watch.md").read_text(encoding="utf-8")
    env = dict(os.environ)
    env["HOME"] = str(project.parent / "test-home")
    env["AGENT_FLOW_AUTO_EXTERNAL_SKILLS"] = "1"
    started = subprocess.run(
        (str(launcher), "run", "local runtime"),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    status = subprocess.run(
        (str(launcher), "status"),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert started.returncode == 0, started.stderr
    assert status.returncode == 0, status.stderr
    assert "next_command:" in status.stdout
    assert str(launcher) in status.stdout


def test_project_launcher_install_repairs_python_runtime_drift(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    launcher = project / ".agent-flow" / "bin" / "agent-flow"
    python_runtime = project / ".agent-flow" / "runtime" / "python"
    python_package = python_runtime / "agent_flow"
    assert not list(python_runtime.rglob("__pycache__"))
    assert not list(python_runtime.rglob("*.pyc"))
    cache = python_package / "__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "cli.cpython-314.pyc").write_bytes(b"generated cache")
    unexpected = python_runtime / "agent_flow" / "unexpected.py"
    unexpected.write_text("tampered = True\n", encoding="utf-8")
    unexpected_root = python_runtime / "unexpected-root.txt"
    unexpected_root.write_text("drift\n", encoding="utf-8")
    removed = python_package / "__init__.py"
    removed.unlink()
    tampered = python_package / "cli.py"
    expected_cli = (KIT_ROOT / "src" / "agent_flow" / "cli.py").read_bytes()
    tampered.write_bytes(b"tampered runtime\n")
    env = {
        **os.environ,
        "HOME": str(project.parent / "test-home"),
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "1",
    }
    rejected = subprocess.run(
        (str(launcher), "status"),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    repaired = subprocess.run(
        (str(launcher), "install"),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    status = subprocess.run(
        (str(launcher), "status"),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert rejected.returncode == 1
    assert "project runtime contract no longer matches installed files" in rejected.stderr
    assert repaired.returncode == 0, repaired.stderr
    assert not cache.exists()
    assert not unexpected.exists()
    assert not unexpected_root.exists()
    assert removed.is_file()
    assert tampered.read_bytes() == expected_cli
    assert status.returncode == 0, status.stderr
    assert status.stdout.strip() == "no runs"
    kit = json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
    assert kit["project_runtime_contract"]["python_runtime"]["integrity"] == _runtime_tree_integrity(
        python_runtime
    )


def test_project_launcher_run_creates_and_pins_git_worktree(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(("git", "init", "-b", "main", str(project)), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(project), "config", "user.name", "Test User"), check=True)
    subprocess.run(("git", "-C", str(project), "config", "user.email", "test@example.com"), check=True)
    (project / "README.md").write_text("project\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(project), "add", "README.md"), check=True)
    subprocess.run(("git", "-C", str(project), "commit", "-m", "initial"), check=True, capture_output=True)
    assert _install(project).returncode == 0
    subprocess.run(("git", "-C", str(project), "add", ".gitignore"), check=True)
    subprocess.run(("git", "-C", str(project), "commit", "-m", "install config"), check=True, capture_output=True)
    launcher = project / ".agent-flow" / "bin" / "agent-flow"
    env = dict(os.environ)
    env["HOME"] = str(project.parent / "test-home")
    env["AGENT_FLOW_AUTO_EXTERNAL_SKILLS"] = "1"
    env["AGENT_FLOW_ACTIVE_HOST"] = "codex"
    env["AGENT_FLOW_EXECUTION_ID"] = "pinned-session"

    started = subprocess.run(
        (str(launcher), "run", "Pinned Task"),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    worktree = project / ".agent-flow" / "worktrees" / "feat-pinned-task"
    status = subprocess.run(
        (str(launcher), "status"),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    fake_bin = project.parent / "fake-bin"
    fake_bin.mkdir()
    fake_git_marker = project.parent / "fake-git-ran"
    fake_git = fake_bin / "git"
    fake_git.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(fake_git_marker))}\nexit 99\n", encoding="utf-8")
    fake_git.chmod(0o755)
    gate_env = {**env, "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"}
    run_root = project / ".git" / "agent-flow" / "worktrees" / "feat-pinned-task"
    meta_path = next(run_root.glob(".agent-flow/runs/*/meta.json"))
    preserved_dir = (
        project
        / ".git"
        / "agent-flow"
        / "worktrees"
        / "preserved-blocked"
        / ".agent-flow"
        / "runs"
        / "preserved-blocked"
    )
    preserved_dir.mkdir(parents=True)
    preserved = json.loads(meta_path.read_text(encoding="utf-8"))
    preserved["run_id"] = "preserved-blocked"
    preserved["workspace"]["workspace_root"] = str(project.resolve())
    (preserved_dir / "meta.json").write_text(json.dumps(preserved), encoding="utf-8")
    (preserved_dir / "active").write_text("active\n", encoding="utf-8")
    gate = subprocess.run(
        (str(launcher), "gate", "--", sys.executable, "-c", "print('gate-ok')"),
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
        env=gate_env,
    )

    assert started.returncode == 0, started.stderr
    assert worktree.is_dir()
    assert subprocess.run(
        ("git", "-C", str(project), "branch", "--show-current"),
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip() == "main"
    assert subprocess.run(
        ("git", "-C", str(worktree), "branch", "--show-current"),
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip() == "feat/pinned-task"
    assert status.returncode == 0, status.stderr
    assert gate.returncode == 0, gate.stderr
    assert "gate-ok" in gate.stdout
    assert not fake_git_marker.exists()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["workspace"]["workspace_root"] == str(worktree.resolve())
    assert meta["execution"] == {
        "host": "codex",
        "session_id": "pinned-session",
        "agent_id": "",
    }
    bindings = list((project / ".git" / "agent-flow" / "executions").glob("*.json"))
    assert len(bindings) == 1
    binding = json.loads(bindings[0].read_text(encoding="utf-8"))
    assert Path(binding["workspace"]["workspace_root"]).samefile(worktree)
    assert Path(binding["run_dir"]).samefile(meta_path.parent)
    assert "next_command:" in status.stdout
    assert str(launcher) in status.stdout


def test_project_launcher_clears_node_preload_environment(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside.txt"
    preload = tmp_path / "preload.cjs"
    project.mkdir()
    preload.write_text(
        f"require('node:fs').writeFileSync({str(outside)!r}, 'changed');\n",
        encoding="utf-8",
    )
    assert _install(project).returncode == 0
    launcher = project / ".agent-flow" / "bin" / "agent-flow"
    env = dict(os.environ)
    env["HOME"] = str(project.parent / "test-home")
    env["AGENT_FLOW_AUTO_EXTERNAL_SKILLS"] = "1"
    env["NODE_OPTIONS"] = f"--require={preload}"

    result = subprocess.run(
        (str(launcher), "status"),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert not outside.exists()


def test_project_launcher_clears_all_loader_environment_families(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0

    launcher = (project / ".agent-flow" / "bin" / "agent-flow").read_text(encoding="utf-8")
    runtime = (project / ".agent-flow" / "runtime" / "node" / "bin" / "agent-flow-kit.mjs").read_text(
        encoding="utf-8"
    )

    for name in (
        "LD_AUDIT",
        "LD_DEBUG",
        "DYLD_FRAMEWORK_PATH",
        "DYLD_FALLBACK_FRAMEWORK_PATH",
        "DYLD_FALLBACK_LIBRARY_PATH",
        "DYLD_ROOT_PATH",
        "DYLD_IMAGE_SUFFIX",
        "DYLD_SHARED_REGION",
    ):
        assert name in launcher
    assert "name.startswith(('DYLD_','LD_'))" in runtime
    assert "DYLD_FRAMEWORK_PATH" in runtime


def test_project_launcher_rejects_replaced_contracted_python(tmp_path: Path) -> None:
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    project.mkdir()
    fake_bin.mkdir()
    python = fake_bin / "python"
    yaml_site = Path(yaml.__file__).resolve().parent.parent
    python.write_text(
        "#!/bin/sh\n"
        f"PYTHONPATH={shlex.quote(str(yaml_site))}${{PYTHONPATH:+:$PYTHONPATH}}\n"
        "export PYTHONPATH\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    env = {"PYTHON": str(python), "PYTHON_EXECUTABLE": str(python)}
    assert _install(project, env=env).returncode == 0
    contract = json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))[
        "project_runtime_contract"
    ]
    assert contract["python"]["path"] == str(python)
    python.write_text(
        python.read_text(encoding="utf-8") + "# replaced\n",
        encoding="utf-8",
    )

    result = _command(project, "status", env=env)

    assert result.returncode != 0
    assert "executable identity changed" in result.stderr


def test_authenticated_python_execution_rejects_same_path_replacement_after_contract_check(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    marker = tmp_path / "replacement-ran"
    project.mkdir()
    fake_bin.mkdir()
    python = fake_bin / "python"
    yaml_site = Path(yaml.__file__).resolve().parent.parent
    python.write_text(
        "#!/bin/sh\n"
        f"PYTHONPATH={shlex.quote(str(yaml_site))}${{PYTHONPATH:+:$PYTHONPATH}}\n"
        "export PYTHONPATH\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PYTHON": str(python),
        "PYTHON_EXECUTABLE": str(python),
        "AGENT_FLOW_TEST_HOLD_BEFORE_AUTHENTICATED_PYTHON_OPEN_MS": "1200",
    }
    assert _install(project, env=env).returncode == 0
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "status"),
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stderr is not None
    assert "agent-flow:test-authenticated-python-ready" in process.stderr.readline()
    python.write_text(
        "#!/bin/sh\n"
        f": > {shlex.quote(str(marker))}\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    stdout, stderr = process.communicate(timeout=15)

    assert process.returncode != 0, stdout
    assert "executable identity changed" in stderr
    assert not marker.exists()


def test_authenticated_execution_rejects_staging_path_replacement_without_hardlinking_source(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    marker = tmp_path / "replacement-ran"
    project.mkdir()
    assert _install(project).returncode == 0
    contract = json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))[
        "project_runtime_contract"
    ]
    source = Path(contract["python"]["resolved_path"])
    source_links = source.stat().st_nlink
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_TEST_HOLD_AFTER_AUTHENTICATED_STAGE_MS": "1500",
    }
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "status"),
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stderr is not None
    prefix = "agent-flow:test-authenticated-stage-ready:"
    line = process.stderr.readline().strip()
    assert line.startswith(prefix), line
    staged = Path(line.removeprefix(prefix))
    assert staged.stat().st_ino != source.stat().st_ino
    assert source.stat().st_nlink == source_links
    staged.unlink()
    staged.write_text(
        "#!/bin/sh\n"
        f": > {shlex.quote(str(marker))}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    staged.chmod(0o755)
    stdout, stderr = process.communicate(timeout=20)

    assert process.returncode != 0, stdout
    assert "executable identity changed" in stderr
    assert not marker.exists()


def test_authenticated_execution_rejects_staging_ancestor_replacement(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_TEST_HOLD_AFTER_AUTHENTICATED_STAGE_MS": "1500",
    }
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "status"),
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stderr is not None
    line = process.stderr.readline().strip()
    assert line.startswith("agent-flow:test-authenticated-stage-ready:"), line
    managed_root = project / ".agent-flow"
    displaced_root = project / ".agent-flow-displaced"
    managed_root.rename(displaced_root)
    managed_root.mkdir()
    (managed_root / "exec-staging").mkdir()
    stdout, stderr = process.communicate(timeout=20)
    shutil.rmtree(managed_root)
    displaced_root.rename(managed_root)

    assert process.returncode != 0, stdout
    assert "executable identity changed" in stderr


def test_authenticated_execution_rejects_symlinked_staging_root_without_external_write(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir(mode=0o755)
    assert _install(project).returncode == 0
    staging_root = project / ".agent-flow" / "exec-staging"
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging_root.symlink_to(outside, target_is_directory=True)
    before_mode = outside.stat().st_mode & 0o777

    result = _command(project, "status")

    assert result.returncode != 0
    assert "executable identity changed" in result.stderr
    assert list(outside.iterdir()) == []
    assert outside.stat().st_mode & 0o777 == before_mode


def test_concurrent_authenticated_launchers_do_not_change_executable_link_count(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project, env={"AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0"}).returncode == 0
    launcher = project / ".agent-flow" / "bin" / "agent-flow"
    contract = json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))[
        "project_runtime_contract"
    ]
    node = Path(contract["node"]["path"])
    before_links = node.stat().st_nlink
    env = {
        **os.environ,
        "HOME": str(tmp_path / "test-home"),
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0",
        "AGENT_FLOW_TEST_HOLD_AFTER_AUTHENTICATED_STAGE_MS": "500",
    }
    processes = [
        subprocess.Popen(
            (str(launcher), "status"),
            cwd=project,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    results = [process.communicate(timeout=30) for process in processes]

    for process, (stdout, stderr) in zip(processes, results):
        assert process.returncode == 0, f"{stdout}\n{stderr}"
    assert node.stat().st_nlink == before_links


def test_installed_guard_skips_runtime_authentication_without_git_run(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    python_runtime = project / ".agent-flow" / "runtime" / "python"
    executed = tmp_path / "tampered-runtime-executed"
    tampered = python_runtime / "agent_flow" / "__init__.py"
    tampered.write_text(
        tampered.read_text(encoding="utf-8")
        + f"\nfrom pathlib import Path\nPath({str(executed)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    kit_path = project / ".agent-flow" / "kit.json"
    kit = json.loads(kit_path.read_text(encoding="utf-8"))
    contract = kit["project_runtime_contract"]
    contract["python_runtime"]["integrity"] = _runtime_tree_integrity(python_runtime)
    kit["project_runtime_contract_commitment"] = _runtime_contract_commitment(contract)
    kit_path.write_text(json.dumps(kit, indent=2) + "\n", encoding="utf-8")
    guard = project / ".agent-flow" / "scripts" / "hooks" / "guard-worktree-write.py"
    payload = {
        "tool_name": "apply_patch",
        "cwd": str(project),
        "tool_input": {"patch": "*** Begin Patch\n*** End Patch"},
    }

    result = subprocess.run(
        (sys.executable, str(guard)),
        cwd=project,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert result.returncode == 0
    assert not executed.exists()


def test_managed_python_hook_runs_repeatedly_without_runtime_drift(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    hooks = json.loads((project / ".Codex" / "hooks.json").read_text(encoding="utf-8"))
    commands: list[str] = []
    guard = str(project / ".agent-flow" / "scripts" / "hooks" / "guard-worktree-write.py")

    def collect(value: object) -> None:
        if isinstance(value, dict):
            command = value.get("command")
            if isinstance(command, str) and guard in command:
                commands.append(command)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(hooks)
    assert commands
    python_runtime = project / ".agent-flow" / "runtime" / "python"
    before = _runtime_tree_integrity(python_runtime)
    payload = json.dumps(
        {
            "tool_name": "apply_patch",
            "cwd": str(project),
            "tool_input": {"patch": "*** Begin Patch\n*** End Patch"},
        }
    )
    env = dict(os.environ)
    env.pop("PYTHONDONTWRITEBYTECODE", None)

    for _ in range(2):
        result = subprocess.run(
            ("/bin/bash", "-c", commands[0]),
            cwd=project,
            input=payload,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        assert result.returncode == 0, result.stderr

    assert _runtime_tree_integrity(python_runtime) == before


@pytest.mark.parametrize(
    "script_name,command",
    (
        ("guard-worktree.sh", "git checkout main"),
        ("guard-protected-branch.sh", "git commit -m test"),
    ),
)
def test_managed_shell_hooks_ignore_path_shadow_binaries(
    tmp_path: Path,
    script_name: str,
    command: str,
) -> None:
    fake_bin = tmp_path / "bin"
    marker = tmp_path / "path-shadow-ran"
    fake_bin.mkdir()
    for name in ("cat", "python3", "git"):
        executable = fake_bin / name
        executable.write_text(
            f"#!/bin/sh\n: > {shlex.quote(str(marker))}\nexit 99\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(KIT_ROOT),
        }
    )

    result = subprocess.run(
        ("/bin/bash", str(KIT_ROOT / "scripts" / "hooks" / script_name)),
        cwd=KIT_ROOT,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
        input=payload,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode in {0, 2}
    assert not marker.exists()


def test_packaged_runtime_resolves_bare_python_from_path(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    project = tmp_path / "project"
    package = tmp_path / "package"
    fake_bin = tmp_path / "bin"
    seed.mkdir()
    project.mkdir()
    fake_bin.mkdir()
    assert _install(seed).returncode == 0
    shutil.copytree(seed / ".agent-flow" / "runtime" / "node", package)
    python = fake_bin / "python3.12"
    yaml_site = Path(yaml.__file__).resolve().parent.parent
    python.write_text(
        "#!/bin/sh\n"
        f"PYTHONPATH={shlex.quote(str(yaml_site))}${{PYTHONPATH:+:$PYTHONPATH}}\n"
        "export PYTHONPATH\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    env = dict(os.environ)
    env["HOME"] = str(project.parent / "test-home")
    env["AGENT_FLOW_AUTO_EXTERNAL_SKILLS"] = "1"
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    for name in ("PYTHON", "PYTHON_EXECUTABLE", "VIRTUAL_ENV"):
        env.pop(name, None)
    runtime_entry = package / "bin" / "agent-flow-kit.mjs"

    result = subprocess.run(
        (_node(), str(runtime_entry), "install"),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    kit = json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
    assert kit["project_runtime_contract"]["python"]["path"] == str(python)


def test_runtime_contract_rejects_adjacent_python_dylib_drift(tmp_path: Path) -> None:
    project = tmp_path / "project"
    runtime = tmp_path / "python-runtime"
    python = runtime / "bin" / "python3"
    dependency = runtime / "lib" / "libpython-agent-flow.dylib"
    project.mkdir()
    python.parent.mkdir(parents=True)
    dependency.parent.mkdir(parents=True)
    yaml_site = Path(yaml.__file__).resolve().parent.parent
    python.write_text(
        "#!/bin/sh\n"
        f"PYTHONPATH={shlex.quote(str(yaml_site))}${{PYTHONPATH:+:$PYTHONPATH}}\n"
        "export PYTHONPATH\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    dependency.write_bytes(b"contracted dependency\n")
    env = {"PYTHON": str(python), "PYTHON_EXECUTABLE": str(python)}
    installed = _install(project, env=env)
    assert installed.returncode == 0, installed.stderr
    kit = json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
    dependencies = kit["project_runtime_contract"]["python"]["dependencies"]
    assert [entry["name"] for entry in dependencies] == [dependency.name]

    dependency.write_bytes(b"changed dependency\n")
    result = _command(project, "status", env=env)

    assert result.returncode != 0
    assert "executable dependencies changed" in result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="Mach-O dependency graph is macOS-only")
def test_runtime_contract_pins_non_system_macho_dependencies(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    installed = _install(project)
    assert installed.returncode == 0, installed.stderr
    kit = json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
    contracted = {
        entry["path"]
        for entry in kit["project_runtime_contract"]["node"]["dependencies"]
    }
    for executable in ("node", "git", "python"):
        for entry in kit["project_runtime_contract"][executable]["dependencies"]:
            assert entry["load_commands"]
            assert entry["stage_kind"] in {"library", "framework"}
            if entry["stage_kind"] == "framework":
                assert ".framework/Versions/" in entry["stage_relative"]
    otool = subprocess.run(
        ("/usr/bin/otool", "-L", str(Path(_node()).resolve())),
        text=True,
        capture_output=True,
        check=True,
    )
    direct_non_system = {
        str(Path(line.strip().split(" (", 1)[0]).resolve())
        for line in otool.stdout.splitlines()[1:]
        if line.strip().startswith("/")
        and not line.strip().startswith(("/usr/lib/", "/System/Library/"))
    }
    if not direct_non_system:
        pytest.skip("Node has no non-system Mach-O dependencies")

    assert direct_non_system <= contracted


@pytest.mark.skipif(sys.platform != "darwin", reason="otool is a macOS runtime dependency")
def test_runtime_install_fails_closed_when_otool_inspection_fails(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = _install(
        project,
        env={**os.environ, "AGENT_FLOW_TEST_OTOOL_FAILURE": "1"},
    )

    assert result.returncode != 0
    assert "Mach-O dependency inspection failed" in result.stderr


def test_same_execution_concurrent_node_start_preserves_starting_binding(
    tmp_path: Path,
) -> None:
    from agent_flow.core.worktrees import create_worktree, plan_worktree

    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(("git", "init", "-b", "main", str(project)), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(project), "config", "user.name", "Test User"), check=True)
    subprocess.run(("git", "-C", str(project), "config", "user.email", "test@example.com"), check=True)
    (project / "README.md").write_text("project\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(project), "add", "README.md"), check=True)
    subprocess.run(("git", "-C", str(project), "commit", "-m", "initial"), check=True, capture_output=True)
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0",
        "AGENT_FLOW_EXECUTION_ID": "same-node-execution",
        "AGENT_FLOW_TEST_HOLD_AFTER_NODE_START_BINDING_MS": "1500",
    }
    assert _install(project, env=env).returncode == 0
    first = create_worktree(root=project, plan=plan_worktree(root=project, name="first"), allow_dirty=True)
    second = create_worktree(root=project, plan=plan_worktree(root=project, name="second"), allow_dirty=True)
    process = subprocess.Popen(
        (
            _node(),
            str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"),
            "run",
            "start",
            "--task",
            "first",
            "--run-id",
            "first",
        ),
        cwd=first.path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stderr is not None
    assert "agent-flow:test-node-start-binding-published" in process.stderr.readline()

    kit = json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
    python = kit["project_runtime_contract"]["python"]["path"]
    python_env = {
        **{key: value for key, value in env.items() if key != "AGENT_FLOW_TEST_HOLD_AFTER_NODE_START_BINDING_MS"},
        "PYTHONPATH": str(project / ".agent-flow" / "runtime" / "python"),
    }
    python_raced = subprocess.run(
        (
            python,
            "-m",
            "agent_flow.cli",
            "run",
            "second",
            "--root",
            str(project),
            "--worktree",
            "second",
        ),
        cwd=project,
        env=python_env,
        text=True,
        capture_output=True,
        check=False,
    )

    raced = _command(
        second.path,
        "run",
        "start",
        "--task",
        "second",
        "--run-id",
        "second",
        env={key: value for key, value in env.items() if key != "AGENT_FLOW_TEST_HOLD_AFTER_NODE_START_BINDING_MS"},
    )
    stdout, stderr = process.communicate(timeout=10)

    assert python_raced.returncode == 2
    assert "differs from bound worktree feat-first" in python_raced.stderr
    assert raced.returncode != 0
    assert "execution already owns active run first" in raced.stderr
    assert process.returncode == 0, stderr or stdout


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


def test_sandboxed_gate_cannot_write_outside_pinned_workspace(tmp_path: Path) -> None:
    if sys.platform != "darwin" and shutil.which("bwrap") is None:
        pytest.skip("platform sandbox is unavailable")
    project = tmp_path / "project"
    outside = tmp_path / "outside.txt"
    project.mkdir()
    outside.write_text("outside\n", encoding="utf-8")
    assert _install(project).returncode == 0
    started = _command(project, "run", "start", "--task", "sandbox")
    assert started.returncode == 0, started.stderr
    benign = _command(project, "gate", "--", sys.executable, "-c", "print('ok')")
    escaped = _command(
        project,
        "gate",
        "--",
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(outside)!r}).write_text('changed')",
    )

    assert benign.returncode == 0, benign.stderr
    assert escaped.returncode != 0
    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_sandboxed_gate_never_uses_path_shadowed_bubblewrap(tmp_path: Path) -> None:
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    marker = tmp_path / "fake-bwrap-ran"
    project.mkdir()
    fake_bin.mkdir()
    fake = fake_bin / "bwrap"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    assert _install(project).returncode == 0
    assert _command(project, "run", "start", "--task", "sandbox").returncode == 0
    env = {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"}

    result = _command(project, "gate", "--", sys.executable, "-c", "print('ok')", env=env)

    assert not marker.exists()
    if sys.platform == "darwin" or Path("/usr/bin/bwrap").is_file():
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0


@pytest.mark.parametrize("action", ("install", "sync"))
def test_internal_project_mutation_rejects_forged_immutable_canary(
    tmp_path: Path,
    action: str,
) -> None:
    if sys.platform != "darwin" or not hasattr(os, "chflags"):
        pytest.skip("macOS immutable flags are required")
    project = tmp_path / "project"
    canary = tmp_path / "outside-canary"
    project.mkdir()
    agent_flow = project / ".agent-flow"
    agent_flow.mkdir()
    nonce = "a" * 48
    canary.write_text(f"{nonce}\n", encoding="utf-8")
    os.chflags(canary, stat.UF_IMMUTABLE)
    lock_path = agent_flow / "install.flock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{nonce}\n".encode())
        os.fsync(descriptor)
        env = {
            **os.environ,
            "AGENT_FLOW_INSTALL_FLOCK_FD": str(descriptor),
            "AGENT_FLOW_INSTALL_FLOCK_NONCE": nonce,
        }
        result = subprocess.run(
            (
                _node(),
                str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"),
                "__sandboxed-mutation",
                action,
                str(project),
                str(canary),
                nonce,
            ),
            cwd=project,
            env=env,
            pass_fds=(descriptor,),
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        os.chflags(canary, 0)
        os.close(descriptor)

    assert result.returncode != 0
    assert "sandbox policy proof is invalid" in result.stderr
    assert not (project / "AGENTS.md").exists()
    assert not (project / "CLAUDE.md").exists()
    assert not (agent_flow / "kit.json").exists()


def test_sandboxed_python_cli_uses_the_contracted_interpreter(tmp_path: Path) -> None:
    if sys.platform != "darwin" and not Path("/usr/bin/bwrap").is_file():
        pytest.skip("platform sandbox is unavailable")
    project = tmp_path / "project"
    marker = project / "contracted-python-ran"
    outside = tmp_path / "outside-python-ran"
    python = project / "python-with-yaml"
    project.mkdir()
    yaml_site = Path(yaml.__file__).resolve().parent.parent
    python.write_text(
        "#!/bin/sh\n"
        f"PYTHONPATH={shlex.quote(str(yaml_site))}${{PYTHONPATH:+:$PYTHONPATH}}\n"
        "export PYTHONPATH\n"
        f"if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"agent_flow.cli\" ] "
        f"&& [ \"$3\" = \"architecture-lint\" ]; then "
        f"/usr/bin/touch {shlex.quote(str(outside))} 2>/dev/null || true; "
        f": > {shlex.quote(str(marker))}; fi\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    env = {"PYTHON": str(python), "PYTHON_EXECUTABLE": str(python)}
    assert _install(project, env=env).returncode == 0
    marker.unlink(missing_ok=True)
    assert _command(project, "run", "start", "--task", "python contract", env=env).returncode == 0
    marker.unlink(missing_ok=True)

    result = _command(project, "architecture-lint", env=env)

    assert result.returncode == 0, result.stderr
    assert marker.is_file()
    assert not outside.exists()


@pytest.mark.parametrize("kit_content", ["{}", "{not json"])
def test_sandboxed_python_cli_rejects_invalid_existing_kit(
    tmp_path: Path,
    kit_content: str,
) -> None:
    project = tmp_path / "project"
    agent_flow = project / ".agent-flow"
    project.mkdir()
    agent_flow.mkdir()
    (agent_flow / "kit.json").write_text(kit_content, encoding="utf-8")

    result = _command(project, "architecture-lint", "--root", str(project))

    assert result.returncode != 0
    assert "project runtime contract commitment is invalid" in result.stderr


def test_sandboxed_python_cli_rejects_broken_kit_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    agent_flow = project / ".agent-flow"
    project.mkdir()
    agent_flow.mkdir()
    (agent_flow / "kit.json").symlink_to(agent_flow / "missing-kit.json")

    result = _command(project, "architecture-lint", "--root", str(project))

    assert result.returncode != 0
    assert "project runtime contract commitment is invalid" in result.stderr


def test_sandboxed_python_cli_rejects_conflicting_root_forms(tmp_path: Path) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()

    result = _command(
        project,
        "architecture-lint",
        "--root",
        str(other),
        f"--root={project}",
    )

    assert result.returncode != 0
    assert "conflicting --root arguments" in result.stderr


def test_node_skill_repin_rejects_external_run_directory_before_writing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside-run"
    project.mkdir()
    outside.mkdir()
    assert _install(project).returncode == 0
    execution_env = {
        "AGENT_FLOW_ACTIVE_HOST": "codex",
        "AGENT_FLOW_EXECUTION_ID": "repin-boundary",
    }
    started = _command(
        project,
        "run",
        "start",
        "--task",
        "repin boundary",
        "--run-id",
        "repin-boundary",
        env=execution_env,
    )
    assert started.returncode == 0, started.stderr
    state_path = _authoritative_current_run_state_path(project)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["run_dir"] = str(outside)
    state["skill_plan_hash"] = "0" * 64
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = _command(project, "run", "status", env=execution_env)

    assert result.returncode != 0
    assert "run directory is outside its authenticated root" in result.stderr
    assert not (outside / "manifest.json").exists()


def test_completed_node_run_is_not_accepted_as_active_gate_authority(tmp_path: Path) -> None:
    project = tmp_path / "project"
    marker = project / "gate-ran"
    project.mkdir()
    subprocess.run(("git", "init", "-b", "main", str(project)), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(project), "config", "user.name", "Test User"), check=True)
    subprocess.run(("git", "-C", str(project), "config", "user.email", "test@example.com"), check=True)
    (project / "README.md").write_text("project\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(project), "add", "README.md"), check=True)
    subprocess.run(("git", "-C", str(project), "commit", "-m", "initial"), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(project), "switch", "-c", "feat/test"), check=True, capture_output=True)
    assert _install(project).returncode == 0
    execution_env = {
        "AGENT_FLOW_ACTIVE_HOST": "codex",
        "AGENT_FLOW_EXECUTION_ID": "completed-gate",
    }
    started = _command(
        project,
        "run",
        "start",
        "--task",
        "completed gate",
        "--run-id",
        "completed-gate",
        env=execution_env,
    )
    assert started.returncode == 0, started.stderr
    state_path = _authoritative_current_run_state_path(project)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = "complete"
    state["phase"] = "complete"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    run_manifest = project / state["run_dir"] / "manifest.json"
    run_manifest.write_text(json.dumps(state), encoding="utf-8")

    result = _command(
        project,
        "gate",
        "--",
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
        env=execution_env,
    )

    assert result.returncode != 0
    assert "no active run" in result.stderr
    assert not marker.exists()


def test_node_current_run_state_isolated_for_ten_cross_host_executions(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    shared_env = {
        "HOME": str(project.parent / "test-home"),
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0",
    }
    assert _install(project, env={**shared_env, "AGENT_FLOW_HOST": "codex"}).returncode == 0
    installed_index = json.loads(
        (project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8")
    )
    assert installed_index["catalog_hosts"] == []
    executions: list[tuple[str, str, dict[str, str]]] = []

    for index in range(10):
        host = ("codex", "claude", "omp")[index % 3]
        run_id = f"parallel-{index}"
        env = {
            **shared_env,
            "AGENT_FLOW_ACTIVE_HOST": host,
            "AGENT_FLOW_EXECUTION_ID": f"session-{index}",
            "AGENT_FLOW_AGENT_ID": f"agent-{index}",
        }
        executions.append((host, run_id, env))

    def start_execution(item: tuple[str, str, dict[str, str]]) -> subprocess.CompletedProcess[str]:
        _host, run_id, env = item
        return _command(
            project,
            "run",
            "start",
            "--task",
            f"parallel task {run_id}",
            "--run-id",
            run_id,
            env=env,
        )

    with ThreadPoolExecutor(max_workers=10) as executor:
        starts = list(executor.map(start_execution, executions))
    for started in starts:
        assert started.returncode == 0, started.stderr
        assert "agent-flow installed" not in started.stdout

    state_paths = list((project / ".agent-flow" / "state" / "current-runs").glob("*.json"))
    assert len(state_paths) == 10
    assert {json.loads(path.read_text(encoding="utf-8"))["run_id"] for path in state_paths} == {
        run_id for _host, run_id, _env in executions
    }
    assert not (project / ".agent-flow" / "state" / "current-run.json").exists()

    unbound = _command(
        project,
        "run",
        "status",
        env={
            **shared_env,
            "AGENT_FLOW_ACTIVE_HOST": "codex",
            "AGENT_FLOW_EXECUTION_ID": "",
            "AGENT_FLOW_SESSION_ID": "",
            "CODEX_THREAD_ID": "",
            "CODEX_SESSION_ID": "",
        },
    )
    assert unbound.returncode != 0
    assert "multiple active runs require an execution identity" in unbound.stderr

    def read_execution(item: tuple[str, str, dict[str, str]]) -> subprocess.CompletedProcess[str]:
        _host, _run_id, env = item
        return _command(project, "run", "status", env=env)

    with ThreadPoolExecutor(max_workers=10) as executor:
        statuses = list(executor.map(read_execution, executions))
    for (host, run_id, _env), status in zip(executions, statuses):
        assert status.returncode == 0, f"{host}: {status.stderr}"
        assert f"run: full-feature/{run_id}" in status.stdout


def test_node_same_execution_cannot_replace_its_active_scoped_run(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    env = {
        "HOME": str(project.parent / "test-home"),
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0",
        "AGENT_FLOW_ACTIVE_HOST": "codex",
        "AGENT_FLOW_EXECUTION_ID": "session-1",
    }
    assert _install(project, env=env).returncode == 0
    first = _command(
        project,
        "run",
        "start",
        "--task",
        "first task",
        "--run-id",
        "first",
        env=env,
    )
    state_path = _authoritative_current_run_state_path(project)
    original_state = state_path.read_text(encoding="utf-8")

    second = _command(
        project,
        "run",
        "start",
        "--task",
        "second task",
        "--run-id",
        "second",
        env=env,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode != 0
    assert "execution_binding_conflict" in second.stderr
    assert state_path.read_text(encoding="utf-8") == original_state
    assert not (project / ".agent-flow" / "runs" / "full-feature" / "second").exists()


def test_node_unbound_start_cannot_create_legacy_run_beside_scoped_run(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    shared_env = {
        "HOME": str(project.parent / "test-home"),
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0",
        "AGENT_FLOW_ACTIVE_HOST": "codex",
    }
    bound_env = {**shared_env, "AGENT_FLOW_EXECUTION_ID": "session-1"}
    assert _install(project, env=bound_env).returncode == 0
    first = _command(
        project,
        "run",
        "start",
        "--task",
        "scoped",
        "--run-id",
        "scoped",
        env=bound_env,
    )
    unbound_env = {
        **shared_env,
        "AGENT_FLOW_EXECUTION_ID": "",
        "AGENT_FLOW_SESSION_ID": "",
        "CODEX_THREAD_ID": "",
        "CODEX_SESSION_ID": "",
    }
    second = _command(
        project,
        "run",
        "start",
        "--task",
        "legacy",
        "--run-id",
        "legacy",
        env=unbound_env,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode != 0
    assert "execution_identity_missing" in second.stderr
    assert not (project / ".agent-flow" / "state" / "current-run.json").exists()
    assert not (project / ".agent-flow" / "runs" / "full-feature" / "legacy").exists()


def test_node_different_execution_cannot_share_active_git_worktree(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(("git", "init", "-b", "main", str(project)), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(project), "config", "user.name", "Test User"), check=True)
    subprocess.run(("git", "-C", str(project), "config", "user.email", "test@example.com"), check=True)
    (project / "README.md").write_text("project\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(project), "add", "README.md"), check=True)
    subprocess.run(("git", "-C", str(project), "commit", "-m", "initial"), check=True, capture_output=True)
    shared_env = {
        "HOME": str(project.parent / "test-home"),
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0",
        "AGENT_FLOW_ACTIVE_HOST": "codex",
    }
    assert _install(project, env=shared_env).returncode == 0
    worktree = project / ".agent-flow" / "worktrees" / "feat-shared"
    subprocess.run(
        ("git", "-C", str(project), "worktree", "add", "-b", "feat/shared", str(worktree), "main"),
        check=True,
        capture_output=True,
    )
    identity = capture_workspace_identity(worktree)
    claims_root = project / ".git" / "agent-flow" / "workspace-start-claims"
    claims_root.mkdir(parents=True)
    stale_claim = acquire_workspace_start_claim(identity, run_id="stale-node-run")
    claim_path = stale_claim.path
    stale_payload = json.loads(claim_path.read_text(encoding="utf-8"))
    release_workspace_start_claim(stale_claim)
    stale_payload["process_start_id"] = "reused-pid-start-identity"
    claim_path.write_text(json.dumps(stale_payload), encoding="utf-8")
    starts = (
        ("first", "session-1"),
        ("second", "session-2"),
    )

    def start(item: tuple[str, str]) -> subprocess.CompletedProcess[str]:
        run_id, session_id = item
        return _command(
            worktree,
            "run",
            "start",
            "--task",
            run_id,
            "--run-id",
            run_id,
            env={**shared_env, "AGENT_FLOW_EXECUTION_ID": session_id},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(start, starts))

    succeeded = [result for result in results if result.returncode == 0]
    blocked = [result for result in results if result.returncode != 0]
    assert len(succeeded) == 1, [result.stderr for result in results]
    assert len(blocked) == 1
    assert "execution_binding_conflict" in blocked[0].stderr
    run_dirs = list((project / ".agent-flow" / "runs" / "full-feature").iterdir())
    assert len(run_dirs) == 1
    assert not claim_path.exists()


def test_node_start_honors_python_claim_and_bindingless_active_run(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(("git", "init", "-b", "main", str(project)), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(project), "config", "user.name", "Test User"), check=True)
    subprocess.run(("git", "-C", str(project), "config", "user.email", "test@example.com"), check=True)
    (project / "README.md").write_text("project\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(project), "add", "README.md"), check=True)
    subprocess.run(("git", "-C", str(project), "commit", "-m", "initial"), check=True, capture_output=True)
    shared_env = {
        "HOME": str(project.parent / "test-home"),
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0",
        "AGENT_FLOW_ACTIVE_HOST": "codex",
    }
    assert _install(project, env=shared_env).returncode == 0
    worktree = project / ".agent-flow" / "worktrees" / "feat-python"
    subprocess.run(
        ("git", "-C", str(project), "worktree", "add", "-b", "feat/python", str(worktree), "main"),
        check=True,
        capture_output=True,
    )
    identity = capture_workspace_identity(worktree)
    claims_root = project / ".git" / "agent-flow" / "workspace-start-claims"
    claims_root.mkdir(parents=True)
    claim_path = claims_root / (
        hashlib.sha256(identity.workspace_root.encode("utf-8")).hexdigest() + ".lock"
    )
    claim_path.write_text(
        json.dumps(
            {
                "version": 1,
                "pid": 99_999_999,
                "token": "stale-python-claim",
                "workspace_root": identity.workspace_root,
            }
        ),
        encoding="utf-8",
    )
    claim = acquire_workspace_start_claim(identity)
    try:
        claim_blocked = _command(
            worktree,
            "run",
            "start",
            "--task",
            "claim blocked",
            "--run-id",
            "claim-blocked",
            env={**shared_env, "AGENT_FLOW_EXECUTION_ID": "session-1"},
        )
    finally:
        release_workspace_start_claim(claim)

    python_run = (
        project
        / ".git"
        / "agent-flow"
        / "worktrees"
        / "python-active"
        / ".agent-flow"
        / "runs"
        / "python-1"
    )
    python_run.mkdir(parents=True)
    (python_run.parents[2] / "manifest.json").write_text(
        json.dumps({"identity": identity.to_dict()}),
        encoding="utf-8",
    )
    (python_run / "active").write_text("", encoding="utf-8")
    (python_run / "meta.json").write_text(
        json.dumps({"run_id": "python-1"}),
        encoding="utf-8",
    )
    active_blocked = _command(
        worktree,
        "run",
        "start",
        "--task",
        "active blocked",
        "--run-id",
        "active-blocked",
        env={**shared_env, "AGENT_FLOW_EXECUTION_ID": "session-2"},
    )

    assert claim_blocked.returncode != 0
    assert "workspace start is already in progress" in claim_blocked.stderr
    assert active_blocked.returncode != 0
    assert "workspace already owns active run python-1" in active_blocked.stderr
    assert not (project / ".agent-flow" / "runs" / "full-feature" / "claim-blocked").exists()
    assert not (project / ".agent-flow" / "runs" / "full-feature" / "active-blocked").exists()


@pytest.mark.parametrize(
    ("failure_environment", "failure_code"),
    (
        ({"AGENT_FLOW_TEST_CRASH_AFTER_NODE_START_MANIFEST": "1"}, 86),
        ({"AGENT_FLOW_TEST_CRASH_AFTER_NODE_START_CURRENT": "1"}, 87),
        ({"AGENT_FLOW_TEST_FAIL_AFTER_NODE_START_CURRENT": "1"}, 1),
    ),
)
def test_node_start_recovers_incomplete_publication(
    tmp_path: Path,
    failure_environment: dict[str, str],
    failure_code: int,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(("git", "init", "-b", "main", str(project)), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(project), "config", "user.name", "Test User"), check=True)
    subprocess.run(("git", "-C", str(project), "config", "user.email", "test@example.com"), check=True)
    (project / "README.md").write_text("project\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(project), "add", "README.md"), check=True)
    subprocess.run(("git", "-C", str(project), "commit", "-m", "initial"), check=True, capture_output=True)
    shared_env = {
        "HOME": str(project.parent / "test-home"),
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0",
        "AGENT_FLOW_ACTIVE_HOST": "codex",
    }
    assert _install(project, env=shared_env).returncode == 0
    worktree = project / ".agent-flow" / "worktrees" / "feat-crash"
    subprocess.run(
        ("git", "-C", str(project), "worktree", "add", "-b", "feat/crash", str(worktree), "main"),
        check=True,
        capture_output=True,
    )
    crashed = _command(
        worktree,
        "run",
        "start",
        "--task",
        "crashed",
        "--run-id",
        "crashed",
        env={
            **shared_env,
            "AGENT_FLOW_EXECUTION_ID": "crashed-session",
            **failure_environment,
        },
    )
    recovered = _command(
        worktree,
        "run",
        "start",
        "--task",
        "recovered",
        "--run-id",
        "recovered",
        env={**shared_env, "AGENT_FLOW_EXECUTION_ID": "recovered-session"},
    )

    assert crashed.returncode == failure_code
    assert recovered.returncode == 0, recovered.stderr
    assert not (project / ".agent-flow" / "runs" / "full-feature" / "crashed").exists()
    assert (project / ".agent-flow" / "runs" / "full-feature" / "recovered").is_dir()


def test_node_completion_records_current_worktree_finalizer_generation(
    tmp_path: Path,
) -> None:
    from agent_flow.core.worktrees import create_worktree, plan_worktree

    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(("git", "init", "-b", "main", str(project)), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(project), "config", "user.name", "Test User"), check=True)
    subprocess.run(("git", "-C", str(project), "config", "user.email", "test@example.com"), check=True)
    (project / "README.md").write_text("project\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(project), "add", "README.md"), check=True)
    subprocess.run(("git", "-C", str(project), "commit", "-m", "initial"), check=True, capture_output=True)
    env = {
        "HOME": str(project.parent / "test-home"),
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0",
        "AGENT_FLOW_ACTIVE_HOST": "codex",
        "AGENT_FLOW_EXECUTION_ID": "node-finalizer",
    }
    assert _install(project, env=env).returncode == 0
    plan = plan_worktree(root=project, name="node-finalizer")
    created = create_worktree(
        root=project,
        plan=plan,
        allow_dirty=True,
    )
    worktree = created.path
    started = _command(
        worktree,
        "run",
        "start",
        "--task",
        "node finalizer",
        "--run-id",
        "node-finalizer",
        env=env,
    )
    assert started.returncode == 0, started.stderr
    state_path = _authoritative_current_run_state_path(project)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    workflow = yaml.safe_load(
        (project / ".agent-flow" / "workflows" / "full-feature.yaml").read_text(
            encoding="utf-8"
        )
    )
    final_index = len(workflow["phases"]) - 1
    final_phase = workflow["phases"][final_index]
    state.update(
        {
            "phase_index": final_index,
            "phase": final_phase["id"],
            "status": "running",
            "phase_entered_at": "2020-01-01T00:00:00.000Z",
        }
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    run_dir = project / state["run_dir"]
    (run_dir / "manifest.json").write_text(json.dumps(state), encoding="utf-8")
    artifact_path = run_dir / "artifacts" / f"{final_phase['id']}.md"
    artifact_path.write_text("done\n", encoding="utf-8")

    completed = _command(worktree, "run", "advance", env=env)

    assert completed.returncode == 0, completed.stderr
    finalizer = json.loads(
        (
            project
            / ".git"
            / "agent-flow"
            / "worktrees"
            / worktree.name
            / "finalizer.json"
        ).read_text(encoding="utf-8")
    )
    assert finalizer["generation"] == 1
    assert finalizer["execution"]["session_id"] == "node-finalizer"
    assert finalizer["run_id"] == "node-finalizer"

    completed_state = json.loads(state_path.read_text(encoding="utf-8"))
    execution = ExecutionIdentity("codex", "node-finalizer", "")
    binding_path = (
        project
        / ".git"
        / "agent-flow"
        / "executions"
        / f"{execution.digest}.json"
    )
    binding_path.write_text(
        json.dumps(
            {
                "version": 2,
                "execution": execution.to_dict(),
                "workspace": completed_state["workspace"],
                "workspace_name": worktree.name,
                "run_id": completed_state["run_id"],
                "run_dir": str(run_dir.resolve()),
                "bound_at": "2020-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    workspace = completed_state["workspace"]
    claim_digest = hashlib.sha256(workspace["workspace_root"].encode()).hexdigest()
    claim_path = (
        project
        / ".git"
        / "agent-flow"
        / "workspace-start-claims"
        / f"{claim_digest}.lock"
    )
    leader_metadata = project.stat()
    claim_path.write_text(
        json.dumps(
            {
                "version": 2,
                "pid": 999_999_999,
                "process_start_id": "stale-process",
                "token": "stale-token",
                "run_id": "finalize:node-finalizer",
                "leader_root": str(project.resolve()),
                "leader_device": leader_metadata.st_dev,
                "leader_inode": leader_metadata.st_ino,
                "workspace_root": workspace["workspace_root"],
                "workspace_git_dir": workspace["git_dir"],
                "workspace_branch": workspace["branch"],
                "workspace_head": workspace["head"],
                "workspace_device": workspace["device"],
                "workspace_inode": workspace["inode"],
                "acquired_at": "2020-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    recovered = _command(worktree, "run", "advance", env=env)

    assert recovered.returncode == 0, recovered.stderr
    assert not binding_path.exists()
    assert not claim_path.exists()
    recovered_finalizer = json.loads(
        (
            project
            / ".git"
            / "agent-flow"
            / "worktrees"
            / worktree.name
            / "finalizer.json"
        ).read_text(encoding="utf-8")
    )
    assert recovered_finalizer["generation"] == 1

    next_env = {**env, "AGENT_FLOW_EXECUTION_ID": "node-finalizer-next"}
    next_started = _command(
        worktree,
        "run",
        "start",
        "--task",
        "next node finalizer",
        "--run-id",
        "node-finalizer-next",
        env=next_env,
    )
    assert next_started.returncode == 0, next_started.stderr

    next_execution = ExecutionIdentity("codex", "node-finalizer-next", "")
    next_state_path = (
        project / ".git" / "agent-flow" / "current-runs" / f"{next_execution.digest}.json"
    )
    next_state = json.loads(next_state_path.read_text(encoding="utf-8"))
    next_state.update(
        {
            "phase_index": final_index,
            "phase": final_phase["id"],
            "status": "running",
            "phase_entered_at": "2020-01-02T00:00:00.000Z",
        }
    )
    next_state_path.write_text(json.dumps(next_state), encoding="utf-8")
    next_run_dir = project / next_state["run_dir"]
    (next_run_dir / "manifest.json").write_text(json.dumps(next_state), encoding="utf-8")
    (next_run_dir / "artifacts" / f"{final_phase['id']}.md").write_text(
        "done\n", encoding="utf-8"
    )
    next_crashed = _command(
        worktree,
        "run",
        "advance",
        env={**next_env, "AGENT_FLOW_TEST_CRASH_AFTER_NODE_FINALIZER": "1"},
    )
    assert next_crashed.returncode == 92
    next_completed = _command(worktree, "run", "advance", env=next_env)
    assert next_completed.returncode == 0, next_completed.stderr

    latest_finalizer_path = (
        project / ".git" / "agent-flow" / "worktrees" / worktree.name / "finalizer.json"
    )
    latest_finalizer = json.loads(latest_finalizer_path.read_text(encoding="utf-8"))
    assert latest_finalizer["generation"] == 2
    assert latest_finalizer["run_id"] == "node-finalizer-next"

    stale_retry = _command(worktree, "run", "advance", env=env)
    assert stale_retry.returncode != 0
    assert "execution_finalizer_stale" in stale_retry.stderr
    assert json.loads(latest_finalizer_path.read_text(encoding="utf-8")) == latest_finalizer

    resolved = resolve_execution_finalizer_workspace(
        project,
        next_execution,
        worktree.name,
    )
    assert resolved.identity.workspace_root == str(worktree.resolve())


def test_node_runtime_rejects_symlinked_git_common_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(("git", "init", "-b", "main", str(project)), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(project), "config", "user.name", "Test User"), check=True)
    subprocess.run(("git", "-C", str(project), "config", "user.email", "test@example.com"), check=True)
    (project / "README.md").write_text("project\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(project), "add", "README.md"), check=True)
    subprocess.run(("git", "-C", str(project), "commit", "-m", "initial"), check=True, capture_output=True)
    env = {
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0",
        "AGENT_FLOW_ACTIVE_HOST": "codex",
        "AGENT_FLOW_EXECUTION_ID": "symlinked-common",
    }
    installed = _install(project, env=env)
    assert installed.returncode == 0, installed.stderr
    real_git = tmp_path / "real-git"
    (project / ".git").rename(real_git)
    (project / ".git").symlink_to(real_git, target_is_directory=True)

    result = _command(
        project,
        "run",
        "start",
        "--task",
        "symlinked git common",
        "--run-id",
        "symlinked-common",
        env=env,
    )

    assert result.returncode != 0
    assert (
        "git-private metadata root is not an owned directory" in result.stderr
        or "pinned workspace belongs to a different repository" in result.stderr
    )


def test_node_runtime_rejects_symlinked_git_private_agent_flow_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(("git", "init", "-b", "main", str(project)), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(project), "config", "user.name", "Test User"), check=True)
    subprocess.run(("git", "-C", str(project), "config", "user.email", "test@example.com"), check=True)
    (project / "README.md").write_text("project\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(project), "add", "README.md"), check=True)
    subprocess.run(("git", "-C", str(project), "commit", "-m", "initial"), check=True, capture_output=True)
    env = {
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0",
        "AGENT_FLOW_ACTIVE_HOST": "codex",
        "AGENT_FLOW_EXECUTION_ID": "symlinked-private-root",
    }
    assert _install(project, env=env).returncode == 0
    worktree = project / ".agent-flow" / "worktrees" / "feat-private-root"
    subprocess.run(
        ("git", "-C", str(project), "worktree", "add", "-b", "feat/private-root", str(worktree), "main"),
        check=True,
        capture_output=True,
    )
    private_root = project / ".git" / "agent-flow"
    outside = tmp_path / "outside-agent-flow"
    outside.mkdir()
    if private_root.exists():
        shutil.rmtree(private_root)
    private_root.symlink_to(outside, target_is_directory=True)

    result = _command(
        worktree,
        "run",
        "start",
        "--task",
        "symlinked private root",
        "--run-id",
        "symlinked-private-root",
        env=env,
    )

    assert result.returncode != 0
    assert "git-private metadata path is not an owned directory" in result.stderr
    assert not any(outside.iterdir())


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


@pytest.mark.parametrize("interruption", ("rollback", "recovery"))
def test_drifted_managed_skill_backup_restores_from_pinned_state(
    tmp_path: Path,
    interruption: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    skill = (
        project
        / ".agent-flow"
        / "skills"
        / "full-feature-workflow"
        / "SKILL.md"
    )
    drifted = "drifted managed skill\n"
    skill.write_text(drifted, encoding="utf-8")

    if interruption == "rollback":
        failed = _install(
            project,
            env={"AGENT_FLOW_TEST_FAIL_AFTER_SKILL_MATERIALIZATION": "1"},
        )

        assert failed.returncode != 0
        assert "injected failure after skill materialization" in failed.stderr
        assert skill.read_text(encoding="utf-8") == drifted
        assert not (project / ".agent-flow" / "install-transaction").exists()
        return

    crashed = _install(
        project,
        env={"AGENT_FLOW_TEST_CRASH_AFTER_SKILL_INDEX": "1"},
    )

    assert crashed.returncode == 87
    backup_skill = (
        project
        / ".agent-flow"
        / "install-transaction"
        / "skills-backup"
        / "full-feature-workflow"
        / "SKILL.md"
    )
    assert backup_skill.read_text(encoding="utf-8") == drifted
    recovered = _install(project)
    assert recovered.returncode == 0, recovered.stderr
    assert not (project / ".agent-flow" / "install-transaction").exists()


def test_self_install_reinstall_treats_generated_agent_flow_skill_as_managed(tmp_path: Path) -> None:
    kit = tmp_path / "kit"
    kit.mkdir()
    for relative in (
        "bin",
        "lib",
        "workflows",
        "profiles",
        "skills",
        "templates",
        "bootstrap",
        "scripts",
        "src",
        ".Codex/agents",
        ".Codex/rules",
        ".Codex/context",
        ".claude/agents",
        ".omp/agents",
    ):
        source = KIT_ROOT / relative
        if source.is_dir():
            shutil.copytree(source, kit / relative)

    process_env = dict(os.environ)
    process_env.update(
        {
            "HOME": str(tmp_path / "test-home"),
            "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0",
            "PYTHON": sys.executable,
            "CODEX_THREAD_ID": "",
            "CODEX_CLI": "",
        }
    )
    command = (_node(), str(kit / "bin" / "agent-flow-kit.mjs"), "install", "--force-managed")
    first = subprocess.run(
        (*command, "--skill", "agent-flow"),
        cwd=kit,
        text=True,
        capture_output=True,
        check=False,
        env=process_env,
    )
    assert first.returncode == 0, first.stderr
    launcher_command = (str(kit / ".agent-flow" / "bin" / "agent-flow"), "install", "--force-managed")
    second = subprocess.run(
        launcher_command,
        cwd=kit,
        text=True,
        capture_output=True,
        check=False,
        env=process_env,
    )
    third = subprocess.run(
        launcher_command,
        cwd=kit,
        text=True,
        capture_output=True,
        check=False,
        env=process_env,
    )

    assert second.returncode == 0, second.stderr
    assert third.returncode == 0, third.stderr
    assert "unmanaged skill entry conflicts" not in third.stderr
    assert not (kit / ".agent-flow" / "install-transaction").exists()
    assert (kit / ".agent-flow" / "skills" / "agent-flow" / "SKILL.md").is_file()


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
    codex = json.loads((project / ".Codex" / "hooks.json").read_text(encoding="utf-8"))
    claude = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    omp = (project / ".omp" / "extensions" / "agent-flow-hooks.ts").read_text(encoding="utf-8")
    commands = [
        hook["command"]
        for settings in (codex, claude)
        for entries in settings["hooks"].values()
        for entry in entries
        for hook in entry["hooks"]
    ]
    assert commands
    assert max(map(len, commands)) < 400
    assert all("AGENT_FLOW_MANAGED_HOOK_SOURCE_PATH" not in command for command in commands)
    assert all("agent-flow-managed-hook-" not in command for command in commands)
    assert 'const BASH_PRE_HOOKS = Object.freeze(["guard-worktree.sh","guard-protected-branch.sh","guard-worktree-write.py"]);' in omp
    assert 'const WRITE_PRE_HOOKS = Object.freeze(["guard-worktree-write.py"]);' in omp
    assert omp.index("for (const scriptName of BASH_PRE_HOOKS)") < omp.index(
        "for (const scriptName of WRITE_PRE_HOOKS)"
    )
    assert "MANAGED_HOOK_VERIFIER" not in omp
    assert "getSessionId" in omp
    assert 'host: "omp"' in omp
    assert len(json.dumps(codex)) < 6_000
    assert len(json.dumps(claude)) < 6_000
    assert len(omp) < 30_000
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

    status = _command(project, "status")
    assert status.returncode in {0, 1}, status.stderr
    assert "managed host file differs" not in status.stderr


def test_node_launcher_dispatches_worktree_commands_to_python_cli(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    installed = _install(
        project,
        env={"AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0"},
    )
    assert installed.returncode == 0, installed.stderr

    result = _command(
        project,
        "worktree",
        "list",
        env={"AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "no worktrees"


def test_parity_checker_validates_external_installed_copy_from_managed_source_worktree(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0",
        }
    )
    installed = _install(project, env=env)
    assert installed.returncode == 0, installed.stderr
    installed_workflow = project / ".agent-flow" / "workflows" / "default.yaml"
    installed_workflow.write_text("id: corrupted\nphases: []\n", encoding="utf-8")
    installed_runtime = (
        project
        / ".agent-flow"
        / "runtime"
        / "python"
        / "agent_flow"
        / "cli.py"
    )
    installed_runtime.write_text("# corrupted runtime\n", encoding="utf-8")
    installed_node_launcher = (
        project
        / ".agent-flow"
        / "runtime"
        / "node"
        / "bin"
        / "agent-flow-kit.mjs"
    )
    installed_node_launcher.write_text("// stale launcher\n", encoding="utf-8")
    installed_skill_selection = (
        project
        / ".agent-flow"
        / "runtime"
        / "node"
        / "lib"
        / "skill-selection.mjs"
    )
    installed_skill_selection.write_text("// stale skill selection\n", encoding="utf-8")
    unexpected_cache = installed_runtime.parent / "__pycache__" / "unexpected.pyc"
    unexpected_cache.parent.mkdir()
    unexpected_cache.write_bytes(b"unexpected bytecode")
    node_runtime_root = project / ".agent-flow" / "runtime" / "node"
    python_runtime_root = project / ".agent-flow" / "runtime" / "python"
    (node_runtime_root / "rogue.mjs").write_text("// rogue\n", encoding="utf-8")
    (python_runtime_root / "unexpected.pyc").write_bytes(b"root bytecode")
    mode_drift = node_runtime_root / "bin" / "agent-flow-install.mjs"
    mode_drift.chmod(0o600)
    (node_runtime_root / "bin").chmod(0o700)
    node_runtime_root.chmod(0o700)
    linked_python_runtime = tmp_path / "linked-python-runtime"
    shutil.copytree(python_runtime_root / "agent_flow", linked_python_runtime)
    shutil.rmtree(python_runtime_root / "agent_flow")
    (python_runtime_root / "agent_flow").symlink_to(
        linked_python_runtime,
        target_is_directory=True,
    )

    result = subprocess.run(
        (_node(), str(KIT_ROOT / "scripts" / "check-installed-runtime-parity.mjs")),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        env=env,
        timeout=300,
    )

    assert result.returncode == 1
    assert ".agent-flow/workflows differs at default.yaml" in result.stderr
    assert "current installed runtime .agent-flow/runtime/python/agent_flow differs at cli.py" in result.stderr
    assert "current installed runtime .agent-flow/runtime/node/bin differs at agent-flow-kit.mjs" in result.stderr
    assert "current installed runtime .agent-flow/runtime/node/lib differs at skill-selection.mjs" in result.stderr
    assert "__pycache__/unexpected.pyc" in result.stderr
    assert "Node unexpected runtime entry rogue.mjs" in result.stderr
    assert "unexpected runtime entry unexpected.pyc" in result.stderr
    assert "runtime/node/bin differs at agent-flow-install.mjs" in result.stderr
    assert "runtime/node/bin root type or mode differs" in result.stderr
    assert "Node runtime root type or mode differs" in result.stderr
    assert "runtime/python/agent_flow root type or mode differs" in result.stderr


def test_runtime_install_uses_canonical_directory_modes_under_restrictive_umask(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0",
    }
    command = (
        "umask 077; exec "
        f"{shlex.quote(_node())} "
        f"{shlex.quote(str(KIT_ROOT / 'bin' / 'agent-flow-kit.mjs'))} install"
    )
    installed = subprocess.run(
        ("/bin/sh", "-c", command),
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    for directory in (
        project / ".agent-flow" / "runtime" / "node",
        project / ".agent-flow" / "runtime" / "node" / "bin",
        project / ".agent-flow" / "runtime" / "python",
        project / ".agent-flow" / "runtime" / "python" / "agent_flow",
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o755

def test_normal_install_preserves_existing_unmanaged_directory_mode(tmp_path: Path) -> None:
    project = tmp_path / "project"
    agents = project / ".Codex" / "agents"
    agents.mkdir(parents=True, mode=0o700)
    (agents / "custom.md").write_text("custom\n", encoding="utf-8")
    agents.chmod(0o700)

    installed = _install(project)

    assert installed.returncode == 0, installed.stderr
    assert stat.S_IMODE(agents.stat().st_mode) == 0o700
    assert (agents / "custom.md").read_text(encoding="utf-8") == "custom\n"


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
    provider = next(
        claim for claim in index["skill_providers"]
        if claim["concrete_id"] == "my-skill"
    )
    assert provider["provider_id"] == "project-local"
    assert provider["source"] == "project://skills/my-skill"
    assert provider["provider_version"] == "1.0.0"
    assert provider["source_hash"] == selected["tree_hash"]
    assert provider["status"] == "verified"
    assert provider["compatibility"] == {
        "registry": 1,
        "profiles": ["*"],
        "hosts": ["*"],
        "source_kinds": ["local", "project", "project-snapshot"],
    }
    assert len(index["provider_registry"]["fingerprint"]) == 64
    assert index["provider_registry"]["quarantined"] == []
    assert "BODY SHOULD NOT BE IN INDEX" not in json.dumps(index)


def test_project_local_skill_precedes_project_and_external_provider_claims(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    _skill(home / ".codex" / "skills" / "shared-name", "external-body")
    _skill(project / "skills" / "shared-name", "project-body")
    _skill(project / ".agent-flow" / "local-skills" / "shared-name", "local-body")

    result = _install(
        project,
        env={"HOME": str(home), "AGENT_FLOW_HOST": "codex"},
    )

    assert result.returncode == 0, result.stderr
    index = json.loads(
        (project / ".agent-flow" / "skills" / "index.json").read_text(
            encoding="utf-8"
        )
    )
    selected = next(skill for skill in index["skills"] if skill["name"] == "shared-name")
    assert selected["source"] == "local"
    assert "local-body" in (project / selected["path"]).read_text(encoding="utf-8")
    claim = next(
        provider
        for provider in index["skill_providers"]
        if provider["concrete_id"] == "shared-name"
    )
    assert claim["provider_id"] == "project-local"
    assert claim["source"] == "project://.agent-flow/local-skills/shared-name"
    assert claim["source_hash"] == selected["tree_hash"]


def test_project_skill_cannot_spoof_provider_ownership(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _skill_with_metadata(
        project / "skills" / "spoofed-skill",
        "provider: android-official\n",
        "UNMANAGED PROJECT CONTENT\n",
    )
    document = project / "skills" / "spoofed-skill" / "SKILL.md"
    before = document.read_text(encoding="utf-8")

    result = _install(project)

    assert result.returncode != 0
    assert "provider_spoofing" in result.stderr
    assert document.read_text(encoding="utf-8") == before


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
    assert len(indexed) <= 30
    matt_skill_closure = {
        "code-review",
        "codebase-design",
        "domain-modeling",
        "grill-with-docs",
        "grilling",
        "tdd",
        "to-prd",
    }
    # generic profile은 공통 workflow/architecture closure만 노출한다.
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
    # generic profile은 공통 host skill만 host별로 link한다.
    assert {link["name"] for link in index["links"]} == host_skills
    codex_roots_are_distinct = not os.path.samefile(
        project / ".Codex" / "skills",
        project / ".codex" / "skills",
    )
    assert len(index["links"]) == (12 if codex_roots_are_distinct else 9)
    if codex_roots_are_distinct:
        assert (
            project / ".codex" / "skills" / "agent-flow" / "SKILL.md"
        ).is_file()
    assert (project / ".agent-flow" / "skills" / "domain-modeling" / "SKILL.md").exists()
    assert (project / ".agent-flow" / "skills" / "full-feature-workflow" / "SKILL.md").exists()
    for host_dir in (".Codex", ".claude"):
        for skill in matt_skill_closure:
            assert not (project / host_dir / "skills" / skill).exists()
    assert not (project / ".Codex" / "skills" / "full-feature-workflow").exists()


def test_clean_architecture_skills_install_core_and_platform_dependency_graph(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = _install(
        project,
        "--profile",
        "android",
        "--profile",
        "ios",
        "--profile",
        "nextjs",
        "--profile",
        "react-native",
        "--profile",
        "python",
    )

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
    compatibility = {
        record["canonical"]: record for record in index["compatibility"]["skills"]
    }
    assert compatibility["clean-architecture-core"]["capabilities"] == [
        "architecture.clean.boundary"
    ]
    assert compatibility["code-generation-discipline"]["capabilities"] == [
        "implementation.code-generation"
    ]

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


def test_renamed_skill_reference_installs_canonical_skill(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = _install(project, "--skills", "code-generation")

    assert result.returncode == 0, result.stderr
    index = json.loads(
        (project / ".agent-flow" / "skills" / "index.json").read_text(
            encoding="utf-8"
        )
    )
    names = {skill["name"] for skill in index["skills"]}
    assert "code-generation-discipline" in names
    assert "code-generation" not in names


def test_replaced_skill_reference_selects_active_concrete_replacement(
    tmp_path: Path,
) -> None:
    kit = tmp_path / "kit"
    _skill(kit / "skills" / "legacy-skill", "legacy")
    _skill(kit / "skills" / "active-skill", "active")
    script = """
import { canonicalizeInstallSelectionCompatibility } from './lib/skill-selection.mjs';
const kitRoot = process.argv[1];
const result = canonicalizeInstallSelectionCompatibility(
  {
    explicitSkills: ['legacy-skill'],
    skillNames: ['legacy-skill'],
  },
  {
    version: 1,
    skills: [
      {
        canonical: 'legacy-skill',
        status: 'deprecated',
        replaced_by: ['active-skill'],
      },
      {
        canonical: 'active-skill',
        status: 'active',
      },
    ],
  },
  kitRoot,
);
process.stdout.write(JSON.stringify({
  explicitSkills: result.explicitSkills,
  skillNames: [...result.skillNames],
}));
"""
    result = subprocess.run(
        (_node(), "--input-type=module", "-e", script, str(kit)),
        cwd=KIT_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "explicitSkills": ["active-skill"],
        "skillNames": ["active-skill"],
    }


def test_filtered_install_rejects_alias_shadowing_unselected_concrete_skill(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    shadow = project / "skills" / "code-generation"
    shadow.mkdir(parents=True)
    (shadow / "SKILL.md").write_text(
        "---\nname: code-generation\n---\n",
        encoding="utf-8",
    )

    result = _install(project, "--skills", "code-generation-discipline")

    assert result.returncode != 0
    assert "compatibility reference shadows concrete skill: code-generation" in result.stderr


def test_explicit_external_concrete_skill_cannot_be_claimed_by_alias(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    _skill(home / ".codex" / "skills" / "code-generation", "external concrete")

    result = _install(
        project,
        "--skills",
        "code-generation",
        env={
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "AGENT_FLOW_HOST": "codex",
        },
    )

    assert result.returncode != 0
    assert "compatibility reference shadows concrete skill: code-generation" in result.stderr


@pytest.mark.parametrize(
    ("state", "setup"),
    [
        ("missing", "missing"),
        ("symlink", "symlink"),
        ("dangling_symlink", "dangling"),
        ("non_regular", "directory"),
        ("non_regular", "fifo"),
    ],
)
def test_project_skill_catalog_document_failures_are_structured(
    tmp_path: Path,
    state: str,
    setup: str,
) -> None:
    project = tmp_path / "project"
    skill_root = project / "skills" / "broken"
    skill_root.mkdir(parents=True)
    document = skill_root / "SKILL.md"
    if setup == "symlink":
        target = tmp_path / "target.md"
        target.write_text("---\nname: broken\n---\n", encoding="utf-8")
        document.symlink_to(target)
    elif setup == "dangling":
        document.symlink_to(tmp_path / "missing-target.md")
    elif setup == "directory":
        document.mkdir()
    elif setup == "fifo":
        os.mkfifo(document)

    result = _install(project)

    assert result.returncode != 0
    assert "skill_resolution_error" in result.stderr
    assert f'"state":"{state}"' in result.stderr
    assert "ENOENT" not in result.stderr


def test_android_profile_installs_android_skills_and_common_dependencies_only(tmp_path: Path) -> None:
    project = tmp_path / "android-project"
    project.mkdir()
    (project / "settings.gradle.kts").write_text("pluginManagement {}\n", encoding="utf-8")

    result = _install(project, "--profile", "android")

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
    assert "react-native-clean-architecture" not in names
    assert "ios-clean-architecture" not in names
    assert not (project / ".agent-flow" / "skills" / "react-native-clean-architecture").exists()
    assert "android-debugging" in names
    assert "android-module-creator" in names
    assert (project / ".agent-flow" / "skills" / "android-debugging" / "SKILL.md").is_file()
    assert (project / ".agent-flow" / "skills" / "android-module-creator" / "SKILL.md").is_file()
    # P1: camerax was pulled from the android_skills catalog (no trusted pinned hash), so it
    # is not a conditional-routing catalog member.
    conditional = index["selection"]["conditional_skills"]["android"]["implementation"]
    assert "camerax" not in conditional
    # P2: android-debugging/android-module-creator are bundled skills.install members (not catalog
    # members) whose profile-routing.json routes now activate them at runtime.
    assert "android-debugging" not in conditional
    debug_plan = resolve_runtime_skill_plan(
        index,
        phase_id="implement",
        changed_files=["app/src/debug/CrashReporter.kt"],
        task_scope="android crash",
    )
    assert "android-debugging" in {skill["name"] for skill in debug_plan["skills"]}
    assert "compose-state-authoring" not in names
    assert not (project / ".agent-flow" / "skills" / "compose-state-authoring").exists()
    assert not (project / ".agent-flow" / "skills" / "kotlin-flow-state-event-modeling").exists()


def test_android_official_provider_rejects_unpinned_host_snapshot(
    tmp_path: Path,
) -> None:
    project = tmp_path / "android-project"
    home = tmp_path / "home"
    project.mkdir()
    (project / "settings.gradle.kts").write_text("pluginManagement {}\n", encoding="utf-8")
    _skill(home / ".codex" / "skills" / "edge-to-edge", "unverified edge skill")

    result = _install(
        project,
        "--profile",
        "android",
        "--skills",
        "edge-to-edge",
        env={"HOME": str(home), "AGENT_FLOW_HOST": "codex"},
    )

    assert result.returncode != 0
    assert "provider_source_hash_mismatch" in result.stderr


def test_android_install_without_upstream_lock_authenticates(tmp_path: Path) -> None:
    from agent_flow.core.skill_plan import authenticated_installed_skill_index

    project = tmp_path / "android-project"
    project.mkdir()
    (project / "settings.gradle.kts").write_text("pluginManagement {}\n", encoding="utf-8")

    result = _install(project, "--profile", "android")
    assert result.returncode == 0, result.stderr

    lock_path = project / ".agent-flow" / "skills" / "upstream-lock.json"
    assert not lock_path.exists()

    index = authenticated_installed_skill_index(project)
    assert index is not None
    assert index["selection"]["profiles"] == ["android"]


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


def test_filtered_reinstall_preserves_replaced_previously_managed_skill(
    tmp_path: Path,
) -> None:
    project = tmp_path / "android-project"
    project.mkdir()
    first = _install(project, "--skills", "react-native-clean-architecture")
    assert first.returncode == 0, first.stderr
    replacement = (
        project
        / ".agent-flow"
        / "skills"
        / "react-native-clean-architecture"
        / "SKILL.md"
    )
    replacement.write_text(
        "---\n"
        "name: react-native-clean-architecture\n"
        "description: User replacement.\n"
        "---\n"
        "user-owned\n",
        encoding="utf-8",
    )

    second = _install(project, "--profile", "android")

    assert second.returncode == 0, second.stderr
    assert "user-owned" in replacement.read_text(encoding="utf-8")
    index = json.loads(
        (project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8")
    )
    assert "react-native-clean-architecture" not in {
        skill["name"] for skill in index["skills"]
    }
    assert any(
        "react-native-clean-architecture: preserved unmanaged skill entry" in warning
        for warning in index["warnings"]
    )


def test_filtered_reinstall_preserves_same_tree_replaced_previously_managed_skill(
    tmp_path: Path,
) -> None:
    project = tmp_path / "android-project"
    project.mkdir()
    first = _install(project, "--skills", "react-native-clean-architecture")
    assert first.returncode == 0, first.stderr
    skill = project / ".agent-flow" / "skills" / "react-native-clean-architecture"
    first_index = json.loads(
        (project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8")
    )
    ownership = first_index["managed_ownership"]
    recorded = ownership["entries"]["react-native-clean-architecture"]
    assert ownership["version"] == 1
    assert recorded["filesystem_identity"]["inode"] == str(skill.lstat().st_ino)
    assert recorded["tree_hash"] == next(
        indexed["tree_hash"]
        for indexed in first_index["skills"]
        if indexed["name"] == "react-native-clean-architecture"
    )
    replacement = project / ".agent-flow" / "skills" / "same-tree-replacement"
    shutil.copytree(skill, replacement)
    shutil.rmtree(skill)
    replacement.rename(skill)
    replacement_inode = str(skill.lstat().st_ino)
    assert replacement_inode != recorded["filesystem_identity"]["inode"]

    second = _install(project, "--profile", "android")

    assert second.returncode == 0, second.stderr
    assert skill.is_dir()
    index = json.loads(
        (project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8")
    )
    assert "react-native-clean-architecture" not in {
        indexed["name"] for indexed in index["skills"]
    }
    assert any(
        "react-native-clean-architecture: preserved unmanaged skill entry" in warning
        for warning in index["warnings"]
    )



def test_legacy_index_without_ownership_preserves_identityless_skill(
    tmp_path: Path,
) -> None:
    project = tmp_path / "android-project"
    project.mkdir()
    assert (
        _install(project, "--skills", "react-native-clean-architecture").returncode
        == 0
    )
    index_path = project / ".agent-flow" / "skills" / "index.json"
    legacy_index = json.loads(index_path.read_text(encoding="utf-8"))
    legacy_index.pop("managed_ownership")
    legacy_bytes = (json.dumps(legacy_index, indent=2) + "\n").encode()
    index_path.write_bytes(legacy_bytes)
    kit_path = project / ".agent-flow" / "kit.json"
    kit = json.loads(kit_path.read_text(encoding="utf-8"))
    kit["skill_index_hash"] = hashlib.sha256(legacy_bytes).hexdigest()
    kit_path.write_text(json.dumps(kit, indent=2) + "\n", encoding="utf-8")
    skill = (
        project
        / ".agent-flow"
        / "skills"
        / "react-native-clean-architecture"
    )
    replacement = project / ".agent-flow" / "skills" / "identityless-replacement"
    shutil.copytree(skill, replacement)
    shutil.rmtree(skill)
    replacement.rename(skill)

    migrated = _install(project, "--profile", "android")

    assert migrated.returncode == 0, migrated.stderr
    migrated_index = json.loads(index_path.read_text(encoding="utf-8"))
    assert migrated_index["managed_ownership"]["version"] == 1
    assert "react-native-clean-architecture" not in {
        indexed["name"] for indexed in migrated_index["skills"]
    }
    assert "react-native-clean-architecture" not in migrated_index["managed_ownership"]["entries"]
    assert (skill / "SKILL.md").is_file()
    assert any(
        "react-native-clean-architecture: preserved unmanaged skill entry"
        in warning
        for warning in migrated_index["warnings"]
    )


def test_reinstall_does_not_adopt_same_tree_replaced_generated_skill(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    skill = project / ".agent-flow" / "skills" / "agent-flow"
    replacement = project / ".agent-flow" / "skills" / "agent-flow-replacement"
    shutil.copytree(skill, replacement)
    shutil.rmtree(skill)
    replacement.rename(skill)
    replacement_inode = skill.lstat().st_ino

    failed = _install(project)

    assert failed.returncode != 0
    assert "unmanaged skill entry conflicts with installed skill: agent-flow" in failed.stderr
    assert skill.lstat().st_ino == replacement_inode


def test_legacy_generated_skill_document_path_is_managed_on_upgrade(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first = _install(project)
    assert first.returncode == 0, first.stderr
    skill = project / ".agent-flow" / "skills" / "agent-flow"
    document = skill / "SKILL.md"
    legacy_document = b"---\nname: agent-flow\n---\nlegacy generated skill\n"
    document.write_bytes(legacy_document)
    index_path = skill.parent / "index.json"
    legacy_index = json.loads(index_path.read_text(encoding="utf-8"))
    legacy_index.pop("managed_ownership")
    indexed = next(
        entry for entry in legacy_index["skills"] if entry["name"] == "agent-flow"
    )
    assert indexed["path"] == ".agent-flow/skills/agent-flow/SKILL.md"
    indexed["hash"] = hashlib.sha256(legacy_document).hexdigest()
    indexed["tree_hash"] = hashlib.sha256(
        b"SKILL.md\0" + legacy_document + b"\0"
    ).hexdigest()
    legacy_bytes = (json.dumps(legacy_index, indent=2) + "\n").encode()
    index_path.write_bytes(legacy_bytes)
    kit_path = project / ".agent-flow" / "kit.json"
    kit = json.loads(kit_path.read_text(encoding="utf-8"))
    kit.pop("skill_index_hash_version", None)
    kit.pop("skill_index_hash", None)
    kit_path.write_text(json.dumps(kit, indent=2) + "\n", encoding="utf-8")

    upgraded = _install(project)

    assert upgraded.returncode == 0, upgraded.stderr
    assert document.read_bytes() != legacy_document


def test_legacy_nested_generated_skill_path_does_not_adopt_root(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first = _install(project)
    assert first.returncode == 0, first.stderr
    skill = project / ".agent-flow" / "skills" / "agent-flow"
    document = skill / "SKILL.md"
    original = document.read_bytes()
    index_path = skill.parent / "index.json"
    legacy_index = json.loads(index_path.read_text(encoding="utf-8"))
    legacy_index.pop("managed_ownership")
    indexed = next(
        entry for entry in legacy_index["skills"] if entry["name"] == "agent-flow"
    )
    indexed["path"] = ".agent-flow/skills/agent-flow/nested"
    index_path.write_text(
        json.dumps(legacy_index, indent=2) + "\n",
        encoding="utf-8",
    )
    kit_path = project / ".agent-flow" / "kit.json"
    kit = json.loads(kit_path.read_text(encoding="utf-8"))
    kit.pop("skill_index_hash_version", None)
    kit.pop("skill_index_hash", None)
    kit_path.write_text(json.dumps(kit, indent=2) + "\n", encoding="utf-8")

    failed = _install(project)

    assert failed.returncode != 0
    assert "unmanaged skill entry conflicts with installed skill: agent-flow" in failed.stderr
    assert document.read_bytes() == original
    rolled_back_index = json.loads(index_path.read_text(encoding="utf-8"))
    assert "managed_ownership" not in rolled_back_index


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


def test_local_project_command_revision_binds_full_tree(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    skill = project / ".agent-flow" / "local-skills" / "agent-flow"
    _skill(skill, "LOCAL")
    extra = skill / "policy.txt"
    extra.write_text("first\n", encoding="utf-8")

    first = _install(project)
    assert first.returncode == 0, first.stderr
    index_path = project / ".agent-flow" / "skills" / "index.json"
    first_index = json.loads(index_path.read_text(encoding="utf-8"))
    first_skill = next(
        entry for entry in first_index["skills"] if entry["name"] == "agent-flow"
    )
    first_provider = next(
        entry
        for entry in first_index["skill_providers"]
        if entry["concrete_id"] == "agent-flow"
    )
    assert first_skill["tree_hash"] == first_provider["source_hash"]

    extra.write_text("second\n", encoding="utf-8")
    second = _install(project)
    assert second.returncode == 0, second.stderr
    second_index = json.loads(index_path.read_text(encoding="utf-8"))
    second_skill = next(
        entry for entry in second_index["skills"] if entry["name"] == "agent-flow"
    )
    second_provider = next(
        entry
        for entry in second_index["skill_providers"]
        if entry["concrete_id"] == "agent-flow"
    )

    assert second_skill["tree_hash"] == second_provider["source_hash"]
    assert second_skill["tree_hash"] != first_skill["tree_hash"]
    assert second_index["revision"] != first_index["revision"]


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


def test_reinstall_preserves_unmanaged_snapshot_matching_indexed_project_skill_name(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = project / "skills" / "source-directory"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: indexed-name\ndescription: Project source.\n---\nproject-owned\n",
        encoding="utf-8",
    )
    assert _install(project).returncode == 0
    unmanaged = project / ".agent-flow" / "skills" / "indexed-name"
    unmanaged.mkdir()
    (unmanaged / "SKILL.md").write_text(
        "---\nname: indexed-name\ndescription: User snapshot.\n---\nuser-owned\n",
        encoding="utf-8",
    )

    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert "user-owned" in (unmanaged / "SKILL.md").read_text(encoding="utf-8")
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    assert any("indexed-name: preserved unmanaged skill entry" in warning for warning in index["warnings"])


@pytest.mark.parametrize(
    ("host", "host_skill_root"),
    (
        ("codex", Path(".codex/skills")),
        ("claude", Path(".claude/skills")),
        ("omp", Path(".omp/agent/skills")),
    ),
)
def test_status_refresh_preserves_unmanaged_snapshot_when_external_name_collides_for_each_host(
    tmp_path: Path,
    host: str,
    host_skill_root: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    host_env = {
        "HOME": str(home),
        "AGENT_FLOW_ACTIVE_HOST": host,
        "AGENT_FLOW_HOST": host,
    }
    assert _install(project, env=host_env).returncode == 0
    unmanaged = project / ".agent-flow" / "skills" / "adaptive"
    unmanaged.mkdir()
    (unmanaged / "SKILL.md").write_text(
        "---\nname: adaptive\ndescription: user snapshot\n---\nuser-owned\n",
        encoding="utf-8",
    )
    _skill(home / host_skill_root / "adaptive", "external snapshot")
    env = {
        **host_env,
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "1",
    }

    result = _command(project, "status", env=env)

    assert result.returncode == 0, result.stderr
    assert "user-owned" in (unmanaged / "SKILL.md").read_text(encoding="utf-8")
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    assert "adaptive" not in {skill["name"] for skill in index["skills"]}
    assert any("adaptive: preserved unmanaged skill entry" in warning for warning in index["warnings"])


def test_regular_skill_dependency_cannot_be_skipped_as_automatic_collision(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    env = {
        "HOME": str(home),
        "AGENT_FLOW_ACTIVE_HOST": "codex",
        "AGENT_FLOW_HOST": "codex",
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "1",
    }
    assert _install(project, env=env).returncode == 0
    dependency = project / ".agent-flow" / "skills" / "collision-dependency"
    dependency.mkdir()
    (dependency / "SKILL.md").write_text(
        "---\nname: collision-dependency\ndescription: user snapshot\n---\nuser-owned\n",
        encoding="utf-8",
    )
    _skill(
        home / ".codex" / "skills" / "collision-dependency",
        "authenticated external dependency",
    )
    _skill_with_metadata(
        project / "skills" / "consumer",
        "dependencies: [collision-dependency]\n",
        "regular consumer",
    )

    result = _install(project, "--skills", "consumer", env=env)

    assert result.returncode != 0
    assert "untrusted existing skill snapshot differs: collision-dependency" in result.stderr
    assert "user-owned" in (dependency / "SKILL.md").read_text(encoding="utf-8")


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


def test_explicit_external_skill_root_symlink_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    project.mkdir()
    _skill(outside, "outside")
    host_skills = home / ".codex" / "skills"
    host_skills.mkdir(parents=True)
    (host_skills / "external").symlink_to(outside, target_is_directory=True)

    result = _command(
        project,
        "install",
        "--skills",
        "external",
        env={
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "AGENT_FLOW_HOST": "codex",
        },
    )

    assert result.returncode != 0
    assert "skill source may not use symlink ancestors" in result.stderr
    assert "outside" in (outside / "SKILL.md").read_text(encoding="utf-8")


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


def test_stale_host_skill_replacement_is_preserved_during_swap(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / "skills" / "demo"
    _skill(skill_dir, "CODEX", hosts="[codex]")
    assert _install(project).returncode == 0
    target = project / ".Codex" / "skills" / "demo"
    _skill(skill_dir, "CLAUDE", hosts="[claude]")
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["AGENT_FLOW_TEST_HOLD_BEFORE_HOST_SWAP_MS"] = "1500"
    env["AGENT_FLOW_TEST_HOLD_HOST_TARGET_SUFFIX"] = ".Codex/skills/demo"
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stderr is not None
    assert "agent-flow:test-host-swap-ready" in process.stderr.readline()
    target.unlink()
    target.mkdir()
    marker = target / "external.txt"
    marker.write_text("external\n", encoding="utf-8")
    stdout, stderr = process.communicate(timeout=15)

    assert process.returncode != 0, stdout
    assert "changed outside install transaction" in stderr
    assert marker.read_text(encoding="utf-8") == "external\n"


def test_stale_host_skill_same_target_replacement_is_preserved_during_swap(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / "skills" / "demo"
    _skill(skill_dir, "CODEX", hosts="[codex]")
    assert _install(project).returncode == 0
    target = project / ".Codex" / "skills" / "demo"
    original_target = os.readlink(target)
    original_inode = target.lstat().st_ino
    _skill(skill_dir, "CLAUDE", hosts="[claude]")
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_TEST_HOLD_BEFORE_HOST_SWAP_MS": "1500",
        "AGENT_FLOW_TEST_HOLD_HOST_TARGET_SUFFIX": ".Codex/skills/demo",
    }
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stderr is not None
    assert "agent-flow:test-host-swap-ready" in process.stderr.readline()
    target.unlink()
    target.symlink_to(original_target, target_is_directory=True)
    replacement_inode = target.lstat().st_ino
    stdout, stderr = process.communicate(timeout=30)

    assert replacement_inode != original_inode
    assert process.returncode != 0, stdout
    assert "changed outside install transaction" in stderr
    assert target.is_symlink()
    assert target.lstat().st_ino == replacement_inode
    assert os.readlink(target) == original_target


def test_stale_host_skill_replacement_after_swap_is_not_adopted(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / "skills" / "demo"
    _skill(skill_dir, "CODEX", hosts="[codex]")
    assert _install(project).returncode == 0
    target = project / ".Codex" / "skills" / "demo"
    _skill(skill_dir, "CLAUDE", hosts="[claude]")
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["AGENT_FLOW_TEST_HOLD_AFTER_HOST_SWAP_MS"] = "1500"
    env["AGENT_FLOW_TEST_HOLD_HOST_TARGET_SUFFIX"] = ".Codex/skills/demo"
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stderr is not None
    assert "agent-flow:test-host-swap-complete" in process.stderr.readline()
    assert not target.exists()
    target.mkdir()
    marker = target / "external.txt"
    marker.write_text("external\n", encoding="utf-8")
    stdout, stderr = process.communicate(timeout=15)

    assert process.returncode != 0, stdout
    assert "changed after swap" in stderr
    assert marker.read_text(encoding="utf-8") == "external\n"


def test_stale_broken_host_skill_symlink_removed_when_skill_deleted(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / "skills" / "demo"
    _skill(skill_dir, "CODEX", hosts="[codex]")
    assert _install(project).returncode == 0
    codex_link = project / ".Codex" / "skills" / "demo"
    assert codex_link.exists() or codex_link.is_symlink()

    shutil.rmtree(skill_dir)
    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert not codex_link.is_symlink()


def test_status_repairs_missing_codex_skill_link_before_phase_output(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    skill_dir = project / "skills" / "samantha-architecture-guide"
    _skill(skill_dir, "CODEX", hosts="[codex]")
    env = {"HOME": str(home)}
    assert _install(project, env=env).returncode == 0
    codex_link = project / ".Codex" / "skills" / "samantha-architecture-guide"
    lowercase_codex_link = project / ".codex" / "skills" / "samantha-architecture-guide"
    assert (codex_link / "SKILL.md").is_file()
    assert (lowercase_codex_link / "SKILL.md").is_file()
    removed_link = (
        lowercase_codex_link
        if lowercase_codex_link.lstat().st_ino != codex_link.lstat().st_ino
        else codex_link
    )
    if removed_link.is_symlink():
        removed_link.unlink()
    else:
        shutil.rmtree(removed_link)

    status = _command(project, "status", env=env)

    assert status.returncode == 0, status.stderr
    assert (codex_link / "SKILL.md").is_file()
    assert (lowercase_codex_link / "SKILL.md").is_file()
    assert "FileNotFoundError" not in status.stderr


def test_stale_cleanup_preserves_same_target_symlink_with_replaced_inode(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / "skills" / "demo"
    _skill(skill_dir, "CODEX", hosts="[codex]")
    assert _install(project).returncode == 0
    codex_link = project / ".Codex" / "skills" / "demo"
    if not codex_link.is_symlink():
        pytest.skip("host skill symlinks are unavailable")
    target = os.readlink(codex_link)
    original_inode = codex_link.lstat().st_ino
    codex_link.unlink()
    codex_link.symlink_to(target, target_is_directory=True)
    assert codex_link.lstat().st_ino != original_inode
    shutil.rmtree(skill_dir)

    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert codex_link.is_symlink()
    assert os.readlink(codex_link) == target
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    assert any(link["status"] == "preserved-identity-mismatch" for link in index["links"])


def test_stale_cleanup_preserves_identical_copied_tree_with_replaced_inode(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / "skills" / "demo"
    _skill(skill_dir, "CODEX", hosts="[codex]")
    force_copy = {"AGENT_FLOW_TEST_FORCE_COPY_HOST_SKILLS": "1"}
    assert _install(project, env=force_copy).returncode == 0
    destination = project / ".Codex" / "skills" / "demo"
    original_inode = destination.lstat().st_ino
    shutil.rmtree(destination)
    shutil.copytree(skill_dir, destination)
    assert destination.lstat().st_ino != original_inode
    shutil.rmtree(skill_dir)

    result = _install(project, env=force_copy)

    assert result.returncode == 0, result.stderr
    assert destination.is_dir()
    assert (destination / "SKILL.md").is_file()
    index = json.loads((project / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
    assert any(link["status"] == "preserved-identity-mismatch" for link in index["links"])


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

    shutil.rmtree(skill_dir)
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
    shutil.rmtree(skill_dir)

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
    state_path = _authoritative_current_run_state_path(project)
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


def test_canonical_status_refreshes_catalog_drift_before_python_dispatch(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    active = home / ".codex" / "skills"
    env = {"HOME": str(home), "AGENT_FLOW_HOST": "codex"}
    _skill(active / "alpha", "alpha-v1")
    assert _install(project, env=env).returncode == 0
    index_path = project / ".agent-flow" / "skills" / "index.json"
    before = json.loads(index_path.read_text(encoding="utf-8"))
    _skill(active / "alpha", "alpha-v2")

    status = _command(project, "status", env=env)

    assert status.returncode in {0, 1}
    assert "agent-flow installed" in status.stdout
    after = json.loads(index_path.read_text(encoding="utf-8"))
    assert after["catalog_fingerprint"] != before["catalog_fingerprint"]
    assert after["revision"] != before["revision"]


def test_worktree_catalog_drift_fails_without_refreshing_leader_install(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    worktree = project / ".agent-flow" / "worktrees" / "feat-drift"
    project.mkdir()
    active = home / ".codex" / "skills"
    env = {
        **os.environ,
        "HOME": str(home),
        "AGENT_FLOW_HOST": "codex",
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "1",
    }
    _skill(active / "alpha", "alpha-v1")
    assert _install(project, env=env).returncode == 0
    worktree.mkdir(parents=True)
    index_path = project / ".agent-flow" / "skills" / "index.json"
    before = index_path.read_bytes()
    _skill(active / "alpha", "alpha-v2")

    result = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "status"),
        cwd=worktree,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "catalog drift must be refreshed from the leader checkout" in result.stderr
    # managed worktree defers repair and points at the leader checkout with a side-effect-free
    # command; `status` triggers the boundary catalog refresh without advancing any run (a bare
    # `continue` from the leader could advance an unrelated leader run instead of only refreshing).
    assert "cd " in result.stderr
    assert "agent-flow status" in result.stderr
    assert "agent-flow continue" not in result.stderr
    assert index_path.read_bytes() == before


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


def test_python_active_run_lock_freezes_on_drift_deferring_to_next_run(
    tmp_path: Path,
) -> None:
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
    assert previous["resolved_skill_lock_hash"]

    _skill(source, "alpha-v2")
    assert _install(project, env=env).returncode == 0
    with mock.patch.dict(os.environ, runtime_env, clear=True):
        assert main(["continue", "--root", str(project)]) == 0
    reconciled = json.loads((active.path / "meta.json").read_text(encoding="utf-8"))

    # run-scoped lock stays frozen; catalog drift only invalidates the next run.
    assert reconciled["skill_plan_hash"] == previous["skill_plan_hash"]
    assert "skill_plan_repin_from" not in reconciled
    assert reconciled["resolved_skill_lock_hash"] == previous["resolved_skill_lock_hash"]
    assert (
        reconciled["skill_plan_drift_observed"]["skill_plan_hash"]
        != previous["skill_plan_hash"]
    )


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
    stdout, stderr = first.communicate(timeout=30)

    assert first.returncode == 0, stdout + stderr
    assert second.returncode != 0
    assert "project install lock is held" in second.stderr


def test_stale_install_lock_takeover_is_serialized_before_recovery(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    lock_path = project / ".agent-flow" / "install.lock"
    lock_path.mkdir()
    stale_token = "1" * 48
    (lock_path / "owner.json").write_text(
        json.dumps(
            {
                "version": 1,
                "root": str(project.resolve()),
                "pid": 2_147_483_647,
                "token": stale_token,
            }
        ),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_TEST_HOLD_AFTER_STALE_LOCK_AUTH_MS": "1200",
    }
    first = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert first.stderr is not None
    assert "agent-flow:test-stale-lock-authenticated" in first.stderr.readline()
    second = _command(project, "install", env={"HOME": str(tmp_path / "home")})
    stdout, stderr = first.communicate(timeout=30)

    assert first.returncode == 0, stdout + stderr
    assert second.returncode != 0
    assert "project install lock is held" in second.stderr
    assert not lock_path.exists()

def test_stale_install_lock_reclaim_preserves_replacement_lock(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    lock_path = project / ".agent-flow" / "install.lock"
    lock_path.mkdir()
    (lock_path / "owner.json").write_text(
        json.dumps(
            {
                "version": 1,
                "root": str(project.resolve()),
                "pid": 2_147_483_647,
                "token": "1" * 48,
            }
        ),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_TEST_HOLD_AFTER_STALE_LOCK_AUTH_MS": "1200",
    }
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stderr is not None
    assert "agent-flow:test-stale-lock-authenticated" in process.stderr.readline()
    lock_path.rename(project / ".agent-flow" / "authenticated-stale-lock")
    replacement_token = "2" * 48
    lock_path.mkdir()
    (lock_path / "owner.json").write_text(
        json.dumps(
            {
                "version": 1,
                "root": str(project.resolve()),
                "pid": os.getpid(),
                "token": replacement_token,
            }
        ),
        encoding="utf-8",
    )
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, stdout
    assert "lock changed during stale recovery" in stderr
    replacement = json.loads((lock_path / "owner.json").read_text(encoding="utf-8"))
    assert replacement["token"] == replacement_token



def test_install_lock_release_never_deletes_replaced_directory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_TEST_HOLD_BEFORE_INSTALL_LOCK_RELEASE_MS": "1200",
    }
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stderr is not None
    assert "agent-flow:test-install-lock-release-ready" in process.stderr.readline()
    lock_path = project / ".agent-flow" / "install.lock"
    original = project / ".agent-flow" / "original-install-lock"
    lock_path.rename(original)
    lock_path.mkdir()
    marker = lock_path / "user-owned"
    marker.write_text("keep\n", encoding="utf-8")
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, stdout
    assert "lock changed during release" in stderr
    assert marker.read_text(encoding="utf-8") == "keep\n"


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


def test_transaction_root_swap_cannot_redirect_journal_write(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    victim = project / ".git"
    victim.mkdir()
    victim_journal = victim / "journal.json"
    victim_journal.write_text("preserve\n", encoding="utf-8")
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0",
        "AGENT_FLOW_TEST_HOLD_BEFORE_INSTALL_JOURNAL_WRITE_MS": "1500",
    }
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stderr is not None
    assert "agent-flow:test-install-journal-write-ready" in process.stderr.readline()
    transaction = project / ".agent-flow" / "install-transaction"
    detached = project / ".agent-flow" / "detached-install-transaction"
    transaction.rename(detached)
    transaction.symlink_to(victim, target_is_directory=True)
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, stdout
    assert "pinned install journal directory" in stderr
    assert victim_journal.read_text(encoding="utf-8") == "preserve\n"
    assert detached.is_dir()


def test_transaction_cleanup_preserves_replaced_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_TEST_HOLD_BEFORE_TRANSACTION_CLEANUP_MS": "1500",
    }
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stderr is not None
    assert "agent-flow:test-transaction-cleanup-ready" in process.stderr.readline()
    transaction = project / ".agent-flow" / "install-transaction"
    transaction.rename(project / ".agent-flow" / "authenticated-install-transaction")
    transaction.mkdir()
    marker = transaction / "user-owned"
    marker.write_text("keep\n", encoding="utf-8")
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, stdout
    assert "install transaction root changed during cleanup" in stderr
    assert marker.read_text(encoding="utf-8") == "keep\n"


def test_live_index_leaf_swap_cannot_truncate_unmanaged_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    victim = project / ".git" / "config"
    victim.parent.mkdir()
    victim.write_text("preserve\n", encoding="utf-8")
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0",
        "AGENT_FLOW_TEST_HOLD_BEFORE_SKILL_INDEX_WRITE_MS": "1500",
    }
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stderr is not None
    assert "agent-flow:test-skill-index-write-ready" in process.stderr.readline()
    index_path = project / ".agent-flow" / "skills" / "index.json"
    index_path.symlink_to(victim)
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, stdout
    assert "pinned skill index target changed" in stderr
    assert victim.read_text(encoding="utf-8") == "preserve\n"


@pytest.mark.parametrize(
    ("mutation_target", "expected_error"),
    (
        ("source", "unmanaged skill source changed while copying: user-skill"),
        ("staging", "unmanaged skill staging changed while copying: user-skill"),
        ("destination", "unmanaged skill destination changed while copying: user-skill"),
    ),
)
def test_unmanaged_skill_copy_uses_source_staging_and_destination_cas(
    tmp_path: Path,
    mutation_target: str,
    expected_error: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    _skill(project / ".agent-flow" / "skills" / "user-skill", "original")
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_TEST_HOLD_AFTER_UNMANAGED_SKILL_COPY_MS": "1500",
    }
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stderr is not None
    assert "agent-flow:test-unmanaged-skill-copy-ready:user-skill" in process.stderr.readline()
    transaction = project / ".agent-flow" / "install-transaction"
    target = {
        "source": transaction / "skills-backup" / "user-skill",
        "staging": transaction / "unmanaged-staging" / "user-skill",
        "destination": project / ".agent-flow" / "skills" / "user-skill",
    }[mutation_target]
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text("external mutation\n", encoding="utf-8")
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, stdout
    assert expected_error in stderr
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "external mutation\n"
    assert transaction.exists()
    retry = _install(project)
    assert retry.returncode != 0
    assert expected_error in retry.stderr
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "external mutation\n"
    assert transaction.exists()


def test_pre_seal_verification_rejects_concurrent_live_skill_addition(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_TEST_HOLD_AFTER_SKILL_LIVE_VERIFY_MS": "1500",
        "AGENT_FLOW_TEST_FAIL_AFTER_SKILL_INDEX": "1",
    }
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stderr is not None
    assert "agent-flow:test-skill-live-verified" in process.stderr.readline()
    concurrent = project / ".agent-flow" / "skills" / "concurrent"
    _skill(concurrent, "external")
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, stdout
    assert "skill transaction live tree changed outside transaction" in stderr
    assert "external" in (concurrent / "SKILL.md").read_text(encoding="utf-8")


def test_recovery_rejects_pre_seal_concurrent_live_skill_addition(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_TEST_HOLD_AFTER_SKILL_LIVE_VERIFY_MS": "5000",
    }
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stderr is not None
    assert "agent-flow:test-skill-live-verified" in process.stderr.readline()
    concurrent = project / ".agent-flow" / "skills" / "concurrent"
    _skill(concurrent, "external")
    process.kill()
    process.communicate(timeout=30)

    recovered = _install(project)

    assert recovered.returncode != 0
    assert "skill transaction live tree changed outside transaction" in recovered.stderr
    assert "external" in (concurrent / "SKILL.md").read_text(encoding="utf-8")
    assert (project / ".agent-flow" / "install-transaction").exists()


def test_late_rollback_never_deletes_concurrent_live_skill_addition(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_TEST_HOLD_AFTER_SKILL_LIVE_SEAL_MS": "1500",
        "AGENT_FLOW_TEST_FAIL_AFTER_SKILL_INDEX": "1",
    }
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stderr is not None
    assert "agent-flow:test-skill-live-sealed" in process.stderr.readline()
    concurrent = project / ".agent-flow" / "skills" / "concurrent"
    _skill(concurrent, "external")
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, stdout
    assert "skill transaction live tree changed outside transaction" in stderr
    assert "external" in (concurrent / "SKILL.md").read_text(encoding="utf-8")
    assert (project / ".agent-flow" / "install-transaction").exists()


def test_initial_install_rollback_never_deletes_concurrent_skill_addition(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_TEST_HOLD_AFTER_SKILL_LIVE_SEAL_MS": "1500",
        "AGENT_FLOW_TEST_FAIL_AFTER_SKILL_INDEX": "1",
    }
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stderr is not None
    assert "agent-flow:test-skill-live-sealed" in process.stderr.readline()
    concurrent = project / ".agent-flow" / "skills" / "concurrent"
    _skill(concurrent, "external")
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, stdout
    assert "skill transaction live tree changed outside transaction" in stderr
    assert "external" in (concurrent / "SKILL.md").read_text(encoding="utf-8")
    assert (project / ".agent-flow" / "install-transaction").exists()


def test_preseal_snapshot_rejects_concurrent_unmanaged_skill_without_deleting_it(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_TEST_HOLD_BEFORE_SKILL_LIVE_SNAPSHOT_MS": "1500",
    }
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stderr is not None
    assert "agent-flow:test-skill-live-snapshot-ready" in process.stderr.readline()
    concurrent = project / ".agent-flow" / "skills" / "concurrent"
    _skill(concurrent, "external")
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, stdout
    assert "skill transaction live tree changed outside transaction: concurrent" in stderr
    assert "external" in (concurrent / "SKILL.md").read_text(encoding="utf-8")
    assert (project / ".agent-flow" / "install-transaction").exists()


def test_recovery_accepts_authenticated_live_created_journal(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    crashed = _install(
        project,
        env={"AGENT_FLOW_TEST_CRASH_AFTER_SKILL_LIVE_CREATE": "1"},
    )

    assert crashed.returncode == 85
    journal = json.loads(
        (
            project
            / ".agent-flow"
            / "install-transaction"
            / "journal.json"
        ).read_text(encoding="utf-8"),
    )
    assert journal["stage"] == "live-created"
    assert set(journal["planned_live_states"]) == {
        ".agent-flow-transaction-owner",
        "index.json",
    }

    recovered = _install(project)

    assert recovered.returncode == 0, recovered.stderr
    assert not (project / ".agent-flow" / "install-transaction").exists()


def test_unsealed_recovery_rejects_replaced_planned_skill_entry(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    source = home / ".codex" / "skills" / "external"
    _skill(source, "trusted")
    env = {
        **os.environ,
        "HOME": str(home),
        "AGENT_FLOW_HOST": "codex",
        "AGENT_FLOW_TEST_HOLD_BEFORE_SKILL_LIVE_SNAPSHOT_MS": "10000",
    }
    process = subprocess.Popen(
        (
            _node(),
            str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"),
            "install",
            "--skills",
            "external",
        ),
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stderr is not None
    assert "agent-flow:test-skill-live-snapshot-ready" in process.stderr.readline()
    journal = json.loads(
        (
            project
            / ".agent-flow"
            / "install-transaction"
            / "journal.json"
        ).read_text(encoding="utf-8"),
    )
    planned_entries = set(journal["planned_live_entries"])
    assert "external" in planned_entries
    assert "react-native-clean-architecture" not in planned_entries
    destination = project / ".agent-flow" / "skills" / "external"
    destination.rename(tmp_path / "trusted-external")
    _skill(destination, "attacker")
    os.killpg(process.pid, signal.SIGKILL)
    process.communicate(timeout=10)

    recovered = _install(
        project,
        "--skills",
        "external",
        env={"HOME": str(home), "AGENT_FLOW_HOST": "codex"},
    )

    assert recovered.returncode != 0
    assert "planned skill live entry changed outside transaction: external" in recovered.stderr
    assert "attacker" in (destination / "SKILL.md").read_text(encoding="utf-8")
    assert (project / ".agent-flow" / "install-transaction").exists()


def test_preseal_materialization_failure_restores_previous_skills(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    source = home / ".codex" / "skills" / "external"
    env = {"HOME": str(home), "AGENT_FLOW_HOST": "codex"}
    _skill(source, "v1")
    assert _install(project, "--skills", "external", env=env).returncode == 0
    destination = project / ".agent-flow" / "skills" / "external" / "SKILL.md"
    failed = _install(
        project,
        "--skills",
        "external",
        env={**env, "AGENT_FLOW_TEST_FAIL_AFTER_SKILL_MATERIALIZATION": "1"},
    )

    assert failed.returncode != 0
    assert "injected failure after skill materialization" in failed.stderr
    assert "v1" in destination.read_text(encoding="utf-8")
    assert not (project / ".agent-flow" / "install-transaction").exists()


def test_commit_final_verification_rejects_concurrent_live_skill_addition(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_TEST_HOLD_BEFORE_COMMIT_FINAL_VERIFY_MS": "1500",
    }
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stderr is not None
    assert "agent-flow:test-commit-final-verify-ready" in process.stderr.readline()
    concurrent = project / ".agent-flow" / "skills" / "concurrent"
    _skill(concurrent, "external")
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, stdout
    assert "skill transaction live tree changed outside transaction" in stderr
    assert "external" in (concurrent / "SKILL.md").read_text(encoding="utf-8")



def test_final_delete_cas_preserves_concurrent_live_skill_addition(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_TEST_HOLD_BEFORE_SKILL_LIVE_DELETE_MS": "1500",
        "AGENT_FLOW_TEST_FAIL_AFTER_SKILL_INDEX": "1",
    }
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stderr is not None
    assert "agent-flow:test-skill-live-delete-ready" in process.stderr.readline()
    concurrent = project / ".agent-flow" / "skills" / "concurrent"
    _skill(concurrent, "external")
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, stdout
    assert "skill transaction live tree changed during delete" in stderr
    assert "external" in (concurrent / "SKILL.md").read_text(encoding="utf-8")


def test_recovery_delete_cas_preserves_concurrent_live_skill_addition(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    crashed = _install(
        project,
        env={"AGENT_FLOW_TEST_CRASH_AFTER_SKILL_INDEX": "1"},
    )
    assert crashed.returncode == 87
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_TEST_HOLD_BEFORE_SKILL_LIVE_DELETE_MS": "1500",
    }
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stderr is not None
    assert "agent-flow:test-skill-live-delete-ready" in process.stderr.readline()
    concurrent = project / ".agent-flow" / "skills" / "concurrent"
    _skill(concurrent, "external")
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, stdout
    assert "skill transaction live tree changed during delete" in stderr
    assert "external" in (concurrent / "SKILL.md").read_text(encoding="utf-8")


def test_recovery_refuses_changed_sealed_live_skill_tree(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    _skill(project / ".agent-flow" / "skills" / "user-skill", "original")
    crashed = _command(
        project,
        "install",
        env={
            "HOME": str(tmp_path / "home"),
            "AGENT_FLOW_TEST_CRASH_AFTER_SKILL_INDEX": "1",
        },
    )
    assert crashed.returncode == 87
    transaction = project / ".agent-flow" / "install-transaction"
    concurrent = project / ".agent-flow" / "skills" / "concurrent"
    _skill(concurrent, "external")

    recovered = _install(project)

    assert recovered.returncode != 0
    assert "skill transaction live tree changed outside transaction" in recovered.stderr
    assert "external" in (concurrent / "SKILL.md").read_text(encoding="utf-8")
    assert transaction.exists()


def test_recovery_accepts_unchanged_sealed_live_skill_tree(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    user_skill = project / ".agent-flow" / "skills" / "user-skill"
    _skill(user_skill, "original")
    crashed = _command(
        project,
        "install",
        env={
            "HOME": str(tmp_path / "home"),
            "AGENT_FLOW_TEST_CRASH_AFTER_SKILL_INDEX": "1",
        },
    )
    assert crashed.returncode == 87

    recovered = _install(project)

    assert recovered.returncode == 0, recovered.stderr
    assert "original" in (user_skill / "SKILL.md").read_text(encoding="utf-8")
    assert not (project / ".agent-flow" / "install-transaction").exists()


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
    assert journal["backup_state"]["kind"] == "directory"
    assert not index_path.exists()
    assert (transaction / "skills-backup" / "index.json").read_bytes() == authenticated

    recovered = _install(project)

    assert recovered.returncode == 0, recovered.stderr
    assert index_path.exists()
    assert not transaction.exists()

def test_moving_recovery_rejects_changed_backup_and_concurrent_live_tree(
    tmp_path: Path,
) -> None:
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
    backup = transaction / "skills-backup"
    (backup / "index.json").unlink()
    index_path.parent.mkdir()
    index_path.write_bytes(authenticated)

    recovered = _install(project)

    assert recovered.returncode != 0
    assert "interrupted skill move has ambiguous live and backup state" in recovered.stderr
    assert transaction.is_dir()
    assert backup.is_dir()
    assert index_path.read_bytes() == authenticated



@pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"),
    reason="secure descriptor flags are required",
)
def test_recovery_rejects_fifo_backup_index_without_blocking(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0

    crashed = _install(
        project,
        env={"AGENT_FLOW_TEST_CRASH_AFTER_SKILLS_MOVE": "1"},
    )
    assert crashed.returncode == 86
    backup_index = (
        project
        / ".agent-flow"
        / "install-transaction"
        / "skills-backup"
        / "index.json"
    )
    backup_index.unlink()
    os.mkfifo(backup_index)

    recovered = _install(project, timeout=10)

    assert recovered.returncode != 0
    assert "unsafe backup skill index file" in recovered.stderr


@pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"),
    reason="secure descriptor flags are required",
)
def test_recovery_rejects_fifo_transaction_marker_without_blocking(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0

    crashed = _install(
        project,
        env={"AGENT_FLOW_TEST_CRASH_AFTER_SKILL_INDEX": "1"},
    )
    assert crashed.returncode == 87
    marker = (
        project
        / ".agent-flow"
        / "skills"
        / ".agent-flow-transaction-owner"
    )
    marker.unlink()
    os.mkfifo(marker)

    recovered = _install(project, timeout=10)

    assert recovered.returncode != 0
    assert "unsafe skill transaction marker file" in recovered.stderr


def test_recovery_rolls_back_interrupted_upgrade_after_managed_install_seal(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    _skill(project / "skills" / "upgrade-skill", "upgrade")
    test_home = tmp_path / "test-home"
    process_env = {
        **os.environ,
        "HOME": str(test_home),
        "CODEX_HOME": str(test_home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(test_home / ".claude"),
        "PI_CODING_AGENT_DIR": str(test_home / ".omp" / "agent"),
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "1",
        "AGENT_FLOW_TEST_HOLD_AFTER_MANAGED_INSTALL_SEAL_MS": "10000",
    }
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=process_env,
    )
    assert process.stderr is not None
    marker = process.stderr.readline()
    assert "agent-flow:test-managed-install-sealed" in marker
    os.killpg(process.pid, signal.SIGKILL)
    process.communicate(timeout=10)
    assert process.returncode != 0

    recovered = _install(project, timeout=30)

    assert recovered.returncode == 0, recovered.stderr
    transaction = project / ".agent-flow" / "install-transaction"
    assert not transaction.exists()
    index_path = project / ".agent-flow" / "skills" / "index.json"
    index_bytes = index_path.read_bytes()
    index = json.loads(index_bytes)
    kit = json.loads(
        (project / ".agent-flow" / "kit.json").read_text(encoding="utf-8")
    )
    assert any(skill["name"] == "upgrade-skill" for skill in index["skills"])
    assert hashlib.sha256(index_bytes).hexdigest() == kit["skill_index_hash"]


def test_recovery_survives_crash_between_managed_callback_and_commitment(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    crashed = _install(
        project,
        env={"AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_CALLBACK": "1"},
    )

    assert crashed.returncode == 89
    transaction = project / ".agent-flow" / "install-transaction"
    journal = json.loads((transaction / "journal.json").read_text(encoding="utf-8"))
    assert any(operation["pending"] is not None for operation in journal["managed_mutations"])

    recovered = _install(project)

    assert recovered.returncode == 0, recovered.stderr
    assert not transaction.exists()
    assert (project / ".agent-flow" / "kit.json").is_file()


def test_recovery_survives_crash_during_managed_path_swap(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    crashed = _install(
        project,
        env={"AGENT_FLOW_TEST_CRASH_DURING_MANAGED_SWAP": "1"},
    )

    assert crashed.returncode == 90
    recovered = _install(project)
    assert recovered.returncode == 0, recovered.stderr
    assert not (project / ".agent-flow" / "install-transaction").exists()


def test_recovery_survives_initial_swap_before_completion_journal(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    crashed = _install(
        project,
        env={"AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_SWAP_BEFORE_COMMITMENT": "1"},
    )

    assert crashed.returncode == 91
    transaction = project / ".agent-flow" / "install-transaction"
    journal = json.loads((transaction / "journal.json").read_text(encoding="utf-8"))
    active = next(operation for operation in journal["managed_mutations"] if operation["pending"] is not None)
    assert active["before"]["kind"] == "absent"
    assert "completed" not in active["pending"]
    assert (project / active["path"]).exists()

    recovered = _install(project)

    assert recovered.returncode == 0, recovered.stderr
    assert not transaction.exists()


def test_recovery_preserves_external_change_after_managed_swap_crash(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    crashed = _install(
        project,
        env={"AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_CALLBACK": "1"},
    )
    assert crashed.returncode == 89
    transaction = project / ".agent-flow" / "install-transaction"
    journal = json.loads((transaction / "journal.json").read_text(encoding="utf-8"))
    active = next(operation for operation in journal["managed_mutations"] if operation["pending"] is not None)
    target = project / active["path"]
    shutil.rmtree(target)
    target.mkdir()
    marker = target / "external.txt"
    marker.write_text("external\n", encoding="utf-8")

    retry = _install(project)

    assert retry.returncode != 0
    assert "changed outside transaction" in retry.stderr
    assert marker.read_text(encoding="utf-8") == "external\n"


@pytest.mark.parametrize("tamper", ("path", "lock", "symlink"))
def test_recovery_rejects_unauthenticated_journal_authority(
    tmp_path: Path,
    tamper: str,
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("outside\n", encoding="utf-8")
    crashed = _install(
        project,
        env={"AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_CALLBACK": "1"},
    )
    assert crashed.returncode == 89
    transaction = project / ".agent-flow" / "install-transaction"
    journal_path = transaction / "journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    active = next(operation for operation in journal["managed_mutations"] if operation["pending"] is not None)
    if tamper == "path":
        active["path"] = "../outside"
        journal_path.write_text(json.dumps(journal, indent=2) + "\n", encoding="utf-8")
    elif tamper == "lock":
        journal["lock_token"] = "forged"
        journal_path.write_text(json.dumps(journal, indent=2) + "\n", encoding="utf-8")
    else:
        managed_target = project / active["path"]
        shutil.rmtree(managed_target)
        managed_target.symlink_to(outside, target_is_directory=True)

    retry = _install(project)

    assert retry.returncode != 0
    assert "invalid interrupted" in retry.stderr or "contains a symlink" in retry.stderr
    assert marker.read_text(encoding="utf-8") == "outside\n"


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


@pytest.mark.parametrize("legacy_version", (4, 5))
def test_recovery_rejects_legacy_live_tree_without_identity_commitment(
    tmp_path: Path,
    legacy_version: int,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    crashed = _install(
        project,
        env={"AGENT_FLOW_TEST_CRASH_AFTER_SKILL_INDEX": "1"},
    )
    assert crashed.returncode == 87
    transaction = project / ".agent-flow" / "install-transaction"
    journal_path = transaction / "journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["version"] = legacy_version
    journal.pop("live_state", None)
    journal.pop("pending_live_state", None)
    journal.pop("initial_live_state", None)
    journal.pop("unmanaged_conflict", None)
    journal_path.write_text(json.dumps(journal, indent=2) + "\n", encoding="utf-8")
    concurrent = project / ".agent-flow" / "skills" / "concurrent"
    _skill(concurrent, "external")

    recovered = _install(project)

    assert recovered.returncode != 0
    assert "legacy live tree is unauthenticated" in recovered.stderr
    assert "external" in (concurrent / "SKILL.md").read_text(encoding="utf-8")
    assert transaction.exists()


def test_recovery_rejects_legacy_v3_host_journal_without_filesystem_identity(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    env = {"HOME": str(home), "AGENT_FLOW_HOST": "codex"}
    _skill(home / ".codex" / "skills" / "external", "v1")

    crashed = _command(
        project,
        "install",
        env={**env, "AGENT_FLOW_TEST_CRASH_AFTER_SKILL_INDEX": "1"},
    )
    assert crashed.returncode == 87
    journal_path = project / ".agent-flow" / "install-transaction" / "journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["version"] = 3
    journal.pop("live_state", None)
    journal.pop("unmanaged_conflict", None)
    journal.pop("initial_live_state", None)
    journal.pop("pending_live_state", None)
    for operation in journal["host_mutations"]:
        for state in (
            operation.get("before"),
            operation.get("after"),
            (operation.get("pending") or {}).get("after"),
        ):
            if isinstance(state, dict):
                state.pop("filesystem_identity", None)
    journal_path.write_text(json.dumps(journal, indent=2) + "\n", encoding="utf-8")

    recovered = _install(project, env=env)

    assert recovered.returncode != 0
    assert "legacy host identity is unauthenticated" in recovered.stderr
    assert (project / ".agent-flow" / "install-transaction").exists()


def test_recovery_authenticates_backup_before_host_rollback(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    env = {
        "HOME": str(home),
        "AGENT_FLOW_HOST": "codex",
        "AGENT_FLOW_TEST_FORCE_COPY_HOST_SKILLS": "1",
    }
    source = home / ".codex" / "skills" / "external"
    _skill(source, "v1")
    assert _install(project, env=env).returncode == 0
    destination = project / ".Codex" / "skills" / "external" / "SKILL.md"
    assert "v1" in destination.read_text(encoding="utf-8")
    _skill(source, "v2")
    crashed = _install(
        project,
        env={**env, "AGENT_FLOW_TEST_CRASH_AFTER_SKILL_INDEX": "1"},
    )
    assert crashed.returncode == 87
    assert "v2" in destination.read_text(encoding="utf-8")
    backup_index = (
        project
        / ".agent-flow"
        / "install-transaction"
        / "skills-backup"
        / "index.json"
    )
    backup_index.write_bytes(backup_index.read_bytes() + b" ")

    recovered = _install(project, env=env)

    assert recovered.returncode != 0
    assert "backup is not authenticated" in recovered.stderr
    assert "v2" in destination.read_text(encoding="utf-8")

def test_recovery_authenticates_complete_backup_before_host_rollback(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    env = {
        "HOME": str(home),
        "AGENT_FLOW_HOST": "codex",
        "AGENT_FLOW_TEST_FORCE_COPY_HOST_SKILLS": "1",
    }
    source = home / ".codex" / "skills" / "external"
    _skill(source, "v1")
    assert _install(project, env=env).returncode == 0
    destination = project / ".Codex" / "skills" / "external" / "SKILL.md"
    _skill(source, "v2")
    crashed = _install(
        project,
        env={**env, "AGENT_FLOW_TEST_CRASH_AFTER_SKILL_INDEX": "1"},
    )

    assert crashed.returncode == 87
    backup_skill = (
        project
        / ".agent-flow"
        / "install-transaction"
        / "skills-backup"
        / "external"
        / "SKILL.md"
    )
    backup_skill.write_text("tampered backup\n", encoding="utf-8")

    recovered = _install(project, env=env)

    assert recovered.returncode != 0
    assert "backup is not authenticated" in recovered.stderr
    assert "v2" in destination.read_text(encoding="utf-8")


def test_recovery_rejects_same_content_replaced_unindexed_managed_backup(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first = _install(project)
    assert first.returncode == 0, first.stderr
    agent_flow = project / ".agent-flow"
    index_path = agent_flow / "skills" / "index.json"
    first_index = json.loads(index_path.read_text(encoding="utf-8"))
    source_name, source_ownership = next(
        iter(first_index["managed_ownership"]["entries"].items())
    )
    unindexed = "unindexed-owned"
    source = agent_flow / "skills" / source_name
    destination = agent_flow / "skills" / unindexed
    shutil.copytree(source, destination)
    stat = destination.lstat()
    first_index["managed_ownership"]["entries"][unindexed] = {
        "tree_hash": source_ownership["tree_hash"],
        "filesystem_identity": {
            "device": str(stat.st_dev),
            "inode": str(stat.st_ino),
            "links": str(stat.st_nlink),
            "mode": stat.st_mode & 0o777,
        },
    }
    index_bytes = (json.dumps(first_index, indent=2) + "\n").encode()
    index_path.write_bytes(index_bytes)
    kit_path = agent_flow / "kit.json"
    kit = json.loads(kit_path.read_text(encoding="utf-8"))
    kit["skill_index_hash"] = hashlib.sha256(index_bytes).hexdigest()
    kit_path.write_text(json.dumps(kit, indent=2) + "\n", encoding="utf-8")
    crashed = _install(
        project,
        env={"AGENT_FLOW_TEST_CRASH_AFTER_SKILL_INDEX": "1"},
    )
    assert crashed.returncode == 87, crashed.stderr
    transaction = agent_flow / "install-transaction"
    backup = transaction / "skills-backup" / unindexed
    replacement = backup.with_name(f"{backup.name}-replacement")
    shutil.copytree(backup, replacement)
    shutil.rmtree(backup)
    replacement.rename(backup)

    recovered = _install(project)

    assert recovered.returncode != 0
    assert "backup is not authenticated" in recovered.stderr
    assert transaction.exists()


def test_recovery_retries_after_partial_managed_rollback(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    profile = project / ".agent-flow" / "profiles" / "node.yaml"
    review = project / ".agent-flow" / "templates" / "_shared" / "review" / "types.md"
    profile.write_text(profile.read_text(encoding="utf-8") + "\nlocal\n", encoding="utf-8")
    review.write_text(review.read_text(encoding="utf-8") + "\nlocal\n", encoding="utf-8")
    crashed = _install(
        project,
        "--force-managed",
        env={
            "AGENT_FLOW_TEST_FAIL_AFTER_SKILL_INDEX": "1",
            "AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_ROLLBACK_COUNT": "1",
        },
    )
    assert crashed.returncode == 92
    transaction = project / ".agent-flow" / "install-transaction"
    assert transaction.exists()
    backup_count = len(tuple((transaction / "managed-backups").iterdir()))
    assert backup_count > 1

    recovered = _install(project)

    assert recovered.returncode == 0, recovered.stderr
    assert not transaction.exists()


def test_recovery_retries_after_completed_host_rollback(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    _skill(home / ".codex" / "skills" / "external", "trusted")
    env = {
        "HOME": str(home),
        "AGENT_FLOW_HOST": "codex",
        "AGENT_FLOW_TEST_FORCE_COPY_HOST_SKILLS": "1",
    }
    crashed = _install(
        project,
        "--skills",
        "external",
        env={
            **env,
            "AGENT_FLOW_TEST_FAIL_AFTER_MANAGED_INSTALL": "1",
            "AGENT_FLOW_TEST_CRASH_AFTER_HOST_ROLLBACK": "1",
        },
    )
    assert crashed.returncode == 93
    transaction = project / ".agent-flow" / "install-transaction"
    journal = json.loads((transaction / "journal.json").read_text(encoding="utf-8"))
    assert journal["host_mutations"]
    assert all(operation.get("rolled_back") is True for operation in journal["host_mutations"])

    recovered = _install(project, "--skills", "external", env=env)

    assert recovered.returncode == 0, recovered.stderr
    assert not transaction.exists()


def test_recovery_rejects_same_content_replaced_managed_backup(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    crashed = _install(
        project,
        env={"AGENT_FLOW_TEST_CRASH_AFTER_SKILL_INDEX": "1"},
    )
    assert crashed.returncode == 87
    transaction = project / ".agent-flow" / "install-transaction"
    journal = json.loads((transaction / "journal.json").read_text(encoding="utf-8"))
    operation = next(
        item
        for item in journal["managed_mutations"]
        if item["before"]["kind"] in {"directory", "file"}
    )
    backup = transaction / operation["before"]["backup"]
    replacement = backup.with_name(f"{backup.name}-replacement")
    if backup.is_dir():
        shutil.copytree(backup, replacement)
        shutil.rmtree(backup)
    else:
        shutil.copy2(backup, replacement)
        backup.unlink()
    replacement.rename(backup)

    recovered = _install(project)

    assert recovered.returncode != 0
    assert "managed install backup authentication failed" in recovered.stderr
    assert transaction.exists()


def test_recovery_authenticates_host_backup_before_rollback(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    env = {
        "HOME": str(home),
        "AGENT_FLOW_HOST": "codex",
        "AGENT_FLOW_TEST_FORCE_COPY_HOST_SKILLS": "1",
    }
    source = home / ".codex" / "skills" / "external"
    _skill(source, "v1")
    assert _install(project, env=env).returncode == 0
    destination = project / ".Codex" / "skills" / "external" / "SKILL.md"
    _skill(source, "v2")
    crashed = _install(
        project,
        env={**env, "AGENT_FLOW_TEST_CRASH_AFTER_SKILL_INDEX": "1"},
    )
    assert crashed.returncode == 87
    transaction = project / ".agent-flow" / "install-transaction"
    journal = json.loads((transaction / "journal.json").read_text(encoding="utf-8"))
    operation = next(
        item
        for item in journal["host_mutations"]
        if item["path"].endswith("/skills/external")
    )
    host_backup = transaction / operation["before"]["backup"] / "SKILL.md"
    host_backup.write_text("tampered host backup\n", encoding="utf-8")

    recovered = _install(project, env=env)

    assert recovered.returncode != 0
    assert "host skill backup authentication failed" in recovered.stderr
    assert "v2" in destination.read_text(encoding="utf-8")




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


@pytest.mark.parametrize("relative", ("AGENTS.md", ".agent-flow/workflows/default.yaml"))
def test_managed_file_symlink_never_writes_outside_project(tmp_path: Path, relative: str) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside.md"
    project.mkdir()
    outside.write_text("outside\n", encoding="utf-8")
    target = project / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(outside)

    result = _install(project)

    assert result.returncode != 0
    assert "contains a symlink" in result.stderr
    assert outside.read_text(encoding="utf-8") == "outside\n"


@pytest.mark.parametrize("replacement_kind", ("symlink", "fifo", "authority"))
def test_json_authority_entry_swap_is_rejected_without_fifo_blocking(
    tmp_path: Path,
    replacement_kind: str,
) -> None:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        pytest.skip("secure descriptor flags are required")
    project = tmp_path / "project"
    project.mkdir()
    target = project / ".agent-flow" / "skills" / "compatibility.json"
    outside = tmp_path / "outside.json"
    outside.write_text('{"version": 999}\n', encoding="utf-8")
    env = dict(os.environ)
    test_home = tmp_path / "test-home"
    env["HOME"] = str(test_home)
    env["CODEX_HOME"] = str(test_home / ".codex")
    env["CLAUDE_CONFIG_DIR"] = str(test_home / ".claude")
    env["PI_CODING_AGENT_DIR"] = str(test_home / ".omp" / "agent")
    env["AGENT_FLOW_AUTO_EXTERNAL_SKILLS"] = "1"
    env["AGENT_FLOW_INSTALL_SANDBOXED"] = "1"
    env["AGENT_FLOW_TEST_HOLD_JSON_AUTH_PATH"] = str(target)
    env["AGENT_FLOW_TEST_HOLD_AFTER_JSON_AUTH_OPEN_MS"] = "1500"
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stderr is not None
    marker = ""
    while line := process.stderr.readline():
        marker += line
        if "agent-flow:test-json-authority-opened" in line:
            break
    assert "agent-flow:test-json-authority-opened" in marker
    if replacement_kind == "authority":
        authority = target.parent
        authority.rename(authority.with_name("skills.original"))
        authority.mkdir()
        target.write_text('{"version": 999}\n', encoding="utf-8")
    else:
        target.rename(target.with_suffix(".original"))
        if replacement_kind == "symlink":
            target.symlink_to(outside)
        else:
            os.mkfifo(target)
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode != 0, stdout
    assert "invalid skill compatibility metadata" in stderr
    assert outside.read_text(encoding="utf-8") == '{"version": 999}\n'


def test_corrupt_existing_kit_cannot_downgrade_index_authentication(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    kit_path = project / ".agent-flow" / "kit.json"
    index_path = project / ".agent-flow" / "skills" / "index.json"
    previous_index = index_path.read_bytes()
    kit_path.write_text("{", encoding="utf-8")

    result = _install(project)

    assert result.returncode != 0
    assert "invalid existing kit metadata" in result.stderr
    assert index_path.read_bytes() == previous_index


@pytest.mark.parametrize(
    ("relative", "expected"),
    (
        (Path("kit.json"), "invalid existing kit metadata"),
        (Path("skills/index.json"), "unsafe previous skill index file"),
    ),
)
def test_installed_authority_fifo_is_rejected_without_blocking(
    tmp_path: Path,
    relative: Path,
    expected: str,
) -> None:
    if not hasattr(os, "O_NONBLOCK"):
        pytest.skip("nonblocking descriptor reads are required")
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    target = project / ".agent-flow" / relative
    target.unlink()
    os.mkfifo(target)
    test_home = tmp_path / "test-home"
    env = {
        **os.environ,
        "HOME": str(test_home),
        "CODEX_HOME": str(test_home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(test_home / ".claude"),
        "PI_CODING_AGENT_DIR": str(test_home / ".omp" / "agent"),
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "1",
        "AGENT_FLOW_INSTALL_SANDBOXED": "1",
    }

    result = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        env=env,
        timeout=10,
    )

    assert result.returncode != 0
    assert expected in result.stderr


def test_authenticated_index_rejects_authority_swap_after_open(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    target = project / ".agent-flow" / "skills" / "index.json"
    test_home = tmp_path / "test-home"
    env = {
        **os.environ,
        "HOME": str(test_home),
        "CODEX_HOME": str(test_home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(test_home / ".claude"),
        "PI_CODING_AGENT_DIR": str(test_home / ".omp" / "agent"),
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "1",
        "AGENT_FLOW_INSTALL_SANDBOXED": "1",
        "AGENT_FLOW_TEST_HOLD_JSON_AUTH_PATH": str(target),
        "AGENT_FLOW_TEST_HOLD_AFTER_JSON_AUTH_OPEN_MS": "1500",
    }
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stderr is not None
    marker = ""
    while line := process.stderr.readline():
        marker += line
        if "agent-flow:test-json-authority-opened" in line:
            break
    assert "agent-flow:test-json-authority-opened" in marker
    authority = target.parent
    preserved = authority.with_name("skills.original")
    authority.rename(preserved)
    authority.mkdir()
    shutil.copy2(preserved / "index.json", target)
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode != 0, stdout
    assert "previous skill index directory changed while reading" in stderr


def test_external_source_deletion_before_copy_rolls_back_without_data_loss(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    source = home / ".codex" / "skills" / "alpha"
    _skill(source, "external alpha")
    env = {
        **os.environ,
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "PI_CODING_AGENT_DIR": str(home / ".omp" / "agent"),
        "AGENT_FLOW_ACTIVE_HOST": "codex",
        "AGENT_FLOW_HOST": "codex",
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "1",
        "AGENT_FLOW_INSTALL_SANDBOXED": "1",
        "AGENT_FLOW_TEST_HOLD_BEFORE_MATERIALIZE_SOURCE_COPY_MS": "1500",
    }
    process = subprocess.Popen(
        (
            _node(),
            str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"),
            "install",
            "--skills",
            "alpha",
        ),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stderr is not None
    output = ""
    while line := process.stderr.readline():
        output += line
        if "agent-flow:test-materialize-source-copy-ready" in line:
            break
    assert "agent-flow:test-materialize-source-copy-ready" in output
    shutil.rmtree(source)
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, stdout
    assert "skill source disappeared while copying: alpha" in stderr
    assert not (project / ".agent-flow" / "skills" / "alpha").exists()


@pytest.mark.parametrize("replacement_body", ("external alpha", "attacker"))
def test_external_materialization_post_rename_swap_preserves_replacement(
    tmp_path: Path,
    replacement_body: str,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    _skill(home / ".codex" / "skills" / "alpha", "external alpha")
    env = {
        **os.environ,
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "PI_CODING_AGENT_DIR": str(home / ".omp" / "agent"),
        "AGENT_FLOW_ACTIVE_HOST": "codex",
        "AGENT_FLOW_HOST": "codex",
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "1",
        "AGENT_FLOW_INSTALL_SANDBOXED": "1",
        "AGENT_FLOW_TEST_HOLD_AFTER_MATERIALIZE_RENAME_MS": "1500",
    }
    process = subprocess.Popen(
        (
            _node(),
            str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"),
            "install",
            "--skills",
            "alpha",
        ),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stderr is not None
    output = ""
    while line := process.stderr.readline():
        output += line
        if "agent-flow:test-materialize-source-renamed" in line:
            break
    assert "agent-flow:test-materialize-source-renamed" in output
    destination = project / ".agent-flow" / "skills" / "alpha"
    destination.rename(tmp_path / "transaction-alpha")
    _skill(destination, replacement_body)
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, stdout
    assert "installed skill destination changed after replacement: alpha" in stderr
    assert replacement_body in (destination / "SKILL.md").read_text(encoding="utf-8")
    assert (project / ".agent-flow" / "install-transaction").exists()


@pytest.mark.parametrize("same_content", (True, False))
def test_bundled_materialization_post_rename_swap_preserves_replacement(
    tmp_path: Path,
    same_content: bool,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_FLOW_INSTALL_SANDBOXED": "1",
        "AGENT_FLOW_TEST_PLANNED_SKILL_NAME": "code-generation-discipline",
        "AGENT_FLOW_TEST_HOLD_AFTER_PLANNED_SKILL_RENAME_MS": "1500",
    }
    process = subprocess.Popen(
        (
            _node(),
            str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"),
            "install",
            "--skills",
            "code-generation-discipline",
        ),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stderr is not None
    output = ""
    while line := process.stderr.readline():
        output += line
        if "agent-flow:test-planned-skill-renamed" in line:
            break
    assert "agent-flow:test-planned-skill-renamed" in output
    destination = (
        project / ".agent-flow" / "skills" / "code-generation-discipline"
    )
    trusted = tmp_path / "trusted-bundled"
    destination.rename(trusted)
    shutil.copytree(trusted, destination, symlinks=True, copy_function=shutil.copy2)
    if not same_content:
        (destination / "SKILL.md").write_text("attacker\n", encoding="utf-8")
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, stdout
    assert (
        "installed skill destination changed after replacement: "
        "code-generation-discipline"
    ) in stderr
    if not same_content:
        assert (destination / "SKILL.md").read_text(encoding="utf-8") == "attacker\n"
    assert (project / ".agent-flow" / "install-transaction").exists()


def test_materialization_checkpoints_each_completed_entry(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    _skill(home / ".codex" / "skills" / "alpha", "external alpha")
    beta_source = home / ".codex" / "skills" / "beta"
    _skill(beta_source, "external beta")
    env = {
        **os.environ,
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "PI_CODING_AGENT_DIR": str(home / ".omp" / "agent"),
        "AGENT_FLOW_ACTIVE_HOST": "codex",
        "AGENT_FLOW_HOST": "codex",
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "1",
        "AGENT_FLOW_INSTALL_SANDBOXED": "1",
        "AGENT_FLOW_TEST_HOLD_AFTER_MATERIALIZE_CHECKPOINT_MS": "1500",
    }
    process = subprocess.Popen(
        (
            _node(),
            str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"),
            "install",
            "--skills",
            "alpha,beta",
        ),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stderr is not None
    output = ""
    while line := process.stderr.readline():
        output += line
        if "agent-flow:test-materialize-source-checkpointed" in line:
            break
    assert "agent-flow:test-materialize-source-checkpointed" in output
    shutil.rmtree(beta_source)
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, stdout
    assert "skill source disappeared while copying: beta" in stderr
    assert not (project / ".agent-flow" / "install-transaction").exists()
    assert not (project / ".agent-flow" / "skills" / "alpha").exists()
    assert not (project / ".agent-flow" / "skills" / "beta").exists()


@pytest.mark.parametrize(
    ("hold_env", "marker"),
    (
        (
            "AGENT_FLOW_TEST_HOLD_BEFORE_MATERIALIZE_SOURCE_COPY_MS",
            "agent-flow:test-materialize-source-copy-ready",
        ),
        (
            "AGENT_FLOW_TEST_HOLD_AFTER_MATERIALIZE_DISPLACE_MS",
            "agent-flow:test-materialize-source-displaced",
        ),
        (
            "AGENT_FLOW_TEST_HOLD_AFTER_MATERIALIZE_RENAME_MS",
            "agent-flow:test-materialize-source-renamed",
        ),
        (
            "AGENT_FLOW_TEST_HOLD_AFTER_MATERIALIZE_CHECKPOINT_MS",
            "agent-flow:test-materialize-source-checkpointed",
        ),
        (
            "AGENT_FLOW_TEST_HOLD_AFTER_MATERIALIZE_CLEANUP_RENAME_MS",
            "agent-flow:test-materialize-cleanup-renamed",
        ),
    ),
)
def test_recovery_reconciles_pending_materialization(
    tmp_path: Path,
    hold_env: str,
    marker: str,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    _skill(home / ".codex" / "skills" / "alpha", "external alpha")
    env = {
        **os.environ,
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "AGENT_FLOW_ACTIVE_HOST": "codex",
        "AGENT_FLOW_HOST": "codex",
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "1",
        "AGENT_FLOW_TEST_PREPOPULATE_MATERIALIZE_SKILL": "alpha",
        hold_env: "10000",
    }
    process = subprocess.Popen(
        (
            _node(),
            str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"),
            "install",
            "--skills",
            "alpha",
        ),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    assert process.stderr is not None
    output = ""
    while line := process.stderr.readline():
        output += line
        if marker in line:
            break
    assert marker in output
    os.killpg(process.pid, signal.SIGKILL)
    process.communicate(timeout=10)

    recovered = _install(
        project,
        "--skills",
        "alpha",
        env={"HOME": str(home), "AGENT_FLOW_HOST": "codex"},
    )

    assert recovered.returncode == 0, recovered.stderr
    assert not (project / ".agent-flow" / "install-transaction").exists()


def test_recovery_retries_after_materialization_restore(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    _skill(home / ".codex" / "skills" / "alpha", "external alpha")
    env = {
        **os.environ,
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "AGENT_FLOW_ACTIVE_HOST": "codex",
        "AGENT_FLOW_HOST": "codex",
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "1",
        "AGENT_FLOW_TEST_PREPOPULATE_MATERIALIZE_SKILL": "alpha",
        "AGENT_FLOW_TEST_FAIL_AFTER_MATERIALIZE_RENAME": "alpha",
        "AGENT_FLOW_TEST_HOLD_AFTER_MATERIALIZE_RESTORE_MS": "10000",
    }
    process = subprocess.Popen(
        (
            _node(),
            str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"),
            "install",
            "--skills",
            "alpha",
        ),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    assert process.stderr is not None
    output = ""
    while line := process.stderr.readline():
        output += line
        if "agent-flow:test-materialize-displaced-restored" in line:
            break
    assert "agent-flow:test-materialize-displaced-restored" in output
    os.killpg(process.pid, signal.SIGKILL)
    process.communicate(timeout=10)

    recovered = _install(
        project,
        "--skills",
        "alpha",
        env={"HOME": str(home), "AGENT_FLOW_HOST": "codex"},
    )

    assert recovered.returncode == 0, recovered.stderr
    assert not (project / ".agent-flow" / "install-transaction").exists()


@pytest.mark.parametrize("same_content", (True, False))
def test_materialization_cleanup_swap_preserves_replacement(
    tmp_path: Path,
    same_content: bool,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    _skill(home / ".codex" / "skills" / "alpha", "external alpha")
    env = {
        **os.environ,
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "AGENT_FLOW_ACTIVE_HOST": "codex",
        "AGENT_FLOW_HOST": "codex",
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "1",
        "AGENT_FLOW_TEST_PREPOPULATE_MATERIALIZE_SKILL": "alpha",
        "AGENT_FLOW_TEST_HOLD_BEFORE_MATERIALIZE_CLEANUP_RENAME_MS": "1500",
    }
    process = subprocess.Popen(
        (
            _node(),
            str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"),
            "install",
            "--skills",
            "alpha",
        ),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stderr is not None
    output = ""
    while line := process.stderr.readline():
        output += line
        if "agent-flow:test-materialize-cleanup-ready" in line:
            break
    assert "agent-flow:test-materialize-cleanup-ready" in output
    skills = project / ".agent-flow" / "skills"
    displaced = next(skills.glob(".agent-flow-displaced-*"))
    trusted = tmp_path / "trusted-displaced"
    displaced.rename(trusted)
    shutil.copytree(trusted, displaced, symlinks=True, copy_function=shutil.copy2)
    if not same_content:
        (displaced / "SKILL.md").write_text("attacker\n", encoding="utf-8")
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, stdout
    assert "changed during cleanup" in stderr
    assert displaced.exists()
    if not same_content:
        assert (displaced / "SKILL.md").read_text(encoding="utf-8") == "attacker\n"
    assert (project / ".agent-flow" / "install-transaction").exists()


@pytest.mark.parametrize("same_content", (True, False))
def test_materialization_stage_swap_preserves_replacement(
    tmp_path: Path,
    same_content: bool,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    _skill(home / ".codex" / "skills" / "alpha", "external alpha")
    env = {
        **os.environ,
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "AGENT_FLOW_ACTIVE_HOST": "codex",
        "AGENT_FLOW_HOST": "codex",
        "AGENT_FLOW_TEST_HOLD_AFTER_MATERIALIZE_STAGE_HASH_MS": "1500",
    }
    process = subprocess.Popen(
        (
            _node(),
            str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"),
            "install",
            "--skills",
            "alpha",
        ),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stderr is not None
    output = ""
    while line := process.stderr.readline():
        output += line
        if "agent-flow:test-materialize-stage-hashed" in line:
            break
    assert "agent-flow:test-materialize-stage-hashed" in output
    staging_root = project / ".agent-flow" / "install-transaction" / "materialized-staging"
    stage = next(staging_root.glob("alpha-*"))
    trusted = tmp_path / "trusted-stage"
    stage.rename(trusted)
    shutil.copytree(trusted, stage, symlinks=True, copy_function=shutil.copy2)
    if not same_content:
        (stage / "SKILL.md").write_text("attacker\n", encoding="utf-8")
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, stdout
    destination = project / ".agent-flow" / "skills" / "alpha"
    assert destination.exists()
    if not same_content:
        assert (destination / "SKILL.md").read_text(encoding="utf-8") == "attacker\n"
    assert (project / ".agent-flow" / "install-transaction").exists()


def test_external_materialization_parent_swap_preserves_outside_content(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    project.mkdir()
    outside_skill = outside / "alpha"
    outside_skill.mkdir(parents=True)
    marker = outside_skill / "user.txt"
    marker.write_text("outside\n", encoding="utf-8")
    _skill(home / ".codex" / "skills" / "alpha", "external alpha")
    env = {
        **os.environ,
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "PI_CODING_AGENT_DIR": str(home / ".omp" / "agent"),
        "AGENT_FLOW_ACTIVE_HOST": "codex",
        "AGENT_FLOW_HOST": "codex",
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "1",
        "AGENT_FLOW_INSTALL_SANDBOXED": "1",
        "AGENT_FLOW_TEST_HOLD_AFTER_MATERIALIZE_STAGE_HASH_MS": "1500",
    }
    process = subprocess.Popen(
        (
            _node(),
            str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"),
            "install",
            "--skills",
            "alpha",
        ),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stderr is not None
    output = ""
    while line := process.stderr.readline():
        output += line
        if "agent-flow:test-materialize-stage-hashed" in line:
            break
    assert "agent-flow:test-materialize-stage-hashed" in output
    skills = project / ".agent-flow" / "skills"
    preserved = project / ".agent-flow" / "skills.original"
    skills.rename(preserved)
    skills.symlink_to(outside, target_is_directory=True)
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode != 0, stdout
    assert marker.read_text(encoding="utf-8") == "outside\n"


def test_managed_ancestor_swap_never_writes_outside_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / ".Codex").mkdir()
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["AGENT_FLOW_INSTALL_SANDBOXED"] = "1"
    env["AGENT_FLOW_TEST_HOLD_MANAGED_PARENT_SUFFIX"] = ".Codex"
    env["AGENT_FLOW_TEST_HOLD_MANAGED_PARENT_MS"] = "1500"
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
    assert "agent-flow:test-managed-parent-anchored" in marker
    outside_owned = outside / ".Codex-owned"
    (project / ".Codex").rename(outside_owned)
    before = sorted(path.relative_to(outside_owned) for path in outside_owned.rglob("*"))
    (project / ".Codex").symlink_to(outside, target_is_directory=True)
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode != 0, stdout
    assert (
        "outside boundary" in stderr
        or "path escapes parent" in stderr
        or "staged mutation mismatch" in stderr
        or "changed outside transaction" in stderr
        or "Operation not permitted" in stderr
    )
    assert sorted(path.relative_to(outside_owned) for path in outside_owned.rglob("*")) == before


def test_managed_leaf_replacement_is_preserved_at_swap_boundary(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    target = project / ".agent-flow" / "workflows"
    preserved = project / ".agent-flow" / "workflows-owned"
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["AGENT_FLOW_TEST_HOLD_BEFORE_MANAGED_SWAP_MS"] = "1500"
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stderr is not None
    assert "agent-flow:test-managed-swap-ready" in process.stderr.readline()
    target.rename(preserved)
    target.mkdir()
    marker = target / "external.txt"
    marker.write_text("external\n", encoding="utf-8")
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode != 0, stdout
    assert "changed outside transaction" in stderr
    assert marker.read_text(encoding="utf-8") == "external\n"
    assert preserved.is_dir()


def test_managed_target_created_after_displacement_is_preserved(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    target = project / ".agent-flow" / "workflows"
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["AGENT_FLOW_TEST_HOLD_AFTER_MANAGED_DISPLACE_MS"] = "1500"
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stderr is not None
    assert "agent-flow:test-managed-target-displaced" in process.stderr.readline()
    assert not target.exists()
    target.mkdir()
    marker = target / "external.txt"
    marker.write_text("external\n", encoding="utf-8")
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode != 0, stdout
    assert "target appeared during swap" in stderr
    assert marker.read_text(encoding="utf-8") == "external\n"


def test_managed_staging_swap_cannot_redirect_live_mutation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    assert _install(project).returncode == 0
    outside_next = outside / "next"
    outside_next.mkdir()
    marker = outside_next / "external.txt"
    marker.write_text("external\n", encoding="utf-8")
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["AGENT_FLOW_TEST_HOLD_BEFORE_MANAGED_SWAP_MS"] = "1500"
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stderr is not None
    assert "agent-flow:test-managed-swap-ready" in process.stderr.readline()
    staging = next((project / ".agent-flow" / "install-transaction" / "managed-staging").iterdir())
    staging.rename(staging.with_name(f"{staging.name}-owned"))
    staging.symlink_to(outside, target_is_directory=True)
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode != 0, stdout
    assert "staging integrity mismatch" in stderr
    assert marker.read_text(encoding="utf-8") == "external\n"
    assert not (outside / "previous").exists()


def test_managed_incoming_replacement_is_preserved(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["AGENT_FLOW_TEST_HOLD_BEFORE_MANAGED_SWAP_MS"] = "1500"
    process = subprocess.Popen(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stderr is not None
    assert "agent-flow:test-managed-swap-ready" in process.stderr.readline()
    transaction = project / ".agent-flow" / "install-transaction"
    journal = json.loads((transaction / "journal.json").read_text(encoding="utf-8"))
    pending = next(operation["pending"] for operation in journal["managed_mutations"] if operation["pending"])
    incoming = project / pending["incoming"]
    incoming.mkdir()
    marker = incoming / "external.txt"
    marker.write_text("external\n", encoding="utf-8")
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode != 0, stdout
    assert "target appeared during swap" in stderr or "temporary path changed" in stderr
    assert marker.read_text(encoding="utf-8") == "external\n"


def test_reinstall_preserves_unowned_legacy_graphify_directories(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    markers = []
    for relative in (".Codex/skills", ".claude/skills", ".omp/skills"):
        marker = project / relative / "graphify" / "user.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("user-owned\n", encoding="utf-8")
        markers.append(marker)

    result = _install(project)

    assert result.returncode == 0, result.stderr
    assert all(marker.read_text(encoding="utf-8") == "user-owned\n" for marker in markers)


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
    original_inode = host_link.lstat().st_ino
    shutil.rmtree(source)

    failed = _command(
        project,
        "install",
        env={**env, "AGENT_FLOW_TEST_FAIL_AFTER_SKILL_INDEX": "1"},
    )

    assert failed.returncode != 0
    assert host_link.is_symlink()
    assert host_link.lstat().st_ino == original_inode
    assert (host_link / "SKILL.md").exists()

    reinstalled = _install(project, env=env)

    assert reinstalled.returncode == 0, reinstalled.stderr
    assert not host_link.exists() and not host_link.is_symlink()


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
    provider_fingerprints: set[str] = set()
    normalized_claim_sets: list[list[dict[str, object]]] = []
    normalized_runtime_plans: list[dict[str, object]] = []
    for host in ("claude", "codex", "omp"):
        project = tmp_path / "project"
        if project.exists():
            shutil.rmtree(project)
        project.mkdir()
        home = tmp_path / f"home-{host}"
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
        provider_fingerprints.add(index["provider_registry"]["fingerprint"])
        normalized_claims = json.loads(json.dumps(index["skill_providers"]))
        for claim in normalized_claims:
            if claim["source_host"] is not None:
                claim["source_host"] = "active-host"
                claim["source_locator"] = (
                    f"host://active-host/skills/{claim['concrete_id']}"
                )
        normalized_claim_sets.append(normalized_claims)
        runtime_plan = resolve_runtime_skill_plan(
            index,
            phase_id="implement",
            required_skills=("parity-skill",),
        )
        for skill in runtime_plan["skills"]:
            provider = skill.get("provider")
            if provider is not None and provider["source_host"] is not None:
                provider["source_host"] = "active-host"
                provider["source_locator"] = (
                    f"host://active-host/skills/{skill['name']}"
                )
        normalized_runtime_plans.append(runtime_plan)
        selected = next(skill for skill in index["skills"] if skill["name"] == "parity-skill")
        assert selected["source"] == "host-bootstrap"
        assert selected["source_host"] == host
        assert (project / ".Codex" / "skills" / "parity-skill" / "SKILL.md").exists()
        assert (project / ".claude" / "skills" / "parity-skill" / "SKILL.md").exists()
        assert (project / ".omp" / "skills" / "parity-skill" / "SKILL.md").exists()

    assert len(revisions) == 1
    assert len(provider_fingerprints) == 1
    assert normalized_claim_sets[0] == normalized_claim_sets[1] == normalized_claim_sets[2]
    assert (
        normalized_runtime_plans[0]
        == normalized_runtime_plans[1]
        == normalized_runtime_plans[2]
    )


def test_installed_skill_plan_is_committed_in_kit_and_tamper_fails(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert _install(project).returncode == 0
    kit = json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))

    pin = installed_skill_plan_pin(project)

    assert pin["skill_plan_hash"] == kit["skill_plan_hash"]
    index_path = project / ".agent-flow" / "skills" / "index.json"
    original = index_path.read_bytes()
    index = json.loads(original)
    index["selection"]["explicit_skills"] = ["forged"]
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(SkillPlanSnapshotError, match="matches kit.json"):
        installed_skill_plan_pin(project)

    index_path.write_bytes(original)
    index = json.loads(original)
    index["skill_providers"][0]["ownership"] = "user"
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
    assert "managed install path contains a symlink" in result.stderr
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

def test_build_resolved_skill_lock_is_deterministic_and_captures_provider_claims() -> None:
    index = {
        "catalog_fingerprint": "cat-fp",
        "provider_registry": {"fingerprint": "reg-fp"},
        "selection": {"profiles": ["python", "android"]},
        "skills": [
            {
                "name": "beta",
                "path": "skills/beta/SKILL.md",
                "tree_hash": "bbb",
                "source": "project",
                "capabilities": ["y", "x"],
            },
            {
                "name": "alpha",
                "path": "skills/alpha/SKILL.md",
                "tree_hash": "aaa",
                "source": "bundled",
            },
        ],
        "skill_providers": [
            {
                "concrete_id": "alpha",
                "provider_id": "official-android",
                "provider_version": "2.1.0",
                "source_hash": "a" * 64,
                "trust_tier": "official",
                "ownership": "upstream",
            }
        ],
    }
    lock, lock_hash = build_resolved_skill_lock(index, "plan-hash")
    # deterministic: same input, same hash; skills sorted by name.
    assert build_resolved_skill_lock(index, "plan-hash")[1] == lock_hash
    assert [entry["name"] for entry in lock["skills"]] == ["alpha", "beta"]
    assert lock["active_profiles"] == ["android", "python"]
    assert lock["catalog_fingerprint"] == "cat-fp"
    assert lock["provider_registry_fingerprint"] == "reg-fp"
    assert lock["skill_plan_hash"] == "plan-hash"
    alpha = lock["skills"][0]
    assert alpha["provider_id"] == "official-android"
    assert alpha["provider_version"] == "2.1.0"
    assert alpha["trust_tier"] == "official"
    assert lock["skills"][1]["capabilities"] == ["x", "y"]
    # ordering of input skills must not change the lock hash.
    reordered = dict(index)
    reordered["skills"] = list(reversed(index["skills"]))
    assert build_resolved_skill_lock(reordered, "plan-hash")[1] == lock_hash

def test_build_resolved_skill_lock_matches_provider_claims_case_insensitively() -> None:
    # Provider claims are keyed by the casefolded concrete_id; a mixed-case installed
    # skill name must still capture its provenance instead of dropping it to None.
    index = {
        "selection": {"profiles": ["android"]},
        "skills": [
            {"name": "CameraX", "path": "skills/camerax/SKILL.md", "tree_hash": "c", "source": "project"},
        ],
        "skill_providers": [
            {
                "concrete_id": "camerax",
                "provider_id": "android-official",
                "provider_version": "2.0.0",
                "source_hash": "c" * 64,
                "trust_tier": "official",
                "ownership": "upstream",
            }
        ],
    }
    lock, _ = build_resolved_skill_lock(index, "h")
    entry = lock["skills"][0]
    assert entry["name"] == "CameraX"
    assert entry["provider_id"] == "android-official"
    assert entry["provider_version"] == "2.0.0"
    assert entry["source_hash"] == "c" * 64

def test_partial_install_raises_structured_install_missing(tmp_path: Path) -> None:
    # kit metadata present but the skill index is absent -> structured install_missing,
    # not a raw/conflated snapshot error.
    from agent_flow.core.skill_plan import (
        HostExposureError,
        authenticated_installed_skill_index,
    )

    project = tmp_path / "project"
    (project / ".agent-flow" / "skills").mkdir(parents=True)
    (project / ".agent-flow" / "kit.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(HostExposureError) as excinfo:
        authenticated_installed_skill_index(project)
    assert excinfo.value.reason == "install_missing"
