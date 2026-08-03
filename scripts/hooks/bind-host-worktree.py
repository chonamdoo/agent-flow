#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

exec(os.environ["AGENT_FLOW_VERIFIED_IMPORT_BOOTSTRAP"], globals())


def load_recorder():
    from agent_flow.core.host_write_boundary import record_host_checkout_binding

    project_root = Path(os.environ["AGENT_FLOW_PROJECT_ROOT"]).resolve()
    return project_root, record_host_checkout_binding


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise RuntimeError("invalid host worktree binding payload")
        project_root, recorder = load_recorder()
        recorder(payload, project_root)
    except Exception as exc:
        print(f"host worktree binding failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
