"""Hosted adapter — one class, parameterized by host.

Replaces the previous Claude / Codex / Gemini subclass trio. Each host
contributes only:
  - a name (claude / codex / gemini)
  - a host-specific hint string

Real behavior divergence (multi-reviewer fan-out, parallel sub-agents) is
driven by the workflow YAML's per-phase `multi_review: true` flag, not by
adapter subclass. This kills the copy-paste polymorphism that the architectural
review flagged.
"""
from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

from agent_flow.adapters.base import Adapter
from agent_flow.cli_detect import detect_available_clis


_CLAUDE_HINT = """\
- For multi-reviewer phases, use the `Task` tool to spawn parallel sub-agents
  — one per review angle. Send all Task calls in a single assistant message
  so they execute in parallel.
- Use `TodoWrite` for slice tracking during `implement` phase. Mark each
  TDD red→green→refactor step in_progress / completed.
- For long-running phases, prefer parallel reads (multiple `Read` calls in
  one message) over sequential.
- Cite file:line references using the `path/to/file:42` format.
"""

_CODEX_HINT = """\
- For multi-reviewer phases, agent-flow has already distributed angles
  across installed CLIs. Use Bash to invoke each non-host CLI in parallel
  (background `&` + wait, or per-angle subprocess). Aggregate stdout into
  the artifact.
- Per-angle artifacts are written by agent-flow as `final-review-<angle>.md`
  when subprocess delegation succeeds; aggregate them into the final
  `final-review.md` summary.
- Cite file:line references using the `path/to/file:42` format.
"""

_GEMINI_HINT = """\
- For multi-reviewer phases, agent-flow has already distributed angles
  across installed CLIs. Invoke each non-host CLI as a subprocess; capture
  stdout per angle and aggregate into the artifact.
- Per-angle artifacts are written by agent-flow as `final-review-<angle>.md`
  when subprocess delegation succeeds; the host aggregates these into the
  final `final-review.md`.
- Cite file:line references using the `path/to/file:42` format.
"""

# Read-only mapping. Wrapped to prevent third-party runtime mutation that
# would silently change adapter behavior across the process.
_HOST_HINTS: MappingProxyType[str, str] = MappingProxyType({
    "claude": _CLAUDE_HINT,
    "codex": _CODEX_HINT,
    "gemini": _GEMINI_HINT,
})


class HostedAdapter(Adapter):
    """Single adapter parameterized by host name.

    Construct with `HostedAdapter("claude")` etc. Behavior is identical
    across hosts except the hint block injected into the prompt envelope
    and the multi-reviewer distribution preview.
    """

    def __init__(self, host_name: str) -> None:
        super().__init__()
        if host_name not in _HOST_HINTS:
            raise ValueError(
                f"Unknown host '{host_name}'. Known: {sorted(_HOST_HINTS)}"
            )
        self.name = host_name
        self._hint = _HOST_HINTS[host_name]

    def execute(self, phase, run_dir: Path, project_root: Path) -> bool:
        host_hint = self._hint
        if phase.multi_review:
            host_hint += "\n" + _multi_reviewer_block()
        prompt = self.render_envelope(
            phase, run_dir, project_root, host_hint=host_hint,
        )
        print(prompt)
        return False  # host AI writes the artifact


def _multi_reviewer_block() -> str:
    """Distribution preview for multi-reviewer phases.

    The actual angle list is profile-driven and read by the host AI from
    the profile YAML; this block shows the suggested CLI distribution.
    """
    available = detect_available_clis()
    if not available:
        return ("### Multi-CLI distribution\n"
                "No external AI CLIs detected. Run all review angles in this "
                "session via parallel sub-agents (host-native mechanism).\n")
    names = [c.name for c in available]
    lines = [
        "### Multi-CLI distribution",
        f"Detected installed CLIs: {', '.join(names)}.",
        "",
        "When fanning out review angles, distribute round-robin across "
        "the installed CLIs (host last). For non-host CLIs, invoke via "
        "the `Bash` tool:",
        "",
    ]
    for cli in available:
        lines.append(f"- `{cli.binaries[0]} {' '.join(cli.invoke)} '<angle prompt>'`")
    lines.append("")
    lines.append(
        "Capture each subprocess's stdout and aggregate into "
        "`final-review.md`. For host-CLI angles, use the host-native "
        "parallel sub-agent mechanism."
    )
    return "\n".join(lines) + "\n"
