#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


def load_recorder(script_dir: Path):
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
        raise RuntimeError("trusted host worktree binding recorder is unavailable")
    from agent_flow.core.host_write_boundary import record_host_checkout_binding

    project_root = install_root.parent if install_root.name == ".agent-flow" else install_root
    return project_root, record_host_checkout_binding


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise RuntimeError("invalid host worktree binding payload")
        project_root, recorder = load_recorder(Path(__file__).resolve().parent)
        recorder(payload, project_root)
    except Exception as exc:
        print(f"host worktree binding failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
