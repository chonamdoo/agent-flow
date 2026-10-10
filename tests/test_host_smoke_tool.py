"""`tools/host-smoke`: 모델 없이 검사할 수 있는 판정, fixture 형식, host 프로세스 수명."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parents[1]
FIXTURES = KIT / "tests" / "fixtures" / "host-payloads"


def _tool():
    spec = importlib.util.spec_from_file_location("host_smoke", KIT / "tools" / "host-smoke" / "run.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass가 문자열 annotation을 풀 때 모듈을 sys.modules에서 찾는다.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_validate_checks_scenarios_and_fixtures_without_model_calls(tmp_path: Path, monkeypatch):
    """반증: 캡처본에 실제 경로나 session id가 남으면 개인 정보가 저장소에 들어간다.
    자리표시자가 빠지거나, 파일이 없거나, 파일과 event가 어긋나면 cross-host 행렬이
    실제 host 모양 대신 다른 payload를 재생한다.
    """
    tool = _tool()

    def no_model_calls(*args, **kwargs):
        raise AssertionError(f"--validate must not run host CLIs: {args!r}")

    monkeypatch.setattr(tool.subprocess, "run", no_model_calls)
    assert tool.validate(FIXTURES) == []

    broken = tmp_path / "host-payloads"
    shutil.copytree(FIXTURES, broken)
    codex = broken / "codex" / "post_tool_use.json"
    record = json.loads(codex.read_text(encoding="utf-8"))
    record["payload"]["cwd"] = "/Users/someone/project"
    codex.write_text(json.dumps(record), encoding="utf-8")
    claude = broken / "claude" / "pre_tool_use.json"
    record = json.loads(claude.read_text(encoding="utf-8"))
    record["payload"]["tool_input"]["command"] = "ls"
    claude.write_text(json.dumps(record), encoding="utf-8")
    omp = broken / "omp" / "tool_result.json"
    record = json.loads(omp.read_text(encoding="utf-8"))
    record["payload"]["event"]["content"][0]["text"] = "plain output"
    omp.write_text(json.dumps(record), encoding="utf-8")
    (broken / "omp" / "tool_call.json").unlink()
    claude_post = broken / "claude" / "post_tool_use.json"
    record = json.loads(claude_post.read_text(encoding="utf-8"))
    record["event"] = "PreToolUse"
    record["payload"]["session_id"] = "0f1e2d3c-real-session"
    claude_post.write_text(json.dumps(record), encoding="utf-8")
    codex_pre = broken / "codex" / "pre_tool_use.json"
    record = json.loads(codex_pre.read_text(encoding="utf-8"))
    for key in ("session_id", "turn_id", "tool_use_id"):
        record["payload"].pop(key)
    codex_pre.write_text(json.dumps(record), encoding="utf-8")

    errors = tool.validate(broken)

    assert any("codex/post_tool_use.json" in error and "absolute path" in error for error in errors)
    assert any("claude/pre_tool_use.json" in error and "${COMMAND}" in error for error in errors)
    assert any("omp/tool_result.json" in error and "${STATUS_OUTPUT}" in error for error in errors)
    assert any("omp/tool_call.json" in error and "missing" in error for error in errors)
    assert any("claude/post_tool_use.json" in error and "PostToolUse" in error for error in errors)
    assert any("claude/post_tool_use.json" in error and "session_id" in error for error in errors)
    assert any(
        "codex/pre_tool_use.json" in error and "session_id" in error and "tool_use_id" in error
        for error in errors
    )


def _shaped(
    shape: str, call_id: str | None, command: str, output: str, hook: str = ""
) -> list[dict]:
    """host마다 관측한 기록 모양. Claude만 PostToolUse 피드백을 결과와 따로 남긴다."""
    if shape == "claude":
        events = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": call_id, "content": output, "is_error": True},
            ]}},
        ]
        if hook:
            events.append({"type": "attachment", "attachment": {
                "type": "hook_blocking_error", "toolUseID": call_id,
                "blockingError": {"blockingError": hook},
            }})
        return events
    output = "\n".join(text for text in (output, hook) if text)
    if shape == "codex":
        return [
            {"type": "response_item", "payload": {
                "type": "custom_tool_call", "call_id": call_id, "name": "exec",
                "input": f"text(await tools.exec_command({{cmd:{json.dumps(command)}}}));",
            }},
            {"type": "response_item", "payload": {
                "type": "custom_tool_call_output", "call_id": call_id,
                "output": [{"type": "input_text", "text": output}],
            }},
        ]
    return [
        {"type": "tool_execution_start", "toolCallId": call_id, "toolName": "bash",
         "args": {"command": command}},
        {"type": "tool_execution_end", "toolCallId": call_id, "toolName": "bash",
         "result": {"content": [{"type": "text", "text": output}]}},
    ]


@pytest.mark.parametrize("shape", ("claude", "codex", "omp"))
def test_smoke_judges_attempts_from_tool_calls_not_prompt_text(tmp_path: Path, shape: str):
    """반증: prompt에도 명령, 파일 이름, 차단 문구가 들어 있다. 기록 전체를 문자열로 찾으면
    모델이 아무것도 실행하지 않아도 leader 쓰기 차단이 `pass`가 되고, 재개 세션이
    leader 쓰기를 건너뛰어도 binding 유지가 `pass`가 된다. binding 없이 거부된
    쓰기를 통과로 세면 binding이 끊긴 세션도 통과한다. status를 실행하지 않은 세션의
    binding 부재를 실패로 세면 인증 실패나 timeout이 hook 실패로 보고된다. status
    세션이 끝난 뒤에 본 binding만 status의 결과로 센다. continue가 만든 binding을
    status의 것으로 세면 status가 binding을 못 만들어도 1번이 통과한다.
    """
    tool = _tool()
    project, checkout = tmp_path / "project", tmp_path / "checkout"
    project.mkdir()
    checkout.mkdir()
    status = f"agent-flow status --root {project} --worktree feat-host-smoke"
    proceed = f"agent-flow continue --root {project} --worktree feat-host-smoke"
    literal = f"touch {project}/host-smoke-literal.txt"
    dynamic = "python3 -c \"pathlib.Path(leader, 'host-smoke-dynamic.txt').write_text('x')\""
    resume_checkout = f"touch {checkout}/host-smoke-resume.txt"
    resume_leader = f"touch {project}/host-smoke-resume-leader.txt"
    refused = (
        f"shell command references checkout path {project} outside the bound worktree {checkout}"
    )
    prompt = {"type": "user", "message": {"role": "user", "content": "\n".join((
        status, proceed, literal, dynamic, tool.FAILURE_MARKER, resume_checkout, resume_leader,
        tool.TRIPWIRE_MARKER, refused,
    ))}}

    def judge(events: list[dict], bindings: list[dict] | None = None) -> dict:
        return tool.judge_main(
            tool.tool_calls(events), project=project, checkout=checkout,
            bindings=bindings or [], failures=[],
        )

    assert tool.judge_status(tool.tool_calls([prompt]), [])[0] == "not-run"
    assert {state for state, _ in judge([prompt]).values()} == {"not-run"}
    assert tool.judge_resume(tool.tool_calls([prompt]), project=project, checkout=checkout)[0] == (
        "not-run"
    )
    assert tool.judge_status(tool.tool_calls(_shaped(shape, "s1", status, "")), [])[0] == "fail"
    assert tool.judge_status(tool.tool_calls(_shaped(shape, "s1", status, "")), [{}])[0] == "pass"

    unbound = "this host session is not bound to an active worktree"
    steps = judge([
        prompt,
        *_shaped(shape, "s2", proceed, ""),
        *_shaped(shape, "c1", literal, unbound),
        *_shaped(shape, "c2", dynamic, "(no output)"),
    ])
    assert [steps[step][0] for step in "245"] == ["fail", "fail", "fail"]

    steps = judge(
        [
            prompt,
            *_shaped(shape, "s2", proceed, ""),
            *_shaped(shape, "c1", literal, refused),
            *_shaped(shape, "c2", dynamic, "(no output)", hook=tool.TRIPWIRE_MARKER),
        ],
        bindings=[{"guidance_eligible": True}],
    )
    assert [steps[step][0] for step in "245"] == ["pass", "pass", "pass"]

    (checkout / "host-smoke-resume.txt").touch()
    skipped = tool.tool_calls(_shaped(shape, "r1", resume_checkout, ""))
    assert tool.judge_resume(skipped, project=project, checkout=checkout)[0] == "not-run"
    resumed = tool.tool_calls([
        *_shaped(shape, "r1", resume_checkout, ""),
        *_shaped(shape, "r2", resume_leader, refused),
    ])
    assert tool.judge_resume(resumed, project=project, checkout=checkout)[0] == "pass"


@pytest.mark.parametrize("shape", ("claude", "codex", "omp"))
def test_smoke_ignores_tool_records_without_a_call_id(tmp_path: Path, shape: str):
    """반증: id가 없는 호출과 결과를 같은 키로 묶으면, 관련 없는 결과의 거부 문구가
    다른 호출에 붙어 leader 쓰기 차단이 `pass`가 된다.
    """
    tool = _tool()
    project, checkout = tmp_path / "project", tmp_path / "checkout"
    refused = f"outside the bound worktree {checkout}"
    calls = tool.tool_calls([
        *_shaped(shape, None, f"touch {project}/host-smoke-literal.txt", ""),
        *_shaped(shape, None, "ls", refused),
    ])

    steps = tool.judge_main(calls, project=project, checkout=checkout, bindings=[], failures=[])

    assert steps["4"][0] == "not-run"


def _host_with_a_grandchild(pid_file: Path) -> list[str]:
    """자손 하나를 띄워 그 pid를 남기고 잠드는 가짜 host. 실제 host의 shell·hook 자리다."""
    script = "\n".join((
        "import os, subprocess, sys, time",
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])",
        f"partial = {str(pid_file) + '.tmp'!r}",
        "with open(partial, 'w') as handle:",
        "    handle.write(str(child.pid))",
        f"os.replace(partial, {str(pid_file)!r})",
        "time.sleep(120)",
    ))
    return [sys.executable, "-c", script]


def _gone(pid: int) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert path.exists(), "the fake host never started its grandchild"


def test_host_sessions_end_their_process_group_on_timeout_and_interrupt(
    tmp_path: Path, monkeypatch
):
    """반증: host는 sandbox와 승인을 끈 채 돈다. 직접 자식만 끝내거나 timeout에서만
    정리하면, Ctrl-C 뒤에도 host의 shell과 hook이 남아 scratch를 지우는 동안 계속 쓴다.
    timeout을 정리하던 중에 Ctrl-C가 와도 host를 회수해야 좀비가 남지 않는다.
    """
    tool = _tool()
    timed_out = tmp_path / "timed-out.pid"

    _, _, returncode = tool.run_host(
        _host_with_a_grandchild(timed_out), cwd=tmp_path, env=dict(os.environ), timeout=3
    )

    assert returncode == 124
    assert _gone(int(timed_out.read_text()))

    interrupted = tmp_path / "interrupted.pid"
    communicate = subprocess.Popen.communicate

    def ctrl_c(self, *args, **kwargs):
        _wait_for(interrupted)
        monkeypatch.setattr(subprocess.Popen, "communicate", communicate)
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess.Popen, "communicate", ctrl_c)

    with pytest.raises(KeyboardInterrupt):
        tool.run_host(
            _host_with_a_grandchild(interrupted), cwd=tmp_path, env=dict(os.environ), timeout=60
        )

    assert _gone(int(interrupted.read_text()))

    cleaning_up = tmp_path / "cleaning-up.pid"
    hosts: list[subprocess.Popen] = []

    def timeout_then_ctrl_c(self, *args, **kwargs):
        hosts.append(self)
        if len(hosts) == 1:
            _wait_for(cleaning_up)
            raise subprocess.TimeoutExpired(self.args, 60)
        monkeypatch.setattr(subprocess.Popen, "communicate", communicate)
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess.Popen, "communicate", timeout_then_ctrl_c)

    with pytest.raises(KeyboardInterrupt):
        tool.run_host(
            _host_with_a_grandchild(cleaning_up), cwd=tmp_path, env=dict(os.environ), timeout=60
        )

    assert hosts[0].returncode is not None
    assert _gone(int(cleaning_up.read_text()))
