from __future__ import annotations

import json
from pathlib import Path

from agent_flow.core.phase_artifacts import PhaseArtifact, read_phase_artifact


def write_run_report(run_dir: Path) -> Path:
    path = run_dir / "RUN_REPORT.md"
    artifacts = _collect_artifacts(run_dir)
    manifest = _read_json(run_dir / "manifest.json") or _read_json(run_dir / "meta.json")
    gate_results = _read_json(run_dir / "gate-results.json")
    review_summary = _read_json(run_dir / "review-summary.json")
    artifact_blocked_count = sum(1 for artifact in artifacts if _is_blocked(artifact))
    review_blocked = _review_blocked(review_summary) and artifact_blocked_count == 0
    lines = [
        "# Run Report",
        "",
        "## Summary",
        "",
        f"- Run: {_manifest_value(manifest, ('run_id',), run_dir.name)}",
        f"- Workflow: {_manifest_value(manifest, ('workflow_id', 'workflow'), 'unknown')}",
        f"- Task: {_manifest_value(manifest, ('task',), '')}",
        f"- Artifacts: {len(artifacts)}",
        f"- Blocked: {artifact_blocked_count + int(review_blocked)}",
        f"- Stale: {sum(1 for artifact in artifacts if artifact.stale)}",
        "",
        "## Artifacts",
        "",
    ]
    if artifacts:
        for artifact in artifacts:
            rel = artifact.path.relative_to(run_dir)
            detail = [
                f"- `{artifact.stage_id}`",
                f"path=`{rel}`",
                f"status={artifact.status}",
            ]
            if artifact.verdict:
                detail.append(f"verdict={artifact.verdict}")
            detail.append(f"evidence={artifact.evidence_type}")
            detail.append(f"confidence={artifact.confidence}")
            if artifact.stale:
                detail.append("stale=true")
            lines.append(" ".join(detail))
    else:
        lines.append("- None")
    lines += ["", "## Gates", ""]
    if isinstance(gate_results, list):
        for gate in gate_results:
            if isinstance(gate, dict):
                status = "passed" if gate.get("passed") else "failed"
                lines.append(f"- `{gate.get('gate_id', 'unknown')}` {status}")
    else:
        lines.append("- Not recorded")
    lines += ["", "## Review Summary", ""]
    if isinstance(review_summary, dict):
        verdict = str(review_summary.get("verdict") or "unknown")
        finding_count = len(review_summary.get("findings") or [])
        lines.append(f"- Verdict: {verdict}")
        lines.append(f"- Findings: {finding_count}")
    else:
        lines.append("- Not recorded")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _collect_artifacts(run_dir: Path) -> list[PhaseArtifact]:
    paths = sorted((run_dir / "artifacts").glob("*.md")) if (run_dir / "artifacts").is_dir() else []
    paths.extend(sorted(path for path in run_dir.glob("*.md") if path.name not in {"RUN_REPORT.md", "recovery.md"}))
    artifacts_by_stage: dict[str, PhaseArtifact] = {}
    for path in paths:
        artifact = read_phase_artifact(path)
        previous = artifacts_by_stage.get(artifact.stage_id)
        if previous is None or _artifact_priority(artifact.path, run_dir) > _artifact_priority(previous.path, run_dir):
            artifacts_by_stage[artifact.stage_id] = artifact
    return sorted(artifacts_by_stage.values(), key=lambda artifact: str(artifact.path))


def _read_json(path: Path) -> object | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _manifest_value(manifest: object | None, keys: tuple[str, ...], default: str) -> str:
    if not isinstance(manifest, dict):
        return default
    for key in keys:
        value = manifest.get(key)
        if value:
            return str(value)
    return default


def _is_blocked(artifact: PhaseArtifact) -> bool:
    values = {artifact.status.lower(), artifact.verdict.lower()}
    return bool(values & {"blocked", "request-changes", "failed", "error"})


def _review_blocked(summary: object | None) -> bool:
    return isinstance(summary, dict) and str(summary.get("verdict") or "").upper() == "NEEDS_CHANGES"


def _artifact_priority(path: Path, run_dir: Path) -> int:
    return 1 if path.parent == run_dir / "artifacts" else 0
