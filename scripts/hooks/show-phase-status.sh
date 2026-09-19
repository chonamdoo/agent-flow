#!/bin/sh
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
"${AGENT_FLOW_HOOK_PYTHON:-python3}" -B -I - "$SCRIPT_DIR" 3<&0 <<'PY' 2>/dev/null || exit 0
import os
import signal
import subprocess
import sys

guidance = r"""
import json
import signal
import subprocess
import sys
from pathlib import Path

def interrupt_guidance(signum, frame):
    raise subprocess.TimeoutExpired("host guidance", 4)

# Git has its own session. Let its timeout handler reap it before the outer deadline.
if hasattr(signal, "SIGALRM"):
    signal.signal(signal.SIGALRM, interrupt_guidance)
    signal.alarm(4)

try:
    payload = json.load(sys.stdin)
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
except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
    raise SystemExit(1)
"""

try:
    process = subprocess.Popen(
        [sys.executable, "-B", "-I", "-c", guidance, sys.argv[1]],
        stdin=3,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.stdout.close()
        process.wait(timeout=1)
    else:
        if process.returncode == 0:
            sys.stdout.buffer.write(output)
except (OSError, subprocess.SubprocessError):
    pass
PY
exit 0
