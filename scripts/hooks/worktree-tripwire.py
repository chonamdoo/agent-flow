#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


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
        return None, None, None
    from agent_flow.core.host_write_boundary import bound_worktree_for_session
    from agent_flow.core.worktree_isolation import assert_leader_unchanged, WorktreeIsolationError

    project_root = install_root.parent if install_root.name == ".agent-flow" else install_root
    return (
        project_root,
        bound_worktree_for_session,
        (assert_leader_unchanged, WorktreeIsolationError),
    )


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

    project_root, get_binding, isolation = _load_boundary(Path(__file__).resolve().parent)
    if project_root is None:
        return 0

    assert_leader_unchanged, WorktreeIsolationError = isolation

    try:
        binding = get_binding(session_id, project_root)
    except Exception:
        return 0

    if binding is None:
        return 0

    # 다른 worktree는 보지 않는다. 저마다 제 세션이 붙어 일하는 중이라 그쪽 변경이
    # 이 명령에서 온 것인지 가릴 수 없다. 명시된 경로는 경계가 이미 막는다.
    try:
        assert_leader_unchanged(
            project_root,
            binding.leader_snapshot,
            worker_root=binding.checkout.checkout,
            include_ignored=False,
        )
    except WorktreeIsolationError as exc:
        print("worktree-tripwire: write outside bound worktree detected", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        print(
            f"Stop and resume from the bound worktree: {binding.checkout.checkout}",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
