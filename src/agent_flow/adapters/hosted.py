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
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from agent_flow.adapters.base import Adapter
from agent_flow.artifact import bind_review_evidence, ensure_review_binding
from agent_flow.core.local_skills import (
    ARCHITECTURE_CONTRACT_FAMILY,
    phase_skill_resolution,
)
from agent_flow.core.review_evidence import (
    ReviewerOutcome,
    complete_provider_names,
    review_evidence_record,
    review_results_path,
    serialize_review_results,
)
from agent_flow.core.worktree_isolation import (
    WorktreeIsolationError,
    git_safe,
    validate_run_artifact_target,
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
    review_job_id,
    run_distribution,
)
from agent_flow.subprocess_pool import SubprocessResult


if TYPE_CHECKING:
    from agent_flow.runner import Phase

_REVIEW_INPUT_TIMEOUT_S = 120
_REVIEW_INPUT_MAX_BYTES = 8 * 1024 * 1024
_OID_PATTERN = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
# base ref는 git argv에 그대로 들어간다. 옵션처럼 보이는 값이나 revision 문법이
# 섞인 값은 거부한다.
_BASE_REF_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-/+@"
)

# `requires`가 있는 angle은 그 skill이 이 phase의 required 집합에 있을 때만 등록한다.
# base_prompt는 angle마다 그대로 복제되므로(`_reviewer_jobs`) required 목록 하나가
# angle 수 × provider 수만큼 늘어난다. 계층 계약을 요구하지 않는 변경에서 그 angle을
# 그대로 띄우면 resolver 쪽 축소가 review phase에서 전부 사라진다 — 이 template이
# `clean-architecture-core/SKILL.md`를 읽으라고 직접 지시하기 때문이다.
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
        # 값은 이름 family다. 정확한 이름(`clean-architecture-core`)으로 보면
        # routed-but-uninstalled 상태에서 dependency 확장이 일어나지 않아 angle이
        # 빠지는데, 작성자 게이트는 family로 판정해 `applied`를 요구한다 —
        # 두 술어가 갈리는 그 자리가 정확히 리뷰 없는 통과가 된다.
        "requires": ARCHITECTURE_CONTRACT_FAMILY,
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
    review_input = _write_review_input_snapshot(
        project_root,
        run_dir,
        phase.id,
        base_branch=_profile_base_branch(adapter),
    )
    jobs = _reviewer_jobs(
        phase,
        run_dir,
        project_root,
        adapter,
        review_input=review_input,
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
    _write_review_results(distribution, execution.outcomes)
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
        blocking_job_ids=(
            ()
            if distribution.accept_any_provider
            else distribution.required_job_ids
        ),
        accept_any_provider=distribution.accept_any_provider,
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


def _write_review_input_snapshot(
    project_root: Path,
    run_dir: Path,
    phase_id: str,
    *,
    base_branch: str | None = None,
) -> ReviewInputSnapshot:
    """리뷰어가 받는 유일한 증거. 기준점은 선언된 base와의 merge-base다.

    `HEAD` 기준으로 찍으면 작업이 이미 커밋된 브랜치에서는 모든 섹션이 비는데,
    같은 프롬프트가 리뷰어에게 샌드박스 안에서 `git diff`를 돌리지 말라고 말한다.
    그래서 리뷰어는 근거 없이 판정하게 된다 — 라운드 하나가 실제로 그렇게 무너졌다.
    """
    # 관측 하나당 상한을 전체 예산보다 낮게 잡는다. unborn HEAD 경로는 관측을
    # 셋까지 만들고, 합계가 예산을 넘으면 스냅샷 자체를 못 쓴다. 상한에 걸린
    # 섹션은 라운드를 죽이지 않고 머리말에 잘렸다고 적는다.
    observation_max_bytes = max(1, _REVIEW_INPUT_MAX_BYTES // 4)
    baseline = _resolve_review_baseline(
        project_root,
        base_branch,
        max_output_bytes=observation_max_bytes,
    )
    status = git_safe(
        "status",
        "--short",
        "--untracked-files=all",
        cwd=project_root,
        optional_locks=False,
        timeout_s=_REVIEW_INPUT_TIMEOUT_S,
        max_output_bytes=observation_max_bytes,
    )
    diff = git_safe(
        "diff",
        "--no-ext-diff",
        "--no-color",
        baseline.rev,
        "--",
        cwd=project_root,
        optional_locks=False,
        timeout_s=_REVIEW_INPUT_TIMEOUT_S,
        max_output_bytes=observation_max_bytes,
    )
    notes: list[str] = []
    diff_observations = []
    if _is_unborn_head_failure(diff):
        notes.append(
            "HEAD carries no commit yet, so the staged and working-tree diffs "
            "below stand in for a baseline diff"
        )
        diff_observations.extend((
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
                    max_output_bytes=observation_max_bytes,
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
                    max_output_bytes=observation_max_bytes,
                ),
            ),
        ))
    else:
        diff_observations.append((f"git diff {baseline.rev}", diff))
    observations = [("git status --short", status), *diff_observations]
    failed = [
        f"{label}: {result.stderr.strip() or result.error or result.returncode}"
        for label, result in observations
        if not result.ok and not _hit_output_limit(result)
    ]
    if failed:
        raise WorktreeIsolationError(
            "could not precompute reviewer input: " + "; ".join(failed)
        )
    truncated = [
        label for label, result in observations if _hit_output_limit(result)
    ]
    if truncated:
        notes.append(
            f"truncated at {observation_max_bytes} bytes, so the sections are "
            f"incomplete: {', '.join(truncated)}"
        )
    has_diff = any(result.stdout.strip() for _, result in diff_observations)
    status_lines = [line for line in status.stdout.splitlines() if line.strip()]
    # 추적 중인 변경은 반드시 diff로도 나타난다. 그런데 diff가 비었다면 스냅샷을
    # 못 만든 것이다. 추적되지 않는 파일(`??`)은 status 목록 자체가 증거다.
    tracked = [line for line in status_lines if not line.startswith("??")]
    if tracked and not has_diff:
        raise WorktreeIsolationError(
            "could not precompute reviewer input: git status reports "
            f"{len(tracked)} tracked change(s) but the diff against "
            f"{baseline.rev} is empty"
        )
    # 성공했지만 빈 출력은 두 가지다. 변경이 정말 없는 review-only 작업은 정당하고,
    # 기준점을 못 잡아 아무것도 못 담은 것은 리뷰를 통과시키면 안 된다.
    if not status_lines and not has_diff:
        if baseline.base_unresolved:
            raise WorktreeIsolationError(
                "could not precompute reviewer input: no diff is available "
                f"against declared base `{base_branch}` ({baseline.detail}); "
                "reviewers would receive no evidence"
            )
        notes.append(
            "no change relative to this baseline: the snapshot is a verified "
            "empty diff, not a missing one"
        )
    header = [
        "# Reviewer input snapshot",
        "",
        f"- phase: {phase_id}",
        f"- diff baseline: {baseline.detail}",
    ]
    header.extend(f"- note: {note}" for note in notes)
    sections = [
        f"## {label}\n\n{result.stdout.rstrip() or '(empty)'}"
        for label, result in observations
    ]
    content = "\n".join(header) + "\n\n" + "\n\n".join(sections) + "\n"
    encoded = content.encode("utf-8")
    if len(encoded) > _REVIEW_INPUT_MAX_BYTES:
        raise WorktreeIsolationError(
            "could not precompute reviewer input: "
            f"snapshot exceeds {_REVIEW_INPUT_MAX_BYTES} bytes"
        )
    target = run_dir.resolve() / f"{phase_id}-review-input.patch"
    write_run_artifact_text(run_dir, target, content)
    return ReviewInputSnapshot(
        path=target,
        digest=hashlib.sha256(encoded).hexdigest(),
    )


@dataclass(frozen=True)
class _ReviewBaseline:
    rev: str
    detail: str
    # 선언된 base가 있는데 그걸 기준으로 삼지 못한 상태. 이때의 빈 diff는
    # "변경 없음"의 증거가 될 수 없다.
    base_unresolved: bool = False


def _resolve_review_baseline(
    project_root: Path,
    base_branch: str | None,
    *,
    max_output_bytes: int,
) -> _ReviewBaseline:
    fallback = "`HEAD` — changes already committed on this branch are NOT below"
    if not base_branch:
        return _ReviewBaseline(
            rev="HEAD",
            detail=(
                f"{fallback} (the active profile declares no `branching.base`)"
            ),
        )
    if not _is_usable_base_ref(base_branch):
        return _ReviewBaseline(
            rev="HEAD",
            detail=(
                f"{fallback} (declared base `{base_branch}` is not a usable git "
                "ref name)"
            ),
            base_unresolved=True,
        )
    diagnostic = "no common ancestor"
    for candidate in (base_branch, f"origin/{base_branch}"):
        result = git_safe(
            "merge-base",
            "HEAD",
            candidate,
            cwd=project_root,
            optional_locks=False,
            timeout_s=_REVIEW_INPUT_TIMEOUT_S,
            max_output_bytes=max_output_bytes,
        )
        oid = result.stdout.strip() if result.ok else ""
        if _OID_PATTERN.fullmatch(oid):
            return _ReviewBaseline(
                rev=oid,
                detail=(
                    f"`git merge-base HEAD {candidate}` = {oid} — every change "
                    "from the base through the current working tree is below, "
                    "committed and uncommitted alike"
                ),
            )
        if not result.ok:
            stderr = result.stderr.strip()
            diagnostic = (
                stderr.splitlines()[-1]
                if stderr
                else result.error or f"git exited {result.returncode}"
            )
    return _ReviewBaseline(
        rev="HEAD",
        detail=(
            f"{fallback} (declared base `{base_branch}` could not be used: "
            f"{diagnostic})"
        ),
        base_unresolved=True,
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


def _is_usable_base_ref(value: str) -> bool:
    return (
        bool(value)
        and not value.startswith("-")
        and ".." not in value
        and set(value) <= _BASE_REF_CHARS
    )


def _hit_output_limit(result) -> bool:
    """상한에 걸려 잘린 것과 실패한 것은 다르다. 잘려도 부분 증거는 남는다."""
    return (
        result.output_truncated
        and not result.timed_out
        and result.error is None
    )


def _is_unborn_head_failure(result) -> bool:
    diagnostic = f"{result.stderr}\n{result.error or ''}".lower()
    return (
        result.returncode == 128
        and "head" in diagnostic
        and ("ambiguous" in diagnostic or "bad revision" in diagnostic)
    )


def _applicable_angles(
    angles: list[dict[str, str]] | tuple[dict[str, str], ...],
    phase: Phase,
    project_root: Path,
    adapter: Adapter,
) -> list[dict[str, str]]:
    """`requires`를 선언한 angle은 그 skill이 required일 때만 남긴다.

    판정은 writer prompt와 **같은 resolver 호출**로 한다. 여기서 조건을 다시 쓰면
    reviewer가 작성자보다 넓거나 좁은 기준을 받고, 그게 agent-flow가 지키겠다는
    성질이다.
    """
    gated = [angle for angle in angles if angle.get("requires")]
    if not gated:
        return list(angles)
    resolution = phase_skill_resolution(
        adapter.config_root_or(project_root),
        phase.id,
        phase_skills=getattr(phase, "skills", None),
        profile=adapter._profile_snapshot,
        changed_files=adapter._changed_files,
        task_text=adapter._task_text,
        concerns=adapter._concerns,
    )
    required = {skill.name for skill in resolution.required}
    return [
        angle
        for angle in angles
        if not angle.get("requires") or _angle_requirement_met(angle["requires"], required)
    ]


def _angle_requirement_met(requirement: str, required: set[str]) -> bool:
    """`requires`는 이름 family다. 작성자 게이트와 같은 술어를 쓴다."""
    if requirement == ARCHITECTURE_CONTRACT_FAMILY:
        return any(ARCHITECTURE_CONTRACT_FAMILY in name for name in required)
    return requirement in required


def _reviewer_jobs(
    phase: Phase,
    run_dir: Path,
    project_root: Path,
    adapter: Adapter,
    *,
    review_input: ReviewInputSnapshot | None = None,
) -> list[ReviewerJob]:
    profile_angles = adapter.profile_review_angles()
    angles = _applicable_angles(
        _merge_review_angles(_BASE_REVIEW_ANGLES, profile_angles),
        phase,
        project_root,
        adapter,
    )
    jobs: list[ReviewerJob] = []
    # host가 받는 envelope와 다른 렌더다(host_hint 없음). 관측 이름을 갈라
    # trace에서 둘을 sha 재계산 없이 구분한다.
    base_prompt = adapter.render_envelope(
        phase, run_dir, project_root, prompt_variant="reviewer-base"
    )
    review_input_prompt = (
        "\n\n## Precomputed review input\n\n"
        f"Read `{review_input.path}` before judging the change. The controller "
        "captured it immediately before launching reviewers: its header names "
        "the diff baseline (the profile base branch's merge-base when one is "
        "available, so changes already committed on this branch are included) "
        "and states whether the snapshot was truncated or is a verified empty "
        "diff. The body holds `git status --short` and that diff. Inspect "
        "untracked files listed there directly. Do not run `git diff` inside "
        "the reviewer sandbox. "
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
