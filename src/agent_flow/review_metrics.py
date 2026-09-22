from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from agent_flow.artifact import read_meta
from agent_flow.core.architecture_policy import (
    ArchitectureMode,
    ArchitectureSnapshot,
    is_private_contract,
)
from agent_flow.core.worktree_isolation import write_run_artifact_text
from agent_flow.multi_review import Distribution, ReviewExecution, review_job_id


def write_review_metrics(
    run_dir: Path,
    distribution: Distribution,
    execution: ReviewExecution,
    *,
    wall_s: float,
    snapshot: ArchitectureSnapshot | None,
) -> None:
    """Write diagnostics only; callers isolate failures from review authority.

    Byte counts describe captured text re-encoded as UTF-8, not wire usage.
    Run elapsed time includes idle time; it is not summed reviewer duration.
    """
    run_dir = run_dir.resolve()
    meta = read_meta(run_dir)
    now = datetime.now(timezone.utc)
    measurement_id = uuid4().hex
    assigned = {
        review_job_id(provider, job): (provider, job)
        for provider, jobs in distribution.by_cli.items()
        for job in jobs
    }
    outcomes = {outcome.job_id: outcome for outcome in execution.outcomes}
    jobs = []
    for result in execution.results:
        provider, job = assigned[result.job_id]
        outcome = outcomes[result.job_id]
        jobs.append({
            "job_id": result.job_id,
            "angle_id": job.match_angle_id,
            "provider": provider,
            "model": outcome.model,
            "effort": outcome.effort,
            "status": outcome.status,
            "duration_s": result.duration_s,
            "prompt_utf8_bytes": len(job.prompt_for(provider).encode("utf-8")),
            "stdout_utf8_bytes": len(result.stdout.encode("utf-8")),
            "stderr_utf8_bytes": len(result.stderr.encode("utf-8")),
        })
    mode = snapshot.selection.mode if snapshot is not None else None
    source = None
    if mode is ArchitectureMode.CLEAN:
        source = "bundled"
    elif mode is ArchitectureMode.LOCAL and snapshot is not None:
        source = "private" if is_private_contract(snapshot.selection) else "team"
    elif mode is ArchitectureMode.PENDING:
        source = "none"
    payload = {
        "schema_version": 1,
        "measurement_id": measurement_id,
        "recorded_at": now.isoformat(),
        "run_id": meta.get("run_id"),
        "workflow": meta.get("workflow"),
        "phase_id": distribution.phase_id,
        "phase_entered_at": meta.get("phase_entered_at"),
        "architecture_mode": mode.value if mode is not None else None,
        "contract_source": source,
        "architecture_digest": snapshot.digest if snapshot is not None else None,
        "wave_wall_s": wall_s,
        "run_elapsed_s": _run_elapsed_s(meta.get("started_at"), now),
        "assigned_job_count": len(assigned),
        "observed_job_count": len(jobs),
        "jobs": jobs,
    }
    write_run_artifact_text(
        run_dir,
        run_dir / f"review-metrics-{measurement_id}.json",
        json.dumps(payload, indent=2, allow_nan=False) + "\n",
    )


def _run_elapsed_s(started_at: object, now: datetime) -> float | None:
    if not isinstance(started_at, str):
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if started.tzinfo is None:
            return None
        elapsed = (now - started).total_seconds()
    except (ValueError, OverflowError):
        return None
    return elapsed if elapsed >= 0 else None
