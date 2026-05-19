from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


CONTRACT_DIRS = ("sources", "tool_outputs", "scratch")
MAX_CONTEXT_CHARS = 12000
MAX_BRIEF_CHARS = 4000


def context_root(*, root: Path, run_dir: Path | None = None) -> Path:
    return (run_dir / "context") if run_dir is not None else root / ".agent-flow" / "context"


def ensure_context_contract(*, root: Path, run_dir: Path | None = None) -> Path:
    base = context_root(root=root, run_dir=run_dir)
    base.mkdir(parents=True, exist_ok=True)
    for name in CONTRACT_DIRS:
        (base / name).mkdir(parents=True, exist_ok=True)
    context_md = base / "context.md"
    if not context_md.exists():
        context_md.write_text("# Context\n\nLarge outputs stay in files; prompts reference paths only.\n", encoding="utf-8")
    events = base / "events.jsonl"
    if not events.exists():
        events.write_text("", encoding="utf-8")
    return base


def append_context_event(*, root: Path, event: str, details: dict[str, object], run_dir: Path | None = None) -> Path:
    base = ensure_context_contract(root=root, run_dir=run_dir)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "details": details,
    }
    events = base / "events.jsonl"
    with events.open("a", encoding="utf-8") as fh:
        fh.write(f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n")
    return events


def offload_tool_output(*, root: Path, name: str, content: str, run_dir: Path | None = None) -> Path:
    base = ensure_context_contract(root=root, run_dir=run_dir)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "-" for ch in name).strip("-") or "output"
    output_path = base / "tool_outputs" / f"{safe_name}-{digest}.txt"
    output_path.write_text(content, encoding="utf-8")
    append_context_event(
        root=root,
        run_dir=run_dir,
        event="tool_output_offloaded",
        details={"path": str(output_path), "bytes": len(content.encode("utf-8"))},
    )
    return output_path


def write_system_invariants(*, root: Path, invariants: list[str]) -> Path:
    path = root / ".agent-flow" / "system-invariants.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# System Invariants", ""]
    lines.extend(f"- {item}" for item in invariants)
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def check_system_invariants(*, root: Path, run_dir: Path | None = None) -> list[str]:
    failures: list[str] = []
    base = ensure_context_contract(root=root, run_dir=run_dir)
    context_md = base / "context.md"
    if context_md.exists() and len(context_md.read_text(encoding="utf-8")) > MAX_CONTEXT_CHARS:
        failures.append("context.md exceeds length limit; move detail to sources/ or tool_outputs/ and reference paths")
    brief_md = base / "brief.md"
    if brief_md.exists() and len(brief_md.read_text(encoding="utf-8")) > MAX_BRIEF_CHARS:
        failures.append("brief.md exceeds length limit; move detail to files and reference paths")
    invariants = root / ".agent-flow" / "system-invariants.md"
    if invariants.exists():
        text = invariants.read_text(encoding="utf-8")
        for marker in ("status/next_command", "worktree", "path-only"):
            if marker not in text:
                failures.append(f"system-invariants.md missing marker: {marker}")
    return failures
