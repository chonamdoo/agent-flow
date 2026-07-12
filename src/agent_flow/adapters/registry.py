from __future__ import annotations

import os
import shutil

from agent_flow.cli_detect import detect_host_from_env


def detect_adapter() -> str:
    host = detect_host_from_env(os.environ)
    if host is not None:
        return f"{host}-session"
    if shutil.which("codex"):
        return "codex-session"
    if shutil.which("omp"):
        return "omp-session"
    if shutil.which("claude"):
        return "claude-session"
    return "manual"
