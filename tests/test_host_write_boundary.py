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
from agent_flow.core.worktree_isolation import WorktreeIsolationError
from agent_flow.core.worktrees import (
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


def test_shell_write_target_obeys_the_same_rule_as_the_write_tool(tmp_path: Path):
    """반증: `Write`가 막는 자리를 `bash`가 열어 주면 경계는 없는 것과 같다.

    두 경로가 같은 규칙을 쓰는지 한 대상으로 대조한다. 규칙이 갈리면 에이전트는
    거부당한 write를 그대로 셸로 옮겨 적기만 하면 된다.
    """
    root, statuses, runs = _setup(tmp_path)
    first, _second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    outside = tmp_path / "outside.txt"
    write_violation = host_write_boundary_violation(_write_payload(outside), root)
    shell_violation = host_write_boundary_violation(
        _command_payload(f"touch {outside}", cwd=first.path),
        root,
    )
    assert write_violation is not None
    assert shell_violation is not None, "bash가 Write와 달리 worktree 밖 쓰기를 통과시켰다"
    assert "outside the bound worktree" in shell_violation

    # 리다이렉션도 같은 대상이다. 명령 이름만 보면 이 형태를 놓친다.
    redirect_violation = host_write_boundary_violation(
        _command_payload(f"printf x > {outside}", cwd=first.path),
        root,
    )
    assert redirect_violation is not None


def test_shell_reads_outside_the_worktree_stay_allowed(tmp_path: Path):
    """불변: 쓰기 경계가 읽기까지 막으면 인터프리터·도구 호출이 전부 죽는다.

    변이 케이스로 짝을 이룬다 — 같은 절대 경로를 쓰기 대상으로 바꾸면 거부돼야
    한다. 그래야 "읽기 허용"이 경계를 통째로 끈 결과가 아님이 증명된다.
    """
    root, statuses, runs = _setup(tmp_path)
    first, _second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    tool = tmp_path / "tool.py"
    tool.write_text("print('hi')\n", encoding="utf-8")
    assert host_write_boundary_violation(
        _command_payload(f"python3 {tool} --check", cwd=first.path),
        root,
    ) is None

    assert host_write_boundary_violation(
        _command_payload(f"truncate -s 0 {tool}", cwd=first.path),
        root,
    ) is not None


def test_host_binding_rejects_recreated_active_run_checkout(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    _git("worktree", "remove", "--force", str(first.path), cwd=root)
    _git("worktree", "add", str(first.path), first.branch, cwd=root)

    with pytest.raises(
        HostWriteBoundaryError,
        match="active run registration does not own its worktree",
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
            first.path.relative_to(root) / "src" / "feature.py",
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
        match="no unique registered worktree",
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

    with pytest.raises(
        WorktreeIsolationError,
        match="not a direct child of a managed root",
    ):
        host_write_boundary_violation(
            _write_payload(checkout / "feature.py", session="unbound"),
            root,
        )


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
