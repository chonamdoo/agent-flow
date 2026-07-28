"""제거·조회 대상은 이름이 아니라 git이 보고한 경로다 (issue #110).

workflow 프롬프트는 사용자에게 raw ``git worktree add``를 시킨다. 그렇게 만든
체크아웃에는 agent-flow manifest가 없고, 이름도 ``feat-`` 정규화 규칙과 무관하다.
여기 있는 케이스는 전부 이름 역산 조회로 되돌리면 실패한다 — 그게 이 파일의 존재
이유다. 안전 방향(잠금·leader·모호성)도 각각 반증 짝을 갖는다.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[1]
SRC = str(KIT_ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.worktree_isolation import (
    WorktreeIsolationError,
    list_registered_worktrees,
    real_path,
)
from agent_flow.core import worktrees as W
from agent_flow.core.worktrees import (
    removable_worktrees,
    resolve_worktree,
)


def _git(*args, cwd) -> subprocess.CompletedProcess[str]:
    return subprocess.run(("git", *args), cwd=str(cwd), capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "f.txt").write_text("base\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "init", cwd=root)


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("AGENT_FLOW_") or key in {"CLAUDECODE", "CLAUDE_CLI", "CODEX_CLI"}:
            env.pop(key, None)
    env["PYTHONPATH"] = SRC
    env["AGENT_FLOW_ADAPTER"] = "generic"
    env["AGENT_FLOW_GENERIC_MODE"] = "stub-success"
    return subprocess.run(
        [sys.executable, "-m", "agent_flow.cli", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )


def _branch_exists(root: Path, branch: str) -> bool:
    return _git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", cwd=root).returncode == 0


def _registered_paths(root: Path) -> set[Path]:
    return {entry.path for entry in list_registered_worktrees(root)}


def _add_raw_worktree(root: Path, branch: str, path: Path) -> None:
    """workflow 프롬프트가 사용자에게 시키는 그대로. agent-flow manifest는 없다."""
    result = _git("worktree", "add", "-b", branch, str(path), "main", cwd=root)
    assert result.returncode == 0, result.stderr


def test_raw_worktree_in_managed_dir_is_listed_and_removed(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    checkout = root / ".agent-flow" / "worktrees" / "feat-demo"
    _add_raw_worktree(root, "feat/demo", checkout)

    listed = _run_cli(["worktree", "list"], root)
    assert listed.returncode == 0, listed.stderr
    assert "feat-demo feat/demo" in listed.stdout
    assert "exists" in listed.stdout

    removed = _run_cli(["worktree", "remove", "--name", "feat-demo"], root)
    assert removed.returncode == 0, removed.stderr
    assert not checkout.exists()
    assert real_path(checkout) not in _registered_paths(root)
    # agent-flow가 만들지 않은 브랜치는 남는다.
    assert _branch_exists(root, "feat/demo")


def test_raw_worktree_in_managed_dir_is_removable_by_absolute_path(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    checkout = root / ".agent-flow" / "worktrees" / "feat-demo"
    _add_raw_worktree(root, "feat/demo", checkout)

    removed = _run_cli(["worktree", "remove", "--name", str(checkout)], root)
    assert removed.returncode == 0, removed.stderr
    assert not checkout.exists()
    assert real_path(checkout) not in _registered_paths(root)


def test_raw_worktree_outside_managed_dir_is_listed_and_removed(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    checkout = tmp_path / "elsewhere" / "wt"
    _add_raw_worktree(root, "feat/outside", checkout)

    listed = _run_cli(["worktree", "list"], root)
    assert listed.returncode == 0, listed.stderr
    assert str(real_path(checkout)) in listed.stdout
    assert "wt feat/outside" in listed.stdout

    removed = _run_cli(["worktree", "remove", "--name", "wt"], root)
    assert removed.returncode == 0, removed.stderr
    assert not checkout.exists()
    assert real_path(checkout) not in _registered_paths(root)
    assert _branch_exists(root, "feat/outside")


def test_outside_worktree_is_removable_by_its_branch(tmp_path: Path):
    """이슈에 실린 실제 사례: 디렉터리 ``feat-issue`` + 브랜치 ``<user>/feat-issue``."""
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    checkout = tmp_path / "workspaces" / "feat-issue"
    _add_raw_worktree(root, "chonamdoo/feat-issue", checkout)

    removed = _run_cli(["worktree", "remove", "--name", "chonamdoo/feat-issue"], root)
    assert removed.returncode == 0, removed.stderr
    assert not checkout.exists()
    assert real_path(checkout) not in _registered_paths(root)
    assert _branch_exists(root, "chonamdoo/feat-issue")


def test_name_normalization_cannot_reach_the_outside_worktree(tmp_path: Path):
    """반증 짝: 이름 역산 경로에는 아무것도 없고, 매칭은 등록부하고만 일어난다."""
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    _add_raw_worktree(root, "feat/outside", tmp_path / "elsewhere" / "wt")

    assert not (root / ".agent-flow" / "worktrees" / "feat-wt").exists()
    assert resolve_worktree(root=root, selector="feat-wt") is None
    assert resolve_worktree(root=root, selector="unrelated") is None
    missing = _run_cli(["worktree", "remove", "--name", "feat-wt"], root)
    assert missing.returncode == 1
    assert "worktree not found" in missing.stderr
    # 등록부에 있는 식별자는 정확히 그 항목으로 해석된다.
    assert resolve_worktree(root=root, selector="wt").branch == "feat/outside"


def test_exact_selector_beats_a_derived_alias(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    derived = root / ".agent-flow" / "worktrees" / "feat-demo"
    literal = tmp_path / "elsewhere" / "demo"
    _add_raw_worktree(root, "feat/demo", derived)
    _add_raw_worktree(root, "feat/literal", literal)

    # 사용자가 입력한 실제 디렉터리 이름이 정규화로 유도한 alias보다 우선한다.
    assert resolve_worktree(root=root, selector="demo").path == real_path(literal)
    assert resolve_worktree(root=root, selector="feat-demo").path == real_path(derived)
    assert resolve_worktree(root=root, selector="feat/literal").path == real_path(literal)


def test_locked_worktree_survives_allow_unmerged(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    checkout = root / ".agent-flow" / "worktrees" / "feat-demo"
    _add_raw_worktree(root, "feat/demo", checkout)
    (checkout / "dirty.txt").write_text("unstaged\n", encoding="utf-8")
    assert _git("worktree", "lock", str(checkout), cwd=root).returncode == 0

    result = _run_cli(["worktree", "remove", "--name", "feat-demo", "--allow-unmerged"], root)
    assert result.returncode == 2
    assert "locked" in result.stderr
    assert "git worktree unlock" in result.stderr
    assert checkout.exists()
    assert real_path(checkout) in _registered_paths(root)

    # 반증 짝: 잠금을 풀면 같은 명령이 통과한다.
    assert _git("worktree", "unlock", str(checkout), cwd=root).returncode == 0
    unlocked = _run_cli(["worktree", "remove", "--name", "feat-demo", "--allow-unmerged"], root)
    assert unlocked.returncode == 0, unlocked.stderr
    assert not checkout.exists()


def test_locked_worktree_with_dead_checkout_is_not_wiped(tmp_path: Path):
    """잔재 정리 경로도 잠금을 넘지 않는다."""
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    checkout = root / ".agent-flow" / "worktrees" / "feat-demo"
    _add_raw_worktree(root, "feat/demo", checkout)
    (checkout / ".git").unlink()
    assert _git("worktree", "lock", str(checkout), cwd=root).returncode == 0

    result = _run_cli(["worktree", "remove", "--name", "feat-demo"], root)
    assert result.returncode == 2
    assert "locked" in result.stderr
    assert checkout.exists()


def test_leader_is_never_resolved_as_a_target(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    linked = root / ".agent-flow" / "worktrees" / "feat-demo"
    _add_raw_worktree(root, "feat/demo", linked)

    for selector in (str(root), str(real_path(root)), ".", "main", root.name):
        assert resolve_worktree(root=root, selector=selector) is None, selector
    assert real_path(root) not in {entry.path for entry in removable_worktrees(root=root)}

    result = _run_cli(["worktree", "remove", "--name", str(root)], root)
    assert result.returncode != 0
    assert (root / "f.txt").exists()
    assert real_path(root) in _registered_paths(root)

    # 반증 짝: leader가 아닌 linked worktree는 같은 경로 선택자로 잡힌다.
    assert resolve_worktree(root=root, selector=str(linked)).path == real_path(linked)


def test_leader_removal_is_refused_from_a_linked_worktree(tmp_path: Path):
    """워커 cwd에서도 leader는 후보가 아니다."""
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    linked = root / ".agent-flow" / "worktrees" / "feat-demo"
    _add_raw_worktree(root, "feat/demo", linked)

    assert resolve_worktree(root=linked, selector=str(root)) is None
    assert resolve_worktree(root=linked, selector=str(linked)).path == real_path(linked)


def test_registered_listing_failure_is_not_an_empty_list(tmp_path: Path):
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    with pytest.raises(WorktreeIsolationError):
        list_registered_worktrees(outside)

    # 저장소인데 git이 대답하지 못하는 상태는 "등록된 worktree가 없다"와 다르다.
    broken = tmp_path / "broken"
    broken.mkdir()
    _init_repo(broken)
    (broken / ".git" / "config").write_text("[core\nnot valid ini\n", encoding="utf-8")

    with pytest.raises(WorktreeIsolationError):
        list_registered_worktrees(broken)
    with pytest.raises(WorktreeIsolationError):
        removable_worktrees(root=broken)
    with pytest.raises(WorktreeIsolationError):
        resolve_worktree(root=broken, selector="feat-demo")

    # 반증 짝: 파일시스템이 non-repo를 증명한 자리에서만 빈 목록으로 접는다.
    assert removable_worktrees(root=outside) == []
    assert resolve_worktree(root=outside, selector="feat-demo") is None


def test_removing_a_colliding_external_worktree_keeps_managed_metadata(tmp_path):
    """불변: 이름이 같은 키로 접히는 외부 worktree를 지워도 관리형 상태는 남는다.

    런타임 메타데이터 키는 정규화된 이름이라 서로 다른 등록 경로가 한 키로 접힌다
    — 관리형 ``.../feat-demo``와 외부 ``.../demo``가 둘 다 ``feat-demo``다. 경로
    소유 증명 없이 지우면 외부 worktree 제거가 관리형 worktree의 활성 run을
    통째로 날린다.
    """
    _init_repo(tmp_path)
    plan = W.plan_worktree(root=tmp_path, name="demo")
    managed = W.create_worktree(root=tmp_path, plan=plan)
    runtime_root = W.worktree_runtime_root(root=tmp_path, name=managed.name)
    active_run = runtime_root / ".agent-flow" / "runs" / "live"
    active_run.mkdir(parents=True)
    (active_run / "active").write_text("1\n", encoding="utf-8")

    external = tmp_path.parent / "elsewhere" / "demo"
    external.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-q", "-b", "other/demo", str(external), "main", cwd=tmp_path)

    status = W.get_worktree_status(root=tmp_path, name=str(external))
    # 충돌은 표시 이름이 아니라 런타임 메타데이터 **키**에서 난다. 전제를 고정해
    # 두지 않으면 정규화 규칙이 바뀔 때 이 테스트가 조용히 무의미해진다.
    assert W._feature_worktree_name(status.name) == W._feature_worktree_name(
        managed.name
    ), "이 테스트는 메타데이터 키가 실제로 충돌할 때만 의미가 있다"
    W.remove_worktree(root=tmp_path, status=status, allow_unmerged=True)

    assert not external.exists()
    assert (active_run / "active").exists()
    assert managed.path.exists()


def test_removal_stops_when_the_path_was_re_registered(tmp_path, monkeypatch):
    """불변: 같은 경로에 다른 worktree가 다시 등록됐으면 제거하지 않는다.

    재확인이 "여전히 등록돼 있다"만 보면, 그 사이 다른 프로세스가 같은 경로를
    갈아끼운 경우를 통과시킨다. ``--force``까지 붙으면 남의 미커밋 변경을 지운다.
    """
    _init_repo(tmp_path)
    plan = W.plan_worktree(root=tmp_path, name="swap")
    status = W.create_worktree(root=tmp_path, plan=plan)

    real = W.list_registered_worktrees
    seen = {"n": 0}

    def replaced(root):
        entries = real(root)
        seen["n"] += 1
        if seen["n"] < 2:
            return entries
        return [
            entry
            if not W.same_worktree_path(entry.path, status.path)
            else replace(entry, branch="someone/else", head="0" * 40)
            for entry in entries
        ]

    monkeypatch.setattr(W, "list_registered_worktrees", replaced)
    with pytest.raises(WorktreeIsolationError) as caught:
        W.remove_worktree(root=tmp_path, status=status, allow_unmerged=True)
    assert "registration changed" in str(caught.value)
    assert status.path.exists()


def test_removal_clears_runtime_state_keyed_by_the_registered_name(tmp_path: Path):
    """불변: 런타임 상태를 쓴 키와 지우는 키가 같다.

    정규화되지 않는 이름(``feat-issue#110``)은 등록된 이름 그대로 상태가 쌓이는데
    제거만 정규화하면 죽은 active 마커가 남아 같은 자리의 다음 run을 막는다.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    checkout = root / ".agent-flow" / "worktrees" / "feat-issue#110"
    _add_raw_worktree(root, "feat/issue#110", checkout)

    started = _run_cli(["run", "task", "--worktree", "feat-issue#110"], root)
    assert started.returncode == 0, started.stderr
    runtime_root = root / ".git" / "agent-flow" / "worktrees" / "feat-issue#110"
    assert runtime_root.exists()

    blocked = _run_cli(
        ["worktree", "remove", "--name", "feat-issue#110", "--allow-unmerged"], root
    )
    assert blocked.returncode == 2
    assert "active run exists" in blocked.stderr
    aborted = _run_cli(
        ["abort", "--worktree", "feat-issue#110", "--yes"],
        root,
    )
    assert aborted.returncode == 0, aborted.stderr
    removed = _run_cli(
        ["worktree", "remove", "--name", "feat-issue#110", "--allow-unmerged"], root
    )
    assert removed.returncode == 0, removed.stderr
    assert not runtime_root.exists()


def test_removal_clears_runtime_state_when_a_sibling_owns_the_normalized_manifest(
    tmp_path: Path,
):
    """불변: 정규화 키를 형제가 쥐고 있어도 대상 자신의 런타임 상태는 지운다.

    ``feat-issue#110``의 정규화 키는 관리형 형제 ``feat-issue-110``과 같다. 소유
    증명이 정규화 키의 manifest(형제의 것)를 보면 경로가 어긋나 먼저 False가 되고,
    제거 대상 자신의 exact-key 상태가 남는다. 그 죽은 active 마커는 같은 경로를
    다시 등록한 뒤 도는 run을 ``already active``로 막는다.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    sibling = W.create_worktree(root=root, plan=W.plan_worktree(root=root, name="issue-110"))
    sibling_manifest = W.worktree_runtime_root(root=root, name=sibling.name) / "manifest.json"
    assert sibling_manifest.is_file()

    checkout = root / ".agent-flow" / "worktrees" / "feat-issue#110"
    _add_raw_worktree(root, "feat/issue#110", checkout)
    # 충돌이 실제로 나야 이 테스트가 의미가 있다. 이름 selector는 형제와 모호해지므로
    # 등록부가 유일 후보를 주는 정확한 경로로 지목한다.
    assert W._feature_worktree_name("feat-issue#110") == sibling.name

    started = _run_cli(["run", "task", "--worktree", str(checkout)], root)
    assert started.returncode == 0, started.stderr
    runtime_root = root / ".git" / "agent-flow" / "worktrees" / "feat-issue#110"
    assert runtime_root.exists()

    blocked = _run_cli(
        ["worktree", "remove", "--name", str(checkout), "--allow-unmerged"], root
    )
    assert blocked.returncode == 2
    assert "active run exists" in blocked.stderr
    aborted = _run_cli(
        ["abort", "--worktree", str(checkout), "--yes"],
        root,
    )
    assert aborted.returncode == 0, aborted.stderr
    removed = _run_cli(
        ["worktree", "remove", "--name", str(checkout), "--allow-unmerged"], root
    )
    assert removed.returncode == 0, removed.stderr
    assert not runtime_root.exists()
    # 반증 짝: 형제의 상태와 checkout은 그대로다.
    assert sibling_manifest.is_file()
    assert sibling.path.exists()
