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
        _host_provider_any(
            "antigravity-cli",
            executables=("agy", "antigravity"),
            env_vars=("ANTIGRAVITY_CLI", "ANTIGRAVITY_HOME", "GEMINI_CLI", "GEMINI_HOME"),
        ),
    ]


def _host_provider(name: str, *, executable: str, env_var: str | None) -> HostProviderStatus:
    resolved = shutil.which(executable)
    available = resolved is not None or (env_var is not None and bool(os.environ.get(env_var)))
    return HostProviderStatus(
        name=name,
        command=resolved or executable,
        available=available,
    )


def _host_provider_any(name: str, *, executables: tuple[str, ...], env_vars: tuple[str, ...]) -> HostProviderStatus:
    # Antigravity CLI는 환경에 따라 `agy` 또는 `antigravity` launcher로 설치될 수 있다.
    for executable in executables:
        resolved = shutil.which(executable)
        if resolved is not None:
            return HostProviderStatus(name=name, command=resolved, available=True)
    return HostProviderStatus(
        name=name,
        command=executables[0],
        available=any(os.environ.get(env_var) for env_var in env_vars),
    )
