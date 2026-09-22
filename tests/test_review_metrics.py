from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path

import pytest

from agent_flow.core.architecture_policy import (
    ArchitectureContract,
    ArchitectureMode,
    ArchitectureSelection,
    ArchitectureSnapshot,
    ContractDocument,
    PRIVATE_CONTRACT_PATH,
    TEAM_CONTRACT_PATH,
)
from agent_flow.core.review_evidence import ReviewerOutcome
from agent_flow.multi_review import Distribution, ReviewExecution, ReviewerJob
from agent_flow.review_metrics import write_review_metrics
from agent_flow.subprocess_pool import SubprocessResult


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    meta = {
        "run_id": "metrics-run",
        "workflow": "full-feature",
        "current_phase": "final-review",
        "phase_entered_at": "2026-01-02T01:00:00+09:00",
        "started_at": "2026-01-02T00:00:00+09:00",
        "task": "SECRET-META-TASK",
        "review_evidence": {"final-review": {"nonce": "SECRET-EVIDENCE-NONCE"}},
    }
    (tmp_path / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (tmp_path / "final-review.md").write_text(
        "## Overall\nverdict: request-changes\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def review(run_dir: Path) -> tuple[Distribution, ReviewExecution]:
    job = ReviewerJob(
        angle_id="architecture-design-codex",
        base_angle_id="architecture-design",
        prompt="",
        prompt_by_provider={
            "codex": "SECRET-PROMPT-CODEX 검토 é",
            "claude": "SECRET-UNUSED-PROMPT 別",
        },
        output_path=run_dir / "codex-architecture-design.md",
        artifact_root=run_dir,
    )
    skipped = ReviewerJob(
        angle_id="generalist",
        prompt="SECRET-SKIPPED-PROMPT",
        output_path=run_dir / "claude-generalist.md",
        artifact_root=run_dir,
    )
    distribution = Distribution(
        by_cli={"codex": [job], "claude": [skipped]},
        phase_id="final-review",
    )
    result = SubprocessResult(
        job_id="codex-architecture-design-codex",
        stdout="SECRET-STDOUT 판단 é",
        stderr="SECRET-STDERR 진단 ü",
        returncode=0,
        duration_s=2.5,
        error="SECRET-PROVIDER-ERROR",
    )
    outcome = ReviewerOutcome(
        job_id=result.job_id,
        provider="codex",
        model="gpt-5.6-sol",
        effort="xhigh",
        status="error",
        verdict=None,
        required=True,
        artifact=job.output_path.name,
        artifact_sha256="a" * 64,
        prompt_digest="b" * 16,
        argv_digest="c" * 16,
    )
    execution = ReviewExecution(
        results=(result,), skipped_providers=("claude",), outcomes=(outcome,)
    )
    return distribution, execution


def _snapshot(mode: ArchitectureMode, path: str | None = None) -> ArchitectureSnapshot:
    contract = None
    if path is not None:
        content = "SECRET-CONTRACT-CONTENT 계약"
        encoded = content.encode("utf-8")
        contract = ArchitectureContract(
            documents=(
                ContractDocument(
                    path=path,
                    sha256=hashlib.sha256(encoded).hexdigest(),
                    bytes=len(encoded),
                ),
            ),
            untracked=(path,) if path == PRIVATE_CONTRACT_PATH else (),
            contents=(content,),
        )
    return ArchitectureSnapshot(
        selection=ArchitectureSelection(mode, path),
        declared=True,
        contract=contract,
        digest=hashlib.sha256(f"{mode.value}:{path}".encode("utf-8")).hexdigest(),
    )


def test_metrics_measure_captured_utf8_without_leaking_review_content(
    run_dir: Path, review: tuple[Distribution, ReviewExecution]
) -> None:
    distribution, execution = review
    snapshot = _snapshot(ArchitectureMode.LOCAL, PRIVATE_CONTRACT_PATH)

    write_review_metrics(run_dir, distribution, execution, wall_s=3.75, snapshot=snapshot)

    metric_path, = run_dir.glob("review-metrics-*.json")
    payload = json.loads(metric_path.read_text(encoding="utf-8"))
    job, = payload["jobs"]
    result = execution.results[0]
    assert job["prompt_utf8_bytes"] == len(
        distribution.by_cli["codex"][0].prompt_for("codex").encode("utf-8")
    )
    assert job["stdout_utf8_bytes"] == len(result.stdout.encode("utf-8"))
    assert job["stderr_utf8_bytes"] == len(result.stderr.encode("utf-8"))
    assert job["job_id"] == result.job_id
    assert job["angle_id"] == "architecture-design"
    assert (job["provider"], job["model"], job["effort"], job["status"]) == (
        "codex", "gpt-5.6-sol", "xhigh", "error"
    )
    assert job["duration_s"] == 2.5
    assert payload["wave_wall_s"] == 3.75
    assert (payload["assigned_job_count"], payload["observed_job_count"]) == (2, 1)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "SECRET-" not in serialized
    assert PRIVATE_CONTRACT_PATH not in serialized
    assert str(run_dir) not in serialized
    assert "codex-architecture-design.md" not in serialized


def test_repeated_measurements_preserve_prior_attempts_and_routing_evidence(
    run_dir: Path, review: tuple[Distribution, ReviewExecution]
) -> None:
    distribution, execution = review
    evidence_before = {path.name: path.read_bytes() for path in run_dir.iterdir()}

    write_review_metrics(run_dir, distribution, execution, wall_s=3.75, snapshot=None)
    first_path, = run_dir.glob("review-metrics-*.json")
    first_bytes = first_path.read_bytes()
    write_review_metrics(run_dir, distribution, execution, wall_s=5.25, snapshot=None)

    paths = list(run_dir.glob("review-metrics-*.json"))
    assert len(paths) == 2
    assert first_path.read_bytes() == first_bytes
    assert {name: (run_dir / name).read_bytes() for name in evidence_before} == evidence_before
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    assert len({payload["measurement_id"] for payload in payloads}) == 2
    assert {payload["wave_wall_s"] for payload in payloads} == {3.75, 5.25}
    for path, payload in zip(paths, payloads):
        assert path.stem == f"review-metrics-{payload['measurement_id']}"
        assert (payload["run_id"], payload["workflow"], payload["phase_id"]) == (
            "metrics-run", "full-feature", "final-review"
        )
        assert payload["phase_entered_at"] == "2026-01-02T01:00:00+09:00"
        recorded_at = datetime.fromisoformat(payload["recorded_at"])
        started_at = datetime.fromisoformat("2026-01-02T00:00:00+09:00")
        assert recorded_at.utcoffset() is not None
        assert payload["run_elapsed_s"] == pytest.approx(
            (recorded_at - started_at).total_seconds()
        )


@pytest.mark.parametrize(
    "mode, contract_path, expected_source",
    [
        (ArchitectureMode.CLEAN, None, "bundled"),
        (ArchitectureMode.LOCAL, TEAM_CONTRACT_PATH, "team"),
        (ArchitectureMode.LOCAL, PRIVATE_CONTRACT_PATH, "private"),
        (ArchitectureMode.PENDING, None, "none"),
        (None, None, None),
    ],
)
def test_architecture_attribution_does_not_publish_contract_paths(
    run_dir: Path,
    mode: ArchitectureMode | None,
    contract_path: str | None,
    expected_source: str | None,
) -> None:
    snapshot = _snapshot(mode, contract_path) if mode is not None else None

    write_review_metrics(
        run_dir, Distribution(), ReviewExecution(), wall_s=0.0, snapshot=snapshot
    )

    metric_path, = run_dir.glob("review-metrics-*.json")
    payload = json.loads(metric_path.read_text(encoding="utf-8"))
    assert payload["architecture_mode"] == (mode.value if mode is not None else None)
    assert payload["contract_source"] == expected_source
    assert payload["architecture_digest"] == (snapshot.digest if snapshot else None)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert TEAM_CONTRACT_PATH not in serialized
    assert PRIVATE_CONTRACT_PATH not in serialized
    assert "SECRET-CONTRACT-CONTENT" not in serialized


@pytest.mark.parametrize(
    "start_fields",
    [
        {},
        {"started_at": "not-a-timestamp"},
        {"started_at": "2026-01-02T00:00:00"},
        {"started_at": 12345},
    ],
    ids=["missing", "malformed", "naive", "wrong-type"],
)
def test_missing_or_invalid_start_time_does_not_invent_run_elapsed(
    run_dir: Path, start_fields: dict[str, object]
) -> None:
    (run_dir / "meta.json").write_text(json.dumps(start_fields), encoding="utf-8")

    write_review_metrics(
        run_dir, Distribution(), ReviewExecution(), wall_s=1.25, snapshot=None
    )

    metric_path, = run_dir.glob("review-metrics-*.json")
    payload = json.loads(metric_path.read_text(encoding="utf-8"))
    assert payload["run_elapsed_s"] is None
    assert payload["wave_wall_s"] == 1.25
