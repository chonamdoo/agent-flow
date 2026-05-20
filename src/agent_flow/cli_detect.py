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
    # 소비자용 Gemini CLI 경로는 Antigravity CLI로 전환됐으므로 `agy -p "<prompt>"`를 쓴다.
    CliInfo(name="gemini", binaries=("agy", "antigravity"), invoke=("-p",)),
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
    if (
        os.environ.get("ANTIGRAVITY_CLI")
        or os.environ.get("ANTIGRAVITY_HOME")
        or os.environ.get("GEMINI_CLI")
        or os.environ.get("GEMINI_HOME")
    ):
        return "gemini"
    return None


def cli_by_name(name: str) -> CliInfo | None:
    normalized = {
        "agy": "gemini",
        "antigravity": "gemini",
        "antigravity-cli": "gemini",
    }.get(name, name)
    for cli in KNOWN_CLIS:
        if cli.name == normalized:
            return _normalize_cli(cli)
    return None


def _normalize_cli(cli: CliInfo) -> CliInfo:
    if cli.name == "gemini":
        # Antigravity 설치 환경마다 launcher 이름이 달라질 수 있어 실제 발견된 binary로 고정한다.
        for binary in cli.binaries:
            if shutil.which(binary):
                return CliInfo(name=cli.name, binaries=(binary,), invoke=cli.invoke)
        return cli
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
