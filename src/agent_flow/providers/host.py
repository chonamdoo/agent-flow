from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from agent_flow.cli_detect import detect_host_from_env


@dataclass(frozen=True)
class HostProviderStatus:
    name: str
    command: str
    available: bool


def list_host_providers() -> list[HostProviderStatus]:
    active_host = detect_host_from_env(os.environ)
    return [
        HostProviderStatus(name="manual", command="manual", available=True),
        _host_provider("codex-session", executable="codex", active=active_host == "codex"),
        _host_provider("claude-session", executable="claude", active=active_host == "claude"),
        _host_provider("omp-session", executable="omp", active=active_host == "omp"),
    ]


def _host_provider(name: str, *, executable: str, active: bool) -> HostProviderStatus:
    resolved = shutil.which(executable)
    return HostProviderStatus(
        name=name,
        command=resolved or executable,
        available=resolved is not None or active,
    )

