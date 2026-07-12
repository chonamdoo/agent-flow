from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import zipfile


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_SKILL_ASSETS = {
    "agent_flow/skills/agent-flow/SKILL.md",
    "agent_flow/skills/profile-routing.json",
    "agent_flow/skills/source-policy.yaml",
    "agent_flow/skills/upstream-lock.json",
}


def test_python_wheel_contains_required_skill_assets(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "uv is required to verify the Python wheel"

    result = subprocess.run(
        (uv, "build", "--wheel", "--out-dir", str(tmp_path)),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "UV_NO_PROGRESS": "1"},
    )
    assert result.returncode == 0, result.stderr or result.stdout

    wheels = sorted(tmp_path.glob("*.whl"))
    assert len(wheels) == 1, wheels
    with zipfile.ZipFile(wheels[0]) as wheel:
        entries = set(wheel.namelist())

    assert REQUIRED_SKILL_ASSETS <= entries
    assert any(
        entry.startswith("agent_flow/skills/") and entry.endswith("/SKILL.md")
        for entry in entries
    )
