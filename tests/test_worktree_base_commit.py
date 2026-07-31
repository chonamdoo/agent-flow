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

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core import worktrees as worktrees_module
from agent_flow.core.commands import SafeCommandResult
from agent_flow.core.worktree_isolation import WorktreeIsolationError
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


def _declare_profile(root: Path, profile_id: str) -> None:
    kit = root / ".agent-flow"
    kit.mkdir(parents=True, exist_ok=True)
    (kit / "kit.json").write_text(
        json.dumps({"kit": "agent-flow", "profiles": [profile_id]}) + "\n", encoding="utf-8"
    )


def test_profile_declared_base_wins_over_the_name_list(tmp_path: Path):
    """불변: profile이 base를 지명하면 worktree는 거기서 갈라진다.

    이름 목록(`main` 우선)만 보면 release-first/gitflow 저장소가 선언과 다른 줄기에서
    작업을 시작하고, PR target과 base가 어긋난 채로 리뷰까지 간다.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    _git("branch", "develop", cwd=root)
    _git("checkout", "develop", cwd=root)
    (root / "f.txt").write_text("develop only\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "develop ahead", cwd=root)
    develop_tip = _git("rev-parse", "develop", cwd=root)
    _git("checkout", "main", cwd=root)
    assert develop_tip != _git("rev-parse", "main", cwd=root)
    # spring profile이 `branching.base: develop`을 선언한다.
    _declare_profile(root, "spring")

    status = create_worktree(root=root, plan=plan_worktree(root=root, name="feat"))

    assert _git("rev-parse", "HEAD", cwd=status.path) == develop_tip
    assert _manifest(root, "feat-feat")["base_oid"] == develop_tip


def test_forced_profile_base_wins_over_installed_profile(
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    _git("branch", "develop", cwd=root)
    _git("checkout", "develop", cwd=root)
    (root / "f.txt").write_text("develop only\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "develop advance", cwd=root)
    develop_tip = _git("rev-parse", "HEAD", cwd=root)
    _git("checkout", "main", cwd=root)
    _declare_profile(root, "android")
    monkeypatch.setenv("AGENT_FLOW_PROFILE", "spring")

    status = create_worktree(root=root, plan=plan_worktree(root=root, name="feat"))

    assert _git("rev-parse", "HEAD", cwd=status.path) == develop_tip
    assert _manifest(root, "feat-feat")["base_oid"] == develop_tip


@pytest.mark.parametrize("forced", [True, False], ids=["env-profile", "kit-profile"])
def test_unknown_profile_fallback_uses_the_generic_base_override(
    tmp_path: Path,
    monkeypatch,
    forced: bool,
):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    _git("branch", "develop", cwd=root)
    _git("checkout", "develop", cwd=root)
    (root / "f.txt").write_text("develop only\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "develop advance", cwd=root)
    develop_tip = _git("rev-parse", "HEAD", cwd=root)
    _git("checkout", "main", cwd=root)
    _declare_profile(root, "android" if forced else "missing")
    (root / ".agent-flow" / "profiles").mkdir(parents=True, exist_ok=True)
    (root / ".agent-flow" / "profiles" / "generic.local.yaml").write_text(
        "branching:\n"
        "  base: develop\n",
        encoding="utf-8",
    )
    if forced:
        monkeypatch.setenv("AGENT_FLOW_PROFILE", "missing")
    else:
        monkeypatch.setenv("AGENT_FLOW_FALLBACK_GENERIC", "1")

    status = create_worktree(root=root, plan=plan_worktree(root=root, name="feat"))

    assert _git("rev-parse", "HEAD", cwd=status.path) == develop_tip
    assert _manifest(root, "feat-feat")["base_oid"] == develop_tip


def test_missing_declared_base_is_rejected(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    _declare_profile(root, "spring")

    with pytest.raises(
        WorktreeIsolationError,
        match="profile base branch is unavailable: develop",
    ):
        plan_worktree(root=root, name="feat")


def test_custom_installed_profile_declared_base_wins_over_main(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    _git("branch", "develop", cwd=root)
    _git("checkout", "develop", cwd=root)
    (root / "f.txt").write_text("custom develop\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "custom develop ahead", cwd=root)
    develop_tip = _git("rev-parse", "develop", cwd=root)
    _git("checkout", "main", cwd=root)
    _declare_profile(root, "my-stack")
    profiles = root / ".agent-flow" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "my-stack.yaml").write_text(
        "id: my-stack\n"
        "branching:\n"
        "  base: develop\n"
        "  integration: develop\n"
        "pr:\n"
        "  target_branch: develop\n"
        "  merge_strategy: merge\n",
        encoding="utf-8",
    )

    status = create_worktree(root=root, plan=plan_worktree(root=root, name="feat"))

    assert _git("rev-parse", "HEAD", cwd=status.path) == develop_tip


def test_profile_base_ignores_same_named_tag_when_only_remote_branch_exists(
    tmp_path: Path,
):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    tag_tip = _git("rev-parse", "main", cwd=root)
    _git("checkout", "-b", "release", cwd=root)
    (root / "f.txt").write_text("remote release\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "release ahead", cwd=root)
    release_tip = _git("rev-parse", "HEAD", cwd=root)
    _git("checkout", "main", cwd=root)
    _git("remote", "add", "upstream", str(root), cwd=root)
    _git("update-ref", "refs/remotes/upstream/release", release_tip, cwd=root)
    _git("branch", "-D", "release", cwd=root)
    _git("tag", "release", tag_tip, cwd=root)
    _declare_profile(root, "my-stack")
    profiles = root / ".agent-flow" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "my-stack.yaml").write_text(
        "id: my-stack\n"
        "branching:\n"
        "  base: release\n"
        "  integration: release\n"
        "pr:\n"
        "  target_branch: release\n"
        "  merge_strategy: merge\n",
        encoding="utf-8",
    )

    status = create_worktree(root=root, plan=plan_worktree(root=root, name="feat"))

    assert tag_tip != release_tip
    assert _git("rev-parse", "HEAD", cwd=status.path) == release_tip
    assert _manifest(root, "feat-feat")["base_ref"] == "refs/remotes/upstream/release"


def test_profile_base_fetches_a_missing_remote_tracking_ref(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--bare", cwd=remote)
    _git("remote", "add", "upstream", str(remote), cwd=root)
    _git("checkout", "-b", "release", cwd=root)
    (root / "f.txt").write_text("remote release\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "release ahead", cwd=root)
    release_tip = _git("rev-parse", "HEAD", cwd=root)
    _git("push", "upstream", "release:refs/heads/release", cwd=root)
    _git("checkout", "main", cwd=root)
    _git("branch", "-D", "release", cwd=root)
    _git("update-ref", "-d", "refs/remotes/upstream/release", cwd=root)
    _declare_profile(root, "my-stack")
    profiles = root / ".agent-flow" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "my-stack.yaml").write_text(
        "id: my-stack\n"
        "branching:\n"
        "  base: release\n"
        "  integration: release\n"
        "pr:\n"
        "  target_branch: release\n"
        "  merge_strategy: merge\n",
        encoding="utf-8",
    )

    status = create_worktree(root=root, plan=plan_worktree(root=root, name="feat"))

    assert _git("rev-parse", "HEAD", cwd=status.path) == release_tip
    assert _manifest(root, "feat-feat")["base_ref"] == "refs/remotes/upstream/release"


def test_profile_base_fetch_retries_remote_ref_lock_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--bare", cwd=remote)
    _git("remote", "add", "upstream", str(remote), cwd=root)
    _git("checkout", "-b", "release", cwd=root)
    (root / "f.txt").write_text("remote release\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "release ahead", cwd=root)
    release_tip = _git("rev-parse", "HEAD", cwd=root)
    _git("push", "upstream", "release:refs/heads/release", cwd=root)
    _git("checkout", "main", cwd=root)
    _git("branch", "-D", "release", cwd=root)
    _git("update-ref", "-d", "refs/remotes/upstream/release", cwd=root)
    _declare_profile(root, "my-stack")
    profiles = root / ".agent-flow" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "my-stack.yaml").write_text(
        "id: my-stack\n"
        "branching:\n"
        "  base: release\n"
        "  integration: release\n"
        "pr:\n"
        "  target_branch: release\n"
        "  merge_strategy: merge\n",
        encoding="utf-8",
    )
    real_git_safe = worktrees_module.git_safe
    fetch_attempts = 0

    def contend_once(*args, **kwargs):
        nonlocal fetch_attempts
        if args and args[0] == "fetch":
            fetch_attempts += 1
            if fetch_attempts == 1:
                return SafeCommandResult(
                    args=tuple(args),
                    returncode=128,
                    stdout="",
                    stderr="fatal: cannot lock ref 'refs/remotes/upstream/release'",
                )
        return real_git_safe(*args, **kwargs)

    monkeypatch.setattr(worktrees_module, "git_safe", contend_once)

    status = create_worktree(root=root, plan=plan_worktree(root=root, name="feat"))

    assert fetch_attempts == 2
    assert _git("rev-parse", "HEAD", cwd=status.path) == release_tip
