"""Multi-reviewer distribution and OS-confined parallel execution.

Every review angle runs in a separate Claude or Codex process bound to the
verified linked worktree. An in-session sub-agent is never accepted as an
isolation fallback.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, TypedDict

from agent_flow.cli_detect import (
    CliInfo,
    REVIEW_CLI_NAMES,
    cli_by_name,
    detect_available_clis,
    detect_host_cli,
)
from agent_flow.core.hook_integrity import assert_managed_hooks_registered
from agent_flow.core.leader_tripwire import leader_sweep_include_ignored_for
from agent_flow.core.profiles import active_profile_ids, load_profile_payload
from agent_flow.core.reviewer_launch import (
    LaunchCandidate,
    ReviewerLaunchError,
    matching_reviewer_rule,
    select_launch_candidate,
)
from agent_flow.core.review_evidence import (
    REVIEW_LAUNCH_DIGEST_CHARS,
    ReviewerOutcome,
    ReviewStatus,
    reviewer_output_verdict,
)
from agent_flow.core.worktree_isolation import (
    WorktreeIsolationError,
    assert_leader_unchanged,
    capture_leader_snapshot,
    leader_root_for,
    leader_sweep_includes_ignored,
    sanitized_worker_env,
    validate_run_artifact_target,
    write_run_artifact_text,
)
from agent_flow.subprocess_pool import SubprocessJob, SubprocessResult, run_parallel

_REVIEWER_PROVENANCE_RE = re.compile(
    r"(?m)^reviewer-source:\s*sub-agent[ \t]*$"
)
_REVIEWER_SANDBOX_FAILURE_SIGNALS = (
    "sandbox_apply: operation not permitted",
)
# final-review를 고르는 자리(`adapters/hosted.py`)와 launch 선언의 `match.phase`를
# 비교하는 자리가 같은 값을 봐야 한다. 두 벌로 두면 한쪽 오타가 선언을 조용히
# 죽인다 — 그래서 소비자가 이 상수를 import한다.
FINAL_REVIEW_PHASE_ID = "final-review"
# 선언하지 않은 값을 artifact에 적는 말. 빈 칸으로 두면 "기록이 없다"와
# "선언이 없다"가 같은 모양이 된다.
_UNSPECIFIED = "unspecified"
_UNKNOWN = "unknown"


@dataclass
class ReviewerJob:
    angle_id: str           # e.g. "architecture-design", "compose-stability"
    prompt: str
    output_path: Path       # artifact target
    artifact_root: Path
    # fan-out으로 파생된 job은 angle_id에 provider 접미사가 붙는다. launch 선언은
    # 파생 이름이 아니라 사람이 선언한 angle을 가리키므로 원본을 함께 들고 간다.
    base_angle_id: str | None = None
    prompt_by_provider: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if bool(self.prompt) == bool(self.prompt_by_provider):
            raise ValueError(
                "reviewer job must carry exactly one of prompt or prompt_by_provider"
            )

    @property
    def match_angle_id(self) -> str:
        return self.base_angle_id or self.angle_id

    def prompt_for(self, cli_name: str) -> str:
        """Return the prompt for the assigned provider.

        An unknown provider is a declaration drift: normal provider names and map keys
        both come from `REVIEW_CLI_NAMES`.
        """
        if not self.prompt_by_provider:
            return self.prompt
        try:
            return self.prompt_by_provider[cli_name]
        except KeyError as exc:
            raise ReviewerLaunchError(
                f"reviewer prompt has no provider render for {cli_name!r}"
            ) from exc


@dataclass
class Distribution:
    by_cli: dict[str, list[ReviewerJob]] = field(default_factory=dict)
    fallback_jobs: list[ReviewerJob] = field(default_factory=list)
    fallback_to_generic: bool = False
    insufficient_reviewers: bool = False
    host: str | None = None
    required_job_ids: frozenset[str] = frozenset()
    accept_any_provider: bool = False
    # launch 선언의 `match.phase`가 비교하는 값. 알 수 없으면 None이고, 그때는
    # phase를 지정한 rule이 매치되지 않는다(모르는 phase를 추측해 맞추지 않는다).
    phase_id: str | None = None

    def empty(self) -> bool:
        return not self.by_cli and not self.fallback_to_generic

    def expected_job_ids_by_provider(self) -> dict[str, list[str]]:
        return {
            provider: [
                review_job_id(provider, job)
                for job in jobs
            ]
            for provider, jobs in self.by_cli.items()
        }

    def summary(self) -> str:
        if self.fallback_to_generic:
            return "no Claude/Codex reviewer CLI available (multi-review blocked)"
        parts = [f"{cli}:{len(jobs)}" for cli, jobs in self.by_cli.items() if jobs]
        if self.host:
            parts.append(f"(host={self.host})")
        return ", ".join(parts) if parts else "(no jobs)"


@dataclass(frozen=True)
class ReviewExecution:
    results: tuple[SubprocessResult, ...] = ()
    skipped_providers: tuple[str, ...] = ()
    outcomes: tuple[ReviewerOutcome, ...] = ()


@dataclass(frozen=True)
class ResolvedLaunch:
    """한 reviewer subprocess의 실행 identity. artifact 머리말의 근거다."""

    provider: str          # "claude" | "codex"
    model: str | None
    effort: str | None
    argv: tuple[str, ...]
    cli_version: str | None


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


def _configured_reviewer_names() -> set[str] | None:
    if not os.environ.get("AGENT_FLOW_REVIEWERS"):
        return None
    return {cli.name for cli in resolve_review_clis()}

def eligible_reviewer_names() -> tuple[str, ...]:
    narrowed = _configured_reviewer_names()
    available = {
        cli.name
        for cli in detect_available_clis()
        if cli.name in REVIEW_CLI_NAMES
        and (narrowed is None or cli.name in narrowed)
    }
    return tuple(name for name in REVIEW_CLI_NAMES if name in available)


def _has_sufficient_reviewer_processes(
    by_cli: dict[str, list[ReviewerJob]],
) -> bool:
    return sum(len(assigned) for assigned in by_cli.values()) >= 2


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
    narrowed = _configured_reviewer_names()
    available = {
        cli.name: cli
        for cli in detect_available_clis()
        if cli.name in REVIEW_CLI_NAMES
        and (narrowed is None or cli.name in narrowed)
    }
    by_cli: dict[str, list[ReviewerJob]] = {}
    for cli_name in REVIEW_CLI_NAMES:
        if cli_name not in available:
            continue
        by_cli[cli_name] = [
            _reviewer_job_for_cli(job, cli_name=cli_name)
            for job in jobs
        ]
    if not by_cli:
        return Distribution(
            fallback_jobs=list(jobs),
            fallback_to_generic=True,
            insufficient_reviewers=True,
            host=host,
            phase_id=FINAL_REVIEW_PHASE_ID,
        )
    _assert_unique_output_paths(by_cli)
    return Distribution(
        by_cli=by_cli,
        insufficient_reviewers=not _has_sufficient_reviewer_processes(by_cli),
        host=host,
        accept_any_provider=True,
        phase_id=FINAL_REVIEW_PHASE_ID,
    )


def distribute(
    jobs: list[ReviewerJob],
    host: str | None = None,
    phase_id: str | None = None,
) -> Distribution:
    """Assign every required angle to one provider and fan out optional peers.

    `phase_id`는 launch 선언의 `match.phase`가 비교하는 값이다. 호출부가 도는
    phase를 알고 있으므로 여기서 추측하지 않는다 — 넘기지 않으면 phase를 지정한
    rule은 발동하지 않는다.
    """
    if not jobs:
        return Distribution()
    narrowed = _configured_reviewer_names()
    available = {
        cli.name: cli
        for cli in detect_available_clis()
        if cli.name in REVIEW_CLI_NAMES
        and (narrowed is None or cli.name in narrowed)
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
            phase_id=phase_id,
        )

    by_cli = {
        primary_cli.name: [
            _bound_reviewer_job(job, cli_name=primary_cli.name) for job in jobs
        ]
    }
    optional = {
        cli_name: cli
        for cli_name, cli in available.items()
        if cli_name != primary_cli.name
    }
    for cli_name in optional:
        by_cli[cli_name] = [
            _optional_reviewer_job(job, cli_name=cli_name)
            for job in jobs
        ]
    _assert_unique_output_paths(by_cli)
    required_job_ids = frozenset(
        review_job_id(primary_cli.name, job)
        for job in by_cli[primary_cli.name]
    )
    return Distribution(
        by_cli=by_cli,
        insufficient_reviewers=not _has_sufficient_reviewer_processes(by_cli),
        host=host,
        required_job_ids=required_job_ids,
        phase_id=phase_id,
    )


def _bound_reviewer_job(
    job: ReviewerJob,
    *,
    cli_name: str,
) -> ReviewerJob:
    return replace(
        job,
        prompt=job.prompt_for(cli_name),
        prompt_by_provider={},
    )


def _optional_reviewer_job(
    job: ReviewerJob,
    *,
    cli_name: str,
) -> ReviewerJob:
    output = job.output_path.with_name(
        f"{job.output_path.stem}-extra-{cli_name}{job.output_path.suffix}"
    )
    return replace(
        job,
        angle_id=f"{job.angle_id}-{cli_name}-extra",
        prompt=job.prompt_for(cli_name),
        prompt_by_provider={},
        output_path=output,
        base_angle_id=job.match_angle_id,
    )


def _reviewer_job_for_cli(
    job: ReviewerJob,
    *,
    cli_name: str,
) -> ReviewerJob:
    output = job.output_path.with_name(
        f"{job.output_path.stem}-{cli_name}{job.output_path.suffix}"
    )
    return replace(
        job,
        prompt=job.prompt_for(cli_name),
        prompt_by_provider={},
        output_path=output,
        base_angle_id=job.match_angle_id,
    )


def _assert_unique_output_paths(
    by_cli: dict[str, list[ReviewerJob]],
) -> None:
    seen: dict[str, tuple[str, str]] = {}
    for cli_name, assigned in by_cli.items():
        for job in assigned:
            key = os.path.normcase(str(job.output_path.resolve()))
            previous = seen.get(key)
            if previous is not None:
                raise WorktreeIsolationError(
                    "reviewer output path collision: "
                    f"{job.output_path} assigned to "
                    f"{previous[0]}:{previous[1]} and {cli_name}:{job.angle_id}"
                )
            seen[key] = (cli_name, job.angle_id)


def review_job_id(cli_name: str, job: ReviewerJob) -> str:
    return f"{cli_name}-{job.angle_id}"


def _reviewer_cli_args(
    cli: CliInfo,
    *,
    prompt: str,
    project_root: Path,
    model: str | None = None,
    effort: str | None = None,
) -> tuple[str, ...]:
    """provider별 플래그 번역의 유일한 자리.

    실측한 철자만 쓴다(`claude --help`, `codex exec --help`, 2026-08 기준):
    claude는 `--model <model>`과 `--effort <level>`을 받고, codex는
    `-m/--model <MODEL>`은 받지만 effort 전용 플래그가 없어 `-c key=value`
    (TOML override)로만 `model_reasoning_effort`를 준다. 없는 플래그를 발명하는
    대신, 받을 자리가 없는 provider는 아래에서 선언을 거부한다.
    """
    root = str(project_root.resolve())
    if cli.name == "codex":
        # 바깥 provider sandbox가 이미 read-only를 강제한다. Codex sandbox까지 중첩하면 macOS sandbox_apply가 실패한다.
        return (
            *cli.invoke,
            *_codex_identity_args(model, effort),
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
            *_claude_identity_args(model, effort),
            "--safe-mode",
            "--permission-mode",
            "plan",
            "--no-session-persistence",
            prompt,
        )
    if model is not None or effort is not None:
        raise ReviewerLaunchError(
            f"provider {cli.name!r} has no model/effort flags: "
            "declare only claude or codex candidates"
        )
    return (*cli.invoke, prompt)


def _claude_identity_args(model: str | None, effort: str | None) -> tuple[str, ...]:
    args: list[str] = []
    if model is not None:
        args += ["--model", model]
    if effort is not None:
        args += ["--effort", effort]
    return tuple(args)


def _codex_identity_args(model: str | None, effort: str | None) -> tuple[str, ...]:
    args: list[str] = []
    if model is not None:
        args += ["--model", model]
    if effort is not None:
        # codex에는 effort 플래그가 없다. 문서화된 `-c <key>=<value>` override로
        # config의 `model_reasoning_effort`를 지정하는 것이 유일한 경로다.
        # 값은 TOML 문자열로 적어야 bare word 파싱 실패에 의존하지 않는다.
        args += ["-c", f'model_reasoning_effort="{effort}"']
    return tuple(args)


def _resolve_reviewer_launch(
    *,
    phase_id: str | None,
    angle_id: str,
    profile: dict[str, Any] | None,
    cli: CliInfo,
    prompt: str,
    project_root: Path,
) -> ResolvedLaunch:
    """launch 정책을 해석하는 유일한 자리.

    선언이 없으면 예전 argv 그대로다. 선언이 있으면 그 model/effort가 argv에
    들어가고, 실제로 무엇으로 띄웠는지가 `ResolvedLaunch`로 나와 artifact에 적힌다.

    provider는 바꾸지 않는다. 배정은 `distribute()`가 했고, 여기서 다른 provider로
    갈아 끼우면 artifact 파일 이름(`<angle>-codex.md`)이 실제 실행과 어긋난다.
    """
    rule = matching_reviewer_rule(profile, phase_id=phase_id, angle_id=angle_id)
    candidate: LaunchCandidate | None = (
        None if rule is None else select_launch_candidate(rule, provider=cli.name)
    )
    return _launch_for(
        cli,
        model=candidate.model if candidate else None,
        effort=candidate.effort if candidate else None,
        prompt=prompt,
        project_root=project_root,
    )


def _launch_for(
    cli: CliInfo,
    *,
    model: str | None,
    effort: str | None,
    prompt: str,
    project_root: Path,
) -> ResolvedLaunch:
    return ResolvedLaunch(
        provider=cli.name,
        model=model,
        effort=effort,
        argv=_reviewer_cli_args(
            cli,
            prompt=prompt,
            project_root=project_root,
            model=model,
            effort=effort,
        ),
        cli_version=_cli_version(cli.binaries[0]),
    )


@lru_cache(maxsize=None)
def _cli_version(binary: str) -> str | None:
    """관측된 CLI 버전. 못 읽으면 None이고, 그것이 실행을 막지는 않는다.

    디코딩까지 여기서 막는다. `text=True`에 encoding을 주지 않으면 로케일 기본
    코덱으로 디코딩하고, 실패는 `UnicodeDecodeError`(= `ValueError`)라서 아래
    핸들러를 빠져나가 reviewer가 하나도 뜨기 전에 wave 전체를 중단시킨다.
    """
    try:
        result = subprocess.run(
            (binary, "--version"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    first = result.stdout.strip().splitlines()
    return first[0].strip() if first else None


def _reviewer_launch_profile(config_root: Path) -> dict[str, Any]:
    """launch 선언을 들고 있는 active profile payload. 없으면 빈 mapping.

    `config_root`는 선언 파일이 있는 곳이다 — worker checkout이 아니라 leader다.
    `branching.worktree: required` 프로젝트의 managed checkout에는 `.agent-flow/`가
    아예 없어서(gitignored) checkout을 소스로 쓰면 kit.json도 override도 못 읽고
    선언 전체가 조용히 무시된다. run의 다른 profile 소비자와 같은 기준을 쓴다
    (`Runner.__init__`의 `resolve_profile(self.kit_root, self.config_root)`).

    두 profile이 동시에 선언하면 어느 쪽이 이기는지 선언된 규칙이 없다. 임의로
    고르면 android+react-native 저장소에서 아무도 고르지 않은 model로 리뷰가 돈다.
    막는다.

    읽기 실패(파일이 없거나 못 읽음)는 "선언 없음"으로 접는다 — 선언한 적 없는
    저장소의 리뷰를 profile 입출력 문제로 막지 않는다. 선언이 **틀린** 경우는
    `load_profile_payload`가 그대로 올려 보낸다.
    """
    requested = os.environ.get("AGENT_FLOW_PROFILE") or "auto"
    declared: list[tuple[str, dict[str, Any]]] = []
    for profile_id in active_profile_ids(config_root, requested):
        try:
            payload = load_profile_payload(
                profile_id, config_root, fallback_unknown_to_generic=True
            )
        except OSError:
            continue
        if payload.get("execution") is not None:
            declared.append((profile_id, payload))
    if len(declared) > 1:
        raise ReviewerLaunchError(
            "active profiles declare conflicting execution blocks: "
            + ", ".join(profile_id for profile_id, _ in declared)
        )
    return declared[0][1] if declared else {}


def run_distribution(
    distribution: Distribution,
    project_root: Path,
    timeout_s: int = 600,
    config_root: Path | None = None,
) -> ReviewExecution:
    """Run one isolated review wave and report observed provider outcomes."""
    if distribution.fallback_to_generic or distribution.empty():
        return ReviewExecution()

    sub_jobs_by_cli: dict[str, list[SubprocessJob]] = {}
    job_to_output: dict[str, ReviewerJob] = {}
    launch_by_job: dict[str, ResolvedLaunch] = {}
    # 리뷰어 자식은 부모 환경을 통째로 물려받으면 오염된 GIT_DIR/GIT_WORK_TREE로
    # leader 저장소에 그대로 닿는다. git 탐색 env를 벗겨서 cwd를 권위로 만든다.
    reviewer_env = sanitized_worker_env()
    # project_root가 worktree면 그 뒤의 leader 체크아웃이 지켜야 할 대상이다.
    # leader에서 그대로 도는 리뷰라면 지킬 바깥 대상이 없어 무장하지 않는다.
    leader = leader_root_for(project_root)
    # 선언은 wave당 한 번만 읽는다. job마다 다시 읽으면 리뷰가 도는 동안 선언이
    # 바뀌었을 때 같은 wave의 reviewer들이 서로 다른 정책으로 돈다.
    #
    # 소스는 호출부가 준 config root다. 주지 않으면 leader로 접는다 — managed
    # checkout에는 `.agent-flow/`가 없어서 checkout을 소스로 쓰면 선언이 사라진다.
    launch_profile = _reviewer_launch_profile(config_root or leader or project_root)
    for cli_name, jobs in distribution.by_cli.items():
        cli = cli_by_name(cli_name)
        if cli is None or not jobs:
            continue
        assigned: list[SubprocessJob] = []
        for job in jobs:
            launch = _resolve_reviewer_launch(
                phase_id=distribution.phase_id,
                angle_id=job.match_angle_id,
                profile=launch_profile,
                cli=cli,
                prompt=job.prompt,
                project_root=project_root,
            )
            sub_id = review_job_id(cli_name, job)
            assigned.append(SubprocessJob(
                job_id=sub_id, binary=cli.binaries[0], args=launch.argv,
                cwd=project_root, timeout_s=timeout_s,
                env=reviewer_env,
                allow_workspace_writes=False,
            ))
            _validate_review_artifact(job)
            job_to_output[sub_id] = job
            launch_by_job[sub_id] = launch
        sub_jobs_by_cli[cli_name] = assigned

    if not sub_jobs_by_cli:
        return ReviewExecution()

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
    skipped_providers: tuple[str, ...] = ()
    if distribution.accept_any_provider:
        probes = [jobs[0] for jobs in sub_jobs_by_cli.values()]
        results = run_parallel(probes)
        available = {
            result.job_id.split("-", 1)[0]
            for result in results
            if reviewer_provider_error(result) is None
        }
        skipped_providers = tuple(
            cli_name
            for cli_name in sub_jobs_by_cli
            if cli_name not in available
        )
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
    artifact_digests: dict[str, str] = {}
    for result in results:
        job = job_to_output.get(result.job_id)
        if job is None:
            continue
        rendered = _render_angle_result(
            result,
            launch=launch_by_job[result.job_id],
            prompt=job.prompt,
        )
        _write_review_artifact(job, rendered)
        artifact_digests[result.job_id] = hashlib.sha256(
            rendered.encode("utf-8")
        ).hexdigest()
    outcomes = tuple(
        _reviewer_outcome(
            result,
            job=job_to_output[result.job_id],
            launch=launch_by_job[result.job_id],
            artifact_sha256=artifact_digests[result.job_id],
            required=(
                not distribution.accept_any_provider
                and result.job_id in distribution.required_job_ids
            ),
        )
        for result in results
        if result.job_id in artifact_digests
    )
    if leader is not None and leader_before is not None:
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
    return ReviewExecution(
        results=tuple(results),
        skipped_providers=skipped_providers,
        outcomes=outcomes,
    )


def _reviewer_outcome(
    result: SubprocessResult,
    *,
    job: ReviewerJob,
    launch: ResolvedLaunch,
    artifact_sha256: str,
    required: bool,
) -> ReviewerOutcome:
    error = reviewer_result_error(result)
    status: ReviewStatus = (
        "timeout" if result.timed_out else ("error" if error else "ok")
    )
    verdict = (
        reviewer_output_verdict(result.stdout.strip())
        if error is None
        else None
    )
    # This is an internal drift tripwire: the error classifier and persisted
    # outcome must never normalize the same stdout differently.
    if (status == "ok") != (verdict is not None):
        raise ValueError(
            f"reviewer outcome status/verdict mismatch: {result.job_id}"
        )
    return ReviewerOutcome(
        job_id=result.job_id,
        provider=launch.provider,
        model=launch.model or _UNSPECIFIED,
        effort=launch.effort or _UNSPECIFIED,
        status=status,
        verdict=verdict,
        required=required,
        artifact=job.output_path.name,
        artifact_sha256=artifact_sha256,
        prompt_digest=_text_digest(job.prompt),
        argv_digest=_argv_digest(launch.argv),
    )


def _validate_review_artifact(job: ReviewerJob) -> tuple[Path, Path]:
    return validate_run_artifact_target(job.artifact_root, job.output_path)


def _write_review_artifact(job: ReviewerJob, content: str) -> None:
    artifact_root, output = _validate_review_artifact(job)
    write_run_artifact_text(artifact_root, output, content)


def residual_host_jobs(distribution: Distribution) -> list[ReviewerJob]:
    """Return jobs only when no confined reviewer process can be launched."""
    if distribution.fallback_to_generic:
        return list(distribution.fallback_jobs)
    return []


def reviewer_provider_error(result: SubprocessResult) -> str | None:
    rate_limit = _rate_limit_payload(result)
    if rate_limit is not None:
        return f"rate limited until {rate_limit['retry_after']}"
    if result.timed_out:
        return "timeout"
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
            for part in (result.stdout, result.stderr, result.error or "")
            if part
        ).lower()
        if any(signal in diagnostics for signal in _REVIEWER_SANDBOX_FAILURE_SIGNALS):
            return "reviewer sandbox unavailable"
    if result.error:
        return result.error
    if result.returncode != 0:
        return (
            f"exit {result.returncode}"
            if result.returncode is not None
            else "missing exit status"
        )
    return None


def reviewer_result_error(result: SubprocessResult) -> str | None:
    provider_error = reviewer_provider_error(result)
    if provider_error is not None:
        return provider_error
    output = result.stdout.strip()
    if not output:
        return "empty reviewer output"
    if _REVIEWER_PROVENANCE_RE.search(output) is None:
        return "reviewer output is missing provenance marker"
    if reviewer_output_verdict(output) is None:
        return "reviewer output is missing one unambiguous verdict"
    return None


def _render_angle_result(
    r: SubprocessResult,
    *,
    launch: ResolvedLaunch,
    prompt: str | None = None,
) -> str:
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
        # rate limit도 실행 기록이다. 어떤 model로 시도해서 막혔는지가 다음 수를
        # 정하므로, 여기서 머리말을 빼면 가장 필요한 자리에서 근거가 사라진다.
        # 기존 `key: value` 줄은 파서가 읽으므로 순서를 바꾸지 않고 뒤에 붙인다.
        lines += _launch_provenance_lines(launch, prompt)
        if r.stdout.strip():
            lines += ["", "## stdout", "", *_markdown_code_block(r.stdout)]
        if r.stderr.strip():
            lines += ["", "## stderr", "", *_markdown_code_block(r.stderr)]
        return "\n".join(lines) + "\n"
    semantic_error = reviewer_result_error(r)
    status = "OK" if semantic_error is None else ("TIMEOUT" if r.timed_out else "ERROR")
    parts = [
        f"# {r.job_id}",
        "",
        f"- status: {status}",
        f"- duration: {r.duration_s:.2f}s",
        f"- returncode: {r.returncode}",
        *_launch_provenance_lines(launch, prompt),
    ]
    if r.error:
        parts += ["", "## error", "", r.error]
    elif semantic_error:
        parts += ["", "## error", "", semantic_error]
    if r.stdout.strip():
        parts += ["", "## review output", "", *_markdown_code_block(r.stdout)]
    if r.stderr.strip():
        parts += ["", "## stderr", "", *_markdown_code_block(r.stderr)]
    if semantic_error is None:
        verdict = reviewer_output_verdict(r.stdout.strip())
        if verdict is None:
            raise ValueError(
                f"reviewer output lost its verdict during rendering: {r.job_id}"
            )
        parts += ["", "## Reviewer verdict", "", f"verdict: {verdict}"]
    return "\n".join(parts) + "\n"


def _markdown_code_block(text: str) -> list[str]:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}text", text, fence]


def _launch_provenance_lines(
    launch: ResolvedLaunch | None, prompt: str | None
) -> list[str]:
    """무엇으로 띄웠는지를 머리말에 남긴다.

    argv 원문은 적지 않는다. 경로와 prompt 본문이 통째로 들어가 artifact가 부풀고
    호스트 경로가 리뷰 산출물로 샌다. digest면 "이 argv였다"를 대조하는 데 충분하다.
    """
    lines: list[str] = []
    if launch is not None:
        lines += [
            f"- provider: {launch.provider}",
            f"- model: {launch.model or _UNSPECIFIED}",
            f"- effort: {launch.effort or _UNSPECIFIED}",
            f"- cli-version: {launch.cli_version or _UNKNOWN}",
            f"- argv-digest: {_argv_digest(launch.argv)}",
        ]
    if prompt is not None:
        lines.append(f"- prompt-digest: {_text_digest(prompt)}")
    return lines


def _argv_digest(argv: tuple[str, ...]) -> str:
    # NUL 구분자로 이어 붙인다. argv 원소에는 나올 수 없는 바이트라서
    # ("a b",) 와 ("a", "b") 가 같은 digest로 접히지 않는다.
    return _text_digest("\x00".join(argv))


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[
        :REVIEW_LAUNCH_DIGEST_CHARS
    ]


class _RateLimitPayload(TypedDict):
    status: str
    reason: str
    reviewer: str
    retry_after: str
    next_command: str


def _rate_limit_payload(r: SubprocessResult) -> _RateLimitPayload | None:
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
