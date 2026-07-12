"""Smoke + robustness tests.

Covers:
  - happy path: run start → pause → continue → complete (stub mode)
  - empty-state: continue / status with no active run
  - CLI detection returns plausible list
  - workflow validation: missing/empty `phases` raises clear error
  - meta.json safe parse: malformed JSON does not crash
  - concurrent run guard: second `run` is rejected while one is active
  - abort: clears active marker, artifacts preserved
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


KIT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = KIT_ROOT / "src"
LEDGER_RUN_SNAPSHOT = {
    "runtime_id": "test",
    "profile_snapshot_sha256": "a" * 64,
    "installed_skill_plan_sha256": "b" * 64,
    "local_skill_plan_sha256": "c" * 64,
    "lore_snapshot_sha256": "d" * 64,
    "prompt_controls_sha256": "e" * 64,
}
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def _run_cli(args: list[str], cwd: Path, env_extra: dict | None = None):
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("AGENT_FLOW_") or key in {
            "OMP_PROFILE",
            "PI_CODING_AGENT_DIR",
            "CLAUDECODE",
            "CLAUDE_CLI",
            "CLAUDE_CONFIG_DIR",
            "CODEX_CLI",
            "CODEX_HOME",
            "CODEX_SHELL",
            "CODEX_THREAD_ID",
            "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
        }:
            env.pop(key, None)
    env["PYTHONPATH"] = str(KIT_ROOT / "src")
    env["AGENT_FLOW_ADAPTER"] = "generic"
    env["AGENT_FLOW_GENERIC_MODE"] = "stub-success"
    if env_extra:
        env.update(env_extra)
    effective_args = list(args)
    if effective_args and effective_args[0] == "run" and "--workflow" not in effective_args:
        effective_args.extend(("--workflow", "default"))
    return subprocess.run(
        [sys.executable, "-m", "agent_flow.cli", *effective_args],
        cwd=cwd, env=env, capture_output=True, text=True,
    )


def _init_git_project(project: Path) -> None:
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)
    (project / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project, check=True, capture_output=True, text=True)


def _branch_exists(project: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=project,
        check=False,
    )
    return result.returncode == 0


def _worktree_runtime_root(project: Path, name: str) -> Path:
    return project / ".git" / "agent-flow" / "worktrees" / name


def test_python_runner_pins_base_snapshot_in_run_meta(tmp_path: Path, monkeypatch) -> None:
    from agent_flow.artifact import read_meta
    from agent_flow.runner import ResumeMode, Runner

    project = tmp_path / "base-pin"
    project.mkdir()
    _init_git_project(project)
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setenv("AGENT_FLOW_ADAPTER", "generic")
    monkeypatch.setenv("AGENT_FLOW_GENERIC_MODE", "stub-success")

    runner = Runner(project, workflow="default")
    runner.run(ResumeMode.START, task="pin base")

    meta = read_meta(runner.run_dir)
    assert meta["base_ref"] == "main"
    assert meta["base_commit"] == expected


def test_full_cycle(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    _init_git_project(project)

    r1 = _run_cli(["run", "test feature"], project)
    assert r1.returncode == 0, r1.stderr
    assert "run started" in r1.stdout

    runs_dir = _worktree_runtime_root(project, "feat-test-feature") / ".agent-flow" / "runs"
    runs = list(runs_dir.iterdir())
    assert len(runs) == 1
    run_dir = runs[0]
    assert (run_dir / "active").exists()

    expected_pre_pause = ["design", "slice-plan"]
    for a in expected_pre_pause:
        assert (run_dir / f"{a}.md").exists(), f"missing pre-pause: {a}"
    assert "pause" in r1.stdout.lower()

    r_status = _run_cli(["status"], project)
    assert r_status.returncode == 0
    assert "test feature" in r_status.stdout

    r2 = _run_cli(["continue", "--approve-pause", "--worktree", "test feature"], project)
    assert r2.returncode == 0, r2.stderr
    assert "run complete" in r2.stdout

    expected_post = [
        "worktree", "implement", "comment-authoring", "final-review", "artifacts/gate-results", "fix-loop",
        "commit", "push-pr", "pr-watch", "merge", "cleanup",
    ]
    for a in expected_post:
        assert (run_dir / f"{a}.md").exists() or (run_dir / f"{a}.json").exists(), f"missing post-pause: {a}"
    assert not (run_dir / "active").exists()


def test_runner_injects_installed_profile_union_into_prompt(tmp_path: Path):
    from agent_flow.adapters.generic import GenericAdapter
    from agent_flow.runner import Phase, Runner

    project = tmp_path / "multi-profile"
    project.mkdir()
    kit = project / ".agent-flow"
    kit.mkdir()
    installed_profiles = kit / "profiles"
    installed_profiles.mkdir()
    for profile in ("android", "react-native"):
        (installed_profiles / f"{profile}.yaml").write_text(
            (KIT_ROOT / "profiles" / f"{profile}.yaml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    (kit / "kit.json").write_text(
        json.dumps(
            {
                "profile": "android",
                "primary_profile": "android",
                "profile_selection": "explicit",
                "profiles": ["android", "react-native"],
            }
        ),
        encoding="utf-8",
    )

    runner = Runner(project_root=project)
    assert runner.profile_id == "android,react-native"
    assert runner.profile["id"] == "multi-profile"
    assert runner.profile["active_profiles"] == ["android", "react-native"]
    assert len(runner.profile["profiles"]) == 2
    assert runner.profile["profiles"][0]["id"] == "android"
    assert runner.profile["profiles"][1]["id"] == "react-native"

    adapter = GenericAdapter()
    adapter._profile_id = runner.profile_id
    adapter._profile_snapshot = runner.profile
    prompt = adapter.render_envelope(
        Phase(id="design", description="Design", prompt="Do work."),
        project / ".agent-flow" / "runs" / "r1",
        project,
    )
    assert "## Active profile: `android,react-native`" in prompt
    assert "active_profiles:" in prompt
    assert "- android" in prompt
    assert "- react-native" in prompt


def test_generic_stub_mode_blocks_instead_of_completing(tmp_path: Path):
    project = tmp_path / "stub-blocked"
    project.mkdir()
    _init_git_project(project)

    result = _run_cli(
        ["run", "test feature"],
        project,
        env_extra={"AGENT_FLOW_GENERIC_MODE": "stub"},
    )

    assert result.returncode == 0, result.stderr
    assert "generic_stub_artifact" in result.stdout
    runs = list(
        (_worktree_runtime_root(project, "feat-test-feature") / ".agent-flow" / "runs").iterdir()
    )
    assert len(runs) == 1
    run_dir = runs[0]
    assert (run_dir / "active").exists()
    assert (run_dir / "design.md").exists()
    assert not (run_dir / "slice-plan.md").exists()

    status = _run_cli(
        ["status"],
        project,
        env_extra={"AGENT_FLOW_GENERIC_MODE": "stub"},
    )
    assert status.returncode == 0
    assert "reason: generic_stub_artifact" in status.stdout


def test_no_active_run(tmp_path: Path):
    project = tmp_path / "empty"
    project.mkdir()
    r_continue = _run_cli(["continue"], project)
    assert r_continue.returncode == 0
    assert "진행 중인 run 없음" in r_continue.stdout

    r_status = _run_cli(["status"], project)
    assert r_status.returncode == 0
    assert "진행 중인 run 없음" in r_status.stdout


def test_concurrent_run_rejected(tmp_path: Path):
    """Starting a run while one is active must be rejected with a clear message."""
    project = tmp_path / "concurrent"
    project.mkdir()
    _init_git_project(project)

    # Start the first run with stub mode → it'll loop and eventually pause.
    r1 = _run_cli(["run", "first task"], project, env_extra={
        # Force pause early-ish: design phase is first; smoke tests use stub
        # which writes artifacts inline, so we'll have an active run after
        # pause at slice-plan.
    })
    assert r1.returncode == 0

    # Active marker should exist (paused).
    runs_dir = _worktree_runtime_root(project, "feat-first-task") / ".agent-flow" / "runs"
    actives = [p for p in runs_dir.iterdir() if (p / "active").exists()]
    assert len(actives) == 1

    # Try starting another run — must fail with exit code 2.
    r2 = _run_cli(["run", "second task"], project)
    assert r2.returncode == 2
    assert "already active" in r2.stdout.lower() or "already active" in r2.stderr.lower()


def test_abort_clears_marker(tmp_path: Path):
    project = tmp_path / "abort"
    project.mkdir()
    _init_git_project(project)

    r1 = _run_cli(["run", "to be aborted"], project)
    assert r1.returncode == 0

    runs_dir = _worktree_runtime_root(project, "feat-to-be-aborted") / ".agent-flow" / "runs"
    active = next(p for p in runs_dir.iterdir() if (p / "active").exists())
    assert active.exists()

    r2 = _run_cli(["abort", "--yes", "--worktree", "to be aborted"], project)
    assert r2.returncode == 0
    assert "aborted" in r2.stdout.lower()
    assert not (active / "active").exists()
    # Artifacts preserved
    assert (active / "design.md").exists()


def test_worktree_run_continue_status_abort(tmp_path: Path):
    project = tmp_path / "parallel"
    project.mkdir()
    _init_git_project(project)

    r1 = _run_cli(["run", "worktree task", "--worktree", "Long Press"], project)
    assert r1.returncode == 0, r1.stderr
    assert "worktree: feat-long-press" in r1.stdout

    worktree = project / ".agent-flow" / "worktrees" / "feat-long-press"
    runtime_root = _worktree_runtime_root(project, "feat-long-press")
    run_dir = next((runtime_root / ".agent-flow" / "runs").iterdir())
    assert (run_dir / "active").exists()
    assert not (worktree / ".agent-flow").exists()
    assert not (worktree / "manifest.json").exists()

    r_status = _run_cli(["status", "--worktree", "Long Press"], project)
    assert r_status.returncode == 0
    assert "worktree task" in r_status.stdout

    r_continue = _run_cli(["continue", "--approve-pause", "--worktree", "long-press"], project)
    assert r_continue.returncode == 0, r_continue.stderr
    assert "run complete" in r_continue.stdout
    assert (run_dir / "artifacts" / "gate-results.json").exists()

    r_empty_continue = _run_cli(["continue", "--worktree", "long-press"], project)
    assert r_empty_continue.returncode == 0
    assert '--worktree "feat-long-press"' in r_empty_continue.stdout

    r2 = _run_cli(["run", "abort me", "--worktree", "long-press"], project)
    assert r2.returncode == 0, r2.stderr
    assert "worktree: feat-long-press" in r2.stdout
    active = next(
        p
        for p in (runtime_root / ".agent-flow" / "runs").iterdir()
        if (p / "active").exists()
    )
    r_abort = _run_cli(["abort", "--worktree", "long-press", "--yes"], project)
    assert r_abort.returncode == 0
    assert not (active / "active").exists()

    r_list = _run_cli(["worktree", "list"], project)
    assert r_list.returncode == 0
    assert "feat-long-press" in r_list.stdout

    r_remove = _run_cli(["worktree", "remove", "--name", "long-press"], project)
    assert r_remove.returncode == 0, r_remove.stderr
    assert not worktree.exists()


def test_worktree_list_empty_and_multiple(tmp_path: Path):
    project = tmp_path / "list-worktrees"
    project.mkdir()
    _init_git_project(project)

    r_empty = _run_cli(["worktree", "list"], project)
    assert r_empty.returncode == 0
    assert "no worktrees" in r_empty.stdout

    assert _run_cli(["worktree", "create", "--name", "one"], project).returncode == 0
    assert _run_cli(["worktree", "create", "--name", "two"], project).returncode == 0
    r_list = _run_cli(["worktree", "list"], project)
    assert r_list.returncode == 0
    assert "feat-one feat/one" in r_list.stdout
    assert "feat-two feat/two" in r_list.stdout


def test_worktree_list_tolerates_invalid_stale_directory_name(tmp_path: Path):
    project = tmp_path / "list-invalid-stale"
    project.mkdir()
    _init_git_project(project)
    invalid_dir = project / ".agent-flow" / "worktrees" / "!!!"
    invalid_dir.mkdir(parents=True)

    r_list = _run_cli(["worktree", "list"], project)
    assert r_list.returncode == 0
    assert "!!!" in r_list.stdout
    assert "stale" in r_list.stdout
    assert "Traceback" not in r_list.stderr


def test_worktree_remove_cleans_stale_manifest(tmp_path: Path):
    project = tmp_path / "stale-worktree"
    project.mkdir()
    _init_git_project(project)
    subprocess.run(["git", "branch", "feat/ghost"], cwd=project, check=True)
    stale_dir = project / ".agent-flow" / "worktrees" / "feat-ghost"
    stale_dir.mkdir(parents=True)
    (stale_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "feat-ghost",
                "branch": "feat/ghost",
                "path": str(project / ".agent-flow" / "worktrees" / "feat-ghost"),
                "exists": True,
                "branch_created_by_agent_flow": True,
            }
        ),
        encoding="utf-8",
    )

    r_list = _run_cli(["worktree", "list"], project)
    assert r_list.returncode == 0
    assert "feat-ghost feat/ghost" in r_list.stdout
    assert "stale" in r_list.stdout

    r_remove = _run_cli(["worktree", "remove", "--name", "ghost"], project)
    assert r_remove.returncode == 0
    assert "removed stale" in r_remove.stdout
    assert not stale_dir.exists()
    assert not _branch_exists(project, "feat/ghost")


def test_worktree_status_tolerates_corrupt_manifest(tmp_path: Path):
    project = tmp_path / "corrupt-manifest"
    project.mkdir()
    _init_git_project(project)
    worktree_dir = project / ".agent-flow" / "worktrees" / "feat-ghost"
    worktree_dir.mkdir(parents=True)
    (worktree_dir / "manifest.json").write_text("{bad json", encoding="utf-8")

    r_status = _run_cli(["worktree", "status", "--name", "ghost"], project)
    assert r_status.returncode == 0
    assert "feat-ghost feat/ghost" in r_status.stdout


def test_worktree_remove_does_not_trust_string_owned_manifest_flag(tmp_path: Path):
    project = tmp_path / "string-owned-flag"
    project.mkdir()
    _init_git_project(project)
    subprocess.run(["git", "branch", "feature/keep"], cwd=project, check=True)
    stale_dir = project / ".agent-flow" / "worktrees" / "feat-ghost"
    stale_dir.mkdir(parents=True)
    (stale_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "feat-ghost",
                "branch": "feature/keep",
                "path": str(stale_dir),
                "exists": True,
                "branch_created_by_agent_flow": "false",
            }
        ),
        encoding="utf-8",
    )

    r_remove = _run_cli(["worktree", "remove", "--name", "ghost"], project)
    assert r_remove.returncode == 0, r_remove.stderr
    assert not stale_dir.exists()
    assert _branch_exists(project, "feature/keep")


def test_worktree_remove_does_not_trust_manifest_owned_branch(tmp_path: Path):
    project = tmp_path / "forged-owned-branch"
    project.mkdir()
    _init_git_project(project)
    subprocess.run(["git", "branch", "feature/keep"], cwd=project, check=True)
    stale_dir = project / ".agent-flow" / "worktrees" / "feat-ghost"
    stale_dir.mkdir(parents=True)
    (stale_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "feat-ghost",
                "branch": "feature/keep",
                "path": str(stale_dir),
                "exists": True,
                "branch_created_by_agent_flow": True,
            }
        ),
        encoding="utf-8",
    )

    r_remove = _run_cli(["worktree", "remove", "--name", "ghost"], project)
    assert r_remove.returncode == 0, r_remove.stderr
    assert not stale_dir.exists()
    assert _branch_exists(project, "feature/keep")


def test_worktree_status_sanitizes_malformed_manifest_name_and_branch(tmp_path: Path):
    project = tmp_path / "malformed-manifest-fields"
    project.mkdir()
    _init_git_project(project)
    worktree_dir = project / ".agent-flow" / "worktrees" / "feat-ghost"
    worktree_dir.mkdir(parents=True)
    (worktree_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "../feat-ghost",
                "branch": "../main",
                "path": str(worktree_dir),
                "exists": True,
                "branch_created_by_agent_flow": True,
            }
        ),
        encoding="utf-8",
    )

    r_status = _run_cli(["worktree", "status", "--name", "ghost"], project)
    assert r_status.returncode == 0
    assert "feat-ghost feat/ghost" in r_status.stdout


def test_worktree_remove_does_not_trust_manifest_path(tmp_path: Path):
    project = tmp_path / "manifest-path-redirect"
    project.mkdir()
    _init_git_project(project)

    r_victim = _run_cli(["worktree", "create", "--name", "victim"], project)
    assert r_victim.returncode == 0, r_victim.stderr
    victim_dir = project / ".agent-flow" / "worktrees" / "feat-victim"
    stale_dir = project / ".agent-flow" / "worktrees" / "feat-ghost"
    stale_dir.mkdir(parents=True)
    (stale_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "feat-ghost",
                "branch": "feat/ghost",
                "path": str(victim_dir),
                "exists": True,
                "branch_created_by_agent_flow": True,
            }
        ),
        encoding="utf-8",
    )

    r_remove = _run_cli(["worktree", "remove", "--name", "ghost"], project)
    assert r_remove.returncode == 0, r_remove.stderr
    assert not stale_dir.exists()
    assert victim_dir.exists()
    assert (victim_dir / ".git").exists()


def test_worktree_remove_handles_stale_path_file(tmp_path: Path):
    project = tmp_path / "stale-path-file"
    project.mkdir()
    _init_git_project(project)
    worktrees_root = project / ".agent-flow" / "worktrees"
    worktrees_root.mkdir(parents=True)
    stale_file = worktrees_root / "feat-ghost"
    stale_file.write_text("not a directory\n", encoding="utf-8")

    r_remove = _run_cli(["worktree", "remove", "--name", "ghost"], project)
    assert r_remove.returncode == 0
    assert "removed stale" in r_remove.stdout
    assert not stale_file.exists()


def test_worktree_remove_prunes_real_stale_owned_worktree_and_deletes_auto_branch(tmp_path: Path):
    project = tmp_path / "real-stale-worktree"
    project.mkdir()
    _init_git_project(project)

    r_create = _run_cli(["worktree", "create", "--name", "ghost"], project)
    assert r_create.returncode == 0, r_create.stderr
    stale_dir = project / ".agent-flow" / "worktrees" / "feat-ghost"
    assert _branch_exists(project, "feat/ghost")
    shutil.rmtree(stale_dir)
    stale_dir.mkdir(parents=True)
    (stale_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "feat-ghost",
                "branch": "feat/ghost",
                "path": str(stale_dir),
                "exists": True,
                "branch_created_by_agent_flow": True,
            }
        ),
        encoding="utf-8",
    )

    r_remove = _run_cli(["worktree", "remove", "--name", "ghost"], project)
    assert r_remove.returncode == 0, r_remove.stderr
    assert not stale_dir.exists()
    assert not _branch_exists(project, "feat/ghost")


def test_worktree_create_rejects_stale_path_reuse(tmp_path: Path):
    project = tmp_path / "stale-path-reuse"
    project.mkdir()
    _init_git_project(project)
    stale_dir = project / ".agent-flow" / "worktrees" / "feat-task"
    stale_dir.mkdir(parents=True)

    r_create = _run_cli(["worktree", "create", "--name", "task"], project)
    assert r_create.returncode == 2
    assert "not a git worktree" in r_create.stderr


def test_worktree_remove_keep_branch_preserves_stale_owned_branch(tmp_path: Path):
    project = tmp_path / "stale-keep-branch"
    project.mkdir()
    _init_git_project(project)
    subprocess.run(["git", "branch", "feat/ghost"], cwd=project, check=True)
    stale_dir = project / ".agent-flow" / "worktrees" / "feat-ghost"
    stale_dir.mkdir(parents=True)
    (stale_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "feat-ghost",
                "branch": "feat/ghost",
                "path": str(project / ".agent-flow" / "worktrees" / "feat-ghost"),
                "exists": True,
                "branch_created_by_agent_flow": True,
            }
        ),
        encoding="utf-8",
    )

    r_remove = _run_cli(["worktree", "remove", "--name", "ghost", "--keep-branch"], project)
    assert r_remove.returncode == 0
    assert not stale_dir.exists()
    assert _branch_exists(project, "feat/ghost")


def test_worktree_selector_requires_existing_worktree(tmp_path: Path):
    project = tmp_path / "missing-worktree"
    project.mkdir()

    r_status = _run_cli(["status", "--worktree", "missing"], project)
    assert r_status.returncode == 1
    assert "worktree not found" in r_status.stderr


def test_invalid_worktree_selectors_do_not_traceback(tmp_path: Path):
    project = tmp_path / "invalid-selectors"
    project.mkdir()

    commands = [
        ["continue", "--worktree", "!!!"],
        ["status", "--worktree", "!!!"],
        ["abort", "--worktree", "!!!"],
        ["worktree", "status", "--name", "!!!"],
        ["worktree", "remove", "--name", "!!!"],
        ["worktree", "status", "--name", "📱🚀"],
        ["worktree", "status", "--name", "feat-!!!"],
        ["worktree", "create", "--name", "..", "--branch", "valid-branch"],
    ]
    for command in commands:
        result = _run_cli(command, project)
        assert result.returncode == 2
        assert "worktree name must contain" in result.stderr
        assert "Traceback" not in result.stderr


def test_worktree_run_requires_git_repo(tmp_path: Path):
    project = tmp_path / "not-git"
    project.mkdir()

    r1 = _run_cli(["run", "task", "--worktree", "task"], project)
    assert r1.returncode == 2
    assert "requires a registered git worktree" in r1.stderr

    r2 = _run_cli(["start", "development", "--task", "task", "--worktree", "task"], project)
    assert r2.returncode == 2
    assert "requires a registered git worktree" in r2.stderr


def test_worktree_run_rejects_invalid_slug_and_branch(tmp_path: Path):
    project = tmp_path / "invalid-worktree"
    project.mkdir()
    _init_git_project(project)

    r_slug = _run_cli(["run", "task", "--worktree", "!!!"], project)
    assert r_slug.returncode == 2
    assert "worktree name must contain" in r_slug.stderr

    r_branch = _run_cli(["run", "task", "--worktree", "task", "--worktree-branch", "bad..branch"], project)
    assert r_branch.returncode == 2
    assert "unsafe worktree branch" in r_branch.stderr


def test_worktree_branch_validation_rejects_invalid_refs(tmp_path: Path):
    project = tmp_path / "invalid-branches"
    project.mkdir()
    _init_git_project(project)

    invalid_branches = ["foo bar", "foo~1", "foo:bar", "foo^", ".foo", "foo/", "foo//bar", "foo.lock"]
    for branch in invalid_branches:
        result = _run_cli(["run", "task", "--worktree", "task", "--worktree-branch", branch], project)
        assert result.returncode == 2
        assert "unsafe worktree branch" in result.stderr


def test_worktree_branch_must_use_feat_prefix(tmp_path: Path):
    project = tmp_path / "feat-branch-prefix"
    project.mkdir()
    _init_git_project(project)

    result = _run_cli(["run", "task", "--worktree", "task", "--worktree-branch", "feature/task"], project)

    assert result.returncode == 2
    assert "worktree branch must start with feat/" in result.stderr


def test_worktree_run_rejects_existing_branch_mismatch(tmp_path: Path):
    project = tmp_path / "branch-mismatch"
    project.mkdir()
    _init_git_project(project)

    r1 = _run_cli(["run", "task", "--worktree", "task"], project)
    assert r1.returncode == 0, r1.stderr
    r_continue = _run_cli(["continue", "--approve-pause", "--worktree", "task"], project)
    assert r_continue.returncode == 0, r_continue.stderr

    r2 = _run_cli(["run", "other", "--worktree", "task", "--worktree-branch", "feat/other"], project)
    assert r2.returncode == 2
    assert "already uses branch" in r2.stderr


def test_worktree_create_and_start_reject_existing_branch_mismatch(tmp_path: Path):
    project = tmp_path / "create-start-mismatch"
    project.mkdir()
    _init_git_project(project)

    r_create = _run_cli(["worktree", "create", "--name", "task"], project)
    assert r_create.returncode == 0, r_create.stderr
    r_mismatch = _run_cli(["worktree", "create", "--name", "task", "--branch", "feat/other"], project)
    assert r_mismatch.returncode == 2
    assert "already uses branch" in r_mismatch.stderr

    r_start_mismatch = _run_cli(
        ["start", "development", "--task", "task", "--worktree", "task", "--worktree-branch", "feat/other"],
        project,
    )
    assert r_start_mismatch.returncode == 2
    assert "already uses branch" in r_start_mismatch.stderr


def test_start_worktree_writes_state_outside_worktree(tmp_path: Path):
    project = tmp_path / "start-worktree-state"
    project.mkdir()
    _init_git_project(project)

    r_start = _run_cli(["start", "development", "--task", "task", "--worktree", "task"], project)
    assert r_start.returncode == 0, r_start.stderr
    worktree = project / ".agent-flow" / "worktrees" / "feat-task"
    runtime_root = _worktree_runtime_root(project, "feat-task")
    run_dir = next((runtime_root / ".agent-flow" / "runs" / "development").iterdir())
    assert (run_dir / "manifest.json").exists()
    assert not (worktree / ".agent-flow").exists()
    assert not (project / ".agent-flow" / "runs" / "default").exists()

    r_status = _run_cli(["status", "--worktree", "task"], project)
    assert r_status.returncode == 0
    assert "development" in r_status.stdout


def test_worktree_run_cleans_up_new_worktree_on_start_failure(tmp_path: Path):
    project = tmp_path / "cleanup"
    project.mkdir()
    _init_git_project(project)

    r1 = _run_cli(["run", "task", "--worktree", "task", "--workflow", "missing"], project)
    assert r1.returncode == 2
    assert "Traceback" not in r1.stderr
    assert not (project / ".agent-flow" / "worktrees" / "feat-task").exists()
    assert not _branch_exists(project, "feat/task")


def test_start_worktree_cleans_up_new_worktree_on_start_failure(tmp_path: Path):
    project = tmp_path / "start-cleanup"
    project.mkdir()
    _init_git_project(project)

    r1 = _run_cli(["start", "missing", "--task", "task", "--worktree", "task"], project)
    assert r1.returncode == 2
    assert "Traceback" not in r1.stderr
    assert not (project / ".agent-flow" / "worktrees" / "feat-task").exists()
    assert not _branch_exists(project, "feat/task")


def test_worktree_remove_preserves_preexisting_branch_by_default(tmp_path: Path):
    project = tmp_path / "preserve-branch"
    project.mkdir()
    _init_git_project(project)
    subprocess.run(["git", "branch", "feat/shared"], cwd=project, check=True)

    r_create = _run_cli(["worktree", "create", "--name", "task", "--branch", "feat/shared"], project)
    assert r_create.returncode == 0, r_create.stderr
    r_remove = _run_cli(["worktree", "remove", "--name", "task"], project)
    assert r_remove.returncode == 0, r_remove.stderr
    assert _branch_exists(project, "feat/shared")


def test_worktree_remove_deletes_agent_flow_created_branch(tmp_path: Path):
    project = tmp_path / "delete-owned-branch"
    project.mkdir()
    _init_git_project(project)

    r_create = _run_cli(["worktree", "create", "--name", "task"], project)
    assert r_create.returncode == 0, r_create.stderr
    assert _branch_exists(project, "feat/task")
    r_remove = _run_cli(["worktree", "remove", "--name", "task"], project)
    assert r_remove.returncode == 0, r_remove.stderr
    assert not _branch_exists(project, "feat/task")


def test_live_worktree_remove_does_not_trust_manifest_branch_redirect(tmp_path: Path):
    project = tmp_path / "live-branch-redirect"
    project.mkdir()
    _init_git_project(project)
    subprocess.run(["git", "branch", "feature/keep"], cwd=project, check=True)

    r_create = _run_cli(["worktree", "create", "--name", "task"], project)
    assert r_create.returncode == 0, r_create.stderr
    worktree = project / ".agent-flow" / "worktrees" / "feat-task"
    (_worktree_runtime_root(project, "feat-task") / "manifest.json").write_text(
        json.dumps(
            {
                "name": "feat-task",
                "branch": "feature/keep",
                "path": str(worktree),
                "exists": True,
                "branch_created_by_agent_flow": True,
            }
        ),
        encoding="utf-8",
    )

    r_remove = _run_cli(["worktree", "remove", "--name", "task"], project)
    assert r_remove.returncode == 0, r_remove.stderr
    assert _branch_exists(project, "feature/keep")
    assert _branch_exists(project, "feat/task")


def test_worktree_remove_keep_branch_preserves_agent_flow_created_branch(tmp_path: Path):
    project = tmp_path / "keep-owned-branch"
    project.mkdir()
    _init_git_project(project)

    r_create = _run_cli(["worktree", "create", "--name", "task"], project)
    assert r_create.returncode == 0, r_create.stderr
    r_remove = _run_cli(["worktree", "remove", "--name", "task", "--keep-branch"], project)
    assert r_remove.returncode == 0, r_remove.stderr
    assert _branch_exists(project, "feat/task")


def test_worktree_run_failure_preserves_preexisting_branch(tmp_path: Path):
    project = tmp_path / "failure-preserve-branch"
    project.mkdir()
    _init_git_project(project)
    subprocess.run(["git", "branch", "feat/shared"], cwd=project, check=True)

    r1 = _run_cli(["run", "task", "--worktree", "task", "--worktree-branch", "feat/shared", "--workflow", "missing"], project)
    assert r1.returncode == 2
    assert "Traceback" not in r1.stderr
    assert _branch_exists(project, "feat/shared")
    assert not (project / ".agent-flow" / "worktrees" / "feat-task").exists()


def test_worktree_run_reports_dirty_leader_without_traceback(tmp_path: Path):
    project = tmp_path / "dirty"
    project.mkdir()
    _init_git_project(project)
    (project / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    r1 = _run_cli(["run", "task", "--worktree", "task"], project)
    assert r1.returncode == 2
    assert "leader workspace is dirty" in r1.stderr
    assert "Traceback" not in r1.stderr


def test_worktree_run_allow_dirty_overrides_dirty_leader(tmp_path: Path):
    project = tmp_path / "allow-dirty"
    project.mkdir()
    _init_git_project(project)
    (project / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    r1 = _run_cli(["run", "task", "--worktree", "task", "--allow-dirty"], project)
    assert r1.returncode == 0, r1.stderr
    assert (project / ".agent-flow" / "worktrees" / "feat-task").exists()


def test_malformed_meta_does_not_crash(tmp_path: Path):
    project = tmp_path / "broken"
    project.mkdir()
    _init_git_project(project)

    r1 = _run_cli(["run", "ok task"], project)
    assert r1.returncode == 0

    runs_dir = _worktree_runtime_root(project, "feat-ok-task") / ".agent-flow" / "runs"
    active = next(p for p in runs_dir.iterdir() if (p / "active").exists())
    # Corrupt the meta file
    (active / "meta.json").write_text("not-json{{{")

    r_status = _run_cli(["status"], project)
    # status should still respond (degraded), not crash
    assert r_status.returncode == 0


def test_invalid_workflow_yaml_clear_error(tmp_path: Path):
    """Direct unit-test of _load_workflow on malformed / invalid YAMLs."""
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import _load_workflow

    # Empty file
    empty = tmp_path / "kit_empty"
    (empty / "workflows").mkdir(parents=True)
    (empty / "workflows" / "broken.yaml").write_text("")
    with pytest.raises(ValueError, match="missing or empty"):
        _load_workflow(empty, "broken")

    # Phases is None
    none_phases = tmp_path / "kit_none"
    (none_phases / "workflows").mkdir(parents=True)
    (none_phases / "workflows" / "x.yaml").write_text("phases:\n")
    with pytest.raises(ValueError, match="missing or empty"):
        _load_workflow(none_phases, "x")

    # Phase missing id
    no_id = tmp_path / "kit_noid"
    (no_id / "workflows").mkdir(parents=True)
    (no_id / "workflows" / "x.yaml").write_text(
        "phases:\n  - description: hi\n"
    )
    with pytest.raises(ValueError, match="missing `id`"):
        _load_workflow(no_id, "x")

    # Duplicate phase ids
    dup = tmp_path / "kit_dup"
    (dup / "workflows").mkdir(parents=True)
    (dup / "workflows" / "x.yaml").write_text(
        "phases:\n"
        "  - id: design\n"
        "  - id: implement\n"
        "  - id: design\n"
    )
    with pytest.raises(ValueError, match="duplicate phase id"):
        _load_workflow(dup, "x")


def test_route_block_returns_without_loop(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import Phase, Runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "pr-watch.md").write_text("status: pending\n", encoding="utf-8")

    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.phases = [
        Phase(
            id="pr-watch",
            description="",
            routes={"pending": "block"},
        )
    ]

    assert runner._next_index(0, runner.phases[0]) == (0, True)


def test_default_final_review_request_changes_routes_to_fix_loop(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import Phase, Runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "final-review.md").write_text(
        "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: request-changes\n\n"
        "## Overall\nverdict: request-changes\n",
        encoding="utf-8",
    )

    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.phases = [
        Phase(id="final-review", description="", multi_review=True, routes={"approve": "commit", "request-changes": "fix-loop"}),
        Phase(id="fix-loop", description=""),
        Phase(id="commit", description=""),
    ]

    assert runner._next_index(0, runner.phases[0]) == (1, False)

    (run_dir / "final-review.md").write_text("verdict: request-changes\n", encoding="utf-8")
    assert runner._next_index(0, runner.phases[0]) == (0, True)


def test_review_fail_marker_overrides_approve_verdict(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import Phase, Runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "final-review.md").write_text(
        "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
        "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n"
        "## Overall\nverdict: approve\n\n"
        "## Completion Gate\n"
        "dependency-rule: fail\n",
        encoding="utf-8",
    )

    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.phases = [
        Phase(id="final-review", description="", multi_review=True, routes={"approve": "commit", "request-changes": "fix-loop"}),
        Phase(id="fix-loop", description=""),
        Phase(id="commit", description=""),
    ]

    assert runner._next_index(0, runner.phases[0]) == (1, False)


def test_missing_required_profile_skills_marker_overrides_approve_verdict(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import Phase, Runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "final-review.md").write_text(
        "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
        "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n"
        "## Overall\nverdict: approve\n\n"
        "## Completion Gate\n"
        "missing-required-profile-skills: missing local profile: ios-clean-architecture\n",
        encoding="utf-8",
    )

    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.phases = [
        Phase(id="final-review", description="", multi_review=True, routes={"approve": "commit", "request-changes": "fix-loop"}),
        Phase(id="fix-loop", description=""),
        Phase(id="commit", description=""),
    ]

    assert runner._next_index(0, runner.phases[0]) == (1, False)


def test_route_key_requires_exact_status_or_verdict_lines():
    from agent_flow.runner import _route_key

    assert _route_key("verdict: approved\n") == "default"
    assert _route_key("status: passed with warnings\n") == "default"
    assert _route_key("verdict: request-changes pending\n") == "default"
    assert _route_key("status: passed\n") == "default"
    assert _route_key("- status: green\n") == "default"
    assert _route_key("note: status: green\n") == "default"
    assert _route_key("  status: green\n") == "default"
    assert _route_key("- verdict: approve\n") == "default"
    assert _route_key("status: green\n") == "green"


def test_route_without_target_blocks_instead_of_falling_through(tmp_path: Path):
    from agent_flow.runner import Phase, Runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "final-review.md").write_text("status: blocked\n", encoding="utf-8")
    phase = Phase(id="final-review", description="", routes={"approve": "commit", "request-changes": "fix-loop"})
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.phases = [phase, Phase(id="fix-loop", description=""), Phase(id="commit", description="")]

    assert runner._next_index(0, phase) == (0, True)


def test_default_final_review_approve_requires_two_reviewers(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import Phase, Runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    phase = Phase(
        id="final-review",
        description="",
        multi_review=True,
        routes={"approve": "commit", "request-changes": "fix-loop"},
    )
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.phases = [phase, Phase(id="fix-loop", description=""), Phase(id="commit", description="")]

    (run_dir / "final-review.md").write_text(
        "## Reviewer 1\nreviewer-source: sub-agent\nverdict: approve\n\n"
        "## Overall\nverdict: approve\n",
        encoding="utf-8",
    )
    assert runner._next_index(0, phase) == (0, True)

    (run_dir / "final-review.md").write_text(
        "## Reviewer 1\nverdict: approve\n\n## Reviewer 2\nverdict: approve\n\n"
        "## Overall\nverdict: approve\n",
        encoding="utf-8",
    )
    assert runner._next_index(0, phase) == (0, True)


def test_required_markers_block_incomplete_artifact(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import Phase, Runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "domain-grill.md").write_text("notes only\n", encoding="utf-8")

    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    phase = Phase(
        id="domain-grill",
        description="",
        required_markers=("domain-grill: complete", "shared_understanding: reached"),
    )

    assert runner._missing_required_markers(phase) == [
        "domain-grill: complete",
        "shared_understanding: reached",
    ]

    (run_dir / "domain-grill.md").write_text(
        "TODO: add domain-grill: complete later\n"
        "shared_understanding: reached\n",
        encoding="utf-8",
    )

    assert runner._missing_required_markers(phase) == [
        "domain-grill: complete",
        "shared_understanding: reached",
    ]

    (run_dir / "domain-grill.md").write_text(
        "notes\n"
        "domain-grill: complete\n"
        "shared_understanding: reached\n",
        encoding="utf-8",
    )

    assert runner._missing_required_markers(phase) == [
        "domain-grill: complete",
        "shared_understanding: reached",
    ]

    (run_dir / "domain-grill.md").write_text(
        "notes\n"
        "```\n"
        "## Completion Gate\n"
        "domain-grill: complete\n"
        "shared_understanding: reached\n"
        "```\n",
        encoding="utf-8",
    )

    assert runner._missing_required_markers(phase) == [
        "domain-grill: complete",
        "shared_understanding: reached",
    ]

    (run_dir / "domain-grill.md").write_text(
        "notes\n"
        "## Completion Gate\n"
        "```\n"
        "domain-grill: complete\n"
        "shared_understanding: reached\n"
        "```\n",
        encoding="utf-8",
    )

    assert runner._missing_required_markers(phase) == [
        "domain-grill: complete",
        "shared_understanding: reached",
    ]

    (run_dir / "domain-grill.md").write_text(
        "notes\n"
        "    ## Completion Gate\n"
        "    domain-grill: complete\n"
        "    shared_understanding: reached\n",
        encoding="utf-8",
    )

    assert runner._missing_required_markers(phase) == [
        "domain-grill: complete",
        "shared_understanding: reached",
    ]

    (run_dir / "domain-grill.md").write_text(
        "notes\n"
        "## Completion Gate\n"
        "domain-grill: complete\n"
        "shared_understanding: reached\n",
        encoding="utf-8",
    )

    assert runner._missing_required_markers(phase) == []

    # 체크리스트로 작성한 Completion Gate도 동일한 마커로 인정한다.
    (run_dir / "domain-grill.md").write_text(
        "notes\n"
        "## Completion Gate\n"
        "- [x] domain-grill: complete\n"
        "* shared_understanding: reached\n",
        encoding="utf-8",
    )

    assert runner._missing_required_markers(phase) == []

    # diff에서 복사한 추가 줄도 Completion Gate 마커로 인정한다.
    (run_dir / "domain-grill.md").write_text(
        "notes\n"
        "## Completion Gate\n"
        "+ domain-grill: complete\n"
        "+ shared_understanding: reached\n",
        encoding="utf-8",
    )

    assert runner._missing_required_markers(phase) == []

    phase = Phase(
        id="domain-grill",
        description="",
        required_markers=("context_docs_updated: true|not_needed",),
    )
    (run_dir / "domain-grill.md").write_text("## Completion Gate\ncontext_docs_updated:\n", encoding="utf-8")
    assert runner._missing_required_markers(phase) == ["context_docs_updated: true|not_needed"]
    (run_dir / "domain-grill.md").write_text("## Completion Gate\ncontext_docs_updated: maybe\n", encoding="utf-8")
    assert runner._missing_required_markers(phase) == ["context_docs_updated: true|not_needed"]
    (run_dir / "domain-grill.md").write_text("## Completion Gate\ncontext_docs_updated: not_needed\n", encoding="utf-8")
    assert runner._missing_required_markers(phase) == []

    phase = Phase(
        id="domain-grill",
        description="",
        required_markers=("## Clean Architecture Boundary Map", "dependency-rule: pass|fail"),
    )
    (run_dir / "domain-grill.md").write_text(
        "## Clean Architecture Boundary Map\n"
        "notes\n"
        "## Completion Gate\n"
        "dependency-rule: pass\n",
        encoding="utf-8",
    )
    assert runner._missing_required_markers(phase) == []

    (run_dir / "domain-grill.md").write_text(
        "```\n"
        "## Clean Architecture Boundary Map\n"
        "```\n"
        "## Completion Gate\n"
        "dependency-rule: pass\n",
        encoding="utf-8",
    )
    assert runner._missing_required_markers(phase) == ["## Clean Architecture Boundary Map"]

    phase = Phase(
        id="domain-grill",
        description="",
        required_markers=("profile-skill-selection: applied|skipped",),
    )
    (run_dir / "domain-grill.md").write_text(
        "## Completion Gate\n"
        "profile-skill-selection: missing\n",
        encoding="utf-8",
    )
    assert runner._missing_required_markers(phase) == ["profile-skill-selection: applied|skipped"]
    (run_dir / "domain-grill.md").write_text(
        "## Completion Gate\n"
        "profile-skill-selection: skipped\n",
        encoding="utf-8",
    )
    assert runner._missing_required_markers(phase) == []


def test_runner_uses_normalized_artifact_path(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import Runner

    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    project.mkdir()
    run_dir.mkdir()
    runner = Runner(project, run_dir=run_dir, workflow="full-feature")
    phase = next(phase for phase in runner.phases if phase.id == "domain-grill")
    legacy_artifact = run_dir / "domain-grill.md"
    legacy_artifact.write_text("legacy root artifact\n", encoding="utf-8")
    runner._adapter_name = "codex"

    assert phase.artifact == "artifacts/domain-grill.md"
    assert not runner._has_artifact(phase)

    artifact = run_dir / phase.artifact
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        "## Completion Gate\n"
        "domain-grill: complete\n"
        "shared_understanding: reached\n"
        "context_docs_checked: true\n"
        "context_docs_updated: not_needed\n",
        encoding="utf-8",
    )

    assert runner._has_artifact(phase)
    assert runner._missing_required_markers(phase) == []


def test_status_uses_normalized_artifact_path(tmp_path: Path, capsys):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.artifact import ActiveRun, write_meta

    run_dir = tmp_path / "project" / ".agent-flow" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    write_meta(
        run_dir,
        {
            "workflow": "full-feature",
            "task": "demo",
            "current_phase": "domain-grill",
            "started_at": "2026-05-20T00:00:00+00:00",
        },
    )

    ActiveRun(
        path=run_dir,
        run_id="r1",
        workflow="full-feature",
        task="demo",
        started_at="2026-05-20T00:00:00+00:00",
    ).print_status()

    output = capsys.readouterr().out
    assert "required_artifact:" in output
    assert "artifacts/domain-grill.md" in output


def test_full_feature_status_ignores_legacy_root_artifact(tmp_path: Path, capsys):
    from agent_flow.artifact import ActiveRun, has_artifact, write_meta

    run_dir = tmp_path / "project" / ".agent-flow" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    write_meta(
        run_dir,
        {
            "workflow": "full-feature",
            "task": "demo",
            "current_phase": "domain-grill",
            "started_at": "2026-05-20T00:00:00+00:00",
        },
    )
    (run_dir / "domain-grill.md").write_text("legacy root artifact\n", encoding="utf-8")
    assert not has_artifact(run_dir, "domain-grill")
    active = ActiveRun(
        path=run_dir,
        run_id="r1",
        workflow="full-feature",
        task="demo",
        started_at="2026-05-20T00:00:00+00:00",
    )

    active.print_status()

    output = capsys.readouterr().out
    assert "reason: missing_phase_artifact" in output
    assert "artifacts/domain-grill.md" in output

    canonical = run_dir / "artifacts" / "domain-grill.md"
    canonical.parent.mkdir()
    canonical.write_text(
        "## Completion Gate\n"
        "domain-grill: complete\n"
        "shared_understanding: reached\n"
        "context_docs_checked: true\n"
        "context_docs_updated: not_needed\n",
        encoding="utf-8",
    )
    assert has_artifact(run_dir, "domain-grill")
    active.print_status()

    output = capsys.readouterr().out
    assert "reason: phase_artifact_written_advance_required" in output


def test_runner_rejects_future_artifact_written_before_phase_transition(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    import agent_flow.runner as runner_module
    from agent_flow.artifact import read_meta
    from agent_flow.runner import Phase, ResumeMode, Runner

    class PrewritingAdapter:
        name = "prewriting"

        def execute(self, phase, run_dir: Path, project_root: Path) -> bool:
            artifact = run_dir / phase.artifact
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(f"# {phase.id}\n", encoding="utf-8")
            if phase.id == "first":
                future = run_dir / "artifacts" / "second.md"
                future.write_text("# second, written early\n", encoding="utf-8")
                os.utime(future, (150, 150))
            return True

    entered_at = iter(
        (
            "1970-01-01T00:01:40+00:00",
            "1970-01-01T00:03:20+00:00",
        )
    )
    monkeypatch.setattr(runner_module, "detect_adapter", lambda: PrewritingAdapter())
    monkeypatch.setattr(runner_module, "detect_available_clis", lambda: [])
    monkeypatch.setattr(runner_module, "_utc_now_iso", lambda: next(entered_at))
    project = tmp_path / "project"
    project.mkdir()
    runner = Runner(project, workflow="default")
    runner.phases = [
        Phase(id="first", description="", artifact="artifacts/first.md"),
        Phase(id="second", description="", artifact="artifacts/second.md"),
    ]

    runner.run(ResumeMode.START, "prewrite regression")

    assert runner.run_dir is not None
    meta = read_meta(runner.run_dir)
    assert meta["current_phase"] == "second"
    assert meta["phase_entered_at"] == "1970-01-01T00:03:20+00:00"
    assert "reason: stale_artifact" in capsys.readouterr().out
    assert (runner.run_dir / "active").exists()


def test_render_angle_result_marks_claude_rate_limit_as_blocker(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from datetime import datetime, timezone

    from agent_flow.multi_review import _render_angle_result
    from agent_flow.subprocess_pool import SubprocessResult

    result = SubprocessResult(
        job_id="claude-generalist",
        stderr="You've hit your limit. Usage limit resets at 2:40pm.",
        returncode=1,
    )

    artifact = _render_angle_result(result)

    assert "status: blocked" in artifact
    assert "reason: reviewer_rate_limited" in artifact
    assert "reviewer: claude" in artifact
    assert "retry_after:" in artifact
    assert "next_command: agent-flow-python review retry --reviewer claude --retry-after " in artifact
    assert '"reason": "reviewer_rate_limited"' in artifact
    retry_after = next(line for line in artifact.splitlines() if line.startswith("retry_after: "))
    parsed = datetime.fromisoformat(retry_after.removeprefix("retry_after: "))
    assert parsed > datetime.now(timezone.utc)


@pytest.mark.parametrize(
    ("job_id", "stderr", "reviewer"),
    [
        ("codex-generalist", "429 too many requests; rate limit resets in 5 minutes", "codex"),
    ],
)
def test_render_angle_result_marks_provider_rate_limits_as_blockers(
    job_id: str,
    stderr: str,
    reviewer: str,
):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.multi_review import _render_angle_result
    from agent_flow.subprocess_pool import SubprocessResult

    result = SubprocessResult(job_id=job_id, stderr=stderr, returncode=1)

    artifact = _render_angle_result(result)

    assert "status: blocked" in artifact
    assert "reason: reviewer_rate_limited" in artifact
    assert f"reviewer: {reviewer}" in artifact
    assert f"next_command: agent-flow-python review retry --reviewer {reviewer} --retry-after " in artifact


def test_generic_stub_does_not_write_completion_markers(tmp_path: Path, monkeypatch):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.adapters.generic import GenericAdapter
    from agent_flow.runner import Phase

    run_dir = tmp_path / "run"
    project_root = tmp_path / "project"
    run_dir.mkdir()
    project_root.mkdir()
    phase = Phase(
        id="domain-grill",
        description="",
        required_markers=("domain-grill: complete", "shared_understanding: reached"),
    )
    monkeypatch.setenv("AGENT_FLOW_GENERIC_MODE", "stub")

    assert GenericAdapter().execute(phase, run_dir=run_dir, project_root=project_root)
    artifact = run_dir / "domain-grill.md"
    text = artifact.read_text(encoding="utf-8")
    assert "domain-grill: complete" not in text
    assert "shared_understanding: reached" not in text


def test_backward_route_invalidates_target_artifact(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import Phase, Runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    watch = run_dir / "artifacts" / "pr-watch.md"
    watch.parent.mkdir()
    watch.write_text("status: comments\n", encoding="utf-8")
    (run_dir / "artifacts" / "pr-comment-fix.md").write_text("fixed\n", encoding="utf-8")

    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.phases = [
        Phase(id="pr-watch", description="", routes={"comments": "pr-comment-fix"}, artifact="artifacts/pr-watch.md"),
        Phase(id="pr-comment-fix", description="", routes={"default": "pr-watch"}, artifact="artifacts/pr-comment-fix.md"),
    ]

    assert runner._next_index(1, runner.phases[1]) == (0, False)
    assert not watch.exists()
    assert not (run_dir / "artifacts" / "pr-comment-fix.md").exists()


def test_backward_route_invalidates_intermediate_fresh_artifacts(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import Phase, Runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    phases = [
        Phase(id="refactor", description=""),
        Phase(id="gates", description=""),
        Phase(id="multi-review", description=""),
        Phase(id="architecture-review", description="", routes={"blocked": "refactor"}),
    ]
    for phase in phases:
        (run_dir / f"{phase.id}.md").write_text(
            "verdict: blocked\n" if phase.id == "architecture-review" else "stale\n",
            encoding="utf-8",
        )

    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.phases = phases

    assert runner._next_index(3, phases[3]) == (0, False)
    for phase in phases:
        assert not (run_dir / f"{phase.id}.md").exists()


def test_python_runner_does_not_add_yaml_external_automatic_artifacts(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import Phase, Runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir

    for phase in (
        Phase(id="pr-watch", description=""),
        Phase(id="architecture-review", description=""),
    ):
        assert runner._write_automatic_artifact(phase) is False
        assert runner._artifact_needs_auto_revalidation(phase) is False
        assert not (run_dir / f"{phase.id}.md").exists()


def test_abort_yes_flag_skips_prompt(tmp_path: Path):
    """`agent-flow abort --yes` must not block on confirmation."""
    project = tmp_path / "abort_yes"
    project.mkdir()
    _init_git_project(project)
    r1 = _run_cli(["run", "any task"], project)
    assert r1.returncode == 0
    r2 = _run_cli(["abort", "--yes", "--worktree", "any task"], project)
    assert r2.returncode == 0
    assert "aborted" in r2.stdout.lower()


def test_run_safe_command_times_out_without_hanging(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.core.commands import run_safe_command

    result = run_safe_command(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        cwd=tmp_path,
        timeout_s=1,
    )

    assert result.returncode is None
    assert result.timed_out is True
    assert result.ok is False


def test_worktree_git_commands_use_longer_timeout(tmp_path: Path, monkeypatch):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.core import worktrees
    from agent_flow.core.commands import SafeCommandResult

    captured: dict[str, int] = {}

    def fake_run_safe_command(args, *, cwd=None, input_text=None, timeout_s=0):
        captured["timeout_s"] = timeout_s
        return SafeCommandResult(args=tuple(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worktrees, "run_safe_command", fake_run_safe_command)

    worktrees._run_git(tmp_path, "worktree", "add", "path", "branch")

    assert captured["timeout_s"] == worktrees.GIT_WORKTREE_TIMEOUT_S
    assert captured["timeout_s"] > 30


def test_cli_detection_runs():
    """Smoke check that detection runs and returns plausible CLIs."""
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.cli_detect import detect_available_clis
    clis = detect_available_clis()
    assert isinstance(clis, list)
    for c in clis:
        assert c.name in {"claude", "codex", "omp"}


def test_multi_review_jobs_include_mandatory_baseline(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.adapters.hosted import HostedAdapter, _reviewer_jobs
    from agent_flow.runner import Phase

    adapter = HostedAdapter("codex")
    adapter._profile_snapshot = {"review_angles": []}
    phase = Phase(id="final-review", description="", multi_review=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    jobs = _reviewer_jobs(phase, run_dir, KIT_ROOT, adapter)
    assert [job.angle_id for job in jobs] == ["generalist", "architecture-design", "clean-architecture"]
    assert "Review Angle" in jobs[0].prompt
    assert "Architecture Design" in jobs[1].prompt
    assert "Clean Architecture" in jobs[2].prompt


def test_multi_review_jobs_dedupe_profile_baseline(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.adapters.hosted import HostedAdapter, _reviewer_jobs
    from agent_flow.runner import Phase

    adapter = HostedAdapter("codex")
    adapter._profile_snapshot = {
        "review_angles": [
            {
                "id": "architecture-design",
                "prompt": "templates/_shared/review/architecture-design.md",
            },
            {
                "id": "compose-stability",
                "prompt": "templates/_shared/review/compose-stability.md",
            },
        ]
    }
    phase = Phase(id="final-review", description="", multi_review=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    jobs = _reviewer_jobs(phase, run_dir, KIT_ROOT, adapter)
    assert [job.angle_id for job in jobs] == [
        "generalist",
        "architecture-design",
        "clean-architecture",
        "compose-stability",
    ]


def test_multi_review_profile_can_override_baseline_prompt(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.adapters.hosted import HostedAdapter, _reviewer_jobs
    from agent_flow.runner import Phase

    project = tmp_path / "project"
    project.mkdir()
    prompt_path = project / "templates" / "_shared" / "review" / "custom-generalist.md"
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_text("custom prompt body\n", encoding="utf-8")
    adapter = HostedAdapter("codex")
    adapter._profile_snapshot = {
        "review_angles": [
            {"id": "generalist", "prompt": "templates/_shared/review/custom-generalist.md"},
        ]
    }
    phase = Phase(id="final-review", description="", multi_review=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    jobs = _reviewer_jobs(phase, run_dir, project, adapter)
    assert [job.angle_id for job in jobs] == ["generalist", "architecture-design", "clean-architecture"]
    assert "custom prompt body" in jobs[0].prompt


def test_multi_review_missing_prompt_file_fails_loudly(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.adapters.hosted import HostedAdapter, _reviewer_jobs
    from agent_flow.runner import Phase

    adapter = HostedAdapter("codex")
    adapter._profile_snapshot = {
        "review_angles": [
            {"id": "missing", "prompt": "templates/_shared/review/missing-review-angle.md"},
        ]
    }
    phase = Phase(id="final-review", description="", multi_review=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(FileNotFoundError, match="review angle prompt not found"):
        _reviewer_jobs(phase, run_dir, tmp_path, adapter)


def test_multi_review_empty_prompt_file_fails_loudly(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.adapters.hosted import HostedAdapter, _reviewer_jobs
    from agent_flow.runner import Phase

    adapter = HostedAdapter("codex")
    adapter._profile_snapshot = {
        "review_angles": [
            {"id": "empty", "prompt": ""},
        ]
    }
    phase = Phase(id="final-review", description="", multi_review=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(ValueError, match="review angle prompt is required"):
        _reviewer_jobs(phase, run_dir, tmp_path, adapter)


def test_multi_review_rejects_escaped_prompt_path(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.adapters.hosted import HostedAdapter, _reviewer_jobs
    from agent_flow.runner import Phase

    adapter = HostedAdapter("codex")
    adapter._profile_snapshot = {
        "review_angles": [
            {"id": "escaped", "prompt": "../../../etc/passwd"},
        ]
    }
    phase = Phase(id="final-review", description="", multi_review=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(ValueError, match="invalid review angle prompt path"):
        _reviewer_jobs(phase, run_dir, tmp_path, adapter)


def test_multi_review_rejects_escaped_angle_id_before_output_creation(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.adapters.hosted import HostedAdapter, _reviewer_jobs
    from agent_flow.runner import Phase

    adapter = HostedAdapter("omp")
    adapter._profile_snapshot = {
        "review_angles": [
            {
                "id": "x/../../../../escaped",
                "prompt": "templates/_shared/review/architecture.md",
            },
        ]
    }
    phase = Phase(id="final-review", description="", multi_review=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(ValueError, match="invalid review angle name"):
        _reviewer_jobs(phase, run_dir, tmp_path, adapter)
    assert not (tmp_path / "escaped.md").exists()


def test_multi_review_rejects_case_colliding_angle_ids(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.adapters.hosted import HostedAdapter, _reviewer_jobs
    from agent_flow.runner import Phase

    adapter = HostedAdapter("claude")
    adapter._profile_snapshot = {
        "review_angles": [
            {
                "id": "Generalist",
                "prompt": "templates/_shared/review/architecture.md",
            },
        ]
    }
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(ValueError, match="collide on case-insensitive filesystems"):
        _reviewer_jobs(
            Phase(id="final-review", description="", multi_review=True),
            run_dir,
            tmp_path,
            adapter,
        )


def test_multi_review_rejects_nested_prompt_prefix(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.adapters.hosted import HostedAdapter, _reviewer_jobs
    from agent_flow.runner import Phase

    adapter = HostedAdapter("codex")
    adapter._profile_snapshot = {
        "review_angles": [
            {"id": "nested", "prompt": "foo/templates/_shared/review/x.md"},
        ]
    }
    phase = Phase(id="final-review", description="", multi_review=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(ValueError, match="invalid review angle prompt path"):
        _reviewer_jobs(phase, run_dir, tmp_path, adapter)


def test_multi_review_packaged_prompt_survives_project_templates_dir(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.adapters.hosted import HostedAdapter, _reviewer_jobs
    from agent_flow.runner import Phase

    project = tmp_path / "project"
    (project / "templates").mkdir(parents=True)
    adapter = HostedAdapter("codex")
    adapter._profile_snapshot = {"review_angles": []}
    phase = Phase(id="final-review", description="", multi_review=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    jobs = _reviewer_jobs(phase, run_dir, project, adapter)
    assert [job.angle_id for job in jobs] == ["generalist", "architecture-design", "clean-architecture"]
    assert "Review Angle" in jobs[0].prompt


def test_packaged_profile_review_prompts_exist():
    for profile_path in (KIT_ROOT / "profiles").glob("*.yaml"):
        if profile_path.name.startswith("_"):
            continue
        text = profile_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "prompt:" not in line:
                continue
            prompt_path = line.split("prompt:", 1)[1].strip()
            assert (KIT_ROOT / prompt_path).is_file(), f"missing prompt: {profile_path.name} {prompt_path}"


def test_stage_result_updates_run_report(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.core.artifacts import write_stage_result

    run_dir = tmp_path / "run"
    write_stage_result(
        run_dir=run_dir,
        stage_id="review",
        status="completed",
        evidence_type="verified",
        confidence="high",
        content="verdict: approve\n",
    )

    report = run_dir / "RUN_REPORT.md"
    assert report.is_file()
    text = report.read_text(encoding="utf-8")
    assert "`review`" in text
    assert "status=completed" in text
    assert "verdict=approve" in text
    assert "evidence=verified" in text
    assert "confidence=high" in text


def test_report_command_regenerates_latest_run_report(tmp_path: Path):
    project = tmp_path / "report_project"
    project.mkdir()
    run_dir = project / ".agent-flow" / "runs" / "development" / "r1"
    artifact_dir = run_dir / "artifacts"
    artifact_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        '{"run_id":"r1","workflow_id":"development","task":"demo","status":"running"}',
        encoding="utf-8",
    )
    (artifact_dir / "review.md").write_text(
        "# Stage Result: review\n\n- Status: blocked\n- Evidence Type: inferred\n- Confidence: low\n",
        encoding="utf-8",
    )

    result = _run_cli(["report"], project)
    assert result.returncode == 0, result.stderr
    assert "RUN_REPORT.md" in result.stdout
    text = (run_dir / "RUN_REPORT.md").read_text(encoding="utf-8")
    assert "Blocked: 1" in text
    assert "evidence=inferred" in text


def test_query_and_explain_commands_search_latest_run(tmp_path: Path):
    project = tmp_path / "query_project"
    project.mkdir()
    run_dir = project / ".agent-flow" / "runs" / "development" / "r1"
    artifact_dir = run_dir / "artifacts"
    artifact_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        '{"run_id":"r1","workflow_id":"development","task":"ddd refactor"}',
        encoding="utf-8",
    )
    (artifact_dir / "architecture-review.md").write_text(
        "# Stage Result: architecture-review\n\n"
        "- Status: blocked\n"
        "- Evidence Type: observed\n"
        "- Confidence: high\n\n"
        "verdict: blocked\n"
        "missing Implementation Structure\n",
        encoding="utf-8",
    )

    query = _run_cli(["query", "blocked", "--limit", "1"], project)
    assert query.returncode == 0, query.stderr
    assert "architecture-review.md" in query.stdout
    assert "blocked" in query.stdout

    explain = _run_cli(["explain", "Implementation Structure"], project)
    assert explain.returncode == 0, explain.stderr
    assert "# Run Explanation" in explain.stdout
    assert "Artifact States" in explain.stdout
    assert "architecture-review" in explain.stdout


def test_query_ignores_generated_run_report(tmp_path: Path):
    project = tmp_path / "query_report_project"
    project.mkdir()
    run_dir = project / ".agent-flow" / "runs" / "development" / "r1"
    artifact_dir = run_dir / "artifacts"
    artifact_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        '{"run_id":"r1","workflow_id":"development","task":"blocked"}',
        encoding="utf-8",
    )
    (run_dir / "RUN_REPORT.md").write_text("blocked blocked blocked\n", encoding="utf-8")
    (artifact_dir / "review.md").write_text("blocked\n", encoding="utf-8")

    query = _run_cli(["query", "blocked", "--limit", "2"], project)
    assert query.returncode == 0, query.stderr
    assert "artifacts/review.md" in query.stdout
    assert "RUN_REPORT.md" not in query.stdout
    assert "manifest.json" not in query.stdout


def test_report_includes_review_summary_and_dedupes_structured_artifact(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.core.report import write_run_report

    run_dir = tmp_path / "run"
    artifact_dir = run_dir / "artifacts"
    artifact_dir.mkdir(parents=True)
    (run_dir / "review.md").write_text("status: completed\n", encoding="utf-8")
    (artifact_dir / "review.md").write_text(
        "# Stage Result: review\n\n- Status: blocked\n",
        encoding="utf-8",
    )
    (run_dir / "review-summary.json").write_text(
        '{"verdict":"NEEDS_CHANGES","findings":["fix it"]}',
        encoding="utf-8",
    )

    write_run_report(run_dir)
    text = (run_dir / "RUN_REPORT.md").read_text(encoding="utf-8")
    assert "Blocked: 1" in text
    assert "Review Summary" in text
    assert "Findings: 1" in text
    assert text.count("`review`") == 1


def test_watch_command_writes_latest_run_snapshot(tmp_path: Path):
    project = tmp_path / "watch_project"
    project.mkdir()
    run_dir = project / ".agent-flow" / "runs" / "development" / "r1"
    artifact_dir = run_dir / "artifacts"
    artifact_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        '{"run_id":"r1","workflow_id":"development","task":"watch"}',
        encoding="utf-8",
    )
    (artifact_dir / "review.md").write_text("status: pending\n", encoding="utf-8")

    result = _run_cli(["watch"], project)
    assert result.returncode == 0, result.stderr
    assert "watch.json" in result.stdout
    payload = json.loads((run_dir / "watch.json").read_text(encoding="utf-8"))
    assert payload["artifact_count"] == 1
    assert payload["blocked"] == []
    assert payload["pending"] == ["review.md"]


def test_watch_dedupes_structured_artifacts(tmp_path: Path):
    project = tmp_path / "watch_dedupe_project"
    project.mkdir()
    run_dir = project / ".agent-flow" / "runs" / "development" / "r1"
    artifact_dir = run_dir / "artifacts"
    artifact_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        '{"run_id":"r1","workflow_id":"development","task":"watch"}',
        encoding="utf-8",
    )
    (run_dir / "review.md").write_text("status: blocked\n", encoding="utf-8")
    (artifact_dir / "review.md").write_text(
        "# Stage Result: review\n\n- Status: completed\n",
        encoding="utf-8",
    )

    result = _run_cli(["watch"], project)
    assert result.returncode == 0, result.stderr
    payload = json.loads((run_dir / "watch.json").read_text(encoding="utf-8"))
    assert payload["artifact_count"] == 1
    assert payload["blocked"] == []


def test_watch_uses_newest_state_metadata(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.core.watch import write_watch_snapshot

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    artifact = run_dir / "review.md"
    manifest = run_dir / "manifest.json"
    meta = run_dir / "meta.json"
    artifact.write_text("status: completed\n", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    meta.write_text("{}", encoding="utf-8")
    now = time.time()
    os.utime(manifest, (now - 20, now - 20))
    os.utime(artifact, (now - 10, now - 10))
    os.utime(meta, (now, now))

    write_watch_snapshot(run_dir)
    payload = json.loads((run_dir / "watch.json").read_text(encoding="utf-8"))
    assert payload["needs_continue"] is False


def test_run_report_ignores_unreadable_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.core.report import write_run_report

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    manifest = run_dir / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    original_read_text = Path.read_text

    def fail_manifest(path: Path, *args, **kwargs):
        if path == manifest:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_manifest)
    assert write_run_report(run_dir).is_file()


def test_run_dir_commands_reject_missing_run_dir(tmp_path: Path):
    project = tmp_path / "missing_run_dir_project"
    project.mkdir()

    result = _run_cli(["report", "--run-dir", ".agent-flow/runs/missing"], project)
    assert result.returncode == 1
    assert "run dir not found" in result.stderr


def test_run_dir_commands_report_no_runs_on_stderr(tmp_path: Path):
    project = tmp_path / "no_runs_project"
    project.mkdir()

    result = _run_cli(["query", "anything"], project)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "no runs" in result.stderr


def test_latest_run_uses_file_activity_not_directory_mtime(tmp_path: Path):
    project = tmp_path / "latest_project"
    project.mkdir()
    old_run = project / ".agent-flow" / "runs" / "development" / "old"
    new_run = project / ".agent-flow" / "runs" / "development" / "new"
    old_run.mkdir(parents=True)
    new_run.mkdir(parents=True)
    old_artifact = old_run / "artifact.md"
    old_artifact.write_text("stale evidence\n", encoding="utf-8")
    (old_run / "manifest.json").write_text('{"run_id":"old"}', encoding="utf-8")
    (new_run / "manifest.json").write_text('{"run_id":"new"}', encoding="utf-8")
    now = time.time()
    os.utime(old_run, (now - 20, now - 20))
    os.utime(new_run, (now - 10, now - 10))
    time.sleep(0.01)
    old_artifact.write_text("latest evidence\n", encoding="utf-8")

    result = _run_cli(["query", "latest"], project)
    assert result.returncode == 0, result.stderr
    assert "artifact.md" in result.stdout


def test_security_guards_reject_unsafe_names_and_escaped_paths(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.core.security import ensure_child_path, validate_safe_name

    root = tmp_path / "root"
    root.mkdir()

    assert validate_safe_name("default-workflow_1", "workflow") == "default-workflow_1"
    with pytest.raises(ValueError, match="invalid workflow name"):
        validate_safe_name("../workflow", "workflow")
    with pytest.raises(ValueError, match="profile path escapes"):
        ensure_child_path(root, root / "nested" / "profile.yaml", "profile")


def test_python_runner_unset_ledger_mode_creates_no_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_flow.artifact import read_meta
    from agent_flow.runner import ResumeMode, Runner

    project = tmp_path / "ledger-unset"
    project.mkdir()
    monkeypatch.delenv("AGENT_FLOW_LEDGER_MODE", raising=False)
    monkeypatch.setenv("AGENT_FLOW_ADAPTER", "generic")
    monkeypatch.setenv("AGENT_FLOW_GENERIC_MODE", "emit")

    runner = Runner(project, workflow="default")
    runner.run(ResumeMode.START, task="no ledger experiment")

    meta = read_meta(runner.run_dir)
    assert meta["ledger_mode"] == "artifacts-only"
    assert meta["experiment_enabled"] is False
    assert meta["ledger_experiment"] == {
        "experiment_id": None,
        "model_id": None,
        "tool_permissions_sha256": None,
        "system_prompt_sha256": None,
        "caps_sha256": None,
        "provider_max_retries": None,
        "provider_retry_policy_sha256": None,
        "pricing_snapshot": None,
        "provider_attestation_key_id": None,
        "provider_attestation_public_key": None,
    }
    assert not (runner.run_dir / "artifacts" / "execution-ledger").exists()


def test_python_explicit_pilot_initialization_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_flow.artifact import find_active_run
    from agent_flow.runner import ResumeMode, Runner

    project = tmp_path / "invalid-ledger-pilot"
    project.mkdir()
    monkeypatch.setenv("AGENT_FLOW_LEDGER_MODE", "ledger-selective")
    monkeypatch.setenv("AGENT_FLOW_EXPERIMENT_TOOL_PERMISSIONS_SHA256", "invalid")

    runner = Runner(project, workflow="default")
    with pytest.raises(RuntimeError, match="execution ledger pilot initialization failed"):
        runner.run(ResumeMode.START, task="invalid pilot")

    assert runner.run_dir is None
    assert find_active_run(project) is None


def test_python_initial_prompt_observation_failure_leaves_no_active_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_flow.artifact import find_active_run
    from agent_flow.runner import ResumeMode, Runner

    project = tmp_path / "initial-observation-failure"
    project.mkdir()
    monkeypatch.setenv("AGENT_FLOW_ADAPTER", "generic")
    monkeypatch.setenv("AGENT_FLOW_GENERIC_MODE", "emit")
    monkeypatch.setenv("AGENT_FLOW_LEDGER_MODE", "ledger-always")
    monkeypatch.setenv("AGENT_FLOW_LEDGER_FAULT_AFTER", "target")

    runner = Runner(project, workflow="default")
    with pytest.raises(
        RuntimeError,
        match="execution ledger pilot prompt observation failed",
    ):
        runner.run(ResumeMode.START, task="initial observation failure")

    assert runner.run_dir is None
    assert find_active_run(project) is None


def test_python_observation_failure_leaves_canonical_transition_uncommitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import agent_flow.runner as runner_module
    from agent_flow.artifact import read_meta, write_meta
    from agent_flow.runner import ResumeMode, Runner

    project = tmp_path / "committed-ledger-prompt"
    project.mkdir()
    monkeypatch.setenv("AGENT_FLOW_ADAPTER", "generic")
    monkeypatch.setenv("AGENT_FLOW_GENERIC_MODE", "emit")
    monkeypatch.setenv("AGENT_FLOW_LEDGER_MODE", "ledger-always")

    runner = Runner(project, workflow="default")
    runner.run(ResumeMode.START, task="committed prompt only")
    capsys.readouterr()
    assert runner.run_dir is not None
    gates = next(phase for phase in runner.phases if phase.id == "gates")
    gate_artifact = runner.run_dir / gates.artifact
    gate_artifact.parent.mkdir(parents=True, exist_ok=True)
    gate_artifact.write_text(
        json.dumps(
            {
                "passed": False,
                "status": "request-changes",
                "results": [
                    {
                        "gate_id": "tests",
                        "command": "python3 -m pytest",
                        "argv": ["python3", "-m", "pytest"],
                        "required": True,
                        "passed": False,
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": "committed observation failure",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    gates_index = next(index for index, phase in enumerate(runner.phases) if phase.id == "gates")
    meta = read_meta(runner.run_dir)
    meta.update(
        {
            "current_phase": "gates",
            "phase_index": gates_index,
            "fix_loop_rounds": 0,
        }
    )
    write_meta(runner.run_dir, meta)
    ledger_root = runner.run_dir / "artifacts" / "execution-ledger"
    injection_path = ledger_root / "injections.jsonl"
    capture_path = ledger_root / "captures.jsonl"
    before = len(injection_path.read_text(encoding="utf-8").splitlines())
    original_observe = runner_module.observe_execution_state_injection

    def fail_fix_loop_observation(**kwargs: object) -> dict[str, object]:
        phase = kwargs.get("phase")
        if getattr(phase, "id", None) == "fix-loop":
            return {"ok": False, "error": "forced observation failure"}
        return original_observe(**kwargs)

    monkeypatch.setattr(
        runner_module,
        "observe_execution_state_injection",
        fail_fix_loop_observation,
    )

    resumed = Runner(project, workflow="default", run_dir=runner.run_dir)
    with pytest.raises(
        RuntimeError,
        match="execution ledger pilot prompt observation failed",
    ):
        resumed.run(ResumeMode.RESUME)
    failed_meta = read_meta(runner.run_dir)
    assert failed_meta["current_phase"] == "gates"
    assert failed_meta["phase_revision"] == 0
    assert len(injection_path.read_text(encoding="utf-8").splitlines()) == before
    assert gate_artifact.exists()
    assert (runner.run_dir / "transition-pending.json").is_file()
    assert len(capture_path.read_text(encoding="utf-8").splitlines()) == 1
    pending = json.loads(
        (runner.run_dir / "transition-pending.json").read_text(encoding="utf-8")
    )
    assert pending["capture"]["committed"] is True
    monkeypatch.setattr(
        runner_module,
        "observe_execution_state_injection",
        original_observe,
    )

    retried = Runner(project, workflow="default", run_dir=runner.run_dir)
    retried.run(ResumeMode.RESUME)
    output = capsys.readouterr().out
    assert "## Execution ledger" in output
    assert len(injection_path.read_text(encoding="utf-8").splitlines()) == before + 1
    assert len(capture_path.read_text(encoding="utf-8").splitlines()) == 1
    assert not (runner.run_dir / "transition-pending.json").exists()
    retried_meta = read_meta(runner.run_dir)
    assert retried_meta["current_phase"] == "fix-loop"
    assert retried_meta["phase_revision"] == 1


def test_python_runner_recovers_state_write_failure_without_phantom_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import agent_flow.runner as runner_module
    from agent_flow.artifact import read_meta, write_meta
    from agent_flow.runner import ResumeMode, Runner

    project = tmp_path / "pending-transition-state-failure"
    project.mkdir()
    monkeypatch.setenv("AGENT_FLOW_ADAPTER", "generic")
    monkeypatch.setenv("AGENT_FLOW_GENERIC_MODE", "emit")
    monkeypatch.setenv("AGENT_FLOW_LEDGER_MODE", "ledger-always")

    runner = Runner(project, workflow="default")
    runner.run(ResumeMode.START, task="recover pending transition")
    capsys.readouterr()
    assert runner.run_dir is not None
    gates = next(phase for phase in runner.phases if phase.id == "gates")
    gates_index = runner.phases.index(gates)
    gate_artifact = runner.run_dir / gates.artifact
    gate_artifact.parent.mkdir(parents=True, exist_ok=True)
    gate_artifact.write_text(
        json.dumps(
            {
                "passed": False,
                "status": "request-changes",
                "results": [
                    {
                        "gate_id": "tests",
                        "argv": ["python3", "-m", "pytest"],
                        "required": True,
                        "passed": False,
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": "state publish failure",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    meta = read_meta(runner.run_dir)
    meta.update(
        {
            "current_phase": "gates",
            "phase_index": gates_index,
            "fix_loop_rounds": 0,
        }
    )
    write_meta(runner.run_dir, meta)
    ledger_root = runner.run_dir / "artifacts" / "execution-ledger"
    injection_path = ledger_root / "injections.jsonl"
    capture_path = ledger_root / "captures.jsonl"
    injection_count = len(injection_path.read_text(encoding="utf-8").splitlines())
    original_write_meta = runner_module.write_meta

    def fail_next_phase_write(run_dir: Path, payload: dict) -> None:
        if payload.get("current_phase") == "fix-loop":
            raise OSError("forced meta publish failure")
        original_write_meta(run_dir, payload)

    resumed = Runner(project, workflow="default", run_dir=runner.run_dir)
    monkeypatch.setattr(runner_module, "write_meta", fail_next_phase_write)
    with pytest.raises(OSError, match="forced meta publish failure"):
        resumed.run(ResumeMode.RESUME)
    assert read_meta(runner.run_dir)["current_phase"] == "gates"
    assert gate_artifact.exists()
    assert (runner.run_dir / "transition-pending.json").is_file()
    assert len(injection_path.read_text(encoding="utf-8").splitlines()) == injection_count + 1
    assert len(capture_path.read_text(encoding="utf-8").splitlines()) == 1
    pending = json.loads(
        (runner.run_dir / "transition-pending.json").read_text(encoding="utf-8")
    )
    assert pending["capture"]["committed"] is True

    monkeypatch.setattr(runner_module, "write_meta", original_write_meta)
    retried = Runner(project, workflow="default", run_dir=runner.run_dir)
    retried.run(ResumeMode.RESUME)
    assert read_meta(runner.run_dir)["current_phase"] == "fix-loop"
    assert not (runner.run_dir / "transition-pending.json").exists()
    assert len(injection_path.read_text(encoding="utf-8").splitlines()) == injection_count + 1
    assert len(capture_path.read_text(encoding="utf-8").splitlines()) == 1


def test_python_transition_capture_failure_leaves_canonical_state_uncommitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agent_flow.artifact import read_meta, write_meta
    from agent_flow.runner import ResumeMode, Runner

    project = tmp_path / "capture-failure-transition"
    project.mkdir()
    monkeypatch.setenv("AGENT_FLOW_ADAPTER", "generic")
    monkeypatch.setenv("AGENT_FLOW_GENERIC_MODE", "emit")
    monkeypatch.setenv("AGENT_FLOW_LEDGER_MODE", "ledger-always")

    runner = Runner(project, workflow="default")
    runner.run(ResumeMode.START, task="capture failure retry")
    capsys.readouterr()
    assert runner.run_dir is not None
    gates = next(phase for phase in runner.phases if phase.id == "gates")
    gates_index = runner.phases.index(gates)
    gate_artifact = runner.run_dir / gates.artifact
    gate_artifact.parent.mkdir(parents=True, exist_ok=True)
    gate_artifact.write_text(
        json.dumps(
            {
                "passed": False,
                "status": "request-changes",
                "results": [
                    {
                        "gate_id": "tests",
                        "argv": ["python3", "-m", "pytest"],
                        "required": True,
                        "passed": False,
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": "capture lock failure",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    meta = read_meta(runner.run_dir)
    meta.update(
        {
            "current_phase": "gates",
            "phase_index": gates_index,
            "fix_loop_rounds": 0,
        }
    )
    write_meta(runner.run_dir, meta)
    ledger_root = runner.run_dir / "artifacts" / "execution-ledger"
    lock_path = ledger_root / "transaction.lock"
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "token": "live-capture-owner"})
        + "\n",
        encoding="utf-8",
    )
    resumed = Runner(project, workflow="default", run_dir=runner.run_dir)
    with pytest.raises(RuntimeError, match="transition capture failed"):
        resumed.run(ResumeMode.RESUME)
    failed_meta = read_meta(runner.run_dir)
    assert failed_meta["current_phase"] == "gates"
    assert failed_meta["phase_revision"] == 0
    assert gate_artifact.exists()
    pending_path = runner.run_dir / "transition-pending.json"
    assert pending_path.is_file()
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    assert pending["capture"]["committed"] is False
    assert not (ledger_root / "captures.jsonl").read_text(
        encoding="utf-8"
    ).strip()
    lock_path.unlink()

    retried = Runner(project, workflow="default", run_dir=runner.run_dir)
    retried.run(ResumeMode.RESUME)
    retried_meta = read_meta(runner.run_dir)
    assert retried_meta["current_phase"] == "fix-loop"
    assert retried_meta["phase_revision"] == 1
    captures = (ledger_root / "captures.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(captures) == 1


def test_python_pending_transition_prepares_sidecars_before_backward_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_flow.runner as runner_module
    from agent_flow.artifact import read_meta, write_meta
    from agent_flow.runner import ResumeMode, Runner, _TransitionPlan

    project = tmp_path / "backward-transition-recovery"
    project.mkdir()
    monkeypatch.setenv("AGENT_FLOW_ADAPTER", "generic")
    monkeypatch.setenv("AGENT_FLOW_GENERIC_MODE", "emit")
    monkeypatch.setenv("AGENT_FLOW_LEDGER_MODE", "ledger-always")

    runner = Runner(project, workflow="full-feature")
    runner.run(ResumeMode.START, task="recover after backward cleanup")
    assert runner.run_dir is not None
    architecture_index = next(
        index
        for index, phase in enumerate(runner.phases)
        if phase.id == "architecture-review"
    )
    refactor_index = next(
        index for index, phase in enumerate(runner.phases) if phase.id == "refactor"
    )
    architecture = runner.phases[architecture_index]
    artifact = runner.run_dir / architecture.artifact
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        "# Architecture review\n\n## Reviewer 1\n\n- Fix boundary leak.\n\n"
        "## Overall\nverdict: request-changes\n",
        encoding="utf-8",
    )
    meta = read_meta(runner.run_dir)
    meta.update(
        {
            "current_phase": architecture.id,
            "phase_index": architecture_index,
            "fix_loop_rounds": 0,
            "phase_revision": 0,
        }
    )
    write_meta(runner.run_dir, meta)
    plan = _TransitionPlan(
        current_index=architecture_index,
        next_index=refactor_index,
        prospective_fix_loop_rounds=0,
        route_key="request-changes",
        routed_to="refactor",
        capture_round=1,
        capture_fix_loop_rounds=0,
    )
    pending = runner._create_pending_transition(plan)
    runner_module._write_pending_transition(runner.run_dir, pending)
    adapter = SimpleNamespace(
        _ledger_run_id=meta["run_id"],
        _ledger_mode="ledger-always",
    )
    original_observe = runner_module.observe_execution_state_injection

    def fail_refactor_observation(**kwargs: object) -> dict[str, object]:
        phase = kwargs.get("phase")
        if getattr(phase, "id", None) == "refactor":
            return {"ok": False, "error": "forced backward observation failure"}
        return original_observe(**kwargs)

    monkeypatch.setattr(
        runner_module,
        "observe_execution_state_injection",
        fail_refactor_observation,
    )
    with pytest.raises(
        RuntimeError,
        match="execution ledger pilot prompt observation failed",
    ):
        runner._finish_pending_transition(adapter, pending)

    assert read_meta(runner.run_dir)["current_phase"] == "architecture-review"
    assert artifact.exists()
    pending_path = runner.run_dir / "transition-pending.json"
    committed_pending = json.loads(pending_path.read_text(encoding="utf-8"))
    assert committed_pending["capture"]["committed"] is True
    capture_path = runner.run_dir / "artifacts/execution-ledger/captures.jsonl"
    assert len(capture_path.read_text(encoding="utf-8").splitlines()) == 1

    monkeypatch.setattr(
        runner_module,
        "observe_execution_state_injection",
        original_observe,
    )
    original_apply_transition_artifacts = runner._apply_transition_artifacts

    def fail_route_artifacts(_plan: _TransitionPlan) -> None:
        raise OSError("forced route artifact failure")

    monkeypatch.setattr(
        runner,
        "_apply_transition_artifacts",
        fail_route_artifacts,
    )
    with pytest.raises(OSError, match="forced route artifact failure"):
        runner._recover_pending_transition(adapter)

    assert read_meta(runner.run_dir)["current_phase"] == "architecture-review"
    assert artifact.exists()
    injection_path = runner.run_dir / "artifacts/execution-ledger/injections.jsonl"
    injection_count = len(injection_path.read_text(encoding="utf-8").splitlines())
    monkeypatch.setattr(
        runner,
        "_apply_transition_artifacts",
        original_apply_transition_artifacts,
    )
    recovered = Runner(project, workflow="full-feature", run_dir=runner.run_dir)
    result = recovered._recover_pending_transition(adapter)
    assert result is not None
    assert result["current_phase"] == "refactor"
    assert not pending_path.exists()
    assert len(capture_path.read_text(encoding="utf-8").splitlines()) == 1
    assert len(injection_path.read_text(encoding="utf-8").splitlines()) == injection_count


def test_python_explicit_pilot_rejects_a_changed_live_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_flow.runner as runner_module
    from agent_flow.runner import ResumeMode, Runner

    kit = tmp_path / "kit"
    shutil.copytree(KIT_ROOT / "workflows", kit / "workflows")
    shutil.copytree(KIT_ROOT / "profiles", kit / "profiles")
    monkeypatch.setattr(runner_module, "_find_kit_root", lambda: kit)
    monkeypatch.setenv("AGENT_FLOW_ADAPTER", "generic")
    monkeypatch.setenv("AGENT_FLOW_GENERIC_MODE", "emit")
    monkeypatch.setenv("AGENT_FLOW_LEDGER_MODE", "artifacts-only")
    project = tmp_path / "workflow-pin-project"
    project.mkdir()

    runner = Runner(project, workflow="default")
    runner.run(ResumeMode.START, task="pin live workflow")
    assert runner.run_dir is not None
    workflow = kit / "workflows" / "default.yaml"
    original = workflow.read_text(encoding="utf-8")
    assert "Unified design phase" in original
    workflow.write_text(
        original.replace("Unified design phase", "Changed design phase"),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="active pilot runner workflow snapshot changed"):
        Runner(project, workflow="default", run_dir=runner.run_dir)


def test_python_status_rejects_a_changed_explicit_pilot_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_flow.artifact as artifact_module
    import agent_flow.runner as runner_module
    from agent_flow.artifact import find_active_run
    from agent_flow.runner import ResumeMode, Runner

    kit = tmp_path / "status-kit"
    shutil.copytree(KIT_ROOT / "workflows", kit / "workflows")
    shutil.copytree(KIT_ROOT / "profiles", kit / "profiles")
    monkeypatch.setattr(runner_module, "_find_kit_root", lambda: kit)
    monkeypatch.setattr(artifact_module, "find_kit_root", lambda: kit)
    monkeypatch.setenv("AGENT_FLOW_ADAPTER", "generic")
    monkeypatch.setenv("AGENT_FLOW_GENERIC_MODE", "emit")
    monkeypatch.setenv("AGENT_FLOW_LEDGER_MODE", "artifacts-only")
    project = tmp_path / "status-workflow-pin-project"
    project.mkdir()

    runner = Runner(project, workflow="default")
    runner.run(ResumeMode.START, task="pin status workflow")
    active = find_active_run(project)
    assert active is not None
    workflow = kit / "workflows" / "default.yaml"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            "Unified design phase",
            "Changed status design phase",
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="active pilot runner workflow snapshot changed"):
        active.print_status(config_root=project, workspace_root=project)


@pytest.mark.parametrize(
    "mode",
    ("artifacts-only", "ledger-always", "ledger-selective", "action-self-review"),
)
def test_python_runner_pins_explicit_ledger_mode_and_resume_ignores_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    from agent_flow.artifact import read_meta
    from agent_flow.runner import ResumeMode, Runner

    project = tmp_path / mode
    project.mkdir()
    monkeypatch.setenv("AGENT_FLOW_ADAPTER", "generic")
    monkeypatch.setenv("AGENT_FLOW_GENERIC_MODE", "emit")
    monkeypatch.setenv("AGENT_FLOW_LEDGER_MODE", mode)
    monkeypatch.setenv("AGENT_FLOW_EXPERIMENT_ID", "pilot-1")
    monkeypatch.setenv("AGENT_FLOW_EXPERIMENT_MODEL_ID", "model-1")

    runner = Runner(project, workflow="default")
    runner.run(ResumeMode.START, task="pin experiment")
    sidecar = runner.run_dir / "artifacts" / "execution-ledger"
    config_before = (sidecar / "config.json").read_bytes()
    meta_before = read_meta(runner.run_dir)
    if mode == "artifacts-only":
        injection = json.loads(
            (sidecar / "injections.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        first_phase = runner.phases[0]
        assert injection["injected"] is False
        assert injection["prompt_bytes"] == len(
            (first_phase.prompt or first_phase.description).encode("utf-8")
        )

    monkeypatch.setenv("AGENT_FLOW_LEDGER_MODE", "ledger-always" if mode != "ledger-always" else "artifacts-only")
    monkeypatch.setenv("AGENT_FLOW_EXPERIMENT_ID", "mutated")
    monkeypatch.setenv("AGENT_FLOW_EXPERIMENT_MODEL_ID", "mutated-model")
    resumed = Runner(project, workflow="default", run_dir=runner.run_dir)
    resumed.run(ResumeMode.RESUME)

    meta_after = read_meta(runner.run_dir)
    assert meta_before["ledger_mode"] == mode
    assert meta_after["ledger_mode"] == mode
    assert meta_after["ledger_experiment"] == meta_before["ledger_experiment"]
    assert (sidecar / "config.json").read_bytes() == config_before
    assert (sidecar / "ledger.json").exists() is (mode != "artifacts-only")


def test_python_ledger_workflow_hash_uses_node_normalized_phase_shape(tmp_path: Path) -> None:
    import hashlib

    from agent_flow.core.execution_state_ledger import initialize_execution_state_ledger
    from agent_flow.runner import Phase, _ledger_workflow_phases

    phases = [
        Phase(
            id="review",
            artifact="artifacts/review.md",
            description="Review changes",
            prompt=None,
            required_markers=("verdict: approve",),
            pause_after=True,
            optional=False,
            multi_review=True,
            cite_lore=True,
            routes={"approve": "complete"},
        )
    ]
    normalized = _ledger_workflow_phases(phases)
    assert normalized == [
        {
            "id": "review",
            "artifact": "artifacts/review.md",
            "description": "Review changes",
            "instruction": "Review changes",
            "required_markers": ["verdict: approve"],
            "pause_after": True,
            "optional": False,
            "multi_review": True,
            "cite_lore": True,
            "routes": {"approve": "complete"},
        }
    ]
    result = initialize_execution_state_ledger(
        run_dir=tmp_path,
        run_id="run-normalized",
        mode="artifacts-only",
        experiment_enabled=True,
        workflow_id="default",
        workflow_phases=normalized,
        run_snapshot=LEDGER_RUN_SNAPSHOT,
    )
    workflow = json.loads(
        (tmp_path / "artifacts/execution-ledger/workflow.json").read_text(
            encoding="utf-8"
        )
    )
    canonical = json.dumps(workflow, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert result["config"]["workflow_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert result["config"]["workflow_snapshot_sha256"] == result["config"]["workflow_sha256"]


def test_routing_only_capture_records_fix_loop_refactor_and_pr_watch(tmp_path: Path) -> None:
    from agent_flow.core.execution_state_ledger import (
        capture_execution_state,
        initialize_execution_state_ledger,
    )
    from agent_flow.runner import Phase

    run_dir = tmp_path / "run"
    workflow_phases = [
        {"id": "fix-loop", "routes": {"default": "refactor"}},
        {"id": "refactor", "routes": {"default": "pr-watch"}},
        {"id": "pr-watch", "routes": {"green": "commit"}},
        {"id": "commit"},
    ]
    initialize_execution_state_ledger(
        run_dir=run_dir,
        run_id="run-1",
        mode="ledger-always",
        experiment_enabled=True,
        workflow_id="default",
        workflow_phases=workflow_phases,
        run_snapshot=LEDGER_RUN_SNAPSHOT,
    )
    sidecar = run_dir / "artifacts" / "execution-ledger"
    transitions = (
        (workflow_phases[0], "default", "refactor", "1" * 64),
        (workflow_phases[1], "default", "pr-watch", "2" * 64),
        (workflow_phases[2], "green", "commit", "3" * 64),
        (workflow_phases[1], "default", "pr-watch", "4" * 64),
        (workflow_phases[2], "green", "commit", "5" * 64),
    )
    for phase, route_key, routed_to, occurrence_id in transitions:
        phase_id = phase["id"]
        artifact = run_dir / "artifacts" / f"{phase_id}.md"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(f"# {phase_id}\n", encoding="utf-8")
        result = capture_execution_state(
            run_dir=run_dir,
            run_id="run-1",
            mode="ledger-always",
            experiment_enabled=True,
            phase=Phase(
                id=phase_id,
                description="",
                artifact=str(artifact.relative_to(run_dir)),
                routes=phase["routes"],
            ),
            artifact_path=artifact,
            project_root=tmp_path,
            round=1,
            fix_loop_rounds=1,
            workflow_id="default",
            route_key=route_key,
            routed_to=routed_to,
            transition_occurrence_id=occurrence_id,
        )
        assert result["ok"] is True
        assert result["captured"] is True

    captures = [
        json.loads(line)
        for line in (sidecar / "captures.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(captures) == 5
    assert all(capture["source_provenance"] == [] for capture in captures)
    assert all(capture["entry_ids"] == [] for capture in captures)
    assert not (sidecar / "sources").exists()
    ledger = json.loads((sidecar / "ledger.json").read_text(encoding="utf-8"))
    assert ledger["entries"] == {"status": [], "knowledge": [], "procedural": []}
    metrics = json.loads((sidecar / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["canonical_routing_violations"] == 0
    assert metrics["canonical_transition_count"] == 5
    assert len(
        {
            item["transition_occurrence_id"]
            for item in metrics["routing_provenance"]
        }
    ) == 5
    assert {item["phase"] for item in metrics["routing_provenance"]} == {
        "fix-loop",
        "refactor",
        "pr-watch",
    }


def test_python_runner_captures_fix_loop_rounds_before_backward_cleanup(tmp_path: Path) -> None:
    from agent_flow.artifact import write_meta
    from agent_flow.core.execution_state_ledger import initialize_execution_state_ledger
    from agent_flow.runner import Phase, Runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_meta(
        run_dir,
        {
            "run_id": "round-run",
            "workflow": "default",
            "ledger_mode": "artifacts-only",
            "experiment_enabled": True,
        },
    )
    review = Phase(
        id="final-review",
        description="",
        artifact="artifacts/final-review.md",
        multi_review=True,
        routes={"approve": "complete", "request-changes": "fix-loop"},
    )
    fix = Phase(id="fix-loop", description="", artifact="artifacts/fix-loop.md")
    initialize_execution_state_ledger(
        run_dir=run_dir,
        run_id="round-run",
        mode="artifacts-only",
        experiment_enabled=True,
        workflow_id="default",
        workflow_phases=[
            {"id": "fix-loop", "routes": None},
            {"id": "final-review", "multi_review": True, "routes": review.routes},
        ],
        run_snapshot=LEDGER_RUN_SNAPSHOT,
    )
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.project_root = tmp_path
    runner.workflow_name = "default"
    runner.phases = [fix, review]
    artifact = run_dir / review.artifact
    artifact.parent.mkdir(parents=True, exist_ok=True)
    review_text = (
        "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: request-changes\n\n"
        "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n"
        "## Overall\nverdict: request-changes\n"
    )

    artifact.write_text(review_text, encoding="utf-8")
    assert runner._next_index(1, review) == (0, False)
    assert not artifact.exists()
    artifact.write_text(review_text, encoding="utf-8")
    assert runner._next_index(1, review) == (0, False)
    assert not artifact.exists()

    captures = [
        json.loads(line)
        for line in (run_dir / "artifacts" / "execution-ledger" / "captures.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [capture["round"] for capture in captures] == [1, 2]
    assert [capture["measurement"]["fix_loop_rounds"] for capture in captures] == [1, 2]
    assert len(
        {capture["transition_occurrence_id"] for capture in captures}
    ) == 2
    for capture in captures:
        provenance = capture["source_provenance"][0]
        summary = json.loads(
            (run_dir / provenance["archive_path"]).read_text(encoding="utf-8")
        )
        archived = run_dir / summary["artifact_archive_path"]
        assert archived.read_text(encoding="utf-8") == review_text


def test_action_self_review_block_is_bounded_and_absent_from_reviewer_prompts(
    tmp_path: Path,
) -> None:
    from agent_flow.adapters.generic import GenericAdapter
    from agent_flow.adapters.hosted import _reviewer_jobs
    from agent_flow.core.execution_state_ledger import (
        capture_execution_state,
        initialize_execution_state_ledger,
    )
    from agent_flow.runner import Phase

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    initialize_execution_state_ledger(
        run_dir=run_dir,
        run_id="action-run",
        mode="action-self-review",
        experiment_enabled=True,
        workflow_id="default",
        workflow_phases=[
            {"id": "gates", "routes": {"request-changes": "fix-loop"}},
            {"id": "fix-loop"},
            {"id": "multi-review", "multi_review": True},
        ],
        run_snapshot=LEDGER_RUN_SNAPSHOT,
    )
    gate_artifact = run_dir / "artifacts" / "gate-results.json"
    gate_artifact.parent.mkdir(parents=True, exist_ok=True)
    raw_failure = f"FAILED secret at {tmp_path}/private.py"
    gate_artifact.write_text(
        json.dumps(
            {
                "passed": False,
                "results": [
                    {
                        "gate_id": "unit",
                        "argv": ["python3", "-m", "pytest"],
                        "required": True,
                        "passed": False,
                        "exit_code": 1,
                        "stderr": raw_failure,
                        "stdout": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    gates = Phase(
        id="gates",
        description="",
        artifact="artifacts/gate-results.json",
        routes={"request-changes": "fix-loop"},
    )
    result = capture_execution_state(
        run_dir=run_dir,
        run_id="action-run",
        mode="action-self-review",
        experiment_enabled=True,
        phase=gates,
        artifact_path=gate_artifact,
        project_root=tmp_path,
        round=1,
        fix_loop_rounds=1,
        workflow_id="default",
        route_key="request-changes",
        routed_to="fix-loop",
    )
    assert result["ok"] is True

    adapter = GenericAdapter()
    adapter._config_root = tmp_path
    adapter._ledger_run_dir = run_dir
    adapter._ledger_run_id = "action-run"
    adapter._ledger_mode = "action-self-review"
    adapter._ledger_experiment_enabled = True
    fix = Phase(id="fix-loop", description="Fix", prompt="Apply a fix.")
    prompt = adapter.render_envelope(fix, run_dir, tmp_path, host_hint="HOST-SENTINEL")
    assert "## Action self-review" in prompt
    action = prompt.split("## Action self-review", 1)[1].split("## Host-specific guidance", 1)[0]
    assert "Execution ledger" not in action
    assert "action-run" not in action
    assert str(tmp_path) not in action
    assert raw_failure not in action
    assert prompt.index("## Action self-review") < prompt.index("HOST-SENTINEL")

    review = Phase(id="multi-review", description="Review", multi_review=True)
    review_prompt = adapter.render_envelope(review, run_dir, tmp_path, host_hint="HOST-SENTINEL")
    assert "Action self-review" not in review_prompt
    assert "Execution ledger" not in review_prompt
    jobs = _reviewer_jobs(review, run_dir, tmp_path, adapter)
    assert jobs
    assert all("Action self-review" not in job.prompt for job in jobs)
    assert all("Execution ledger" not in job.prompt for job in jobs)


def test_experiment_record_usage_cli_is_idempotent(tmp_path: Path) -> None:
    from agent_flow.core.execution_state_ledger import initialize_execution_state_ledger

    project = tmp_path / "usage-project"
    run_dir = project / ".agent-flow" / "runs" / "default" / "usage-run"
    run_dir.mkdir(parents=True)
    initialize_execution_state_ledger(
        run_dir=run_dir,
        run_id="usage-run",
        mode="artifacts-only",
        experiment_enabled=True,
        workflow_id="default",
        workflow_phases=[{"id": "fix-loop"}],
        experiment={"model_id": "model-1"},
        run_snapshot=LEDGER_RUN_SNAPSHOT,
    )
    args = [
        "experiment",
        "record-usage",
        "--run-dir",
        ".agent-flow/runs/default/usage-run",
        "--event-id",
        "phase-1",
        "--generated-at",
        "2026-07-11T00:00:00Z",
        "--scope",
        "phase",
        "--phase-id",
        "fix-loop",
        "--round",
        "1",
        "--model-id",
        "model-1",
        "--input-tokens",
        "100",
        "--output-tokens",
        "20",
        "--additional-tokens",
        "4",
        "--latency-ms",
        "50",
        "--estimated-cost-usd",
        "0.0100",
    ]

    first = _run_cli(args, project)
    second = _run_cli(args, project)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert json.loads(first.stdout)["recorded"] is True
    assert json.loads(second.stdout)["recorded"] is False
    usage = run_dir / "artifacts" / "execution-ledger" / "usage.jsonl"
    assert len(usage.read_text(encoding="utf-8").splitlines()) == 1


def test_python_status_output_is_independent_of_ledger_sidecar(
    tmp_path: Path,
) -> None:
    project = tmp_path / "status-project"
    project.mkdir()
    _init_git_project(project)
    started = _run_cli(
        ["run", "status contract"],
        project,
        env_extra={
            "AGENT_FLOW_GENERIC_MODE": "emit",
            "AGENT_FLOW_LEDGER_MODE": "artifacts-only",
        },
    )
    assert started.returncode == 0, started.stderr
    runs_root = (
        _worktree_runtime_root(project, "feat-status-contract")
        / ".agent-flow"
        / "runs"
    )
    run_dir = next(runs_root.iterdir())

    sidecar = run_dir / "artifacts" / "execution-ledger"
    with_sidecar = _run_cli(["status"], project)
    hidden = project / "ledger-sidecar-hidden"
    sidecar.rename(hidden)
    without_sidecar = _run_cli(["status"], project)
    hidden.rename(sidecar)

    assert with_sidecar.returncode == without_sidecar.returncode == 0
    assert with_sidecar.stdout == without_sidecar.stdout
    assert with_sidecar.stderr == without_sidecar.stderr
