"""Generic fallback adapter.

Two modes selectable via env var:

  AGENT_FLOW_GENERIC_MODE=stub  (default for tests)
    Stubs the artifact and returns True so the loop advances. Useful for
    smoke tests of the runner state machine.

  AGENT_FLOW_GENERIC_MODE=emit
    Prints the prompt to stdout and returns False, expecting a human (or
    untracked AI) to write the artifact and follow status `next_command`.
"""
from __future__ import annotations

import os
from pathlib import Path

from agent_flow.adapters.base import Adapter


class GenericAdapter(Adapter):
    name = "generic"

    def execute(self, phase, run_dir: Path, project_root: Path) -> bool:
        prompt = self.render_envelope(
            phase, run_dir, project_root,
            host_hint="No AI host detected. Paste the phase prompt into your "
                      "AI of choice; have it write the artifact at the path "
                      "above; then run `agent-flow status` and follow "
                      "`next_command`.",
        )
        print(prompt)
        if os.environ.get("AGENT_FLOW_GENERIC_MODE", "stub") == "stub":
            artifact = self.artifact_path(phase, run_dir)
            if not artifact.exists():
                if getattr(phase, "multi_review", False):
                    artifact.write_text(
                        f"# {phase.id}\n\n"
                        "## Reviewer 1\n"
                        "reviewer-source: sub-agent\n"
                        "verdict: approve\n\n"
                        "## Reviewer 2\n"
                        "reviewer-source: sub-agent\n"
                        "verdict: approve\n\n"
                        "## Overall\n"
                        "verdict: approve\n",
                        encoding="utf-8",
                    )
                    return True
                artifact.write_text(
                    f"# {phase.id}\n\n"
                    f"_stub artifact written by GenericAdapter (stub mode)._\n"
                )
            return True
        return False
