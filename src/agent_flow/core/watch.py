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
    paths_by_stage: dict[str, Path] = {}
    for path in paths:
        artifact = read_phase_artifact(path)
        previous = paths_by_stage.get(artifact.stage_id)
        if previous is None or _artifact_priority(path, run_dir) > _artifact_priority(previous, run_dir):
            paths_by_stage[artifact.stage_id] = path
    return sorted(paths_by_stage.values())


def _needs_continue(run_dir: Path, artifacts: list[Path]) -> bool:
    manifest = run_dir / "manifest.json"
    meta = run_dir / "meta.json"
    state_files = [path for path in (manifest, meta) if path.exists()]
    if not state_files or not artifacts:
        return False
    latest_state_mtime = max(path.stat().st_mtime for path in state_files)
    latest_artifact_mtime = max(path.stat().st_mtime for path in artifacts)
    return latest_artifact_mtime > latest_state_mtime


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


def _artifact_priority(path: Path, run_dir: Path) -> int:
    return 1 if path.parent == run_dir / "artifacts" else 0
