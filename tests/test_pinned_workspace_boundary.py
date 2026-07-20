from __future__ import annotations

import hashlib
import json
import os
import py_compile
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import agent_flow.core.workspace_boundary as workspace_boundary
from agent_flow.runner import Phase, Runner
from agent_flow.core.workspace_boundary import (
    ExecutionIdentity,
    acquire_workspace_start_claim,
    authenticated_git_private_directory,
    bind_execution_to_workspace,
    capture_workspace_identity,
    execution_identity_from_context,
    find_active_pinned_workspaces,
    resolve_execution_workspace,
    resolve_mutation_path,
    release_workspace_start_claim,
    record_workspace_finalizer,
    select_execution_workspace,
)


KIT_ROOT = Path(__file__).resolve().parent.parent
GUARD = KIT_ROOT / "scripts" / "hooks" / "guard-worktree-write.py"


def test_git_private_metadata_rejects_symlinked_common_root(tmp_path: Path) -> None:
    real_common = tmp_path / "real-common"
    real_common.mkdir()
    linked_common = tmp_path / "linked-common"
    linked_common.symlink_to(real_common, target_is_directory=True)

    with pytest.raises(workspace_boundary.WorkspaceBoundaryError):
        authenticated_git_private_directory(
            linked_common,
            "agent-flow",
            create=True,
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS system alias")
def test_git_private_metadata_accepts_macos_var_alias(tmp_path: Path) -> None:
    canonical = tmp_path.resolve()
    private_var = Path("/private/var")
    try:
        relative = canonical.relative_to(private_var)
    except ValueError:
        pytest.skip("temporary directory is not under /private/var")
    common = canonical / "common"
    common.mkdir()
    alias = Path("/var") / relative / "common"

    private = authenticated_git_private_directory(alias, "agent-flow", create=True)

    assert private == common / "agent-flow"


def test_git_private_metadata_rejects_dangling_common_root(tmp_path: Path) -> None:
    linked_common = tmp_path / "linked-common"
    linked_common.symlink_to(tmp_path / "missing-common", target_is_directory=True)

    with pytest.raises(workspace_boundary.WorkspaceBoundaryError):
        authenticated_git_private_directory(linked_common, "agent-flow", create=True)


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
        json.dumps(contract["node"].get("dependencies", []), separators=(",", ":"), sort_keys=True),
        contract["git"]["path"],
        contract["git"]["sha256"],
        contract["git"]["device"],
        contract["git"]["inode"],
        contract["git"]["links"],
        contract["git"]["mode"],
        json.dumps(contract["git"].get("dependencies", []), separators=(",", ":"), sort_keys=True),
        contract["python"]["path"],
        contract["python"]["resolved_path"],
        contract["python"]["sha256"],
        contract["python"]["device"],
        contract["python"]["inode"],
        contract["python"]["links"],
        contract["python"]["mode"],
        json.dumps(contract["python"].get("dependencies", []), separators=(",", ":"), sort_keys=True),
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


def _executable_dependencies(path: Path) -> list[dict[str, object]]:
    resolved = path.resolve()
    library_root = resolved.parent.parent / "lib"
    if not library_root.is_dir():
        return []
    return [
        {
            "name": dependency.resolve().name,
            "path": str(dependency.resolve()),
            **_executable_identity(dependency),
        }
        for dependency in sorted(library_root.iterdir(), key=lambda item: item.name)
        if (
            (dependency.name.startswith("libpython") or dependency.name.startswith("libnode"))
            and dependency.name.endswith(".dylib")
        )
    ]


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
    leader = tmp_path / "feat-project"
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
            "dependencies": _executable_dependencies(Path(node or sys.executable)),
        },
        "git": {
            "path": str(Path("/usr/bin/git").resolve()),
            **_executable_identity(Path("/usr/bin/git")),
            "dependencies": _executable_dependencies(Path("/usr/bin/git")),
        },
        "python": {
            "path": str(Path(sys.executable).absolute()),
            "resolved_path": str(Path(sys.executable).resolve()),
            **_executable_identity(Path(sys.executable)),
            "dependencies": _executable_dependencies(Path(sys.executable)),
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
    patch: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(KIT_ROOT / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for name in (
        "GRADLE_USER_HOME",
        "GRADLE_OPTS",
        "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS",
        "JAVA_OPTS",
    ):
        env.pop(name, None)
    move_line = f"\n*** Move to: {move_to}" if move_to is not None else ""
    rendered_patch = patch or (
        f"*** Begin Patch\n*** Update File: {target}{move_line}\n@@\n-old\n+new\n*** End Patch"
    )
    payload = {
        "tool_name": "apply_patch",
        "cwd": str(cwd or leader),
        "host": host,
        "session_id": session_id,
        "phase": phase,
        "tool_input": {"patch": rendered_patch},
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
    tool_cwd: Path | None = None,
    env_override: dict[str, str | None] | None = None,
    guard: Path = GUARD,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(KIT_ROOT / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for name in (
        "GRADLE_USER_HOME",
        "GRADLE_OPTS",
        "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS",
        "JAVA_OPTS",
    ):
        env.pop(name, None)
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
        "tool_input": (
            {"command": command}
            if tool_cwd is None
            else {"command": command, "cwd": str(tool_cwd)}
        ),
    }
    return subprocess.run(
        (sys.executable, str(guard)),
        cwd=leader,
        env=env,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


def _structured_guard(
    leader: Path,
    worktree: Path,
    tool_name: str,
    tool_input: dict[str, object],
    *,
    host: str,
    phase: str = "implement",
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(KIT_ROOT / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    payload = {
        "tool_name": tool_name,
        "cwd": str(worktree),
        "host": host,
        "session_id": "session-1",
        "phase": phase,
        "tool_input": tool_input,
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


@pytest.mark.parametrize("active_count", (9, 10, 11))
def test_parallel_execution_threshold_resolves_only_owned_worktrees(
    tmp_path: Path,
    active_count: int,
) -> None:
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
    for index in range(active_count):
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


def test_workspace_start_claim_recovers_reused_pid_but_not_live_owner(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    _leader, worktree, _runtime, _run_dir = pinned_run
    identity = capture_workspace_identity(worktree)
    original = acquire_workspace_start_claim(identity, run_id="old-run")
    payload = json.loads(original.path.read_text(encoding="utf-8"))
    release_workspace_start_claim(original)
    payload["process_start_id"] = "reused-pid-start-identity"
    original.path.write_text(json.dumps(payload), encoding="utf-8")

    recovered = acquire_workspace_start_claim(identity, run_id="new-run")
    owner = json.loads(recovered.path.read_text(encoding="utf-8"))

    assert owner["version"] == 2
    assert owner["run_id"] == "new-run"
    assert owner["leader_root"] == str(Path(identity.git_common_dir).resolve().parent)
    assert owner["workspace_root"] == identity.workspace_root
    with pytest.raises(Exception, match="already in progress"):
        acquire_workspace_start_claim(identity, run_id="blocked-run")
    release_workspace_start_claim(recovered)


def test_worktree_lifecycle_claim_recovers_after_leader_head_changes(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, _worktree, _runtime, _run_dir = pinned_run
    identity = capture_workspace_identity(leader)
    original = acquire_workspace_start_claim(
        identity,
        run_id="worktree-lifecycle",
    )
    payload = json.loads(original.path.read_text(encoding="utf-8"))
    release_workspace_start_claim(original)
    payload["process_start_id"] = "crashed-process-start"
    original.path.write_text(json.dumps(payload), encoding="utf-8")
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "leader moved"],
        cwd=leader,
        check=True,
        capture_output=True,
    )

    recovered = acquire_workspace_start_claim(
        capture_workspace_identity(leader),
        run_id="worktree-lifecycle",
    )

    release_workspace_start_claim(recovered)


def test_workspace_start_claim_authenticates_process_before_publication(
    pinned_run: tuple[Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _leader, worktree, _runtime, _run_dir = pinned_run
    identity = capture_workspace_identity(worktree)
    digest = hashlib.sha256(identity.workspace_root.encode("utf-8")).hexdigest()
    claim_path = (
        Path(identity.git_common_dir)
        / "agent-flow"
        / "workspace-start-claims"
        / f"{digest}.lock"
    )

    def process_start_identity(_pid: int) -> str:
        assert not claim_path.exists()
        return "authenticated-process-start"

    monkeypatch.setattr(
        workspace_boundary,
        "_process_start_identity",
        process_start_identity,
    )

    claim = acquire_workspace_start_claim(identity, run_id="ordered-run")

    release_workspace_start_claim(claim)


def test_execution_binding_publication_is_atomic_across_worktrees(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, _runtime, run_dir = pinned_run
    second = leader / ".agent-flow" / "worktrees" / "feat-second"
    _git(leader, "worktree", "add", "-b", "feat/second", str(second), "main")
    second_runtime = leader / ".git" / "agent-flow" / "worktrees" / "feat-second"
    second_run = second_runtime / ".agent-flow" / "runs" / "run-2"
    second_run.mkdir(parents=True)
    (second_run / "active").write_text("", encoding="utf-8")
    first_identity = capture_workspace_identity(worktree)
    second_identity = capture_workspace_identity(second)
    execution = ExecutionIdentity("codex", "atomic-execution", "")

    def bind(candidate: tuple[object, Path, str]) -> str:
        identity, candidate_run, run_id = candidate
        try:
            bind_execution_to_workspace(
                execution,
                identity,
                candidate_run,
                run_id=run_id,
            )
        except Exception as exc:
            return str(exc)
        return "bound"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                bind,
                (
                    (first_identity, run_dir, "run-1"),
                    (second_identity, second_run, "run-2"),
                ),
            )
        )

    assert results.count("bound") == 1
    assert sum("execution_binding_conflict" in result for result in results) == 1


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


def test_stale_binding_without_active_run_blocks_local_write(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, _worktree, _runtime, run_dir = pinned_run
    (run_dir / "active").unlink()

    local = _guard(leader, Path("shared.txt"), cwd=leader)

    assert local.returncode == 2
    assert "bound_run_not_active" in local.stderr
    with pytest.raises(Exception, match="execution_binding_stale"):
        resolve_execution_workspace(
            leader,
            ExecutionIdentity("codex", "session-1", ""),
        )


def test_no_binding_and_no_active_run_allows_local_write(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, _worktree, _runtime, run_dir = pinned_run
    (run_dir / "active").unlink()
    for binding in (leader / ".git" / "agent-flow" / "executions").glob("*.json"):
        binding.unlink()

    local = _guard(leader, Path("shared.txt"), cwd=leader)

    assert local.returncode == 0, local.stderr


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
@pytest.mark.parametrize("relative", ("AGENTS.md", "CLAUDE.md", ".gitignore", "docs/AGENTS.md"))
def test_context_and_ignore_files_are_editable_inside_pinned_worktree(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
    relative: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    target = worktree / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old\n", encoding="utf-8")

    result = _guard(leader, target, host=host, cwd=worktree)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
@pytest.mark.parametrize(
    "relative",
    ("AGENTS.md", "agents.md", "Claude.MD", ".gitignore", ".GITIGNORE"),
)
def test_context_file_marker_block_is_immutable_but_user_region_is_editable(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
    relative: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    target = worktree / relative
    target.write_text(
        "old\n<!-- agent-flow:start -->\nmanaged\n<!-- agent-flow:end -->\nuser tail\n",
        encoding="utf-8",
    )
    outside_patch = (
        f"*** Begin Patch\n*** Update File: {target}\n@@\n-old\n+new\n*** End Patch"
    )
    managed_patch = (
        f"*** Begin Patch\n*** Update File: {target}\n@@\n-managed\n+changed\n*** End Patch"
    )

    allowed = _guard(leader, target, host=host, cwd=worktree, patch=outside_patch)
    blocked = _guard(leader, target, host=host, cwd=worktree, patch=managed_patch)

    assert allowed.returncode == 0, allowed.stderr
    assert blocked.returncode == 2
    assert "managed marker block" in blocked.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
@pytest.mark.parametrize("tool_name", ("Write", "Edit", "MultiEdit"))
def test_structured_context_edits_preserve_managed_marker_block(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
    tool_name: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    target = worktree / "docs" / "CLAUDE.md"
    target.parent.mkdir()
    current = (
        "old\n<!-- agent-flow:start -->\nmanaged\n<!-- agent-flow:end -->\nuser tail\n"
    )
    target.write_text(current, encoding="utf-8")
    allowed_inputs: dict[str, dict[str, object]] = {
        "Write": {"file_path": str(target), "content": current.replace("old", "new", 1)},
        "Edit": {
            "file_path": str(target),
            "old_string": "old\n",
            "new_string": "new\n",
        },
        "MultiEdit": {
            "file_path": str(target),
            "edits": [
                {"old_string": "old\n", "new_string": "new\n"},
                {"old_string": "user tail\n", "new_string": "updated tail\n"},
            ],
        },
    }
    blocked_inputs: dict[str, dict[str, object]] = {
        "Write": {"file_path": str(target), "content": current.replace("managed", "changed")},
        "Edit": {
            "file_path": str(target),
            "old_string": "managed\n",
            "new_string": "changed\n",
        },
        "MultiEdit": {
            "file_path": str(target),
            "edits": [
                {"old_string": "old\n", "new_string": "new\n"},
                {"old_string": "managed\n", "new_string": "changed\n"},
            ],
        },
    }

    allowed = _structured_guard(
        leader,
        worktree,
        tool_name,
        allowed_inputs[tool_name],
        host=host,
    )
    blocked = _structured_guard(
        leader,
        worktree,
        tool_name,
        blocked_inputs[tool_name],
        host=host,
    )

    assert allowed.returncode == 0, allowed.stderr
    assert blocked.returncode == 2
    assert "managed marker block" in blocked.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_multi_file_edit_checks_only_edits_for_each_managed_context_file(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    target = worktree / "docs" / "CLAUDE.md"
    other = worktree / "docs" / "notes.txt"
    target.parent.mkdir()
    target.write_text(
        "old\n<!-- agent-flow:start -->\nmanaged\n<!-- agent-flow:end -->\n",
        encoding="utf-8",
    )
    other.write_text("other\n", encoding="utf-8")

    result = _structured_guard(
        leader,
        worktree,
        "MultiEdit",
        {
            "edits": [
                {
                    "file_path": str(target),
                    "old_string": "old\n",
                    "new_string": "new\n",
                },
                {
                    "file_path": str(other),
                    "old_string": "other\n",
                    "new_string": "updated\n",
                },
            ]
        },
        host=host,
    )

    assert result.returncode == 0, result.stderr


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


def test_xargs_stdin_mutation_target_fails_closed(
    pinned_run: tuple[Path, Path, Path, Path],
    tmp_path: Path,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")

    result = _bash_guard(
        leader,
        worktree,
        f"printf '%s\\0' {shlex.quote(str(outside))} | xargs -0 rm -f",
    )

    assert result.returncode == 2
    assert "unresolved mutation target" in result.stderr
    assert outside.read_text(encoding="utf-8") == "keep\n"


def test_python_finalizer_retry_reuses_same_generation(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    _leader, worktree, runtime, run_dir = pinned_run
    identity = capture_workspace_identity(worktree)
    execution = ExecutionIdentity("codex", "python-finalizer", "")

    first = record_workspace_finalizer(
        identity,
        execution,
        run_dir,
        run_id="run-1",
        completed_at="2026-07-16T00:00:00+00:00",
    )
    second = record_workspace_finalizer(
        identity,
        execution,
        run_dir,
        run_id="run-1",
        completed_at="2026-07-16T00:00:01+00:00",
    )

    assert first == second == runtime / "finalizer.json"
    finalizer = json.loads(second.read_text(encoding="utf-8"))
    assert finalizer["generation"] == 1
    assert finalizer["completed_at"] == "2026-07-16T00:00:00+00:00"


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_shell_cwd_transition_cannot_escape_the_pinned_worktree(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    commands = (
        "cd .. && touch leaked",
        "cd>log .. && touch leaked",
        "cd ..>log && touch leaked",
        "command cd .. && printf changed > leaked",
        "command -p cd .. && touch leaked",
        "builtin cd .. && git add leaked",
        "builtin -- cd .. && touch leaked",
    )

    for command in commands:
        result = _bash_guard(leader, worktree, command, host=host)
        assert result.returncode == 2, (command, result.stderr)
        assert "target_outside_pinned_workspace" in result.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_shell_cwd_transition_inside_the_pinned_worktree_is_allowed(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    (worktree / "packages" / "app").mkdir(parents=True)
    commands = (
        "cd packages && touch generated.txt",
        "cd packages && cd app && touch generated.txt",
        "cd packages; touch generated.txt",
        "true || cd .. && touch generated.txt",
        "false && cd .. || touch generated.txt",
    )

    for command in commands:
        result = _bash_guard(leader, worktree, command, host=host)
        assert result.returncode == 0, (command, result.stderr)


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_conditional_shell_cwd_states_cannot_hide_an_escape(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    pinned = shlex.quote(str(worktree))
    commands = (
        f"cd .. || cd {pinned} && touch leaked",
        f"builtin cd .. || cd {pinned} && printf changed > leaked",
        f"command cd .. || cd {pinned} && git add leaked",
    )

    for command in commands:
        result = _bash_guard(leader, worktree, command, host=host)
        assert result.returncode == 2, (command, result.stderr)
        assert "target_outside_pinned_workspace" in result.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_compound_nested_and_directory_stack_shell_escapes_are_rejected(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    (leader / "packages").mkdir()
    leader_cdpath = shlex.quote(str(leader))
    fake_true = worktree / "true"
    fake_true.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_true.chmod(0o755)
    commands = (
        "{ cd ..; touch leaked; }",
        "(cd ..; touch leaked)",
        "if cd ..; then touch leaked; fi",
        "if cd ..; then sed -i '' -e 's/a/b/' shared.txt; fi",
        "if cd ..; then git diff --output=leaked; fi",
        f"if true; then git diff --output={leader / 'leaked'}; fi",
        f"if true; then git -C{leader} add shared.txt; fi",
        "case x in x) cd ..;; esac; touch leaked",
        "case x in x) cd>log ..;; esac; touch leaked",
        "case x in x) env -C .. touch leaked;; esac",
        "case x in x) sh -c 'cd ..; touch leaked';; esac",
        f"case x in x) git diff --output={leader / 'leaked'};; esac",
        "f() { cd ..; }; f; touch leaked",
        "f() { env -C .. touch leaked; }; f",
        "f() { sh -c 'cd ..; touch leaked'; }; f",
        "function f { cd ..; }; f; touch leaked",
        "sh -c 'cd .. && touch leaked'",
        "bash -c 'cd .. && touch leaked'",
        "bash -O extglob -c 'cd .. && touch leaked'",
        "bash -o posix -c 'cd .. && touch leaked'",
        "bash +o posix -c 'cd .. && touch leaked'",
        "exec sh -c 'cd .. && touch leaked'",
        "exec -- sh -c 'cd .. && touch leaked'",
        "exec -a worker sh -c 'cd .. && touch leaked'",
        "eval 'cd ..; touch leaked'",
        "eval -- 'cd ..; touch leaked'",
        f"env CDPATH={leader_cdpath} sh -c 'cd packages && touch leaked'",
        f"sh -c 'CDPATH={leader_cdpath} :; cd packages && touch leaked'",
        (
            f"bash --posix -c 'CDPATH={leader_cdpath} :; "
            "cd packages && touch leaked'"
        ),
        (
            f"sh -c 'CDPATH={leader_cdpath} export FOO=1; "
            "cd packages && touch leaked'"
        ),
        (
            f"sh -c 'CDPATH={leader_cdpath} readonly FOO=1; "
            "cd packages && touch leaked'"
        ),
        (
            f"sh -c \"CDPATH={leader_cdpath} set -- value; "
            "cd packages && touch leaked\""
        ),
        (
            f"sh -c \"CDPATH={leader_cdpath} eval ':'; "
            "cd packages && touch leaked\""
        ),
        "env -C .. touch leaked",
        "env -P /bin sh -c 'cd .. && touch leaked'",
        "env -S \"sh -c 'cd .. && touch leaked'\"",
        "env -S 'FOO=bar' sh -c 'cd .. && touch leaked'",
        "env -S '-i' sh -c 'cd .. && touch leaked'",
        (
            f"env -S \"CDPATH={leader_cdpath} "
            "sh -c 'cd packages && touch leaked'\""
        ),
        f"readonly CDPATH={leader_cdpath}; cd packages && touch leaked",
        f"declare CDPATH={leader_cdpath}; cd packages && touch leaked",
        f"CDPATH={leader_cdpath} pushd packages && touch leaked",
        f"CDPATH={leader_cdpath}; pushd packages && touch leaked",
        f"CDPATH={leader_cdpath}; unset -f CDPATH; cd packages && touch leaked",
        "export 1BAD || cd ..; touch leaked",
        "unset -z CDPATH || cd ..; touch leaked",
        (
            f"CDPATH={leader_cdpath}; readonly CDPATH; unset CDPATH; "
            "cd packages && touch leaked"
        ),
        "! cd .. || touch leaked",
        f"! false && touch {leader / 'leaked'}",
        f"! true || touch {leader / 'leaked'}",
        f"! false && git -C{leader} add shared.txt",
        f"set -o pipefail; false | true || touch {leader / 'leaked'}",
        (
            "set -euo pipefail; false | true || "
            f"/usr/bin/touch {leader / 'leaked'}"
        ),
        (
            "set -o pipefail; false | true || "
            f"custom-writer {leader / 'leaked'}"
        ),
        (
            "bash -o pipefail -c 'false | true || "
            f"touch {leader / 'leaked'}'"
        ),
        (
            "bash -eo pipefail -c 'false | true || "
            f"touch {leader / 'leaked'}'"
        ),
        "time cd .. && touch leaked",
        "true > . || cd ..; touch leaked",
        "./true || cd ..; touch leaked",
        "TRUE || cd ..; touch leaked",
        "chdir .. && touch leaked",
        "pushd .. && touch leaked",
        "command pushd .. && printf changed > leaked",
        "builtin pushd .. && git add leaked",
        "source ./move-out.sh && touch leaked",
        ". ./move-out.sh && touch leaked",
    )

    for command in commands:
        result = _bash_guard(leader, worktree, command, host=host)
        assert result.returncode == 2, (command, result.stderr)


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_cd_redirection_and_cdpath_cannot_escape_the_pinned_worktree(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    (leader / "packages").mkdir()
    (worktree / "packages").mkdir()
    leader_target = shlex.quote(str(leader / "leaked"))
    leader_cdpath = shlex.quote(str(leader))
    commands = (
        f"cd packages > {leader_target}",
        f"printf changed >|{leader_target}",
        f"CDPATH={leader_cdpath} cd packages && touch leaked",
        f"CDPATH={leader_cdpath}; cd packages && touch leaked",
    )

    for command in commands:
        result = _bash_guard(leader, worktree, command, host=host)
        assert result.returncode == 2, (command, result.stderr)


def test_shell_cwd_state_space_is_bounded(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    transitions = "; ".join(f"cd d{index}" for index in range(20))

    read_only = _bash_guard(leader, worktree, transitions)
    mutating = _bash_guard(leader, worktree, f"{transitions}; touch generated.txt")

    assert read_only.returncode == 0, read_only.stderr
    assert mutating.returncode == 2
    assert "unresolved mutation target" in mutating.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_nested_shell_mutation_inside_the_pinned_worktree_is_allowed(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    (worktree / "packages").mkdir()

    commands = (
        "sh -c 'cd packages && touch generated.txt'",
        "{ cd packages; touch generated.txt; }",
        "(cd packages; touch generated.txt)",
        "if true; then touch generated.txt; fi",
        "if true; then git diff --output=generated.diff; fi",
        "set -o pipefail; false | true || git status",
        "bash -o pipefail -c 'false | true || pwd'",
        "case x in x) true;; esac",
        "f() { true; }; f",
        "function f { git status; }; f",
    )

    for command in commands:
        result = _bash_guard(leader, worktree, command, host=host)
        assert result.returncode == 0, (command, result.stderr)


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
    escaped_cwd = _bash_guard(leader, worktree, "cd escape && touch new.txt")

    assert absolute.returncode == 2
    assert escaped.returncode == 2
    assert escaped_cwd.returncode == 2
    assert not (outside / "new.txt").exists()


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_dynamic_shell_mutations_cannot_escape_the_pinned_worktree(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    commands = (
        f"TARGET={leader / 'shared.txt'} node -e \"require('fs').writeFileSync(process.env.TARGET, 'changed')\"",
        "printf changed > \"$TARGET\"",
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
    line_continuation = "\\" + "\n"
    commands = (
        f"cat <(cp shared.txt {leader / 'shared.txt'})",
        f"cat $(touch {leader / 'shared.txt'})",
        f"cat `touch {leader / 'shared.txt'}`",
        f"cat \\\\$(cp shared.txt {leader / 'shared.txt'})",
        f"cat ${line_continuation}(touch {leader / 'shared.txt'})",
        f"cat <{line_continuation}(touch {leader / 'shared.txt'})",
        f"cat =(touch {leader / 'shared.txt'})",
        f"git diff --output={leader / 'shared.txt'}",
    )

    for command in commands:
        result = _bash_guard(leader, worktree, command, host=host)
        assert result.returncode == 2, (command, result.stderr)
    assert (leader / "shared.txt").read_text(encoding="utf-8") == "leader\n"


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_shell_quoted_substitution_literals_are_allowed(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    commands = (
        "VALUE='$(literal)'",
        "printf '%s\\n' '<(literal)'",
        'printf \'%s\\n\' ">(literal)"',
        "printf '%s\\n' '`literal`'",
        'printf \'%s\\n\' "*(e:\'literal\':)"',
    )

    for command in commands:
        result = _bash_guard(leader, worktree, command, host=host)
        assert result.returncode == 0, (command, result.stderr)


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_gradle_wrapper_tasks_and_local_outputs_are_allowed(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    wrapper = worktree / "gradlew"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)
    commands = (
        "./gradlew assembleSandbox",
        "./gradlew bundleRelease",
        "./gradlew testDebugUnitTest",
        "./gradlew lint check",
        "./gradlew connectedDebugAndroidTest",
        "./gradlew --gradle-user-home .gradle-local assembleDebug",
        "GRADLE_USER_HOME=.gradle-local ./gradlew check",
    )

    for command in commands:
        result = _bash_guard(leader, worktree, command, host=host)
        assert result.returncode == 0, (command, result.stderr)


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_gradle_wrapper_external_paths_and_symlink_escape_are_rejected(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    wrapper = worktree / "gradlew"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)
    outside_wrapper = leader / "external-tools" / "gradlew"
    outside_wrapper.parent.mkdir()
    outside_wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    outside_wrapper.chmod(0o755)
    linked_wrapper = worktree / "tools" / "gradlew"
    linked_wrapper.parent.mkdir()
    linked_wrapper.symlink_to(outside_wrapper)
    commands = (
        f"./gradlew -p {shlex.quote(str(leader))} assembleDebug",
        f"./gradlew --project-dir={shlex.quote(str(leader))} check",
        f"./gradlew -g {shlex.quote(str(leader / '.gradle'))} test",
        f"./gradlew --gradle-user-home={shlex.quote(str(leader / '.gradle'))} lint",
        f"./gradlew --init-script={shlex.quote(str(leader / 'init.gradle'))} check",
        f"./gradlew --build-file={shlex.quote(str(leader / 'build.gradle'))} check",
        f"./gradlew --settings-file={shlex.quote(str(leader / 'settings.gradle'))} check",
        f"./gradlew -PbuildDir={shlex.quote(str(leader / 'build'))} assembleDebug",
        f"GRADLE_USER_HOME={shlex.quote(str(leader / '.gradle-env'))} ./gradlew check",
        f"GRADLE_OPTS='-Dgradle.user.home={leader / '.gradle-opts'}' ./gradlew check",
        f"JAVA_TOOL_OPTIONS='-Dagent.output={leader / 'java-tool-output'}' ./gradlew check",
        f"JDK_JAVA_OPTIONS='-Dagent.output={leader / 'jdk-java-output'}' ./gradlew check",
        f"_JAVA_OPTIONS='-Dagent.output={leader / 'java-output'}' ./gradlew check",
        f"JAVA_OPTS='-Dagent.output={leader / 'java-opts-output'}' ./gradlew check",
        f"{shlex.quote(str(outside_wrapper))} assembleDebug",
        "./tools/gradlew assembleDebug",
        "gradlew assembleDebug",
    )

    for command in commands:
        result = _bash_guard(leader, worktree, command, host=host)
        assert result.returncode == 2, (command, result.stderr)


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_exported_and_appended_gradle_environment_cannot_escape_worktree(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    wrapper = worktree / "gradlew"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)
    commands = (
        f"export GRADLE_USER_HOME={shlex.quote(str(leader / '.gradle-export'))}; ./gradlew check",
        f"export GRADLE_OPTS='-p {leader}'; ./gradlew assembleDebug",
        f"GRADLE_OPTS+=' -p {leader}' ./gradlew lint",
        f"JAVA_TOOL_OPTIONS='-javaagent:{leader / 'evil-agent.jar'}' ./gradlew test",
        f"printf -v GRADLE_USER_HOME %s {shlex.quote(str(leader / '.gradle-printf'))}; export GRADLE_USER_HOME; ./gradlew check",
        f"read JAVA_TOOL_OPTIONS <<< '-javaagent:{leader / 'read-agent.jar'}'; export JAVA_TOOL_OPTIONS; ./gradlew check",
    )

    for command in commands:
        result = _bash_guard(leader, worktree, command, host=host)
        assert result.returncode == 2, (command, result.stderr)


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_dynamic_shell_variables_and_zsh_executable_globs_fail_closed(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    commands = (
        f"writer=touch; target={shlex.quote(str(leader / 'variable-write'))}; \"$writer\" \"$target\"",
        f"echo *(e:'touch {leader / 'zsh-write'}':)",
        "touch {safe,../feat-other/owned}",
    )

    for command in commands:
        result = _bash_guard(leader, worktree, command, host=host)
        assert result.returncode == 2, (command, result.stderr)


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_interpreter_wrapped_agent_flow_runtime_is_not_a_trusted_launcher(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, _worktree, _runtime, run_dir = pinned_run
    (run_dir / "active").unlink()
    node = shutil.which("node")
    assert node is not None
    node_alias = leader / "node-alias"
    node_alias.symlink_to(node)
    launcher_alias = leader / "af"
    launcher_alias.symlink_to(leader / ".agent-flow" / "bin" / "agent-flow")
    commands = (
        f"NODE_OPTIONS=--require=/tmp/payload.js node {leader / '.agent-flow/runtime/node/bin/agent-flow-kit.mjs'} install",
        f"BASH_ENV=/tmp/payload.sh bash {leader / '.agent-flow/bin/agent-flow'} install",
        f"{node_alias} {leader / '.agent-flow/runtime/node/bin/agent-flow-kit.mjs'} install",
        f"PATH={shlex.quote(str(leader))} node-alias {leader / '.agent-flow/runtime/node/bin/agent-flow-kit.mjs'} install",
        f"env PATH={shlex.quote(str(leader))} node-alias {leader / '.agent-flow/runtime/node/bin/agent-flow-kit.mjs'} install",
        f"PATH={shlex.quote(str(leader))} af install",
        f"env -P {shlex.quote(str(leader))} af install",
        f"env -C {shlex.quote(str(leader))} -P {shlex.quote(str(leader))} af install",
        "PATH='$ALIAS_DIR:$PATH' af install",
    )

    for command in commands:
        result = _bash_guard(leader, leader, command, host=host)
        assert result.returncode == 2, (command, result.stderr)
        assert "launcher is not trusted" in result.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
@pytest.mark.parametrize(
    "variable",
    ("GRADLE_OPTS", "JDK_JAVA_OPTIONS", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JAVA_OPTS"),
)
def test_inherited_gradle_option_paths_outside_worktree_are_rejected(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
    variable: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    wrapper = worktree / "gradlew"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)

    result = _bash_guard(
        leader,
        worktree,
        "./gradlew check",
        host=host,
        env_override={variable: f"-Dagent.output={leader / 'outside'}"},
    )

    assert result.returncode == 2, result.stderr


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


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_guard_import_does_not_create_python_bytecode_cache(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    python_runtime = leader / ".agent-flow" / "runtime" / "python"
    before = _tree_integrity(python_runtime)

    for _ in range(2):
        result = _bash_guard(
            leader,
            worktree,
            "touch generated.txt",
            host=host,
            env_override={"PYTHONDONTWRITEBYTECODE": None},
        )
        assert result.returncode == 0, result.stderr

    assert _tree_integrity(python_runtime) == before


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
@pytest.mark.parametrize("variable", ("NODE_OPTIONS", "LD_PRELOAD", "LD_AUDIT"))
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
def test_nested_agent_flow_launchers_are_rejected(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, _worktree, _runtime, _run_dir = pinned_run
    launcher_command = f"{leader / '.agent-flow' / 'bin' / 'agent-flow'} status"
    nested = shlex.quote(launcher_command)
    commands = (
        f"bash -lc {nested}",
        f"sh -c {nested}",
        f"eval {nested}",
        f"env -S {nested}",
    )

    for command in commands:
        result = _bash_guard(leader, leader, command, host=host)
        assert result.returncode == 2, (command, result.stderr)
        assert "launcher is not trusted" in result.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
@pytest.mark.parametrize(
    ("variable", "value"),
    (("NODE_OPTIONS", "--require=/tmp/payload.js"), ("LD_AUDIT", "/tmp/payload.so")),
)
def test_inherited_loader_environment_rejects_agent_flow_launcher(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
    variable: str,
    value: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"

    result = _bash_guard(
        leader,
        worktree,
        f"{launcher} status",
        host=host,
        env_override={variable: value},
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
@pytest.mark.parametrize("install_args", ("", " --force-managed"))
def test_authenticated_install_is_allowed_when_python_runtime_drift_blocks_writes(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
    install_args: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"
    python_runtime = leader / ".agent-flow" / "runtime" / "python" / "agent_flow"
    source = python_runtime / "payload.py"
    bytecode = python_runtime / "payload.pyc"
    source.write_text("tampered = True\n", encoding="utf-8")
    py_compile.compile(str(source), cfile=str(bytecode), doraise=True)
    source.unlink()

    blocked = _bash_guard(leader, worktree, "touch generated.txt", host=host)
    result = _bash_guard(leader, leader, f"{launcher} install{install_args}", host=host)

    assert blocked.returncode == 2
    assert "runtime authentication failed" in blocked.stderr
    assert result.returncode == 0, result.stderr


def test_install_recovery_rejects_self_consistent_launcher_and_contract_tamper(
    pinned_run: tuple[Path, Path, Path, Path],
    tmp_path: Path,
) -> None:
    leader, _worktree, _runtime, _run_dir = pinned_run
    kit_path = leader / ".agent-flow" / "kit.json"
    kit = json.loads(kit_path.read_text(encoding="utf-8"))
    original_commitment = kit["project_runtime_contract_commitment"]
    original_python_integrity = kit["project_runtime_contract"]["python_runtime"][
        "integrity"
    ]
    rendered_guard = tmp_path / "rendered-guard.py"
    rendered_guard.write_text(
        GUARD.read_text(encoding="utf-8")
        .replace(
            "__AGENT_FLOW_PROJECT_RUNTIME_CONTRACT_SHA256__",
            original_commitment,
        )
        .replace(
            "__AGENT_FLOW_PYTHON_RUNTIME_INTEGRITY__",
            original_python_integrity,
        ),
        encoding="utf-8",
    )
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"
    launcher.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    launcher.chmod(0o755)
    contract = kit["project_runtime_contract"]
    contract["launcher"]["sha256"] = hashlib.sha256(launcher.read_bytes()).hexdigest()
    kit["project_runtime_contract_commitment"] = _runtime_contract_commitment(contract)
    kit_path.write_text(json.dumps(kit), encoding="utf-8")

    result = _bash_guard(
        leader,
        leader,
        f"{launcher} install",
        guard=rendered_guard,
    )

    assert result.returncode == 2
    assert "launcher is not trusted" in result.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
@pytest.mark.parametrize(
    "cwd_name,install_args",
    (
        ("worktree", ""),
        ("worktree", " --force-managed"),
        ("leader", " --profile node"),
        ("leader", " --force-managed extra"),
    ),
)
def test_authenticated_install_recovery_rejects_non_leader_or_extra_arguments(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
    cwd_name: str,
    install_args: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"
    cwd = leader if cwd_name == "leader" else worktree

    result = _bash_guard(leader, cwd, f"{launcher} install{install_args}", host=host)

    assert result.returncode == 2
    assert "launcher is not trusted" in result.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_authenticated_worktree_finalizer_only_targets_completed_worktree_from_leader(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"
    command = f"{launcher} worktree remove --name test"
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["status"] = "complete"
    meta["execution"] = ExecutionIdentity(host, "session-1", "").to_dict()
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    (run_dir / "active").unlink()

    allowed = _bash_guard(leader, leader, command, host=host)
    wrong_worktree = _bash_guard(
        leader,
        leader,
        f"{launcher} worktree remove --name another-run",
        host=host,
    )
    wrong_cwd = _bash_guard(leader, worktree, command, host=host)
    keep_branch = _bash_guard(leader, leader, f"{command} --keep-branch", host=host)
    assert allowed.returncode == 0, allowed.stderr
    for result in (wrong_worktree, wrong_cwd, keep_branch):
        assert result.returncode == 2, result.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_authenticated_worktree_finalizer_requires_completed_run_ownership(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, _worktree, _runtime, run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"
    (run_dir / "active").unlink()

    result = _bash_guard(
        leader,
        leader,
        f"{launcher} worktree remove --name test",
        host=host,
    )

    assert result.returncode == 2
    assert "completed run ownership" in result.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_active_run_cannot_authorize_worktree_finalizer_or_leader_name_collision(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, _worktree, _runtime, _run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"

    for requested_name in ("test", "project"):
        result = _bash_guard(
            leader,
            leader,
            f"{launcher} worktree remove --name {requested_name}",
            host=host,
        )
        assert result.returncode == 2, result.stderr
        assert "completed run ownership" in result.stderr
    direct_git = _bash_guard(
        leader,
        leader,
        f"git worktree remove {leader / '.agent-flow' / 'worktrees' / 'feat-test'}",
        host=host,
    )
    assert direct_git.returncode == 2, direct_git.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_authenticated_finalizer_accepts_completed_python_run_ownership(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, _worktree, _runtime, run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["status"] = "complete"
    meta["execution"] = ExecutionIdentity(host, "session-1", "").to_dict()
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    (run_dir / "active").unlink()

    allowed = _bash_guard(
        leader,
        leader,
        f"{launcher} worktree remove --name test",
        host=host,
    )
    wrong_execution = _bash_guard(
        leader,
        leader,
        f"{launcher} worktree remove --name test",
        host=host,
        session_id="different-session",
    )

    assert allowed.returncode == 0, allowed.stderr
    assert wrong_execution.returncode == 2
    assert "completed run ownership" in wrong_execution.stderr


def test_completed_finalizer_accepts_authenticated_stale_checkout_identity(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, _runtime, run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["status"] = "complete"
    meta["execution"] = ExecutionIdentity("codex", "session-1", "").to_dict()
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    (run_dir / "active").unlink()
    _git(leader, "worktree", "remove", str(worktree))

    result = _bash_guard(
        leader,
        leader,
        f"{launcher} worktree remove --name test",
    )

    assert result.returncode == 0, result.stderr


def test_completed_finalizer_rejects_workspace_reused_by_active_run(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, runtime, run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["status"] = "complete"
    meta["execution"] = ExecutionIdentity("codex", "session-1", "").to_dict()
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    (run_dir / "active").unlink()
    reused_run = runtime / ".agent-flow" / "runs" / "reused-run"
    reused_run.mkdir()
    (reused_run / "active").write_text("", encoding="utf-8")
    (reused_run / "meta.json").write_text(
        json.dumps(
            {
                "run_id": "reused-run",
                "workspace": _identity(worktree),
                "execution": ExecutionIdentity(
                    "codex", "replacement-session", ""
                ).to_dict(),
            }
        ),
        encoding="utf-8",
    )

    result = _bash_guard(
        leader,
        leader,
        f"{launcher} worktree remove --name test",
    )

    assert result.returncode == 2
    assert "completed run ownership" in result.stderr


def test_completed_finalizer_only_authorizes_latest_execution_owner(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, runtime, run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"
    first_meta_path = run_dir / "meta.json"
    first_meta = json.loads(first_meta_path.read_text(encoding="utf-8"))
    first_meta.update(
        {
            "status": "complete",
            "completed_at": "2026-07-14T01:00:00+00:00",
            "execution": ExecutionIdentity("codex", "session-1", "").to_dict(),
        }
    )
    first_meta_path.write_text(json.dumps(first_meta), encoding="utf-8")
    (run_dir / "active").unlink()
    second_run = runtime / ".agent-flow" / "runs" / "run-2"
    second_run.mkdir()
    (second_run / "meta.json").write_text(
        json.dumps(
            {
                "run_id": "run-2",
                "status": "complete",
                "completed_at": "2026-07-14T02:00:00+00:00",
                "workspace": _identity(worktree),
                "execution": ExecutionIdentity(
                    "codex", "replacement-session", ""
                ).to_dict(),
            }
        ),
        encoding="utf-8",
    )

    old_owner = _bash_guard(
        leader,
        leader,
        f"{launcher} worktree remove --name test",
    )
    current_owner = _bash_guard(
        leader,
        leader,
        f"{launcher} worktree remove --name test",
        session_id="replacement-session",
    )

    assert old_owner.returncode == 2
    assert current_owner.returncode == 0, current_owner.stderr


def test_completed_finalizer_rejects_symlinked_git_private_runtime_parent(
    pinned_run: tuple[Path, Path, Path, Path],
    tmp_path: Path,
) -> None:
    leader, _worktree, runtime, run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.update(
        {
            "status": "complete",
            "execution": ExecutionIdentity("codex", "session-1", "").to_dict(),
        }
    )
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    (run_dir / "active").unlink()
    outside = tmp_path / "outside-runtime"
    runtime.rename(outside)
    runtime.symlink_to(outside, target_is_directory=True)

    result = _bash_guard(
        leader,
        leader,
        f"{launcher} worktree remove --name test",
    )

    assert result.returncode == 2
    assert "completed run ownership" in result.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_authenticated_finalizer_accepts_completed_node_run_ownership(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"
    (run_dir / "active").unlink()
    execution = ExecutionIdentity(host, "session-1", "")
    state_root = leader / ".git" / "agent-flow" / "current-runs"
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / f"{execution.digest}.json").write_text(
        json.dumps(
            {
                "run_id": "node-complete",
                "run_dir": ".agent-flow/runs/full-feature/node-complete",
                "status": "complete",
                "phase": "complete",
                "execution": execution.to_dict(),
                "workspace": _identity(worktree),
            }
        ),
        encoding="utf-8",
    )

    result = _bash_guard(
        leader,
        leader,
        f"{launcher} worktree remove --name test",
        host=host,
    )

    assert result.returncode == 0, result.stderr


def test_incomplete_node_publication_cannot_authorize_worktree_write(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    from agent_flow.core.workspace_boundary import (
        WorkspaceBoundaryError,
        find_active_pinned_workspaces,
        resolve_execution_workspace,
    )

    leader, worktree, _runtime, python_run_dir = pinned_run
    (python_run_dir / "active").unlink()
    execution = ExecutionIdentity("codex", "session-1", "")
    identity = _identity(worktree)
    node_run_dir = leader / ".agent-flow" / "runs" / "full-feature" / "node-starting"
    node_run_dir.mkdir(parents=True)
    state = {
        "run_id": "node-starting",
        "workflow": "full-feature",
        "run_dir": ".agent-flow/runs/full-feature/node-starting",
        "status": "running",
        "phase": "implement",
        "publication_status": "starting",
        "start_claim_token": "starting-token",
        "execution": execution.to_dict(),
        "workspace": identity,
    }
    (node_run_dir / "manifest.json").write_text(json.dumps(state), encoding="utf-8")
    current_root = leader / ".git" / "agent-flow" / "current-runs"
    current_root.mkdir(parents=True, exist_ok=True)
    (current_root / f"{execution.digest}.json").write_text(
        json.dumps(state), encoding="utf-8"
    )
    binding_root = leader / ".git" / "agent-flow" / "executions"
    binding_root.mkdir(parents=True, exist_ok=True)
    (binding_root / f"{execution.digest}.json").write_text(
        json.dumps(
            {
                "version": 2,
                "execution": execution.to_dict(),
                "workspace": identity,
                "workspace_name": worktree.name,
                "run_id": state["run_id"],
                "run_dir": str(node_run_dir.resolve()),
                "start_claim_token": state["start_claim_token"],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceBoundaryError, match="execution_binding_incomplete"):
        find_active_pinned_workspaces(leader)
    with pytest.raises(WorkspaceBoundaryError, match="execution_binding_stale"):
        resolve_execution_workspace(leader, execution)
    assert _bash_guard(leader, leader, "touch shared.txt").returncode == 2


def test_python_preserves_starting_binding_while_workspace_claim_is_live(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    from agent_flow.core.workspace_boundary import (
        _binding_is_active,
        acquire_workspace_start_claim,
        release_workspace_start_claim,
        workspace_identity_from_dict,
    )

    leader, worktree, _runtime, _python_run_dir = pinned_run
    identity_payload = _identity(worktree)
    identity = workspace_identity_from_dict(identity_payload)
    run_dir = leader / ".agent-flow" / "runs" / "full-feature" / "node-starting-live"
    run_dir.mkdir(parents=True)
    claim = acquire_workspace_start_claim(identity, run_id="node-starting-live")
    binding = {
        "execution": ExecutionIdentity("codex", "same-execution", "").to_dict(),
        "workspace": identity_payload,
        "run_id": "node-starting-live",
        "run_dir": str(run_dir.resolve()),
        "start_claim_token": claim.token,
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                **binding,
                "status": "running",
                "phase": "implement",
                "publication_status": "starting",
            }
        ),
        encoding="utf-8",
    )

    try:
        assert _binding_is_active(binding)
    finally:
        release_workspace_start_claim(claim)

    assert not _binding_is_active(binding)


def test_completed_finalizer_remains_available_while_other_runs_are_active(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, _worktree, _runtime, run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["status"] = "complete"
    meta["execution"] = ExecutionIdentity("codex", "session-1", "").to_dict()
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    (run_dir / "active").unlink()

    other = leader / ".agent-flow" / "worktrees" / "feat-other"
    _git(leader, "worktree", "add", "-b", "feat/other", str(other), "main")
    other_runtime = leader / ".git" / "agent-flow" / "worktrees" / "feat-other"
    other_run = other_runtime / ".agent-flow" / "runs" / "other-run"
    other_run.mkdir(parents=True)
    (other_run / "active").write_text("", encoding="utf-8")
    other_identity = capture_workspace_identity(other)
    (other_run / "meta.json").write_text(
        json.dumps(
            {
                "run_id": "other-run",
                "workspace": other_identity.to_dict(),
                "execution": ExecutionIdentity(
                    "codex", "other-session", ""
                ).to_dict(),
            }
        ),
        encoding="utf-8",
    )
    bind_execution_to_workspace(
        ExecutionIdentity("codex", "other-session", ""),
        other_identity,
        other_run,
        run_id="other-run",
    )

    result = _bash_guard(
        leader,
        leader,
        f"{launcher} worktree remove --name test",
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("status", ("aborted", "running"))
def test_authenticated_finalizer_rejects_non_completed_inactive_run(
    pinned_run: tuple[Path, Path, Path, Path],
    status: str,
) -> None:
    leader, _worktree, _runtime, run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["status"] = status
    meta["execution"] = ExecutionIdentity("codex", "session-1", "").to_dict()
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    (run_dir / "active").unlink()

    result = _bash_guard(
        leader,
        leader,
        f"{launcher} worktree remove --name test",
    )

    assert result.returncode == 2
    assert "completed run ownership" in result.stderr


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
def test_authenticated_install_rejects_bare_local_path_alias(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, _worktree, _runtime, _run_dir = pinned_run
    local_bin = leader / ".agent-flow" / "bin"
    env = {"PATH": f"{local_bin}{os.pathsep}{os.environ.get('PATH', '')}"}

    result = _bash_guard(
        leader,
        leader,
        "agent-flow install",
        host=host,
        env_override=env,
    )

    assert result.returncode == 2
    assert "launcher is not trusted" in result.stderr


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

def _native_edit_input(target: Path, body: str = "changed") -> dict[str, object]:
    patch = f"[{target}#1A2B]\nSWAP 1.=1:\n+{body}\n"
    return {"input": patch, "i": "edit source file"}


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_native_edit_patch_targets_are_resolved(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    target = worktree / "src" / "big.py"
    target.parent.mkdir(parents=True)
    target.write_text("line1\nline2\n", encoding="utf-8")

    allowed = _structured_guard(
        leader, worktree, "Edit", _native_edit_input(target), host=host
    )
    assert allowed.returncode == 0, allowed.stderr

    outside = leader / "escape.py"
    blocked = _structured_guard(
        leader, worktree, "Edit", _native_edit_input(outside), host=host
    )
    assert blocked.returncode == 2
    assert "target_outside_pinned_workspace" in blocked.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_native_edit_move_target_is_bounded(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    target = worktree / "src" / "mod.py"
    target.parent.mkdir(parents=True)
    target.write_text("value = 1\n", encoding="utf-8")
    escape = leader / "escaped.py"
    patch = f"[{target}#1A2B]\nSWAP 1.=1:\n+value = 2\nMV {escape}\n"

    blocked = _structured_guard(
        leader, worktree, "Edit", {"input": patch, "i": "move out"}, host=host
    )
    assert blocked.returncode == 2
    assert "target_outside_pinned_workspace" in blocked.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_bash_mutation_honors_tool_declared_cwd(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run

    allowed = _bash_guard(
        leader, leader, "touch generated.txt", host=host, tool_cwd=worktree
    )
    assert allowed.returncode == 0, allowed.stderr

    blocked = _bash_guard(
        leader, leader, "touch generated.txt", host=host, tool_cwd=leader
    )
    assert blocked.returncode == 2
    assert "mutation_cwd_not_pinned" in blocked.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_phase_artifact_write_into_git_private_run_dir_is_allowed(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, run_dir = pinned_run

    artifact = run_dir / "design.md"
    allowed = _structured_guard(
        leader,
        worktree,
        "Write",
        {"file_path": str(artifact), "content": "# design\n"},
        host=host,
        phase="design",
    )
    assert allowed.returncode == 0, allowed.stderr

    nested = run_dir / "artifacts" / "gate-results.json"
    allowed_nested = _structured_guard(
        leader,
        worktree,
        "Write",
        {"file_path": str(nested), "content": "{}\n"},
        host=host,
        phase="gates",
    )
    assert allowed_nested.returncode == 0, allowed_nested.stderr

    escape = leader / ".git" / "agent-flow" / "executions" / "evil.json"
    blocked = _structured_guard(
        leader, worktree, "Write",
        {"file_path": str(escape), "content": "{}\n"},
        host=host,
    )
    assert blocked.returncode == 2
    assert "target_outside_pinned_workspace" in blocked.stderr

    for protected in ("meta.json", "manifest.json", "active"):
        blocked_state = _structured_guard(
            leader,
            worktree,
            "Write",
            {"file_path": str(run_dir / protected), "content": "changed\n"},
            host=host,
            phase="design",
        )
        assert blocked_state.returncode == 2
        assert "protected_run_state_path" in blocked_state.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_xd_ast_edit_real_targets_are_bounded(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    target = worktree / "src" / "mod.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n", encoding="utf-8")

    inside = {"ops": [{"pat": "x", "out": "y"}], "paths": [str(target)]}
    allowed = _structured_guard(
        leader, worktree, "Write",
        {"path": "xd://ast_edit", "content": json.dumps(inside)},
        host=host,
    )
    assert allowed.returncode == 0, allowed.stderr

    outside = {"ops": [{"pat": "x", "out": "y"}], "paths": [str(leader / "outside.py")]}
    blocked = _structured_guard(
        leader, worktree, "Write",
        {"path": "xd://ast_edit", "content": json.dumps(outside)},
        host=host,
    )
    assert blocked.returncode == 2
    assert "target_outside_pinned_workspace" in blocked.stderr

    internal = {
        "ops": [{"pat": "x", "out": "y"}],
        "paths": ["skill://code-generation-discipline"],
    }
    blocked_internal = _structured_guard(
        leader,
        worktree,
        "Write",
        {"path": "xd://ast_edit", "content": json.dumps(internal)},
        host=host,
    )
    assert blocked_internal.returncode == 2
    assert "target_uri_not_supported" in blocked_internal.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_read_only_xd_tool_write_is_allowed(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    allowed = _structured_guard(
        leader, worktree, "Write",
        {"path": "xd://web_search", "content": json.dumps({"query": "x"})},
        host=host,
    )
    assert allowed.returncode == 0, allowed.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_status_next_command_is_a_trusted_launcher(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    from agent_flow.cli import _continue_command

    monkeypatch.delenv("AGENT_FLOW_PROJECT_LAUNCHER", raising=False)
    command = _continue_command(leader, None)
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"
    assert str(launcher) in command

    result = _bash_guard(leader, worktree, command, host=host)
    assert result.returncode == 0, result.stderr

@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_stale_binding_blocks_leader_shell_and_write(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, _worktree, _runtime, run_dir = pinned_run
    (run_dir / "active").unlink()

    shell = _bash_guard(leader, leader, "touch generated.txt", host=host)
    assert shell.returncode == 2, shell.stderr
    assert "bound_run_not_active" in shell.stderr

    write = _structured_guard(
        leader, leader, "Write",
        {"file_path": str(leader / "leaked.txt"), "content": "x\n"},
        host=host,
    )
    assert write.returncode == 2, write.stderr
    assert "bound_run_not_active" in write.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_unbound_execution_without_active_run_stays_allowed(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, _worktree, _runtime, run_dir = pinned_run
    (run_dir / "active").unlink()

    shell = _bash_guard(
        leader, leader, "touch generated.txt", host=host, session_id="unbound-session"
    )
    assert shell.returncode == 0, shell.stderr


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_declared_cwd_outside_repo_does_not_bypass_guard(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, _worktree, _runtime, _run_dir = pinned_run
    outside = leader.parent / "outside"
    outside.mkdir()

    result = _bash_guard(
        leader,
        leader,
        "touch escaped.txt",
        host=host,
        tool_cwd=outside,
    )

    assert result.returncode == 2, result.stderr
    assert "mutation_cwd_not_pinned" in result.stderr
    assert not (outside / "escaped.txt").exists()


@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_record_stage_launcher_is_bounded_to_authenticated_run(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, _worktree, _runtime, run_dir = pinned_run
    outside = leader.parent / "outside-run"
    outside.mkdir()
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"

    def command(target: Path, stage: str) -> str:
        return " ".join(
            (
                shlex.quote(str(launcher)),
                "record-stage",
                "--root",
                shlex.quote(str(leader)),
                "--run-dir",
                shlex.quote(str(target)),
                "--stage",
                shlex.quote(stage),
                "--content",
                shlex.quote("stage result"),
            )
        )

    allowed = _bash_guard(leader, leader, command(run_dir, "design"), host=host)
    outside_run = _bash_guard(leader, leader, command(outside, "design"), host=host)
    unsafe_stage = _bash_guard(
        leader,
        leader,
        command(run_dir, "../../exploit"),
        host=host,
    )

    assert allowed.returncode == 0, allowed.stderr
    assert outside_run.returncode == 2, outside_run.stderr
    assert "launcher is not trusted" in outside_run.stderr
    assert unsafe_stage.returncode == 2, unsafe_stage.stderr
    assert "launcher is not trusted" in unsafe_stage.stderr
