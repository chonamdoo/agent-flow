#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


# 셸 계열 tool 이름
_COMMAND_TOOLS = frozenset({
    "bash", "shell", "run_terminal_cmd", "execute_command", "local_shell", "terminal",
})


def _load_boundary(script_dir: Path):
    install_root = script_dir.resolve().parents[1]
    for source in (
        install_root / "runtime" / "python",
        install_root / "src",
    ):
        module = source / "agent_flow" / "core" / "host_write_boundary.py"
        if module.is_file():
            sys.path.insert(0, str(source))
            break
    else:
        return None, None, None, None
    from agent_flow.core.host_write_boundary import (
        bound_worktree_for_session,
        active_worktree_checkouts,
    )
    from agent_flow.core.worktree_isolation import assert_leader_unchanged, WorktreeIsolationError

    project_root = install_root.parent if install_root.name == ".agent-flow" else install_root
    return project_root, bound_worktree_for_session, active_worktree_checkouts, (assert_leader_unchanged, WorktreeIsolationError)


def _git_dirty_files(path: Path) -> list[str]:
    """git status --porcelain 으로 변경된 경로 목록을 반환한다."""
    try:
        result = subprocess.run(
            ("git", "-C", str(path), "status", "--porcelain"),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        return [line[3:].strip() for line in result.stdout.splitlines() if line.strip()]
    except Exception:
        return []


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0

    tool = payload.get("tool_name", "") or payload.get("toolName", "")
    if not isinstance(tool, str) or tool.lower() not in _COMMAND_TOOLS:
        return 0

    session_id = payload.get("session_id") or payload.get("sessionId") or ""
    if not isinstance(session_id, str) or not session_id.strip():
        return 0
    session_id = session_id.strip()

    project_root, get_binding, get_active, isolation = _load_boundary(
        Path(__file__).resolve().parent
    )
    if project_root is None:
        return 0

    assert_leader_unchanged, WorktreeIsolationError = isolation

    try:
        binding = get_binding(session_id, project_root)
    except Exception:
        return 0

    if binding is None:
        return 0

    violations: list[str] = []

    # leader 변경 탐지 — binding 시점 스냅샷과 현재를 비교한다
    try:
        assert_leader_unchanged(
            project_root,
            binding.leader_snapshot,
            worker_root=binding.checkout.checkout,
            include_ignored=False,
        )
    except WorktreeIsolationError as exc:
        violations.append(str(exc))

    # sibling worktree 변경 탐지 — git status 가 깨끗해야 한다
    # 한계: 명령 실행 전 이미 dirty 상태였으면 구분하지 못한다
    try:
        active = get_active(project_root)
    except Exception:
        active = ()

    for other in active:
        if other.checkout == binding.checkout.checkout:
            continue
        dirty = _git_dirty_files(other.checkout)
        if dirty:
            violations.append(
                f"sibling worktree {other.checkout} has unexpected changes after command: "
                + ", ".join(dirty[:5])
            )

    if violations:
        print("worktree-tripwire: write outside bound worktree detected", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print(
            f"Stop and resume from the bound worktree: {binding.checkout.checkout}",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
