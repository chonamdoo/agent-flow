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

import pytest


KIT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = KIT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def _run_cli(args: list[str], cwd: Path, env_extra: dict | None = None):
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("AGENT_FLOW_") or key in {
            "CLAUDECODE",
            "CLAUDE_CLI",
            "CODEX_CLI",
        }:
            env.pop(key, None)
    env["PYTHONPATH"] = str(KIT_ROOT / "src")
    env["AGENT_FLOW_ADAPTER"] = "generic"
    env["AGENT_FLOW_GENERIC_MODE"] = "stub-success"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "agent_flow.cli", *args],
        cwd=cwd, env=env, capture_output=True, text=True,
    )


def _init_git_project(project: Path) -> None:
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=project, check=True, capture_output=True, text=True
    )
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


def test_full_cycle(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()

    r1 = _run_cli(["run", "test feature"], project)
    assert r1.returncode == 0, r1.stderr
    assert "run started" in r1.stdout

    runs_dir = project / ".agent-flow" / "runs"
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

    r2 = _run_cli(["continue"], project)
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
    (kit / "kit.json").write_text(
        json.dumps({"profile": "android", "profiles": ["android", "react-native"]}),
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

    result = _run_cli(
        ["run", "test feature"],
        project,
        env_extra={"AGENT_FLOW_GENERIC_MODE": "stub"},
    )

    assert result.returncode == 0, result.stderr
    assert "generic_stub_artifact" in result.stdout
    runs = list((project / ".agent-flow" / "runs").iterdir())
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

    # Start the first run with stub mode → it'll loop and eventually pause.
    r1 = _run_cli(["run", "first task"], project, env_extra={
        # Force pause early-ish: design phase is first; smoke tests use stub
        # which writes artifacts inline, so we'll have an active run after
        # pause at slice-plan.
    })
    assert r1.returncode == 0

    # Active marker should exist (paused).
    runs_dir = project / ".agent-flow" / "runs"
    actives = [p for p in runs_dir.iterdir() if (p / "active").exists()]
    assert len(actives) == 1

    # Try starting another run — must fail with exit code 2.
    r2 = _run_cli(["run", "second task"], project)
    assert r2.returncode == 2
    assert "already active" in r2.stdout.lower() or "already active" in r2.stderr.lower()


def test_abort_clears_marker(tmp_path: Path):
    project = tmp_path / "abort"
    project.mkdir()

    r1 = _run_cli(["run", "to be aborted"], project)
    assert r1.returncode == 0

    runs_dir = project / ".agent-flow" / "runs"
    active = next(p for p in runs_dir.iterdir() if (p / "active").exists())
    assert active.exists()

    r2 = _run_cli(["abort", "--yes"], project)
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

    r_continue = _run_cli(["continue", "--worktree", "long-press"], project)
    assert r_continue.returncode == 0, r_continue.stderr
    assert "run complete" in r_continue.stdout
    assert (run_dir / "artifacts" / "gate-results.json").exists()

    r_empty_continue = _run_cli(["continue", "--worktree", "long-press"], project)
    assert r_empty_continue.returncode == 0
    assert '--worktree "feat-long-press"' in r_empty_continue.stdout

    r2 = _run_cli(["run", "abort me", "--worktree", "long-press"], project)
    assert r2.returncode == 0, r2.stderr
    active = next(p for p in (runtime_root / ".agent-flow" / "runs").iterdir() if (p / "active").exists())
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
    assert "worktree runs require a git repository" in r1.stderr

    r2 = _run_cli(["start", "development", "--task", "task", "--worktree", "task"], project)
    assert r2.returncode == 2
    assert "worktree runs require a git repository" in r2.stderr


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
    r_continue = _run_cli(["continue", "--worktree", "task"], project)
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

    r1 = _run_cli(["run", "ok task"], project)
    assert r1.returncode == 0

    runs_dir = project / ".agent-flow" / "runs"
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


def test_overall_review_verdict_ignores_code_fences_and_body_prose():
    from agent_flow.core.phase_workflow import overall_review_route_key

    assert overall_review_route_key(
        "```markdown\n## Overall\nverdict: approve\n```\n"
    ) == "default"
    assert overall_review_route_key(
        "## Overall\nA suggested example is verdict: approve.\n"
    ) == "default"
    assert overall_review_route_key(
        "```markdown\n## Overall\nverdict: request-changes\n```\n"
        "## Overall\nverdict: approve\n"
    ) == "approve"

    from agent_flow.runner import _multi_review_route_key

    assert _multi_review_route_key(
        "```markdown\n"
        "## Reviewer 1\nreviewer-source: sub-agent\nverdict: approve\n"
        "## Reviewer 2\nreviewer-source: sub-agent\nverdict: approve\n"
        "```\n"
        "## Overall\nverdict: approve\n"
    ) == "missing-reviewer"


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
        "## Reviewer 1\nreviewer-1 verdict: approve\n\nverdict: approve\n",
        encoding="utf-8",
    )
    assert runner._next_index(0, phase) == (0, True)

    (run_dir / "final-review.md").write_text(
        "## Reviewer 1\nverdict: approve\n\n## Reviewer 2\nverdict: approve\n\nverdict: approve\n",
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
    runner.config_root = tmp_path
    runner.project_root = tmp_path
    runner.profile = {}
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
    runner._adapter_name = "codex"

    assert phase.artifact == "artifacts/domain-grill.md"
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
    assert "next_command: agent-flow review retry --reviewer claude --retry-after " in artifact
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
    assert f"next_command: agent-flow review retry --reviewer {reviewer} --retry-after " in artifact


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


def test_generic_stub_success_source_phase_emits_task_backed_spec_item(
    tmp_path: Path,
    monkeypatch,
):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.adapters.generic import GenericAdapter
    from agent_flow.runner import Phase

    run_dir = tmp_path / "run"
    project_root = tmp_path / "project"
    run_dir.mkdir()
    project_root.mkdir()
    (run_dir / "meta.json").write_text(
        json.dumps({"task": "Show empty search results."}),
        encoding="utf-8",
    )
    phase = Phase(id="design", description="")
    monkeypatch.setenv("AGENT_FLOW_GENERIC_MODE", "stub-success")

    assert GenericAdapter().execute(
        phase,
        run_dir=run_dir,
        project_root=project_root,
    )
    text = (run_dir / "design.md").read_text(encoding="utf-8")
    assert "SPEC-1: Show empty search results." in text
    assert "verify: manual" in text
    assert "spec-items: SPEC-1" in text


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


def test_non_git_pr_phases_are_skipped(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import Phase, Runner

    project = tmp_path / "plain"
    project.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    runner = Runner.__new__(Runner)
    runner.project_root = project
    runner.run_dir = run_dir
    runner.architecture = "default"

    phase = Phase(id="pr-watch", description="")
    assert runner._write_automatic_artifact(phase) is True
    assert "status: skipped" in (run_dir / "pr-watch.md").read_text(encoding="utf-8")


def test_ddd_architecture_review_blocks_incomplete_design_artifact(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import Phase, Runner

    project = tmp_path / "ios_or_python_project"
    project.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ddd-design.md").write_text(
        "# ddd-design\n\n"
        "Bounded Context: Market data\n"
        "Service layer: services/*\n",
        encoding="utf-8",
    )

    runner = Runner.__new__(Runner)
    runner.project_root = project
    runner.run_dir = run_dir
    runner.architecture = "ddd"
    runner.phases = [
        Phase(
            id="architecture-review",
            description="",
            routes={"approve": "commit", "request-changes": "refactor", "blocked": "block"},
        ),
        Phase(id="commit", description=""),
    ]

    phase = runner.phases[0]
    assert runner._write_automatic_artifact(phase) is True
    text = (run_dir / "architecture-review.md").read_text(encoding="utf-8")
    assert "verdict: blocked" in text
    assert "`aggregate`" in text
    assert "`domain flow`" in text
    assert runner._next_index(0, phase) == (0, True)


def test_ddd_architecture_review_rechecks_stale_blocked_artifact(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import Phase, Runner

    project = tmp_path / "project"
    project.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    artifact = run_dir / "architecture-review.md"
    artifact.write_text("verdict: blocked\n", encoding="utf-8")
    (run_dir / "ddd-design.md").write_text(
        "# ddd-design\n\n"
        "## Bounded Context\n"
        "## Ubiquitous Language\n"
        "## Aggregates\n"
        "## Entities\n"
        "## Value Objects\n"
        "## Domain Events\n"
        "## Domain Invariants\n"
        "## Domain Flow\n",
        encoding="utf-8",
    )

    runner = Runner.__new__(Runner)
    runner.project_root = project
    runner.run_dir = run_dir
    runner.architecture = "ddd"
    phase = Phase(id="architecture-review", description="")

    assert runner._artifact_needs_auto_revalidation(phase) is True
    artifact.unlink()
    assert runner._write_automatic_artifact(phase) is False


def test_ddd_architecture_review_rejects_service_layer_refactor_bypass(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import Phase, Runner

    project = tmp_path / "project"
    project.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ddd-design.md").write_text(
        "# ddd-design\n\n## service-layer refactor\n",
        encoding="utf-8",
    )

    runner = Runner.__new__(Runner)
    runner.project_root = project
    runner.run_dir = run_dir
    runner.architecture = "ddd"
    phase = Phase(id="architecture-review", description="")

    assert runner._write_automatic_artifact(phase) is True
    text = (run_dir / "architecture-review.md").read_text(encoding="utf-8")
    assert "ddd mode cannot be service-layer refactor" in text


def test_ddd_design_validation_ignores_body_paragraph_labels(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import _missing_ddd_design_terms

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ddd-design.md").write_text(
        "# ddd-design\n\n"
        "This paragraph mentions Bounded Context: Market data, Aggregates: Trade,\n"
        "Ubiquitous Language: trade desk, Entities: Position, Value Objects: Price,\n"
        "Domain Events: Trade Imported, Domain Invariants: balanced position,\n"
        "and Domain Flow: import trades.\n"
        "It also says this is not a service-layer refactor.\n",
        encoding="utf-8",
    )

    missing = _missing_ddd_design_terms(run_dir)

    assert "bounded context" in missing
    assert "domain flow" in missing
    assert "ddd mode cannot be service-layer refactor" not in missing


def test_ddd_design_validation_accepts_markdown_heading_and_list_labels(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import _missing_ddd_design_terms

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ddd-design.md").write_text(
        "# ddd-design\n\n"
        "## Bounded Context\n"
        "## Ubiquitous Language\n"
        "- Aggregates: Trade Journal\n"
        "- Entities: Entry\n"
        "- Value Objects: Money\n"
        "- Domain Events: Trade Imported\n"
        "- Domain Invariants: Entry amount is non-zero\n"
        "- Domain Flow: Import creates journal entries\n",
        encoding="utf-8",
    )

    assert _missing_ddd_design_terms(run_dir) == []


def test_abort_yes_flag_skips_prompt(tmp_path: Path):
    """`agent-flow abort --yes` must not block on confirmation."""
    project = tmp_path / "abort_yes"
    project.mkdir()
    r1 = _run_cli(["run", "any task"], project)
    assert r1.returncode == 0
    r2 = _run_cli(["abort", "--yes"], project)
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
    from agent_flow.core import worktree_isolation
    from agent_flow.core.commands import SafeCommandResult

    captured: dict[str, int] = {}

    def fake_run_safe_command(args, *, cwd=None, input_text=None, timeout_s=0, env=None):
        captured["timeout_s"] = timeout_s
        return SafeCommandResult(args=tuple(args), returncode=0, stdout="", stderr="")

    # git calls route through worktree_isolation.git_safe, which strips leaky env.
    monkeypatch.setattr(worktree_isolation, "run_safe_command", fake_run_safe_command)

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


def test_blocked_route_keeps_phase_entered_at(tmp_path: Path):
    """불변: route가 막혀 제자리에 멈추는 것은 phase 진입이 아니다.

    여기서 시각을 밀면 방금 쓴 artifact가 진입 시각보다 과거가 되어 다음
    실행이 진짜 사유(route_blocked) 대신 stale_artifact를 보고한다.
    """
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import Phase, Runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "pr-watch.md").write_text("status: pending\n", encoding="utf-8")

    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.phases = [Phase(id="pr-watch", description="", routes={"pending": "block"})]

    phase_index, blocked = runner._next_index(0, runner.phases[0])
    assert blocked is True

    meta = {"current_phase": "pr-watch", "phase_entered_at": "2026-01-01T00:00:00+00:00"}
    runner._advance_phase(meta, phase_index, blocked)
    assert meta["phase_entered_at"] == "2026-01-01T00:00:00+00:00"


def test_self_loop_route_refreshes_phase_entered_at(tmp_path: Path):
    """불변: 같은 phase로 되도는 것은 새 라운드다. 지난 라운드 읽음 기록을 물려받지 않는다."""
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import Phase, Runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()

    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.phases = [Phase(id="review", description="")]

    meta = {"current_phase": "review", "phase_entered_at": "2026-01-01T00:00:00+00:00"}
    runner._advance_phase(meta, 0, False)
    assert meta["phase_entered_at"] != "2026-01-01T00:00:00+00:00"


def test_phase_change_always_refreshes_phase_entered_at(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import Phase, Runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()

    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.phases = [Phase(id="a", description=""), Phase(id="b", description="")]

    meta = {"current_phase": "a", "phase_entered_at": "2026-01-01T00:00:00+00:00"}
    runner._advance_phase(meta, 1, True)
    assert meta["current_phase"] == "b"
    assert meta["phase_entered_at"] != "2026-01-01T00:00:00+00:00"
