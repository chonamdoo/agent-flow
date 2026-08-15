"""Hosted adapter — one class, parameterized by host.

Replaces the previous Claude / Codex subclass pair and also covers OMP.
Each host contributes only:
  - a name (claude / codex / omp)
  - a host-specific hint string

Real behavior divergence (multi-reviewer fan-out, parallel sub-agents) is
driven by the workflow YAML's per-phase `multi_review: true` flag, not by
adapter subclass. This kills the copy-paste polymorphism that the architectural
review flagged.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from agent_flow.adapters.base import Adapter
from agent_flow.core.worktree_isolation import (
    WorktreeIsolationError,
    git_safe,
    write_run_artifact_text,
)
from agent_flow.multi_review import (
    Distribution,
    FINAL_REVIEW_PHASE_ID,
    ReviewerJob,
    ReviewExecution,
    distribute,
    distribute_final_review,
    reviewer_result_error,
    run_distribution,
)
from agent_flow.subprocess_pool import SubprocessResult


if TYPE_CHECKING:
    from agent_flow.runner import Phase

_REVIEW_INPUT_TIMEOUT_S = 120
_REVIEW_INPUT_MAX_BYTES = 8 * 1024 * 1024

_BASE_REVIEW_ANGLES: tuple[dict[str, str], ...] = (
    {
        "id": "generalist",
        "prompt": "templates/_shared/review/architecture.md",
    },
    {
        "id": "architecture-design",
        "prompt": "templates/_shared/review/architecture-design.md",
    },
    {
        "id": "clean-architecture",
        "prompt": "templates/_shared/review/clean-architecture.md",
    },
)
_BASE_REVIEW_PROMPTS = {
    str(item["prompt"])
    for item in _BASE_REVIEW_ANGLES
}
_ARTIFACT_COMPONENT_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz0123456789-"
)

_CLAUDE_HINT = """\
- Multi-review angles are launched by agent-flow as independent OS-confined
  subprocesses. Aggregate their per-angle artifacts; do not replace them with
  in-session Task sub-agents.
- Use `TodoWrite` for slice tracking during `implement` phase. Mark each
  TDD red→green→refactor step in_progress / completed.
- For long-running phases, prefer parallel reads (multiple `Read` calls in
  one message) over sequential.
- Cite file:line references using the `path/to/file:42` format.
"""

_CODEX_HINT = """\
- Multi-review angles are launched by agent-flow as independent OS-confined
  subprocesses. Aggregate their per-angle artifacts; do not replace them with
  in-session Codex sub-agents.
- Each reviewer section must include `reviewer-source: sub-agent`.
- Per-angle artifacts are written as `<phase>-<angle>[-<provider>].md`;
  aggregate them into the phase's own summary artifact.
- Cite file:line references using the `path/to/file:42` format.
"""

_OMP_HINT = """\
- Multi-review angles are launched by agent-flow as independent OS-confined
  subprocesses. Aggregate their per-angle artifacts; do not replace them with
  in-session task sub-agents.
- Each reviewer section must include `reviewer-source: sub-agent`.
- For long-running phases, prefer parallel reads/tool calls over sequential
  exploration.
- Cite file:line references using the `path/to/file:42` format.
"""

# Read-only mapping. Wrapped to prevent third-party runtime mutation that
# would silently change adapter behavior across the process.
_HOST_HINTS: MappingProxyType[str, str] = MappingProxyType({
    "claude": _CLAUDE_HINT,
    "codex": _CODEX_HINT,
    "omp": _OMP_HINT,
})


class HostedAdapter(Adapter):
    """Single adapter parameterized by host name.

    Construct with `HostedAdapter("claude")` etc. Behavior is identical
    across hosts except the hint block injected into the prompt envelope
    and the multi-reviewer distribution preview.
    """

    def __init__(self, host_name: str) -> None:
        super().__init__()
        if host_name not in _HOST_HINTS:
            raise ValueError(
                f"Unknown host '{host_name}'. Known: {sorted(_HOST_HINTS)}"
            )
        self.name = host_name
        self._hint = _HOST_HINTS[host_name]

    def execute(self, phase: Phase, run_dir: Path, project_root: Path) -> bool:
        host_hint = self._hint
        host_hint += (
            "\n\n### Host-session isolation boundary\n"
            "This controller session is not counted as an isolated worker. "
            "Every child reviewer is launched separately through the verified "
            "provider sandbox; never substitute controller-session work for a "
            "failed child process."
        )
        if phase.multi_review:
            distribution, execution = _run_multi_review_distribution(
                phase, run_dir, project_root, self
            )
            failures = _required_reviewer_failures(
                distribution,
                execution.results,
            )
            if distribution.fallback_to_generic:
                failures.append("no usable Claude/Codex reviewer CLI is available")
            if distribution.insufficient_reviewers:
                failures.append("fewer than two reviewer processes were assigned")
            if failures:
                raise WorktreeIsolationError(
                    "required reviewer subprocesses failed closed: "
                    + "; ".join(failures)
                )
            host_hint += "\n" + _multi_reviewer_block(
                distribution,
                execution,
            )
        prompt = self.render_envelope(
            phase, run_dir, project_root, host_hint=host_hint,
        )
        print(prompt)
        return False  # host AI writes the artifact


def _run_multi_review_distribution(
    phase: Phase,
    run_dir: Path,
    project_root: Path,
    adapter: Adapter,
) -> tuple[Distribution, ReviewExecution]:
    review_input_path = _write_review_input_snapshot(
        project_root,
        run_dir,
        phase.id,
    )
    jobs = _reviewer_jobs(
        phase,
        run_dir,
        project_root,
        adapter,
        review_input_path=review_input_path,
    )
    # phase는 여기서만 안다. 넘기지 않으면 launch 선언의 `match.phase`는
    # final-review 밖에서 비교할 값이 없어 절대 발동하지 않는다.
    #
    # config root도 여기서만 안다. reviewer launch 선언은 leader의 `.agent-flow/`에
    # 있고, managed checkout에는 그 디렉터리가 없다(gitignored) — project_root를
    # 소스로 두면 선언이 조용히 무시된다.
    distribution = (
        distribute_final_review(jobs, host=adapter.name)
        if phase.id == FINAL_REVIEW_PHASE_ID
        else distribute(jobs, host=adapter.name, phase_id=phase.id)
    )
    execution = run_distribution(
        distribution,
        project_root,
        config_root=adapter.config_root_or(project_root),
    )
    return distribution, execution


def _write_review_input_snapshot(
    project_root: Path,
    run_dir: Path,
    phase_id: str,
) -> Path:
    status = git_safe(
        "status",
        "--short",
        "--untracked-files=all",
        cwd=project_root,
        optional_locks=False,
        timeout_s=_REVIEW_INPUT_TIMEOUT_S,
        max_output_bytes=_REVIEW_INPUT_MAX_BYTES,
    )
    diff = git_safe(
        "diff",
        "--no-ext-diff",
        "--no-color",
        "HEAD",
        "--",
        cwd=project_root,
        optional_locks=False,
        timeout_s=_REVIEW_INPUT_TIMEOUT_S,
        max_output_bytes=_REVIEW_INPUT_MAX_BYTES,
    )
    observations = [("git status --short", status)]
    if _is_unborn_head_failure(diff):
        observations.extend((
            (
                "git diff --cached",
                git_safe(
                    "diff",
                    "--cached",
                    "--no-ext-diff",
                    "--no-color",
                    "--",
                    cwd=project_root,
                    optional_locks=False,
                    timeout_s=_REVIEW_INPUT_TIMEOUT_S,
                    max_output_bytes=_REVIEW_INPUT_MAX_BYTES,
                ),
            ),
            (
                "git diff",
                git_safe(
                    "diff",
                    "--no-ext-diff",
                    "--no-color",
                    "--",
                    cwd=project_root,
                    optional_locks=False,
                    timeout_s=_REVIEW_INPUT_TIMEOUT_S,
                    max_output_bytes=_REVIEW_INPUT_MAX_BYTES,
                ),
            ),
        ))
    else:
        observations.append(("git diff HEAD", diff))
    failed = [
        f"{label}: {result.stderr.strip() or result.error or result.returncode}"
        for label, result in observations
        if not result.ok
    ]
    if failed:
        raise WorktreeIsolationError(
            "could not precompute reviewer input: " + "; ".join(failed)
        )
    sections = [
        f"## {label}\n\n{result.stdout.rstrip() or '(empty)'}"
        for label, result in observations
    ]
    content = "\n\n".join(sections) + "\n"
    if len(content.encode("utf-8")) > _REVIEW_INPUT_MAX_BYTES:
        raise WorktreeIsolationError(
            "could not precompute reviewer input: "
            f"snapshot exceeds {_REVIEW_INPUT_MAX_BYTES} bytes"
        )
    target = run_dir.resolve() / f"{phase_id}-review-input.patch"
    write_run_artifact_text(run_dir, target, content)
    return target


def _is_unborn_head_failure(result) -> bool:
    diagnostic = f"{result.stderr}\n{result.error or ''}".lower()
    return (
        result.returncode == 128
        and "head" in diagnostic
        and ("ambiguous" in diagnostic or "bad revision" in diagnostic)
    )


def _reviewer_jobs(
    phase: Phase,
    run_dir: Path,
    project_root: Path,
    adapter: Adapter,
    *,
    review_input_path: Path | None = None,
) -> list[ReviewerJob]:
    profile_angles = adapter.profile_review_angles()
    angles = _merge_review_angles(_BASE_REVIEW_ANGLES, profile_angles)
    jobs: list[ReviewerJob] = []
    # host가 받는 envelope와 다른 렌더다(host_hint 없음). 관측 이름을 갈라
    # trace에서 둘을 sha 재계산 없이 구분한다.
    base_prompt = adapter.render_envelope(
        phase, run_dir, project_root, prompt_variant="reviewer-base"
    )
    for item in angles:
        angle_id = str(item.get("id") or "").strip()
        if not angle_id:
            continue
        angle_output = _review_angle_output(run_dir, phase.id, angle_id)
        review_input = (
            "\n\n## Precomputed review input\n\n"
            f"Read `{review_input_path}` before judging the change. The controller "
            "captured its `git status --short` and applicable staged/working-tree "
            "diff immediately before launching reviewers. Inspect untracked files "
            "listed there directly. Do not run `git diff` inside the reviewer sandbox."
            if review_input_path is not None
            else ""
        )
        angle_prompt = (
            f"{base_prompt}\n\n"
            "## Isolated reviewer process contract\n\n"
            "You are one read-only reviewer subprocess. Do not invoke "
            "`agent-flow status`, do not continue the workflow, and do not "
            "write the aggregate phase artifact named above. Return only this "
            "angle's review in your final stdout; the parent writes it to this "
            "angle's per-provider artifact in the run directory. Start your "
            "output with exactly these two plain lines:\n"
            "`## Reviewer`\n"
            "`reviewer-source: sub-agent`\n"
            "Do not wrap either line in bold, a list, or a code fence."
            f"{review_input}\n\n"
            f"## Review angle\n\n"
            f"- id: {angle_id}\n"
            f"{_review_angle_prompt(project_root, item.get('prompt', ''))}\n"
        )
        jobs.append(ReviewerJob(
            angle_id=angle_id,
            prompt=angle_prompt,
            output_path=angle_output,
            artifact_root=run_dir.resolve(),
        ))
    return jobs


def _review_angle_output(run_dir: Path, phase_id: str, angle_id: str) -> Path:
    for label, value in (("phase", phase_id), ("angle", angle_id)):
        if (
            not value
            or len(value) > 64
            or value[0] not in "abcdefghijklmnopqrstuvwxyz0123456789"
            or any(character not in _ARTIFACT_COMPONENT_CHARS for character in value)
        ):
            raise ValueError(f"invalid review {label} id: {value}")
    artifact_root = run_dir.resolve()
    output = artifact_root / f"{phase_id}-{angle_id}.md"
    if output.parent != artifact_root or output.is_symlink():
        raise ValueError(f"invalid review angle artifact path: {output}")
    if output.exists() and not output.is_file():
        raise ValueError(f"review angle artifact is not a regular file: {output}")
    return output


def _merge_review_angles(
    baseline: tuple[dict[str, str], ...],
    profile_angles: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {
        str(item["id"]): dict(item)
        for item in baseline
        if item.get("id")
    }
    order = list(merged)
    for item in profile_angles:
        angle_id = str(item.get("id") or "").strip()
        if not angle_id:
            continue
        if angle_id not in merged:
            order.append(angle_id)
        merged[angle_id] = dict(item)
    return [merged[angle_id] for angle_id in order]


def _review_angle_prompt(project_root: Path, prompt_ref: object) -> str:
    prompt_path = str(prompt_ref or "").strip()
    if not prompt_path:
        raise ValueError("review angle prompt is required")
    _validate_review_prompt_path(prompt_path)
    package_path = resources.files("agent_flow").joinpath(prompt_path)
    repo_path = Path(__file__).resolve().parents[3] / prompt_path
    project_path = project_root / prompt_path
    # Built-in angles are kit contracts; profile angles are extension points
    # that the active project may override.
    paths = (
        (package_path, repo_path, project_path)
        if prompt_path in _BASE_REVIEW_PROMPTS
        else (project_path, package_path, repo_path)
    )
    for path in paths:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"review angle prompt not found: {prompt_path}")


def _validate_review_prompt_path(prompt_path: str) -> None:
    path = Path(prompt_path)
    if (
        path.is_absolute()
        or path.parts[:3] != ("templates", "_shared", "review")
        or len(path.parts) != 4
        or not path.parts[-1].endswith(".md")
    ):
        raise ValueError(f"invalid review angle prompt path: {prompt_path}")


def _required_reviewer_failures(
    distribution: Distribution,
    results: Sequence[SubprocessResult],
) -> list[str]:
    by_id = {result.job_id: result for result in results}
    if not distribution.accept_any_provider:
        failures: list[str] = []
        for job_id in sorted(distribution.required_job_ids):
            result = by_id.get(job_id)
            if result is None:
                failures.append(f"{job_id}: missing result")
            else:
                reason = reviewer_result_error(result)
                if reason is not None:
                    failures.append(f"{job_id}: {reason}")
        return failures

    provider_failures: list[str] = []
    for cli_name, jobs in distribution.by_cli.items():
        failures = []
        for job in jobs:
            job_id = f"{cli_name}-{job.angle_id}"
            result = by_id.get(job_id)
            reason = (
                "missing result"
                if result is None
                else reviewer_result_error(result)
            )
            if reason is not None:
                failures.append(f"{job.angle_id}: {reason}")
        if not failures:
            return []
        provider_failures.append(f"{cli_name}: " + ", ".join(failures))
    return provider_failures or ["no reviewer provider completed every angle"]


def _multi_reviewer_block(
    distribution: Distribution | None = None,
    execution: ReviewExecution | None = None,
) -> str:
    """Render the observed subprocess distribution for host aggregation."""
    lines = [
        "### Confined reviewer subprocesses",
        "Use only the per-angle artifacts produced by agent-flow's isolated "
        "reviewer processes. Do not spawn or substitute in-session sub-agents.",
        "Each accepted reviewer section must include "
        "`reviewer-source: sub-agent`.",
    ]
    if distribution is None:
        return "\n".join(lines) + "\n"
    lines.extend(("", f"Distribution summary: {distribution.summary()}."))
    if execution is not None and execution.skipped_providers:
        # 조용한 축소는 승인 근거를 흔든다. 어떤 provider가 왜 빠졌는지 적는다.
        lines.append(
            "Skipped providers (current probe failed): "
            + ", ".join(execution.skipped_providers)
        )
    if distribution.fallback_to_generic:
        lines.extend(
            (
                "status: blocked",
                "reason: no verified reviewer subprocess is available",
                "Do not approve this phase from controller-session review.",
            )
        )
        return "\n".join(lines) + "\n"
    by_id = {
        result.job_id: result
        for result in (execution.results if execution is not None else ())
    }
    for cli_name, jobs in distribution.by_cli.items():
        for job in jobs:
            job_id = f"{cli_name}-{job.angle_id}"
            result = by_id.get(job_id)
            if result is None:
                status = "skipped"
            elif reviewer_result_error(result) is None:
                status = "pass"
            else:
                status = (
                    "unavailable"
                    if distribution.accept_any_provider
                    else "optional-failed"
                )
            source = (
                "candidate"
                if distribution.accept_any_provider
                else (
                    "required"
                    if job_id in distribution.required_job_ids
                    else "optional"
                )
            )
            # skip된 angle의 artifact는 이번 시도가 쓴 것이 아니다. 경로를 그대로
            # 나열하면 host가 수정 전 코드에 대한 낡은 리뷰를 현재 결과로 집계한다.
            if result is None:
                lines.append(
                    f"- {cli_name}:{job.angle_id} ({source}, {status}) "
                    "-> no artifact from this attempt; ignore any stale file"
                )
                continue
            lines.append(
                f"- {cli_name}:{job.angle_id} ({source}, {status}) "
                f"-> `{job.output_path.name}`"
            )
    if distribution.insufficient_reviewers:
        lines.extend(
            (
                "status: blocked",
                "reason: fewer than two independent reviewer processes were assigned",
            )
        )
    return "\n".join(lines) + "\n"
