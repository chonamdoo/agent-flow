from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from agent_flow.core.report import write_run_report

ReviewVerdict = Literal["LGTM", "NEEDS_CHANGES"]


@dataclass(frozen=True)
class ReviewInput:
    path: Path
    verdict: ReviewVerdict
    findings: tuple[str, ...]


@dataclass(frozen=True)
class ReviewSummary:
    verdict: ReviewVerdict
    reviews: tuple[ReviewInput, ...]
    findings: tuple[str, ...]


def summarize_reviews(paths: list[Path]) -> ReviewSummary:
    reviews = tuple(_read_review(path) for path in paths)
    findings = tuple(finding for review in reviews for finding in review.findings)
    verdict: ReviewVerdict = "NEEDS_CHANGES" if findings else "LGTM"
    return ReviewSummary(verdict=verdict, reviews=reviews, findings=findings)


def write_review_summary(*, run_dir: Path, summary: ReviewSummary) -> Path:
    path = run_dir / "review-summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{json.dumps(_summary_payload(summary), indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )
    write_run_report(run_dir)
    return path


def _read_review(path: Path) -> ReviewInput:
    content = path.read_text(encoding="utf-8")
    verdict = _parse_verdict(content)
    findings = _parse_findings(content)
    if verdict == "NEEDS_CHANGES" and not findings:
        findings = ("Review marked NEEDS_CHANGES without structured findings.",)
    return ReviewInput(path=path, verdict=verdict, findings=findings)


def _parse_verdict(content: str) -> ReviewVerdict:
    verdict_heading_seen = False
    for line in content.splitlines():
        normalized = line.strip().upper()
        if normalized in {"## VERDICT", "# VERDICT"}:
            verdict_heading_seen = True
            continue
        if normalized in {
            "LGTM",
            "VERDICT: LGTM",
            "- VERDICT: LGTM",
            "APPROVE",
            "VERDICT: APPROVE",
            "- VERDICT: APPROVE",
        }:
            return "LGTM"
        if normalized in {
            "NEEDS_CHANGES",
            "VERDICT: NEEDS_CHANGES",
            "- VERDICT: NEEDS_CHANGES",
            "REQUEST-CHANGES",
            "VERDICT: REQUEST-CHANGES",
            "- VERDICT: REQUEST-CHANGES",
        }:
            return "NEEDS_CHANGES"
        if verdict_heading_seen and normalized:
            return "NEEDS_CHANGES"
    return "NEEDS_CHANGES"


def _parse_findings(content: str) -> tuple[str, ...]:
    findings: list[str] = []
    in_findings = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.lower() in {"## findings", "# findings"}:
            in_findings = True
            continue
        if in_findings and stripped.startswith("#"):
            break
        if in_findings and stripped.startswith("- "):
            value = stripped[2:].strip()
            if value and value.lower() != "none":
                findings.append(value)
    return tuple(findings)


def _summary_payload(summary: ReviewSummary) -> dict[str, object]:
    return {
        "verdict": summary.verdict,
        "findings": list(summary.findings),
        "reviews": [
            {
                **asdict(review),
                "path": str(review.path),
                "findings": list(review.findings),
            }
            for review in summary.reviews
        ],
    }
