"""Multi-reviewer distribution and OS-confined parallel execution.

Every review angle runs in a separate Claude or Codex process bound to the
verified linked worktree. An in-session sub-agent is never accepted as an
isolation fallback.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path

from agent_flow.cli_detect import (
    CliInfo,
    REVIEW_CLI_NAMES,
    cli_by_name,
    detect_available_clis,
    detect_host_cli,
)
from agent_flow.core.hook_integrity import assert_managed_hooks_registered
from agent_flow.core.leader_tripwire import leader_sweep_include_ignored_for
from agent_flow.core.worktree_isolation import (
    leader_sweep_includes_ignored,
    assert_leader_unchanged,
    capture_leader_snapshot,
    leader_root_for,
    WorktreeIsolationError,
    sanitized_worker_env,
)
from agent_flow.subprocess_pool import SubprocessJob, SubprocessResult, run_parallel

_REVIEWER_PROVENANCE_RE = re.compile(
    r"(?m)^\s*(?:-\s*)?reviewer-source:\s*sub-agent\s*$"
)
_PROVIDER_FAILURE_RE = re.compile(
    r"(?m)^(?:- status: (?:ERROR|TIMEOUT)|reason: reviewer_rate_limited)$"
)
_REVIEWER_SANDBOX_FAILURE_SIGNALS = (
    "sandbox_apply: operation not permitted",
)


@dataclass
class ReviewerJob:
    angle_id: str           # e.g. "architecture-design", "compose-stability"
    prompt: str             # full angle prompt text (rendered)
    output_path: Path       # artifact target
    artifact_root: Path


@dataclass
class Distribution:
    by_cli: dict[str, list[ReviewerJob]] = field(default_factory=dict)
    fallback_jobs: list[ReviewerJob] = field(default_factory=list)
    fallback_to_generic: bool = False
    insufficient_reviewers: bool = False
    host: str | None = None
    required_job_ids: frozenset[str] = frozenset()
    accept_any_provider: bool = False
    # 이전 시도의 실패 artifact 때문에 제외된 provider. 조용히 한 provider로
    # 줄어드는 것을 host가 알아야 한다.
    skipped_providers: tuple[str, ...] = ()

    def empty(self) -> bool:
        return not self.by_cli and not self.fallback_to_generic

    def summary(self) -> str:
        if self.fallback_to_generic:
            return "no Claude/Codex reviewer CLI available (multi-review blocked)"
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
        if cli is not None and cli.name in REVIEW_CLI_NAMES:
            out.append(cli)
    return out


def distribute_final_review(
    jobs: list[ReviewerJob],
    host: str | None = None,
) -> Distribution:
    """Assign final-review angles to available Claude and Codex processes."""
    if not jobs:
        return Distribution()
    host = host or detect_host_cli()
    # AGENT_FLOW_REVIEWERS는 pool을 **좁히는** 스위치다. 여기서 무시하면 문서가
    # 약속한 좁히기가 final-review에서만 조용히 풀린다.
    narrowed = {cli.name for cli in resolve_review_clis()}
    available = {
        cli.name: cli
        for cli in detect_available_clis()
        if cli.name in REVIEW_CLI_NAMES
        and (not narrowed or cli.name in narrowed)
    }
    by_cli: dict[str, list[ReviewerJob]] = {}
    latched: dict[str, list[ReviewerJob]] = {}
    for cli_name in REVIEW_CLI_NAMES:
        if cli_name not in available:
            continue
        assigned = [
            _reviewer_job_for_cli(job, cli_name=cli_name)
            for job in jobs
        ]
        if any(_existing_provider_failure(job) for job in assigned):
            latched[cli_name] = assigned
            continue
        by_cli[cli_name] = assigned
    # 이전 시도에서 실패한 provider는 건너뛴다. 단 모두 실패했다면 건너뛰기가
    # 보호가 아니라 교착이다 — per-angle 실패 artifact는 phase 재시도에서 지워지지
    # 않으므로, 그대로 두면 fix-loop을 돌아도 final-review가 영구히 raise한다.
    if not by_cli and latched:
        by_cli = latched
        latched = {}
    if not by_cli:
        return Distribution(
            fallback_jobs=list(jobs),
            fallback_to_generic=True,
            insufficient_reviewers=True,
            host=host,
            accept_any_provider=True,
        )
    required_job_ids = frozenset(
        _subprocess_job_id(cli_name, job)
        for cli_name, assigned in by_cli.items()
        for job in assigned
    )
    # 독립성 기준은 process 수다. angle 1개라도 provider 2개면 독립 reviewer 2개다.
    reviewer_processes = sum(len(assigned) for assigned in by_cli.values())
    return Distribution(
        by_cli=by_cli,
        insufficient_reviewers=reviewer_processes < 2,
        host=host,
        required_job_ids=required_job_ids,
        accept_any_provider=True,
        skipped_providers=tuple(latched),
    )


def distribute(jobs: list[ReviewerJob], host: str | None = None) -> Distribution:
    """Assign every required angle to the host and duplicate optional extras."""
    if not jobs:
        return Distribution()
    host = host or detect_host_cli()
    available = {
        cli.name: cli
        for cli in detect_available_clis()
        if cli.name in REVIEW_CLI_NAMES
    }
    primary_name = (
        host
        if host in available
        else next((name for name in REVIEW_CLI_NAMES if name in available), None)
    )
    primary_cli = available.get(primary_name) if primary_name is not None else None
    if primary_cli is None:
        return Distribution(
            fallback_jobs=list(jobs),
            fallback_to_generic=True,
            insufficient_reviewers=True,
            host=host,
        )

    by_cli = {primary_cli.name: list(jobs)}
    optional = {
        cli.name: cli
        for cli in resolve_review_clis()
        if cli.name != primary_cli.name
    }
    for cli_name in optional:
        by_cli[cli_name] = [
            _optional_reviewer_job(job, cli_name=cli_name)
            for job in jobs
        ]
    required_job_ids = frozenset(
        _subprocess_job_id(primary_cli.name, job)
        for job in jobs
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


def _reviewer_job_for_cli(
    job: ReviewerJob,
    *,
    cli_name: str,
) -> ReviewerJob:
    output = job.output_path.with_name(
        f"{job.output_path.stem}-{cli_name}{job.output_path.suffix}"
    )
    return ReviewerJob(
        angle_id=job.angle_id,
        prompt=job.prompt,
        output_path=output,
        artifact_root=job.artifact_root,
    )


def _existing_provider_failure(job: ReviewerJob) -> str | None:
    """이전 시도가 남긴 실패 artifact. 판정은 **header 블록**만 본다 —
    리뷰 본문이 `- status: ERROR`를 인용해도 provider가 탈락하면 안 된다."""
    output = job.output_path
    if output.is_symlink() or not output.is_file():
        return None
    header_lines: list[str] = []
    for line in output.read_text(encoding="utf-8")[:1024].splitlines():
        if line.startswith("## "):
            break
        header_lines.append(line)
    match = _PROVIDER_FAILURE_RE.search("\n".join(header_lines))
    return match.group(0) if match is not None else None


def _subprocess_job_id(cli_name: str, job: ReviewerJob) -> str:
    return f"{cli_name}-{job.angle_id}"


def _reviewer_cli_args(
    cli: CliInfo,
    *,
    prompt: str,
    project_root: Path,
) -> tuple[str, ...]:
    root = str(project_root.resolve())
    if cli.name == "codex":
        # 바깥 provider sandbox가 이미 read-only를 강제한다. Codex sandbox까지 중첩하면 macOS sandbox_apply가 실패한다.
        return (
            *cli.invoke,
            "--ephemeral",
            "--ignore-user-config",
            "--sandbox",
            "danger-full-access",
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
    return (*cli.invoke, prompt)


def run_distribution(
    distribution: Distribution,
    project_root: Path,
    timeout_s: int = 600,
) -> list[SubprocessResult]:
    """Final-review probes each selected provider once in parallel. A provider
    whose probe fails is excluded from the remaining angle wave; other
    multi-review phases retain the single parallel wave.
    """
    if distribution.fallback_to_generic or distribution.empty():
        return []

    sub_jobs_by_cli: dict[str, list[SubprocessJob]] = {}
    job_to_output: dict[str, ReviewerJob] = {}
    # 리뷰어 자식은 부모 환경을 통째로 물려받으면 오염된 GIT_DIR/GIT_WORK_TREE로
    # leader 저장소에 그대로 닿는다. git 탐색 env를 벗겨서 cwd를 권위로 만든다.
    reviewer_env = sanitized_worker_env()
    for cli_name, jobs in distribution.by_cli.items():
        cli = cli_by_name(cli_name)
        if cli is None or not jobs:
            continue
        assigned: list[SubprocessJob] = []
        for job in jobs:
            binary = cli.binaries[0]
            args = _reviewer_cli_args(
                cli,
                prompt=job.prompt,
                project_root=project_root,
            )
            sub_id = _subprocess_job_id(cli_name, job)
            assigned.append(SubprocessJob(
                job_id=sub_id, binary=binary, args=args,
                cwd=project_root, timeout_s=timeout_s,
                env=reviewer_env,
                allow_workspace_writes=False,
            ))
            _validate_review_artifact(job)
            job_to_output[sub_id] = job
        sub_jobs_by_cli[cli_name] = assigned

    if not sub_jobs_by_cli:
        return []

    # project_root가 worktree면 그 뒤의 leader 체크아웃이 지켜야 할 대상이다.
    # leader에서 그대로 도는 리뷰라면 지킬 바깥 대상이 없어 무장하지 않는다.
    leader = leader_root_for(project_root)
    # 스냅샷보다 먼저다. 오염된 등록 상태를 기준선으로 굳히면 안 된다.
    assert_managed_hooks_registered(project_root, leader)
    # 범위는 profile이 정한다. 여기서 기본값으로 두면 `tracked-only`를 선언한
    # 프로젝트도 이 경로에서만 전수 sweep으로 돌아 막힌다 — reviewer subprocess를
    # 병렬로 돌리는 이 창이 phase 중 가장 길어서 leader의 gitignored 산출물이
    # 바뀔 확률도 가장 높다. 즉 고치려던 마찰이 정확히 여기서 재발한다.
    leader_before = None
    if leader is not None:
        include_ignored = leader_sweep_include_ignored_for(leader)
        leader_before = capture_leader_snapshot(leader, include_ignored=include_ignored)
    if distribution.accept_any_provider:
        probes = [jobs[0] for jobs in sub_jobs_by_cli.values()]
        results = run_parallel(probes)
        available = {
            result.job_id.split("-", 1)[0]
            for result in results
            if reviewer_result_error(result) is None
        }
        remaining = [
            job
            for cli_name, jobs in sub_jobs_by_cli.items()
            if cli_name in available
            for job in jobs[1:]
        ]
        if remaining:
            results.extend(run_parallel(remaining))
    else:
        results = run_parallel([
            job
            for jobs in sub_jobs_by_cli.values()
            for job in jobs
        ])
    # Write each artifact at the angle's intended output_path so the host AI
    # can aggregate them into final-review.md.
    #
    # 기록이 tripwire보다 **먼저**다. 순서를 뒤집으면 오탐 1회에 완료된
    # 리뷰어 N명의 산출물이 통째로 사라진다.
    for r in results:
        job = job_to_output.get(r.job_id)
        if job is None:
            continue
        _write_review_artifact(job, _render_angle_result(r))
    if leader_before is not None:
        # 범위는 기록에서 되읽는다. 여기서 다시 profile을 해석하면 리뷰가 도는
        # 동안 선언이 바뀌었을 때 baseline과 관측이 서로 다른 범위가 되고, 그
        # 불일치는 leader를 아무도 건드리지 않아도 항상 diff로 나온다.
        assert_leader_unchanged(
            leader,
            leader_before,
            run_id="multi-review",
            worker_root=project_root,
            include_ignored=leader_sweep_includes_ignored(leader_before.scope),
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
    # 리뷰어가 본문에서 sandbox 오류 문자열을 **인용**한 정상 리뷰를 실패로 채점하면
    # 이 저장소를 리뷰할 때마다 provider가 탈락한다. rate limit 판정과 같은 예외를
    # 쓴다 — 정상 종료 + provenance면 인용으로 본다. 실제 sandbox_apply 실패는 CLI가
    # 아예 실행되지 못하므로 provenance가 남지 않는다.
    if not (
        result.returncode == 0
        and _REVIEWER_PROVENANCE_RE.search(result.stdout)
    ):
        diagnostics = "\n".join(
            part
            for part in (result.stdout, result.error or "")
            if part
        ).lower()
        if any(signal in diagnostics for signal in _REVIEWER_SANDBOX_FAILURE_SIGNALS):
            return "reviewer sandbox unavailable"
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
