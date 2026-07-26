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
from agent_flow.core.artifacts import run_gate_nonce


# stub-success가 만든 artifact임을 나타내는 표식. runner는 이 표식이 있는
# artifact에서만 마커 검사를 건너뛴다. 표식이 없으면 사람이 쓴 artifact이므로
# 환경변수 하나로 마커 검사 전체가 꺼지지 않는다.
STUB_SENTINEL = "agent-flow generic stub-success"


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
                        "verdict: approve\n",
                        encoding="utf-8",
                    )
                    return True
                if phase.id == "gates":
                    # 출처 표식은 runner 쪽에서 찍는다. 이 파일을 쓴 주체가
                    # agent가 아니라 adapter이므로 provenance는 성립한다.
                    payload = {
                        "passed": True,
                        "status": "green",
                        "results": [
                            {
                                "id": "stub",
                                "command": "agent-flow generic stub-success",
                                "argv": ["agent-flow", "generic", "stub-success"],
                                "passed": True,
                                "exit_code": 0,
                            }
                        ],
                    }
                    nonce = run_gate_nonce(run_dir)
                    if nonce:
                        payload["produced_by"] = {"tool": "agent-flow gates", "nonce": nonce}
                    artifact.write_text(
                        f"{json.dumps(payload, sort_keys=True)}\n", encoding="utf-8"
                    )
                    return True
                if phase.id == "pr-watch":
                    artifact.write_text(
                        f"# {phase.id}\n\n"
                        f"<!-- {STUB_SENTINEL} -->\n\n"
                        "status: green\n",
                        encoding="utf-8",
                    )
                    return True
                artifact.write_text(
                    f"# {phase.id}\n\n"
                    f"<!-- {STUB_SENTINEL} -->\n\n"
                    f"_stub artifact written by GenericAdapter (stub mode)._\n"
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
