from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def evaluator(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "evals"))
    spec = importlib.util.spec_from_file_location("phase_eval_execution_scope", ROOT / "evals/phase_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_author_contract_preserves_phase_instructions_and_scopes_lifecycle(evaluator, tmp_path):
    prompt = "Read /project/SKILL.md. Write artifacts/green.log. Then run agent-flow status."
    tooling = {"launcher": str(tmp_path / ".agent-flow/bin/agent-flow"),
               "tools": {"node": "/verified tools/node"},
               "test_command": ["/verified tools/node", "--test", "tests/*.test.ts"]}
    result = evaluator._author_execution_contract(prompt, tmp_path, tooling)
    assert result.startswith(prompt)
    assert "phase-only evaluation" in result
    assert "controller owns workflow lifecycle" in result
    assert "status, continue, advance, next, run, or start" in result
    assert "--no-hooks" in result
    assert "do not install or enable hooks" in result
    assert tooling["launcher"] in result
    assert "'/verified tools/node' --test 'tests/*.test.ts'" in result
    assert "Do not search outside this project" in result


def test_codex_transports_verified_path_without_login_shell_or_weaker_sandbox(evaluator, monkeypatch, tmp_path):
    launcher = tmp_path / ".agent-flow/bin/agent-flow"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    monkeypatch.setenv("PATH", "/verified tools/bin:/usr/bin")
    captured = []

    def invoke(args, prompt, cwd, timeout):
        captured.append(args)
        stdout = json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "done"}})
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(evaluator, "_run_cli", invoke)
    evaluator._codex("Implement.", tmp_path, author=True, timeout=10)
    args = captured[0]
    overrides = [args[index + 1] for index, value in enumerate(args) if value == "-c"]
    assert "allow_login_shell=false" in overrides
    path = next(value for value in overrides if value.startswith("shell_environment_policy.set.PATH="))
    assert json.loads(path.split("=", 1)[1]).split(":")[0] == str(launcher.parent)
    assert "/verified tools/bin" in path
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    assert "skills.include_instructions=false" not in overrides
    assert "project_doc_max_bytes=0" not in overrides


def test_fixture_tooling_uses_current_launcher_and_absolute_stack_executable(evaluator, monkeypatch, tmp_path):
    project = tmp_path / "project"
    launcher = project / ".agent-flow/bin/agent-flow"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nprintf 'project launcher\\n'\n")
    launcher.chmod(0o755)
    tools = tmp_path / "verified tools"
    tools.mkdir()
    node = tools / "node"
    node.write_text("#!/bin/sh\nprintf 'v24.0.0\\n'\n")
    node.chmod(0o755)
    global_launcher = tools / "agent-flow"
    global_launcher.write_text("#!/bin/sh\nexit 99\n")
    global_launcher.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:/usr/bin:/bin")
    result = evaluator._fixture_tooling(project, {"stack": "web", "test_command": ["node", "--version"]}, "author")
    assert result["launcher"] == str(launcher)
    assert result["test_command"] == [str(node), "--version"]
    child = subprocess.run(result["test_command"], cwd=project, env={"PATH": "/missing"},
                           capture_output=True, text=True)
    assert child.returncode == 0
    assert child.stdout.strip() == "v24.0.0"
    launcher_call = subprocess.run([result["launcher"], "--version"], cwd=project,
                                  capture_output=True, text=True)
    assert launcher_call.stdout.strip() == "project launcher"


def test_fixture_tooling_rejects_absent_launcher_instead_of_global_fallback(evaluator, tmp_path):
    with pytest.raises(RuntimeError, match="project launcher unavailable"):
        evaluator._fixture_tooling(tmp_path, {"stack": "web", "test_command": ["node", "--version"]}, "author")


def test_claude_author_allows_resolved_stack_paths_without_lifecycle_permission(evaluator, monkeypatch, tmp_path):
    original = evaluator.shutil.which
    node = "/verified tools/node"
    monkeypatch.setattr(evaluator.shutil, "which", lambda name: node if name == "node" else original(name))
    captured = []

    def invoke(args, prompt, cwd, timeout):
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps({"type": "result", "result": "done"}), "")

    monkeypatch.setattr(evaluator, "_run_cli", invoke)
    evaluator._claude("Implement.", tmp_path, author=True, timeout=10)
    args = captured[0]
    allowed = args[args.index("--allowedTools") + 1:args.index("--disallowedTools")]
    denied = args[args.index("--disallowedTools") + 1:]
    assert f"Bash({node}:*)" in allowed
    assert "Bash(agent-flow:*)" in denied
    assert not any(".agent-flow/bin/agent-flow" in rule for rule in allowed)


def test_project_tool_path_finds_fixture_compiler_before_global_tool(evaluator, monkeypatch, tmp_path):
    local = tmp_path / "project/node_modules/.bin"
    local.mkdir(parents=True)
    compiler = local / "tsc"
    compiler.write_text("#!/bin/sh\nprintf 'fixture compiler\\n'\n")
    compiler.chmod(0o755)
    global_bin = tmp_path / "global"
    global_bin.mkdir()
    other = global_bin / "tsc"
    other.write_text("#!/bin/sh\nprintf 'wrong compiler\\n'\n")
    other.chmod(0o755)
    monkeypatch.setenv("PATH", str(global_bin))
    path = evaluator._project_tool_path(tmp_path / "project")
    child = subprocess.run(["/bin/sh", "-c", "command -v tsc; tsc --version"],
                           env={"PATH": path}, capture_output=True, text=True)
    assert child.returncode == 0
    assert child.stdout.splitlines() == [str(compiler), "fixture compiler"]
