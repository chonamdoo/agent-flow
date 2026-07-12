from __future__ import annotations

import contextlib
import hashlib
import io
from pathlib import Path

import pytest

import agent_flow.runner as runner_module
from agent_flow.artifact import read_meta, write_meta
from agent_flow.cli import main
from agent_flow.runner import Phase, ResumeMode, Runner


class _ManualAdapter:
    name = "manual"

    def execute(self, phase: Phase, run_dir: Path, project_root: Path) -> bool:
        return False


def _pause_runner(tmp_path: Path, monkeypatch) -> tuple[Runner, Path]:
    project = tmp_path / "project"
    run_dir = project / ".agent-flow" / "runs" / "r1"
    artifact = run_dir / "artifacts" / "pause.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("version one\n", encoding="utf-8")
    write_meta(
        run_dir,
        {
            "run_id": "r1",
            "workflow": "pause-test",
            "task": "pause approval",
            "current_phase": "pause",
            "phase_index": 0,
            "started_at": "1970-01-01T00:01:40+00:00",
            "phase_entered_at": "1970-01-01T00:03:20+00:00",
        },
    )
    runner = Runner.__new__(Runner)
    runner.project_root = project
    runner.state_root = project
    runner.config_root = project
    runner.workflow_name = "pause-test"
    runner.run_dir = run_dir
    runner.architecture = "default"
    runner.next_command = "agent-flow-python continue --root /leader --worktree feat-pause"
    runner.profile_id = "generic"
    runner.profile = {}
    runner.phases = [
        Phase(id="pause", description="", pause_after=True, artifact="artifacts/pause.md"),
        Phase(id="done", description="", artifact="artifacts/done.md"),
    ]
    monkeypatch.setattr(runner_module, "detect_adapter", lambda: _ManualAdapter())
    monkeypatch.setattr(runner_module, "detect_available_clis", lambda: [])
    monkeypatch.setattr(
        runner_module,
        "_ensure_lore_snapshot",
        lambda run_path, _config_root, _project_root: (read_meta(run_path), []),
    )
    return runner, artifact


def test_resume_stays_paused_until_explicit_matching_approval(tmp_path: Path, monkeypatch) -> None:
    runner, artifact = _pause_runner(tmp_path, monkeypatch)

    runner.run(ResumeMode.RESUME)
    first = read_meta(runner.run_dir)
    first_pending = first["pause_after_pending"]
    assert first_pending["phase"] == "pause"
    assert first_pending["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert isinstance(first_pending["requested_at"], str)

    runner.run(ResumeMode.RESUME)
    second = read_meta(runner.run_dir)
    assert second["current_phase"] == "pause"
    assert second["pause_after_pending"] == first_pending
    assert "pause_after_approval" not in second

    runner.run(ResumeMode.RESUME, approve_pause=True)
    approved = read_meta(runner.run_dir)
    assert approved["current_phase"] == "done"
    assert "pause_after_pending" not in approved
    assert approved["pause_after_approval"]["phase"] == "pause"
    assert approved["pause_after_approval"]["artifact_sha256"] == first_pending["artifact_sha256"]
    assert isinstance(approved["pause_after_approval"]["approved_at"], str)


def test_mutated_artifact_invalidates_approval_and_repauses(tmp_path: Path, monkeypatch) -> None:
    runner, artifact = _pause_runner(tmp_path, monkeypatch)
    runner.run(ResumeMode.RESUME)
    original = read_meta(runner.run_dir)["pause_after_pending"]
    artifact.write_text("version two\n", encoding="utf-8")

    runner.run(ResumeMode.RESUME, approve_pause=True)

    meta = read_meta(runner.run_dir)
    changed = meta["pause_after_pending"]
    assert meta["current_phase"] == "pause"
    assert changed["artifact_sha256"] != original["artifact_sha256"]
    assert changed["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert isinstance(changed["requested_at"], str)
    assert "pause_after_approval" not in meta


def test_approval_without_pending_request_fails_closed(tmp_path: Path, monkeypatch) -> None:
    runner, _artifact = _pause_runner(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="existing pause request"):
        runner.run(ResumeMode.RESUME, approve_pause=True)


def test_approval_on_non_pause_phase_fails_closed(tmp_path: Path, monkeypatch) -> None:
    runner, artifact = _pause_runner(tmp_path, monkeypatch)
    runner.phases = [Phase(id="work", description="", artifact="artifacts/pause.md")]
    meta = read_meta(runner.run_dir)
    meta.update({"current_phase": "work", "phase_index": 0})
    write_meta(runner.run_dir, meta)
    assert artifact.exists()

    with pytest.raises(RuntimeError, match="non-pause phase work"):
        runner.run(ResumeMode.RESUME, approve_pause=True)


def test_cli_approval_without_active_run_fails_closed(tmp_path: Path) -> None:
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        assert main(["continue", "--root", str(tmp_path), "--approve-pause"]) == 2
    assert "requires an existing paused run" in stderr.getvalue()
