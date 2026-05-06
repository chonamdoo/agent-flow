"""Pick the host adapter that drives the current run.

Detection order:
  1. AGENT_FLOW_ADAPTER env var (forces a specific adapter)
  2. detect_host_cli() — env-var hints exported by each CLI
  3. fallback to generic adapter (prompts go to stdout)

The Claude / Codex / Gemini hosts share a single `HostedAdapter` class
parameterized by name; only the hint string differs.

Note: this picks the *host* adapter only. Multi-reviewer phases distribute
across all installed CLIs via multi_review.distribute(); see cli_detect.py
and multi_review.py.
"""
from __future__ import annotations

import os

from agent_flow.adapters.base import Adapter
from agent_flow.adapters.generic import GenericAdapter
from agent_flow.adapters.hosted import HostedAdapter
from agent_flow.cli_detect import detect_host_cli


_HOSTED = {"claude", "codex", "gemini"}


def detect_adapter() -> Adapter:
    forced = os.environ.get("AGENT_FLOW_ADAPTER")
    if forced:
        return _by_name(forced)

    host = detect_host_cli()
    if host in _HOSTED:
        return HostedAdapter(host)

    return GenericAdapter()


def _by_name(name: str) -> Adapter:
    n = name.lower()
    if n == "generic":
        return GenericAdapter()
    if n in _HOSTED:
        return HostedAdapter(n)
    raise ValueError(
        f"Unknown adapter '{name}'. Choose from: generic, "
        f"{', '.join(sorted(_HOSTED))}"
    )
