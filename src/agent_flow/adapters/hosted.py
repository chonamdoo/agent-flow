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
from typing import TYPE_CHECKING, NamedTuple

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
from agent_flow.core.skill_resolver import selector_matches
from agent_flow.core.worktree_isolation import (
    WorktreeIsolationError,
    git_proves_ancestor,
    git_safe,
    validate_run_artifact_target,
    write_run_artifact_text,
)
from agent_flow.multi_review import (
    FINAL_REVIEW_PHASE_ID,
    REVIEW_CLI_NAMES,
    Distribution,
    ReviewerJob,
    ReviewExecution,
    distribute,
    distribute_final_review,
    eligible_reviewer_names,
    review_job_id,
    reviewer_result_error,
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
        "requires": ARCHITECTURE_CONTRACT_FAMILY,
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
        # 값은 이름 family다. 정확한 이름(`clean-architecture-core`)으로 보면
        # routed-but-uninstalled 상태에서 dependency 확장이 일어나지 않아 angle이
        # 빠지는데, 작성자 게이트는 family로 판정해 `applied`를 요구한다 —
        # 두 술어가 갈리는 그 자리가 정확히 리뷰 없는 통과가 된다.
        "requires": ARCHITECTURE_CONTRACT_FAMILY,
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
        providers=eligible_reviewer_names(),
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
    if baseline.note:
        notes.append(baseline.note)
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
    # 기준점을 그 후보에서 잡은 근거. 선언된 base를 쓰지 못한 경우에만 채운다.
    note: str = ""


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
    resolved: list[_BaseCandidate] = []
    for candidate in _base_candidate_refs(
        project_root, base_branch, max_output_bytes=max_output_bytes
    ):
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
            resolved.append(_BaseCandidate(ref=candidate, oid=oid))
            continue
        if not result.ok:
            stderr = result.stderr.strip()
            diagnostic = (
                stderr.splitlines()[-1]
                if stderr
                else result.error or f"git exited {result.returncode}"
            )
    if not resolved:
        return _ReviewBaseline(
            rev="HEAD",
            detail=(
                f"{fallback} (declared base `{base_branch}` could not be used: "
                f"{diagnostic})"
            ),
            base_unresolved=True,
        )
    choice = _newest_review_baseline(project_root, resolved)
    return _ReviewBaseline(
        rev=choice.oid,
        detail=(
            f"`git merge-base HEAD {choice.candidate}` = {choice.oid} — every "
            "change from the base through the current working tree is below, "
            "committed and uncommitted alike"
        ),
        note=choice.note,
    )


def _base_candidate_refs(
    project_root: Path,
    base_branch: str,
    *,
    max_output_bytes: int,
) -> tuple[str, ...]:
    """선언된 base와, 그 base가 실제로 추적하는 remote ref.

    `origin/`을 정본으로 두면 fork나 다중 remote 체크아웃에서 틀린 기준점을 고른다 —
    선언된 `main`이 `upstream/main`을 추적하는데 `origin/main`을 기준으로 잡으면,
    origin에만 있는 커밋이 선언된 base 대비 변경인데도 diff에서 조용히 빠진다.
    추적 설정이 없으면 `origin/<base>`로 내려간다. 그 경우가 단일 remote 체크아웃이다.
    """
    result = git_safe(
        "rev-parse",
        "--symbolic-full-name",
        f"{base_branch}@{{upstream}}",
        cwd=project_root,
        optional_locks=False,
        timeout_s=_REVIEW_INPUT_TIMEOUT_S,
        max_output_bytes=max_output_bytes,
    )
    tracked = result.stdout.strip() if result.ok else ""
    prefix = "refs/remotes/"
    remote_ref = tracked[len(prefix):] if tracked.startswith(prefix) else ""
    # git이 준 값도 argv에 그대로 들어간다. 선언된 base와 같은 검사를 통과해야 한다.
    if not remote_ref or not _is_usable_base_ref(remote_ref):
        remote_ref = f"origin/{base_branch}"
    if remote_ref == base_branch:
        return (base_branch,)
    return (base_branch, remote_ref)


class _BaseCandidate(NamedTuple):
    """base ref 하나와 그 merge-base. 두 칸이 모두 `str`이라 위치로 두면 조용히 섞인다."""

    ref: str
    oid: str


class _BaselineChoice(NamedTuple):
    """어느 후보에서 기준점을 잡았는가. 세 칸이 모두 `str`이라 위치로 두면 조용히 섞인다."""

    candidate: str
    oid: str
    note: str


def _newest_review_baseline(
    project_root: Path,
    resolved: Sequence[_BaseCandidate],
) -> _BaselineChoice:
    """후보 중 가장 descendant인 merge-base와 그 선택의 근거. `resolved`는 비어 있지 않다.

    먼저 resolve된 후보를 쓰면 뒤처진 로컬 base ref가 항상 이긴다. 로컬 base는
    아무도 전진시키지 않는다(킷의 유일한 fetch는 cleanup 전용이고 remote-tracking
    ref만 갱신한다). 그러면 스냅샷에 이미 upstream에 머지된 커밋이 들어가고,
    리뷰어는 그것을 이 브랜치의 변경으로 읽어 코드로는 지울 수 없는
    request-changes를 낸다.

    순서를 정하지 못한 두 기준점 중에서는 뒤에 선언된 후보(remote-tracking)를
    쓴다. 둘 다 HEAD의 조상이지만 서로를 포함하지 않는 상태에서 하나를 골라야
    하고, 이미 통합된 쪽을 기준으로 삼는 것이 리뷰 범위에 대한 사실에 가깝다.
    그때 다른 후보에서만 닿는 커밋은 diff에 남는다 — rev 하나로는 두 base를
    동시에 뺄 수 없으므로, 그 사실을 note에 적는다.

    note는 비교마다 **누적한다**. 후보가 셋 이상일 때 뒤 비교가 앞의 인정을 덮으면
    머리말은 깨끗한 기준점을 주장하면서 diff에 남은 커밋을 숨긴다 — 이 변경이
    지우려는 실패가 그 자리에서 그대로 돌아온다.
    """
    best = resolved[0]
    notes: list[str] = []
    for candidate in resolved[1:]:
        if candidate.oid == best.oid:
            continue
        if git_proves_ancestor(
            root=project_root,
            ancestor=best.oid,
            descendant=candidate.oid,
            timeout_s=_REVIEW_INPUT_TIMEOUT_S,
        ):
            notes.append(
                f"declared base `{best.ref}` is behind `{candidate.ref}`, so its "
                "merge-base still carries commits that are already merged upstream; "
                f"the baseline above is the `{candidate.ref}` merge-base and those "
                "commits are not in the diff below"
            )
        elif git_proves_ancestor(
            root=project_root,
            ancestor=candidate.oid,
            descendant=best.oid,
            timeout_s=_REVIEW_INPUT_TIMEOUT_S,
        ):
            continue
        else:
            notes.append(
                f"`{best.ref}` and `{candidate.ref}` merge-bases could not be "
                "ordered, so the baseline above is the remote-tracking one "
                f"(`{candidate.ref}`); commits reachable only from `{best.ref}` are "
                "still in the diff below"
            )
        best = candidate
    return _BaselineChoice(candidate=best.ref, oid=best.oid, note="; ".join(notes))


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
    angles: list[dict[str, object]] | tuple[dict[str, object], ...],
    phase: Phase,
    project_root: Path,
    adapter: Adapter,
    *,
    providers: Sequence[str],
) -> list[dict[str, object]]:
    skill_gated = [angle for angle in angles if "requires" in angle]
    required: set[str] = set()
    if skill_gated:
        for provider in providers:
            resolution = phase_skill_resolution(
                adapter.config_root_or(project_root),
                phase.id,
                phase_skills=getattr(phase, "skills", None),
                profile=adapter._profile_snapshot,
                changed_files=adapter._changed_files,
                task_text=adapter._task_text,
                concerns=adapter._concerns,
                host=provider,
            )
            required.update(skill.name for skill in resolution.required)
    return [
        angle
        for angle in angles
        if (
            "requires" not in angle
            or _angle_requirement_met(_angle_requirement_value(angle), required)
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
    providers: Sequence[str] | None = None,
) -> list[ReviewerJob]:
    providers = REVIEW_CLI_NAMES if providers is None else tuple(providers)
    profile_angles = adapter.profile_review_angles()
    angles = _applicable_angles(
        _merge_review_angles(_BASE_REVIEW_ANGLES, profile_angles),
        phase,
        project_root,
        adapter,
        providers=providers,
    )
    jobs: list[ReviewerJob] = []
    base_prompt_by_provider = {
        provider: adapter.render_envelope(
            phase,
            run_dir,
            project_root,
            prompt_variant=f"reviewer-base-{provider}",
            skill_host=provider,
        )
        for provider in providers
    }
    fallback_prompt = (
        ""
        if providers
        else adapter.render_envelope(
            phase,
            run_dir,
            project_root,
            prompt_variant="reviewer-base-host",
            skill_host=adapter.name,
        )
    )
    review_input_prompt = (
        "\n\n## Precomputed review input\n\n"
        f"Read `{review_input.path}` before judging the change. The controller "
        "captured it immediately before launching reviewers: its header names "
        "the diff baseline — the merge-base of the declared base branch when one "
        "is available, or of its remote-tracking counterpart when the declared "
        "base is behind, so changes already committed on this branch are "
        "included — and states in `- note:` lines whether the baseline skipped a "
        "stale declared base, whether the snapshot was truncated, and whether it "
        "is a verified empty diff. The body holds `git status --short` and that "
        "diff. Inspect "
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
        angle_contract = (
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
