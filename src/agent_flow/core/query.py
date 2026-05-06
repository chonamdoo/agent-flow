from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_flow.core.phase_artifacts import read_phase_artifact

_GENERATED_SEARCH_EXCLUDES = {
    "RUN_REPORT.md",
    "manifest.json",
    "meta.json",
    "recovery.md",
    "watch.json",
}


@dataclass(frozen=True)
class QueryHit:
    path: Path
    score: int
    line: int
    text: str


def query_run(run_dir: Path, query: str, limit: int = 10) -> list[QueryHit]:
    terms = [term.lower() for term in query.split() if term.strip()]
    if not terms:
        return []
    hits: list[QueryHit] = []
    for path in _searchable_paths(run_dir):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines, start=1):
            lowered = line.lower()
            score = sum(1 for term in terms if term in lowered)
            if score:
                hits.append(QueryHit(path=path, score=score, line=index, text=line.strip()))
    hits.sort(key=lambda hit: (-hit.score, str(hit.path), hit.line))
    return hits[:limit]


def explain_run(run_dir: Path, question: str) -> str:
    hits = query_run(run_dir, question, limit=8)
    lines = ["# Run Explanation", "", f"Question: {question}", ""]
    if not hits:
        lines += ["No matching run evidence found.", ""]
        return "\n".join(lines)
    lines += ["## Evidence", ""]
    for hit in hits:
        rel = hit.path.relative_to(run_dir)
        lines.append(f"- `{rel}:{hit.line}` {hit.text}")
    lines += ["", "## Artifact States", ""]
    for artifact_path in _artifact_paths(run_dir):
        artifact = read_phase_artifact(artifact_path)
        rel = artifact.path.relative_to(run_dir)
        state = artifact.verdict or artifact.status
        lines.append(f"- `{artifact.stage_id}` {state or 'unknown'} evidence={artifact.evidence_type} path=`{rel}`")
    return "\n".join(lines) + "\n"


def _searchable_paths(run_dir: Path) -> list[Path]:
    paths = []
    for pattern in ("*.md", "artifacts/*.md", "handoffs/*.md", "*.json", "*.jsonl"):
        paths.extend(run_dir.glob(pattern))
    return sorted(path for path in paths if path.is_file() and path.name not in _GENERATED_SEARCH_EXCLUDES)


def _artifact_paths(run_dir: Path) -> list[Path]:
    paths = sorted((run_dir / "artifacts").glob("*.md")) if (run_dir / "artifacts").is_dir() else []
    paths.extend(sorted(path for path in run_dir.glob("*.md") if path.name not in {"RUN_REPORT.md", "recovery.md"}))
    return paths
