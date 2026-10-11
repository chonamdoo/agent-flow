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
    for key in ("OMP_PROFILE", "CLAUDECODE", "CLAUDE_CLI", "CODEX_CLI", "CODEX_HOME"):
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


def _shell(command: str, checkout: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """host처럼 셸로 실행한다. hook이 앞에 붙인 환경 변수 할당은 셸만 해석한다."""
    return subprocess.run(
        ("/bin/sh", "-c", command), cwd=checkout, env=env, capture_output=True, text=True,
    )


def _nonce(prepared: str) -> str:
    name, value = shlex.split(prepared)[0].split("=", 1)
    assert name == "AGENT_FLOW_HOST_CONTINUE_NONCE"
    return value



def _prepared_command(
    root: Path, checkout: Path, env: dict[str, str], command: str, *, host: str = "codex",
) -> str:
    pre = _payload(host, "pre_tool_use", root, checkout, command, "")
    guarded = _hook(root, checkout, env, "guard-host-worktree.sh", pre)
    assert guarded.returncode == 0, guarded.stderr
    if not guarded.stdout.strip():
        return command
    rewrite = json.loads(guarded.stdout)
    assert rewrite["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    # Codex는 allow 없는 updatedInput을 hook 실패로 보고 적용하지 않는다.
    # Claude의 allow는 권한 확인을 건너뛰므로 붙지 않아야 한다.
    expected_decision = "allow" if host == "codex" else None
    assert rewrite["hookSpecificOutput"].get("permissionDecision") == expected_decision
    return rewrite["hookSpecificOutput"]["updatedInput"]["command"]


@pytest.mark.parametrize("post_input", ("original", "rewritten"))
def test_codex_continue_runner_receipt_grants_guidance(tmp_path: Path, post_input: str):
    root, checkout, run_dir, env = _installed_project(tmp_path)
    command = f"agent-flow continue --root {shlex.quote(str(root))} --worktree first"
    prepared = _prepared_command(root, checkout, env, command)
    continued = _shell(prepared, checkout, env)
    assert continued.returncode == 0, continued.stderr
    status_lines = [
        line.removeprefix("status_json:")
        for line in continued.stdout.splitlines()
        if line.startswith("status_json:")
    ]
    assert len(status_lines) == 1
    assert json.loads(status_lines[0])["run"] == f"default/{run_dir.name}"
    post = _payload(
        "codex", "post_tool_use", root, checkout,
        command if post_input == "original" else prepared, continued.stdout,
    )
    assert isinstance(post["tool_response"], str)
    assert "exit_code" not in post
    bound = _hook(root, checkout, env, "bind-host-worktree.py", post)
    assert bound.returncode == 0, bound.stderr

    binding = bound_worktree_for_session(SESSION, root)
    assert binding is not None
    assert binding.checkout.run_id == run_dir.name
    assert binding.guidance_eligible is True


def _completed_continue(
    root: Path, checkout: Path, env: dict[str, str], *, host: str = "codex",
) -> tuple[str, str, str, Path]:
    command = f"agent-flow continue --root {shlex.quote(str(root))} --worktree first"
    prepared = _prepared_command(root, checkout, env, command, host=host)
    continued = _shell(prepared, checkout, env)
    assert continued.returncode == 0, continued.stderr
    receipt_lines = [line for line in continued.stdout.splitlines() if line.startswith("continue_receipt:")]
    assert len(receipt_lines) == 1
    nonce, token = receipt_lines[0].split(" ", 1)[1].split(":")
    assert len(nonce) == len(token) == 64
    result = root / ".git" / "agent-flow" / "host-sessions" / "continue-receipts" / f"{nonce}.result.json"
    assert result.is_file()
    return command, prepared, continued.stdout, result


@pytest.mark.parametrize("case", (
    "output-only", "wrong-token", "session", "tool-use", "tool-name", "argv",
    "run-metadata", "stale", "explicit-failure", "returncode-failure", "rewritten-nonce",
    "replay", "failed-event-replay",
    "duplicate-output", "pre-event", "public-result", "public-directory",
    "result-symlink", "result-hardlink", "result-fifo",
))
def test_continue_receipt_rejects_unproven_results(tmp_path: Path, case: str):
    root, checkout, run_dir, env = _installed_project(tmp_path)
    command, prepared, output, result = _completed_continue(root, checkout, env)
    post = _payload("codex", "post_tool_use", root, checkout, command, output)
    session = SESSION
    if case == "output-only":
        result.unlink()
    elif case == "wrong-token":
        nonce = result.name.split(".", 1)[0]
        post["tool_response"] = output[:output.index("continue_receipt:")] + f"continue_receipt: {nonce}:{'0' * 64}\n"
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
    elif case == "rewritten-nonce":
        post["tool_input"]["command"] = f"AGENT_FLOW_HOST_CONTINUE_NONCE={'0' * 64} {command}"
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


@pytest.mark.parametrize("case", ("public-request", "reused-request", "runner-failure"))
def test_continue_request_requires_private_single_use_runner_success(tmp_path: Path, case: str):
    root, checkout, _, env = _installed_project(tmp_path)
    command = f"agent-flow continue --root {shlex.quote(str(root))} --worktree first"
    if case == "runner-failure":
        command += " --approve invalid-approval-token"
    prepared = _prepared_command(root, checkout, env, command)
    assert prepared != command
    nonce = _nonce(prepared)
    request = root / ".git" / "agent-flow" / "host-sessions" / "continue-receipts" / f"{nonce}.request.json"
    assert request.is_file()
    if case == "public-request":
        request.chmod(0o666)
    elif case == "reused-request":
        first = _shell(prepared, checkout, env)
        assert first.returncode == 0, first.stderr
        assert "continue_receipt:" in first.stdout

    second = _shell(prepared, checkout, env)
    if case == "reused-request":
        # 이미 소모된 nonce는 다른 root의 요청과 구별되지 않는다. continue는 돌되 증거는 없다.
        assert second.returncode == 0, second.stderr
        assert "status_json:" in second.stdout
    else:
        assert second.returncode != 0
    assert "continue_receipt:" not in second.stdout
    assert not request.exists()


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
    command, _, output, result = _completed_continue(root, checkout, env)
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
        resumed = subprocess.run(
            shlex.split(command), cwd=checkout, env=env, capture_output=True, text=True,
        )
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
        command = f"agent-flow continue --root {shlex.quote(str(root))} --worktree first"
        receipt_dir = root / ".git" / "agent-flow" / "host-sessions" / "continue-receipts"
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


@pytest.mark.parametrize("case", ("missing-request", "expired-request"))
def test_continue_without_current_hook_request_runs_without_receipt(tmp_path: Path, case: str):
    """반증: 요청이 없거나 만료됐다고 continue가 실패하면 lifecycle 명령이 hook 상태에 갇힌다."""
    root, checkout, run_dir, env = _installed_project(tmp_path)
    command = f"agent-flow continue --root {shlex.quote(str(root))} --worktree first"
    prepared = _prepared_command(root, checkout, env, command)
    nonce = _nonce(prepared)
    request = root / ".git" / "agent-flow" / "host-sessions" / "continue-receipts" / f"{nonce}.request.json"
    if case == "missing-request":
        request.unlink()
    else:
        state = json.loads(request.read_text(encoding="utf-8"))
        state["recorded_at"] = time.time() - 24 * 60 * 60 - 1
        request.write_text(json.dumps(state), encoding="utf-8")
    continued = _shell(prepared, checkout, env)
    assert continued.returncode == 0, continued.stderr
    assert f"default/{run_dir.name}" in continued.stdout
    assert "continue_receipt:" not in continued.stdout
    assert not request.exists()
    post = _payload("codex", "post_tool_use", root, checkout, prepared, continued.stdout)
    bound = _hook(root, checkout, env, "bind-host-worktree.py", post)
    assert bound.returncode == 0, bound.stderr
    assert bound_worktree_for_session(SESSION, root).guidance_eligible is False


@pytest.mark.parametrize("case", ("malformed-request", "argv-mismatch"))
def test_continue_rejects_tampered_hook_request(tmp_path: Path, case: str):
    root, checkout, _, env = _installed_project(tmp_path)
    command = f"agent-flow continue --root {shlex.quote(str(root))} --worktree first"
    prepared = _prepared_command(root, checkout, env, command)
    nonce = _nonce(prepared)
    request = root / ".git" / "agent-flow" / "host-sessions" / "continue-receipts" / f"{nonce}.request.json"
    if case == "malformed-request":
        request.write_text("not JSON", encoding="utf-8")
    else:
        prepared += " --accept-leader-drift"
    rejected = _shell(prepared, checkout, env)
    assert rejected.returncode != 0
    assert "continue_receipt:" not in rejected.stdout
    assert not request.exists()


def test_continue_receipt_survives_a_long_approval_wait(tmp_path: Path):
    """반증: PreToolUse 뒤 승인 프롬프트에 머문 시간만으로 정상 continue가 guidance를 잃는다."""
    root, checkout, run_dir, env = _installed_project(tmp_path)
    command = f"agent-flow continue --root {shlex.quote(str(root))} --worktree first"
    prepared = _prepared_command(root, checkout, env, command)
    nonce = _nonce(prepared)
    request = root / ".git" / "agent-flow" / "host-sessions" / "continue-receipts" / f"{nonce}.request.json"
    approved_at = time.time() - 60 * 60
    state = json.loads(request.read_text(encoding="utf-8"))
    state["recorded_at"] = approved_at
    request.write_text(json.dumps(state), encoding="utf-8")
    os.utime(request, (approved_at, approved_at))
    continued = _shell(prepared, checkout, env)
    assert continued.returncode == 0, continued.stderr
    assert f"continue_receipt: {nonce}:" in continued.stdout
    post = _payload("codex", "post_tool_use", root, checkout, prepared, continued.stdout)
    bound = _hook(root, checkout, env, "bind-host-worktree.py", post)
    assert bound.returncode == 0, bound.stderr
    binding = bound_worktree_for_session(SESSION, root)
    assert binding.checkout.run_id == run_dir.name
    assert binding.guidance_eligible is True


def test_unbound_continue_for_another_project_root_runs_without_receipt(tmp_path: Path):
    """반증: hook이 붙인 nonce 때문에 다른 프로젝트를 가리킨 continue가 PR 전과 달리 실패한다."""
    root, checkout, _, env = _installed_project(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    other_root, _, other_run, _ = _installed_project(other)
    command = f"agent-flow continue --root {shlex.quote(str(other_root))} --worktree first"
    prepared = _prepared_command(root, checkout, env, command)
    assert prepared != command
    continued = _shell(prepared, checkout, env)
    assert continued.returncode == 0, continued.stderr
    assert f"default/{other_run.name}" in continued.stdout
    assert "continue_receipt:" not in continued.stdout
    assert not (other_root / ".git" / "agent-flow" / "host-sessions").exists()


def test_unconsumed_continue_states_are_swept_after_their_deadline(tmp_path: Path):
    """반증: 승인 거부나 PostToolUse 누락으로 남은 요청·결과가 영구히 쌓인다."""
    root, checkout, _, env = _installed_project(tmp_path)
    command = f"agent-flow continue --root {shlex.quote(str(root))} --worktree first"
    receipt_dir = root / ".git" / "agent-flow" / "host-sessions" / "continue-receipts"
    abandoned = receipt_dir / f"{_nonce(_prepared_command(root, checkout, env, command))}.request.json"
    waiting = receipt_dir / f"{_nonce(_prepared_command(root, checkout, env, command))}.request.json"
    unclaimed_result = receipt_dir / f"{'a' * 64}.result.json"
    fresh_result = receipt_dir / f"{'b' * 64}.result.json"
    for path in (unclaimed_result, fresh_result):
        path.write_text("{}", encoding="utf-8")
        path.chmod(0o600)
    now = time.time()
    os.utime(abandoned, (now - 24 * 60 * 60 - 1,) * 2)
    os.utime(waiting, (now - 60 * 60,) * 2)
    os.utime(unclaimed_result, (now - 301,) * 2)

    latest = receipt_dir / f"{_nonce(_prepared_command(root, checkout, env, command))}.request.json"

    assert not abandoned.exists()
    assert not unclaimed_result.exists()
    assert waiting.is_file()
    assert fresh_result.is_file()
    assert latest.is_file()


def test_receipt_write_failure_keeps_continue_result(tmp_path: Path):
    """반증: run은 진행됐는데 영수증 기록 실패로 continue가 실패를 보고한다."""
    root, checkout, run_dir, env = _installed_project(tmp_path)
    command = f"agent-flow continue --root {shlex.quote(str(root))} --worktree first"
    prepared = _prepared_command(root, checkout, env, command)
    nonce = _nonce(prepared)
    blocker = root / ".git" / "agent-flow" / "host-sessions" / "continue-receipts" / f"{nonce}.result.json"
    blocker.mkdir()
    (blocker / "occupied").write_text("", encoding="utf-8")
    continued = _shell(prepared, checkout, env)
    assert continued.returncode == 0, continued.stderr
    assert f"default/{run_dir.name}" in continued.stdout
    assert "continue_receipt:" not in continued.stdout
    assert "warning: continue receipt was not recorded" in continued.stderr
    post = _payload("codex", "post_tool_use", root, checkout, prepared, continued.stdout)
    bound = _hook(root, checkout, env, "bind-host-worktree.py", post)
    assert bound.returncode == 0, bound.stderr
    assert bound_worktree_for_session(SESSION, root).guidance_eligible is False


def test_rewritten_continue_passes_unchanged_arguments_to_the_cli_on_path(tmp_path: Path):
    """반증: PATH의 `agent-flow`가 이 kit보다 오래되면 hook이 붙인 인자를 몰라 continue가 exit 2로 멈춘다.

    실제 Codex smoke에서 Homebrew 설치본이 `unrecognized arguments`로 거부했다.
    """
    root, checkout, _, env = _installed_project(tmp_path)
    command = f"agent-flow continue --root {shlex.quote(str(root))} --worktree first"
    prepared = _prepared_command(root, checkout, env, command)
    older = tmp_path / "older-cli"
    older.mkdir()
    (older / "agent-flow").write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    (older / "agent-flow").chmod(0o755)
    continued = _shell(prepared, checkout, {**env, "PATH": f"{older}{os.pathsep}{env['PATH']}"})
    assert continued.returncode == 0, continued.stderr
    assert continued.stdout.splitlines() == shlex.split(command)[1:]



@pytest.mark.parametrize("output", ("status-only", "request-nonce"))
def test_codex_continue_without_runner_receipt_stays_ineligible(tmp_path: Path, output: str):
    root, checkout, _, env = _installed_project(tmp_path)
    command = f"agent-flow continue --root {shlex.quote(str(root))} --worktree first"
    prepared = _prepared_command(root, checkout, env, command)
    assert prepared != command
    continued = subprocess.run(
        shlex.split(command), cwd=checkout, env=env, capture_output=True, text=True,
    )
    assert continued.returncode == 0, continued.stderr
    assert "continue_receipt:" not in continued.stdout
    status = continued.stdout
    if output == "request-nonce":
        nonce = _nonce(prepared)
        status += f"continue_receipt: {nonce}:{nonce}\n"
    post = _payload("codex", "post_tool_use", root, checkout, command, status)
    bound = _hook(root, checkout, env, "bind-host-worktree.py", post)
    assert bound.returncode == 0, bound.stderr
    binding = bound_worktree_for_session(SESSION, root)
    assert binding is not None
    assert binding.guidance_eligible is False


def test_parallel_continue_nonces_keep_call_identity(tmp_path: Path):
    root, checkout, _, env = _installed_project(tmp_path)
    command = f"agent-flow continue --root {shlex.quote(str(root))} --worktree first"
    calls: list[tuple[str, str]] = []
    for tool_id in ("matrix-id-first", "matrix-id-second"):
        pre = _payload("codex", "pre_tool_use", root, checkout, command, "")
        pre["tool_use_id"] = tool_id
        guarded = _hook(root, checkout, env, "guard-host-worktree.sh", pre)
        assert guarded.returncode == 0, guarded.stderr
        prepared = json.loads(guarded.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
        calls.append((tool_id, prepared))
    assert _nonce(calls[0][1]) != _nonce(calls[1][1])

    for tool_id, prepared in reversed(calls):
        continued = _shell(prepared, checkout, env)
        assert continued.returncode == 0, continued.stderr
        nonce = _nonce(prepared)
        assert f"continue_receipt: {nonce}:" in continued.stdout
        post = _payload("codex", "post_tool_use", root, checkout, command, continued.stdout)
        post["tool_use_id"] = tool_id
        bound = _hook(root, checkout, env, "bind-host-worktree.py", post)
        assert bound.returncode == 0, bound.stderr
        assert bound_worktree_for_session(SESSION, root).guidance_eligible is True
        receipt_dir = root / ".git" / "agent-flow" / "host-sessions" / "continue-receipts"
        assert not (receipt_dir / f"{nonce}.result.json").exists()
        assert not (receipt_dir / f"{nonce}.request.json").exists()
        binding_key = hashlib.sha256(SESSION.encode("utf-8")).hexdigest()
        (receipt_dir.parent / f"{binding_key}.json").unlink()


def test_continue_result_is_consumed_for_claude_success_event(tmp_path: Path):
    root, checkout, _, env = _installed_project(tmp_path)
    command, _, output, result = _completed_continue(root, checkout, env, host="claude")
    post = _payload("claude", "post_tool_use", root, checkout, command, output)
    bound = _hook(root, checkout, env, "bind-host-worktree.py", post)
    assert bound.returncode == 0, bound.stderr
    assert bound_worktree_for_session(SESSION, root).guidance_eligible is True
    assert not result.exists()


def test_omp_actual_continue_preserves_explicit_success_guidance(tmp_path: Path):
    root, checkout, _, env = _installed_project(tmp_path)
    command = f"agent-flow continue --root {shlex.quote(str(root))} --worktree first"
    continued = subprocess.run(
        shlex.split(command), cwd=checkout, env=env, capture_output=True, text=True,
    )
    assert continued.returncode == 0, continued.stderr
    assert "continue_receipt:" not in continued.stdout
    result = _omp(checkout, "tool_result", _payload("omp", "tool_result", root, checkout, command, continued.stdout))
    assert result is None
    assert bound_worktree_for_session(SESSION, root).guidance_eligible is True
