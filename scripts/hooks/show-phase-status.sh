#!/bin/sh
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
"${AGENT_FLOW_HOOK_PYTHON:-python3}" -B -I - "$SCRIPT_DIR" 3<&0 <<'PY' 2>/dev/null || exit 0
import json
import os
import sys
from pathlib import Path

try:
    with os.fdopen(3, encoding="utf-8") as stream:
        payload = json.load(stream)
    install_root = Path(sys.argv[1]).resolve().parents[1]
    for source in (install_root / "runtime" / "python", install_root / "src"):
        if (source / "agent_flow" / "core" / "host_write_boundary.py").is_file():
            sys.path.insert(0, str(source))
            break
    else:
        raise SystemExit(0)
    from agent_flow.core.host_write_boundary import host_session_guidance

    project_root = install_root.parent if install_root.name == ".agent-flow" else install_root
    message = host_session_guidance(payload, project_root)
    if message:
        print(json.dumps({"systemMessage": message}, ensure_ascii=False))
except (OSError, UnicodeError, ValueError):
    pass
PY
exit 0
