"""profile이 선언한 이름으로 정해진 동작만 돌린다.

임의 셸 문자열을 받으면 `post_create: ["rm -rf $HOME"]` 한 줄이 CLI 신뢰 컨텍스트에서
검증 없이 돈다. 그리고 그걸 정적 분석으로 막으려는 시도는 이 저장소가 이미 한 번
겪었다 — 예외가 계속 쌓인다.

그래서 profile은 **예/아니오만** 말하고 명령은 agent-flow가 정한다. 주입면이 없다.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.worktrees import (
    WORKTREE_SETUP_ACTIONS,
    UnknownWorktreeSetupAction,
    run_declared_worktree_actions,
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True, text=True)


def _repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "f.txt").write_text("base\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "init", cwd=root)


def _pair(tmp_path: Path) -> tuple[Path, Path]:
    leader, checkout = tmp_path / "leader", tmp_path / "wt"
    _repo(leader)
    checkout.mkdir()
    return leader, checkout


def test_profile_can_only_choose_from_known_actions():
    """불변: 선언은 이름이지 명령이 아니다. 임의 문자열이 들어올 자리가 없다."""
    for name, action in WORKTREE_SETUP_ACTIONS.items():
        assert callable(action), name
        assert name.replace("_", "").isalnum(), name


def test_unknown_action_is_refused_not_ignored(tmp_path: Path):
    """반증: 오타를 조용히 넘기면 선언했는데 아무 일도 안 일어난다."""
    leader, checkout = _pair(tmp_path)
    with pytest.raises(UnknownWorktreeSetupAction):
        run_declared_worktree_actions(
            leader=leader, checkout=checkout, declared={"run_npm_instal": True}
        )


def test_disabled_action_does_not_run(tmp_path: Path):
    """불변: `false`로 끈 것이 돌면 선언이 의미를 잃는다."""
    leader, checkout = _pair(tmp_path)
    (leader / "node_modules").mkdir()
    ran = run_declared_worktree_actions(
        leader=leader, checkout=checkout, declared={"link_node_modules": False}
    )
    assert ran == ()
    assert not (checkout / "node_modules").exists()


def test_link_node_modules_shares_the_leader_tree(tmp_path: Path):
    """불변: 큰 의존성 트리를 worktree마다 다시 설치하지 않는다."""
    leader, checkout = _pair(tmp_path)
    (leader / "node_modules").mkdir()
    (leader / "node_modules" / "marker.txt").write_text("x", encoding="utf-8")

    ran = run_declared_worktree_actions(
        leader=leader, checkout=checkout, declared={"link_node_modules": True}
    )

    assert ran == ("link_node_modules",)
    assert (checkout / "node_modules").is_symlink()
    assert (checkout / "node_modules" / "marker.txt").read_text(encoding="utf-8") == "x"


def test_linked_directory_is_excluded_from_git(tmp_path: Path):
    """불변: symlink가 untracked로 잡히면 worktree 정리가 매번 막힌다.

    `node_modules/`처럼 슬래시로 끝나는 gitignore 항목은 디렉터리만 맞아서 symlink
    파일 자체를 못 가린다. 루트 고정 패턴으로 따로 적어야 한다.
    """
    leader, checkout = _pair(tmp_path)
    (leader / "node_modules").mkdir()

    run_declared_worktree_actions(
        leader=leader, checkout=checkout, declared={"link_node_modules": True}
    )

    exclude = (leader / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "/node_modules" in exclude


def test_link_skips_when_the_worktree_already_has_one(tmp_path: Path):
    """불변: 이미 설치된 트리를 symlink로 갈아치우면 그 안의 작업이 사라진다."""
    leader, checkout = _pair(tmp_path)
    (leader / "node_modules").mkdir()
    (checkout / "node_modules").mkdir()
    (checkout / "node_modules" / "mine.txt").write_text("mine", encoding="utf-8")

    ran = run_declared_worktree_actions(
        leader=leader, checkout=checkout, declared={"link_node_modules": True}
    )

    assert ran == ()
    assert not (checkout / "node_modules").is_symlink()
    assert (checkout / "node_modules" / "mine.txt").exists()


def test_link_skips_when_the_leader_has_nothing_to_share(tmp_path: Path):
    """불변: 없는 것을 가리키는 symlink는 깨진 링크만 남긴다."""
    leader, checkout = _pair(tmp_path)
    ran = run_declared_worktree_actions(
        leader=leader, checkout=checkout, declared={"link_node_modules": True}
    )
    assert ran == ()
    assert not (checkout / "node_modules").exists()


def test_action_failure_does_not_block_worktree_creation(tmp_path: Path, monkeypatch):
    """불변: 셋업이 실패해도 방금 만든 checkout이 사라지면 안 된다.

    설정 하나 없어 빌드가 한 번 실패하는 것과 작업 자리가 통째로 없어지는 것은
    무게가 다르다.
    """
    leader, checkout = _pair(tmp_path)

    def _boom(**_kwargs):
        raise OSError("disk full")

    monkeypatch.setitem(WORKTREE_SETUP_ACTIONS, "link_node_modules", _boom)
    ran = run_declared_worktree_actions(
        leader=leader, checkout=checkout, declared={"link_node_modules": True}
    )
    assert ran == ()
    assert checkout.is_dir()


def test_declaration_is_read_from_a_multi_profile_union():
    """불변: profile이 둘 이상인 프로젝트에서 선언이 사라지면 안 된다.

    합성 profile에는 최상위 `branching`이 없다. 같은 함정을 `copy`에서 한 번 겪었다.
    """
    from agent_flow.cli import _declared_worktree_actions

    node = {"branching": {"worktree_setup": {"link_node_modules": True}}}
    android = {"branching": {"worktree_setup": {"copy": ["local.properties"]}}}
    union = {"id": "multi-profile", "profiles": [android, node]}

    assert _declared_worktree_actions(union) == {"link_node_modules": True}


def test_copy_is_not_treated_as_an_action():
    """불변: `copy`는 목록이지 켜고 끄는 동작이 아니다."""
    from agent_flow.cli import _declared_worktree_actions

    single = {"branching": {"worktree_setup": {"copy": ["local.properties"]}}}
    assert _declared_worktree_actions(single) == {}


def test_unknown_action_warns_but_does_not_stop_the_run(tmp_path: Path, capsys):
    """불변: 오타 하나로 worktree 생성이 죽으면 안 된다. 대신 사실대로 말한다."""
    from agent_flow import cli as CLI

    CLI._run_worktree_setup_actions(
        root=tmp_path,
        checkout=tmp_path,
        profile={"branching": {"worktree_setup": {"link_node_module": True}}},
    )
    err = capsys.readouterr().err
    assert "link_node_module" in err
    assert "known:" in err
