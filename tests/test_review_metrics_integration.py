from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_flow import multi_review, review_metrics
from agent_flow.adapters import hosted
from agent_flow.cli_detect import CliInfo
from agent_flow.core.worktree_isolation import WorktreeIsolationError
from agent_flow.runner import Phase
from agent_flow.subprocess_pool import SubprocessResult


@pytest.fixture
def review_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "meta.json").write_text(json.dumps({
        "run_id": "metrics-regression",
        "workflow": "development",
        "phase_entered_at": "2026-09-22T00:00:00+00:00",
    }))
    clis = [CliInfo("claude", ("claude",), ("-p",)), CliInfo("codex", ("codex",), ("exec",))]
    monkeypatch.delenv("AGENT_FLOW_REVIEWERS", raising=False)
    monkeypatch.setattr(multi_review, "detect_available_clis", lambda: clis)
    monkeypatch.setattr(multi_review, "cli_by_name", lambda name: next(cli for cli in clis if cli.name == name))
    monkeypatch.setattr(multi_review, "_cli_version", lambda _: "fixture-version")
    monkeypatch.setattr(multi_review, "_reviewer_launch_profile", lambda _: {})
    monkeypatch.setattr(multi_review, "assert_managed_hooks_registered", lambda *_: None)
    monkeypatch.setattr(multi_review, "leader_root_for", lambda _: None)
    job = multi_review.ReviewerJob("generalist", "private prompt", tmp_path / "review-generalist.md", tmp_path)
    monkeypatch.setattr(hosted, "_reviewer_jobs", lambda *a, **k: ([job], {}))
    monkeypatch.setattr(hosted, "_write_review_input_snapshot", lambda *a, **k: hosted.ReviewInputSnapshot(tmp_path / "input.patch", "a" * 64))
    results = {
        provider: SubprocessResult(
            job_id=f"{provider}-generalist",
            returncode=0,
            stdout="## Reviewer\nreviewer-source: sub-agent\nverdict: approve\n",
            stderr="private diagnostic warning",
            duration_s=1.5,
        )
        for provider in ("claude", "codex")
    }

    def run_parallel(jobs, **_kwargs):
        return [results[job.job_id.split("-", 1)[0]] for job in jobs]

    monkeypatch.setattr(multi_review, "run_parallel", run_parallel)
    adapter = hosted.HostedAdapter("codex")
    return adapter, Phase(id="review", description="", multi_review=True), results


@pytest.mark.parametrize("failure", [OSError("private path"), ValueError("private payload")])
def test_metrics_failure_preserves_sealed_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog, review_run, failure: Exception,
) -> None:
    adapter, phase, _ = review_run
    original = hosted._run_multi_review_distribution(phase, tmp_path, tmp_path, adapter)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if not path.name.startswith("review-metrics-")}
    measurements = list(tmp_path.glob("review-metrics-*.json"))
    assert len(measurements) == 1

    def fail_metrics(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(review_metrics, "write_run_artifact_text", fail_metrics)
    observed = hosted._run_multi_review_distribution(phase, tmp_path, tmp_path, adapter)
    assert observed == original
    assert {name: (tmp_path / name).read_bytes() for name in before} == before
    assert list(tmp_path.glob("review-metrics-*.json")) == measurements
    assert type(failure).__name__ in caplog.text
    assert str(failure) not in caplog.text


def test_metrics_failure_does_not_hide_all_providers_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, review_run,
) -> None:
    adapter, phase, results = review_run
    for result in results.values():
        result.returncode = 1
        result.error = "provider failed"

    def fail_metrics(*_args, **_kwargs):
        raise OSError("metrics disk failure")

    monkeypatch.setattr(review_metrics, "write_run_artifact_text", fail_metrics)
    with pytest.raises(WorktreeIsolationError, match="required reviewer subprocesses failed closed"):
        adapter.execute(phase, tmp_path, tmp_path)
    payload = json.loads((tmp_path / "review-review-results.json").read_text())
    assert all(outcome["status"] == "error" for outcome in payload["outcomes"])
    assert not list(tmp_path.glob("review-metrics-*.json"))


def test_metrics_does_not_swallow_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, review_run,
) -> None:
    adapter, phase, _ = review_run

    def interrupt_metrics(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(review_metrics, "write_run_artifact_text", interrupt_metrics)
    with pytest.raises(KeyboardInterrupt):
        hosted._run_multi_review_distribution(phase, tmp_path, tmp_path, adapter)
