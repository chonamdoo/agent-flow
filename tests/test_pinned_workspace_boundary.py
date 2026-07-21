from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import agent_flow.core.workspace_boundary as workspace_boundary
from agent_flow.cli import main as cli_main
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




def _fast_validate_workspace_identity(
    identity: workspace_boundary.WorkspaceIdentity,
) -> Path:
    configured = Path(identity.workspace_root)
    try:
        root = configured.resolve(strict=True)
    except FileNotFoundError as exc:
        raise workspace_boundary.WorkspaceBoundaryError(
            f"pinned workspace is missing: {configured}"
        ) from exc
    if not root.is_dir():
        raise workspace_boundary.WorkspaceBoundaryError(
            f"pinned workspace is not a directory: {root}"
        )
    metadata = root.stat()
    if (metadata.st_dev, metadata.st_ino) != (identity.device, identity.inode):
        raise workspace_boundary.WorkspaceBoundaryError(
            "pinned workspace filesystem identity changed"
        )
    return root




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


@pytest.fixture(scope="module")
def pinned_run_template(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, Path, dict[str, object], str]:
    template_root = tmp_path_factory.mktemp("pinned-run-template")
    leader_template = template_root / "leader"
    leader_template.mkdir()
    subprocess.run(
        ("git", "init", "-b", "main", str(leader_template)),
        check=True,
        capture_output=True,
    )
    _git(leader_template, "config", "user.name", "Test User")
    _git(leader_template, "config", "user.email", "test@example.com")
    (leader_template / "shared.txt").write_text("leader\n", encoding="utf-8")
    _git(leader_template, "add", "shared.txt")
    _git(leader_template, "commit", "-m", "initial")
    template_head = _git(leader_template, "rev-parse", "HEAD")

    node_runtime_template = template_root / "node-runtime"
    node_entry = node_runtime_template / "bin" / "agent-flow-kit.mjs"
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
        (node_runtime_template / relative).mkdir(parents=True, exist_ok=True)
    (node_runtime_template / "lib" / "skill-selection.mjs").write_text(
        "export {};\n",
        encoding="utf-8",
    )

    python_runtime_template = template_root / "python-runtime" / "agent_flow"
    for relative in ("__init__.py", "core/__init__.py", "core/workspace_boundary.py"):
        destination = python_runtime_template / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(KIT_ROOT / "src" / "agent_flow" / relative, destination)
    python_runtime_template = python_runtime_template.parent
    node = Path(shutil.which("node") or sys.executable)
    contract = {
        "version": 3,
        "node": {
            "path": str(node.resolve()),
            **_executable_identity(node),
            "dependencies": [],
        },
        "git": {
            "path": str(Path("/usr/bin/git").resolve()),
            **_executable_identity(Path("/usr/bin/git")),
            "dependencies": [],
        },
        "python": {
            "path": str(Path(sys.executable).absolute()),
            "resolved_path": str(Path(sys.executable).resolve()),
            **_executable_identity(Path(sys.executable)),
            "dependencies": [],
        },
        "runtime": {
            "path": ".agent-flow/runtime/node",
            "integrity": _tree_integrity(node_runtime_template),
        },
        "python_runtime": {
            "path": ".agent-flow/runtime/python",
            "integrity": _tree_integrity(python_runtime_template),
        },
    }
    return (
        leader_template,
        node_runtime_template,
        python_runtime_template,
        contract,
        template_head,
    )


def _materialize_pinned_state(
    leader: Path,
    worktree: Path,
    workspace_identity: workspace_boundary.WorkspaceIdentity,
    node_runtime_template: Path,
    python_runtime_template: Path,
    base_contract: dict[str, object],
) -> tuple[Path, Path, Path, Path]:
    identity = workspace_identity.to_dict()
    runtime = leader / ".git" / "agent-flow" / "worktrees" / "feat-test"
    runtime.mkdir(parents=True)
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

    bindings_root = Path(workspace_identity.git_common_dir) / "agent-flow" / "executions"
    bindings_root.mkdir(parents=True, exist_ok=True)
    # Bindings are written directly (not via bind_execution_to_workspace) so the
    # fixture avoids validate_workspace_identity's git subprocess storm; the guard
    # only ever reads these files. Real identity comes from a single
    # capture_workspace_identity call in the git_auth branch.
    for host in ("codex", "claude", "omp"):
        execution = ExecutionIdentity(host=host, session_id="session-1", agent_id="")
        binding_path = bindings_root / f"{execution.digest}.json"
        binding_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "execution": execution.to_dict(),
                    "workspace": identity,
                    "workspace_name": worktree.name,
                    "run_id": "run-1",
                    "run_dir": str(run_dir.resolve()),
                    "bound_at": "2026-07-14T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        binding_path.chmod(0o600)

    launcher = leader / ".agent-flow" / "bin" / "agent-flow"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    shutil.copytree(
        node_runtime_template,
        leader / ".agent-flow" / "runtime" / "node",
    )
    shutil.copytree(
        python_runtime_template,
        leader / ".agent-flow" / "runtime" / "python",
    )

    contract = json.loads(json.dumps(base_contract))
    contract["launcher"] = {
        "path": ".agent-flow/bin/agent-flow",
        "sha256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
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


@pytest.fixture
def pinned_run(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    pinned_run_template: tuple[Path, Path, Path, dict[str, object], str],
) -> tuple[Path, Path, Path, Path]:
    (
        leader_template,
        node_runtime_template,
        python_runtime_template,
        base_contract,
        template_head,
    ) = pinned_run_template
    leader = tmp_path / "feat-project"
    leader.mkdir()
    worktree = leader / ".agent-flow" / "worktrees" / "feat-test"
    authenticate_bindings = request.node.get_closest_marker("git_auth") is not None

    if authenticate_bindings:
        shutil.copytree(leader_template / ".git", leader / ".git")
        shutil.copy2(leader_template / "shared.txt", leader / "shared.txt")
        _git(leader, "worktree", "add", "-b", "feat/test", str(worktree), "main")
        # Identity is built deterministically from the known worktree layout and the
        # template HEAD instead of capture_workspace_identity's 5 git subprocesses,
        # which otherwise stampede git under -n auto. This exactly matches the
        # production capture output (verified in tests) so the real subprocess guard
        # still re-validates against it.
        metadata = worktree.stat()
        workspace_identity = workspace_boundary.WorkspaceIdentity(
            workspace_root=str(worktree.resolve()),
            git_common_dir=str((leader / ".git").resolve()),
            git_dir=str((leader / ".git" / "worktrees" / "feat-test").resolve()),
            branch="feat/test",
            head=template_head,
            device=metadata.st_dev,
            inode=metadata.st_ino,
        )
    else:
        git_common_dir = leader / ".git"
        git_common_dir.mkdir()
        (leader / "shared.txt").write_text("leader\n", encoding="utf-8")
        worktree.mkdir(parents=True)
        (worktree / "shared.txt").write_text("leader\n", encoding="utf-8")
        git_dir = git_common_dir / "worktrees" / "feat-test"
        git_dir.mkdir(parents=True)
        metadata = worktree.stat()
        workspace_identity = workspace_boundary.WorkspaceIdentity(
            workspace_root=str(worktree.resolve()),
            git_common_dir=str(git_common_dir.resolve()),
            git_dir=str(git_dir.resolve()),
            branch="feat/test",
            head="0" * 40,
            device=metadata.st_dev,
            inode=metadata.st_ino,
        )

    result = _materialize_pinned_state(
        leader,
        worktree,
        workspace_identity,
        node_runtime_template,
        python_runtime_template,
        base_contract,
    )
    return result


def _guard_environment(
    env_override: dict[str, str | None] | None = None,
) -> dict[str, str]:
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
    return env


def _run_guard(
    leader: Path,
    payload: dict[str, object],
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
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
    session_id: str = "session-1",
    agent_id: str = "",
    env_override: dict[str, str | None] | None = None,
) -> subprocess.CompletedProcess[str]:
    payload = {
        "tool_name": "Bash",
        "cwd": str(cwd),
        "host": host,
        "session_id": session_id,
        "agent_id": agent_id,
        "tool_input": {"command": command},
    }
    return _run_guard(
        leader,
        payload,
        _guard_environment(env_override),
    )


def _structured_guard(
    leader: Path,
    worktree: Path,
    tool_name: str,
    tool_input: dict[str, object],
    *,
    host: str = "codex",
) -> subprocess.CompletedProcess[str]:
    payload = {
        "tool_name": tool_name,
        "cwd": str(worktree),
        "host": host,
        "tool_input": tool_input,
    }
    return _run_guard(
        leader,
        payload,
        _guard_environment(),
    )


@pytest.mark.git_auth
@pytest.mark.parametrize("host", ("codex", "claude", "omp"))
def test_stateless_guard_confines_writes_to_the_current_checkout(
    pinned_run: tuple[Path, Path, Path, Path],
    host: str,
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run

    allowed = _structured_guard(
        leader,
        worktree,
        "write",
        {"path": str(worktree / "shared.txt"), "content": "changed\n"},
        host=host,
    )
    blocked = _structured_guard(
        leader,
        worktree,
        "write",
        {"path": str(leader / "shared.txt"), "content": "changed\n"},
        host=host,
    )

    assert allowed.returncode == 0, allowed.stderr
    assert blocked.returncode == 2
    assert "target_outside_pinned_workspace" in blocked.stderr


@pytest.mark.git_auth
def test_stateless_guard_gates_private_run_artifacts(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, _runtime, run_dir = pinned_run
    artifacts = run_dir / "artifacts"
    artifacts.mkdir()
    outside = leader.parent / "outside"
    outside.mkdir(exist_ok=True)

    design = _structured_guard(
        leader, worktree, "write",
        {"path": str(run_dir / "design.md"), "content": "design\n"},
    )
    log = _structured_guard(
        leader, worktree, "write",
        {"path": str(artifacts / "red.log"), "content": "failure\n"},
    )
    meta = _structured_guard(
        leader, worktree, "write",
        {"path": str(run_dir / "meta.json"), "content": "{}\n"},
    )
    escaped = _structured_guard(
        leader, worktree, "write",
        {"path": str(outside / "steal.md"), "content": "x\n"},
    )
    sibling = _structured_guard(
        leader, worktree, "write",
        {"path": str(
            leader / ".git" / "agent-flow" / "worktrees" / "feat-other"
            / ".agent-flow" / "runs" / "run-2" / "design.md"
        ), "content": "x\n"},
    )
    leader_run = _structured_guard(
        leader, worktree, "write",
        {"path": str(leader / ".agent-flow" / "runs" / "run-x" / "design.md"), "content": "x\n"},
    )

    assert design.returncode == 0, design.stderr
    assert log.returncode == 0, log.stderr
    assert meta.returncode == 2
    assert "protected_run_state_path" in meta.stderr
    assert escaped.returncode == 2
    assert "target_outside_pinned_workspace" in escaped.stderr
    assert sibling.returncode == 2
    assert "target_outside_pinned_workspace" in sibling.stderr
    assert leader_run.returncode == 2
    assert "target_outside_pinned_workspace" in leader_run.stderr


@pytest.mark.git_auth
def test_leader_cwd_writes_worktree_private_run_artifacts(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    # omp keeps cwd on the leader even for a worktree run, so the host writes
    # phase artifacts into the worktree's git-private run dir from the leader.
    # Those artifacts must be allowed while run metadata and git internals stay
    # blocked. This is the regression the run-area model fixes.
    leader, _worktree, _runtime, run_dir = pinned_run
    artifacts = run_dir / "artifacts"
    artifacts.mkdir()
    executions = leader / ".git" / "agent-flow" / "executions"
    executions.mkdir(parents=True, exist_ok=True)

    design = _structured_guard(
        leader, leader, "write",
        {"path": str(run_dir / "design.md"), "content": "design\n"},
    )
    log = _structured_guard(
        leader, leader, "write",
        {"path": str(artifacts / "red.log"), "content": "failure\n"},
    )
    meta = _structured_guard(
        leader, leader, "write",
        {"path": str(run_dir / "meta.json"), "content": "{}\n"},
    )
    binding = _structured_guard(
        leader, leader, "write",
        {"path": str(executions / "forged.json"), "content": "{}\n"},
    )
    config = _structured_guard(
        leader, leader, "write",
        {"path": str(leader / ".git" / "config"), "content": "x\n"},
    )

    assert design.returncode == 0, design.stderr
    assert log.returncode == 0, log.stderr
    assert meta.returncode == 2
    assert "protected_run_state_path" in meta.stderr
    assert binding.returncode == 2
    assert "git_metadata_write" in binding.stderr
    assert config.returncode == 2
    assert "git_metadata_write" in config.stderr


@pytest.mark.git_auth
def test_stateless_guard_protects_leader_run_metadata(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, _worktree, _runtime, _run_dir = pinned_run
    run_dir = leader / ".agent-flow" / "runs" / "default" / "run-1"
    run_dir.mkdir(parents=True)

    artifact = _structured_guard(
        leader,
        leader,
        "write",
        {"path": str(run_dir / "design.md"), "content": "design\n"},
    )
    blocked = {
        name: _structured_guard(
            leader,
            leader,
            "write",
            {"path": str(run_dir / name), "content": "{}\n"},
        )
        for name in ("meta.json", "events.jsonl", "review-summary.json", "active")
    }
    gate = _structured_guard(
        leader,
        leader,
        "write",
        {"path": str(run_dir / "artifacts" / "gate-results.json"), "content": "{}\n"},
    )

    assert artifact.returncode == 0, artifact.stderr
    for name, result in blocked.items():
        assert result.returncode == 2, (name, result.stderr)
        assert "protected_run_state_path" in result.stderr, (name, result.stderr)
    assert gate.returncode == 2
    assert "protected_run_state_path" in gate.stderr


@pytest.mark.git_auth
def test_stateless_guard_rejects_symlink_target_and_cwd_escapes(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    outside = leader.parent / "outside"
    outside.mkdir()
    escape = worktree / "escape"
    escape.symlink_to(outside, target_is_directory=True)

    target_escape = _structured_guard(
        leader,
        worktree,
        "write",
        {"path": str(escape / "new.txt"), "content": "escape\n"},
    )
    cwd_escape = _bash_guard(leader, escape, "touch escaped.txt")

    assert target_escape.returncode == 2
    assert "target_outside_pinned_workspace" in target_escape.stderr
    assert cwd_escape.returncode == 2
    assert "mutation_cwd_not_pinned" in cwd_escape.stderr


@pytest.mark.git_auth
def test_stateless_guard_blocks_writes_to_git_metadata(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run

    worktree_marker = _bash_guard(leader, worktree, "rm .git")
    leader_metadata = _structured_guard(
        leader,
        leader,
        "write",
        {"path": str(leader / ".git" / "config"), "content": "x\n"},
    )

    assert worktree_marker.returncode == 2
    assert "git metadata is not writable" in worktree_marker.stderr
    assert leader_metadata.returncode == 2
    assert "git metadata is not writable" in leader_metadata.stderr


@pytest.mark.git_auth
def test_find_active_pinned_workspaces_resolves_the_bound_worktree(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run

    active = find_active_pinned_workspaces(leader)

    roots = {Path(entry.identity.workspace_root).resolve() for entry in active}
    assert active
    assert worktree.resolve() in roots

@pytest.mark.git_auth
def test_stateless_guard_rejects_missing_declared_cwd(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    result = _run_guard(
        leader,
        {
            "tool_name": "write",
            "cwd": str(leader.parent / "missing"),
            "tool_input": {"path": str(worktree / "shared.txt")},
        },
        _guard_environment(),
    )

    assert result.returncode == 2
    assert "mutation cwd is unavailable" in result.stderr


@pytest.mark.git_auth
def test_pathless_mutation_still_authenticates_python_runtime(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    python_init = leader / ".agent-flow" / "runtime" / "python" / "agent_flow" / "__init__.py"
    python_init.write_text("# tampered\n", encoding="utf-8")

    result = _bash_guard(leader, worktree, "pytest -q")

    assert result.returncode == 2
    assert "runtime authentication failed" in result.stderr


@pytest.mark.git_auth
def test_authenticated_install_recovers_from_python_runtime_drift(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    python_init = leader / ".agent-flow" / "runtime" / "python" / "agent_flow" / "__init__.py"
    python_init.write_text("# tampered\n", encoding="utf-8")
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"

    blocked_write = _structured_guard(
        leader,
        worktree,
        "write",
        {"path": str(worktree / "shared.txt"), "content": "changed\n"},
    )
    install = _bash_guard(leader, leader, f"{launcher} install")

    assert blocked_write.returncode == 2
    assert "runtime authentication failed" in blocked_write.stderr
    assert install.returncode == 0, install.stderr


@pytest.mark.git_auth
def test_repin_workspace_identity_recovers_inode_only_replacement(
    pinned_run: tuple[Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    leader, worktree, runtime, run_dir = pinned_run
    original = workspace_boundary.workspace_identity_from_dict(
        json.loads((runtime / "manifest.json").read_text(encoding="utf-8"))["identity"]
    )
    displaced = worktree.with_name(".feat-test-displaced")
    worktree.rename(displaced)
    shutil.copytree(displaced, worktree)
    shutil.rmtree(displaced)
    current = worktree.stat()
    assert current.st_ino != original.inode

    with pytest.raises(
        workspace_boundary.WorkspaceBoundaryError,
        match="pinned workspace filesystem identity changed",
    ):
        workspace_boundary.validate_workspace_identity(original)

    unauthorized = ExecutionIdentity(host="omp", session_id="other-session")
    with pytest.raises(
        workspace_boundary.WorkspaceBoundaryError,
        match="execution_binding_missing",
    ):
        workspace_boundary.repin_workspace_identity(
            leader,
            "feat-test",
            unauthorized,
        )
    assert (
        json.loads((runtime / "manifest.json").read_text(encoding="utf-8"))["identity"]["inode"]
        == original.inode
    )

    execution = ExecutionIdentity(host="omp", session_id="session-1")
    monkeypatch.setenv("AGENT_FLOW_ACTIVE_HOST", execution.host)
    monkeypatch.setenv("AGENT_FLOW_EXECUTION_ID", execution.session_id)
    monkeypatch.setenv("AGENT_FLOW_AGENT_ID", execution.agent_id)
    assert cli_main(
        [
            "worktree",
            "repin",
            "--root",
            str(leader),
            "--name",
            "feat-test",
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "updated=" in output
    assert int(output.rsplit("updated=", 1)[1].strip()) >= 3
    repinned = workspace_boundary.workspace_identity_from_dict(
        json.loads((runtime / "manifest.json").read_text(encoding="utf-8"))["identity"]
    )
    assert (repinned.device, repinned.inode) == (current.st_dev, current.st_ino)
    assert workspace_boundary.validate_workspace_identity(repinned) == worktree
    assert resolve_execution_workspace(leader, execution).identity == repinned
    assert find_active_pinned_workspaces(leader)
    assert json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))["workspace"] == (
        repinned.to_dict()
    )

    stale_meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    stale_meta["workspace"] = original.to_dict()
    (run_dir / "meta.json").write_text(json.dumps(stale_meta), encoding="utf-8")
    with pytest.raises(
        workspace_boundary.WorkspaceBoundaryError,
        match="pinned workspace filesystem identity changed",
    ):
        find_active_pinned_workspaces(leader)

    reconciled, reconciled_count = workspace_boundary.repin_workspace_identity(
        leader,
        "feat-test",
        execution,
    )
    assert reconciled == repinned
    assert reconciled_count == 1
    assert find_active_pinned_workspaces(leader)

@pytest.mark.git_auth
def test_abort_recovers_stale_bound_workspace(
    pinned_run: tuple[Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader, worktree, _runtime, run_dir = pinned_run
    displaced = worktree.with_name(".feat-test-displaced")
    worktree.rename(displaced)
    shutil.copytree(displaced, worktree)
    shutil.rmtree(displaced)
    monkeypatch.setenv("AGENT_FLOW_ACTIVE_HOST", "omp")
    monkeypatch.setenv("AGENT_FLOW_EXECUTION_ID", "session-1")
    monkeypatch.setenv("AGENT_FLOW_AGENT_ID", "")

    assert cli_main(["abort", "--root", str(leader), "--yes"]) == 0

    assert not (run_dir / "active").exists()
    assert json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))["status"] == "aborted"


@pytest.mark.git_auth
def test_abort_recovers_deleted_bound_workspace(
    pinned_run: tuple[Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader, worktree, _runtime, run_dir = pinned_run
    shutil.rmtree(worktree)
    monkeypatch.setenv("AGENT_FLOW_ACTIVE_HOST", "omp")
    monkeypatch.setenv("AGENT_FLOW_EXECUTION_ID", "session-1")
    monkeypatch.setenv("AGENT_FLOW_AGENT_ID", "")
    execution = ExecutionIdentity(host="omp", session_id="session-1", agent_id="")
    binding = (
        leader / ".git" / "agent-flow" / "executions" / f"{execution.digest}.json"
    )
    assert binding.exists()

    assert cli_main(["abort", "--root", str(leader), "--yes"]) == 0

    assert not (run_dir / "active").exists()
    assert json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))["status"] == "aborted"
    assert not binding.exists()


@pytest.mark.git_auth
def test_repin_rolls_back_all_records_on_mid_loop_failure(
    pinned_run: tuple[Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader, worktree, runtime, run_dir = pinned_run
    displaced = worktree.with_name(".feat-test-displaced")
    worktree.rename(displaced)
    shutil.copytree(displaced, worktree)
    shutil.rmtree(displaced)
    execution = ExecutionIdentity(host="omp", session_id="session-1", agent_id="")

    def stored_inode(path: Path, key: str) -> int:
        return workspace_boundary.workspace_identity_from_dict(
            json.loads(path.read_text(encoding="utf-8"))[key]
        ).inode

    original_inode = stored_inode(runtime / "manifest.json", "identity")
    binding_dir = leader / ".git" / "agent-flow" / "executions"

    real_replace = workspace_boundary._replace_owned_json_atomic
    calls = {"n": 0}

    def flaky(path, expected, payload):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("injected failure on the 2nd repin record")
        return real_replace(path, expected, payload)

    monkeypatch.setattr(workspace_boundary, "_replace_owned_json_atomic", flaky)

    with pytest.raises(OSError, match="injected failure on the 2nd repin record"):
        workspace_boundary.repin_workspace_identity(leader, "feat-test", execution)

    # Every record must still carry the original (stale) inode: the partially
    # committed record was rolled back, so no mixed old/new-inode state remains.
    assert stored_inode(runtime / "manifest.json", "identity") == original_inode
    for binding in binding_dir.glob("*.json"):
        assert stored_inode(binding, "workspace") == original_inode
    assert stored_inode(run_dir / "meta.json", "workspace") == original_inode


@pytest.mark.git_auth
def test_repin_rejects_advanced_head(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, runtime, _run_dir = pinned_run
    displaced = worktree.with_name(".feat-test-displaced")
    worktree.rename(displaced)
    shutil.copytree(displaced, worktree)
    shutil.rmtree(displaced)
    (worktree / "advance.txt").write_text("more\n", encoding="utf-8")
    _git(worktree, "add", "advance.txt")
    _git(worktree, "commit", "-m", "advance head")
    execution = ExecutionIdentity(host="omp", session_id="session-1", agent_id="")

    original_inode = workspace_boundary.workspace_identity_from_dict(
        json.loads((runtime / "manifest.json").read_text(encoding="utf-8"))["identity"]
    ).inode

    with pytest.raises(
        workspace_boundary.WorkspaceBoundaryError,
        match="repin requires an unchanged HEAD",
    ):
        workspace_boundary.repin_workspace_identity(leader, "feat-test", execution)

    assert (
        workspace_boundary.workspace_identity_from_dict(
            json.loads((runtime / "manifest.json").read_text(encoding="utf-8"))["identity"]
        ).inode
        == original_inode
    )


def test_replace_owned_json_anchors_writes_to_parent_dirfd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    target = real / "meta.json"
    target.write_text(json.dumps({"v": 1}), encoding="utf-8")
    attacker = tmp_path / "attacker"
    attacker.mkdir()

    real_fsync = os.fsync
    state = {"swapped": False}

    def swapping_fsync(fd: int) -> None:
        real_fsync(fd)
        if state["swapped"]:
            return
        state["swapped"] = True
        # Simulate a parent-directory symlink swap landing after the pre-checks:
        # the path now resolves into `attacker`, but the pinned dir fd must keep
        # create+rename inside the original directory inode.
        os.rename(real, tmp_path / "real-moved")
        os.symlink(attacker, real)

    monkeypatch.setattr(os, "fsync", swapping_fsync)

    workspace_boundary._replace_owned_json_atomic(target, {"v": 1}, {"v": 2})

    moved = tmp_path / "real-moved"
    assert json.loads((moved / "meta.json").read_text(encoding="utf-8")) == {"v": 2}
    assert not (attacker / "meta.json").exists()


@pytest.mark.git_auth
def test_launcher_authentication_rejects_shadowing_and_shell_chains(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"

    allowed = _bash_guard(leader, leader, f"{launcher} status")
    worktree_list = _bash_guard(leader, leader, f"{launcher} worktree list")
    worktree_from_linked = _bash_guard(
        leader,
        worktree,
        f"{launcher} worktree list",
    )
    apk_export = _bash_guard(leader, worktree, f"{launcher} export-apk app-debug.apk")
    stale_abort = _bash_guard(leader, leader, f"{launcher} abort --yes")
    shadowed = _bash_guard(leader, worktree, "PATH=. agent-flow status")
    chained = _bash_guard(leader, leader, f"{launcher} status\ntouch shared.txt")

    assert allowed.returncode == 0, allowed.stderr
    assert worktree_list.returncode == 0, worktree_list.stderr
    assert apk_export.returncode == 0, apk_export.stderr
    assert stale_abort.returncode == 0, stale_abort.stderr
    assert worktree_from_linked.returncode == 2
    assert "launcher is not trusted" in worktree_from_linked.stderr
    for result in (shadowed, chained):
        assert result.returncode == 2
        assert "launcher is not trusted" in result.stderr


@pytest.mark.git_auth
def test_managed_context_marker_remains_immutable(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run
    target = worktree / "AGENTS.md"
    target.write_text(
        "user text\n"
        "<!-- agent-flow:start -->\n"
        "managed text\n"
        "<!-- agent-flow:end -->\n",
        encoding="utf-8",
    )

    allowed = _structured_guard(
        leader,
        worktree,
        "Edit",
        {
            "file_path": str(target),
            "old_string": "user text",
            "new_string": "changed user text",
        },
    )
    blocked = _structured_guard(
        leader,
        worktree,
        "Edit",
        {
            "file_path": str(target),
            "old_string": "managed text",
            "new_string": "changed managed text",
        },
    )

    assert allowed.returncode == 0, allowed.stderr
    assert blocked.returncode == 2
    assert "managed marker block is immutable" in blocked.stderr


@pytest.mark.git_auth
def test_shell_mutation_paths_remain_confined_to_the_worktree(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run

    allowed = _bash_guard(leader, worktree, "touch generated.txt")
    blocked = _bash_guard(leader, worktree, f"touch {leader / 'shared.txt'}")

    assert allowed.returncode == 0, allowed.stderr
    assert blocked.returncode == 2
    assert "target_outside_pinned_workspace" in blocked.stderr


@pytest.mark.git_auth
def test_stateless_guard_rejects_symlinked_git_marker_and_escaping_run_area(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, worktree, _runtime, _run_dir = pinned_run

    git_marker = worktree / ".git"
    real_marker = worktree / ".git-real"
    git_marker.rename(real_marker)
    git_marker.symlink_to(real_marker)
    linked_git = _structured_guard(
        leader, worktree, "write",
        {"path": str(worktree / "shared.txt"), "content": "changed\n"},
    )
    git_marker.unlink()
    real_marker.rename(git_marker)

    outside = leader.parent / "outside-runs"
    outside.mkdir(exist_ok=True)
    evil = leader / ".git" / "agent-flow" / "worktrees" / "evil"
    evil.symlink_to(outside, target_is_directory=True)
    escaped_area = _structured_guard(
        leader, leader, "write",
        {"path": str(evil / ".agent-flow" / "runs" / "r" / "design.md"), "content": "x\n"},
    )

    assert linked_git.returncode == 2
    assert "git checkout marker is a symlink" in linked_git.stderr
    assert escaped_area.returncode == 2
    assert "target_outside_pinned_workspace" in escaped_area.stderr



@pytest.mark.parametrize("active_count", (9, 10, 11))
def test_parallel_execution_threshold_resolves_only_owned_worktrees(
    tmp_path: Path,
    active_count: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real git worktrees + identities are created, but validate_workspace_identity's
    # per-call git re-validation is swapped for the cached-identity seam so the
    # O(N^2) mutation-path assertions below do not spawn hundreds of git processes.
    monkeypatch.setattr(
        workspace_boundary,
        "validate_workspace_identity",
        _fast_validate_workspace_identity,
    )
    leader = tmp_path / "project"
    leader.mkdir()
    subprocess.run(("git", "init", "-b", "main", str(leader)), check=True, capture_output=True)
    _git(leader, "config", "user.name", "Test User")
    _git(leader, "config", "user.email", "test@example.com")
    (leader / "shared.txt").write_text("leader\n", encoding="utf-8")
    _git(leader, "add", "shared.txt")
    _git(leader, "commit", "-m", "initial")
    leader_head = _git(leader, "rev-parse", "HEAD")

    worktrees: list[Path] = []
    executions: list[ExecutionIdentity] = []
    for index in range(active_count):
        worktree = leader / ".agent-flow" / "worktrees" / f"task-{index}"
        _git(leader, "worktree", "add", "-b", f"feat/task-{index}", str(worktree), "main")
        run_dir = leader / ".git" / "agent-flow" / "worktrees" / f"task-{index}" / ".agent-flow" / "runs" / f"run-{index}"
        run_dir.mkdir(parents=True)
        (run_dir / "active").write_text("", encoding="utf-8")
        worktree_metadata = worktree.stat()
        identity = workspace_boundary.WorkspaceIdentity(
            workspace_root=str(worktree.resolve()),
            git_common_dir=str((leader / ".git").resolve()),
            git_dir=str((leader / ".git" / "worktrees" / f"task-{index}").resolve()),
            branch=f"feat/task-{index}",
            head=leader_head,
            device=worktree_metadata.st_dev,
            inode=worktree_metadata.st_ino,
        )
        (run_dir / "meta.json").write_text(
            json.dumps({"run_id": f"run-{index}", "workspace": identity.to_dict()}),
            encoding="utf-8",
        )
        execution = ExecutionIdentity("codex", f"session-{index}", "")
        bindings_root = leader / ".git" / "agent-flow" / "executions"
        bindings_root.mkdir(parents=True, exist_ok=True)
        (bindings_root / f"{execution.digest}.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "execution": execution.to_dict(),
                    "workspace": identity.to_dict(),
                    "workspace_name": worktree.name,
                    "run_id": f"run-{index}",
                    "run_dir": str(run_dir.resolve()),
                    "bound_at": "2026-07-14T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
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


@pytest.mark.git_auth
def test_resolve_mutation_path_allows_pinned_run_dir(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, _worktree, _runtime, run_dir = pinned_run
    active = resolve_execution_workspace(
        leader, ExecutionIdentity("codex", "session-1", "")
    )
    target = run_dir / "design.md"
    assert resolve_mutation_path(
        active.identity,
        target,
        host="codex",
        phase="design",
        run_dir=active.run_dir,
    ) == target.resolve()
    with pytest.raises(Exception, match="target_outside_pinned_workspace"):
        resolve_mutation_path(
            active.identity,
            target,
            host="codex",
            phase="design",
        )
    with pytest.raises(Exception, match="target_outside_pinned_workspace"):
        resolve_mutation_path(
            active.identity,
            leader / "escape.txt",
            host="codex",
            phase="design",
            run_dir=active.run_dir,
        )










def test_omp_extension_source_normalizes_cwd_and_xd_writes() -> None:
    source = (KIT_ROOT / "bin" / "agent-flow-kit.mjs").read_text(encoding="utf-8")
    assert "normalizeGuardInput" in source
    assert "XD_FILE_MUTATORS" in source
    assert "path.isAbsolute(declaredCwd)" in source
    assert "ast_edit(args)" in source


@pytest.mark.git_auth
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


@pytest.mark.git_auth
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


@pytest.mark.git_auth
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


@pytest.mark.git_auth
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






































@pytest.mark.git_auth
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












































































































@pytest.mark.git_auth
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












@pytest.mark.git_auth
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


@pytest.mark.git_auth
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


@pytest.mark.git_auth
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
