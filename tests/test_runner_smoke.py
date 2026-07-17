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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


KIT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = KIT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import agent_flow.artifact as artifact
from agent_flow.artifact import create_run


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


def test_create_run_reports_success_when_only_lock_release_is_tampered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "lock-release"
    project.mkdir()

    def fail_release(_lock) -> None:
        raise artifact.ActiveRunExists("lock ownership changed")

    monkeypatch.setattr(artifact, "_release_active_run_lock", fail_release)

    run_dir = create_run(project, "default", "task", run_id="created")

    assert (run_dir / "active").is_file()
    assert "run was created but its start lock could not be released" in capsys.readouterr().err


def _authorize_worktree_cleanup(
    project: Path,
    name: str,
    *,
    session_id: str = "cleanup-session",
) -> dict[str, str]:
    runtime = _worktree_runtime_root(project, name)
    manifest = json.loads((runtime / "manifest.json").read_text(encoding="utf-8"))
    run_dir = runtime / ".agent-flow" / "runs" / f"cleanup-{session_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "status": "complete",
                "completed_at": f"2026-07-16T12:00:{len(session_id):02d}+00:00",
                "workspace": manifest["identity"],
                "execution": {
                    "host": "codex",
                    "session_id": session_id,
                    "agent_id": "",
                },
            }
        ),
        encoding="utf-8",
    )
    return {
        "AGENT_FLOW_ACTIVE_HOST": "codex",
        "AGENT_FLOW_EXECUTION_ID": session_id,
    }


def _write_authenticated_stale_manifest(project: Path, name: str) -> None:
    runtime_manifest = _worktree_runtime_root(project, name) / "manifest.json"
    stale_manifest = project / ".agent-flow" / "worktrees" / name / "manifest.json"
    stale_manifest.write_bytes(runtime_manifest.read_bytes())


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


def test_phase_profile_projection_and_completion_markers_have_bounded_context(tmp_path: Path):
    from agent_flow.adapters.generic import GenericAdapter
    from agent_flow.core.profiles import load_profile_payload
    from agent_flow.runner import Phase

    project = tmp_path / "context-budget"
    project.mkdir()
    adapter = GenericAdapter()
    adapter._profile_id = "android"
    adapter._profile_snapshot = load_profile_payload("android")
    marker = "architecture-contract-check: pass|fail|n/a"
    body = f"Use the architecture contract.\n{marker}\n"
    prompt = adapter.render_envelope(
        Phase(
            id="architecture-review",
            description="Review",
            prompt=body,
            required_markers=(marker,),
        ),
        project / ".agent-flow" / "runs" / "r1",
        project,
    )

    assert "android_skills:" not in prompt
    assert "chrisbanes_skills:" not in prompt
    assert "review_angles:" in prompt
    assert prompt.count(marker) == 1
    assert len(prompt) < 5_000


def test_reviewer_jobs_use_compact_packets_instead_of_repeating_phase_envelope(tmp_path: Path):
    from agent_flow.adapters.hosted import HostedAdapter, _reviewer_jobs
    from agent_flow.core.profiles import load_profile_payload
    from agent_flow.runner import Phase

    project = tmp_path / "review-budget"
    project.mkdir()
    adapter = HostedAdapter("codex")
    adapter._profile_id = "android"
    adapter._profile_snapshot = load_profile_payload("android")
    adapter._task_scope = "x" * 20_000
    unique_body = "FULL_PHASE_BODY_MUST_NOT_REPEAT " * 500
    phase = Phase(id="final-review", description="Review", prompt=unique_body, multi_review=True)
    run_dir = project / ".agent-flow" / "runs" / "r1"

    jobs = _reviewer_jobs(phase, run_dir, project, adapter)
    full_envelope = adapter.render_envelope(phase, run_dir, project)

    assert len(jobs) >= 3
    assert all("FULL_PHASE_BODY_MUST_NOT_REPEAT" not in job.prompt for job in jobs)
    assert all("do not rerun test suites" in job.prompt for job in jobs)
    assert max(len(job.prompt) for job in jobs) < 8_000
    assert all("x" * 1_000 not in job.prompt for job in jobs)
    assert sum(len(job.prompt) for job in jobs) < len(full_envelope) * len(jobs) // 2


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


def test_create_run_recovers_authenticated_lock_after_process_crash(tmp_path: Path):
    project = tmp_path / "crashed-start"
    runs_root = project / ".agent-flow" / "runs"
    lock_root = runs_root / "active.lock"
    lock_root.mkdir(parents=True)
    (lock_root / "owner.json").write_text(
        json.dumps(
            {
                "version": 1,
                "pid": 99_999_999,
                "process_start_id": "crashed-process",
                "token": "stale-owner-token",
                "run_id": "crashed-run",
                "runs_root": str(runs_root.resolve()),
                "acquired_at": "2026-07-16T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    run_dir = create_run(project, "default", "recovered", run_id="recovered-run")

    assert run_dir.name == "recovered-run"
    assert (run_dir / "active").is_file()
    assert not lock_root.exists()


def test_create_run_recovers_old_empty_legacy_lock_without_active_run(tmp_path: Path):
    project = tmp_path / "empty-legacy-lock"
    runs_root = project / ".agent-flow" / "runs"
    lock_root = runs_root / "active.lock"
    lock_root.mkdir(parents=True)
    stale_time = time.time() - artifact.LEGACY_EMPTY_LOCK_GRACE_SECONDS - 1
    os.utime(lock_root, (stale_time, stale_time))

    run_dir = create_run(project, "default", "recovered", run_id="legacy-recovered")

    assert run_dir.name == "legacy-recovered"
    assert (run_dir / "active").is_file()
    assert not lock_root.exists()


def test_create_run_does_not_steal_fresh_empty_legacy_lock(tmp_path: Path):
    project = tmp_path / "fresh-legacy-lock"
    lock_root = project / ".agent-flow" / "runs" / "active.lock"
    lock_root.mkdir(parents=True)

    with pytest.raises(artifact.ActiveRunExists, match="ownership is live"):
        create_run(project, "default", "blocked", run_id="blocked-run")

    assert lock_root.is_dir()


def test_create_run_publishes_complete_lock_before_it_becomes_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project = tmp_path / "atomic-start-lock"
    project.mkdir()
    observed: list[dict[str, object]] = []
    release = artifact._release_active_run_lock

    def inspect_and_release(lock) -> None:
        metadata = lock.path.lstat()
        owner = json.loads(lock.path.read_text(encoding="utf-8"))
        assert lock.path.is_file()
        assert not lock.path.is_symlink()
        assert metadata.st_nlink == 1
        assert owner == lock.owner
        observed.append(owner)
        release(lock)

    monkeypatch.setattr(artifact, "_release_active_run_lock", inspect_and_release)

    run_dir = create_run(project, "default", "atomic", run_id="atomic-run")

    assert run_dir.name == "atomic-run"
    assert observed[0]["run_id"] == "atomic-run"


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

    execution_env = {
        "AGENT_FLOW_ACTIVE_HOST": "codex",
        "AGENT_FLOW_EXECUTION_ID": "worktree-cycle",
    }
    r1 = _run_cli(
        ["run", "worktree task", "--worktree", "Long Press"],
        project,
        execution_env,
    )
    assert r1.returncode == 0, r1.stderr
    assert "worktree: feat-long-press" in r1.stdout

    worktree = project / ".agent-flow" / "worktrees" / "feat-long-press"
    runtime_root = _worktree_runtime_root(project, "feat-long-press")
    run_dir = next((runtime_root / ".agent-flow" / "runs").iterdir())
    assert (run_dir / "active").exists()
    assert not (worktree / ".agent-flow").exists()
    assert not (worktree / "manifest.json").exists()

    r_status = _run_cli(["status", "--worktree", "Long Press"], project, execution_env)
    assert r_status.returncode == 0
    assert "worktree task" in r_status.stdout

    r_continue = _run_cli(["continue", "--worktree", "long-press"], project, execution_env)
    assert r_continue.returncode == 0, r_continue.stderr
    assert "run complete" in r_continue.stdout
    assert (run_dir / "artifacts" / "gate-results.json").exists()

    r_empty_continue = _run_cli(
        ["continue", "--worktree", "long-press"], project, execution_env
    )
    assert r_empty_continue.returncode == 0
    assert '--worktree "feat-long-press"' in r_empty_continue.stdout

    r2 = _run_cli(["run", "abort me", "--worktree", "long-press"], project, execution_env)
    assert r2.returncode == 0, r2.stderr
    active = next(p for p in (runtime_root / ".agent-flow" / "runs").iterdir() if (p / "active").exists())
    r_abort = _run_cli(
        ["abort", "--worktree", "long-press", "--yes"], project, execution_env
    )
    assert r_abort.returncode == 0
    assert not (active / "active").exists()

    r_list = _run_cli(["worktree", "list"], project)
    assert r_list.returncode == 0
    assert "feat-long-press" in r_list.stdout

    r_remove = _run_cli(
        ["worktree", "remove", "--name", "long-press"], project, execution_env
    )
    assert r_remove.returncode == 0, r_remove.stderr
    assert not worktree.exists()


def test_latest_completed_run_generation_owns_worktree_cleanup(tmp_path: Path):
    project = tmp_path / "finalizer-generation"
    project.mkdir()
    _init_git_project(project)
    first_env = {
        "AGENT_FLOW_ACTIVE_HOST": "codex",
        "AGENT_FLOW_EXECUTION_ID": "first-owner",
    }
    second_env = {
        "AGENT_FLOW_ACTIVE_HOST": "codex",
        "AGENT_FLOW_EXECUTION_ID": "second-owner",
    }
    first = _run_cli(["run", "first", "--worktree", "task"], project, first_env)
    assert first.returncode == 0, first.stderr
    first_complete = _run_cli(
        ["continue", "--worktree", "task"], project, first_env
    )
    assert first_complete.returncode == 0, first_complete.stderr
    second = _run_cli(["run", "second", "--worktree", "task"], project, second_env)
    assert second.returncode == 0, second.stderr
    second_complete = _run_cli(
        ["continue", "--worktree", "task"], project, second_env
    )
    assert second_complete.returncode == 0, second_complete.stderr

    old_owner = _run_cli(
        ["worktree", "remove", "--name", "task"], project, first_env
    )
    current_owner = _run_cli(
        ["worktree", "remove", "--name", "task"], project, second_env
    )

    assert old_owner.returncode == 2
    assert "not the current cleanup owner" in old_owner.stderr
    assert current_owner.returncode == 0, current_owner.stderr


def test_worktree_runtime_cleanup_preserves_unowned_root_file(tmp_path: Path) -> None:
    from agent_flow.core.worktrees import _remove_owned_worktree_runtime
    from agent_flow.core.workspace_boundary import WorkspaceBoundaryError

    runtime = tmp_path / "feat-task"
    runtime.mkdir()
    (runtime / "manifest.json").write_text("{}\n", encoding="utf-8")
    unrelated = runtime / "unrelated.txt"
    unrelated.write_text("keep\n", encoding="utf-8")

    with pytest.raises(WorkspaceBoundaryError, match="unowned files"):
        _remove_owned_worktree_runtime(runtime)

    assert unrelated.read_text(encoding="utf-8") == "keep\n"


def test_worktree_runtime_cleanup_removes_canonical_state_layout(tmp_path: Path) -> None:
    from agent_flow.core.worktrees import _remove_owned_worktree_runtime

    runtime = tmp_path / "feat-task"
    for name in ("handoffs", "runs", "state", "team"):
        directory = runtime / ".agent-flow" / name
        directory.mkdir(parents=True)
        (directory / "owned.json").write_text("{}\n", encoding="utf-8")
    (runtime / "manifest.json").write_text("{}\n", encoding="utf-8")

    _remove_owned_worktree_runtime(runtime)

    assert not runtime.exists()


def test_worktree_runtime_cleanup_preserves_unowned_state_directory(tmp_path: Path) -> None:
    from agent_flow.core.worktrees import _remove_owned_worktree_runtime
    from agent_flow.core.workspace_boundary import WorkspaceBoundaryError

    runtime = tmp_path / "feat-task"
    unexpected = runtime / ".agent-flow" / "unexpected"
    unexpected.mkdir(parents=True)
    (unexpected / "keep.txt").write_text("keep\n", encoding="utf-8")
    (runtime / "manifest.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(WorkspaceBoundaryError, match="unowned state"):
        _remove_owned_worktree_runtime(runtime)

    assert (runtime / ".agent-flow" / "unexpected" / "keep.txt").read_text(
        encoding="utf-8"
    ) == "keep\n"


def test_worktree_runtime_cleanup_preserves_concurrent_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_flow.core.worktrees import _remove_owned_worktree_runtime

    runtime = tmp_path / "feat-task"
    runtime.mkdir()
    (runtime / "manifest.json").write_text("{}\n", encoding="utf-8")
    original_rmdir = Path.rmdir

    def racing_rmdir(path: Path) -> None:
        if path.name.startswith(".feat-task.remove-"):
            (path / "late-user-file.txt").write_text("keep\n", encoding="utf-8")
        original_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", racing_rmdir)

    with pytest.raises(OSError):
        _remove_owned_worktree_runtime(runtime)

    assert (runtime / "late-user-file.txt").read_text(encoding="utf-8") == "keep\n"


def test_worktree_runtime_cleanup_never_adopts_file_created_after_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_flow.core.worktrees as worktrees

    runtime = tmp_path / "feat-task"
    run_dir = runtime / ".agent-flow" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (runtime / "manifest.json").write_text("{}\n", encoding="utf-8")
    (run_dir / "meta.json").write_text("{}\n", encoding="utf-8")
    original_validate = worktrees._validate_worktree_runtime_layout

    def racing_validate(path: Path) -> dict[str, tuple[int, int, int]]:
        snapshot = original_validate(path)
        (path / ".agent-flow" / "runs" / "run-1" / "late-user-file.txt").write_text(
            "keep\n",
            encoding="utf-8",
        )
        return snapshot

    monkeypatch.setattr(worktrees, "_validate_worktree_runtime_layout", racing_validate)

    with pytest.raises(OSError):
        worktrees._remove_owned_worktree_runtime(runtime)

    assert (
        runtime / ".agent-flow" / "runs" / "run-1" / "late-user-file.txt"
    ).read_text(encoding="utf-8") == "keep\n"


def test_worktree_runtime_cleanup_preserves_replaced_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_flow.core.worktrees as worktrees

    runtime = tmp_path / "feat-task"
    runtime.mkdir()
    (runtime / "manifest.json").write_text("{}\n", encoding="utf-8")
    original_rename = Path.rename
    raced = False

    def racing_rename(path: Path, target: Path) -> Path:
        nonlocal raced
        if path == runtime and not raced:
            raced = True
            original_rename(path, tmp_path / "original-runtime")
            path.mkdir()
            (path / "attacker.txt").write_text("keep\n", encoding="utf-8")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", racing_rename)

    with pytest.raises(worktrees.WorkspaceBoundaryError, match="changed before removal"):
        worktrees._remove_owned_worktree_runtime(runtime)

    assert (runtime / "attacker.txt").read_text(encoding="utf-8") == "keep\n"


def test_worktree_runtime_cleanup_preserves_replaced_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_flow.core.worktrees as worktrees

    runtime = tmp_path / "feat-task"
    runs = runtime / ".agent-flow" / "runs"
    runs.mkdir(parents=True)
    (runs / "owned.json").write_text("{}\n", encoding="utf-8")
    (runtime / "manifest.json").write_text("{}\n", encoding="utf-8")
    original_rename = Path.rename
    raced = False

    def racing_rename(path: Path, target: Path) -> Path:
        nonlocal raced
        if path.name == "runs" and target.name.startswith(".runs.remove-") and not raced:
            raced = True
            original_rename(path, path.with_name("original-runs"))
            path.mkdir()
            (path / "attacker.txt").write_text("keep\n", encoding="utf-8")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", racing_rename)

    with pytest.raises(worktrees.WorkspaceBoundaryError, match="entry changed before removal"):
        worktrees._remove_owned_worktree_runtime(runtime)

    assert (runtime / ".agent-flow" / "runs" / "attacker.txt").read_text(
        encoding="utf-8"
    ) == "keep\n"


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


def test_worktree_remove_refuses_legacy_only_stale_manifest_and_preserves_branch(tmp_path: Path):
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
    assert r_remove.returncode == 2
    assert "without ownership metadata" in r_remove.stderr
    assert stale_dir.exists()
    assert _branch_exists(project, "feat/ghost")


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
    assert r_remove.returncode == 2
    assert stale_dir.exists()
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
    assert r_remove.returncode == 2
    assert stale_dir.exists()
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
    assert r_remove.returncode == 2
    assert stale_dir.exists()
    assert victim_dir.exists()
    assert (victim_dir / ".git").exists()


def test_worktree_remove_preserves_unowned_stale_path_file(tmp_path: Path):
    project = tmp_path / "stale-path-file"
    project.mkdir()
    _init_git_project(project)
    worktrees_root = project / ".agent-flow" / "worktrees"
    worktrees_root.mkdir(parents=True)
    stale_file = worktrees_root / "feat-ghost"
    stale_file.write_text("not a directory\n", encoding="utf-8")

    r_remove = _run_cli(["worktree", "remove", "--name", "ghost"], project)
    assert r_remove.returncode == 2
    assert "refusing to delete unowned stale worktree path" in r_remove.stderr
    assert stale_file.read_text(encoding="utf-8") == "not a directory\n"


def test_worktree_remove_preserves_unowned_files_in_stale_checkout(tmp_path: Path):
    project = tmp_path / "stale-user-files"
    project.mkdir()
    _init_git_project(project)
    stale_dir = project / ".agent-flow" / "worktrees" / "feat-ghost"
    stale_dir.mkdir(parents=True)
    (stale_dir / "manifest.json").write_text(
        json.dumps({"name": "feat-ghost", "branch": "feat/ghost"}),
        encoding="utf-8",
    )
    runtime = _worktree_runtime_root(project, "feat-ghost")
    runtime.mkdir(parents=True)
    (runtime / "manifest.json").write_text(
        json.dumps({"name": "feat-ghost", "branch": "feat/ghost"}),
        encoding="utf-8",
    )
    user_file = stale_dir / "user-untracked.txt"
    user_file.write_text("keep\n", encoding="utf-8")

    result = _run_cli(["worktree", "remove", "--name", "ghost"], project)

    assert result.returncode == 2
    assert "unowned files" in result.stderr
    assert user_file.read_text(encoding="utf-8") == "keep\n"


def test_worktree_remove_preserves_directory_named_manifest_json(tmp_path: Path):
    project = tmp_path / "stale-manifest-directory"
    project.mkdir()
    _init_git_project(project)
    stale_dir = project / ".agent-flow" / "worktrees" / "feat-ghost"
    nested = stale_dir / "manifest.json"
    nested.mkdir(parents=True)
    marker = nested / "keep.txt"
    marker.write_text("keep\n", encoding="utf-8")
    runtime = _worktree_runtime_root(project, "feat-ghost")
    runtime.mkdir(parents=True)
    (runtime / "manifest.json").write_text("{}\n", encoding="utf-8")

    result = _run_cli(["worktree", "remove", "--name", "ghost"], project)

    assert result.returncode == 2
    assert marker.read_text(encoding="utf-8") == "keep\n"


def test_stale_cleanup_preserves_file_created_after_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_flow.cli import (
        _quarantine_stale_worktree_checkout,
        _remove_quarantined_stale_worktree_checkout,
    )

    project = tmp_path / "stale-quarantine-race"
    project.mkdir()
    _init_git_project(project)
    stale_dir = project / ".agent-flow" / "worktrees" / "feat-ghost"
    stale_dir.mkdir(parents=True)
    runtime = _worktree_runtime_root(project, "feat-ghost")
    runtime.mkdir(parents=True)
    payload = b'{"name":"feat-ghost","branch":"feat/ghost"}\n'
    (stale_dir / "manifest.json").write_bytes(payload)
    (runtime / "manifest.json").write_bytes(payload)
    quarantine = _quarantine_stale_worktree_checkout(
        project,
        stale_dir,
        "feat-ghost",
    )
    assert quarantine is not None
    original_rmdir = Path.rmdir

    def racing_rmdir(path: Path) -> None:
        if path == quarantine:
            (path / "late-user-file.txt").write_text("keep\n", encoding="utf-8")
        original_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", racing_rmdir)

    with pytest.raises(OSError):
        _remove_quarantined_stale_worktree_checkout(
            project,
            quarantine,
            "feat-ghost",
        )

    assert (quarantine / "late-user-file.txt").read_text(encoding="utf-8") == "keep\n"
    assert (quarantine / "manifest.json").read_bytes() == payload


def test_stale_cleanup_restores_owned_manifest_and_preserves_raced_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_flow.cli import (
        _quarantine_stale_worktree_checkout,
        _remove_quarantined_stale_worktree_checkout,
    )

    project = tmp_path / "stale-manifest-race"
    project.mkdir()
    _init_git_project(project)
    stale_dir = project / ".agent-flow" / "worktrees" / "feat-ghost"
    stale_dir.mkdir(parents=True)
    runtime = _worktree_runtime_root(project, "feat-ghost")
    runtime.mkdir(parents=True)
    payload = b'{"name":"feat-ghost","branch":"feat/ghost"}\n'
    replacement = b'{"user":"replacement"}\n'
    (stale_dir / "manifest.json").write_bytes(payload)
    (runtime / "manifest.json").write_bytes(payload)
    quarantine = _quarantine_stale_worktree_checkout(project, stale_dir, "feat-ghost")
    assert quarantine is not None
    original_rmdir = Path.rmdir

    def racing_rmdir(path: Path) -> None:
        if path == quarantine:
            (path / "manifest.json").write_bytes(replacement)
        original_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", racing_rmdir)

    with pytest.raises(OSError):
        _remove_quarantined_stale_worktree_checkout(project, quarantine, "feat-ghost")

    assert (quarantine / "manifest.json").read_bytes() == payload
    raced = list(quarantine.glob(".manifest.json.raced-*"))
    assert len(raced) == 1
    assert raced[0].read_bytes() == replacement


def test_stale_cleanup_rejects_manifest_replaced_by_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_flow.cli import (
        _quarantine_stale_worktree_checkout,
        _remove_quarantined_stale_worktree_checkout,
    )

    project = tmp_path / "stale-manifest-symlink-race"
    project.mkdir()
    _init_git_project(project)
    stale_dir = project / ".agent-flow" / "worktrees" / "feat-ghost"
    stale_dir.mkdir(parents=True)
    runtime = _worktree_runtime_root(project, "feat-ghost")
    runtime.mkdir(parents=True)
    payload = b'{"name":"feat-ghost","branch":"feat/ghost"}\n'
    (stale_dir / "manifest.json").write_bytes(payload)
    runtime_manifest = runtime / "manifest.json"
    runtime_manifest.write_bytes(payload)
    quarantine = _quarantine_stale_worktree_checkout(project, stale_dir, "feat-ghost")
    assert quarantine is not None
    manifest = quarantine / "manifest.json"
    original_rename = Path.rename

    def racing_rename(path: Path, target: Path) -> Path:
        if path == manifest:
            path.unlink()
            path.symlink_to(runtime_manifest)
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", racing_rename)

    with pytest.raises(RuntimeError, match="changed before removal"):
        _remove_quarantined_stale_worktree_checkout(project, quarantine, "feat-ghost")

    assert not manifest.is_symlink()
    assert manifest.read_bytes() == payload
    raced = list(quarantine.glob(".manifest.json.raced-*"))
    assert len(raced) == 1
    assert raced[0].is_symlink()


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
    _write_authenticated_stale_manifest(project, "feat-ghost")
    cleanup_env = _authorize_worktree_cleanup(project, "feat-ghost")

    r_remove = _run_cli(
        ["worktree", "remove", "--name", "ghost"], project, cleanup_env
    )
    assert r_remove.returncode == 0, r_remove.stderr
    assert not stale_dir.exists()
    assert not _branch_exists(project, "feat/ghost")


def test_stale_worktree_remove_handles_reference_hook_rejection(tmp_path: Path):
    project = tmp_path / "stale-hook-rejection"
    project.mkdir()
    _init_git_project(project)
    created = _run_cli(["worktree", "create", "--name", "ghost"], project)
    assert created.returncode == 0, created.stderr
    stale_dir = project / ".agent-flow" / "worktrees" / "feat-ghost"
    shutil.rmtree(stale_dir)
    stale_dir.mkdir(parents=True)
    _write_authenticated_stale_manifest(project, "feat-ghost")
    hook = project / ".git" / "hooks" / "reference-transaction"
    hook.write_text(
        "#!/bin/sh\n[ \"$1\" != prepared ]\n",
        encoding="utf-8",
    )
    hook.chmod(0o700)
    cleanup_env = _authorize_worktree_cleanup(project, "feat-ghost")

    result = _run_cli(
        ["worktree", "remove", "--name", "ghost"],
        project,
        cleanup_env,
    )

    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "ref updates aborted by hook" in result.stderr
    assert _branch_exists(project, "feat/ghost")


def test_stale_worktree_remove_preserves_unpushed_unique_branch_commit(tmp_path: Path):
    project = tmp_path / "stale-unique-commit"
    project.mkdir()
    _init_git_project(project)
    created = _run_cli(["worktree", "create", "--name", "ghost"], project)
    assert created.returncode == 0, created.stderr
    stale_dir = project / ".agent-flow" / "worktrees" / "feat-ghost"
    (stale_dir / "unique.txt").write_text("unique\n", encoding="utf-8")
    subprocess.run(["git", "add", "unique.txt"], cwd=stale_dir, check=True)
    subprocess.run(["git", "commit", "-m", "unique"], cwd=stale_dir, check=True)
    shutil.rmtree(stale_dir)
    stale_dir.mkdir(parents=True)
    _write_authenticated_stale_manifest(project, "feat-ghost")

    result = _run_cli(["worktree", "remove", "--name", "ghost"], project)

    assert result.returncode == 2
    assert "unpreserved commits" in result.stderr
    assert stale_dir.exists()
    assert _branch_exists(project, "feat/ghost")


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
    created = _run_cli(["worktree", "create", "--name", "ghost"], project)
    assert created.returncode == 0, created.stderr
    stale_dir = project / ".agent-flow" / "worktrees" / "feat-ghost"
    shutil.rmtree(stale_dir)
    stale_dir.mkdir(parents=True)
    _write_authenticated_stale_manifest(project, "feat-ghost")
    cleanup_env = _authorize_worktree_cleanup(project, "feat-ghost")

    r_remove = _run_cli(
        ["worktree", "remove", "--name", "ghost", "--keep-branch"],
        project,
        cleanup_env,
    )
    assert r_remove.returncode == 0
    assert not stale_dir.exists()
    assert _branch_exists(project, "feat/ghost")


def test_worktree_remove_refuses_stale_checkout_symlink(tmp_path: Path):
    project = tmp_path / "stale-symlink"
    project.mkdir()
    _init_git_project(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep\n", encoding="utf-8")
    stale_path = project / ".agent-flow" / "worktrees" / "feat-ghost"
    stale_path.parent.mkdir(parents=True)
    stale_path.symlink_to(outside, target_is_directory=True)
    runtime = _worktree_runtime_root(project, "feat-ghost")
    runtime.mkdir(parents=True)
    (runtime / "manifest.json").write_text(
        json.dumps({"name": "feat-ghost", "branch": "feat/ghost"}),
        encoding="utf-8",
    )

    result = _run_cli(["worktree", "remove", "--name", "ghost"], project)

    assert result.returncode == 2
    assert "unowned stale worktree path" in result.stderr
    assert stale_path.is_symlink()
    assert marker.read_text(encoding="utf-8") == "keep\n"


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
    cleanup_env = _authorize_worktree_cleanup(project, "feat-task")
    r_remove = _run_cli(
        ["worktree", "remove", "--name", "task"], project, cleanup_env
    )
    assert r_remove.returncode == 0, r_remove.stderr
    assert _branch_exists(project, "feat/shared")


def test_worktree_remove_deletes_agent_flow_created_branch(tmp_path: Path):
    project = tmp_path / "delete-owned-branch"
    project.mkdir()
    _init_git_project(project)

    r_create = _run_cli(["worktree", "create", "--name", "task"], project)
    assert r_create.returncode == 0, r_create.stderr
    assert _branch_exists(project, "feat/task")
    runtime = _worktree_runtime_root(project, "feat-task")
    manifest = json.loads((runtime / "manifest.json").read_text(encoding="utf-8"))
    node_state = project / ".git" / "agent-flow" / "current-runs" / "completed.json"
    node_state.parent.mkdir(parents=True, exist_ok=True)
    node_state.write_text(
        json.dumps(
            {
                "run_id": "node-complete",
                "run_dir": ".agent-flow/worktrees/feat-task/.agent-flow/runs/node-complete",
                "status": "complete",
                "phase": "complete",
                "completed_at": "2026-07-16T13:00:00+00:00",
                "workspace": manifest["identity"],
                "execution": {
                    "host": "codex",
                    "session_id": "node-cleanup",
                    "agent_id": "",
                },
            }
        ),
        encoding="utf-8",
    )
    r_remove = _run_cli(
        ["worktree", "remove", "--name", "task"],
        project,
        {
            "AGENT_FLOW_ACTIVE_HOST": "codex",
            "AGENT_FLOW_EXECUTION_ID": "node-cleanup",
        },
    )
    assert r_remove.returncode == 0, r_remove.stderr
    assert not _branch_exists(project, "feat/task")
    assert not node_state.exists()


def test_worktree_remove_requires_unique_commits_to_be_preserved_by_another_ref(
    tmp_path: Path,
):
    project = tmp_path / "preserve-unique-commit"
    project.mkdir()
    _init_git_project(project)
    created = _run_cli(["worktree", "create", "--name", "task"], project)
    assert created.returncode == 0, created.stderr
    worktree = project / ".agent-flow" / "worktrees" / "feat-task"
    (worktree / "unique.txt").write_text("unique\n", encoding="utf-8")
    subprocess.run(["git", "add", "unique.txt"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", "unique"], cwd=worktree, check=True)

    refused = _run_cli(["worktree", "remove", "--name", "task"], project)

    assert refused.returncode == 2
    assert "unpreserved branch commits" in refused.stderr
    assert worktree.exists()
    assert _branch_exists(project, "feat/task")

    tip = subprocess.run(
        ["git", "rev-parse", "feat/task"],
        cwd=project,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/feat/task", tip],
        cwd=project,
        check=True,
    )
    cleanup_env = _authorize_worktree_cleanup(project, "feat-task")
    removed = _run_cli(
        ["worktree", "remove", "--name", "task"], project, cleanup_env
    )

    assert removed.returncode == 0, removed.stderr
    assert not worktree.exists()
    assert not _branch_exists(project, "feat/task")


def test_compare_and_delete_preserves_branch_that_moves_after_validation(tmp_path: Path):
    from agent_flow.core.worktrees import (
        delete_worktree_branch_at_tip,
        preserved_worktree_branch_tip,
    )

    project = tmp_path / "branch-cas"
    project.mkdir()
    _init_git_project(project)
    subprocess.run(["git", "branch", "feat/task"], cwd=project, check=True)
    tip = subprocess.run(
        ["git", "rev-parse", "feat/task"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/feat/task", tip],
        cwd=project,
        check=True,
    )
    preserved = preserved_worktree_branch_tip(root=project, branch="feat/task")
    tree = subprocess.run(
        ["git", "rev-parse", f"{tip}^{{tree}}"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    moved = subprocess.run(
        ["git", "commit-tree", tree, "-p", tip, "-m", "moved after validation"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", "refs/heads/feat/task", moved, tip],
        cwd=project,
        check=True,
    )

    with pytest.raises(subprocess.CalledProcessError):
        delete_worktree_branch_at_tip(
            root=project,
            branch="feat/task",
            expected_tip=preserved or "",
        )

    assert _branch_exists(project, "feat/task")
    assert subprocess.run(
        ["git", "rev-parse", "feat/task"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip() == moved


def test_compare_and_delete_preserves_branch_that_moves_during_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import agent_flow.core.worktrees as worktrees

    project = tmp_path / "branch-cas-race"
    project.mkdir()
    _init_git_project(project)
    subprocess.run(["git", "branch", "feat/task"], cwd=project, check=True)
    tip = subprocess.run(
        ["git", "rev-parse", "feat/task"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "rev-parse", f"{tip}^{{tree}}"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    moved = subprocess.run(
        ["git", "commit-tree", tree, "-p", tip, "-m", "moved during deletion"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    original_run_git = worktrees._run_git
    raced = False

    def run_git_after_move(root: Path, *args: str, **kwargs):
        nonlocal raced
        if args[2:4] == ("update-ref", "-d"):
            subprocess.run(
                ["git", "update-ref", "refs/heads/feat/task", moved, tip],
                cwd=project,
                check=True,
            )
            raced = True
        return original_run_git(root, *args, **kwargs)

    monkeypatch.setattr(worktrees, "_run_git", run_git_after_move)

    with pytest.raises(subprocess.CalledProcessError):
        worktrees.delete_worktree_branch_at_tip(
            root=project,
            branch="feat/task",
            expected_tip=tip,
        )

    assert raced
    assert subprocess.run(
        ["git", "rev-parse", "feat/task"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip() == moved


def test_compare_and_delete_preserves_branch_checked_out_in_another_worktree(tmp_path: Path):
    from agent_flow.core.worktrees import delete_worktree_branch_at_tip

    project = tmp_path / "branch-checked-out"
    project.mkdir()
    _init_git_project(project)
    subprocess.run(["git", "branch", "feat/task"], cwd=project, check=True)
    tip = subprocess.run(
        ["git", "rev-parse", "feat/task"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    other_worktree = tmp_path / "other-worktree"
    subprocess.run(
        ["git", "worktree", "add", str(other_worktree), "feat/task"],
        cwd=project,
        check=True,
    )

    with pytest.raises(RuntimeError, match="checked out in another worktree"):
        delete_worktree_branch_at_tip(
            root=project,
            branch="feat/task",
            expected_tip=tip,
        )

    assert _branch_exists(project, "feat/task")
    assert subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=other_worktree,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip() == "feat/task"


def test_compare_and_delete_preserves_branch_checked_out_during_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import agent_flow.core.worktrees as worktrees

    project = tmp_path / "branch-checkout-race"
    project.mkdir()
    _init_git_project(project)
    subprocess.run(["git", "branch", "feat/task"], cwd=project, check=True)
    tip = subprocess.run(
        ["git", "rev-parse", "feat/task"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    other_worktree = tmp_path / "racing-worktree"
    original_run_git = worktrees._run_git
    raced = False

    def run_git_after_checkout(root: Path, *args: str, **kwargs):
        nonlocal raced
        if args[2:4] == ("update-ref", "-d"):
            subprocess.run(
                ["git", "worktree", "add", str(other_worktree), "feat/task"],
                cwd=project,
                check=True,
            )
            raced = True
        return original_run_git(root, *args, **kwargs)

    monkeypatch.setattr(worktrees, "_run_git", run_git_after_checkout)

    with pytest.raises(subprocess.CalledProcessError):
        worktrees.delete_worktree_branch_at_tip(
            root=project,
            branch="feat/task",
            expected_tip=tip,
        )

    assert raced
    assert _branch_exists(project, "feat/task")
    assert subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=other_worktree,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip() == "feat/task"


def test_compare_and_delete_preserves_branch_when_existing_reference_hook_rejects(
    tmp_path: Path,
):
    from agent_flow.core.worktrees import delete_worktree_branch_at_tip

    project = tmp_path / "branch-existing-hook"
    project.mkdir()
    _init_git_project(project)
    subprocess.run(["git", "branch", "feat/task"], cwd=project, check=True)
    tip = subprocess.run(
        ["git", "rev-parse", "feat/task"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    hook = project / ".git" / "hooks" / "reference-transaction"
    hook.write_text(
        "#!/bin/sh\n[ \"$1\" != prepared ]\n",
        encoding="utf-8",
    )
    hook.chmod(0o700)

    with pytest.raises(subprocess.CalledProcessError):
        delete_worktree_branch_at_tip(
            root=project,
            branch="feat/task",
            expected_tip=tip,
        )

    assert _branch_exists(project, "feat/task")


def test_two_owned_worktrees_can_finish_cleanup_concurrently(tmp_path: Path):
    project = tmp_path / "concurrent cleanup with spaces"
    project.mkdir()
    _init_git_project(project)
    for name in ("first", "second"):
        created = _run_cli(["worktree", "create", "--name", name], project)
        assert created.returncode == 0, created.stderr

    def remove(name: str):
        cleanup_env = _authorize_worktree_cleanup(
            project,
            f"feat-{name}",
            session_id=f"cleanup-{name}",
        )
        return _run_cli(
            ["worktree", "remove", "--name", name], project, cleanup_env
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(remove, ("first", "second")))

    assert all(result.returncode == 0 for result in results), [
        result.stderr for result in results
    ]
    assert not (project / ".agent-flow" / "worktrees" / "feat-first").exists()
    assert not (project / ".agent-flow" / "worktrees" / "feat-second").exists()
    assert not _branch_exists(project, "feat/first")
    assert not _branch_exists(project, "feat/second")


def test_authenticated_cleanup_lease_blocks_a_new_run_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from agent_flow.cli import _acquire_authenticated_cleanup_claim
    from agent_flow.core.workspace_boundary import release_workspace_start_claim

    project = tmp_path / "cleanup-start-race"
    project.mkdir()
    _init_git_project(project)
    created = _run_cli(["worktree", "create", "--name", "task"], project)
    assert created.returncode == 0, created.stderr
    cleanup_env = _authorize_worktree_cleanup(project, "feat-task")
    for name, value in cleanup_env.items():
        monkeypatch.setenv(name, value)

    claim = _acquire_authenticated_cleanup_claim(project, "feat-task")
    try:
        started = _run_cli(
            ["run", "replacement", "--worktree", "task"],
            project,
            {
                "AGENT_FLOW_ACTIVE_HOST": "codex",
                "AGENT_FLOW_EXECUTION_ID": "replacement-session",
            },
        )
    finally:
        release_workspace_start_claim(claim)

    assert started.returncode == 2
    assert "workspace start is already in progress" in started.stderr


def test_worktree_remove_refuses_dirty_agent_owned_worktree(tmp_path: Path):
    project = tmp_path / "dirty-owned-worktree"
    project.mkdir()
    _init_git_project(project)
    r_create = _run_cli(["worktree", "create", "--name", "task"], project)
    assert r_create.returncode == 0, r_create.stderr
    worktree = project / ".agent-flow" / "worktrees" / "feat-task"
    cleanup_env = _authorize_worktree_cleanup(project, "feat-task")
    (worktree / "user-untracked.txt").write_text("keep\n", encoding="utf-8")

    r_remove = _run_cli(
        ["worktree", "remove", "--name", "task"], project, cleanup_env
    )

    assert r_remove.returncode == 2
    assert "uncommitted changes" in r_remove.stderr
    assert worktree.exists()
    assert (worktree / "user-untracked.txt").read_text(encoding="utf-8") == "keep\n"
    assert _branch_exists(project, "feat/task")


def test_live_worktree_remove_does_not_trust_manifest_branch_redirect(tmp_path: Path):
    project = tmp_path / "live-branch-redirect"
    project.mkdir()
    _init_git_project(project)
    subprocess.run(["git", "branch", "feature/keep"], cwd=project, check=True)

    r_create = _run_cli(["worktree", "create", "--name", "task"], project)
    assert r_create.returncode == 0, r_create.stderr
    worktree = project / ".agent-flow" / "worktrees" / "feat-task"
    cleanup_env = _authorize_worktree_cleanup(project, "feat-task")
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

    r_remove = _run_cli(
        ["worktree", "remove", "--name", "task"], project, cleanup_env
    )
    assert r_remove.returncode == 0, r_remove.stderr
    assert _branch_exists(project, "feature/keep")
    assert _branch_exists(project, "feat/task")


def test_worktree_remove_keep_branch_preserves_agent_flow_created_branch(tmp_path: Path):
    project = tmp_path / "keep-owned-branch"
    project.mkdir()
    _init_git_project(project)

    r_create = _run_cli(["worktree", "create", "--name", "task"], project)
    assert r_create.returncode == 0, r_create.stderr
    cleanup_env = _authorize_worktree_cleanup(project, "feat-task")
    r_remove = _run_cli(
        ["worktree", "remove", "--name", "task", "--keep-branch"],
        project,
        cleanup_env,
    )
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


def test_gates_route_uses_top_level_green_with_result_evidence() -> None:
    from agent_flow.runner import _gates_route_key

    evidenced = json.dumps(
        {
            "status": "green",
            "results": [
                {
                    "command": "./gradlew test",
                    "passed": True,
                    "exit_code": 0,
                }
            ],
        }
    )
    status_only = json.dumps({"status": "green"})
    contradicted = json.dumps(
        {
            "passed": False,
            "status": "green",
            "results": [
                {
                    "command": "./gradlew test",
                    "passed": True,
                    "exit_code": 0,
                }
            ],
        }
    )

    assert _gates_route_key(evidenced) == "green"
    assert _gates_route_key(status_only) == "default"
    assert _gates_route_key(contradicted) == "request-changes"

    targeted = json.dumps(
        {
            "passed": True,
            "status": "targeted-green",
            "verification_mode": "targeted",
            "results": [
                {
                    "command": "./gradlew :feature:chat:test",
                    "passed": True,
                    "exit_code": 0,
                }
            ],
        }
    )
    assert _gates_route_key(targeted) == "default"


def test_gates_top_level_green_routes_to_commit_instead_of_default_fix_loop(
    tmp_path: Path,
) -> None:
    from agent_flow.core.artifacts import gate_execution_fingerprint
    from agent_flow.core.gates import GateCommand
    from agent_flow.runner import Phase, Runner

    subprocess.run(("git", "init", "-b", "main", str(tmp_path)), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(tmp_path), "config", "user.name", "Test User"), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "config", "user.email", "test@example.com"), check=True)
    (tmp_path / "README.md").write_text("gate\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("run/\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(tmp_path), "add", "README.md", ".gitignore"), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "commit", "-m", "initial"), check=True, capture_output=True)
    run_dir = tmp_path / "run"
    artifact = run_dir / "artifacts" / "gate-results.json"
    artifact.parent.mkdir(parents=True)
    command = GateCommand("test", ("./gradlew", ":feature:chat:test"))
    fingerprint = gate_execution_fingerprint(
        root=tmp_path,
        profile_ids=["android"],
        verification_mode="full",
        changed_files=[],
        commands=[command],
    )
    artifact.write_text(
        json.dumps(
            {
                "passed": True,
                "status": "green",
                "verification_mode": "full",
                "fingerprint": fingerprint,
                "results": [
                    {
                        "gate_id": "test",
                        "command": "./gradlew :feature:chat:test",
                        "requested_argv": ["./gradlew", ":feature:chat:test"],
                        "required": True,
                        "passed": True,
                        "exit_code": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.project_root = tmp_path
    runner.phases = [
        Phase(
            id="gates",
            description="",
            artifact="artifacts/gate-results.json",
            routes={"green": "commit", "default": "fix-loop"},
        ),
        Phase(id="fix-loop", description=""),
        Phase(id="commit", description=""),
    ]

    assert runner._next_index(0, runner.phases[0]) == (2, False)

    (tmp_path / "README.md").write_text("changed after gates\n", encoding="utf-8")
    assert runner._next_index(0, runner.phases[0]) == (1, False)


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


def test_multi_review_architecture_blocked_routes_to_refactor(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import Phase, Runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    phases = [
        Phase(id="refactor", description=""),
        Phase(
            id="architecture-review",
            description="",
            multi_review=True,
            routes={"approve": "done", "request-changes": "refactor", "blocked": "refactor"},
        ),
        Phase(id="done", description=""),
    ]
    (run_dir / "architecture-review.md").write_text(
        "verdict: blocked\n",
        encoding="utf-8",
    )
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.phases = phases

    assert runner._next_index(1, phases[1]) == (0, False)


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


def test_multi_review_packets_include_only_compact_architecture_profile_contract(
    tmp_path: Path,
):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.adapters.hosted import HostedAdapter, _reviewer_jobs
    from agent_flow.runner import Phase

    adapter = HostedAdapter("codex")
    adapter._profile_snapshot = {
        "id": "node",
        "review_angles": [],
        "architecture": {
            "contract": "clean-architecture-core",
            "platform": "node",
            "strict_when_roots_present": True,
            "activation_roots": ["src", "lib"],
            "roles": {"domain": ["src/domain/**"]},
        },
        "gates": [{"id": "test", "command": ["npm", "test"]}],
    }
    phase = Phase(id="architecture-review", description="", multi_review=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    jobs = _reviewer_jobs(phase, run_dir, KIT_ROOT, adapter)

    assert [job.angle_id for job in jobs] == [
        "generalist",
        "architecture-design",
        "clean-architecture",
    ]
    for job in jobs:
        assert "Profile contract:" in job.prompt
        assert "architecture:" in job.prompt
        assert "contract: clean-architecture-core" in job.prompt
        assert "platform: node" in job.prompt
        assert "roles:" not in job.prompt
        assert "gates:" not in job.prompt
        assert len(job.prompt) < 8_000


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
