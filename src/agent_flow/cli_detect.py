"""Detect which AI CLIs are installed on PATH.

The runner uses this to (a) pick a host adapter when none is forced and
(b) distribute multi-reviewer phases across whichever CLIs are present.
A user with both CLIs installed gets diverse opinions; a user with
only Claude gets parallel sub-agents within Claude.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class CliInfo:
    name: str             # canonical id: claude / codex
    binaries: tuple[str, ...]   # candidates to check on PATH
    invoke: tuple[str, ...]     # argv prefix to invoke a one-shot prompt


KNOWN_CLIS: tuple[CliInfo, ...] = (
    # Claude Code one-shot: `claude -p "<prompt>"`
    CliInfo(name="claude", binaries=("claude",), invoke=("-p",)),
    # OpenAI Codex CLI: `codex exec "<prompt>"` (older `codex run` also seen)
    CliInfo(name="codex", binaries=("codex",), invoke=("exec",)),
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
    id (claude / codex) or None.

    주의: codex는 PATH 휴리스틱을 쓰므로 호스트가 아닌 일반 셸에서도 codex
    바이너리가 설치돼 있으면 "codex"를 반환할 수 있다. 호스트 확정 신호가
    필요한 호출자는 env 힌트(CLAUDECODE/CODEX_CLI 등)가 있는 경우만 신뢰한다.
    """
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CLI"):
        return "claude"
    if os.environ.get("CODEX_CLI") or os.environ.get("CODEX_HOME"):
        return "codex"
    # Codex CLI는 호스트 env 힌트를 export하지 않는 버전이 있어 PATH로 보강한다.
    # Claude는 항상 CLAUDECODE를 export하므로 PATH fallback에서 제외한다.
    if shutil.which("codex"):
        return "codex"
    return None


def cli_by_name(name: str) -> CliInfo | None:
    normalized = name
    for cli in KNOWN_CLIS:
        if cli.name == normalized:
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
