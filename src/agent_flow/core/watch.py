from __future__ import annotations

import json
from pathlib import Path

from agent_flow.core.phase_artifacts import read_phase_artifact


def write_watch_snapshot(run_dir: Path) -> Path:
    path = run_dir / "watch.json"
    artifacts = _artifact_paths(run_dir)
    buckets = _artifact_state_buckets(artifacts)
    payload = {
        "run_dir": str(run_dir),
        "artifact_count": len(artifacts),
        "needs_continue": _needs_continue(run_dir, artifacts),
        "blocked": buckets["blocked"],
        "pending": buckets["pending"],
    }
    path.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
    return path


def _artifact_paths(run_dir: Path) -> list[Path]:
    paths = sorted((run_dir / "artifacts").glob("*.md")) if (run_dir / "artifacts").is_dir() else []
    paths.extend(sorted(path for path in run_dir.glob("*.md") if path.name not in {"RUN_REPORT.md", "recovery.md"}))
    return paths


def _needs_continue(run_dir: Path, artifacts: list[Path]) -> bool:
    manifest = run_dir / "manifest.json"
    meta = run_dir / "meta.json"
    state_file = manifest if manifest.exists() else meta
    if not state_file.exists() or not artifacts:
        return False
    latest_artifact_mtime = max(path.stat().st_mtime for path in artifacts)
    return latest_artifact_mtime > state_file.stat().st_mtime


def _artifact_state_buckets(artifacts: list[Path]) -> dict[str, list[str]]:
    buckets = {"blocked": [], "pending": []}
    for path in artifacts:
        artifact = read_phase_artifact(path)
        state = {artifact.status.lower(), artifact.verdict.lower()}
        if state & {"blocked", "request-changes", "failed", "error"}:
            buckets["blocked"].append(path.name)
        elif "pending" in state:
            buckets["pending"].append(path.name)
    return buckets
