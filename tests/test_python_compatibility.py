from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet


REPO = Path(__file__).resolve().parents[1]


def test_python_metadata_matches_runtime_minimum() -> None:
    metadata = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    supported = SpecifierSet(metadata["project"]["requires-python"])

    assert "3.10" not in supported
    assert "3.11" in supported


def _select_python(
    tmp_path: Path,
    *,
    consumer: str,
    preference: str,
    supported: bool = True,
    preferred_supported: bool = False,
) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for interpreter selection")
    unsupported_python = tmp_path / "unsupported" / "bin" / "python"
    supported_python = tmp_path / "supported" / "bin" / "python"
    for candidate in (unsupported_python, supported_python):
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.touch()
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHON", "PYTHON_EXECUTABLE", "VIRTUAL_ENV"}}
    preferred_python = supported_python if preferred_supported else unsupported_python
    if preference == "VIRTUAL_ENV":
        env[preference] = str(preferred_python.parent.parent)
    elif preference != "PATH":
        env[preference] = str(preferred_python)
    if consumer == "safety":
        env["AGENT_FLOW_INSTALL_LEASE_FD"] = "3"
    module = REPO / ("bin/agent-flow-kit.mjs" if consumer == "relay" else "lib/installer-shared.mjs")
    script = f"""
import childProcess from "node:child_process";
import {{ syncBuiltinESMExports }} from "node:module";
const realSpawn = childProcess.spawnSync;
const realPython = {json.dumps(sys.executable)};
const unsupportedPython = {json.dumps(str(unsupported_python))};
const supportedPython = {json.dumps(str(supported_python))};
childProcess.spawnSync = (candidate, args, options) => {{
  if (!String(candidate).includes("python")) return realSpawn(candidate, args, options);
  const accepted = {json.dumps(supported)} && (candidate === "python3.11" || candidate === supportedPython);
  const minor = accepted ? 11 : 10;
  const identity = accepted ? supportedPython : unsupportedPython;
  if (args[0] === "--version") {{
    return {{status: 0, stdout: `Python 3.${{minor}}.0`, stderr: ""}};
  }}
  const codeIndex = args.indexOf("-c");
  if (codeIndex !== -1) {{
    if (args[codeIndex + 1].includes("from agent_flow.core.installation import main")) {{
      process.stdout.write(identity);
      return {{status: 0, stdout: identity, stderr: ""}};
    }}
    const prefix = `import sys; sys.version_info = (3, ${{minor}}, 0, 'final', 0); sys.executable = ${{JSON.stringify(identity)}};\n`;
    const probeArgs = [...args];
    probeArgs[codeIndex + 1] = prefix + args[codeIndex + 1];
    return realSpawn(realPython, probeArgs, options);
  }}
  if (options?.stdio === "inherit") process.stdout.write(identity);
  return {{status: 0, stdout: identity, stderr: ""}};
}};
syncBuiltinESMExports();
process.argv = [process.execPath, {json.dumps(str(module))}, "compatibility-probe"];
const api = await import({json.dumps(module.as_uri())});
if ({json.dumps(consumer)} === "installer") {{
  process.stdout.write(api.resolveManagedPython().python);
}} else if ({json.dumps(consumer)} === "safety") {{
  api.ensureInstallLease({json.dumps(str(tmp_path))});
}}
"""
    return subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("preference", ["PYTHON", "PYTHON_EXECUTABLE", "VIRTUAL_ENV", "PATH"])
def test_node_relay_skips_unsupported_python(tmp_path: Path, preference: str) -> None:
    result = _select_python(tmp_path, consumer="relay", preference=preference)

    assert result.returncode == 0, result.stderr
    assert result.stdout == str(tmp_path / "supported" / "bin" / "python")


@pytest.mark.parametrize("consumer", ["installer", "safety"])
@pytest.mark.parametrize("preference", ["PYTHON", "PYTHON_EXECUTABLE", "VIRTUAL_ENV", "PATH"])
def test_installer_skips_unsupported_python(
    tmp_path: Path, consumer: str, preference: str,
) -> None:
    result = _select_python(tmp_path, consumer=consumer, preference=preference)

    assert result.returncode == 0, result.stderr
    assert result.stdout == str(tmp_path / "supported" / "bin" / "python")


@pytest.mark.parametrize("consumer", ["relay", "installer", "safety"])
def test_python_selection_accepts_the_minimum_python(tmp_path: Path, consumer: str) -> None:
    result = _select_python(
        tmp_path, consumer=consumer, preference="PYTHON", preferred_supported=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == str(tmp_path / "supported" / "bin" / "python")


@pytest.mark.parametrize("consumer", ["relay", "installer", "safety"])
def test_python_selection_reports_the_minimum_when_no_candidate_qualifies(
    tmp_path: Path, consumer: str,
) -> None:
    result = _select_python(tmp_path, consumer=consumer, preference="PYTHON", supported=False)

    assert result.returncode != 0
    assert "Python >=3.11" in result.stderr
    assert "PYTHON" in result.stderr
    assert "Tried:" in result.stderr


def test_minimum_python_cli_entry_points(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO / "src"),
        "HOME": str(tmp_path / "home"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "AGENT_FLOW_NO_UPDATE_CHECK": "1",
    }

    def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, "-m", "agent_flow.cli", *args],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    help_result = run_cli("--help")
    assert "status" in help_result.stdout
    status_result = run_cli("status", "--root", str(project))
    assert "진행 중인 run 없음." in status_result.stdout
    workflow_result = run_cli("workflow", "export", "--workflow", "default")
    assert json.loads(workflow_result.stdout)["id"] == "default"
