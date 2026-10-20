"""Project gates must not borrow the installed kit's interpreter (#286 revision)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from agent_flow.core.gate_plan import (
    _normalize_profile_gate_command,
    profile_gate_commands,
)
from agent_flow.core.gates import run_gate
from agent_flow.core.command_evidence import is_test_command_execution


@pytest.mark.parametrize("separate_config_root", [False, True])
def test_project_only_tool_runs_in_project_venv_without_activation(tmp_path, monkeypatch, separate_config_root):
    project_python = tmp_path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    venv.EnvBuilder(with_pip=False).create(tmp_path / ".venv")
    purelib = subprocess.check_output(
        [str(project_python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        text=True,
    ).strip()
    (Path(purelib) / "ruff.py").write_text(
        "import json, sys\nprint(json.dumps({'prefix': sys.prefix, 'args': sys.argv[1:]}))\n"
    )
    # A foreign active environment must not select the kit's tool/dependencies.
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "unrelated-venv"))
    config_root = tmp_path
    if separate_config_root:
        config_root = tmp_path / "leader-config"
        config_root.mkdir()
        # Profile declarations may come from the leader, but its environment
        # must not override the checkout being tested.
        (config_root / "uv.lock").touch()
    command = next(g for g in profile_gate_commands(
        ["python"], root=config_root, execution_root=tmp_path
    ) if g.gate_id == "lint")
    result = run_gate(command, cwd=tmp_path)
    assert result.passed, result.stderr
    assert json.loads(result.stdout) == {"prefix": str(tmp_path / ".venv"), "args": ["check", "."]}


@pytest.mark.parametrize("tool,args", [("mypy", (".",)), ("ruff", ("check", ".")), ("pytest", ("-q",))])
def test_uv_lock_selects_locked_project_tools_even_with_existing_venv(tmp_path, tool, args):
    (tmp_path / "uv.lock").write_text("version = 1\n")
    (tmp_path / ".venv/bin").mkdir(parents=True)
    (tmp_path / ".venv/bin/python").touch()
    command = _normalize_profile_gate_command("python", "probe", (tool, *args), profile_root=tmp_path)
    assert command == ("uv", "run", "--locked", "python", "-m", tool, *args)


@pytest.mark.parametrize("command", [
    ("uv", "run", "--frozen", "ruff", "check", "src"),
    ("poetry", "run", "pytest", "-q"),
    ("/custom/python", "-m", "mypy", "src"),
    ("./scripts/check-python",),
])
def test_explicit_python_gate_command_is_preserved(tmp_path, command):
    (tmp_path / "uv.lock").touch()
    assert _normalize_profile_gate_command("python", "probe", command, profile_root=tmp_path) == command


def test_python_without_project_environment_keeps_declared_path_tools(tmp_path):
    assert _normalize_profile_gate_command("python", "lint", ("ruff", "check", "."), profile_root=tmp_path) == ("ruff", "check", ".")


def test_architecture_lint_keeps_kit_python_in_uv_project(tmp_path):
    (tmp_path / "uv.lock").touch()
    command = next(g for g in profile_gate_commands(["python"], root=tmp_path) if g.gate_id == "architecture-lint")
    assert command.command[:4] == (sys.executable, "-P", "-m", "agent_flow.core.architecture_lint")


@pytest.mark.parametrize("command", [
    "uv run --locked python -c pytest tests/test_x.py::test_bug",
    "uv run --with pytest echo tests/test_x.py::test_bug",
    "uv run --locked echo pytest tests/test_x.py::test_bug",
    "uv run --locked python -m pytest --collect-only tests/test_x.py::test_bug",
])
def test_uv_wrapper_does_not_turn_arguments_or_collection_into_test_evidence(command):
    assert not is_test_command_execution(command, ("pytest",), "tests/test_x.py::test_bug")


def test_identical_project_commands_cannot_silently_drop_a_distinct_ci_check(tmp_path):
    profiles = tmp_path / ".agent-flow/profiles"
    profiles.mkdir(parents=True)
    for profile_id in ("python", "nextjs"):
        (profiles / f"{profile_id}.yaml").write_text(json.dumps({
            "id": profile_id,
            "gates": [{
                "id": "test", "command": ["pytest", "-q"],
                "execution": "ci", "ci_check": f"{profile_id}-ci",
            }],
        }))
    with pytest.raises(ValueError, match="conflicting gate ci_check"):
        profile_gate_commands(["python", "nextjs"], root=tmp_path, execution="ci")


def test_runner_uses_checkout_environment_with_leader_profile_declarations(tmp_path):
    from agent_flow.artifact import create_run
    from tests.test_gate_provenance import _gates_runner

    leader = tmp_path / "leader"
    checkout = tmp_path / "checkout"
    profiles = leader / ".agent-flow/profiles"
    profiles.mkdir(parents=True)
    checkout.mkdir()
    (leader / ".agent-flow/kit.json").write_text(json.dumps({"profile": "python", "profiles": ["python"]}))
    (leader / "uv.lock").touch()  # The leader's environment must not select uv here.
    (profiles / "python.local.yaml").write_text(
        "gates:\n  - id: lint\n    command: [ruff, check, .]\n    required: true\n"
    )
    venv.EnvBuilder(with_pip=False).create(checkout / ".venv")
    python = checkout / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    purelib = subprocess.check_output(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True
    ).strip()
    (Path(purelib) / "ruff.py").write_text("import sys\nprint(sys.prefix)\n")
    run_dir = create_run(leader, "default", "task", checkout_root=checkout)
    _gates_runner(run_dir, checkout, leader)._run_project_gates()
    ledger = json.loads((run_dir / "artifacts/gate-results.json").read_text())
    assert len(ledger["results"]) == 1
    result = ledger["results"][0]
    assert result["passed"] is True, result
    # The persisted gate ledger deliberately makes checkout paths relative.
    assert result["stdout"].strip() == ".venv"
    assert result["command"][0] == str(python.relative_to(checkout))
