from __future__ import annotations

import os
import shutil


def detect_adapter() -> str:
    if os.environ.get("OMP_PROFILE"):
        return "omp-session"
    if os.environ.get("CODEX_CLI") or os.environ.get("CODEX_HOME"):
        return "codex-session"
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CLI"):
        return "claude-session"
    if shutil.which("codex"):
        return "codex-session"
    if shutil.which("omp"):
        return "omp-session"
    if shutil.which("claude"):
        return "claude-session"
    return "manual"
