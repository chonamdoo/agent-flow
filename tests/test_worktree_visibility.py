from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agent_flow.artifact import create_run, mark_inactive
from agent_flow.cli import main
from agent_flow.core.worktrees import (
    adopt_worktree,
    create_worktree,
    plan_worktree,
    worktree_runtime_root,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=root, check=True, capture_output=True, text=True, timeout=30
    ).stdout


@pytest.fixture
def leader(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "file.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


@pytest.fixture
def checkout(leader: Path) -> Path:
    return create_worktree(
        root=leader, plan=plan_worktree(root=leader, name="visible")
    ).path


@pytest.fixture
def editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "editor-bin"
    directory.mkdir()
    output = tmp_path / "editor-call.json"
    script = (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        f"Path({str(output)!r}).write_text(json.dumps({{"
        "'argv': sys.argv[1:], 'cwd': os.getcwd(), "
        "'git_dir': os.environ.get('GIT_DIR'), "
        "'git_work_tree': os.environ.get('GIT_WORK_TREE')"
        "}), encoding='utf-8')\n"
        "sys.exit(int(os.environ.get('TEST_EDITOR_EXIT', '0')))\n"
    )
    for name in ("code", "cursor", "zed"):
        executable = directory / name
        executable.write_text(script, encoding="utf-8")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ["PATH"])
    return output


def _snapshot(root: Path, checkout: Path) -> tuple[bytes, bytes, str, str, dict[str, bytes], bytes]:
    runtime = worktree_runtime_root(root=root, name=checkout.name)
    files = {
        str(path.relative_to(runtime)): path.read_bytes()
        for path in runtime.rglob("*") if path.is_file()
    }
    checkout_git = Path(_git(checkout, "rev-parse", "--absolute-git-dir").strip())
    return (
        (root / ".git" / "index").read_bytes(),
        (checkout_git / "index").read_bytes(),
        _git(root, "show-ref"),
        _git(root, "worktree", "list", "--porcelain"),
        files,
        (checkout / "file.txt").read_bytes(),
    )


def test_detail_separates_code_and_runtime_without_mutation(
    leader: Path, checkout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = worktree_runtime_root(root=leader, name=checkout.name)
    first = create_run(runtime, "review", "first task", run_id="first")
    mark_inactive(first)
    create_run(runtime, "review", "second\ntask", run_id="second")
    (first / "active").touch()
    (checkout / "untracked.txt").write_text("keep", encoding="utf-8")
    before = _snapshot(leader, checkout)

    assert main(["worktree", "status", "--root", str(leader), "--name", checkout.name, "--detail"]) == 0

    out = capsys.readouterr().out
    assert f"code_path: {checkout.resolve()}" in out
    assert f"runtime_path: {runtime}" in out
    assert "recorded_active_run: review/first task: first task" in out
    assert "recorded_active_run: review/second task: second\\ntask" in out
    assert _snapshot(leader, checkout) == before
    assert (checkout / "untracked.txt").read_text() == "keep"


def test_plain_status_output_is_unchanged(
    leader: Path, checkout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["worktree", "status", "--root", str(leader), "--name", checkout.name]) == 0
    assert capsys.readouterr().out == f"{checkout.name} feat/visible {checkout} exists\n"


@pytest.mark.parametrize("layout", ["legacy", "sibling"])
@pytest.mark.parametrize("command", ["status", "open"])
def test_same_basename_never_displays_another_checkouts_runtime(
    leader: Path, checkout: Path, editor: Path, capsys: pytest.CaptureFixture[str],
    layout: str, command: str,
) -> None:
    runtime = worktree_runtime_root(root=leader, name=checkout.name)
    create_run(runtime, "review", "managed-only-task", run_id="owner")
    other_root = (
        leader / ".agent-flow" / "worktrees"
        if layout == "legacy"
        else leader.parent / f"{leader.name}.worktrees"
    )
    other_root.mkdir(parents=True, exist_ok=True)
    other = other_root / checkout.name
    _git(leader, "worktree", "add", "-b", "feat/other", str(other), "main")
    before = _snapshot(leader, other)
    args = ["worktree", command, "--root", str(leader), "--name", str(other)]
    args += ["--detail"] if command == "status" else ["--editor", "code"]

    assert main(args) == (0 if layout == "legacy" else 2)

    out = capsys.readouterr().out
    if layout == "legacy":
        assert f"code_path: {other.resolve()}" in out
        assert "runtime_path: unknown (ownership mismatch)" in out
        assert "recorded_active_runs: unavailable" in out
    assert str(runtime) not in out
    assert "managed-only-task" not in out
    assert _snapshot(leader, other) == before
    if command == "open" and layout == "legacy":
        assert json.loads(editor.read_text())["argv"] == ["--new-window", str(other.resolve())]
    else:
        assert not editor.exists()
    assert main(["worktree", "status", "--root", str(leader), "--name", str(checkout), "--detail"]) == 0
    assert "recorded_active_run: review/owner task: managed-only-task" in capsys.readouterr().out


def test_detail_without_active_runs(
    leader: Path, checkout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["worktree", "status", "--root", str(leader), "--name", checkout.name, "--detail"]) == 0
    assert "recorded_active_runs: none" in capsys.readouterr().out


@pytest.mark.parametrize("manifest", ["missing", "malformed"])
def test_detail_does_not_attribute_unowned_runtime(
    leader: Path, checkout: Path, capsys: pytest.CaptureFixture[str], manifest: str
) -> None:
    runtime = worktree_runtime_root(root=leader, name=checkout.name)
    create_run(runtime, "review", "unproven-task")
    manifest_path = runtime / "manifest.json"
    if manifest == "missing":
        manifest_path.unlink()
    else:
        manifest_path.write_text("{}", encoding="utf-8")
    before = _snapshot(leader, checkout)

    assert main(["worktree", "status", "--root", str(leader), "--name", str(checkout), "--detail"]) == 0

    out = capsys.readouterr().out
    assert f"code_path: {checkout.resolve()}" in out
    assert "runtime_path: unknown (ownership mismatch)" in out
    assert "recorded_active_runs: unavailable" in out
    assert "unproven-task" not in out
    assert _snapshot(leader, checkout) == before


@pytest.mark.parametrize("name, flag", [("code", "--new-window"), ("cursor", "--new-window"), ("zed", "-n")])
def test_open_uses_selected_checkout_and_new_window(
    leader: Path, checkout: Path, editor: Path, name: str, flag: str
) -> None:
    before = _snapshot(leader, checkout)
    assert main(["worktree", "open", "--root", str(leader), "--name", "feat/visible", "--editor", name]) == 0
    call = json.loads(editor.read_text())
    assert call["argv"] == [flag, str(checkout.resolve())]
    assert call["cwd"] == str(checkout.resolve())
    assert _snapshot(leader, checkout) == before


@pytest.mark.parametrize("layout", ["legacy", "adopted"])
def test_open_supports_existing_layouts_and_literal_paths(
    leader: Path, tmp_path: Path, editor: Path, layout: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checkout = (
        leader / ".agent-flow" / "worktrees" / "feat-old"
        if layout == "legacy"
        else tmp_path / "outside space 한글" / "feat-literal;echo"
    )
    checkout.parent.mkdir(parents=True)
    _git(leader, "worktree", "add", "-b", "feat/old", str(checkout), "main")
    if layout == "adopted":
        adopt_worktree(root=leader, path=checkout)
    assert main(["worktree", "open", "--root", str(leader), "--name", str(checkout), "--editor", "code"]) == 0
    assert json.loads(editor.read_text())["argv"] == ["--new-window", str(checkout.resolve())]
    if layout == "legacy":
        assert "legacy checkout is inside the leader" in capsys.readouterr().err


@pytest.mark.parametrize("case", ["missing", "leader", "unadopted", "stale", "foreign", "ambiguous"])
@pytest.mark.parametrize("command", ["status", "open"])
def test_invalid_selection_never_opens_or_recreates(
    leader: Path, tmp_path: Path, editor: Path, case: str, command: str
) -> None:
    selected = tmp_path / "external" / "same"
    selected.parent.mkdir()
    if case == "missing":
        selector = "missing"
    elif case == "leader":
        selector = str(leader)
    elif case == "foreign":
        selected.mkdir()
        _git(selected, "init", "-b", "main")
        selector = str(selected)
    else:
        _git(leader, "worktree", "add", "-b", "feat/one", str(selected), "main")
        selector = str(selected)
        if case == "stale":
            adopt_worktree(root=leader, path=selected)
            shutil.rmtree(selected)
        elif case == "ambiguous":
            other = tmp_path / "other" / "same"
            other.parent.mkdir()
            _git(leader, "worktree", "add", "-b", "feat/two", str(other), "main")
            selector = "same"
    before = _git(leader, "worktree", "list", "--porcelain")
    args = ["worktree", command, "--root", str(leader), "--name", selector]
    args += ["--detail"] if command == "status" else ["--editor", "code"]

    assert main(args) == 2

    assert not editor.exists()
    assert _git(leader, "worktree", "list", "--porcelain") == before
    if case == "stale":
        assert not selected.exists()
    assert not (leader / ".agent-flow" / "worktrees").exists()


@pytest.mark.parametrize("failure", ["unsupported", "missing", "nonzero"])
def test_editor_failure_keeps_code_path_available(
    leader: Path, checkout: Path, editor: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], failure: str,
) -> None:
    name = "vim" if failure == "unsupported" else "code"
    if failure == "missing":
        monkeypatch.setattr("agent_flow.cli.shutil.which", lambda name: None)
    if failure == "nonzero":
        monkeypatch.setenv("TEST_EDITOR_EXIT", "7")
    assert main(["worktree", "open", "--root", str(leader), "--name", checkout.name, "--editor", name]) == 2
    captured = capsys.readouterr()
    assert f"code_path: {checkout.resolve()}" in captured.out
    assert "editor_launch_requested" not in captured.out
    assert captured.err
    if failure == "nonzero":
        assert editor.is_file()
        assert "exit status 7" in captured.err


def test_open_clears_git_discovery_environment(
    leader: Path, checkout: Path, editor: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_DIR", str(leader / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(leader))
    assert main(["worktree", "open", "--root", str(leader), "--name", checkout.name, "--editor", "code"]) == 0
    call = json.loads(editor.read_text())
    assert call["git_dir"] is None
    assert call["git_work_tree"] is None


@pytest.mark.parametrize("location", ["leader", "checkout"])
def test_open_refuses_editor_from_checkout(
    leader: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    directory = leader if location == "leader" else checkout
    executable = directory / "code"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ["PATH"])
    assert main(["worktree", "open", "--root", str(leader), "--name", checkout.name, "--editor", "code"]) == 2


def test_open_does_not_kill_a_foreground_editor(
    leader: Path, checkout: Path, editor: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = subprocess.Popen
    calls = []

    class ForegroundEditor:
        def wait(self, timeout: float) -> int:
            raise subprocess.TimeoutExpired("code", timeout)

    def launch(args, **kwargs):
        if Path(args[0]).name == "code":
            calls.append(kwargs)
            return ForegroundEditor()
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)
    assert main(["worktree", "open", "--root", str(leader), "--name", checkout.name, "--editor", "code"]) == 0
    assert calls[0]["stdin"] == subprocess.DEVNULL
    assert calls[0]["stdout"] == subprocess.DEVNULL
    assert calls[0]["stderr"] == subprocess.DEVNULL
    assert calls[0]["start_new_session"] == (os.name != "nt")
    assert calls[0]["close_fds"] is True
    assert "editor_launch_requested: code" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["init", "handoff"])
def test_python_initialization_does_not_create_legacy_worktrees(
    leader: Path, command: str
) -> None:
    args = [command, "--root", str(leader)]
    if command == "handoff":
        run_dir = create_run(leader, "review", "handoff")
        args += ["--run-dir", str(run_dir), "--from-stage", "review", "--to-stage", "qa"]
    assert main(args) == 0
    assert not (leader / ".agent-flow" / "worktrees").exists()


@pytest.mark.parametrize("entry", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
@pytest.mark.parametrize("git_project", [False, True])
def test_install_does_not_create_unused_worktree_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, git_project: bool
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    project = tmp_path / "install-target"
    project.mkdir()
    if git_project:
        _git(project, "init", "-b", "main")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENT_FLOW_SKIP_CODEX_TRUST", "1")

    result = subprocess.run(
        (node, str(REPO / "bin" / entry), "install"), cwd=project,
        capture_output=True, text=True, check=False, timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert (project / ".agent-flow" / "kit.json").is_file()
    assert not (project / ".agent-flow" / "worktrees").exists()
    if not git_project:
        runtime = worktree_runtime_root(root=project, name="later")
        create_run(runtime, "review", "lazy runtime")
        assert (runtime / ".agent-flow" / "runs").is_dir()


@pytest.mark.parametrize("entry", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_install_preserves_existing_legacy_worktree_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    project = tmp_path / "existing"
    legacy = project / ".agent-flow" / "worktrees" / "feat-old" / "local.txt"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"uncommitted legacy data")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENT_FLOW_SKIP_CODEX_TRUST", "1")
    result = subprocess.run(
        (node, str(REPO / "bin" / entry), "install"), cwd=project,
        capture_output=True, text=True, check=False, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert legacy.read_bytes() == b"uncommitted legacy data"
