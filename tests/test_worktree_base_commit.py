"""worktree가 어느 커밋에서 갈라졌는지 기록이 남고 그 값이 앵커로 쓸 만한지 본다.

`WorktreeStatus.base_oid`는 있지만 그 값을 단언하는 테스트가 없었다. 구현이 있어도
검증이 없으면 앵커가 조용히 이름 기반으로 되돌아가거나, merge-base가 아니라 움직이는
브랜치 tip을 담게 되어도 아무도 모른다.

`base_ref`(브랜치 이름)만으로는 앵커가 안 된다 — leader가 그 뒤로 커밋을 쌓으면
"이 세션이 바꾼 것"이 따라 줄어든다. 그래서 커밋을 박는지까지 본다.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.worktrees import (
    create_worktree,
    plan_worktree,
    worktree_runtime_root,
)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ("git", *args), cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_repo(root: Path) -> None:
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "f.txt").write_text("base\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "init", cwd=root)


def _manifest(root: Path, name: str) -> dict:
    path = worktree_runtime_root(root=root, name=name) / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_manifest_records_the_commit_the_worktree_started_from(tmp_path: Path):
    """불변: base가 기록되지 않으면 누적 diff를 물을 기준이 없다."""
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    expected = _git("rev-parse", "HEAD", cwd=root)

    create_worktree(root=root, plan=plan_worktree(root=root, name="feat"))

    assert _manifest(root, "feat-feat")["base_oid"] == expected


def test_base_commit_survives_the_base_branch_moving_on(tmp_path: Path):
    """불변: 앵커는 이름이 아니라 커밋이다.

    브랜치 이름을 기록하면 leader가 그 뒤로 커밋을 쌓는 순간 앵커가 따라 움직여
    "이 세션이 바꾼 것"이 조용히 줄어든다.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    started_at = _git("rev-parse", "HEAD", cwd=root)

    create_worktree(root=root, plan=plan_worktree(root=root, name="feat"))

    (root / "f.txt").write_text("leader moved on\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "later", cwd=root)
    assert _git("rev-parse", "HEAD", cwd=root) != started_at

    assert _manifest(root, "feat-feat")["base_oid"] == started_at


def test_worktree_is_created_from_the_resolved_commit(tmp_path: Path):
    """불변: 기록한 커밋과 실제로 갈라진 지점이 같아야 앵커가 쓸모 있다."""
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)

    status = create_worktree(root=root, plan=plan_worktree(root=root, name="feat"))

    recorded = _manifest(root, "feat-feat")["base_oid"]
    actual = _git("rev-parse", "HEAD", cwd=status.path)
    assert actual == recorded
