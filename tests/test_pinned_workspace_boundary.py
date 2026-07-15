from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_flow.runner import Phase, Runner
from agent_flow.core.workspace_boundary import (
    ExecutionIdentity,
    bind_execution_to_workspace,
    capture_workspace_identity,
    execution_identity_from_context,
    find_active_pinned_workspaces,
    resolve_execution_workspace,
    resolve_mutation_path,
    select_execution_workspace,
)


KIT_ROOT = Path(__file__).resolve().parent.parent
GUARD = KIT_ROOT / "scripts" / "hooks" / "guard-worktree-write.py"


def _tree_integrity(root: Path) -> str:
    entries: list[dict[str, object]] = []

    def visit(current: Path, relative: str) -> None:
        stat = current.lstat()
        entries.append({"path": relative, "type": "directory", "mode": stat.st_mode & 0o777})
        for child in sorted(current.iterdir(), key=lambda path: path.name):
            child_relative = f"{relative}/{child.name}" if relative else child.name
            child_stat = child.lstat()
            if child.is_dir():
                visit(child, child_relative)
            else:
                entries.append(
                    {
                        "path": child_relative,
                        "type": "file",
                        "mode": child_stat.st_mode & 0o777,
                        "sha256": hashlib.sha256(child.read_bytes()).hexdigest(),
                    }
                )

    visit(root, "")
    entries.sort(key=lambda entry: str(entry["path"]))
    payload = json.dumps(
        {"version": 1, "entries": entries},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _runtime_contract_commitment(contract: dict[str, object]) -> str:
    payload = [
        contract["version"],
        contract["launcher"]["path"],
        contract["launcher"]["sha256"],
        contract["node"]["path"],
        contract["node"]["sha256"],
        contract["node"]["device"],
        contract["node"]["inode"],
        contract["node"]["links"],
        contract["node"]["mode"],
        contract["git"]["path"],
        contract["git"]["sha256"],
        contract["git"]["device"],
        contract["git"]["inode"],
        contract["git"]["links"],
        contract["git"]["mode"],
        contract["python"]["path"],
        contract["python"]["resolved_path"],
        contract["python"]["sha256"],
        contract["python"]["device"],
        contract["python"]["inode"],
        contract["python"]["links"],
        contract["python"]["mode"],
        contract["runtime"]["path"],
        contract["runtime"]["integrity"],
        contract["python_runtime"]["path"],
        contract["python_runtime"]["integrity"],
    ]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *args),
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _executable_identity(path: Path) -> dict[str, object]:
    resolved = path.resolve()
    metadata = resolved.lstat()
    return {
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
        "device": str(metadata.st_dev),
        "inode": str(metadata.st_ino),
        "links": str(metadata.st_nlink),
        "mode": metadata.st_mode & 0o777,
    }


def _identity(worktree: Path) -> dict[str, object]:
    resolved = worktree.resolve(strict=True)
    metadata = resolved.stat()
    return {
        "workspace_root": str(resolved),
        "git_common_dir": _git(resolved, "rev-parse", "--path-format=absolute", "--git-common-dir"),
        "git_dir": _git(resolved, "rev-parse", "--path-format=absolute", "--git-dir"),
        "branch": _git(resolved, "branch", "--show-current"),
        "head": _git(resolved, "rev-parse", "HEAD"),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
    }


@pytest.fixture
def pinned_run(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    leader = tmp_path / "project"
    leader.mkdir()
    subprocess.run(("git", "init", "-b", "main", str(leader)), check=True, capture_output=True)
    _git(leader, "config", "user.name", "Test User")
    _git(leader, "config", "user.email", "test@example.com")
    (leader / "shared.txt").write_text("leader\n", encoding="utf-8")
    _git(leader, "add", "shared.txt")
    _git(leader, "commit", "-m", "initial")

    worktree = leader / ".agent-flow" / "worktrees" / "feat-test"
    _git(leader, "worktree", "add", "-b", "feat/test", str(worktree), "main")
    runtime = leader / ".git" / "agent-flow" / "worktrees" / "feat-test"
    runtime.mkdir(parents=True)
    identity = _identity(worktree)
    (runtime / "manifest.json").write_text(
        json.dumps(
            {
                "name": "feat-test",
                "branch": "feat/test",
                "path": ".agent-flow/worktrees/feat-test",
                "identity": identity,
            }
        ),
        encoding="utf-8",
    )
    run_dir = runtime / ".agent-flow" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "active").write_text("", encoding="utf-8")
    meta = {
        "run_id": "run-1",
        "workflow": "default",
        "task": "pinned boundary",
        "started_at": "2026-07-14T00:00:00+00:00",
        "current_phase": "implement",
        "workspace": identity,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    for host in ("codex", "claude", "omp"):
        bind_execution_to_workspace(
            ExecutionIdentity(host=host, session_id="session-1", agent_id=""),
            capture_workspace_identity(worktree),
            run_dir,
            run_id="run-1",
        )
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    node_runtime = leader / ".agent-flow" / "runtime" / "node"
    node_entry = node_runtime / "bin" / "agent-flow-kit.mjs"
    node_entry.parent.mkdir(parents=True)
    node_entry.write_text("process.exit(0);\n", encoding="utf-8")
    for relative in (
        "lib",
        "workflows",
        "profiles",
        "skills",
        "templates",
        "scripts",
        "bootstrap",
        "src/agent_flow",
        ".Codex/agents",
        ".Codex/rules",
        ".Codex/context",
        ".claude/agents",
    ):
        (node_runtime / relative).mkdir(parents=True, exist_ok=True)
    (node_runtime / "lib" / "skill-selection.mjs").write_text("export {};\n", encoding="utf-8")
    python_runtime = leader / ".agent-flow" / "runtime" / "python"
    shutil.copytree(
        KIT_ROOT / "src" / "agent_flow",
        python_runtime / "agent_flow",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    node = shutil.which("node")
    contract = {
        "version": 3,
        "launcher": {
            "path": ".agent-flow/bin/agent-flow",
            "sha256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
        },
        "node": {
            "path": str(Path(node or sys.executable).resolve()),
            **_executable_identity(Path(node or sys.executable)),
        },
        "git": {
            "path": str(Path("/usr/bin/git").resolve()),
            **_executable_identity(Path("/usr/bin/git")),
        },
        "python": {
            "path": str(Path(sys.executable).absolute()),
            "resolved_path": str(Path(sys.executable).resolve()),
            **_executable_identity(Path(sys.executable)),
        },
        "runtime": {
            "path": ".agent-flow/runtime/node",
            "integrity": _tree_integrity(node_runtime),
        },
        "python_runtime": {
            "path": ".agent-flow/runtime/python",
            "integrity": _tree_integrity(python_runtime),
        },
    }
    (leader / ".agent-flow" / "kit.json").write_text(
        json.dumps(
            {
                "project_runtime_contract": contract,
                "project_runtime_contract_commitment_version": 1,
                "project_runtime_contract_commitment": _runtime_contract_commitment(contract),
            }
        ),
        encoding="utf-8",
    )
    return leader, worktree, runtime, run_dir


def _guard(
    leader: Path,
    target: Path,
    *,
    host: str = "codex",
    phase: str = "implement",
    move_to: Path | None = None,
    session_id: str = "session-1",
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(KIT_ROOT / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    move_line = f"\n*** Move to: {move_to}" if move_to is not None else ""
    payload = {
        "tool_name": "apply_patch",
        "cwd": str(cwd or leader),
        "host": host,
        "session_id": session_id,
        "phase": phase,
        "tool_input": {
            "patch": f"*** Begin Patch\n*** Update File: {target}{move_line}\n@@\n-old\n+new\n*** End Patch"
        },
    }
    return subprocess.run(
        (sys.executable, str(GUARD)),
        cwd=leader,
        env=env,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


def _bash_guard(
    leader: Path,
    cwd: Path,
    command: str,
    *,
    host: str = "codex",
    phase: str = "implement",
    session_id: str = "session-1",
    agent_id: str = "",
    env_override: dict[str, str | None] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(KIT_ROOT / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for name, value in (env_override or {}).items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    payload = {
        "tool_name": "Bash",
        "cwd": str(cwd),
        "host": host,
        "session_id": session_id,
        "agent_id": agent_id,
        "phase": phase,
        "tool_input": {"command": command},
    }
    return subprocess.run(
        (sys.executable, str(GUARD)),
        cwd=leader,
        env=env,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


def test_ten_parallel_executions_resolve_only_their_bound_worktree(tmp_path: Path) -> None:
    leader = tmp_path / "project"
    leader.mkdir()
    subprocess.run(("git", "init", "-b", "main", str(leader)), check=True, capture_output=True)
    _git(leader, "config", "user.name", "Test User")
    _git(leader, "config", "user.email", "test@example.com")
    (leader / "shared.txt").write_text("leader\n", encoding="utf-8")
    _git(leader, "add", "shared.txt")
    _git(leader, "commit", "-m", "initial")

    worktrees: list[Path] = []
    executions: list[ExecutionIdentity] = []
    for index in range(10):
        worktree = leader / ".agent-flow" / "worktrees" / f"task-{index}"
        _git(leader, "worktree", "add", "-b", f"feat/task-{index}", str(worktree), "main")
        run_dir = leader / ".git" / "agent-flow" / "worktrees" / f"task-{index}" / ".agent-flow" / "runs" / f"run-{index}"
        run_dir.mkdir(parents=True)
        (run_dir / "active").write_text("", encoding="utf-8")
        identity = capture_workspace_identity(worktree)
        (run_dir / "meta.json").write_text(
            json.dumps({"run_id": f"run-{index}", "workspace": identity.to_dict()}),
            encoding="utf-8",
        )
        execution = ExecutionIdentity("codex", f"session-{index}", "")
        bind_execution_to_workspace(execution, identity, run_dir, run_id=f"run-{index}")
        worktrees.append(worktree)
        executions.append(execution)

    for index, execution in enumerate(executions):
        active = resolve_execution_workspace(leader, execution)
        assert Path(active.identity.workspace_root) == worktrees[index].resolve()
        assert resolve_mutation_path(
            active.identity,
            "shared.txt",
            base_dir=worktrees[index],
            host="codex",
            phase="implement",
        ) == (worktrees[index] / "shared.txt").resolve()
        with pytest.raises(Exception, match="escapes pinned workspace"):
            resolve_mutation_path(
                active.identity,
                "shared.txt",
                base_dir=leader,
                host="codex",
                phase="implement",
            )
        for sibling_index, sibling in enumerate(worktrees):
            if sibling_index == index:
                continue
            with pytest.raises(Exception, match="target_outside_pinned_workspace"):
                resolve_mutation_path(
                    active.identity,
                    sibling / "shared.txt",
                    base_dir=worktrees[index],
                    host="codex",
                    phase="implement",
                )

    with pytest.raises(Exception, match="execution_identity_ambiguous"):
        select_execution_workspace(leader, None)
    second_identity = capture_workspace_identity(worktrees[1])
    second_run = (
        leader
        / ".git"
        / "agent-flow"
        / "worktrees"
        / "task-1"
        / ".agent-flow"
        / "runs"
        / "run-1"
    )
    with pytest.raises(Exception, match="execution_binding_conflict"):
        bind_execution_to_workspace(
            executions[0],
            second_identity,
            second_run,
            run_id="run-1",
        )


def test_unbound_execution_and_relative_leader_write_fail_closed(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, _worktree, _runtime, _run_dir = pinned_run

    unbound = _guard(leader, Path("shared.txt"), session_id="unknown", cwd=leader)
    wrong_cwd = _guard(leader, Path("shared.txt"), cwd=leader)

    assert unbound.returncode == 2
    assert "execution_binding_missing" in unbound.stderr
    assert wrong_cwd.returncode == 2
    assert "escapes pinned workspace" in wrong_cwd.stderr


def test_unbound_execution_can_read_and_enter_authenticated_launcher(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, _worktree, _runtime, _run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"

    read_only = _bash_guard(
        leader,
        leader,
        "git status --short",
        session_id="unbound-session",
    )
    status = _bash_guard(
        leader,
        leader,
        f"{launcher} status",
        session_id="unbound-session",
    )

    assert read_only.returncode == 0, read_only.stderr
    assert status.returncode == 0, status.stderr


def test_host_execution_fields_normalize_to_the_canonical_identity() -> None:
    codex = execution_identity_from_context(
        {"host": "codex", "thread_id": "shared-session"},
        {},
    )
    claude = execution_identity_from_context(
        {"host": "claude", "session_id": "shared-session", "agent_id": "worker-1"},
        {},
    )
    omp = execution_identity_from_context(
        {"host": "omp", "session_id": "shared-session"},
        {},
    )
    override = execution_identity_from_context(
        {"host": "codex", "thread_id": "ignored"},
        {"AGENT_FLOW_EXECUTION_ID": "explicit-session"},
    )
    environment_host = execution_identity_from_context(
        {},
        {
            "AGENT_FLOW_HOST": "codex",
            "AGENT_FLOW_EXECUTION_ID": "environment-session",
        },
    )

    assert codex == ExecutionIdentity("codex", "shared-session", "")
    assert claude == ExecutionIdentity("claude", "shared-session", "worker-1")
    assert omp == ExecutionIdentity("omp", "shared-session", "")
    assert override == ExecutionIdentity("codex", "explicit-session", "")
    assert environment_host == ExecutionIdentity("codex", "environment-session", "")


def test_unicode_execution_identity_digest_matches_node() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    execution = ExecutionIdentity("claude", "세션-十", "작업자-ß")
    payload = json.dumps(execution.to_dict(), ensure_ascii=False)
    result = subprocess.run(
        (
            node,
            "-e",
            (
                "const crypto=require('node:crypto');"
                "const e=JSON.parse(process.argv[1]);"
                "const canonical=JSON.stringify({agent_id:e.agent_id,host:e.host,session_id:e.session_id});"
                "process.stdout.write(crypto.createHash('sha256').update(canonical).digest('hex'));"
            ),
            payload,
        ),
        text=True,
        capture_output=True,
        check=True,
    )

    assert execution.digest == result.stdout


def test_no_active_run_allows_local_write_and_stale_binding_is_blocked(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, _worktree, _runtime, run_dir = pinned_run
    (run_dir / "active").unlink()

    local = _guard(leader, Path("shared.txt"), cwd=leader)

    assert local.returncode == 0, local.stderr
    with pytest.raises(Exception, match="execution_binding_stale"):
        resolve_execution_workspace(
            leader,
            ExecutionIdentity("codex", "session-1", ""),
        )


def test_follow_up_launched_from_leader_mutates_only_the_pinned_worktree(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    leader_hash = hashlib.sha256((leader / "shared.txt").read_bytes()).hexdigest()
    accepted = _guard(leader, worktree / "shared.txt")
    rejected = _guard(leader, leader / "shared.txt")

    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode == 2
    (worktree / "shared.txt").write_text("pinned\n", encoding="utf-8")
    assert hashlib.sha256((leader / "shared.txt").read_bytes()).hexdigest() == leader_hash
    assert (worktree / "shared.txt").read_text(encoding="utf-8") == "pinned\n"


def test_apply_patch_move_target_must_remain_in_pinned_worktree(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run

    accepted = _guard(
        leader,
        worktree / "shared.txt",
        move_to=worktree / "moved.txt",
    )
    rejected = _guard(
        leader,
        worktree / "shared.txt",
        move_to=leader / "moved.txt",
    )

    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode == 2
    assert str(leader / "moved.txt") in rejected.stderr


def test_sub_agent_absolute_leader_path_is_rejected(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, _worktree, _runtime, _run_dir = pinned_run
    result = _guard(leader, leader / "shared.txt", host="claude")

    assert result.returncode == 2
    assert str(leader / "shared.txt") in result.stderr
    assert "pinned_workspace_root" in result.stderr


def test_symlink_escape_from_pinned_worktree_is_rejected(
    pinned_run: tuple[Path, Path, Path, Path],
    tmp_path: Path,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "existing.txt").write_text("outside\n", encoding="utf-8")
    (worktree / "escape").symlink_to(outside, target_is_directory=True)

    result = _guard(leader, worktree / "escape" / "existing.txt")

    assert result.returncode == 2
    assert str((outside / "existing.txt").resolve()) in result.stderr


def test_missing_target_below_an_escaping_symlink_is_rejected(
    pinned_run: tuple[Path, Path, Path, Path],
    tmp_path: Path,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    outside = tmp_path / "outside"
    outside.mkdir()
    (worktree / "escape").symlink_to(outside, target_is_directory=True)

    result = _guard(leader, worktree / "escape" / "missing" / "new.txt")

    assert result.returncode == 2
    assert str((outside / "missing" / "new.txt").resolve(strict=False)) in result.stderr


def test_missing_pinned_worktree_fails_without_leader_fallback(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    shutil.rmtree(worktree)

    result = _guard(leader, leader / "shared.txt")

    assert result.returncode == 2
    assert "pinned workspace is missing" in result.stderr
    assert "fallback" not in result.stderr.lower()


@pytest.mark.parametrize("phase", ("fix-loop", "final-review", "pr-comment-fix"))
def test_fix_and_review_mutations_remain_in_the_original_pinned_worktree(
    pinned_run: tuple[Path, Path, Path, Path],
    phase: str,
) -> None:
    leader, worktree, _runtime, run_dir = pinned_run
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["current_phase"] = phase
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    accepted = _guard(leader, worktree / "shared.txt", phase=phase)
    rejected = _guard(leader, leader / "shared.txt", phase=phase)

    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode == 2


def test_codex_claude_and_omp_share_the_same_pinned_workspace_and_mutation_set(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    decisions = {}
    blocked_reasons = {}
    for host in ("codex", "claude", "omp"):
        allowed = _guard(leader, worktree / "shared.txt", host=host)
        blocked = _guard(leader, leader / "shared.txt", host=host)
        decisions[host] = (allowed.returncode, blocked.returncode)
        blocked_reasons[host] = "target_outside_pinned_workspace" in blocked.stderr

    assert decisions == {"codex": (0, 2), "claude": (0, 2), "omp": (0, 2)}
    assert blocked_reasons == {"codex": True, "claude": True, "omp": True}


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_shell_mutation_from_leader_is_rejected_for_every_host(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, _worktree, _runtime, _run_dir = pinned_run
    command = "printf changed > shared.txt"

    result = _bash_guard(leader, leader, command, host=host)

    assert result.returncode == 2
    assert "must run from pinned workspace" in result.stderr
    assert (leader / "shared.txt").read_text(encoding="utf-8") == "leader\n"


def test_shell_mutation_inside_pinned_worktree_is_allowed_and_leader_unchanged(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    command = "printf pinned > shared.txt"

    accepted = _bash_guard(leader, worktree, command)
    assert accepted.returncode == 0, accepted.stderr
    subprocess.run(command, cwd=worktree, shell=True, check=True)

    assert (leader / "shared.txt").read_text(encoding="utf-8") == "leader\n"
    assert (worktree / "shared.txt").read_text(encoding="utf-8") == "pinned"


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_shell_mutation_inside_pinned_worktree_subdirectory_is_allowed(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    nested = worktree / "packages" / "app"
    nested.mkdir(parents=True)

    result = _bash_guard(leader, nested, "touch generated.txt", host=host)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
@pytest.mark.parametrize(
    "command",
    (
        "sed -i.bak '/leader/d' shared.txt",
        "sed -Ei 's/leader/changed/' shared.txt",
        "sed -ni 's/leader/changed/' shared.txt",
        "perl -pi -e '/leader/ && s/leader/changed/' shared.txt",
        "perl -pi -e 's/a > /leader/a/' shared.txt",
    ),
)
def test_in_place_program_text_is_not_treated_as_a_mutation_path(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
    command: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run

    result = _bash_guard(leader, worktree, command, host=host)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
@pytest.mark.parametrize(
    "command",
    (
        "sed -Ei 's/leader/changed/' shared.txt",
        "sed -ni 's/leader/changed/' shared.txt",
    ),
)
def test_combined_sed_in_place_options_cannot_mutate_the_leader(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
    command: str,
) -> None:
    leader, _worktree, _runtime, _run_dir = pinned_run

    result = _bash_guard(leader, leader, command, host=host)

    assert result.returncode == 2
    assert "must run from pinned workspace" in result.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_newline_shell_chain_cannot_hide_leader_mutation(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, _worktree, _runtime, _run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"

    chained = _bash_guard(leader, leader, "true\ntouch shared.txt", host=host)
    launcher_suffix = _bash_guard(
        leader,
        leader,
        f"{launcher} status\ntouch shared.txt",
        host=host,
    )

    assert chained.returncode == 2
    assert launcher_suffix.returncode == 2
    assert (leader / "shared.txt").read_text(encoding="utf-8") == "leader\n"


def test_shell_absolute_leader_target_and_symlink_escape_are_rejected(
    pinned_run: tuple[Path, Path, Path, Path],
    tmp_path: Path,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    outside = tmp_path / "outside"
    outside.mkdir()
    (worktree / "escape").symlink_to(outside, target_is_directory=True)

    absolute = _bash_guard(leader, worktree, f"printf changed > {leader / 'shared.txt'}")
    escaped = _bash_guard(leader, worktree, "printf changed > escape/new.txt")

    assert absolute.returncode == 2
    assert escaped.returncode == 2
    assert not (outside / "new.txt").exists()


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_dynamic_shell_mutations_cannot_escape_the_pinned_worktree(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    commands = (
        f"TARGET={leader / 'shared.txt'} node -e \"require('fs').writeFileSync(process.env.TARGET, 'changed')\"",
        f"dd if=/dev/null of={leader / 'shared.txt'}",
        f"perl -pi -e 's/leader/changed/' {leader / 'shared.txt'}",
        f"rsync shared.txt {leader / 'shared.txt'}",
    )

    for command in commands:
        result = _bash_guard(leader, worktree, command, host=host)
        assert result.returncode == 2, (command, result.stderr)
    assert (leader / "shared.txt").read_text(encoding="utf-8") == "leader\n"


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_wrapped_and_computed_shell_targets_fail_closed(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    target_codes = ",".join(str(value) for value in str(leader / "shared.txt").encode())
    commands = (
        f"command cp shared.txt {leader / 'shared.txt'}",
        f"node -e \"require('fs').writeFileSync(String.fromCharCode({target_codes}), 'changed')\"",
        (
            "python3 -c \"from pathlib import Path; "
            f"Path(bytes([{target_codes}]).decode()).write_text('changed')\""
        ),
    )

    for command in commands:
        result = _bash_guard(leader, worktree, command, host=host)
        assert result.returncode == 2, (command, result.stderr)
    assert (leader / "shared.txt").read_text(encoding="utf-8") == "leader\n"


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_shell_substitutions_and_git_output_fail_closed(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    commands = (
        f"cat <(cp shared.txt {leader / 'shared.txt'})",
        f"cat $(touch {leader / 'shared.txt'})",
        f"git diff --output={leader / 'shared.txt'}",
    )

    for command in commands:
        result = _bash_guard(leader, worktree, command, host=host)
        assert result.returncode == 2, (command, result.stderr)
    assert (leader / "shared.txt").read_text(encoding="utf-8") == "leader\n"


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
@pytest.mark.parametrize(
    "command",
    (
        "npm test",
        "python3 -m pytest -q",
        f"{shlex.quote(sys.executable)} -m pytest -q",
        f'"{sys.executable}" -m pytest -q',
        "node --test tests/test_skill_source_runtime.mjs",
        "git add shared.txt",
        "git commit -m change",
        "git push origin HEAD",
    ),
)
def test_pathless_gate_and_git_commands_are_allowed_inside_pinned_worktree(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
    command: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run

    result = _bash_guard(leader, worktree, command, host=host)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_chained_read_paths_do_not_become_mutation_targets(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    leader_file = shlex.quote(str(leader / "shared.txt"))
    commands = (
        f"cp shared.txt local-copy.txt && cat {leader_file}",
        f"cat {leader_file} && cp shared.txt local-copy.txt",
        f"touch local.txt && git -C {shlex.quote(str(leader))} status --short",
    )

    for command in commands:
        result = _bash_guard(leader, worktree, command, host=host)
        assert result.returncode == 0, (command, result.stderr)


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_read_only_git_may_target_leader_from_pinned_worktree(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run

    result = _bash_guard(
        leader,
        worktree,
        f"git -C {shlex.quote(str(leader))} status --short",
        host=host,
    )

    assert result.returncode == 0, result.stderr


def test_git_command_targeting_leader_from_pinned_worktree_is_rejected(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run

    result = _bash_guard(leader, worktree, f"git -C {leader} add shared.txt")

    assert result.returncode == 2
    assert "target_outside_pinned_workspace" in result.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_chained_git_mutation_is_not_hidden_by_a_read_only_first_command(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    command = "git status --short && git add shared.txt"

    allowed = _bash_guard(leader, worktree, command, host=host)
    blocked = _bash_guard(leader, leader, command, host=host)

    assert allowed.returncode == 0, allowed.stderr
    assert blocked.returncode == 2
    assert "mutation_cwd_not_pinned" in blocked.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
@pytest.mark.parametrize("subcommand", ("status", "continue"))
def test_agent_flow_launcher_is_allowed_from_leader(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
    subcommand: str,
) -> None:
    leader, _worktree, _runtime, _run_dir = pinned_run
    command = f"{leader / '.agent-flow' / 'bin' / 'agent-flow'} {subcommand}"

    result = _bash_guard(leader, leader, command, host=host)

    assert result.returncode == 0, result.stderr


def test_claude_launcher_forwards_hook_identity_to_the_runner(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, _worktree, _runtime, _run_dir = pinned_run
    command = f"{leader / '.agent-flow' / 'bin' / 'agent-flow'} status"

    result = _bash_guard(
        leader,
        leader,
        command,
        host="claude",
        session_id="claude-session-'42",
        agent_id="subagent-7",
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    updated = output["hookSpecificOutput"]["updatedInput"]
    assert output["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert updated["command"].endswith(command)
    assert "AGENT_FLOW_ACTIVE_HOST=claude" in updated["command"]
    assert "AGENT_FLOW_EXECUTION_ID='claude-session-'\"'\"'42'" in updated["command"]
    assert "AGENT_FLOW_AGENT_ID=subagent-7" in updated["command"]


@pytest.mark.parametrize("host", ("claude", "omp"))
def test_host_launcher_blocks_when_stable_session_identity_is_missing(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, _worktree, _runtime, _run_dir = pinned_run
    command = f"{leader / '.agent-flow' / 'bin' / 'agent-flow'} status"

    result = _bash_guard(
        leader,
        leader,
        command,
        host=host,
        session_id="",
        env_override={"AGENT_FLOW_EXECUTION_ID": None},
    )

    assert result.returncode == 2
    assert "execution_identity_missing" in result.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_authenticated_sandboxed_gate_launcher_is_allowed(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"

    result = _bash_guard(leader, worktree, f"{launcher} gate -- npm test", host=host)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_path_shadowed_agent_flow_launcher_is_rejected(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    fake = worktree / "agent-flow"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)

    result = _bash_guard(leader, worktree, "PATH=. agent-flow status", host=host)

    assert result.returncode == 2


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
@pytest.mark.parametrize("variable", ("NODE_OPTIONS", "LD_PRELOAD"))
def test_environment_prefixed_agent_flow_launcher_is_rejected(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
    variable: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"

    result = _bash_guard(leader, worktree, f"{variable}=payload {launcher} status", host=host)

    assert result.returncode == 2


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_inherited_node_options_rejects_agent_flow_launcher(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"

    result = _bash_guard(
        leader,
        worktree,
        f"{launcher} status",
        host=host,
        env_override={"NODE_OPTIONS": "--require=/tmp/payload.js"},
    )

    assert result.returncode == 2


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_replaced_project_agent_flow_launcher_is_rejected(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"
    launcher.write_text("#!/bin/sh\ntouch ../escaped\n", encoding="utf-8")
    launcher.chmod(0o755)

    result = _bash_guard(leader, worktree, f"{launcher} status", host=host)

    assert result.returncode == 2


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_replaced_project_node_runtime_is_rejected(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"
    node_entry = leader / ".agent-flow" / "runtime" / "node" / "bin" / "agent-flow-kit.mjs"
    node_entry.write_text("process.exit(1);\n", encoding="utf-8")

    result = _bash_guard(leader, worktree, f"{launcher} status", host=host)

    assert result.returncode == 2


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_global_launcher_dependency_tamper_is_rejected(
    pinned_run: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    package = tmp_path / "global-package"
    shutil.copytree(leader / ".agent-flow" / "runtime" / "node", package)
    global_bin = tmp_path / "global-bin"
    global_bin.mkdir()
    launcher = global_bin / "agent-flow"
    launcher.symlink_to(package / "bin" / "agent-flow-kit.mjs")
    command = f"{launcher} status"
    clean_python_env = {
        name: None
        for name in ("PYTHON", "PYTHON_EXECUTABLE", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")
    }

    accepted = _bash_guard(
        leader,
        worktree,
        command,
        host=host,
        env_override=clean_python_env,
    )
    (package / "lib" / "skill-selection.mjs").write_text("throw new Error('tampered');\n", encoding="utf-8")
    rejected = _bash_guard(
        leader,
        worktree,
        command,
        host=host,
        env_override=clean_python_env,
    )

    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode == 2


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_global_launcher_with_path_shadowed_node_is_rejected(
    pinned_run: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    package = tmp_path / "global-package"
    shutil.copytree(leader / ".agent-flow" / "runtime" / "node", package)
    global_bin = tmp_path / "global-bin"
    global_bin.mkdir()
    (global_bin / "agent-flow").symlink_to(package / "bin" / "agent-flow-kit.mjs")
    fake_node = global_bin / "node"
    fake_node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_node.chmod(0o755)
    clean_env: dict[str, str | None] = {
        name: None
        for name in ("PYTHON", "PYTHON_EXECUTABLE", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")
    }
    clean_env["PATH"] = f"{global_bin}{os.pathsep}/usr/bin:/bin"

    result = _bash_guard(
        leader,
        worktree,
        "agent-flow status",
        host=host,
        env_override=clean_env,
    )

    assert result.returncode == 2


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_agent_flow_install_alias_is_not_a_runtime_launcher(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"

    result = _bash_guard(leader, worktree, f"{launcher} run install", host=host)

    assert result.returncode == 2


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_unclassified_shell_command_fails_closed_from_the_leader(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, _worktree, _runtime, _run_dir = pinned_run

    result = _bash_guard(leader, leader, "custom-writer --output shared.txt", host=host)
    read_only = _bash_guard(leader, leader, "git status --short", host=host)

    assert result.returncode == 2
    assert "must run from pinned workspace" in result.stderr
    assert read_only.returncode == 0, read_only.stderr


def test_leader_status_without_execution_identity_is_rejected(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, _worktree, _runtime, _run_dir = pinned_run
    env = dict(os.environ)
    env["PYTHONPATH"] = str(KIT_ROOT / "src")
    for name in (
        "AGENT_FLOW_EXECUTION_ID",
        "AGENT_FLOW_SESSION_ID",
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "OMP_SESSION_ID",
    ):
        env.pop(name, None)

    result = subprocess.run(
        (sys.executable, "-m", "agent_flow.cli", "status", "--root", str(leader)),
        cwd=leader,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "execution_identity_missing" in result.stderr


def test_worktree_manifest_records_canonical_identity(tmp_path: Path) -> None:
    leader = tmp_path / "project"
    leader.mkdir()
    subprocess.run(("git", "init", "-b", "main", str(leader)), check=True, capture_output=True)
    _git(leader, "config", "user.name", "Test User")
    _git(leader, "config", "user.email", "test@example.com")
    (leader / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(leader, "add", "tracked.txt")
    _git(leader, "commit", "-m", "initial")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(KIT_ROOT / "src")

    result = subprocess.run(
        (
            sys.executable,
            "-m",
            "agent_flow.cli",
            "worktree",
            "create",
            "--root",
            str(leader),
            "--name",
            "identity",
        ),
        cwd=leader,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads(
        (leader / ".git" / "agent-flow" / "worktrees" / "feat-identity" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    identity = manifest["identity"]
    assert Path(identity["workspace_root"]).is_absolute()
    assert Path(identity["workspace_root"]).samefile(leader / ".agent-flow" / "worktrees" / "feat-identity")
    assert Path(identity["git_common_dir"]).samefile(leader / ".git")
    assert identity["branch"] == "feat/identity"
    assert identity["head"] == _git(leader, "rev-parse", "HEAD")


def test_runner_fails_when_leader_changes_during_a_mutation_phase(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, runtime, run_dir = pinned_run
    runner = Runner(
        worktree,
        state_root=runtime,
        config_root=leader,
        run_dir=run_dir,
    )
    runner._pin_workspace_identity()
    runner._begin_mutation_boundary(Phase(id="implement", description="implement"))

    (leader / "shared.txt").write_text("unexpected leader mutation\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="leader checkout changed.*shared.txt"):
        runner._observe_mutation_boundary(clear=True)
    assert (leader / "shared.txt").read_text(encoding="utf-8") == "unexpected leader mutation\n"


def test_runner_records_only_pinned_changes_during_a_mutation_phase(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, runtime, run_dir = pinned_run
    runner = Runner(
        worktree,
        state_root=runtime,
        config_root=leader,
        run_dir=run_dir,
    )
    runner._pin_workspace_identity()
    runner._begin_mutation_boundary(Phase(id="fix-loop", description="fix"))

    (worktree / "shared.txt").write_text("pinned mutation\n", encoding="utf-8")
    runner._observe_mutation_boundary(clear=True)

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["pinned_mutation_paths"] == ["shared.txt"]
    assert "mutation_boundary" not in meta
    assert (leader / "shared.txt").read_text(encoding="utf-8") == "leader\n"


def test_node_follow_up_from_leader_reuses_and_validates_registered_worktree(
    tmp_path: Path,
) -> None:
    leader = tmp_path / "project"
    leader.mkdir()
    subprocess.run(("git", "init", "-b", "main", str(leader)), check=True, capture_output=True)
    _git(leader, "config", "user.name", "Test User")
    _git(leader, "config", "user.email", "test@example.com")
    (leader / "shared.txt").write_text("leader\n", encoding="utf-8")
    _git(leader, "add", "shared.txt")
    _git(leader, "commit", "-m", "initial")
    node = shutil.which("node")
    assert node is not None
    cli = KIT_ROOT / "bin" / "agent-flow-kit.mjs"
    env = {
        **os.environ,
        "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0",
        "AGENT_FLOW_ACTIVE_HOST": "codex",
        "AGENT_FLOW_EXECUTION_ID": "session-1",
    }
    installed = subprocess.run(
        (node, str(cli), "install"),
        cwd=leader,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr

    worktree = leader / ".agent-flow" / "worktrees" / "feat-node"
    _git(leader, "worktree", "add", "-b", "feat/node", str(worktree), "main")
    runtime = leader / ".git" / "agent-flow" / "worktrees" / "feat-node"
    runtime.mkdir(parents=True)
    identity = _identity(worktree)
    (runtime / "manifest.json").write_text(
        json.dumps(
            {
                "name": "feat-node",
                "branch": "feat/node",
                "path": ".agent-flow/worktrees/feat-node",
                "identity": identity,
            }
        ),
        encoding="utf-8",
    )

    started = subprocess.run(
        (node, str(cli), "run", "start", "--task", "node pinned", "--run-id", "node-1"),
        cwd=worktree,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert started.returncode == 0, started.stderr
    state_paths = list((leader / ".git" / "agent-flow" / "current-runs").glob("*.json"))
    assert len(state_paths) == 1
    state_path = state_paths[0]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert Path(state["workspace_root"]).samefile(worktree)
    assert state["workspace"] == identity
    active = find_active_pinned_workspaces(leader)
    assert len(active) == 1
    assert active[0].run_dir.samefile(leader / state["run_dir"])

    follow_up = subprocess.run(
        (node, str(cli), "run", "status"),
        cwd=leader,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert follow_up.returncode == 0, follow_up.stderr
    assert f"workspace_root: {worktree.resolve()}" in follow_up.stdout
    assert _guard(leader, worktree / "shared.txt").returncode == 0
    assert _guard(leader, leader / "shared.txt").returncode == 2

    binding_path = next((leader / ".git" / "agent-flow" / "executions").glob("*.json"))
    binding_path.unlink()
    lost_binding_write = _bash_guard(leader, leader, "touch shared.txt")
    assert lost_binding_write.returncode == 2
    assert "execution_binding_missing" in lost_binding_write.stderr
    replacement = subprocess.run(
        (node, str(cli), "run", "start", "--task", "replacement", "--run-id", "node-2"),
        cwd=worktree,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert replacement.returncode == 1
    assert "execution_binding_missing" in replacement.stderr
    assert json.loads(state_path.read_text(encoding="utf-8"))["run_id"] == "node-1"
    assert not (leader / ".agent-flow" / "runs" / "full-feature" / "node-2").exists()

    shutil.rmtree(worktree)
    missing = subprocess.run(
        (node, str(cli), "run", "status"),
        cwd=leader,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert missing.returncode == 1
    assert "pinned workspace is missing" in missing.stderr


def test_node_run_start_rejects_the_leader_protected_branch(tmp_path: Path) -> None:
    leader = tmp_path / "project"
    leader.mkdir()
    subprocess.run(("git", "init", "-b", "main", str(leader)), check=True, capture_output=True)
    _git(leader, "config", "user.name", "Test User")
    _git(leader, "config", "user.email", "test@example.com")
    (leader / "shared.txt").write_text("leader\n", encoding="utf-8")
    _git(leader, "add", "shared.txt")
    _git(leader, "commit", "-m", "initial")
    node = shutil.which("node")
    assert node is not None
    cli = KIT_ROOT / "bin" / "agent-flow-kit.mjs"
    env = {**os.environ, "AGENT_FLOW_AUTO_EXTERNAL_SKILLS": "0"}
    installed = subprocess.run(
        (node, str(cli), "install"),
        cwd=leader,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr

    started = subprocess.run(
        (node, str(cli), "run", "start", "--task", "blocked leader"),
        cwd=leader,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert started.returncode == 1
    assert "protected branch main" in started.stderr
    assert not (leader / ".agent-flow" / "state" / "current-run.json").exists()
