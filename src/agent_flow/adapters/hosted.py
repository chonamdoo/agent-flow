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

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

import yaml

from agent_flow.adapters.base import Adapter
from agent_flow.artifact import bind_review_evidence, ensure_review_binding, read_meta, write_meta
from agent_flow.cli_detect import cli_by_name
from agent_flow.core.local_skills import (
    ARCHITECTURE_CONTRACT_REQUIREMENT,
    architecture_contract_required,
)
from agent_flow.core.skill_scope import record_reviewer_documents, reviewer_document_ids
from agent_flow.core.review_evidence import (
    ReviewerOutcome,
    complete_provider_names,
    review_evidence_record,
    review_results_path,
    serialize_review_results,
)
from agent_flow.core import review_input
from agent_flow.core.skill_resolver import SkillResolution, selector_matches
from agent_flow.core.worktree_isolation import (
    WorktreeIsolationError,
    validate_run_artifact_target,
    write_run_artifact_text,
)
from agent_flow.multi_review import (
    REVIEW_CLI_NAMES,
    Distribution,
    ReviewerJob,
    ReviewExecution,
    _reviewer_launch_profile,
    distribute,
    eligible_reviewer_names,
    review_job_id,
    reviewer_provider_error,
    reviewer_result_error,
    run_distribution,
)
from agent_flow.subprocess_pool import SubprocessResult

if TYPE_CHECKING:
    from agent_flow.runner import Phase

# 계약 angle과 작성자 게이트는 같은 resolver 판정을 사용해야 리뷰 없는 통과가 없다.
_BASE_REVIEW_ANGLES: tuple[dict[str, object], ...] = (
    {
        "id": "generalist",
        "prompt": "templates/_shared/review/architecture.md",
    },
    {
        "id": "types",
        "prompt": "templates/_shared/review/types.md",
    },
    {
        "id": "architecture-design",
        "prompt": "templates/_shared/review/architecture-design.md",
        "requires": ARCHITECTURE_CONTRACT_REQUIREMENT,
    },
    {
        "id": "state-integrity",
        "prompt": "templates/_shared/review/state-integrity.md",
        "task_terms": (
            "database",
            "db transaction",
            "sql",
            "orm",
            "migration",
            "payment",
            "billing",
            "inventory",
            "stock",
            "persistent state",
            "race condition",
            "partial write",
            "row lock",
            "idempotency",
        ),
        "path_globs": (
            "**/*.sql",
            "**/migration/**",
            "**/migrations/**",
            "**/database/**",
            "**/db/**",
            "**/dao/**",
            "**/payment/**",
            "**/payments/**",
            "**/billing/**",
            "**/inventory/**",
            "**/stock/**",
            "**/persistence/**",
            "**/storage/**",
        ),
    },
    {
        "id": "clean-architecture",
        "prompt": "templates/_shared/review/clean-architecture.md",
        # legacy angle id는 유지하되 심사 기준은 프로젝트가 선택한 계약을 쓴다.
        "requires": ARCHITECTURE_CONTRACT_REQUIREMENT,
    },
)
_UNCONDITIONAL_REVIEW_ANGLE_IDS = frozenset({"generalist", "types"})
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

    def execute(
        self, phase: Phase, run_dir: Path, project_root: Path, *,
        resolution: SkillResolution | None = None,
    ) -> bool:
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
            resolution=resolution,
        )
        print(prompt)
        return False  # host AI writes the artifact


def _run_multi_review_distribution(
    phase: Phase,
    run_dir: Path,
    project_root: Path,
    adapter: Adapter,
) -> tuple[Distribution, ReviewExecution]:
    review_input = _write_review_input_snapshot(
        project_root,
        run_dir,
        phase.id,
        base_branch=_profile_base_branch(adapter),
    )
    jobs, documents = _reviewer_jobs(
        phase,
        run_dir,
        project_root,
        adapter,
        review_input=review_input,
        providers=eligible_reviewer_names(),
    )
    # phase는 여기서만 안다. 넘기지 않으면 launch 선언의 `match.phase`는
    # final-review 밖에서 비교할 값이 없어 절대 발동하지 않는다.
    #
    # config root도 여기서만 안다. reviewer launch 선언은 leader의 `.agent-flow/`에
    # 있고, managed checkout에는 그 디렉터리가 없다(gitignored) — project_root를
    # 소스로 두면 선언이 조용히 무시된다.
    distribution = distribute(jobs, host=adapter.name, phase_id=phase.id)
    execution = run_distribution(
        distribution,
        project_root,
        config_root=adapter.config_root_or(project_root),
    )
    _write_review_results(distribution, execution.outcomes)
    delivered_jobs = {
        result.job_id
        for result in execution.results
        if reviewer_provider_error(result) is None
    }
    delivered_documents = {
        provider: identities
        for provider, identities in documents.items()
        if identities and any(
            review_job_id(provider, job) in delivered_jobs
            for job in distribution.by_cli.get(provider, ())
        )
    }
    if delivered_documents:
        meta = read_meta(run_dir)
        for provider, identities in delivered_documents.items():
            record_reviewer_documents(meta, phase.id, provider, identities)
        write_meta(run_dir, meta)
    return distribution, execution


def _write_review_results(
    distribution: Distribution,
    outcomes: tuple[ReviewerOutcome, ...],
) -> None:
    if distribution.phase_id is None:
        return
    roots = {
        job.artifact_root.resolve()
        for jobs in distribution.by_cli.values()
        for job in jobs
    }
    if not roots:
        return
    if len(roots) != 1:
        raise ValueError("review result artifacts must share one run directory")
    artifact_root = roots.pop()
    output = review_results_path(artifact_root, distribution.phase_id)
    validate_run_artifact_target(artifact_root, output)
    binding = ensure_review_binding(artifact_root)
    serialized = serialize_review_results(
        phase_id=distribution.phase_id,
        run_id=binding.run_id,
        nonce=binding.nonce,
        phase_entered_at=binding.phase_entered_at,
        outcomes=outcomes,
    )
    write_run_artifact_text(artifact_root, output, serialized)
    expected_by_provider = distribution.expected_job_ids_by_provider()
    record = review_evidence_record(
        nonce=binding.nonce,
        phase_entered_at=binding.phase_entered_at,
        serialized_results=serialized,
        outcomes=outcomes,
        expected_job_ids_by_provider=expected_by_provider,
    )
    bind_review_evidence(
        artifact_root,
        phase_id=distribution.phase_id,
        run_id=binding.run_id,
        nonce=binding.nonce,
        phase_entered_at=binding.phase_entered_at,
        record=record,
    )


@dataclass(frozen=True)
class ReviewInputSnapshot:
    path: Path
    digest: str
    document_scope: tuple[str, ...] | None = None


def _write_review_input_snapshot(
    project_root: Path,
    run_dir: Path,
    phase_id: str,
    *,
    base_branch: str | None = None,
) -> ReviewInputSnapshot:
    observation = review_input.capture_review_input(
        project_root, read_meta(run_dir), phase_id, base_branch=base_branch,
    )
    target = run_dir.resolve() / f"{phase_id}-review-input.patch"
    write_run_artifact_text(run_dir, target, observation.content)
    return ReviewInputSnapshot(
        path=target,
        digest=hashlib.sha256(observation.content.encode("utf-8")).hexdigest(),
        document_scope=observation.document_scope,
    )


def _profile_base_branch(adapter: Adapter) -> str | None:
    """diff 기준 브랜치. profile의 `branching.base`가 정본이다."""
    snapshot = getattr(adapter, "_profile_snapshot", None)
    if not isinstance(snapshot, Mapping):
        return None
    for section, key in (("branching", "base"), ("pr", "target_branch")):
        block = snapshot.get(section)
        if not isinstance(block, Mapping):
            continue
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _applicable_angles(
    angles: list[dict[str, object]] | tuple[dict[str, object], ...],
    phase: Phase,
    project_root: Path,
    adapter: Adapter,
    *,
    providers: Sequence[str],
) -> list[dict[str, object]]:
    """Return review angles that apply to the active architecture."""
    skill_gated = [angle for angle in angles if "requires" in angle]
    required: set[str] = set()
    # 계약 충족 여부는 작성자 게이트와 **같은 함수**로 판정한다. 여기서 이름을
    # 다시 해석하면 두 판정이 갈리고, 그 자리가 리뷰 없는 통과가 된다.
    contract_satisfied = False
    if skill_gated:
        for provider in providers:
            resolution = adapter.phase_resolution(
                phase, project_root, skill_host=provider,
            )
            required.update(skill.name for skill in resolution.required)
            contract_satisfied = contract_satisfied or architecture_contract_required(resolution)
    return [
        angle
        for angle in angles
        if (
            "requires" not in angle
            or _angle_requirement_met(
                _angle_requirement_value(angle),
                required,
                contract_satisfied=contract_satisfied,
            )
        )
        and _angle_selectors_match(angle, adapter)
    ]


def _angle_selectors_match(angle: Mapping[str, object], adapter: Adapter) -> bool:
    if "task_terms" not in angle and "path_globs" not in angle:
        return True
    return selector_matches(
        task_terms=_angle_selector_values(angle, "task_terms"),
        path_globs=_angle_selector_values(angle, "path_globs"),
        changed_files=adapter._changed_files,
        task_text=adapter._task_text,
    )


def _angle_selector_values(
    angle: Mapping[str, object], key: str
) -> tuple[str, ...]:
    raw = angle.get(key, ())
    if not isinstance(raw, (list, tuple)) or any(
        not isinstance(value, str) or not value.strip() for value in raw
    ):
        angle_id = str(angle.get("id") or "<unknown>")
        raise ValueError(
            f"review angle {angle_id!r} {key} must be a list of non-empty strings"
        )
    return tuple(value.strip() for value in raw)


def _angle_requirement_value(angle: Mapping[str, object]) -> str:
    raw = angle.get("requires")
    if not isinstance(raw, str) or not raw.strip():
        angle_id = str(angle.get("id") or "<unknown>")
        raise ValueError(
            f"review angle {angle_id!r} requires must be a non-empty string"
        )
    return raw.strip()


def _angle_requirement_met(
    requirement: str, required: set[str], *, contract_satisfied: bool
) -> bool:
    """구조 계약을 요구하는 angle은 작성자 게이트와 같은 판정을 쓴다.

    무엇이 계약인지는 프로젝트 선택이 정한다. 여기서 이름 조각으로 다시 판정하면
    local 프로젝트에서 구조 리뷰가 통째로 사라진다.
    """
    if requirement == ARCHITECTURE_CONTRACT_REQUIREMENT:
        return contract_satisfied
    return requirement in required


def _reviewer_jobs(
    phase: Phase,
    run_dir: Path,
    project_root: Path,
    adapter: Adapter,
    *,
    review_input: ReviewInputSnapshot | None = None,
    providers: Sequence[str] | None = None,
) -> tuple[list[ReviewerJob], dict[str, tuple[str, ...]]]:
    """Build provider-specific reviewer jobs from a single captured input."""
    providers = REVIEW_CLI_NAMES if providers is None else tuple(providers)
    adapter._provider_authority = tuple(providers)
    adapter._provider_launch_authority = yaml.safe_dump((
        _reviewer_launch_profile(adapter.config_root_or(project_root)),
        tuple((provider, repr(cli_by_name(provider))) for provider in providers),
    ), sort_keys=True, allow_unicode=True)
    profile_angles = adapter.profile_review_angles()
    angles = _applicable_angles(
        _merge_review_angles(_BASE_REVIEW_ANGLES, profile_angles),
        phase,
        project_root,
        adapter,
        providers=providers,
    )
    jobs: list[ReviewerJob] = []
    meta = read_meta(run_dir)
    base_prompt_by_provider: dict[str, str] = {}
    rendered_documents: dict[str, tuple[str, ...]] = {}
    for provider in providers:
        resolution = adapter.phase_resolution(
            phase, project_root, skill_host=provider,
            document_scope=review_input.document_scope if review_input is not None else None,
            required_document_ids=reviewer_document_ids(meta, phase.id, provider),
        )
        base_prompt_by_provider[provider] = adapter.render_envelope(
            phase, run_dir, project_root,
            prompt_variant=f"reviewer-base-{provider}",
            skill_host=provider, role="reviewer", resolution=resolution,
        )
        rendered_documents[provider] = resolution.required_document_ids
    fallback_prompt = (
        ""
        if providers
        else adapter.render_envelope(
            phase,
            run_dir,
            project_root,
            prompt_variant="reviewer-base-host",
            skill_host=adapter.name,
            role="reviewer",
        )
    )
    review_input_prompt = (
        "\n\n## Precomputed review input\n\n"
        f"Read `{review_input.path}` before judging the change. The controller "
        "captured it immediately before launching reviewers: its header names "
        "the diff baseline — a pinned OID for explicit publication-only scope, "
        "otherwise the merge-base of the declared base branch when available "
        "or its remote-tracking counterpart when the declared base is behind, "
        "so committed changes since that baseline are included — and states "
        "in `- note:` lines whether the baseline skipped a "
        "stale declared base, whether the snapshot was truncated, and whether it "
        "is a verified empty diff. The body holds `git status --porcelain=v1` and that "
        "diff. Inspect "
        "untracked files listed there directly. Do not run `git diff` inside "
        "the reviewer sandbox. When the header declares publication-only scope, "
        "judge defects introduced or worsened by the pinned-base delta, including "
        "security defects, with no path or category exclusions. Read surrounding "
        "code for context as needed. Keep unchanged pre-baseline findings as "
        "separately recorded risks; do not claim they are fixed. Publication-only "
        "approval is not whole-PR approval and cannot authorize merge. "
        f"Its SHA-256 is `{review_input.digest}`; this digest is part of your "
        "prompt identity."
        if review_input is not None
        else ""
    )
    for item in angles:
        angle_id = str(item.get("id") or "").strip()
        if not angle_id:
            continue
        angle_output = _review_angle_output(run_dir, phase.id, angle_id)
        angle_contract = (
            "\n\nDo not run project-wide test suites, builds, linters, or "
            "formatters. Use supplied verification evidence; any necessary "
            "reproduction must be limited to the changed boundary.\n"
            "\n\n## Isolated reviewer process contract\n\n"
            "You are one read-only reviewer subprocess. Do not invoke "
            "`agent-flow status`, do not continue the workflow, and do not "
            "write the aggregate phase artifact named above. Return only this "
            "angle's review in your final stdout; the parent writes it to this "
            "angle's per-provider artifact in the run directory. Start your "
            "output with exactly these two plain lines:\n"
            "`## Reviewer`\n"
            "`reviewer-source: sub-agent`\n"
            "Do not wrap either line in bold, a list, or a code fence. End with "
            "exactly one unfenced plain line: `verdict: approve` or "
            "`verdict: request-changes`. Do not write another unfenced verdict "
            "line anywhere else."
            f"{review_input_prompt}\n\n"
            f"## Review angle\n\n"
            f"- id: {angle_id}\n"
            f"{_review_angle_prompt(project_root, item.get('prompt', ''))}\n"
        )
        jobs.append(ReviewerJob(
            angle_id=angle_id,
            prompt=fallback_prompt + angle_contract if fallback_prompt else "",
            output_path=angle_output,
            artifact_root=run_dir.resolve(),
            prompt_by_provider={
                provider: prompt + angle_contract
                for provider, prompt in base_prompt_by_provider.items()
            },
        ))
    return jobs, rendered_documents


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
    baseline: Sequence[Mapping[str, object]],
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
        profile_item = dict(item)
        if angle_id in _UNCONDITIONAL_REVIEW_ANGLE_IDS:
            for selector in ("requires", "task_terms", "path_globs"):
                profile_item.pop(selector, None)
        baseline_item = merged.get(angle_id)
        if (
            baseline_item is not None
            and "requires" in baseline_item
            and "requires" not in profile_item
        ):
            profile_item["requires"] = baseline_item["requires"]
        merged[angle_id] = profile_item
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
    expected_by_provider = distribution.expected_job_ids_by_provider()
    successful_job_ids = {
        result.job_id
        for result in results
        if reviewer_result_error(result) is None
    }
    if complete_provider_names(
        expected_by_provider,
        successful_job_ids,
    ):
        return []

    provider_failures: list[str] = []
    for cli_name, jobs in distribution.by_cli.items():
        failures = []
        for job in jobs:
            job_id = review_job_id(cli_name, job)
            result = by_id.get(job_id)
            reason = (
                "missing result"
                if result is None
                else reviewer_result_error(result)
            )
            if reason is not None:
                failures.append(f"{job.angle_id}: {reason}")
        if failures:
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
            job_id = review_job_id(cli_name, job)
            result = by_id.get(job_id)
            if result is None:
                status = "skipped"
            elif reviewer_result_error(result) is None:
                status = "pass"
            else:
                status = "unavailable"
            source = "candidate"
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
