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
import subprocess
import sys
from pathlib import Path

import pytest


KIT_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(args: list[str], cwd: Path, env_extra: dict | None = None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(KIT_ROOT / "src")
    env["AGENT_FLOW_ADAPTER"] = "generic"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "agent_flow.cli", *args],
        cwd=cwd, env=env, capture_output=True, text=True,
    )


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
        "worktree", "implement", "final-review", "fix-loop",
        "commit", "push-pr", "pr-watch", "merge", "cleanup",
    ]
    for a in expected_post:
        assert (run_dir / f"{a}.md").exists(), f"missing post-pause: {a}"
    assert not (run_dir / "active").exists()


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


def test_backward_route_invalidates_target_artifact(tmp_path: Path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.runner import Phase, Runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    watch = run_dir / "pr-watch.md"
    watch.write_text("status: comments\n", encoding="utf-8")
    (run_dir / "pr-comment-fix.md").write_text("fixed\n", encoding="utf-8")

    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.phases = [
        Phase(id="pr-watch", description="", routes={"comments": "pr-comment-fix"}),
        Phase(id="pr-comment-fix", description="", routes={"default": "pr-watch"}),
    ]

    assert runner._next_index(1, runner.phases[1]) == (0, False)
    assert not watch.exists()


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
    assert "`implementation structure`" in text
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
        "## Aggregates\n"
        "## Entities\n"
        "## Value Objects\n"
        "## Application Use Cases\n"
        "## Infrastructure Adapters\n"
        "## Presentation Routes\n"
        "## Dependency Rule\n"
        "## Implementation Structure\n",
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
        "# ddd-design\n\nservice-layer refactor\n",
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


def test_abort_yes_flag_skips_prompt(tmp_path: Path):
    """`agent-flow abort --yes` must not block on confirmation."""
    project = tmp_path / "abort_yes"
    project.mkdir()
    r1 = _run_cli(["run", "any task"], project)
    assert r1.returncode == 0
    r2 = _run_cli(["abort", "--yes"], project)
    assert r2.returncode == 0
    assert "aborted" in r2.stdout.lower()


def test_cli_detection_runs():
    """Smoke check that detection runs and returns plausible CLIs."""
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.cli_detect import detect_available_clis
    clis = detect_available_clis()
    assert isinstance(clis, list)
    for c in clis:
        assert c.name in {"claude", "codex", "gemini"}
