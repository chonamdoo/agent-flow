"""worktree-tripwire.py 및 관련 정적 분석 변경 테스트.

테스트 범위:
1. 이전에 _opaque_command_violation 이 막던 합법 명령(echo $HOME, 히어독, $(date))이
   이제 정적 분석을 통과하는지 확인한다.
2. tripwire hook 이 실제 leader 외부 쓰기를 잡는지 확인한다(exit 2).
3. tripwire hook 이 worktree 내부 쓰기에 반응하지 않는지 확인한다(exit 0).
4. node_modules 심링크 false positive 회귀 — 심링크가 leader 쪽으로 해소되더라도
   논리 경로가 worktree 안이면 위반으로 보지 않는다.

탐지 방법과 한계:
- leader: binding 저장 시점 스냅샷과 현재 git status 를 비교한다. 스냅샷 이후
  변경만 잡는다.
- sibling worktree: git status --porcelain 이 깨끗한지 본다. 명령 실행 전에 이미
  dirty 상태였으면 새 쓰기와 구분하지 못한다(false positive 가능).
- gitignore 대상 파일이나 git 트리 밖(/tmp 등) 쓰기는 잡지 못한다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_flow.artifact import create_run
from agent_flow.core.host_write_boundary import (
    HostWriteBoundaryError,
    host_write_boundary_violation,
    record_host_checkout_binding,
)
from agent_flow.core.worktrees import (
    create_worktree,
    plan_worktree,
    worktree_runtime_root,
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ("git", *args),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _setup(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "init", cwd=root)

    statuses = []
    runs = []
    for name in ("first", "second"):
        status = create_worktree(
            root=root,
            plan=plan_worktree(root=root, name=name),
        )
        runtime_root = worktree_runtime_root(root=root, name=status.name)
        run_dir = create_run(
            runtime_root,
            "default",
            f"task-{name}",
            checkout_identity=f"worktree:{status.name}",
            checkout_registration_identity=status.registration_identity,
        )
        statuses.append(status)
        runs.append(run_dir)
    return root, statuses, runs


def _status_payload(root: Path, status, run_dir: Path, session: str = "session-1"):
    next_command = f"agent-flow continue --root {root} --worktree {status.name}"
    return {
        "tool_name": "bash",
        "tool_input": {
            "command": f"agent-flow status --root {root} --worktree {status.name}"
        },
        "session_id": session,
        "exit_code": 0,
        "output": "status_json: "
        + json.dumps(
            {
                "status": "awaiting_host",
                "run": f"default/{run_dir.name}",
                "next_command": next_command,
            }
        ),
        "cwd": str(root),
    }


def _command_payload(
    command: str,
    *,
    cwd: Path | None = None,
    session: str = "session-1",
    host_cwd: Path | None = None,
) -> dict:
    tool_input = {"command": command}
    if cwd is not None:
        tool_input["cwd"] = str(cwd)
    payload: dict = {"tool_name": "bash", "tool_input": tool_input, "session_id": session}
    if host_cwd is not None:
        payload["cwd"] = str(host_cwd)
    return payload


def _install_tripwire_hook(root: Path) -> Path:
    kit_root = Path(__file__).resolve().parents[1]
    installed = root / ".agent-flow"
    hooks = installed / "scripts" / "hooks"
    runtime = installed / "runtime" / "python"
    hooks.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        kit_root / "scripts" / "hooks" / "worktree-tripwire.py",
        hooks / "worktree-tripwire.py",
    )
    shutil.copy2(
        kit_root / "scripts" / "hooks" / "bind-host-worktree.py",
        hooks / "bind-host-worktree.py",
    )
    shutil.copytree(
        kit_root / "src" / "agent_flow",
        runtime / "agent_flow",
        dirs_exist_ok=True,
    )
    return hooks


def test_echo_dollar_home_passes_static_check(tmp_path: Path):
    """$HOME 처럼 변수 확장이 있는 명령은 이제 정적 분석을 통과한다."""
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    violation = host_write_boundary_violation(
        _command_payload("echo $HOME", cwd=first.path),
        root,
    )

    assert violation is None


def test_heredoc_command_passes_static_check(tmp_path: Path):
    """히어독(<<EOF)이 포함된 명령은 이제 정적 분석을 통과한다."""
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    heredoc_cmd = "cat <<EOF\nhello world\nEOF"
    violation = host_write_boundary_violation(
        _command_payload(heredoc_cmd, cwd=first.path),
        root,
    )

    assert violation is None


def test_command_substitution_passes_static_check(tmp_path: Path):
    """$(...) 명령 치환이 있는 명령은 이제 정적 분석을 통과한다."""
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    violation = host_write_boundary_violation(
        _command_payload("echo $(date)", cwd=first.path),
        root,
    )

    assert violation is None


def test_python_inline_code_passes_static_check(tmp_path: Path):
    """python3 -c 인라인 코드는 이제 정적 분석을 통과한다.

    실제 쓰기 탐지는 PostToolUse worktree-tripwire.py 가 담당한다.
    """
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    violation = host_write_boundary_violation(
        _command_payload(
            "python3 -c \"import os; print(os.getcwd())\"",
            cwd=first.path,
        ),
        root,
    )

    assert violation is None


def test_shell_minus_c_passes_static_check(tmp_path: Path):
    """bash -c 인라인 코드는 이제 정적 분석을 통과한다."""
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    violation = host_write_boundary_violation(
        _command_payload("bash -c 'echo hello'", cwd=first.path),
        root,
    )

    assert violation is None


def test_tripwire_fires_on_leader_write(tmp_path: Path):
    """tripwire 는 leader 에 파일이 생긴 것을 binding 스냅샷과 비교해 잡는다."""
    root, statuses, runs = _setup(tmp_path)
    hooks = _install_tripwire_hook(root)

    # binding 수립
    subprocess.run(
        ("/usr/bin/python3", "-I", str(hooks / "bind-host-worktree.py")),
        cwd=root,
        input=json.dumps(_status_payload(root, statuses[0], runs[0])),
        text=True,
        capture_output=True,
        check=False,
    )

    # binding 이후 leader 에 파일 생성 — worktree 밖 쓰기 시뮬레이션
    (root / "leaked.py").write_text("leaked\n", encoding="utf-8")

    result = subprocess.run(
        ("/usr/bin/python3", "-I", str(hooks / "worktree-tripwire.py")),
        cwd=root,
        input=json.dumps(
            _command_payload("echo $HOME", cwd=statuses[0].path)
        ),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "leader checkout changed during the phase" in result.stderr


def test_tripwire_silent_on_worktree_write(tmp_path: Path):
    """tripwire 는 bound worktree 내부 쓰기에는 반응하지 않는다."""
    root, statuses, runs = _setup(tmp_path)
    hooks = _install_tripwire_hook(root)

    subprocess.run(
        ("/usr/bin/python3", "-I", str(hooks / "bind-host-worktree.py")),
        cwd=root,
        input=json.dumps(_status_payload(root, statuses[0], runs[0])),
        text=True,
        capture_output=True,
        check=False,
    )

    # worktree 내부에 파일 생성 — 정상 쓰기
    (statuses[0].path / "feature.py").write_text("ok\n", encoding="utf-8")

    result = subprocess.run(
        ("/usr/bin/python3", "-I", str(hooks / "worktree-tripwire.py")),
        cwd=root,
        input=json.dumps(
            _command_payload("echo $HOME", cwd=statuses[0].path)
        ),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0


def test_tripwire_silent_when_no_binding(tmp_path: Path):
    """binding 이 없는 세션에서 tripwire 는 조용히 통과한다."""
    root, statuses, runs = _setup(tmp_path)
    hooks = _install_tripwire_hook(root)

    result = subprocess.run(
        ("/usr/bin/python3", "-I", str(hooks / "worktree-tripwire.py")),
        cwd=root,
        input=json.dumps(
            _command_payload("echo hello", cwd=root, session="unbound-session")
        ),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0


def test_symlinked_node_modules_not_flagged_as_outside_checkout(tmp_path: Path):
    """node_modules 이 leader 쪽 심링크여도 논리 경로가 worktree 안이면 통과한다.

    이전 버그: _resolve_path 가 심링크를 따라 leader 경로로 해소하면
    candidate loop 이 "references checkout path outside the bound worktree"
    를 반환했다.
    """
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]

    # binding 전에 leader 에 node_modules 구성 — snapshot 기준선에 포함돼야
    # assert_leader_unchanged 가 오탐하지 않는다.
    leader_node_bin = root / "node_modules" / ".bin"
    leader_node_bin.mkdir(parents=True)
    (leader_node_bin / "tsc").write_text("#!/bin/sh\n", encoding="utf-8")
    (leader_node_bin / "tsc").chmod(0o755)

    # worktree 에서 leader node_modules 를 심링크로 공유 — binding 전에 설치
    wt_node_link = first.path / "node_modules"
    wt_node_link.symlink_to(root / "node_modules", target_is_directory=True)

    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    # 심링크를 따르면 leader 경로가 나온다 — 이것이 이전 false positive 원인
    resolved = (first.path / "node_modules" / ".bin" / "tsc").resolve()
    assert str(resolved).startswith(str(root)), (
        f"심링크가 leader 를 가리켜야 하는데 {resolved}"
    )

    violation = host_write_boundary_violation(
        _command_payload(
            "./node_modules/.bin/tsc --noEmit",
            cwd=first.path,
        ),
        root,
    )

    assert violation is None, (
        f"node_modules 심링크에서 false positive 발생: {violation}"
    )


def test_genuine_sibling_path_still_blocked_after_symlink_fix(tmp_path: Path):
    """심링크 수정 이후에도 실제 sibling checkout 경로는 여전히 막힌다."""
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    violation = host_write_boundary_violation(
        _command_payload(
            f"python3 {second.path / 'tool.py'}",
            cwd=first.path,
        ),
        root,
    )

    assert violation is not None
    assert "outside the bound worktree" in violation
