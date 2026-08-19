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
from agent_flow.core.design_ledger import capture_design_ledger
from agent_flow.core.worktree_isolation import write_run_subpath_text


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
                if getattr(phase, "multi_review", False):
                    write_run_subpath_text(
                        run_dir,
                        artifact,
                        f"# {phase.id}\n\n"
                        f"<!-- {STUB_SENTINEL} -->\n\n"
                        "## Reviewer 1\n"
                        "reviewer-source: sub-agent\n"
                        "verdict: approve\n\n"
                        "## Reviewer 2\n"
                        "reviewer-source: sub-agent\n"
                        "verdict: approve\n\n"
                        "## Overall\n"
                        "verdict: approve\n",
                    )
                    return True
                if phase.id == "pr-watch":
                    write_run_subpath_text(
                        run_dir,
                        artifact,
                        f"# {phase.id}\n\n"
                        f"<!-- {STUB_SENTINEL} -->\n\n"
                        "status: green\n",
                    )
                    return True
                if phase.id in {"design", "prd"}:
                    task = _stub_task(run_dir)
                    if task:
                        spec_items = (
                            f"SPEC-1: {task}\n"
                            "verify: manual\n\n"
                        )
                        spec_marker = "SPEC-1"
                    else:
                        spec_items = ""
                        spec_marker = "none"
                    content = (
                        f"# {phase.id}\n\n"
                        f"<!-- {STUB_SENTINEL} -->\n\n"
                        "## Spec Items\n\n"
                        f"{spec_items}"
                        "## Design Values\n\n"
                        "## Completion Gate\n"
                        f"spec-items: {spec_marker}\n"
                        "design-values: none\n"
                    )
                    write_run_subpath_text(run_dir, artifact, content)
                    # stub-success는 host 없는 state-machine smoke 전용이다.
                    # runner보다 먼저 source ledger를 capture한다.
                    capture_design_ledger(run_dir, phase.id, content)
                    return True
                write_run_subpath_text(
                    run_dir,
                    artifact,
                    f"# {phase.id}\n\n"
                    f"<!-- {STUB_SENTINEL} -->\n\n"
                    f"_stub artifact written by GenericAdapter (stub mode)._\n",
                )
            return True
        if getattr(phase, "multi_review", False):
            self._write_blocked_stub(
                phase,
                run_dir,
                reason="No AI host detected; no Claude/Codex reviewer subprocess is available.",
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
        write_run_subpath_text(
            run_dir,
            artifact,
            f"# {phase.id}\n\n"
            "status: blocked\n"
            f"reason: {reason}\n\n"
            "_stub artifact written by GenericAdapter (stub mode)._\n",
        )


def _stub_task(run_dir: Path) -> str:
    for name in ("meta.json", "manifest.json"):
        try:
            payload = json.loads((run_dir / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("task"), str):
            return " ".join(payload["task"].split())
    return ""
