from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


KIT_ROOT = Path(__file__).resolve().parent.parent
GUARD = KIT_ROOT / "scripts" / "hooks" / "guard-worktree-write.py"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *args),
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


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
    return leader, worktree, runtime, run_dir


def _guard(
    leader: Path,
    target: Path,
    *,
    host: str = "codex",
    phase: str = "implement",
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(KIT_ROOT / "src")
    payload = {
        "tool_name": "apply_patch",
        "cwd": str(leader),
        "host": host,
        "phase": phase,
        "tool_input": {
            "patch": f"*** Begin Patch\n*** Update File: {target}\n@@\n-old\n+new\n*** End Patch"
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
    decisions = {
        host: (
            _guard(leader, worktree / "shared.txt", host=host).returncode,
            _guard(leader, leader / "shared.txt", host=host).returncode,
        )
        for host in ("codex", "claude", "omp")
    }

    assert decisions == {"codex": (0, 2), "claude": (0, 2), "omp": (0, 2)}


def test_leader_status_reuses_the_active_pinned_worktree(
    pinned_run: tuple[Path, Path, Path, Path],
) -> None:
    leader, _worktree, _runtime, _run_dir = pinned_run
    env = dict(os.environ)
    env["PYTHONPATH"] = str(KIT_ROOT / "src")

    result = subprocess.run(
        (sys.executable, "-m", "agent_flow.cli", "status", "--root", str(leader)),
        cwd=leader,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--worktree feat-test" in result.stdout


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
