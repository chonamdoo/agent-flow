"""cross-host 보호 행렬: Claude·Codex·OMP의 실제 payload 모양이 같은 보호에 닿는가.

fixture는 `tools/host-smoke/run.py --capture`가 실제 host에서 잡은 hook stdin(Claude·Codex)과
extension 이벤트(OMP)다. 자리표시자만 이 테스트의 저장소 경로와 명령으로 채운다. Claude·
Codex는 설치된 launcher로 hook 스크립트를 실행하고, OMP는 생성된 extension을 node로
구동한다. host가 tool 이름이나 결과 모양을 바꾸면 여기서 보호가 빠진 것이 드러난다.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_flow.artifact import create_run
from agent_flow.core.hook_integrity import MANAGED_HOOK_PLACEMENT
from agent_flow.core.host_write_boundary import bound_worktree_for_session
from agent_flow.core.worktrees import create_worktree, plan_worktree, worktree_runtime_root

KIT = Path(__file__).resolve().parents[1]
FIXTURES = KIT / "tests" / "fixtures" / "host-payloads"
HOSTS = ("claude", "codex", "omp")
SESSION = "matrix-session"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True, text=True)


def _project(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "init", cwd=root)
    # installer처럼 설치본을 git status 밖에 둔다. 관측 hook의 기록이 leader 변경이 아니다.
    with (root / ".git" / "info" / "exclude").open("a", encoding="utf-8") as exclude:
        exclude.write("/.agent-flow/\n")
    status = create_worktree(root=root, plan=plan_worktree(root=root, name="first"))
    run_dir = create_run(
        worktree_runtime_root(root=root, name=status.name),
        "default",
        "host protection matrix",
        checkout_identity=f"worktree:{status.name}",
        checkout_registration_identity=status.registration_identity,
    )
    installed = root / ".agent-flow"
    shutil.copytree(KIT / "scripts" / "hooks", installed / "scripts" / "hooks")
    shutil.copytree(KIT / "src" / "agent_flow", installed / "runtime" / "python" / "agent_flow")
    (installed / "kit.json").write_text("{}", encoding="utf-8")
    launcher = installed / "bin" / "agent-flow-hook"
    launcher.parent.mkdir()
    launcher.write_text(
        '#!/bin/sh\ncase "$1" in\n'
        '  *.py) exec "$AGENT_FLOW_HOOK_PYTHON" -I "$@" ;;\n'
        '  *) exec /bin/sh "$@" ;;\nesac\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return root, status.path, run_dir


def _status_output(root: Path, checkout: Path, run_dir: Path) -> str:
    next_command = f"agent-flow continue --root {root} --worktree {checkout.name}"
    payload = {"status": "awaiting_host", "run": f"default/{run_dir.name}", "next_command": next_command}
    return (
        f"status: awaiting_host\nrun: default/{run_dir.name}\n"
        f"next_command: {next_command}\nstatus_json: {json.dumps(payload)}\n"
    )


def _fill(node: Any, values: dict[str, str]) -> Any:
    if isinstance(node, dict):
        return {key: _fill(child, values) for key, child in node.items()}
    if isinstance(node, list):
        return [_fill(child, values) for child in node]
    if isinstance(node, str):
        for name, value in values.items():
            node = node.replace("${" + name + "}", value)
    return node


def _payload(host: str, event: str, root: Path, checkout: Path, command: str, output: str) -> dict:
    record = json.loads((FIXTURES / host / f"{event}.json").read_text(encoding="utf-8"))
    return _fill(
        record["payload"],
        {
            "COMMAND": command,
            "STATUS_OUTPUT": output,
            "CHECKOUT": str(checkout),
            "PROJECT": str(root),
            "SESSION": SESSION,
            "ID": "matrix-id",
            "TRANSCRIPT": str(root.parent / "transcript.jsonl"),
            "HOME": str(root.parent / "home"),
            "SCRATCH": str(root.parent),
        },
    )


def _hook_env() -> dict[str, str]:
    return {**os.environ, "AGENT_FLOW_HOOK_PYTHON": sys.executable}


def _run_hook(root: Path, checkout: Path, script: str, payload: dict) -> subprocess.CompletedProcess:
    """host가 등록 명령(`<launcher> <script>`)을 실행하는 것과 같은 모양으로 돌린다."""
    event, matcher = MANAGED_HOOK_PLACEMENT[script]
    assert payload["hook_event_name"] == event
    assert re.fullmatch(matcher, payload["tool_name"]), (
        f"{payload['tool_name']!r} does not match the registered {script} matcher {matcher!r}"
    )
    return subprocess.run(
        (str(root / ".agent-flow" / "bin" / "agent-flow-hook"),
         str(root / ".agent-flow" / "scripts" / "hooks" / script)),
        input=json.dumps(payload),
        cwd=checkout,
        env=_hook_env(),
        capture_output=True,
        text=True,
    )


def _omp(checkout: Path, handler: str, record: dict) -> Any:
    extension = checkout / ".omp" / "extensions" / "agent-flow-hooks.mjs"
    if not extension.exists():
        extension.parent.mkdir(parents=True, exist_ok=True)
        extension.write_text(
            subprocess.run(
                ("node", "--input-type=module", "-e",
                 "import { ompHooksExtensionSource } from "
                 + json.dumps(str(KIT / "lib" / "omp-hooks-extension.mjs"))
                 + "; process.stdout.write(ompHooksExtensionSource());"),
                capture_output=True, text=True, check=True,
            ).stdout,
            encoding="utf-8",
        )
    driver = (
        f"import extension from {json.dumps(str(extension))};\n"
        "const handlers = {};\n"
        "extension({on(name, handler) { handlers[name] = handler; }});\n"
        f"const record = {json.dumps(record)};\n"
        "const ctx = {...record.ctx, hasUI: false,\n"
        "  sessionManager: {getSessionId() { return record.ctx.sessionId; }}};\n"
        f"handlers[{json.dumps(handler)}](record.event, ctx).then(\n"
        "  (result) => process.stdout.write(JSON.stringify(result ?? null)),\n"
        "  (error) => { console.error(error); process.exitCode = 1; });\n"
    )
    completed = subprocess.run(
        ("node", "--input-type=module", "-e", driver),
        cwd=checkout, env=_hook_env(), capture_output=True, text=True, check=True,
    )
    return json.loads(completed.stdout)


def _post(host: str, root: Path, checkout: Path, command: str, output: str) -> Any:
    """명령이 끝난 뒤 host가 부르는 관측·binding·tripwire 경로."""
    if host == "omp":
        return _omp(checkout, "tool_result",
                    _payload(host, "tool_result", root, checkout, command, output))
    payload = _payload(host, "post_tool_use", root, checkout, command, output)
    results = [
        _run_hook(root, checkout, script, payload)
        for script in ("record-command-run.py", "bind-host-worktree.py", "worktree-tripwire.py")
    ]
    return results


def _pre_blocks(host: str, root: Path, checkout: Path, command: str) -> tuple[bool, str]:
    if host == "omp":
        verdict = _omp(checkout, "tool_call",
                       _payload(host, "tool_call", root, checkout, command, ""))
        return bool(verdict and verdict.get("block")), (verdict or {}).get("reason", "")
    result = _run_hook(root, checkout, "guard-host-worktree.sh",
                       _payload(host, "pre_tool_use", root, checkout, command, ""))
    return result.returncode == 2, result.stderr


def _status(root: Path, checkout: Path, operation: str = "status") -> str:
    return f"agent-flow {operation} --root {root} --worktree {checkout.name}"


@pytest.mark.parametrize("host", HOSTS)
def test_pre_block_refuses_a_leader_write_for_every_host(tmp_path: Path, host: str):
    """반증: host가 tool 이름이나 입력 모양을 바꾸면 PreToolUse guard가 그 명령을 못
    보고 leader 쓰기가 실행된다. 같은 세션의 worktree 쓰기는 통과해야 guard가 전부
    막아서 통과하는 것이 아님이 드러난다.
    """
    root, checkout, run_dir = _project(tmp_path)
    _post(host, root, checkout, _status(root, checkout), _status_output(root, checkout, run_dir))
    assert bound_worktree_for_session(SESSION, root) is not None

    allowed, reason = _pre_blocks(host, root, checkout, f"touch {checkout}/feature.txt")
    blocked, block_reason = _pre_blocks(host, root, checkout, f"touch {root}/leaked.txt")

    assert not allowed, reason
    assert blocked and block_reason


@pytest.mark.parametrize("host", HOSTS)
def test_tripwire_reports_a_leader_write_for_every_host(tmp_path: Path, host: str):
    """반증: 동적 경로 쓰기는 pre-block을 지나간다. 그 뒤 PostToolUse tripwire가 host
    결과 모양에서 명령을 못 읽으면 leader 오염이 그대로 남는다.
    """
    root, checkout, run_dir = _project(tmp_path)
    _post(host, root, checkout, _status(root, checkout), _status_output(root, checkout, run_dir))
    command = "python3 -c \"import os; print(os.getcwd())\""

    quiet = _post(host, root, checkout, command, "ok\n")
    (root / "leaked.py").write_text("leaked\n", encoding="utf-8")
    tripped = _post(host, root, checkout, command, "ok\n")

    if host == "omp":
        assert quiet is None
        assert tripped["isError"] is True
        assert tripped["details"] == {"agentFlowHook": "worktree-tripwire.py"}
        assert "write outside bound worktree detected" in tripped["content"][0]["text"]
    else:
        assert quiet[2].returncode == 0, quiet[2].stderr
        assert tripped[2].returncode == 2
        assert "write outside bound worktree detected" in tripped[2].stderr


@pytest.mark.parametrize("host", HOSTS)
def test_binding_records_the_session_for_every_host(tmp_path: Path, host: str):
    """반증: binding이 안 맺히면 그 세션은 worktree에도 쓸 수 없고, 맺힌 것처럼 보고만
    하면 다른 checkout으로 옮겨 갈 수 있다. 세 host 모두 status 결과로 같은 run에 묶인다.
    """
    root, checkout, run_dir = _project(tmp_path)

    _post(host, root, checkout, _status(root, checkout), _status_output(root, checkout, run_dir))

    binding = bound_worktree_for_session(SESSION, root)
    assert binding is not None
    assert binding.checkout.checkout == Path(os.path.realpath(checkout))
    assert binding.checkout.run_id == run_dir.name
    assert binding.guidance_eligible is False


@pytest.mark.parametrize(
    ("host", "continue_grants_guidance"),
    (("claude", True), ("codex", False), ("omp", True)),
)
def test_explicit_resume_grants_guidance_only_on_continue_for_every_host(
    tmp_path: Path, host: str, continue_grants_guidance: bool
):
    """반증: status만으로 guidance가 열리면 run을 재개하지 않은 세션이 흐름에 끌려든다.

    Codex PostToolUse는 exit code 없이 stdout 문자열만 준다. 이 fixture 출력에는 실제
    runner의 성공 receipt가 없어 continue에도 guidance를 열지 않는다(fail-closed).
    """
    root, checkout, run_dir = _project(tmp_path)
    output = _status_output(root, checkout, run_dir)

    _post(host, root, checkout, _status(root, checkout), output)
    after_status = bound_worktree_for_session(SESSION, root)
    _post(host, root, checkout, _status(root, checkout, "continue"), output)
    after_continue = bound_worktree_for_session(SESSION, root)

    assert after_status is not None and after_status.guidance_eligible is False
    assert after_continue is not None
    assert after_continue.guidance_eligible is continue_grants_guidance


def _model_context(host: str, result: Any) -> str:
    """host가 모델 컨텍스트에 넣는 PostToolUse 출력. systemMessage는 사용자에게만 간다."""
    if host == "omp":
        return (result or {}).get("additionalContext", "")
    bound = result[1]
    assert bound.returncode == 0, bound.stderr
    if not bound.stdout.strip():
        return ""
    output = json.loads(bound.stdout)
    assert "systemMessage" not in output
    assert output["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    return output["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("host", HOSTS)
def test_explicit_resume_hands_the_run_command_prefix_to_the_model_for_every_host(
    tmp_path: Path, host: str
):
    """반증: session ID가 Stop systemMessage로만 나가면 세 host 모두 사용자에게만 보여
    모델은 bound worktree에서 run-command를 쓸 수 없다. 모델 채널로 받은 prefix를 그대로
    실행하면 실제 종료 결과가 이 run의 증거로 남아야 한다.
    """
    root, checkout, run_dir = _project(tmp_path)
    output = _status_output(root, checkout, run_dir)

    status = _post(host, root, checkout, _status(root, checkout), output)
    resumed = _post(host, root, checkout, _status(root, checkout, "continue"), output)

    assert _model_context(host, status) == ""
    marker = "[agent-flow] run_command_prefix: "
    lines = [line for line in _model_context(host, resumed).splitlines() if line.startswith(marker)]
    assert len(lines) == 1
    prefix = shlex.split(lines[0].removeprefix(marker))
    assert prefix == ["agent-flow", "run-command", "--host-session-id", SESSION, "--"]
    env = {**_hook_env(), "PYTHONPATH": str(KIT / "src")}
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        env.pop(key, None)
    executed = subprocess.run(
        (sys.executable, "-m", "agent_flow.cli", *prefix[1:],
         sys.executable, "-c", "raise SystemExit(3)"),
        cwd=checkout, env=env, capture_output=True, text=True, timeout=60,
    )
    assert executed.returncode == 3, executed.stderr
    entries = [
        json.loads(line)
        for line in (root / ".agent-flow" / "commands-run.jsonl").read_text().splitlines()
    ]
    observed = [entry for entry in entries if entry.get("source") == "runner"]
    assert len(observed) == 1
    assert observed[0]["exit_code"] == 3 and observed[0]["run_id"] == run_dir.name
