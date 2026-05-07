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
import time
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
    assert [job.angle_id for job in jobs] == ["generalist", "architecture-design"]
    assert "Review Angle" in jobs[0].prompt
    assert "Architecture Design" in jobs[1].prompt


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
    assert [job.angle_id for job in jobs] == ["generalist", "architecture-design"]
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
    assert [job.angle_id for job in jobs] == ["generalist", "architecture-design"]
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
