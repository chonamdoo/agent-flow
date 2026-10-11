from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_flow.artifact import create_run
from agent_flow.core.host_write_boundary import bound_worktree_for_session
from agent_flow.core.worktrees import create_worktree, plan_worktree, worktree_runtime_root
from tests.test_host_protection_matrix import KIT, SESSION, _omp, _payload


def _git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, text=True)


def _installed_project(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")

    env = dict(os.environ)
    for key in ("OMP_PROFILE", "CLAUDECODE", "CLAUDE_CLI", "CODEX_CLI", "CODEX_HOME", "CODEX_THREAD_ID"):
        env.pop(key, None)
    env["HOME"] = str(tmp_path / "home")
    Path(env["HOME"]).mkdir()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    installed = subprocess.run(
        ("node", str(KIT / "bin" / "agent-flow-kit.mjs"), "install",
         "--root", str(root), "--profile", "python", "--hooks"),
        cwd=root, env=env, capture_output=True, text=True,
    )
    assert installed.returncode == 0, installed.stderr
    _git(root, "add", ".")
    _git(root, "commit", "--allow-empty", "-m", "installed fixture")

    checkout = create_worktree(root=root, plan=plan_worktree(root=root, name="first"))
    run_dir = create_run(
        worktree_runtime_root(root=root, name=checkout.name),
        "default",
        "Codex continue success receipt",
        checkout_root=checkout.path,
        checkout_identity=f"worktree:{checkout.name}",
        checkout_registration_identity=checkout.registration_identity,
    )
    env["PATH"] = str(root / ".agent-flow" / "bin") + os.pathsep + env["PATH"]
    env["AGENT_FLOW_ADAPTER"] = "codex"
    return root, checkout.path, run_dir, env


def _hook(
    root: Path, checkout: Path, env: dict[str, str], script: str, payload: dict
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (str(root / ".agent-flow" / "bin" / "agent-flow-hook"),
         str(root / ".agent-flow" / "scripts" / "hooks" / script)),
        input=json.dumps(payload), cwd=checkout, env=env, capture_output=True, text=True,
    )


def _receipt_dir(root: Path) -> Path:
    return root / ".git" / "agent-flow" / "host-sessions" / "continue-receipts"


def _continue_command(root: Path) -> str:
    return f"agent-flow continue --root {shlex.quote(str(root))} --worktree first"


def _pre_tool_use(
    root: Path, checkout: Path, env: dict[str, str], command: str, *,
    host: str = "codex", session: str = SESSION, tool_use_id: str = "matrix-id",
) -> Path | None:
    """guard를 돌리고 새로 남은 요청 파일을 돌려준다."""
    receipts = _receipt_dir(root)
    before = set(receipts.glob("*.request.json")) if receipts.is_dir() else set()
    pre = _payload(host, "pre_tool_use", root, checkout, command, "")
    pre["session_id"] = session
    pre["tool_use_id"] = tool_use_id
    guarded = _hook(root, checkout, env, "guard-host-worktree.sh", pre)
    assert guarded.returncode == 0, guarded.stderr
    # 명령을 고쳐 쓰면 host의 승인 규칙 매칭이 바뀌고 오래된 CLI가 모르는 인자로 멈춘다.
    assert guarded.stdout.strip() == ""
    created = (set(receipts.glob("*.request.json")) if receipts.is_dir() else set()) - before
    assert len(created) <= 1
    return created.pop() if created else None


def _run_continue(
    command: str, checkout: Path, env: dict[str, str], *, thread: str | None = SESSION,
) -> subprocess.CompletedProcess[str]:
    """Codex가 명령 환경에 thread id를 넣는 것처럼 실행한다."""
    run_env = dict(env)
    if thread is not None:
        run_env["CODEX_THREAD_ID"] = thread
    return subprocess.run(
        shlex.split(command), cwd=checkout, env=run_env, capture_output=True, text=True,
    )


def _post_tool_use(
    root: Path, checkout: Path, env: dict[str, str], command: str, output: str, *,
    session: str = SESSION, tool_use_id: str = "matrix-id",
) -> None:
    post = _payload("codex", "post_tool_use", root, checkout, command, output)
    post["session_id"] = session
    post["tool_use_id"] = tool_use_id
    bound = _hook(root, checkout, env, "bind-host-worktree.py", post)
    assert bound.returncode == 0, bound.stderr


def test_codex_continue_runner_receipt_grants_guidance(tmp_path: Path):
    root, checkout, run_dir, env = _installed_project(tmp_path)
    command = _continue_command(root)
    request = _pre_tool_use(root, checkout, env, command)
    assert request is not None
    continued = _run_continue(command, checkout, env)
    assert continued.returncode == 0, continued.stderr
    status_lines = [
        line.removeprefix("status_json:")
        for line in continued.stdout.splitlines()
        if line.startswith("status_json:")
    ]
    assert len(status_lines) == 1
    assert json.loads(status_lines[0])["run"] == f"default/{run_dir.name}"
    assert not request.exists()
    post = _payload("codex", "post_tool_use", root, checkout, command, continued.stdout)
    assert isinstance(post["tool_response"], str)
    assert "exit_code" not in post
    bound = _hook(root, checkout, env, "bind-host-worktree.py", post)
    assert bound.returncode == 0, bound.stderr

    binding = bound_worktree_for_session(SESSION, root)
    assert binding is not None
    assert binding.checkout.run_id == run_dir.name
    assert binding.guidance_eligible is True


def _completed_continue(
    root: Path, checkout: Path, env: dict[str, str],
) -> tuple[str, str, Path]:
    command = _continue_command(root)
    _pre_tool_use(root, checkout, env, command)
    continued = _run_continue(command, checkout, env)
    assert continued.returncode == 0, continued.stderr
    receipt_lines = [line for line in continued.stdout.splitlines() if line.startswith("continue_receipt:")]
    assert len(receipt_lines) == 1
    request_id, token = receipt_lines[0].split(" ", 1)[1].split(":")
    assert len(request_id) == len(token) == 64
    result = _receipt_dir(root) / f"{request_id}.result.json"
    assert result.is_file()
    return command, continued.stdout, result


@pytest.mark.parametrize("case", (
    "output-only", "wrong-token", "session", "tool-use", "tool-name", "argv",
    "run-metadata", "stale", "explicit-failure", "returncode-failure",
    "replay", "failed-event-replay",
    "duplicate-output", "pre-event", "public-result", "public-directory",
    "result-symlink", "result-hardlink", "result-fifo",
))
def test_continue_receipt_rejects_unproven_results(tmp_path: Path, case: str):
    root, checkout, run_dir, env = _installed_project(tmp_path)
    command, output, result = _completed_continue(root, checkout, env)
    post = _payload("codex", "post_tool_use", root, checkout, command, output)
    session = SESSION
    if case == "output-only":
        result.unlink()
    elif case == "wrong-token":
        request_id = result.name.split(".", 1)[0]
        post["tool_response"] = output[:output.index("continue_receipt:")] + f"continue_receipt: {request_id}:{'0' * 64}\n"
    elif case == "session":
        session = post["session_id"] = "another-session"
    elif case == "tool-use":
        post["tool_use_id"] = "another-tool-call"
    elif case == "tool-name":
        post["tool_name"] = "Edit"
    elif case == "argv":
        post["tool_input"]["command"] += " --accept-leader-drift"
    elif case == "run-metadata":
        metadata = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        metadata["task"] = "changed after this continue"
        (run_dir / "meta.json").write_text(json.dumps(metadata), encoding="utf-8")
    elif case == "stale":
        receipt = json.loads(result.read_text(encoding="utf-8"))
        receipt["recorded_at"] = time.time() - 301
        result.write_text(json.dumps(receipt), encoding="utf-8")
    elif case == "explicit-failure":
        post["exit_code"] = 1
    elif case == "returncode-failure":
        post["returncode"] = 1
    elif case == "replay":
        first = _hook(root, checkout, env, "bind-host-worktree.py", post)
        assert first.returncode == 0, first.stderr
        assert bound_worktree_for_session(SESSION, root).guidance_eligible is True
        binding_key = hashlib.sha256(SESSION.encode("utf-8")).hexdigest()
        (root / ".git" / "agent-flow" / "host-sessions" / f"{binding_key}.json").unlink()
    elif case == "failed-event-replay":
        post["exit_code"] = 1
        failed = _hook(root, checkout, env, "bind-host-worktree.py", post)
        assert failed.returncode == 0, failed.stderr
        assert bound_worktree_for_session(SESSION, root) is None
        del post["exit_code"]
    elif case == "duplicate-output":
        receipt_line = output[output.index("continue_receipt:"):]
        post["tool_response"] += receipt_line
    elif case == "pre-event":
        post["hook_event_name"] = "PreToolUse"
    elif case == "public-result":
        result.chmod(0o666)
    elif case == "public-directory":
        result.parent.chmod(0o777)
    elif case in {"result-symlink", "result-hardlink"}:
        substitute = tmp_path / "planted-result.json"
        result.rename(substitute)
        if case == "result-symlink":
            result.symlink_to(substitute)
        else:
            os.link(substitute, result)
    elif case == "result-fifo":
        result.unlink()
        os.mkfifo(result)

    _hook(root, checkout, env, "bind-host-worktree.py", post)
    binding = bound_worktree_for_session(session, root)
    assert binding is None or binding.guidance_eligible is False


@pytest.mark.parametrize("case", (
    "missing-request", "expired-request", "malformed-request", "public-request",
    "argv-mismatch", "another-thread", "no-thread", "consumed-request",
))
def test_continue_without_a_provable_hook_request_runs_without_receipt(tmp_path: Path, case: str):
    """반증: 요청을 증명할 수 없다고 continue가 실패하면 lifecycle 명령이 hook 상태에 갇힌다.

    반대로 증명할 수 없는 요청으로 receipt를 내면 다른 호출이 guidance를 얻는다.
    """
    root, checkout, run_dir, env = _installed_project(tmp_path)
    command = _continue_command(root)
    request = _pre_tool_use(root, checkout, env, command)
    assert request is not None
    run_command, thread = command, SESSION
    if case == "missing-request":
        request.unlink()
    elif case == "expired-request":
        state = json.loads(request.read_text(encoding="utf-8"))
        state["recorded_at"] = time.time() - 24 * 60 * 60 - 1
        request.write_text(json.dumps(state), encoding="utf-8")
    elif case == "malformed-request":
        request.write_text("not JSON", encoding="utf-8")
    elif case == "public-request":
        request.chmod(0o666)
    elif case == "argv-mismatch":
        run_command += " --accept-leader-drift"
    elif case == "another-thread":
        thread = "another-session"
    elif case == "no-thread":
        thread = None
    else:
        first = _run_continue(command, checkout, env)
        assert first.returncode == 0, first.stderr
        assert "continue_receipt:" in first.stdout
        next(_receipt_dir(root).glob("*.result.json")).unlink()
    continued = _run_continue(run_command, checkout, env, thread=thread)
    assert continued.returncode == 0, continued.stderr
    assert f"default/{run_dir.name}" in continued.stdout
    assert "continue_receipt:" not in continued.stdout
    assert not list(_receipt_dir(root).glob("*.result.json"))
    _post_tool_use(root, checkout, env, command, continued.stdout)
    assert bound_worktree_for_session(SESSION, root).guidance_eligible is False


def test_continue_runner_failure_issues_no_receipt(tmp_path: Path):
    root, checkout, _, env = _installed_project(tmp_path)
    command = _continue_command(root) + " --approve invalid-approval-token"
    request = _pre_tool_use(root, checkout, env, command)
    assert request is not None
    failed = _run_continue(command, checkout, env)
    assert failed.returncode != 0
    assert "continue_receipt:" not in failed.stdout
    assert not list(_receipt_dir(root).glob("*.result.json"))


def _actual_status(root: Path, checkout: Path, env: dict[str, str], name: str) -> str:
    status = subprocess.run(
        ("agent-flow", "status", "--root", str(root), "--worktree", name),
        cwd=checkout, env=env, capture_output=True, text=True,
    )
    assert status.returncode == 0, status.stderr
    assert "status_json:" in status.stdout
    return status.stdout


@pytest.mark.parametrize("context", ("run", "checkout", "root"))
def test_continue_receipt_rejects_another_checkout_or_run(tmp_path: Path, context: str):
    root, checkout, run_dir, env = _installed_project(tmp_path)
    command, output, result = _completed_continue(root, checkout, env)
    receipt_line = output[output.index("continue_receipt:"):]
    target_name = "first"
    if context == "run":
        metadata = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        (run_dir / "active").unlink()
        successor = create_run(
            worktree_runtime_root(root=root, name="first"), "default", "successor run",
            checkout_root=checkout, checkout_identity=metadata["checkout_identity"],
            checkout_registration_identity=metadata["checkout_registration_identity"],
        )
        resumed = _run_continue(command, checkout, env, thread=None)
        assert resumed.returncode == 0, resumed.stderr
        assert f"default/{successor.name}" in resumed.stdout
    elif context == "checkout":
        sibling = create_worktree(root=root, plan=plan_worktree(root=root, name="second"))
        create_run(
            worktree_runtime_root(root=root, name=sibling.name), "default", "sibling run",
            checkout_root=sibling.path, checkout_identity=f"worktree:{sibling.name}",
            checkout_registration_identity=sibling.registration_identity,
        )
        checkout, target_name = sibling.path, sibling.name
    else:
        other = tmp_path / "other"
        other.mkdir()
        root, checkout, _, env = _installed_project(other)
        command = _continue_command(root)
        receipt_dir = _receipt_dir(root)
        receipt_dir.mkdir(mode=0o700, parents=True)
        transplanted = receipt_dir / result.name
        transplanted.write_bytes(result.read_bytes())
        transplanted.chmod(0o600)

    status = _actual_status(root, checkout, env, target_name)
    post = _payload("codex", "post_tool_use", root, checkout, command, status + receipt_line)
    bound = _hook(root, checkout, env, "bind-host-worktree.py", post)
    assert bound.returncode == 0, bound.stderr
    binding = bound_worktree_for_session(SESSION, root)
    assert binding is not None
    assert binding.guidance_eligible is False


def test_continue_receipt_survives_a_long_approval_wait(tmp_path: Path):
    """반증: PreToolUse 뒤 승인 프롬프트에 머문 시간만으로 정상 continue가 guidance를 잃는다."""
    root, checkout, run_dir, env = _installed_project(tmp_path)
    command = _continue_command(root)
    request = _pre_tool_use(root, checkout, env, command)
    approved_at = time.time() - 60 * 60
    state = json.loads(request.read_text(encoding="utf-8"))
    state["recorded_at"] = approved_at
    request.write_text(json.dumps(state), encoding="utf-8")
    os.utime(request, (approved_at, approved_at))
    continued = _run_continue(command, checkout, env)
    assert continued.returncode == 0, continued.stderr
    assert f"continue_receipt: {request.name.split('.', 1)[0]}:" in continued.stdout
    _post_tool_use(root, checkout, env, command, continued.stdout)
    binding = bound_worktree_for_session(SESSION, root)
    assert binding.checkout.run_id == run_dir.name
    assert binding.guidance_eligible is True


def test_unbound_continue_for_another_project_root_runs_without_receipt(tmp_path: Path):
    """반증: hook이 남긴 요청 때문에 다른 프로젝트를 가리킨 continue가 PR 전과 달리 실패한다."""
    root, checkout, _, env = _installed_project(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    other_root, _, other_run, _ = _installed_project(other)
    command = _continue_command(other_root)
    assert _pre_tool_use(root, checkout, env, command) is not None
    continued = _run_continue(command, checkout, env)
    assert continued.returncode == 0, continued.stderr
    assert f"default/{other_run.name}" in continued.stdout
    assert "continue_receipt:" not in continued.stdout
    assert not (other_root / ".git" / "agent-flow" / "host-sessions").exists()


def test_unconsumed_continue_states_are_swept_after_their_deadline(tmp_path: Path):
    """반증: 승인 거부나 PostToolUse 누락으로 남은 요청·결과가 영구히 쌓인다."""
    root, checkout, _, env = _installed_project(tmp_path)
    command = _continue_command(root)
    abandoned = _pre_tool_use(root, checkout, env, command, tool_use_id="abandoned")
    waiting = _pre_tool_use(root, checkout, env, command, tool_use_id="waiting")
    unclaimed_result = _receipt_dir(root) / f"{'a' * 64}.result.json"
    fresh_result = _receipt_dir(root) / f"{'b' * 64}.result.json"
    for path in (unclaimed_result, fresh_result):
        path.write_text("{}", encoding="utf-8")
        path.chmod(0o600)
    now = time.time()
    os.utime(abandoned, (now - 24 * 60 * 60 - 1,) * 2)
    os.utime(waiting, (now - 60 * 60,) * 2)
    os.utime(unclaimed_result, (now - 301,) * 2)

    latest = _pre_tool_use(root, checkout, env, command, tool_use_id="latest")

    assert not abandoned.exists()
    assert not unclaimed_result.exists()
    assert waiting.is_file()
    assert fresh_result.is_file()
    assert latest.is_file()


def test_receipt_write_failure_keeps_continue_result(tmp_path: Path):
    """반증: run은 진행됐는데 영수증 기록 실패로 continue가 실패를 보고한다."""
    root, checkout, run_dir, env = _installed_project(tmp_path)
    command = _continue_command(root)
    request = _pre_tool_use(root, checkout, env, command)
    blocker = _receipt_dir(root) / f"{request.name.split('.', 1)[0]}.result.json"
    blocker.mkdir()
    (blocker / "occupied").write_text("", encoding="utf-8")
    continued = _run_continue(command, checkout, env)
    assert continued.returncode == 0, continued.stderr
    assert f"default/{run_dir.name}" in continued.stdout
    assert "continue_receipt:" not in continued.stdout
    assert "warning: continue receipt was not recorded" in continued.stderr
    _post_tool_use(root, checkout, env, command, continued.stdout)
    assert bound_worktree_for_session(SESSION, root).guidance_eligible is False


@pytest.mark.parametrize("output", ("status-only", "request-id"))
def test_codex_continue_without_runner_receipt_stays_ineligible(tmp_path: Path, output: str):
    root, checkout, _, env = _installed_project(tmp_path)
    command = _continue_command(root)
    request = _pre_tool_use(root, checkout, env, command)
    continued = _run_continue(command, checkout, env, thread=None)
    assert continued.returncode == 0, continued.stderr
    assert "continue_receipt:" not in continued.stdout
    status = continued.stdout
    if output == "request-id":
        request_id = request.name.split(".", 1)[0]
        status += f"continue_receipt: {request_id}:{request_id}\n"
    _post_tool_use(root, checkout, env, command, status)
    binding = bound_worktree_for_session(SESSION, root)
    assert binding is not None
    assert binding.guidance_eligible is False


def test_parallel_continue_sessions_keep_call_identity(tmp_path: Path):
    root, checkout, _, env = _installed_project(tmp_path)
    command = _continue_command(root)
    calls = (("session-first", "tool-first"), ("session-second", "tool-second"))
    requests = [
        _pre_tool_use(root, checkout, env, command, session=session, tool_use_id=tool_id)
        for session, tool_id in calls
    ]
    for (session, tool_id), request in reversed(list(zip(calls, requests))):
        continued = _run_continue(command, checkout, env, thread=session)
        assert continued.returncode == 0, continued.stderr
        assert f"continue_receipt: {request.name.split('.', 1)[0]}:" in continued.stdout
        _post_tool_use(root, checkout, env, command, continued.stdout, session=session, tool_use_id=tool_id)
        assert bound_worktree_for_session(session, root).guidance_eligible is True
        assert not request.exists()
    assert not list(_receipt_dir(root).iterdir())


def test_ambiguous_continue_requests_in_one_session_issue_no_receipt(tmp_path: Path):
    """반증: 같은 세션·같은 인자의 두 호출 중 아무 요청이나 소모하면 receipt가 다른 호출로 간다."""
    root, checkout, _, env = _installed_project(tmp_path)
    command = _continue_command(root)
    requests = [
        _pre_tool_use(root, checkout, env, command, tool_use_id=tool_id)
        for tool_id in ("tool-first", "tool-second")
    ]
    continued = _run_continue(command, checkout, env)
    assert continued.returncode == 0, continued.stderr
    assert "continue_receipt:" not in continued.stdout
    assert all(request.is_file() for request in requests)


def test_claude_continue_keeps_exit_code_guidance_without_requests(tmp_path: Path):
    root, checkout, _, env = _installed_project(tmp_path)
    command = _continue_command(root)
    assert _pre_tool_use(root, checkout, env, command, host="claude") is None
    continued = _run_continue(command, checkout, env, thread=None)
    assert continued.returncode == 0, continued.stderr
    assert "continue_receipt:" not in continued.stdout
    post = _payload("claude", "post_tool_use", root, checkout, command, continued.stdout)
    bound = _hook(root, checkout, env, "bind-host-worktree.py", post)
    assert bound.returncode == 0, bound.stderr
    assert bound_worktree_for_session(SESSION, root).guidance_eligible is True


def test_omp_actual_continue_preserves_explicit_success_guidance(tmp_path: Path):
    root, checkout, _, env = _installed_project(tmp_path)
    command = _continue_command(root)
    continued = _run_continue(command, checkout, env, thread=None)
    assert continued.returncode == 0, continued.stderr
    assert "continue_receipt:" not in continued.stdout
    result = _omp(checkout, "tool_result", _payload("omp", "tool_result", root, checkout, command, continued.stdout))
    # OMP는 explicit continue 뒤 이 세션의 run-command prefix를 모델 채널로 넘긴다.
    marker = "[agent-flow] run_command_prefix: "
    lines = [line for line in result["additionalContext"].splitlines() if line.startswith(marker)]
    assert len(lines) == 1
    assert shlex.split(lines[0].removeprefix(marker)) == [
        "agent-flow", "run-command", "--host-session-id", SESSION, "--",
    ]
    assert bound_worktree_for_session(SESSION, root).guidance_eligible is True
