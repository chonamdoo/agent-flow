from __future__ import annotations

import json
import shlex
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from agent_flow.artifact import read_meta
from agent_flow.core.command_evidence import COMMANDS_RUN_LOG, test_code_baseline
from agent_flow.core.host_write_boundary import (
    HostWriteBoundaryError,
    bound_worktree_for_session,
    host_write_boundary_violation,
)


def run_observed_command(
    argv: Sequence[str],
    *,
    project_root: Path,
    checkout_root: Path,
    cwd: Path,
    run_dir: Path | None = None,
    host_session_id: str | None = None,
) -> int:
    """명령을 직접 실행하고 실제 종료 결과를 기록한다. 미완료 실행은 RED가 아니다."""
    if not argv:
        raise ValueError("run-command requires an executable after --")
    command = shlex.join(argv)
    payload = {"tool_name": "shell", "cwd": str(cwd), "tool_input": {"command": command}}
    if host_session_id is not None:
        binding = bound_worktree_for_session(host_session_id, project_root)
        if (
            binding is None
            or binding.checkout.checkout != checkout_root.resolve()
            or run_dir is None
            or run_dir.resolve() != (
                binding.checkout.runtime_root / ".agent-flow" / "runs" / binding.checkout.run_id
            ).resolve()
        ):
            raise HostWriteBoundaryError(
                "host session is not bound to this checkout's current active run; "
                "use the run_command_prefix printed for this session after agent-flow continue"
            )
        payload["session_id"] = host_session_id
    violation = host_write_boundary_violation(payload, project_root)
    if violation is not None:
        raise HostWriteBoundaryError(violation)
    meta = read_meta(run_dir) if run_dir is not None else {}
    baseline = test_code_baseline(checkout_root)
    started_at = time.time()
    completed = subprocess.run(tuple(argv), cwd=cwd, check=False)
    code = completed.returncode
    entry = {
        "command": command,
        "cwd": str(cwd),
        "at": time.time(),
        "started_at": started_at,
        "exit_code": code if code >= 0 else None,
        "returncode": code,
        "signal": -code if code < 0 else None,
        "code_baseline": baseline if code > 0 else "",
        "source": "runner",
        "run_id": run_dir.name if run_dir is not None else "",
        "phase_id": meta.get("current_phase", ""),
    }
    log_path = project_root / COMMANDS_RUN_LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return code if code >= 0 else 128 - code
