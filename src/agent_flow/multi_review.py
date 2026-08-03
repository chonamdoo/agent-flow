"""Multi-reviewer distribution and OS-confined parallel execution.

Every review angle runs in a separate provider process bound to the verified
linked worktree. The active host CLI participates through the same subprocess
boundary; an in-session sub-agent is never accepted as an isolation fallback.

AGENT_FLOW_REVIEWERS may add cross-host providers. Without that opt-in, two or
more independent processes from the active host satisfy reviewer independence.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_flow.cli_detect import (
    CliInfo,
    cli_by_name,
    detect_available_clis,
    detect_host_cli,
)
from agent_flow.core.hook_integrity import assert_managed_hooks_registered
from agent_flow.core.worktree_isolation import (
    WorktreeIsolationError,
    assert_leader_unchanged,
    capture_leader_snapshot,
    leader_root_for,
    sanitized_worker_env,
)
from agent_flow.subprocess_pool import SubprocessJob, SubprocessResult, run_parallel

_REVIEWER_PROVENANCE_RE = re.compile(
    r"(?m)^\s*(?:-\s*)?reviewer-source:\s*sub-agent\s*$"
)


@dataclass
class ReviewerJob:
    angle_id: str  # 예: "architecture-design", "compose-stability"
    prompt: str  # 렌더링된 전체 관점별 프롬프트
    output_path: Path  # 산출물 경로
    artifact_root: Path


@dataclass
class Distribution:
    by_cli: dict[str, list[ReviewerJob]] = field(default_factory=dict)
    fallback_jobs: list[ReviewerJob] = field(default_factory=list)
    fallback_to_generic: bool = False
    insufficient_reviewers: bool = False
    host: str | None = None
    required_job_ids: frozenset[str] = frozenset()

    def empty(self) -> bool:
        return not self.by_cli and not self.fallback_to_generic

    def summary(self) -> str:
        if self.fallback_to_generic:
            return "host-native sub-agent required (host records reviewer verdict)"
        parts = [f"{cli}:{len(jobs)}" for cli, jobs in self.by_cli.items() if jobs]
        if self.host:
            parts.append(f"(host={self.host})")
        return ", ".join(parts) if parts else "(no jobs)"


def resolve_review_clis() -> list[CliInfo]:
    forced = os.environ.get("AGENT_FLOW_REVIEWERS")
    if not forced:
        return []
    out: list[CliInfo] = []
    for name in (n.strip() for n in forced.split(",")):
        cli = cli_by_name(name)
        if cli is not None:
            out.append(cli)
    return out


def distribute(jobs: list[ReviewerJob], host: str | None = None) -> Distribution:
    """Assign every required angle to the host and duplicate optional extras."""
    if not jobs:
        return Distribution()
    host = host or detect_host_cli()
    installed_host = next(
        (cli for cli in detect_available_clis() if cli.name == host),
        None,
    )
    if installed_host is None:
        return Distribution(
            fallback_jobs=list(jobs),
            fallback_to_generic=True,
            insufficient_reviewers=True,
            host=host,
        )

    by_cli = {installed_host.name: list(jobs)}
    optional = {
        cli.name: cli
        for cli in resolve_review_clis()
        if cli.name != installed_host.name
    }
    for cli_name in optional:
        by_cli[cli_name] = [
            _optional_reviewer_job(job, cli_name=cli_name) for job in jobs
        ]
    required_job_ids = frozenset(
        _subprocess_job_id(installed_host.name, job) for job in jobs
    )
    return Distribution(
        by_cli=by_cli,
        insufficient_reviewers=len(required_job_ids) < 2,
        host=host,
        required_job_ids=required_job_ids,
    )


def _optional_reviewer_job(
    job: ReviewerJob,
    *,
    cli_name: str,
) -> ReviewerJob:
    output = job.output_path.with_name(
        f"{job.output_path.stem}-{cli_name}{job.output_path.suffix}"
    )
    return ReviewerJob(
        angle_id=f"{job.angle_id}-{cli_name}-extra",
        prompt=job.prompt,
        output_path=output,
        artifact_root=job.artifact_root,
    )


def _subprocess_job_id(cli_name: str, job: ReviewerJob) -> str:
    return f"{cli_name}-{job.angle_id}"


def _reviewer_cli_args(
    cli: CliInfo,
    *,
    prompt: str,
    project_root: Path,
) -> tuple[str, ...]:
    root = str(project_root.resolve())
    # 바깥 `run_provider`가 이미 sandbox-exec로 격리하므로 macOS에서는 중첩된
    # Codex sandbox를 초기화할 수 없다.
    if cli.name == "codex":
        return (
            *cli.invoke,
            "--ephemeral",
            "--ignore-user-config",
            "--dangerously-bypass-approvals-and-sandbox",
            "--cd",
            root,
            prompt,
        )
    if cli.name == "claude":
        return (
            *cli.invoke,
            "--permission-mode",
            "plan",
            "--no-session-persistence",
            prompt,
        )
    if cli.name == "omp":
        return (
            *cli.invoke,
            "--no-session",
            f"--cwd={root}",
            "--tools=read,grep,glob,bash",
            "--auto-approve",
            prompt,
        )
    return (*cli.invoke, prompt)


def run_distribution(
    distribution: Distribution,
    project_root: Path,
    timeout_s: int = 1200,
) -> list[SubprocessResult]:
    """Execute every assigned angle in an independent confined subprocess.

    The active host is not special here. Running it through the same provider
    boundary is what makes reviewer isolation enforceable rather than prompt
    advice.
    """
    if distribution.fallback_to_generic or distribution.empty():
        return []

    sub_jobs: list[SubprocessJob] = []
    job_to_output: dict[str, ReviewerJob] = {}
    # 리뷰어 자식은 부모 환경을 통째로 물려받으면 오염된 GIT_DIR/GIT_WORK_TREE로
    # leader 저장소에 그대로 닿는다. git 탐색 env를 벗겨서 cwd를 권위로 만든다.
    reviewer_env = sanitized_worker_env()
    for cli_name, jobs in distribution.by_cli.items():
        cli = cli_by_name(cli_name)
        if cli is None or not jobs:
            continue
        for job in jobs:
            binary = cli.binaries[0]
            args = _reviewer_cli_args(
                cli,
                prompt=job.prompt,
                project_root=project_root,
            )
            sub_id = _subprocess_job_id(cli_name, job)
            sub_jobs.append(
                SubprocessJob(
                    job_id=sub_id,
                    binary=binary,
                    args=args,
                    cwd=project_root,
                    timeout_s=timeout_s,
                    env=reviewer_env,
                    allow_workspace_writes=False,
                )
            )
            _validate_review_artifact(job)
            job_to_output[sub_id] = job

    if not sub_jobs:
        return []

    # project_root가 worktree면 그 뒤의 leader 체크아웃이 지켜야 할 대상이다.
    # leader에서 그대로 도는 리뷰라면 지킬 바깥 대상이 없어 무장하지 않는다.
    leader = leader_root_for(project_root)
    # 스냅샷보다 먼저다. 오염된 등록 상태를 기준선으로 굳히면 안 된다.
    assert_managed_hooks_registered(project_root, leader)
    leader_before = capture_leader_snapshot(leader) if leader is not None else None
    results = run_parallel(sub_jobs)
    # 기록이 tripwire보다 **먼저**다. 순서를 뒤집으면 오탐 1회에 완료된
    # 리뷰어 N명의 산출물이 통째로 사라진다.
    for r in results:
        job = job_to_output.get(r.job_id)
        if job is None:
            continue
        _write_review_artifact(job, _render_angle_result(r))
    if leader_before is not None:
        assert_leader_unchanged(
            leader,
            leader_before,
            run_id="multi-review",
            worker_root=project_root,
        )
    return results


def _validate_review_artifact(job: ReviewerJob) -> tuple[Path, Path]:
    artifact_root = job.artifact_root.resolve()
    output = job.output_path
    if (
        not artifact_root.is_dir()
        or output.parent != artifact_root
        or output.parent.resolve() != artifact_root
        or output.is_symlink()
        or (output.exists() and not output.is_file())
    ):
        raise WorktreeIsolationError(
            f"review artifact target is outside its attested run directory: {output}"
        )
    return artifact_root, output


def _write_review_artifact(job: ReviewerJob, content: str) -> None:
    artifact_root, output = _validate_review_artifact(job)
    fd, raw_temporary = tempfile.mkstemp(
        dir=artifact_root,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def residual_host_jobs(distribution: Distribution) -> list[ReviewerJob]:
    """Return jobs only when no confined reviewer process can be launched."""
    if distribution.fallback_to_generic:
        return list(distribution.fallback_jobs)
    return []


def reviewer_result_error(result: SubprocessResult) -> str | None:
    rate_limit = _rate_limit_payload(result)
    if rate_limit is not None:
        return f"rate limited until {rate_limit['retry_after']}"
    if result.timed_out:
        return "timeout"
    if result.error:
        return result.error
    if result.returncode != 0:
        return (
            f"exit {result.returncode}"
            if result.returncode is not None
            else "missing exit status"
        )
    output = result.stdout.strip()
    if not output:
        return "empty reviewer output"
    if _REVIEWER_PROVENANCE_RE.search(output) is None:
        return "reviewer output is missing provenance marker"
    return None


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
    semantic_error = reviewer_result_error(r)
    status = "OK" if semantic_error is None else ("TIMEOUT" if r.timed_out else "ERROR")
    parts = [
        f"# {r.job_id}",
        "",
        f"- status: {status}",
        f"- duration: {r.duration_s:.2f}s",
        f"- returncode: {r.returncode}",
    ]
    if r.error:
        parts += ["", "## error", "", r.error]
    elif semantic_error:
        parts += ["", "## error", "", semantic_error]
    if r.stdout.strip():
        parts += ["", "## review output", "", r.stdout]
    if r.stderr.strip():
        parts += ["", "## stderr", "", r.stderr]
    return "\n".join(parts) + "\n"


def _rate_limit_payload(r: SubprocessResult) -> dict[str, str] | None:
    reviewer = r.job_id.split("-", 1)[0]
    text = "\n".join(part for part in (r.stdout, r.stderr, r.error or "") if part)
    lowered = text.lower()
    signals = ("rate limit", "too many requests", "429")
    if reviewer == "claude":
        signals += ("you've hit your limit", "usage limit", "limit reached")
    if r.returncode == 0 and _REVIEWER_PROVENANCE_RE.search(r.stdout):
        return None
    if not any(signal in lowered for signal in signals):
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
        delta = (
            timedelta(hours=amount)
            if unit.startswith("h")
            else timedelta(minutes=amount)
        )
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
    candidate = (
        datetime.now()
        .astimezone()
        .replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )
        .astimezone(timezone.utc)
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.isoformat()
