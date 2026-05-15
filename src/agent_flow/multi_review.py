"""Multi-reviewer distribution and parallel execution.

The final-review phase fans out N review angles. Distribution rules:

  - 0 CLIs detected → "generic" (host AI handles all angles itself).
  - 1 CLI → all angles to that CLI; host adapter decides whether to do
    them via in-host parallelism (Claude Task tool, Codex sessions) or
    sequentially.
  - 2+ CLIs → round-robin assignment, host CLI last (it already has the
    user's session attention; let other CLIs run first via subprocess).

Async execution:
  - Each non-host CLI is invoked via subprocess in parallel (asyncio).
  - Each angle writes an independent artifact: `final-review-<angle>.md`.
  - Partial results survive: a timed-out angle leaves siblings intact, and
    the host AI can aggregate whatever completed into `final-review.md`.

Override:
  AGENT_FLOW_REVIEWERS="claude,codex,gemini" forces the order/list.
  AGENT_FLOW_REVIEWERS="claude" pins all angles to Claude regardless.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_flow.cli_detect import (
    CliInfo,
    cli_by_name,
    detect_available_clis,
    detect_host_cli,
)
from agent_flow.subprocess_pool import SubprocessJob, SubprocessResult, run_parallel


@dataclass
class ReviewerJob:
    angle_id: str           # e.g. "architecture-design", "compose-stability"
    prompt: str             # full angle prompt text (rendered)
    output_path: Path       # artifact target


@dataclass
class Distribution:
    by_cli: dict[str, list[ReviewerJob]] = field(default_factory=dict)
    fallback_jobs: list[ReviewerJob] = field(default_factory=list)
    fallback_to_generic: bool = False
    insufficient_reviewers: bool = False
    host: str | None = None

    def empty(self) -> bool:
        return not self.by_cli and not self.fallback_to_generic

    def summary(self) -> str:
        if self.fallback_to_generic:
            return "generic (host handles all angles with 2+ independent reviewer verdicts)"
        parts = [f"{cli}:{len(jobs)}" for cli, jobs in self.by_cli.items() if jobs]
        if self.host:
            parts.append(f"(host={self.host})")
        return ", ".join(parts) if parts else "(no jobs)"


def resolve_review_clis() -> list[CliInfo]:
    forced = os.environ.get("AGENT_FLOW_REVIEWERS")
    if forced:
        out: list[CliInfo] = []
        for name in (n.strip() for n in forced.split(",")):
            cli = cli_by_name(name)
            if cli is not None:
                out.append(cli)
        return out
    return detect_available_clis()


def distribute(jobs: list[ReviewerJob]) -> Distribution:
    """Round-robin angles across available CLIs, host last."""
    if not jobs:
        return Distribution()
    available = resolve_review_clis()
    host = detect_host_cli()
    if not available:
        return Distribution(
            fallback_jobs=list(jobs),
            fallback_to_generic=True,
            insufficient_reviewers=True,
            host=host,
        )

    # Host last: non-host CLIs run first.
    ordered = sorted(available, key=lambda c: c.name == host)

    by_cli: dict[str, list[ReviewerJob]] = {c.name: [] for c in ordered}
    for i, job in enumerate(jobs):
        cli = ordered[i % len(ordered)]
        by_cli[cli.name].append(job)
    return Distribution(
        by_cli=by_cli,
        insufficient_reviewers=len(ordered) < 2,
        host=host,
    )


def run_distribution(distribution: Distribution, project_root: Path,
                     timeout_s: int = 600) -> list[SubprocessResult]:
    """Execute the distribution by subprocess-invoking non-host CLIs in
    parallel. The host CLI's jobs are NOT subprocess-invoked here — they
    are returned as residual jobs the host AI must handle directly (a
    subprocess from inside a host AI session would block that session).

    Returns the list of subprocess results (one per non-host job). Each
    result also gets written as `<output_path>` for partial-survival.

    Caller is responsible for surfacing residual host-CLI jobs (those need
    in-host parallelism, not subprocess).
    """
    if distribution.fallback_to_generic or distribution.empty():
        return []

    sub_jobs: list[SubprocessJob] = []
    job_to_output: dict[str, Path] = {}
    for cli_name, jobs in distribution.by_cli.items():
        if cli_name == distribution.host:
            continue  # host AI handles its own angles in-session
        cli = cli_by_name(cli_name)
        if cli is None or not jobs:
            continue
        for job in jobs:
            binary = cli.binaries[0]
            args = (*cli.invoke, job.prompt)
            sub_id = f"{cli_name}-{job.angle_id}"
            sub_jobs.append(SubprocessJob(
                job_id=sub_id, binary=binary, args=args,
                cwd=project_root, timeout_s=timeout_s,
            ))
            job_to_output[sub_id] = job.output_path

    if not sub_jobs:
        return []

    results = run_parallel(sub_jobs)
    # Write each artifact at the angle's intended output_path so the host AI
    # can aggregate them into final-review.md.
    for r in results:
        out = job_to_output.get(r.job_id)
        if out is None:
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_render_angle_result(r))
    return results


def residual_host_jobs(distribution: Distribution) -> list[ReviewerJob]:
    """Return jobs assigned to the host CLI — these need in-host parallelism
    (e.g., Claude Task tool fan-out), not subprocess invocation.
    """
    if distribution.fallback_to_generic:
        return list(distribution.fallback_jobs)
    if distribution.host is None:
        return []
    return list(distribution.by_cli.get(distribution.host, []))


def _render_angle_result(r: SubprocessResult) -> str:
    rate_limit = _rate_limit_payload(r)
    if rate_limit is not None:
        lines = [
            f"# {r.job_id}",
            "",
            "status: blocked",
            "reason: reviewer_rate_limited",
            f"reviewer: {rate_limit['reviewer']}",
            f"retry_after: {rate_limit['retry_after']}",
            f"next_command: {rate_limit['next_command']}",
            f"status_json: {json.dumps(rate_limit, sort_keys=True)}",
        ]
        if r.stdout.strip():
            lines += ["", "## stdout", "", r.stdout]
        if r.stderr.strip():
            lines += ["", "## stderr", "", r.stderr]
        return "\n".join(lines) + "\n"
    status = "OK" if r.ok else ("TIMEOUT" if r.timed_out else "ERROR")
    parts = [
        f"# {r.job_id}",
        "",
        f"- status: {status}",
        f"- duration: {r.duration_s:.2f}s",
        f"- returncode: {r.returncode}",
    ]
    if r.error:
        parts += ["", "## error", "", r.error]
    if r.stdout.strip():
        parts += ["", "## review output", "", r.stdout]
    if r.stderr.strip():
        parts += ["", "## stderr", "", r.stderr]
    return "\n".join(parts) + "\n"


def _rate_limit_payload(r: SubprocessResult) -> dict[str, str] | None:
    reviewer = r.job_id.split("-", 1)[0]
    text = "\n".join(part for part in (r.stdout, r.stderr, r.error or "") if part)
    lowered = text.lower()
    signals = {
        "claude": ("limit", "rate limit", "too many requests", "429"),
        "codex": ("rate limit", "too many requests", "429"),
        "gemini": ("quota exceeded", "resource exhausted", "429"),
    }
    reviewer_signals = signals.get(reviewer, ("rate limit", "too many requests", "429"))
    if not any(signal in lowered for signal in reviewer_signals):
        return None
    retry_after = _parse_retry_after(text)
    return {
        "status": "blocked",
        "reason": "reviewer_rate_limited",
        "reviewer": reviewer,
        "retry_after": retry_after,
        "next_command": (
            "agent-flow review retry "
            f"--reviewer {shlex.quote(reviewer)} "
            f"--retry-after {shlex.quote(retry_after)}"
        ),
    }


def _parse_retry_after(text: str) -> str:
    now = datetime.now(timezone.utc)
    relative = re.search(
        r"resets?\s+in\s+(\d+)\s*(minutes?|mins?|m|hours?|hrs?|h)\b",
        text,
        re.IGNORECASE,
    )
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        delta = timedelta(hours=amount) if unit.startswith("h") else timedelta(minutes=amount)
        return (now + delta).isoformat()

    match = re.search(
        r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
        text,
        re.IGNORECASE,
    )
    if not match:
        return (now + timedelta(hours=1)).isoformat()
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return (now + timedelta(hours=1)).isoformat()
    suffix = (match.group(3) or "").lower()
    if suffix == "pm" and hour != 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    candidate = datetime.now().astimezone().replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    ).astimezone(timezone.utc)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.isoformat()
