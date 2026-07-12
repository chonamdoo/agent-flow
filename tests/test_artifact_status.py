from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path

from agent_flow.artifact import ActiveRun, write_meta


def _status_fixture(tmp_path: Path, workflow: str, yaml_text: str, phase: str) -> tuple[Path, Path]:
    project = tmp_path / "project"
    (project / "worktree").mkdir(parents=True)
    workflow_path = project / ".agent-flow" / "workflows" / f"{workflow}.yaml"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text(yaml_text, encoding="utf-8")
    run_dir = project / ".agent-flow" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    write_meta(
        run_dir,
        {
            "run_id": "r1",
            "workflow": workflow,
            "task": "status regression",
            "current_phase": phase,
            "phase_index": 0,
            "started_at": "1970-01-01T00:01:40+00:00",
            "phase_entered_at": "1970-01-01T00:03:20+00:00",
        },
    )
    return project, run_dir


def _render_status(project: Path, run_dir: Path, workflow: str) -> str:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        ActiveRun(
            path=run_dir,
            run_id="r1",
            workflow=workflow,
            task="status regression",
            started_at="1970-01-01T00:01:40+00:00",
        ).print_status(
            next_command="agent-flow-python continue --root /leader --worktree feat-status",
            config_root=project,
            workspace_root=project / "worktree",
        )
    return output.getvalue()


def test_status_reports_stale_canonical_artifact_without_mutating_meta(tmp_path: Path) -> None:
    workflow = "status-freshness"
    project, run_dir = _status_fixture(
        tmp_path,
        workflow,
        f"""id: {workflow}
phases:
  - id: inspect
    artifact: artifacts/inspect.md
    required_markers:
      - "verified: true"
""",
        "inspect",
    )
    artifact = run_dir / "artifacts" / "inspect.md"
    artifact.parent.mkdir()
    artifact.write_text("## Completion Gate\nverified: true\n", encoding="utf-8")
    os.utime(artifact, (150, 150))
    meta_path = run_dir / "meta.json"
    before = meta_path.read_bytes(), meta_path.stat().st_mtime_ns

    output = _render_status(project, run_dir, workflow)

    assert "status: blocked" in output
    assert "reason: stale_artifact" in output
    assert "next_command: agent-flow-python continue --root /leader --worktree feat-status" in output
    assert (meta_path.read_bytes(), meta_path.stat().st_mtime_ns) == before


def test_status_distinguishes_pr_watch_block_from_advance_required(tmp_path: Path) -> None:
    workflow = "status-routing"
    project, run_dir = _status_fixture(
        tmp_path,
        workflow,
        f"""id: {workflow}
phases:
  - id: pr-watch
    artifact: artifacts/pr-watch.md
    routes:
      pending: block
      green: done
      default: block
  - id: done
    artifact: artifacts/done.md
""",
        "pr-watch",
    )
    artifact = run_dir / "artifacts" / "pr-watch.md"
    artifact.parent.mkdir()
    artifact.write_text("status: pending\n", encoding="utf-8")

    blocked = _render_status(project, run_dir, workflow)

    assert "reason: route_blocked" in blocked
    assert "next_command: agent-flow-python continue --root /leader --worktree feat-status" in blocked

    artifact.write_text("status: green\n", encoding="utf-8")
    advance = _render_status(project, run_dir, workflow)

    assert "reason: phase_artifact_written_advance_required" in advance
    assert "reason: route_blocked" not in advance


def test_status_prioritizes_stub_markers_and_pause_hash(tmp_path: Path, monkeypatch) -> None:
    workflow = "status-validation"
    project, run_dir = _status_fixture(
        tmp_path,
        workflow,
        f"""id: {workflow}
phases:
  - id: inspect
    pause_after: true
    artifact: artifacts/inspect.md
    required_markers:
      - "verified: true"
""",
        "inspect",
    )
    artifact = run_dir / "artifacts" / "inspect.md"
    artifact.parent.mkdir()
    monkeypatch.delenv("AGENT_FLOW_GENERIC_MODE", raising=False)
    artifact.write_text("_stub artifact written by GenericAdapter (stub mode)._\n", encoding="utf-8")

    assert "reason: generic_stub_artifact" in _render_status(project, run_dir, workflow)

    artifact.write_text("not verified\n", encoding="utf-8")
    assert "reason: missing_completion_markers" in _render_status(project, run_dir, workflow)

    artifact.write_text("## Completion Gate\nverified: true\n", encoding="utf-8")
    assert "reason: phase_artifact_written_advance_required" in _render_status(
        project,
        run_dir,
        workflow,
    )

    payload = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    payload["pause_after_pending"] = {
        "phase": "inspect",
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }
    write_meta(run_dir, payload)
    before = (run_dir / "meta.json").read_bytes()

    paused_status = _render_status(project, run_dir, workflow)
    assert "reason: pause_after" in paused_status
    next_command_line = next(
        line for line in paused_status.splitlines() if line.startswith("next_command: ")
    )
    assert next_command_line.endswith(" --approve-pause")
    assert next_command_line.count("--approve-pause") == 1
    assert (run_dir / "meta.json").read_bytes() == before

    payload["pause_after_approval"] = {
        "phase": "inspect",
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "approved_at": "2026-07-11T00:00:00+00:00",
    }
    write_meta(run_dir, payload)

    approved_status = _render_status(project, run_dir, workflow)
    assert "reason: phase_artifact_written_advance_required" in approved_status
    assert "--approve-pause" not in approved_status
