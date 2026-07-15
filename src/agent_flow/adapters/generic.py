"""Generic fallback adapter.

환경 변수로 세 동작을 고른다.

  AGENT_FLOW_GENERIC_MODE=emit  (기본값)
    프롬프트만 출력하고 False를 반환한다. 사람 또는 외부 AI가 artifact를
    작성한 뒤 status의 `next_command`를 따라야 한다.

  AGENT_FLOW_GENERIC_MODE=stub
    blocked stub artifact를 쓰고 True를 반환한다. runner는 workflow를
    진행하지 않고 degraded/blocked phase로 보고한다.

  AGENT_FLOW_GENERIC_MODE=stub-success
    기존 smoke test 전용 모드다. AI host 없이 state machine을 검증할 때만
    artifact를 성공 처리한다.
"""
from __future__ import annotations

import json
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
        mode = os.environ.get("AGENT_FLOW_GENERIC_MODE", "emit")
        if mode == "stub-success":
            artifact = self.artifact_path(phase, run_dir)
            if not artifact.exists():
                artifact.parent.mkdir(parents=True, exist_ok=True)
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
                        "verdict: approve\n"
                        f"{_phase_contract_line(phase)}",
                        encoding="utf-8",
                    )
                    return True
                if phase.id == "gates":
                    artifact.write_text(
                        '{"passed": true, "status": "green", '
                        '"results": [{"id": "stub", '
                        '"command": "agent-flow generic stub-success", '
                        '"argv": ["agent-flow", "generic", "stub-success"], '
                        '"passed": true, "exit_code": 0}]}\n',
                        encoding="utf-8",
                    )
                    return True
                if phase.id == "pr-watch":
                    artifact.write_text(
                        f"# {phase.id}\n\n"
                        "status: green\n",
                        encoding="utf-8",
                    )
                    return True
                artifact.write_text(
                    f"# {phase.id}\n\n"
                    f"_stub artifact written by GenericAdapter (stub mode)._\n"
                    f"{_phase_contract_line(phase)}"
                )
            return True
        if getattr(phase, "multi_review", False):
            self._write_blocked_stub(
                phase,
                run_dir,
                reason="No AI host detected; active-host reviewer sub-agents are unavailable.",
            )
            return True
        if mode == "stub":
            self._write_blocked_stub(
                phase,
                run_dir,
                reason="GenericAdapter stub mode cannot complete workflow phases.",
            )
            return True
        return False

    def _write_blocked_stub(self, phase, run_dir: Path, *, reason: str) -> None:
        artifact = self.artifact_path(phase, run_dir)
        if artifact.exists():
            return
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            f"# {phase.id}\n\n"
            "status: blocked\n"
            f"reason: {reason}\n\n"
            "_stub artifact written by GenericAdapter (stub mode)._\n",
            encoding="utf-8",
        )


def _phase_contract_line(phase) -> str:
    required_skills = list(getattr(phase, "required_skills", ()))
    requirements = list(getattr(phase, "requirements", ()))
    if not required_skills and not requirements:
        return ""
    payload = json.dumps(
        {
            "applied_skills": required_skills,
            "requirements": {requirement: "pass" for requirement in requirements},
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"\nphase-contract: {payload}\n"
