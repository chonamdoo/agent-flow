from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.artifact import create_run
from agent_flow.core.host_write_boundary import (
    HostWriteBoundaryError,
    assert_adoption_allowed,
    host_write_boundary_violation,
    record_host_checkout_binding,
)
from agent_flow.core.worktree_isolation import (
    WorktreeIsolationError,
    adopted_record_path,
)
from agent_flow.core.worktrees import (
    adopt_worktree,
    create_worktree,
    plan_worktree,
    list_registered_worktrees,
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


def _install_boundary_hooks(root: Path) -> Path:
    kit_root = Path(__file__).resolve().parents[1]
    installed = root / ".agent-flow"
    hooks = installed / "scripts" / "hooks"
    runtime = installed / "runtime" / "python"
    hooks.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        kit_root / "scripts" / "hooks" / "guard-host-worktree.sh",
        hooks / "guard-host-worktree.sh",
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


def _status_payload(root: Path, status, run_dir: Path, session: str = "session-1"):
    next_command = (
        f"agent-flow continue --root {root} --worktree {status.name}"
    )
    return {
        "tool_name": "bash",
        "tool_input": {
            "command": (
                f"agent-flow status --root {root} --worktree {status.name}"
            )
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


def _write_payload(
    path: Path | str,
    *,
    session: str = "session-1",
    host_cwd: Path | None = None,
) -> dict:
    payload = {
        "tool_name": "write",
        "tool_input": {"path": str(path), "content": "changed\n"},
        "session_id": session,
    }
    if host_cwd is not None:
        payload["cwd"] = str(host_cwd)
    return payload


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
    payload = {
        "tool_name": "bash",
        "tool_input": tool_input,
        "session_id": session,
    }
    if host_cwd is not None:
        payload["cwd"] = str(host_cwd)
    return payload


def test_host_binding_allows_only_its_checkout_and_runtime(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    binding = record_host_checkout_binding(
        _status_payload(root, first, runs[0]),
        root,
    )

    assert binding is not None and binding.is_file()
    assert host_write_boundary_violation(
        _write_payload(first.path / "feature.py"),
        root,
    ) is None
    assert host_write_boundary_violation(
        _write_payload(runs[0] / "review.md"),
        root,
    ) is None

    leader_violation = host_write_boundary_violation(
        _write_payload(root / "leaked.py"),
        root,
    )
    sibling_violation = host_write_boundary_violation(
        _write_payload(second.path / "leaked.py"),
        root,
    )
    assert leader_violation is not None and "outside the bound worktree" in leader_violation
    assert sibling_violation is not None and "outside the bound worktree" in sibling_violation


def test_command_write_targets_sees_wrappers_moves_and_globs():
    """반증: 명령 이름이 wrapper에 가려지거나 원본이 목적지가 아니면 대상을 놓친다.

    이 추출기가 놓치면 경계 판정은 아예 호출되지 않는다 — 뒤의 protected 목록이
    아무리 정확해도 통과다.
    """
    from agent_flow.core.host_write_boundary import _command_write_targets

    assert _command_write_targets("env touch /tmp/x") == ("/tmp/x",)
    assert _command_write_targets("FOO=1 nohup rm -rf /tmp/y") == ("/tmp/y",)
    assert set(_command_write_targets("mv /a/one /b/two")) == {"/a/one", "/b/two"}
    assert set(_command_write_targets("ln /a/one /b/two")) == {"/a/one", "/b/two"}
    assert _command_write_targets("cp /a/one /b/two") == ("/b/two",)
    assert set(
        _command_write_targets("rsync --remove-source-files /a/one /b/two")
    ) == {"/a/one", "/b/two"}
    assert _command_write_targets("rm -rf /tmp/dir/*") == ("/tmp/dir/*",)


def test_shell_write_target_obeys_the_same_rule_as_the_write_tool(tmp_path: Path):
    """반증: `Write`가 막는 자리를 `bash`가 열어 주면 경계는 없는 것과 같다.

    대상은 idle 형제 checkout이다. 두 경로가 같은 대상을 같은 결론으로 판정하는지,
    그리고 무관한 자리는 둘 다 통과하는지 대조한다.
    """
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    (runs[1] / "active").unlink()

    leaked = second.path / "leaked.txt"
    write_violation = host_write_boundary_violation(_write_payload(leaked), root)
    shell_violation = host_write_boundary_violation(
        _command_payload(f"touch {leaked}", cwd=first.path),
        root,
    )
    assert write_violation is not None
    assert shell_violation is not None, "bash가 Write와 달리 형제 checkout 쓰기를 통과시켰다"
    assert "outside the bound worktree" in shell_violation

    # 리다이렉션도 같은 대상이다. 명령 이름만 보면 이 형태를 놓친다.
    redirect_violation = host_write_boundary_violation(
        _command_payload(f"printf x > {leaked}", cwd=first.path),
        root,
    )
    assert redirect_violation is not None

    # checkout 어디에도 속하지 않는 자리는 두 경로 모두 통과한다. leader가
    # `~/Downloads` 같은 폴더 안에 있을 때 무관한 파일까지 막지 않는다.
    unrelated = tmp_path / "outside.txt"
    assert host_write_boundary_violation(_write_payload(unrelated), root) is None
    assert host_write_boundary_violation(
        _command_payload(f"touch {unrelated}", cwd=first.path),
        root,
    ) is None


def test_shell_reads_outside_the_worktree_stay_allowed(tmp_path: Path):
    """불변: 쓰기 경계가 읽기까지 막으면 인터프리터·도구 호출이 전부 죽는다.

    무관한 자리는 읽기도 쓰기도 통과한다. 변이 케이스로 형제 checkout을 짝지어
    대조한다 — 그쪽은 읽기든 쓰기든 거부다. 그래야 "무관한 자리 허용"이 경계를
    통째로 끈 결과가 아님이 증명된다.
    """
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    (runs[1] / "active").unlink()

    tool = tmp_path / "tool.py"
    tool.write_text("print('hi')\n", encoding="utf-8")
    assert host_write_boundary_violation(
        _command_payload(f"python3 {tool} --check", cwd=first.path),
        root,
    ) is None

    assert host_write_boundary_violation(
        _command_payload(f"truncate -s 0 {tool}", cwd=first.path),
        root,
    ) is None
    assert host_write_boundary_violation(
        _command_payload(f"python3 {second.path / 'README.md'} --check", cwd=first.path),
        root,
    ) is not None
    assert host_write_boundary_violation(
        _command_payload(f"git -C {second.path} checkout -- .", cwd=first.path),
        root,
    ) is not None


def test_idle_registered_sibling_worktree_stays_protected(tmp_path: Path):
    """반증: run이 끝난 형제 checkout도 남의 작업이다.

    protected를 active 목록만으로 만들면, 정리 전 형제 worktree의 커밋 안 된
    작업이 bound 세션의 쓰기에 열린다. 무관한 자리 허용과 짝으로 대조한다.
    """
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    (runs[1] / "active").unlink()

    write_violation = host_write_boundary_violation(
        _write_payload(second.path / "leaked.py"),
        root,
    )
    shell_violation = host_write_boundary_violation(
        _command_payload(f"touch {second.path / 'leaked.py'}", cwd=first.path),
        root,
    )

    assert write_violation is not None
    assert "outside the bound worktree" in write_violation
    assert shell_violation is not None
    assert host_write_boundary_violation(
        _write_payload(tmp_path / "outside.txt"),
        root,
    ) is None


def test_boundary_blocks_paths_that_contain_or_drain_other_checkouts(tmp_path: Path):
    """반증: 보호 대상의 **안쪽**만 보면 두 구멍이 남는다.

    1) 보호 대상을 품는 경로 한 줄(`rm -rf <leader의 부모>`)이 leader를 통째로 지운다.
    2) `mv`는 목적지만 검사하면 형제 checkout의 파일을 밖으로 옮겨 그 checkout에서 지운다.
    """
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    ancestor = host_write_boundary_violation(
        _command_payload(f"rm -rf {root.parent}", cwd=first.path),
        root,
    )
    drained = host_write_boundary_violation(
        _command_payload(
            f"mv {second.path / 'README.md'} {tmp_path / 'stolen.md'}",
            cwd=first.path,
        ),
        root,
    )
    unrelated_move = host_write_boundary_violation(
        _command_payload(
            f"mv {tmp_path / 'a.md'} {tmp_path / 'b.md'}",
            cwd=first.path,
        ),
        root,
    )

    assert ancestor is not None
    assert drained is not None
    assert unrelated_move is None


def test_host_binding_rejects_recreated_active_run_checkout(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    _git("worktree", "remove", "--force", str(first.path), cwd=root)
    _git("worktree", "add", str(first.path), first.branch, cwd=root)

    # 같은 경로에 raw git으로 다시 만든 checkout은 등록 지문도 채택 기록도 원래
    # 것을 잇지 못한다. 관리 자리 밖이면 채택 증명이 먼저 깨지므로 경계 오류로 나온다.
    with pytest.raises(
        HostWriteBoundaryError,
        match="does not own its worktree|is not a provable managed worktree",
    ):
        record_host_checkout_binding(
            _status_payload(root, first, runs[0]),
            root,
        )


def test_bound_host_stops_after_an_undetected_leader_write(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    leaked = root / "dynamically-computed-leak.py"
    leaked.write_text("leaked\n", encoding="utf-8")

    violation = host_write_boundary_violation(
        _write_payload(first.path / "safe.py"),
        root,
    )

    assert violation is not None
    assert "leader checkout changed during the phase" in violation
    assert str(first.path) in violation
    assert leaked.read_text(encoding="utf-8") == "leaked\n"


def test_relative_write_target_uses_the_real_host_cwd(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    leader_relative = host_write_boundary_violation(
        _write_payload("src/leaked.py", host_cwd=root),
        root,
    )
    unknown_cwd = host_write_boundary_violation(
        _write_payload("src/unknown.py"),
        root,
    )
    worktree_relative = host_write_boundary_violation(
        _write_payload("src/feature.py", host_cwd=first.path),
        root,
    )
    leader_to_worktree = host_write_boundary_violation(
        _write_payload(
            # 새 기본 자리는 leader 밖이므로 leader 기준 상대 경로는 `..`로 시작한다.
            os.path.relpath(first.path / "src" / "feature.py", root),
            host_cwd=root,
        ),
        root,
    )

    assert leader_relative is not None and "outside the bound worktree" in leader_relative
    assert unknown_cwd is not None and "no trusted host cwd" in unknown_cwd
    assert worktree_relative is None
    assert leader_to_worktree is None


def test_codex_apply_patch_command_uses_the_host_cwd(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    patch = "*** Begin Patch\n*** Update File: src/feature.py\n*** End Patch\n"
    payload = {
        "tool_name": "apply_patch",
        "tool_input": {"command": patch},
        "session_id": "session-1",
        "cwd": str(first.path),
    }

    assert host_write_boundary_violation(payload, root) is None

    payload["cwd"] = str(root)
    violation = host_write_boundary_violation(payload, root)

    assert violation is not None
    assert "outside the bound worktree" in violation


def test_host_boundary_resolves_symlinks_before_allowing_write(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    link = first.path / "sibling-link"
    link.symlink_to(second.path, target_is_directory=True)

    violation = host_write_boundary_violation(
        _write_payload(link / "leaked.py"),
        root,
    )

    assert violation is not None
    assert str(second.path) in violation


def test_unbound_host_cannot_write_while_worktree_runs_are_active(tmp_path: Path):
    root, statuses, _ = _setup(tmp_path)

    violation = host_write_boundary_violation(
        _write_payload(statuses[0].path / "feature.py", session="unbound"),
        root,
    )

    assert violation is not None
    assert "not bound to an active worktree" in violation


def test_active_run_without_registered_worktree_fails_closed(tmp_path: Path):
    root, statuses, _ = _setup(tmp_path)
    _git(
        "worktree",
        "remove",
        "--force",
        str(statuses[0].path),
        cwd=root,
    )

    with pytest.raises(
        HostWriteBoundaryError,
        match="no provable registered worktree owner",
    ):
        host_write_boundary_violation(
            _write_payload(root / "leaked.py", session="unbound"),
            root,
        )


def test_active_run_in_external_worktree_is_not_trusted(tmp_path: Path):
    root, _, _ = _setup(tmp_path)
    checkout = tmp_path / "external-worktree"
    _git(
        "worktree",
        "add",
        "-b",
        "feat/external-worktree",
        str(checkout),
        cwd=root,
    )
    registration = next(
        entry.registration_identity
        for entry in list_registered_worktrees(root)
        if entry.path.resolve() == checkout.resolve()
    )
    runtime_root = worktree_runtime_root(root=root, name=checkout.name)
    create_run(
        runtime_root,
        "default",
        "external",
        checkout_identity=f"worktree:{checkout.name}",
        checkout_registration_identity=registration,
    )

    # 채택 기록이 없으면 claim의 소유 checkout을 증명할 수 없다. 등록만 된 checkout이
    # 자기 이름의 런타임 상태를 만들어 놓는 것으로는 신뢰를 얻지 못한다.
    with pytest.raises(
        HostWriteBoundaryError,
        match="no provable registered worktree owner",
    ):
        host_write_boundary_violation(
            _write_payload(checkout / "feature.py", session="unbound"),
            root,
        )


def test_adoption_is_refused_while_a_run_is_active(tmp_path: Path):
    root, statuses, _runs = _setup(tmp_path)
    outside = tmp_path / "worker-made"
    _git("worktree", "add", "-b", "feat/worker-made", str(outside), cwd=root)

    # `git worktree add`는 경계의 write 명령 집합에 없어 워커도 실행할 수 있다. 채택까지
    # 실행할 수 있으면 워커가 스스로 임의 checkout을 인가해 쓰기 경계를 넓힌다. 명령
    # 문자열(`env agent-flow …`)도 cwd(`cd /tmp && …`)도 호출자가 고르므로, 호출자가
    # 고를 수 없는 것으로 판정한다 — 활성 run의 존재다.
    with pytest.raises(HostWriteBoundaryError, match="refusing to adopt while runs are active"):
        assert_adoption_allowed(root=root)

    for status in statuses:
        (worktree_runtime_root(root=root, name=status.name) / ".agent-flow" / "runs").rename(
            worktree_runtime_root(root=root, name=status.name) / ".agent-flow" / "runs-done"
        )
    assert assert_adoption_allowed(root=root) is None


def test_bound_session_cannot_write_the_adoption_record(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    record_host_checkout_binding(_status_payload(root, statuses[0], runs[0]), root)
    record = adopted_record_path(root=root, name=statuses[0].name)
    assert record is not None

    # 채택 기록이 워커의 쓰기 허용 구역(런타임 상태 디렉터리) 안에 있으면 워커가 자기
    # 이름의 기록에서 경로만 바꿔 관리 루트 밖 디렉터리를 자기 checkout으로 만든다.
    assert host_write_boundary_violation(_write_payload(record), root) is not None
    assert (
        host_write_boundary_violation(
            _command_payload(f"tee {record}", cwd=statuses[0].path), root
        )
        is not None
    )


def test_adopted_external_worktree_is_trusted(tmp_path: Path):
    """불변: 채택은 사람이 주는 인가다. 받은 checkout은 관리 루트 밖이어도 신뢰된다."""
    root, _, _ = _setup(tmp_path)
    checkout = tmp_path / "external-worktree"
    _git(
        "worktree",
        "add",
        "-b",
        "feat/external-worktree",
        str(checkout),
        cwd=root,
    )
    # 채택이 먼저다. runtime state가 이미 있으면 attach가 manifest를 쓰지 않고 돌아간다.
    status = adopt_worktree(root=root, path=checkout, allow_dirty=True)
    runtime_root = worktree_runtime_root(root=root, name=status.name)
    run_dir = create_run(
        runtime_root,
        "default",
        "external",
        checkout_identity=f"worktree:{status.name}",
        checkout_registration_identity=status.registration_identity,
    )
    binding = record_host_checkout_binding(
        _status_payload(root, status, run_dir),
        root,
    )

    assert binding is not None
    assert host_write_boundary_violation(
        _write_payload(checkout / "feature.py"),
        root,
    ) is None


def test_basename_collision_does_not_poison_unrelated_worktrees(tmp_path: Path):
    """반증: claim을 디렉터리 이름으로 묶으면 같은 basename 하나로 저장소 전역이 죽는다.

    소유자는 manifest에 적힌 경로가 지목한다.
    """
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    collision = tmp_path / "elsewhere" / first.path.name
    collision.parent.mkdir()
    _git(
        "worktree",
        "add",
        "-b",
        "feat/collision",
        str(collision),
        cwd=root,
    )
    binding = record_host_checkout_binding(
        _status_payload(root, first, runs[0]),
        root,
    )

    assert binding is not None
    assert host_write_boundary_violation(
        _write_payload(first.path / "feature.py"),
        root,
    ) is None


def test_bound_shell_requires_current_worktree_cwd_and_blocks_sibling_paths(
    tmp_path: Path,
):
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    assert host_write_boundary_violation(
        _command_payload("python3 -m pytest -q", cwd=first.path),
        root,
    ) is None
    wrong_cwd = host_write_boundary_violation(
        _command_payload("python3 -m pytest -q", host_cwd=root),
        root,
    )
    sibling_path = host_write_boundary_violation(
        _command_payload(
            f"python3 -c 'print(1)' {second.path / 'leaked.py'}",
            cwd=first.path,
        ),
        root,
    )
    assert wrong_cwd is not None and "shell cwd is not bound" in wrong_cwd
    assert sibling_path is not None and "outside the bound worktree" in sibling_path

def test_bound_shell_blocks_embedded_leader_literals_and_relative_symlinks(
    tmp_path: Path,
):
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    leak_link = first.path / "leak"
    leak_link.symlink_to(root / "leaked.py")

    embedded = host_write_boundary_violation(
        _command_payload(
            f"python3 -c \"from pathlib import Path; Path('{root / 'leaked.py'}').write_text('x')\"",
            cwd=first.path,
        ),
        root,
    )
    symlinked = host_write_boundary_violation(
        _command_payload("printf leaked > leak", cwd=first.path),
        root,
    )

    assert embedded is not None and "outside the bound worktree" in embedded
    assert symlinked is not None and "outside the bound worktree" in symlinked


def test_bound_shell_allows_dynamic_path_commands_at_static_check(
    tmp_path: Path,
):
    """정책 변경: 정적 분석은 인라인 코드를 더 이상 막지 않는다.

    실제 쓰기 탐지는 PostToolUse worktree-tripwire.py 가 담당한다.
    """
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    violation = host_write_boundary_violation(
        _command_payload(
            (
                "python3 -c \"from pathlib import Path; "
                "target = Path.cwd().parents[2] / 'dynamic-leak.py'; "
                "target.write_text('leaked')\""
            ),
            cwd=first.path,
        ),
        root,
    )

    # 정적 분석은 이제 통과한다. 실제 쓰기는 PostToolUse tripwire 가 탐지한다.
    assert violation is None
    assert not (root / "dynamic-leak.py").exists()



def test_bound_shell_accepts_explicit_cd_and_matching_lifecycle_command(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    assert host_write_boundary_violation(
        _command_payload(
            f"cd {first.path} && python3 -m pytest -q",
            host_cwd=root,
        ),
        root,
    ) is None
    assert host_write_boundary_violation(
        _command_payload(
            f"agent-flow continue --root {root} --worktree {first.name}",
            host_cwd=first.path,
        ),
        root,
    ) is None


def test_bound_host_blocks_new_run_and_foreign_lifecycle_root(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    foreign = tmp_path / "foreign"
    foreign.mkdir()

    new_run = host_write_boundary_violation(
        _command_payload(
            f"agent-flow run other --root {root}",
            cwd=first.path,
        ),
        root,
    )
    foreign_status = host_write_boundary_violation(
        _command_payload(
            f"agent-flow status --root {foreign}",
            cwd=first.path,
        ),
        root,
    )

    assert new_run is not None and "new run" in new_run
    assert foreign_status is not None and "another project root" in foreign_status


def test_live_binding_cannot_move_to_another_checkout(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    with pytest.raises(HostWriteBoundaryError, match="already bound"):
        record_host_checkout_binding(
            _status_payload(root, second, runs[1]),
            root,
        )


def test_unbound_lifecycle_command_is_allowed_to_establish_binding(tmp_path: Path):
    root, statuses, _ = _setup(tmp_path)
    first = statuses[0]

    assert host_write_boundary_violation(
        _command_payload(
            f"agent-flow status --root {root} --worktree {first.name}",
            session="new-session",
            host_cwd=root,
        ),
        root,
    ) is None


def test_compound_lifecycle_command_cannot_bypass_an_unbound_session(tmp_path: Path):
    root, statuses, _ = _setup(tmp_path)
    first = statuses[0]
    payload = _command_payload(
        (
            f"agent-flow status --root {root} --worktree {first.name}; "
            f"printf leaked > {root / 'leaked.py'}"
        ),
        session="unbound",
        host_cwd=root,
    )

    violation = host_write_boundary_violation(payload, root)

    assert violation is not None
    assert "not bound to an active worktree" in violation


def test_unbound_session_does_not_trust_lifecycle_lookalike_executables(
    tmp_path: Path,
):
    root, statuses, _ = _setup(tmp_path)
    first = statuses[0]
    payload = _command_payload(
        (
            "node /tmp/agent-flow-kit.mjs status "
            f"--root {root} --worktree {first.name}"
        ),
        session="unbound",
        host_cwd=root,
    )

    violation = host_write_boundary_violation(payload, root)

    assert violation is not None
    assert "not bound to an active worktree" in violation


def test_compound_lifecycle_output_cannot_create_a_binding(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    payload = _status_payload(root, statuses[0], runs[0])
    payload["tool_input"]["command"] += "; printf forged"

    assert record_host_checkout_binding(payload, root) is None


def test_binding_status_must_match_a_verified_active_run(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    payload = _status_payload(root, statuses[0], runs[0])
    payload["output"] = payload["output"].replace(runs[0].name, "forged-run")

    with pytest.raises(HostWriteBoundaryError):
        record_host_checkout_binding(payload, root)


def test_parallel_run_binds_from_lines_when_status_json_is_truncated(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    payload = _status_payload(root, first, runs[0])
    payload["tool_input"]["command"] = f"agent-flow run new-task --root {root}"
    payload["output"] = (
        f"run: default/{runs[0].name}\n"
        f"next_command: agent-flow continue --root {root} --worktree {first.name}\n"
        'status_json: {"next_command": "agent-flow continue'
    )

    binding = record_host_checkout_binding(payload, root)

    assert binding is not None
    assert host_write_boundary_violation(
        _write_payload(first.path / "feature.py"),
        root,
    ) is None


def test_patch_paths_are_checked_even_without_a_structured_path_field(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    payload = {
        "tool_name": "apply_patch",
        "tool_input": {
            "patch": (
                "*** Begin Patch\n"
                f"*** Update File: {second.path / 'leaked.py'}\n"
                "@@\n-old\n+new\n"
                "*** End Patch\n"
            )
        },
        "session_id": "session-1",
    }

    violation = host_write_boundary_violation(payload, root)

    assert violation is not None
    assert str(second.path) in violation


def test_patch_move_destination_is_checked(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    payload = {
        "tool_name": "edit",
        "tool_input": {
            "input": (
                "*** Begin Patch\n"
                f"[{first.path / 'source.py'}#A1B2]\n"
                f"MV {second.path / 'leaked.py'}\n"
                "*** End Patch\n"
            )
        },
        "session_id": "session-1",
        "cwd": str(first.path),
    }

    violation = host_write_boundary_violation(payload, root)

    assert violation is not None
    assert str(second.path) in violation


def test_binding_file_symlink_is_not_trusted(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    binding = record_host_checkout_binding(
        _status_payload(root, statuses[0], runs[0]),
        root,
    )
    assert binding is not None
    replacement = binding.with_name("forged.json")
    replacement.write_text(binding.read_text(encoding="utf-8"), encoding="utf-8")
    binding.unlink()
    binding.symlink_to(replacement)

    violation = host_write_boundary_violation(
        _write_payload(statuses[0].path / "feature.py"),
        root,
    )

    assert violation is not None
    assert "not bound to an active worktree" in violation


def test_binding_directory_symlink_is_rejected(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    binding_directory = root / ".git" / "agent-flow" / "host-sessions"
    binding_directory.symlink_to(outside, target_is_directory=True)

    with pytest.raises(HostWriteBoundaryError, match="not a real directory"):
        record_host_checkout_binding(
            _status_payload(root, statuses[0], runs[0]),
            root,
        )

    assert not tuple(outside.iterdir())


def test_installed_hooks_bind_then_block_a_leader_write(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    hooks = _install_boundary_hooks(root)

    binding = subprocess.run(
        ("/usr/bin/python3", "-I", str(hooks / "bind-host-worktree.py")),
        cwd=root,
        input=json.dumps(_status_payload(root, statuses[0], runs[0])),
        text=True,
        capture_output=True,
        check=False,
    )
    blocked = subprocess.run(
        ("/bin/bash", str(hooks / "guard-host-worktree.sh")),
        cwd=root,
        input=json.dumps(_write_payload(root / "leaked.py")),
        text=True,
        capture_output=True,
        check=False,
    )

    assert binding.returncode == 0, binding.stderr
    assert blocked.returncode == 2
    assert "작업을 중단했습니다" in blocked.stderr


def test_installed_guard_stops_after_a_dynamic_leader_leak(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    hooks = _install_boundary_hooks(root)
    binding = subprocess.run(
        ("/usr/bin/python3", "-I", str(hooks / "bind-host-worktree.py")),
        cwd=root,
        input=json.dumps(_status_payload(root, statuses[0], runs[0])),
        text=True,
        capture_output=True,
        check=False,
    )
    (root / "dynamic-leak.py").write_text("leaked\n", encoding="utf-8")

    blocked = subprocess.run(
        ("/bin/bash", str(hooks / "guard-host-worktree.sh")),
        cwd=root,
        input=json.dumps(_write_payload(statuses[0].path / "safe.py")),
        text=True,
        capture_output=True,
        check=False,
    )

    assert binding.returncode == 0, binding.stderr
    assert blocked.returncode == 2
    assert "leader checkout changed during the phase" in blocked.stderr
    assert str(statuses[0].path) in blocked.stderr


def test_installed_guard_allows_non_git_projects_without_active_worktrees(
    tmp_path: Path,
):
    root = tmp_path / "non-git"
    root.mkdir()
    hooks = _install_boundary_hooks(root)

    result = subprocess.run(
        ("/bin/bash", str(hooks / "guard-host-worktree.sh")),
        cwd=root,
        input=json.dumps(_write_payload(root / "feature.py")),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_virtual_write_targets_do_not_escape_the_filesystem_boundary(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    record_host_checkout_binding(_status_payload(root, statuses[0], runs[0]), root)
    payload = {
        "tool_name": "write",
        "tool_input": {"path": "xd://lsp", "content": "{}"},
        "session_id": "session-1",
    }

    assert host_write_boundary_violation(payload, root) is None

    payload["tool_input"]["path"] = "file:///tmp/leaked.py"
    violation = host_write_boundary_violation(payload, root)

    assert violation is not None
    assert "virtual write target is not trusted" in violation
