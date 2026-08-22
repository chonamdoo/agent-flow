"""Load runner-owned review artifacts into a validated routing value."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import cast, Literal, TypedDict, TypeGuard

from agent_flow.core.atomic_io import read_bounded_regular_file
from agent_flow.core.markers import unfenced_markdown_text


ReviewStatus = Literal["ok", "error", "timeout"]
ReviewVerdict = Literal["approve", "request-changes"]
ReviewRouteKey = Literal[
    "approve",
    "request-changes",
    "missing-reviewer",
    "insufficient-reviewers",
    "invalid-evidence",
    "invalid-aggregate",
]
ReviewEvidenceIssue = Literal["invalid", "incomplete-provider"]

REVIEW_LAUNCH_DIGEST_CHARS = 16
REVIEW_EVIDENCE_MAX_BYTES = 32 * 1024 * 1024
_SHA256_HEX_CHARS = 64

REGENERATABLE_REVIEW_ROUTE_KEYS: frozenset[ReviewRouteKey] = frozenset(
    {"missing-reviewer", "invalid-evidence"}
)


def review_route_needs_regeneration(key: ReviewRouteKey) -> bool:
    return key in REGENERATABLE_REVIEW_ROUTE_KEYS

@dataclass(frozen=True)
class ReviewerOutcome:
    job_id: str
    provider: str
    model: str
    effort: str
    status: ReviewStatus
    verdict: ReviewVerdict | None
    required: bool
    artifact: str
    artifact_sha256: str
    prompt_digest: str
    argv_digest: str


@dataclass(frozen=True)
class ReviewEvidence:
    validation: Literal["missing", "invalid", "incomplete", "verified"]
    outcomes: tuple[ReviewerOutcome, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class ReviewEvidenceError:
    code: ReviewEvidenceIssue
    detail: str


def overall_review_route_key(text: str) -> str:
    in_overall_section = False
    verdicts: list[str] = []
    overall_sections = 0
    for line in unfenced_markdown_text(text).splitlines():
        stripped = line.strip()
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped, re.IGNORECASE)
        if heading:
            in_overall_section = bool(
                heading.group(1) == "##"
                and re.fullmatch(
                    r"(?:overall|final)(?:\s+verdict)?",
                    heading.group(2).strip(),
                    re.IGNORECASE,
                )
            )
            if in_overall_section:
                overall_sections += 1
            continue
        if not in_overall_section:
            continue
        match = re.fullmatch(
            r"verdict:\s*(approve|request-changes)",
            stripped,
            re.IGNORECASE,
        )
        if match:
            verdicts.append(match.group(1).lower())
    if overall_sections > 1 or len(verdicts) > 1:
        return "invalid-verdict"
    return verdicts[0] if verdicts else "default"


def multi_review_route_key(evidence: ReviewEvidence) -> ReviewRouteKey:
    if evidence.validation in {"missing", "incomplete"}:
        return "missing-reviewer"
    if evidence.validation != "verified":
        return "invalid-evidence"
    verdicts: list[ReviewVerdict] = []
    for outcome in evidence.outcomes:
        if outcome.status != "ok":
            if outcome.required:
                return "missing-reviewer"
            continue
        if outcome.verdict is None:
            return "invalid-evidence"
        verdicts.append(outcome.verdict)
    if not verdicts:
        return "missing-reviewer"
    if "request-changes" in verdicts:
        return "request-changes"
    if len(verdicts) < 2:
        return "insufficient-reviewers"
    return "approve"


def review_route_key(
    evidence: ReviewEvidence,
    aggregate_text: str,
) -> ReviewRouteKey:
    key = multi_review_route_key(evidence)
    if key != "approve":
        return key
    aggregate_key = overall_review_route_key(aggregate_text)
    if aggregate_key == "request-changes":
        return "request-changes"
    if aggregate_key != "approve":
        return "invalid-aggregate"
    return "approve"


class ReviewEvidenceBindingError(RuntimeError):
    """Run metadata cannot safely publish reviewer evidence."""


class ReviewEvidenceRecord(TypedDict):
    schema_version: Literal[1]
    nonce: str
    results_sha256: str
    phase_entered_at: str
    observed_job_ids: list[str]
    blocking_job_ids: list[str]
    accept_any_provider: bool
    expected_job_ids_by_provider: dict[str, list[str]]
    complete_providers: list[str]


def review_results_path(artifact_root: Path, phase_id: str) -> Path:
    return artifact_root / f"{phase_id}-review-results.json"


def serialize_review_results(
    *,
    phase_id: str,
    run_id: str,
    nonce: str,
    phase_entered_at: str,
    outcomes: Sequence[ReviewerOutcome],
) -> str:
    """Serialize the persisted review-result schema owned by this boundary."""
    payload = {
        "schema_version": 1,
        "phase_id": phase_id,
        "produced_by": {
            "run_id": run_id,
            "nonce": nonce,
            "phase_entered_at": phase_entered_at,
        },
        "outcomes": [
            {
                "job_id": outcome.job_id,
                "provider": outcome.provider,
                "model": outcome.model,
                "effort": outcome.effort,
                "status": outcome.status,
                "verdict": outcome.verdict,
                "required": outcome.required,
                "artifact": outcome.artifact,
                "artifact_sha256": outcome.artifact_sha256,
                "prompt_digest": outcome.prompt_digest,
                "argv_digest": outcome.argv_digest,
            }
            for outcome in outcomes
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def review_evidence_record(
    *,
    nonce: str,
    phase_entered_at: str,
    serialized_results: str,
    outcomes: Sequence[ReviewerOutcome],
    blocking_job_ids: Iterable[str],
    accept_any_provider: bool,
    expected_job_ids_by_provider: Mapping[str, list[str]],
) -> ReviewEvidenceRecord:
    """Build the metadata binding from the same typed outcomes as the payload."""
    expected = {
        provider: list(job_ids)
        for provider, job_ids in expected_job_ids_by_provider.items()
    }
    return {
        "schema_version": 1,
        "nonce": nonce,
        "results_sha256": hashlib.sha256(
            serialized_results.encode("utf-8")
        ).hexdigest(),
        "phase_entered_at": phase_entered_at,
        "observed_job_ids": [outcome.job_id for outcome in outcomes],
        "blocking_job_ids": sorted(blocking_job_ids),
        "accept_any_provider": accept_any_provider,
        "expected_job_ids_by_provider": expected,
        "complete_providers": complete_review_providers(expected, outcomes),
    }


def load_review_evidence(
    *,
    artifact_root: Path,
    phase_id: str,
    run_meta: Mapping[str, object],
) -> ReviewEvidence:
    results_path = review_results_path(artifact_root, phase_id)
    try:
        raw, _size = read_bounded_regular_file(
            results_path,
            max_bytes=REVIEW_EVIDENCE_MAX_BYTES,
        )
    except FileNotFoundError:
        return ReviewEvidence("missing", detail="review results artifact is missing")
    except OSError:
        return ReviewEvidence(
            "invalid",
            detail="review results cannot be read safely",
        )
    try:
        text = raw.decode("utf-8")
        payload = json.loads(text)
    except UnicodeDecodeError:
        return ReviewEvidence("invalid", detail="review results are not UTF-8")
    except json.JSONDecodeError as exc:
        return ReviewEvidence(
            "invalid",
            detail=f"review results contain invalid JSON at line {exc.lineno}",
        )
    except RecursionError:
        return ReviewEvidence("invalid", detail="review results JSON is too deeply nested")
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("phase_id") != phase_id
        or not isinstance(payload.get("outcomes"), list)
    ):
        return ReviewEvidence("invalid", detail="review results schema does not match")
    outcomes: list[ReviewerOutcome] = []
    seen: set[str] = set()
    for index, item in enumerate(payload["outcomes"]):
        outcome, invalid_field = _review_outcome(item)
        if outcome is None:
            return ReviewEvidence(
                "invalid",
                detail=(
                    f"review outcome {index} has invalid field: "
                    f"{invalid_field}"
                ),
            )
        if outcome.job_id in seen:
            return ReviewEvidence(
                "invalid",
                detail=f"duplicate review outcome: {outcome.job_id}",
            )
        seen.add(outcome.job_id)
        if not _review_artifact_matches(artifact_root, outcome):
            return ReviewEvidence(
                "invalid",
                detail=f"review artifact does not match outcome: {outcome.job_id}",
            )
        outcomes.append(outcome)
    evidence_by_phase = run_meta.get("review_evidence")
    expected: object = (
        evidence_by_phase.get(phase_id)
        if isinstance(evidence_by_phase, dict)
        else None
    )
    run_id = run_meta.get("run_id")
    review_nonce = run_meta.get("review_nonce")
    phase_entered_at = run_meta.get("phase_entered_at")
    if not _review_evidence_record_shape(expected):
        return ReviewEvidence(
            "invalid",
            tuple(outcomes),
            "run metadata cannot bind review evidence",
        )
    if (
        not isinstance(run_id, str)
        or not isinstance(review_nonce, str)
        or not isinstance(phase_entered_at, str)
    ):
        return ReviewEvidence(
            "invalid",
            tuple(outcomes),
            "run metadata cannot bind review evidence",
        )
    error = _review_evidence_error(
        text,
        payload,
        outcomes=tuple(outcomes),
        expected=expected,
        run_id=run_id,
        review_nonce=review_nonce,
        phase_entered_at=phase_entered_at,
    )
    if error is not None and error.code == "incomplete-provider":
        return ReviewEvidence("incomplete", tuple(outcomes), error.detail)
    if error is not None:
        return ReviewEvidence("invalid", tuple(outcomes), error.detail)
    return ReviewEvidence("verified", tuple(outcomes))


def review_route_evidence(
    artifact_root: Path,
    phase_id: str,
    aggregate_text: str,
    *,
    run_meta: Mapping[str, object],
) -> tuple[ReviewEvidence, ReviewRouteKey]:
    evidence = load_review_evidence(
        artifact_root=artifact_root,
        phase_id=phase_id,
        run_meta=run_meta,
    )
    return evidence, review_route_key(evidence, aggregate_text)


def reviewer_output_verdict(text: str) -> ReviewVerdict | None:
    verdicts = [
        match.group(1).lower()
        for line in unfenced_markdown_text(text).splitlines()
        if (
            match := re.fullmatch(
                r"\s*verdict:\s*(approve|request-changes)\s*",
                line,
                flags=re.IGNORECASE,
            )
        )
    ]
    if len(verdicts) != 1:
        return None
    return "approve" if verdicts[0] == "approve" else "request-changes"


def complete_provider_names(
    expected_by_provider: Mapping[str, list[str]],
    successful_job_ids: Iterable[str],
) -> list[str]:
    successful = set(successful_job_ids)
    return [
        provider
        for provider, expected_ids in expected_by_provider.items()
        if expected_ids and set(expected_ids).issubset(successful)
    ]


def complete_review_providers(
    expected_by_provider: Mapping[str, list[str]],
    outcomes: Iterable[ReviewerOutcome],
) -> list[str]:
    outcomes_by_id = {outcome.job_id: outcome for outcome in outcomes}
    successful = {
        job_id
        for provider, expected_ids in expected_by_provider.items()
        for job_id in expected_ids
        if (
            (outcome := outcomes_by_id.get(job_id)) is not None
            and outcome.provider == provider
            and outcome.status == "ok"
            and outcome.verdict in {"approve", "request-changes"}
        )
    }
    return complete_provider_names(expected_by_provider, successful)


def _review_outcome(value: object) -> tuple[ReviewerOutcome | None, str]:
    if not isinstance(value, dict):
        return None, "outcome"
    job_id = value.get("job_id")
    provider = value.get("provider")
    model = value.get("model")
    effort = value.get("effort")
    status = value.get("status")
    verdict = value.get("verdict")
    required = value.get("required")
    artifact = value.get("artifact")
    artifact_sha256 = value.get("artifact_sha256")
    prompt_digest = value.get("prompt_digest")
    argv_digest = value.get("argv_digest")
    if not isinstance(job_id, str) or not job_id:
        return None, "job_id"
    if not isinstance(provider, str) or not provider:
        return None, "provider"
    if not isinstance(model, str) or not model:
        return None, "model"
    if not isinstance(effort, str) or not effort:
        return None, "effort"
    if status not in {"ok", "error", "timeout"}:
        return None, "status"
    if verdict not in {None, "approve", "request-changes"}:
        return None, "verdict"
    if not isinstance(required, bool):
        return None, "required"
    if not isinstance(artifact, str) or not artifact:
        return None, "artifact"
    if (
        not isinstance(artifact_sha256, str)
        or re.fullmatch(
            rf"[0-9a-f]{{{_SHA256_HEX_CHARS}}}",
            artifact_sha256,
        )
        is None
    ):
        return None, "artifact_sha256"
    if (
        not isinstance(prompt_digest, str)
        or re.fullmatch(
            rf"[0-9a-f]{{{REVIEW_LAUNCH_DIGEST_CHARS}}}",
            prompt_digest,
        )
        is None
    ):
        return None, "prompt_digest"
    if (
        not isinstance(argv_digest, str)
        or re.fullmatch(
            rf"[0-9a-f]{{{REVIEW_LAUNCH_DIGEST_CHARS}}}",
            argv_digest,
        )
        is None
    ):
        return None, "argv_digest"
    if (status == "ok" and verdict is None) or (
        status != "ok" and verdict is not None
    ):
        return None, "verdict"
    return (
        ReviewerOutcome(
            job_id=job_id,
            provider=provider,
            model=model,
            effort=effort,
            status=cast(ReviewStatus, status),
            verdict=(
                cast(ReviewVerdict, verdict)
                if verdict is not None
                else None
            ),
            required=required,
            artifact=artifact,
            artifact_sha256=artifact_sha256,
            prompt_digest=prompt_digest,
            argv_digest=argv_digest,
        ),
        "",
    )


def _review_evidence_error(
    text: str,
    payload: dict[str, object],
    *,
    outcomes: tuple[ReviewerOutcome, ...],
    expected: ReviewEvidenceRecord,
    run_id: str,
    review_nonce: str,
    phase_entered_at: str,
) -> ReviewEvidenceError | None:
    nonce = expected["nonce"]
    results_sha256 = expected["results_sha256"]
    recorded_phase_entered_at = expected["phase_entered_at"]
    observed = expected["observed_job_ids"]
    blocking = expected["blocking_job_ids"]
    accept_any = expected["accept_any_provider"]
    expected_by_provider = expected["expected_job_ids_by_provider"]
    recorded_complete = expected["complete_providers"]
    if nonce != review_nonce or recorded_phase_entered_at != phase_entered_at:
        return ReviewEvidenceError(
            "invalid",
            "review evidence binding does not match the current run attempt",
        )
    expected_provider_by_job = {
        job_id: provider
        for provider, job_ids in expected_by_provider.items()
        for job_id in job_ids
    }
    if len(expected_provider_by_job) != sum(
        len(job_ids) for job_ids in expected_by_provider.values()
    ):
        return ReviewEvidenceError(
            "invalid",
            "expected review jobs contain duplicate identifiers",
        )
    if len(blocking) != len(set(blocking)):
        return ReviewEvidenceError(
            "invalid",
            "blocking review jobs contain duplicate identifiers",
        )
    outcome_ids = [outcome.job_id for outcome in outcomes]
    if not set(blocking).issubset(outcome_ids):
        return ReviewEvidenceError(
            "invalid",
            "blocking review jobs are missing outcomes",
        )
    if not set(outcome_ids).issubset(expected_provider_by_job):
        return ReviewEvidenceError(
            "invalid",
            "review outcomes contain an unexpected job",
        )
    if not all(provider in expected_by_provider for provider in recorded_complete):
        return ReviewEvidenceError(
            "invalid",
            "complete review provider is not expected",
        )
    produced_by = payload.get("produced_by")
    if (
        not isinstance(produced_by, dict)
        or produced_by.get("run_id") != run_id
        or produced_by.get("nonce") != nonce
        or produced_by.get("phase_entered_at") != phase_entered_at
    ):
        return ReviewEvidenceError(
            "invalid",
            "review results are bound to another run attempt",
        )
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != results_sha256:
        return ReviewEvidenceError(
            "invalid",
            "review results digest does not match run metadata",
        )
    if outcome_ids != observed:
        return ReviewEvidenceError(
            "invalid",
            "observed review jobs do not match result order",
        )
    required_outcome_ids = {
        outcome.job_id for outcome in outcomes if outcome.required
    }
    if required_outcome_ids != set(blocking):
        return ReviewEvidenceError(
            "invalid",
            "blocking review jobs do not match required outcomes",
        )
    computed_complete = complete_review_providers(
        expected_by_provider,
        outcomes,
    )
    if computed_complete != recorded_complete:
        return ReviewEvidenceError(
            "invalid",
            "complete review providers do not match outcomes",
        )
    if accept_any and not computed_complete:
        return ReviewEvidenceError(
            "incomplete-provider",
            "no provider completed every expected review",
        )
    return None


def _review_artifact_matches(root: Path, outcome: ReviewerOutcome) -> bool:
    if Path(outcome.artifact).name != outcome.artifact:
        return False
    artifact = root / outcome.artifact
    try:
        content, size = read_bounded_regular_file(
            artifact,
            max_bytes=REVIEW_EVIDENCE_MAX_BYTES,
        )
        artifact_text = content.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return (
        size == len(content)
        and hashlib.sha256(content).hexdigest() == outcome.artifact_sha256
        and (
            outcome.status != "ok"
            or reviewer_output_verdict(artifact_text) == outcome.verdict
        )
    )


def _review_evidence_record_shape(
    value: object,
) -> TypeGuard[ReviewEvidenceRecord]:
    # Metadata bindings are strict snapshots; schema_version forces an explicit
    # migration instead of accepting unknown fields from another release.
    if (
        not isinstance(value, dict)
        or set(value) != set(ReviewEvidenceRecord.__annotations__)
    ):
        return False
    nonce = value.get("nonce")
    results_sha256 = value.get("results_sha256")
    phase_entered_at = value.get("phase_entered_at")
    schema_version = value.get("schema_version")
    return (
        schema_version == 1
        and isinstance(nonce, str)
        and re.fullmatch(r"[0-9a-f]{32}", nonce) is not None
        and isinstance(results_sha256, str)
        and re.fullmatch(
            rf"[0-9a-f]{{{_SHA256_HEX_CHARS}}}",
            results_sha256,
        )
        is not None
        and isinstance(phase_entered_at, str)
        and bool(phase_entered_at)
        and _string_list(value.get("observed_job_ids"))
        and _string_list(value.get("blocking_job_ids"))
        and isinstance(value.get("accept_any_provider"), bool)
        and _expected_job_map(value.get("expected_job_ids_by_provider"))
        and _string_list(value.get("complete_providers"))
    )


def _string_list(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(
        isinstance(item, str) and item for item in value
    )


def _expected_job_map(value: object) -> TypeGuard[dict[str, list[str]]]:
    return isinstance(value, dict) and all(
        isinstance(provider, str)
        and provider
        and _string_list(job_ids)
        and bool(job_ids)
        for provider, job_ids in value.items()
    )
