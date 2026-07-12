from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from agent_flow.artifact import create_run, find_active_run
from agent_flow.cli import (
    _find_node_active_run,
    _find_project_active_run,
    _preflight_installed_skill_snapshot,
    main,
)
from agent_flow.core.start_lock import START_LOCK_KEYS, project_start_lock, project_start_lock_path
from agent_flow.core.worktrees import worktree_runtime_root


KIT_ROOT = Path(__file__).resolve().parents[1]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(("git", *args), cwd=root, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout
    return result


def _init_git(root: Path) -> None:
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "init")


def test_python_required_profile_rejects_non_git_run_and_start(tmp_path: Path) -> None:
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        assert main(["run", "required task", "--root", str(tmp_path), "--workflow", "default"]) == 2
        assert main(["start", "development", "--root", str(tmp_path), "--task", "required task"]) == 2
    assert stderr.getvalue().count("requires a registered git worktree") == 2
    assert not (tmp_path / ".agent-flow/worktrees").exists()


def test_python_disabled_profile_can_start_on_non_git_leader(tmp_path: Path) -> None:
    with mock.patch("agent_flow.cli._profile_worktree_mode", return_value="disabled"), mock.patch.dict(
        os.environ,
        {
            "AGENT_FLOW_ADAPTER": "generic",
            "AGENT_FLOW_GENERIC_MODE": "stub-success",
        },
    ), contextlib.redirect_stdout(io.StringIO()):
        assert main(["run", "disabled task", "--root", str(tmp_path), "--workflow", "default"]) == 0
    assert not (tmp_path / ".agent-flow/state/start.lock").exists()
    metas = list((tmp_path / ".agent-flow/runs").glob("*/meta.json"))
    assert len(metas) == 1
    assert json.loads(metas[0].read_text(encoding="utf-8"))["worktree_mode"] == "disabled"


def test_python_compatibility_start_rejects_second_run_for_implicit_or_explicit_worktree(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(
            [
                "start",
                "development",
                "--root",
                str(tmp_path),
                "--task",
                "first prompt",
                "--adapter",
                "manual",
                "--run-id",
                "compat-active",
            ]
        ) == 0

    first = _find_project_active_run(tmp_path)
    assert first is not None
    worktree_name, active = first
    assert worktree_name == "feat-first-prompt"
    assert active.run_id == "compat-active"
    assert active.path == (
        worktree_runtime_root(root=tmp_path, name=worktree_name)
        / ".agent-flow/runs/development/compat-active"
    )

    for explicit_worktree in (None, "feat-first-prompt", "second-prompt"):
        command = [
            "start",
            "development",
            "--root",
            str(tmp_path),
            "--task",
            "second prompt",
            "--adapter",
            "manual",
        ]
        if explicit_worktree is not None:
            command.extend(("--worktree", explicit_worktree))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            assert main(command) == 2

        assert "already active: compat-active" in output.getvalue()
        assert "--worktree feat-first-prompt" in output.getvalue()
    assert not (tmp_path / ".agent-flow/worktrees/feat-second-prompt").exists()
    assert not worktree_runtime_root(root=tmp_path, name="feat-second-prompt").exists()


def test_python_compatibility_start_rejects_a_tampered_nested_manifest_before_worktree_creation(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(
            [
                "start",
                "development",
                "--root",
                str(tmp_path),
                "--task",
                "first prompt",
                "--adapter",
                "manual",
                "--run-id",
                "compat-active",
            ]
        ) == 0
    manifest = (
        worktree_runtime_root(root=tmp_path, name="feat-first-prompt")
        / ".agent-flow/runs/development/compat-active/manifest.json"
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["run_id"] = "forged"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        assert main(
            [
                "start",
                "development",
                "--root",
                str(tmp_path),
                "--task",
                "second prompt",
                "--adapter",
                "manual",
            ]
        ) == 2

    assert "Python run manifest identity mismatch" in stderr.getvalue()
    assert not (tmp_path / ".agent-flow/worktrees/feat-second-prompt").exists()
    assert not worktree_runtime_root(root=tmp_path, name="feat-second-prompt").exists()


@pytest.mark.parametrize("entrypoint", ("run", "start"))
def test_python_start_validates_managed_provenance_before_host_bridge(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    _init_git(tmp_path)
    install = subprocess.run(
        ("node", str(KIT_ROOT / "bin/agent-flow-kit.mjs"), "install", "--profile", "generic"),
        cwd=tmp_path,
        env={**os.environ, "AGENT_FLOW_SKIP_CODEX_TRUST": "1"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert install.returncode == 0, install.stderr
    existing_worktree = tmp_path / ".agent-flow/worktrees/feat-existing"
    if entrypoint == "start":
        _git(
            tmp_path,
            "worktree",
            "add",
            "-b",
            "feat/existing",
            str(existing_worktree),
            "main",
        )
    reviewer = tmp_path / ".Codex/agents/code-reviewer.md"
    reviewer.write_text(reviewer.read_text(encoding="utf-8") + "\nforged\n", encoding="utf-8")
    command = (
        ["run", "tampered start", "--root", str(tmp_path), "--workflow", "default"]
        if entrypoint == "run"
        else [
            "start",
            "development",
            "--root",
            str(tmp_path),
            "--task",
            "tampered start",
            "--adapter",
            "manual",
            "--worktree",
            "existing",
        ]
    )

    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        assert main(command) == 2

    assert "installed managed host file changed" in stderr.getvalue()
    assert not (tmp_path / ".agent-flow/worktrees/feat-tampered-start").exists()
    assert not worktree_runtime_root(root=tmp_path, name="feat-tampered-start").exists()
    assert not (tmp_path / ".git/agent-flow/worktree-host-bridges").exists()
    if entrypoint == "start":
        assert existing_worktree.is_dir()
        assert not (existing_worktree / ".Codex").exists()
        assert not (existing_worktree / ".claude").exists()
        assert not (existing_worktree / ".omp").exists()


def test_python_new_runs_suffix_completed_semantic_worktree_collisions_from_old_cwd(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    environment = {
        "AGENT_FLOW_ADAPTER": "generic",
        "AGENT_FLOW_GENERIC_MODE": "stub-success",
    }
    first_output = io.StringIO()
    with mock.patch.dict(os.environ, environment), contextlib.redirect_stdout(first_output):
        assert main(
            [
                "run",
                "checkout.png payment bug",
                "--root",
                str(tmp_path),
                "--workflow",
                "default",
            ]
        ) == 0
    first_match = _find_project_active_run(tmp_path)
    assert first_match is not None
    first_name, first_active = first_match
    assert first_name == "feat-payment-bug"
    assert f"--worktree {first_name}" in first_output.getvalue()
    first_worktree = tmp_path / ".agent-flow" / "worktrees" / first_name
    (first_active.path / "active").unlink()

    second_output = io.StringIO()
    with mock.patch.dict(os.environ, environment), contextlib.redirect_stdout(second_output):
        assert main(
            [
                "run",
                "checkout.png payment bug",
                "--root",
                str(tmp_path),
                "--workflow",
                "default",
            ]
        ) == 0
    second_match = _find_project_active_run(tmp_path)
    assert second_match is not None
    second_name, second_active = second_match
    assert second_name == "feat-payment-bug-2"
    assert f"--worktree {second_name}" in second_output.getvalue()
    assert "--worktree feat-payment-bug\n" not in second_output.getvalue()
    (second_active.path / "active").unlink()

    third_output = io.StringIO()
    with mock.patch.dict(os.environ, environment), contextlib.redirect_stdout(third_output):
        assert main(
            [
                "run",
                "payment bug",
                "--root",
                str(first_worktree),
                "--workflow",
                "default",
            ]
        ) == 0
    third_match = _find_project_active_run(tmp_path)
    assert third_match is not None
    third_name, _third_active = third_match
    assert third_name == "feat-payment-bug-3"
    assert f"--worktree {third_name}" in third_output.getvalue()


def test_python_collision_suffix_truncates_non_bmp_slugs_by_code_point(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    task = "𐐀" * 60
    normalized = task.lower()
    environment = {
        "AGENT_FLOW_ADAPTER": "generic",
        "AGENT_FLOW_GENERIC_MODE": "stub-success",
    }
    with mock.patch.dict(os.environ, environment), contextlib.redirect_stdout(io.StringIO()):
        assert main(["run", task, "--root", str(tmp_path), "--workflow", "default"]) == 0
    first_match = _find_project_active_run(tmp_path)
    assert first_match is not None
    first_name, first_active = first_match
    assert first_name == f"feat-{normalized}"
    (first_active.path / "active").unlink()

    with mock.patch.dict(os.environ, environment), contextlib.redirect_stdout(io.StringIO()):
        assert main(["run", task, "--root", str(tmp_path), "--workflow", "default"]) == 0
    second_match = _find_project_active_run(tmp_path)
    assert second_match is not None
    second_name, _second_active = second_match
    assert second_name == f"feat-{normalized[:58]}-2"
    assert len(second_name.removeprefix("feat-")) == 60


def test_python_named_current_worktree_is_reused_until_it_has_run_history(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _init_git(root)
    current_worktree = tmp_path / "existing"
    _git(root, "worktree", "add", "-b", "feat/existing", str(current_worktree), "HEAD")
    environment = {
        "AGENT_FLOW_ADAPTER": "generic",
        "AGENT_FLOW_GENERIC_MODE": "stub-success",
    }

    old_cwd = Path.cwd()
    try:
        os.chdir(current_worktree)
        with mock.patch.dict(os.environ, environment), contextlib.redirect_stdout(io.StringIO()):
            assert main(["run", "follow up", "--workflow", "default"]) == 0
    finally:
        os.chdir(old_cwd)

    first_match = _find_project_active_run(root)
    assert first_match is not None
    first_name, first_active = first_match
    assert first_name == "feat-existing"
    first_manifest = json.loads(
        (worktree_runtime_root(root=root, name=first_name) / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    first_path = Path(first_manifest["path"])
    if not first_path.is_absolute():
        first_path = root / first_path
    assert first_path.resolve() == current_worktree.resolve()
    assert not (root / ".agent-flow/worktrees/feat-existing-2").exists()
    (first_active.path / "active").unlink()

    try:
        os.chdir(current_worktree)
        with mock.patch.dict(os.environ, environment), contextlib.redirect_stdout(io.StringIO()):
            assert main(["run", "follow up", "--workflow", "default"]) == 0
    finally:
        os.chdir(old_cwd)

    second_match = _find_project_active_run(root)
    assert second_match is not None
    second_name, second_active = second_match
    assert second_name == "feat-existing-2"
    second_manifest = json.loads(
        (worktree_runtime_root(root=root, name=second_name) / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    second_path = Path(second_manifest["path"])
    if not second_path.is_absolute():
        second_path = root / second_path
    assert second_path.resolve() != current_worktree.resolve()
    assert (root / ".agent-flow/worktrees/feat-existing-2").is_dir()


def test_python_lock_schema_and_cross_runtime_block_before_worktree(tmp_path: Path) -> None:
    _init_git(tmp_path)
    install = subprocess.run(
        ("node", str(KIT_ROOT / "bin/agent-flow-kit.mjs"), "install"),
        cwd=tmp_path,
        env={**os.environ, "AGENT_FLOW_SKIP_CODEX_TRUST": "1"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert install.returncode == 0, install.stderr
    with project_start_lock(tmp_path, runtime="python") as lock_path:
        assert lock_path == tmp_path.resolve() / ".git/agent-flow/start.lock"
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert tuple(payload) == START_LOCK_KEYS
        assert project_start_lock_path(tmp_path) == lock_path

        node = subprocess.run(
            (
                "node",
                str(KIT_ROOT / "bin/agent-flow-kit.mjs"),
                "run",
                "start",
                "--task",
                "node contender",
                "--workflow",
                "default",
            ),
            cwd=tmp_path,
            env={**os.environ, "AGENT_FLOW_SKIP_CODEX_TRUST": "1"},
            text=True,
            capture_output=True,
            check=False,
        )
        assert node.returncode != 0
        assert "start lock exists" in node.stderr
        assert not (tmp_path / ".agent-flow/worktrees/feat-node-contender").exists()

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            assert main(["run", "python contender", "--root", str(tmp_path), "--workflow", "default"]) == 2
        assert "start lock exists" in stderr.getvalue()
        assert not (tmp_path / ".agent-flow/worktrees/feat-python-contender").exists()
    assert not lock_path.exists()


def test_python_status_rejects_required_leader_active_run(tmp_path: Path) -> None:
    _init_git(tmp_path)
    create_run(tmp_path, "default", "leader active")
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        assert main(["status", "--root", str(tmp_path)]) == 2
    assert "leader checkout" in stderr.getvalue()


def test_python_status_rejects_unregistered_and_detached_active_worktree(tmp_path: Path) -> None:
    _init_git(tmp_path)
    with mock.patch.dict(
        os.environ,
        {"AGENT_FLOW_ADAPTER": "generic", "AGENT_FLOW_GENERIC_MODE": "stub-success"},
    ), contextlib.redirect_stdout(io.StringIO()):
        assert main(["run", "workspace task", "--root", str(tmp_path), "--workflow", "default"]) == 0
    worktree = tmp_path / ".agent-flow/worktrees/feat-workspace-task"
    _git(worktree, "switch", "--detach")
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        assert main(["status", "--root", str(tmp_path)]) == 2
    assert "detached HEAD" in stderr.getvalue()
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        assert main(["continue", "--root", str(tmp_path), "--worktree", "workspace task"]) == 2
    assert "detached HEAD" in stderr.getvalue()

    _git(tmp_path, "branch", "develop", "main")
    _git(worktree, "switch", "develop")
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        assert main(["status", "--root", str(tmp_path)]) == 2
    assert "protected branch develop" in stderr.getvalue()

    _git(tmp_path, "worktree", "remove", "--force", str(worktree))
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        assert main(["status", "--root", str(tmp_path)]) == 2
    assert "not a registered git worktree" in stderr.getvalue()


def test_node_start_is_blocked_after_python_start_lock_is_released(tmp_path: Path) -> None:
    _init_git(tmp_path)
    install = subprocess.run(
        ("node", str(KIT_ROOT / "bin/agent-flow-kit.mjs"), "install"),
        cwd=tmp_path,
        env={**os.environ, "AGENT_FLOW_SKIP_CODEX_TRUST": "1"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert install.returncode == 0, install.stderr
    with mock.patch.dict(
        os.environ,
        {
            "AGENT_FLOW_ADAPTER": "generic",
            "AGENT_FLOW_GENERIC_MODE": "stub-success",
            "AGENT_FLOW_SKIP_CODEX_TRUST": "1",
        },
    ), contextlib.redirect_stdout(io.StringIO()):
        assert main(["run", "python active", "--root", str(tmp_path), "--workflow", "default"]) == 0
    assert not project_start_lock_path(tmp_path).exists()

    node = subprocess.run(
        (
            "node",
            str(KIT_ROOT / "bin/agent-flow-kit.mjs"),
            "run",
            "start",
            "--task",
            "node later",
            "--workflow",
            "default",
        ),
        cwd=tmp_path,
        env={**os.environ, "AGENT_FLOW_SKIP_CODEX_TRUST": "1"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert node.returncode != 0
    assert "active run" in node.stderr
    assert not (tmp_path / ".agent-flow/worktrees/feat-node-later").exists()


def test_python_multiple_active_runs_fail_closed_within_and_across_worktrees(
    tmp_path: Path,
) -> None:
    same_root = tmp_path / "same"
    create_run(same_root, "default", "first")
    duplicate = same_root / ".agent-flow/runs/duplicate"
    duplicate.mkdir(parents=True)
    (duplicate / "active").write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="multiple active Python runs"):
        find_active_run(same_root)

    project = tmp_path / "project"
    project.mkdir()
    _init_git(project)
    for name, task in (("feat-a", "run a"), ("feat-b", "run b")):
        create_run(worktree_runtime_root(root=project, name=name), "default", task)
    with pytest.raises(RuntimeError, match="across project worktrees"):
        _find_project_active_run(project)
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        assert main(["status", "--root", str(project)]) == 2
    assert "multiple active Python runs" in stderr.getvalue()


def test_python_active_run_discovery_rejects_symlinked_runtime_state_root(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _init_git(project)
    outside = tmp_path / "outside"
    create_run(outside, "default", "outside")
    runtime_root = worktree_runtime_root(root=project, name="feat-evil").parent
    runtime_root.mkdir(parents=True, exist_ok=True)
    (runtime_root / "feat-evil").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="unsafe agent-flow worktree runtime entry"):
        _find_project_active_run(project)


def test_python_active_run_discovery_rejects_symlinked_legacy_run(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    outside = tmp_path / "outside"
    outside_run = create_run(outside, "default", "outside")
    runs_root = state_root / ".agent-flow" / "runs"
    runs_root.mkdir(parents=True)
    (runs_root / "evil").symlink_to(outside_run, target_is_directory=True)

    with pytest.raises(RuntimeError, match="unsafe Python run root"):
        find_active_run(state_root)


def test_python_active_run_discovery_rejects_symlinked_agent_flow_parent(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    create_run(outside, "default", "outside")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".agent-flow").symlink_to(
        outside / ".agent-flow",
        target_is_directory=True,
    )

    with pytest.raises(RuntimeError, match="unsafe Python state root"):
        find_active_run(project)
    with pytest.raises(RuntimeError, match="unsafe"):
        _find_project_active_run(project)


def test_python_worktree_discovery_rejects_symlinked_checkout_state_parent(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _init_git(project)
    outside = tmp_path / "outside-agent-flow"
    (outside / "worktrees" / "feat-external").mkdir(parents=True)
    (project / ".agent-flow").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="unsafe agent-flow worktree checkout state root"):
        _find_project_active_run(project)


def test_python_node_run_discovery_rejects_symlinked_state_ancestors(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    external_run = outside / ".agent-flow" / "runs" / "default" / "evil"
    external_run.mkdir(parents=True)
    (external_run / "manifest.json").write_text(
        json.dumps(
            {
                "workflow": "default",
                "run_id": "evil",
                "run_dir": ".agent-flow/runs/default/evil",
                "status": "running",
            }
        ),
        encoding="utf-8",
    )
    parent_link = tmp_path / "parent-link"
    parent_link.mkdir()
    (parent_link / ".agent-flow").symlink_to(
        outside / ".agent-flow",
        target_is_directory=True,
    )
    with pytest.raises(RuntimeError, match="unsafe Node state root"):
        _find_node_active_run(parent_link)

    workflow_link = tmp_path / "workflow-link"
    runs_root = workflow_link / ".agent-flow" / "runs"
    runs_root.mkdir(parents=True)
    (runs_root / "default").symlink_to(
        outside / ".agent-flow" / "runs" / "default",
        target_is_directory=True,
    )
    with pytest.raises(RuntimeError, match="unsafe Node workflow run root"):
        _find_node_active_run(workflow_link)


def test_python_active_run_discovery_ignores_harmless_regular_files(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _init_git(project)
    runtime_root = worktree_runtime_root(root=project, name="feat-safe").parent
    runtime_root.mkdir(parents=True, exist_ok=True)
    (runtime_root / ".DS_Store").write_bytes(b"metadata")
    runs_root = project / ".agent-flow" / "runs"
    runs_root.mkdir(parents=True)
    (runs_root / "README").write_text("runtime notes\n", encoding="utf-8")

    assert _find_project_active_run(project) is None


@pytest.mark.parametrize(
    ("relative", "message"),
    (
        (Path(".agent-flow/kit.json"), "skill index is missing"),
        (Path(".agent-flow/skills/index.json"), "kit metadata is missing"),
    ),
)
def test_python_command_preflight_rejects_partial_installed_provenance(
    tmp_path: Path,
    relative: Path,
    message: str,
) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=message):
        _preflight_installed_skill_snapshot(tmp_path)


@pytest.mark.parametrize("command", ("status", "run"))
def test_python_commands_fail_closed_during_install_transaction(
    tmp_path: Path,
    command: str,
) -> None:
    _init_git(tmp_path)
    transaction_root = tmp_path / ".agent-flow" / "install-transaction"
    transaction_root.mkdir(parents=True)
    argv = [command, "--root", str(tmp_path)]
    if command == "run":
        argv.insert(1, "blocked while installing")
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        assert main(argv) == 2
    assert "install transaction is in progress or requires recovery" in stderr.getvalue()
    assert not (tmp_path / ".agent-flow" / "runs").exists()


@pytest.mark.parametrize(
    "journal",
    (
        {"version": 2, "status": "committed"},
        {"version": 3, "status": "committed"},
        {"version": 4, "status": "committed"},
        {"version": 4, "status": "committed", "extra": True},
        {"version": 4, "status": "committed", "codex_trust": {"version": 1}},
    ),
)
def test_python_status_rejects_every_committed_install_cleanup_remnant(
    tmp_path: Path,
    journal: dict[str, object],
) -> None:
    _init_git(tmp_path)
    transaction_root = tmp_path / ".agent-flow" / "install-transaction"
    transaction_root.mkdir(parents=True)
    (transaction_root / "journal.json").write_text(
        f"{json.dumps(journal)}\n",
        encoding="utf-8",
    )

    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        assert main(["status", "--root", str(tmp_path)]) == 2
    assert "install transaction is in progress or requires recovery" in stderr.getvalue()


def test_python_legacy_start_rolls_back_if_install_begins_after_run_publication(
    tmp_path: Path,
) -> None:
    with mock.patch("agent_flow.cli._profile_worktree_mode", return_value="disabled"), mock.patch(
        "agent_flow.runner.assert_no_install_transaction",
        side_effect=RuntimeError("blocked: concurrent install"),
    ), mock.patch.dict(
        os.environ,
        {"AGENT_FLOW_ADAPTER": "generic", "AGENT_FLOW_GENERIC_MODE": "stub-success"},
    ), contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        assert main(["run", "race task", "--root", str(tmp_path), "--workflow", "default"]) == 2

    assert list((tmp_path / ".agent-flow" / "runs").glob("*")) == []


def test_python_compatibility_start_rolls_back_if_install_begins_after_run_publication(
    tmp_path: Path,
) -> None:
    checks = 0

    def transaction_check(_root: Path) -> None:
        nonlocal checks
        checks += 1
        if checks == 3:
            raise RuntimeError("blocked: concurrent install")

    with mock.patch("agent_flow.cli._profile_worktree_mode", return_value="disabled"), mock.patch(
        "agent_flow.cli.assert_no_install_transaction",
        side_effect=transaction_check,
    ), contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        assert main(
            [
                "start",
                "development",
                "--root",
                str(tmp_path),
                "--task",
                "race task",
                "--adapter",
                "manual",
            ]
        ) == 2

    assert checks == 3
    assert list((tmp_path / ".agent-flow" / "runs").glob("*/*")) == []
