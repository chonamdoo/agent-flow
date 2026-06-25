from __future__ import annotations

import os
import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class HostProviderStatus:
    name: str
    command: str
    available: bool


def list_host_providers() -> list[HostProviderStatus]:
    return [
        HostProviderStatus(name="manual", command="manual", available=True),
        _host_provider("codex-session", executable="codex", env_var="CODEX_HOME"),
        _host_provider("claude-session", executable="claude", env_var="CLAUDECODE"),
        _host_provider("omp-session", executable="omp", env_var="OMP_PROFILE"),
    ]


def _host_provider(name: str, *, executable: str, env_var: str | None) -> HostProviderStatus:
    resolved = shutil.which(executable)
    available = resolved is not None or (env_var is not None and bool(os.environ.get(env_var)))
    return HostProviderStatus(
        name=name,
        command=resolved or executable,
        available=available,
    )


