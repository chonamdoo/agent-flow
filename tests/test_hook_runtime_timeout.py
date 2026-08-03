from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[1]
PYTHON_HOOK_RUNTIME = KIT_ROOT / "scripts" / "hook-runtime" / "agent-flow-hook.py"
OMP_EXTENSION_SOURCE = KIT_ROOT / "lib" / "omp-hooks-extension.mjs"


def _load_python_hook_runtime():
    spec = importlib.util.spec_from_file_location(
        "agent_flow_hook_runtime_timeout_test",
        PYTHON_HOOK_RUNTIME,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        exit_code = ctypes.c_ulong()
        try:
            return (
                ctypes.windll.kernel32.GetExitCodeProcess(
                    process,
                    ctypes.byref(exit_code),
                )
                != 0
                and exit_code.value == 259
            )
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _assert_processes_reaped(*pid_files: Path) -> None:
    pids = [int(path.read_text(encoding="utf-8")) for path in pid_files]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and any(_pid_exists(pid) for pid in pids):
        time.sleep(0.02)
    assert not [pid for pid in pids if _pid_exists(pid)]


def _python_process_tree_fixture(
    diagnostic: str,
    *,
    root: Path | None = None,
) -> str:
    root_assignment = (
        f"root = Path({str(root)!r})\n"
        if root is not None
        else "root = Path(sys.argv[1])\n"
    )
    grandchild = (
        "import os, sys, time\n"
        "from pathlib import Path\n"
        + root_assignment
        + "(root / 'grandchild.pid').write_text(str(os.getpid()), encoding='utf-8')\n"
        + f"sys.stderr.write({diagnostic!r} + '-grandchild\\n')\n"
        + "sys.stderr.flush()\n"
        + "time.sleep(2.5)\n"
        + "(root / 'escaped-side-effect').write_text('escaped', encoding='utf-8')\n"
    )
    return (
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        + root_assignment
        + "(root / 'parent.pid').write_text(str(os.getpid()), encoding='utf-8')\n"
        + f"subprocess.Popen([sys.executable, '-c', {grandchild!r}, str(root)])\n"
        + f"sys.stderr.write({diagnostic!r} + '-parent\\n')\n"
        + "sys.stderr.flush()\n"
        + "time.sleep(10)\n"
    )


def test_python_policy_timeout_kills_and_reaps_the_process_tree(tmp_path: Path) -> None:
    runtime = _load_python_hook_runtime()
    fixture = _python_process_tree_fixture("python-partial")

    result, timed_out = runtime._run_managed_process(
        [sys.executable, "-c", fixture, str(tmp_path)],
        cwd=tmp_path,
        env=dict(os.environ),
        payload=b"",
        pass_fds=(),
        timeout=1,
    )

    assert timed_out is True
    assert result.returncode == 124
    assert b"python-partial-parent" in result.stderr
    assert b"python-partial-grandchild" in result.stderr
    _assert_processes_reaped(tmp_path / "parent.pid", tmp_path / "grandchild.pid")
    time.sleep(1.7)
    assert not (tmp_path / "escaped-side-effect").exists()


def _generated_omp_extension(agent_flow_home: Path) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    return subprocess.run(
        (
            node,
            "--input-type=module",
            "-e",
            "import { ompHooksExtensionSource } from "
            f"{json.dumps(str(OMP_EXTENSION_SOURCE))};"
            "process.stdout.write(ompHooksExtensionSource());",
        ),
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "AGENT_FLOW_HOME": str(agent_flow_home)},
        timeout=30,
    ).stdout


def test_omp_launcher_timeout_kills_and_reaps_the_process_tree(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    fixture = _python_process_tree_fixture("omp-partial", root=project)
    source = _generated_omp_extension(home)
    source = re.sub(
        r"^const HOOK_BOOTSTRAP = .*;$",
        lambda _: "const HOOK_BOOTSTRAP = " + json.dumps(fixture) + ";",
        source,
        count=1,
        flags=re.MULTILINE,
    )
    source = re.sub(
        r"^const HOOK_PYTHON = .*;$",
        lambda _: "const HOOK_PYTHON = " + json.dumps(sys.executable) + ";",
        source,
        count=1,
        flags=re.MULTILINE,
    )
    source = source.replace(
        "const HOOK_TIMEOUT_MS = 15000;",
        "const HOOK_TIMEOUT_MS = 1000;",
        1,
    )
    source += "\nexport { spawnHook as __spawnHook };\n"
    installed = tmp_path / "agent-flow-hooks.mjs"
    installed.write_text(source, encoding="utf-8")
    driver = (
        f"import {{ __spawnHook }} from {json.dumps(str(installed))};\n"
        f"const result = await __spawnHook('PreToolUse', '{{}}', {json.dumps(str(project))});\n"
        "process.stdout.write(JSON.stringify(result));\n"
    )

    completed = subprocess.run(
        (node, "--input-type=module", "-e", driver),
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "AGENT_FLOW_HOME": str(home)},
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == 124
    assert "omp-partial-parent" in result["stderr"]
    assert "omp-partial-grandchild" in result["stderr"]
    assert "agent-flow hook timed out" in result["stderr"]
    _assert_processes_reaped(project / "parent.pid", project / "grandchild.pid")
    time.sleep(1.7)
    assert not (project / "escaped-side-effect").exists()
