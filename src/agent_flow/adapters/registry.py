from __future__ import annotations

import os
import shutil


def detect_adapter() -> str:
    if os.environ.get("CODEX_HOME") or shutil.which("codex"):
        return "codex-session"
    if os.environ.get("CLAUDECODE") or shutil.which("claude"):
        return "claude-session"
    # Gemini CLI의 consumer 경로는 Antigravity CLI로 전환됐으므로 새 launcher를 우선 탐지한다.
    if (
        os.environ.get("ANTIGRAVITY_CLI")
        or os.environ.get("ANTIGRAVITY_HOME")
        or os.environ.get("GEMINI_CLI")
        or os.environ.get("GEMINI_HOME")
        or shutil.which("agy")
        or shutil.which("antigravity")
    ):
        return "antigravity-cli"
    return "manual"
