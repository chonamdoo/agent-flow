from __future__ import annotations

from pathlib import Path


def init_project(root: Path) -> None:
    for relative in (
        ".agent-flow/runs",
        ".agent-flow/state",
        ".agent-flow/handoffs",
        ".agent-flow/team",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)


def write_prompt(*, root: Path, run_dir: Path, stage_id: str, content: str) -> Path:
    init_project(root)
    path = run_dir / "prompts" / f"{stage_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path

