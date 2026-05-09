#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / "CONTEXT.md"
ABSOLUTE_PATH_RE = re.compile(
    r"(?<![\w.-])(?:/Users/|/home/|/private/var/|/workspace/|/tmp/|/var/|/opt/|/mnt/|[A-Za-z]:[\\/])"
)
CONFLICT_RE = re.compile(r"^(<<<<<<<|=======|>>>>>>>)", re.MULTILINE)
FUTURE_TERMS = ("Worker", "Task", "Team State", "Mailbox", "Heartbeat")


def main() -> int:
    errors: list[str] = []
    _check_context(errors)
    _check_repo_docs(errors)
    _check_agent_flow_artifacts(errors)
    if errors:
        for error in errors:
            print(f"context-docs: FAIL: {error}")
        return 1
    print("context-docs: OK")
    return 0


def _check_context(errors: list[str]) -> None:
    if not CONTEXT.exists():
        errors.append("CONTEXT.md missing")
        return
    text = CONTEXT.read_text(encoding="utf-8")
    lines = text.splitlines()
    if len(lines) >= 200:
        errors.append(f"CONTEXT.md has {len(lines)} lines; must be under 200 lines")
    _check_text("CONTEXT.md", text, errors)
    current = _section_until_next_heading(text, "Current Vocabulary")
    if current is None:
        errors.append("CONTEXT.md must include Current Vocabulary before Future Vocabulary")
        current = ""
    for term in FUTURE_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", current, flags=re.IGNORECASE):
            errors.append(f"future term used in current vocabulary: {term}")
    if "Future Vocabulary" not in text:
        errors.append("CONTEXT.md must separate current and future vocabulary")


def _check_repo_docs(errors: list[str]) -> None:
    for rel in _required_context_files():
        if not (ROOT / rel).exists():
            errors.append(f"required context file missing: {rel}")
    candidates = [
        ROOT / "AGENTS.md",
        ROOT / ".Codex" / "rules",
        ROOT / "docs",
        ROOT / "workflows",
    ]
    for path in candidates:
        if path.is_file():
            _check_text(str(path.relative_to(ROOT)), path.read_text(encoding="utf-8"), errors)
        elif path.is_dir():
            for file in path.rglob("*.md"):
                _check_text(str(file.relative_to(ROOT)), file.read_text(encoding="utf-8"), errors)


def _check_agent_flow_artifacts(errors: list[str]) -> None:
    agent_flow = ROOT / ".agent-flow"
    if not agent_flow.exists():
        return
    for file in agent_flow.rglob("*"):
        if not file.is_file() or file.suffix not in {".md", ".json", ".jsonl"}:
            continue
        rel = file.relative_to(ROOT)
        text = file.read_text(encoding="utf-8", errors="replace")
        if ABSOLUTE_PATH_RE.search(text):
            errors.append(f"absolute local path in Agent Flow artifact: {rel}")


def _check_text(label: str, text: str, errors: list[str]) -> None:
    if CONFLICT_RE.search(text):
        errors.append(f"conflict marker in {label}")
    if ABSOLUTE_PATH_RE.search(text):
        errors.append(f"absolute local path in {label}")


def _required_context_files() -> tuple[Path, ...]:
    return (
        Path(".Codex/rules/context/domain-glossary-full.md"),
        Path(".Codex/rules/context/research-context.md"),
        Path(".Codex/rules/context/paper-runtime-context.md"),
        Path(".Codex/rules/context/agent-flow-context-map.md"),
        Path(".Codex/rules/context/context-maintenance.md"),
    )


def _section(text: str, start: str, end: str) -> str | None:
    pattern = rf"^## {re.escape(start)}\s*$([\s\S]*?)^## {re.escape(end)}\s*$"
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1) if match else None


def _section_until_next_heading(text: str, start: str) -> str | None:
    pattern = rf"^## {re.escape(start)}\s*$([\s\S]*?)(?=^## |\Z)"
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1) if match else None


if __name__ == "__main__":
    sys.exit(main())
