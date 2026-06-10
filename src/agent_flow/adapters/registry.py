from __future__ import annotations

import os
import shutil


def detect_adapter() -> str:
    if os.environ.get("CODEX_HOME") or shutil.which("codex"):
        return "codex-session"
    if os.environ.get("CLAUDECODE") or shutil.which("claude"):
        return "claude-session"
    return "manual"
