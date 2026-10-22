"""Linux Landlock write confinement for read-only provider runs (#422)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_flow.core import worktrees as W
from agent_flow.core.worktree_isolation import WorktreeIsolationError
from agent_flow.providers import landlock_launch
from agent_flow.providers.subprocess import (
    ProviderCommand,
    _LANDLOCK_LAUNCHER,
    run_provider,
    verify_provider_sandbox_backend,
)

LANDLOCK = pytest.mark.skipif(
    not sys.platform.startswith("linux") or landlock_launch.abi_version() < 1,
    reason="Linux Landlock is required",
)

# 각 시도는 실패해야 참이다. 대상은 argv로 받는다.
ESCAPES = r"""
import json, os, subprocess, sys
from pathlib import Path
scratch, outside = map(Path, sys.argv[1:3])
def denied(action):
    try:
        action()
        return False
    except OSError:
        return True
existing = outside / "existing.txt"
link = scratch / "out-link"
os.symlink(existing, link)
(scratch / "moving.txt").write_text("m")
child = subprocess.run([sys.executable, "-c",
    "import sys; open(sys.argv[1], 'w').write('child')", str(outside / "child.txt")])
result = {
    "write": denied(lambda: existing.write_text("leak")),
    "create": denied(lambda: (outside / "new.txt").write_text("leak")),
    "truncate": denied(lambda: os.truncate(existing, 0)),
    "symlink": denied(lambda: link.write_text("leak")),
    "mkdir": denied(lambda: (outside / "dir").mkdir()),
    "rename-out": denied(lambda: os.rename(scratch / "moving.txt", outside / "moved.txt")),
    "rename-in": denied(lambda: os.rename(existing, scratch / "taken.txt")),
    "remove": denied(lambda: existing.unlink()),
    "git-pointer": denied(lambda: (outside / ".git").write_text("gitdir: /elsewhere")),
    "host-state": denied(lambda: (outside / ".codex" / "config.toml").write_text("leak")),
    "child": child.returncode != 0,
    "read-allowed": existing.read_text() == "outside",
    "devnull-allowed": not denied(lambda: open(os.devnull, "w").write("x")),
}
(scratch / "inside.txt").write_text("inside")
(scratch / "result.json").write_text(json.dumps(result))
"""


def _launch(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(_LANDLOCK_LAUNCHER), *args],
        capture_output=True, text=True, timeout=60,
    )


@LANDLOCK
def test_launcher_denies_every_write_outside_scratch_for_the_whole_tree(tmp_path):
    scratch, outside = tmp_path / "scratch", tmp_path / "outside"
    scratch.mkdir()
    (outside / ".codex").mkdir(parents=True)
    (outside / "existing.txt").write_text("outside")
    (outside / ".git").write_text("gitdir: /original")

    run = _launch("--write", str(scratch), "--", sys.executable, "-c", ESCAPES, str(scratch), str(outside))

    assert run.returncode == 0, run.stderr
    result = json.loads((scratch / "result.json").read_text())
    assert result == {key: True for key in result}, result
    assert (scratch / "inside.txt").read_text() == "inside"
    assert (outside / "existing.txt").read_text() == "outside"
    assert (outside / ".git").read_text() == "gitdir: /original"
    assert sorted(path.name for path in outside.iterdir()) == [".codex", ".git", "existing.txt"]
    assert list((outside / ".codex").iterdir()) == []


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--write"],
        ["--write", "relative", "--", "/bin/true"],
        ["--write", "/does/not/exist", "--", "/bin/true"],
        ["--", "/bin/true"],
        ["--write", "/tmp", "/bin/true"],
        ["--write", "/tmp", "--"],
    ],
)
def test_launcher_refuses_bad_arguments_before_running_anything(args):
    run = _launch(*args)

    assert run.returncode == 126
    assert "provider not started" in run.stderr


def test_launcher_fails_closed_without_landlock(tmp_path, monkeypatch):
    executed = []
    monkeypatch.setattr(landlock_launch, "abi_version", lambda libc=None: 0)
    monkeypatch.setattr(landlock_launch.os, "execv", lambda *args: executed.append(args))

    with pytest.raises(SystemExit) as exit_info:
        landlock_launch.main(["--write", str(tmp_path), "--", "/bin/true"])

    assert exit_info.value.code == 126
    assert executed == []


def test_read_only_runs_need_landlock_on_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("agent_flow.providers.subprocess._landlock_abi", lambda: 0)

    with pytest.raises(WorktreeIsolationError, match="Landlock is unavailable"):
        verify_provider_sandbox_backend(allow_workspace_writes=False)
    # Landlock은 허용한 디렉터리 아래 경로를 다시 막지 못한다. 쓰는 run은 여전히 backend가 없다.
    monkeypatch.setattr("agent_flow.providers.subprocess._landlock_abi", lambda: 7)
    with pytest.raises(WorktreeIsolationError, match="for this operating system"):
        verify_provider_sandbox_backend()
    assert verify_provider_sandbox_backend(allow_workspace_writes=False) == _LANDLOCK_LAUNCHER


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@LANDLOCK
def test_read_only_provider_cannot_write_its_checkout_leader_or_sibling(tmp_path):
    _git("init", "-q", "-b", "main", cwd=tmp_path)
    _git("config", "user.email", "t@example.com", cwd=tmp_path)
    _git("config", "user.name", "t", cwd=tmp_path)
    (tmp_path / ".gitignore").write_text(".agent-flow/\n")
    _git("add", ".gitignore", cwd=tmp_path)
    _git("commit", "-q", "-m", "init", cwd=tmp_path)
    worker = W.create_worktree(root=tmp_path, plan=W.plan_worktree(root=tmp_path, name="worker"))
    sibling = W.create_worktree(root=tmp_path, plan=W.plan_worktree(root=tmp_path, name="sibling"))
    git_pointer = (worker.path / ".git").read_bytes()
    program = (
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "def denied(path):\n"
        "    try:\n"
        "        Path(path).write_text('leak')\n"
        "        return False\n"
        "    except OSError:\n"
        "        return True\n"
        "scratch = Path(os.environ['TMPDIR'])\n"
        "result = {name: denied(path) for name, path in zip(\n"
        "    ('checkout', 'git-pointer', 'leader', 'sibling'), sys.argv[1:5])}\n"
        "(scratch / 'probe.txt').write_text('scratch')\n"
        "print(json.dumps(result))\n"
    )

    result = run_provider(
        ProviderCommand(
            name="read-only-probe",
            argv=(sys.executable, "-c", program, "inside.txt", ".git",
                  str(tmp_path / "leader-leak.txt"), str(sibling.path / "sibling-leak.txt")),
            allow_workspace_writes=False,
        ),
        prompt="",
        cwd=worker.path,
    )

    assert result.failed is False, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "checkout": True, "git-pointer": True, "leader": True, "sibling": True,
    }
    assert not (worker.path / "inside.txt").exists()
    assert (worker.path / ".git").read_bytes() == git_pointer
    assert not (tmp_path / "leader-leak.txt").exists()
    assert not (sibling.path / "sibling-leak.txt").exists()
