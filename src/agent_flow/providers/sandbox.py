"""Platform backend selection.

Kept separate from the backends so callers depend on the protocol and the
choice lives in one place. Adding Linux means adding a branch here and a new
adapter, not touching any spawn site.
"""
from __future__ import annotations

import sys

from agent_flow.core.provider_sandbox import SandboxBackend, SandboxCapability
from agent_flow.providers.seatbelt import SeatbeltBackend


class UnsupportedPlatformBackend:
    """Refuses every spawn on a platform with no implemented backend.

    A null object rather than `None`: callers keep one code path, and the
    refusal carries the reason instead of surfacing as an attribute error far
    from the decision.
    """

    def __init__(self, platform: str) -> None:
        self.name = f"unsupported:{platform}"
        self._platform = platform

    def probe(self) -> SandboxCapability:
        return SandboxCapability(
            self.name,
            False,
            f"no sandbox backend for platform {self._platform!r}; "
            f"spawning agent processes without a write boundary is not supported",
        )

    def wrap(self, argv, *, policy):
        raise RuntimeError(f"{self.name} cannot wrap a command")


def select_sandbox_backend() -> SandboxBackend:
    if sys.platform == "darwin":
        return SeatbeltBackend()
    return UnsupportedPlatformBackend(sys.platform)
