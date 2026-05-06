"""Detect which AI CLIs are installed on PATH.

The runner uses this to (a) pick a host adapter when none is forced and
(b) distribute multi-reviewer phases across whichever CLIs are present.
A user with all three CLIs installed gets diverse opinions; a user with
only Claude gets parallel sub-agents within Claude.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class CliInfo:
    name: str             # canonical id: claude / codex / gemini
    binaries: tuple[str, ...]   # candidates to check on PATH
    invoke: tuple[str, ...]     # argv prefix to invoke a one-shot prompt


KNOWN_CLIS: tuple[CliInfo, ...] = (
    # Claude Code one-shot: `claude -p "<prompt>"`
    CliInfo(name="claude", binaries=("claude",), invoke=("-p",)),
    # OpenAI Codex CLI: `codex exec "<prompt>"` (older `codex run` also seen)
    CliInfo(name="codex", binaries=("codex",), invoke=("exec",)),
    # Google Gemini CLI: `gemini -p "<prompt>"`
    CliInfo(name="gemini", binaries=("gemini",), invoke=("-p",)),
)


def detect_available_clis() -> list[CliInfo]:
    """Return CLIs whose binary resolves on PATH."""
    found: list[CliInfo] = []
    for cli in KNOWN_CLIS:
        if any(shutil.which(b) for b in cli.binaries):
            found.append(_normalize_cli(cli))
    return found


def detect_host_cli() -> str | None:
    """Best-effort detection of which AI CLI is hosting the current process.

    Falls back to env-var hints exported by each CLI. Returns the canonical
    id (claude / codex / gemini) or None.
    """
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CLI"):
        return "claude"
    if os.environ.get("CODEX_CLI") or os.environ.get("CODEX_HOME"):
        return "codex"
    if os.environ.get("GEMINI_CLI") or os.environ.get("GEMINI_HOME"):
        return "gemini"
    return None


def cli_by_name(name: str) -> CliInfo | None:
    for cli in KNOWN_CLIS:
        if cli.name == name:
            return _normalize_cli(cli)
    return None


def _normalize_cli(cli: CliInfo) -> CliInfo:
    if cli.name != "codex" or not shutil.which("codex"):
        return cli
    if _codex_supports("exec"):
        return cli
    if _codex_supports("run"):
        return CliInfo(name=cli.name, binaries=cli.binaries, invoke=("run",))
    return cli


def _codex_supports(subcommand: str) -> bool:
    try:
        result = subprocess.run(
            ("codex", subcommand, "--help"),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0
