"""Reuse consent decision matrix.

The decision is pure so a refusal can be proven to change nothing; the CLI
test covers the end-to-end zero-mutation claim.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.worktree_consent import (  # noqa: E402
    CONSENT_REQUIRED,
    GRANTED,
    NOT_APPLICABLE,
    PROMPT,
    REFUSED,
    CurrentCheckout,
    consent_granted,
    decide_worktree_reuse,
    render_checkout_summary,
)
from agent_flow.core.provider_sandbox import UnboundedSpawn  # noqa: E402
from agent_flow.core.worktree_isolation import WorktreeIsolationError  # noqa: E402
from agent_flow.core.worktrees import leader_checkout_of, sibling_worktree_policy  # noqa: E402


def _checkout(**overrides) -> CurrentCheckout:
    values = dict(
        path=Path("/repo/.agent-flow/worktrees/feat-a"),
        branch="feat/a",
        dirty=False,
        state_key="feat-a",
        is_leader=False,
    )
    values.update(overrides)
    return CurrentCheckout(**values)


def test_no_checkout_is_not_applicable() -> None:
    decision = decide_worktree_reuse(checkout=None, explicit_selector=False, interactive=True)
    assert decision.outcome == NOT_APPLICABLE
    assert not decision.blocks_run


def test_leader_checkout_is_not_applicable() -> None:
    decision = decide_worktree_reuse(
        checkout=_checkout(is_leader=True), explicit_selector=False, interactive=True
    )
    assert decision.outcome == NOT_APPLICABLE


def test_implicit_interactive_reuse_requires_consent() -> None:
    decision = decide_worktree_reuse(
        checkout=_checkout(), explicit_selector=False, interactive=True
    )
    assert decision.outcome == CONSENT_REQUIRED
    assert decision.needs_prompt


def test_explicit_selector_is_consent() -> None:
    """`--worktree`/`--reuse-current` already names the target."""
    decision = decide_worktree_reuse(
        checkout=_checkout(), explicit_selector=True, interactive=False
    )
    assert decision.outcome == GRANTED
    assert not decision.blocks_run


def test_non_interactive_implicit_reuse_fails_closed() -> None:
    """The falsification case: defaulting to reuse here restores the silent attach."""
    decision = decide_worktree_reuse(
        checkout=_checkout(), explicit_selector=False, interactive=False
    )
    assert decision.outcome == REFUSED
    assert decision.blocks_run
    assert "--reuse-current" in decision.reason


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES", " yes \n"])
def test_affirmative_answers(answer: str) -> None:
    assert consent_granted(answer) is True


@pytest.mark.parametrize("answer", ["", "\n", "n", "no", "sure", "yep", "1", "true"])
def test_everything_else_declines(answer: str) -> None:
    assert consent_granted(answer) is False


def test_prompt_defaults_to_no() -> None:
    assert PROMPT.endswith("[y/N] ")


def test_summary_shows_the_four_distinguishing_facts() -> None:
    summary = render_checkout_summary(_checkout(dirty=True))
    assert "/repo/.agent-flow/worktrees/feat-a" in summary
    assert "feat/a" in summary
    assert "dirty     : yes" in summary
    assert "state key : feat-a" in summary


def test_summary_names_detached_head() -> None:
    assert "(detached HEAD)" in render_checkout_summary(_checkout(branch=None))


class TestLeaderCheckoutOf:
    """`leader_checkout_of` decides whether a boundary is needed at all.

    A wrong None here means a spawn runs unbounded; a wrong raise stops a
    review phase that used to work. Both failure directions are covered.
    """

    @staticmethod
    def _git(*args: str, cwd: Path):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)

    def _repo(self, root: Path) -> Path:
        root.mkdir(parents=True)
        for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@t"),
                     ("config", "user.name", "t")):
            self._git(*args, cwd=root)
        (root / "f.txt").write_text("x\n")
        self._git("add", "-A", cwd=root)
        self._git("commit", "-q", "-m", "init", cwd=root)
        return root

    def test_leader_checkout_has_nothing_outside_it(self, tmp_path: Path) -> None:
        assert leader_checkout_of(self._repo(tmp_path / "repo")) is None

    def test_linked_worktree_resolves_its_leader(self, tmp_path: Path) -> None:
        leader = self._repo(tmp_path / "repo")
        linked = tmp_path / "wt"
        self._git("worktree", "add", "-q", str(linked), "-b", "feat/x", "main", cwd=leader)
        assert leader_checkout_of(linked) == Path(os.path.realpath(str(leader)))

    def test_plain_directory_needs_no_boundary(self, tmp_path: Path) -> None:
        """반증: 여기서 raise하면 non-git 프로젝트의 multi-review가 통째로 죽는다."""
        plain = tmp_path / "plain"
        plain.mkdir()
        assert leader_checkout_of(plain) is None

    def test_separate_git_dir_checkout_is_still_answerable(self, tmp_path: Path) -> None:
        """반증: `git worktree list`는 여기서 checkout이 아니라 git dir을 보고한다.

        그 값을 leader로 믿으면 진짜 checkout이 무방비가 되고, 못 믿겠다고
        raise하면 이미 안전한 자리에서 phase가 죽는다.
        """
        work = tmp_path / "work"
        work.mkdir()
        gitdir = tmp_path / "gitdirs" / "main.git"
        gitdir.parent.mkdir(parents=True)
        self._git("init", "-q", "-b", "main", f"--separate-git-dir={gitdir}", str(work), cwd=tmp_path)
        assert leader_checkout_of(work) is None

    def test_bare_repository_has_no_working_tree(self, tmp_path: Path) -> None:
        bare = tmp_path / "bare.git"
        self._git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
        assert leader_checkout_of(bare) is None

    def test_linked_worktree_of_a_bare_repository_fails_closed(self, tmp_path: Path) -> None:
        """반증: None을 돌려주면 이 배치에서 경계가 통째로 꺼진다.

        bare 저장소에는 지킬 working tree가 없지만 sibling worktree와 common
        metadata는 그대로 있다. 유일한 호출자는 None을 "지킬 것 없음"으로
        읽어 UnboundedSpawn을 만들고 tripwire까지 끈다. 이름을 못 대는 것을
        지킬 것이 없다는 뜻으로 접으면 안 된다.
        """
        source = self._repo(tmp_path / "src")
        bare = tmp_path / "bare.git"
        self._git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
        self._git("fetch", str(source), "main:main", cwd=bare)
        linked = tmp_path / "bare-wt"
        assert self._git("worktree", "add", "-q", str(linked), "-b", "feat/b", "main",
                         cwd=bare).returncode == 0
        with pytest.raises(WorktreeIsolationError):
            leader_checkout_of(linked)

    def test_normal_repository_without_core_bare_still_names_its_leader(self, tmp_path: Path) -> None:
        """반증: bareness를 cwd로 물으면 평범한 저장소가 bare로 답한다.

        git dir 안에서 물으면 `core.bare`가 없을 때 git이 추측한다. 그 답을
        믿고 None으로 접으면 정상 저장소 전체에서 경계가 꺼진다.
        """
        leader = self._repo(tmp_path / "repo")
        self._git("config", "--unset", "core.bare", cwd=leader)
        linked = tmp_path / "wt"
        self._git("worktree", "add", "-q", str(linked), "-b", "feat/z", "main", cwd=leader)
        assert leader_checkout_of(linked) == leader.resolve()

    def test_linked_worktree_of_separate_git_dir_fails_closed(self, tmp_path: Path) -> None:
        """leader가 존재하는데 git이 이름을 대지 못하는 유일한 배치.

        이때만 raise한다. None으로 접으면 그 leader가 무방비가 된다.
        """
        work = tmp_path / "work"
        work.mkdir()
        gitdir = tmp_path / "gitdirs" / "main.git"
        gitdir.parent.mkdir(parents=True)
        self._git("init", "-q", "-b", "main", f"--separate-git-dir={gitdir}", str(work), cwd=tmp_path)
        for args in (("config", "user.email", "t@t"), ("config", "user.name", "t")):
            self._git(*args, cwd=work)
        (work / "f.txt").write_text("x\n")
        self._git("add", "-A", cwd=work)
        self._git("commit", "-q", "-m", "init", cwd=work)
        linked = tmp_path / "sgd-wt"
        self._git("worktree", "add", "-q", str(linked), "-b", "feat/y", "main", cwd=work)
        with pytest.raises(WorktreeIsolationError):
            leader_checkout_of(linked)


class TestSiblingWorktreePolicy:
    """A spawn in the main checkout still has other workers' trees beside it."""

    def _git(self, *args: str, cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(("git", *args), cwd=cwd, capture_output=True, text=True)

    def _repo(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        self._git("init", "-q", "-b", "main", cwd=path)
        for args in (("config", "user.email", "t@t"), ("config", "user.name", "t")):
            self._git(*args, cwd=path)
        (path / "README.md").write_text("r\n")
        self._git("add", "-A", cwd=path)
        self._git("commit", "-q", "-m", "init", cwd=path)
        return path

    def test_registered_worktrees_are_denied_and_nothing_is_granted(self, tmp_path: Path) -> None:
        """반증: leader에서 도는 리뷰어를 무경계로 두면 worker 체크아웃을 덮어쓴다."""
        leader = self._repo(tmp_path / "repo")
        worker = leader / ".agent-flow" / "worktrees" / "feat-mine"
        worker.parent.mkdir(parents=True)
        self._git("worktree", "add", "-q", str(worker), "-b", "feat/mine", "main", cwd=leader)
        policy = sibling_worktree_policy(leader)
        assert policy is not None
        assert policy.protected_roots == (worker.resolve(),)
        # leader 자신은 열지 않는다. `allow default`가 이미 덮으므로 grant를
        # 얹으면 deny보다 뒤에 놓여 sibling을 다시 열어 버린다.
        assert policy.writable_subpaths == ()
        assert policy.writable_literals == ()

    def test_nothing_registered_beside_it_is_no_policy(self, tmp_path: Path) -> None:
        """빈 정책은 아무 경로도 막지 않으면서 경계가 있다고 주장한다."""
        leader = self._repo(tmp_path / "solo")
        assert sibling_worktree_policy(leader) is None

    def test_separate_git_dir_checkout_denies_only_real_siblings(self, tmp_path: Path) -> None:
        """반증: git이 여기서 보고하는 첫 항목은 checkout이 아니라 git dir이다.

        그걸 sibling으로 세면 프로세스가 자기 `index.lock`을 못 만들어
        `add`와 `commit`이 128로 죽는다. 자기 저장소 metadata는 sibling이
        아니다.
        """
        work = tmp_path / "work"
        work.mkdir()
        gitdir = tmp_path / "gitdirs" / "main.git"
        gitdir.parent.mkdir(parents=True)
        self._git("init", "-q", "-b", "main", f"--separate-git-dir={gitdir}", str(work), cwd=tmp_path)
        for args in (("config", "user.email", "t@t"), ("config", "user.name", "t")):
            self._git(*args, cwd=work)
        (work / "f.txt").write_text("x\n")
        self._git("add", "-A", cwd=work)
        self._git("commit", "-q", "-m", "init", cwd=work)
        assert sibling_worktree_policy(work) is None

    def test_non_git_directory_gets_an_unbounded_spawn_not_a_raise(self, tmp_path: Path) -> None:
        """반증: `leader_checkout_of`의 None 셋 중 하나만 안전하다고 가정하면 죽는다.

        None은 non-repo, main worktree, bare 세 경우에 나온다. 그중 non-repo에
        sibling을 물으면 `git worktree list`가 128로 답하고 fail-closed 헬퍼가
        raise한다. 그 자리는 계속 돌아야 하는 지원 배치다.
        """
        from agent_flow.multi_review import _reviewer_boundary  # noqa: E402

        plain = tmp_path / "plain"
        plain.mkdir()
        boundary = _reviewer_boundary(project_root=plain, leader=None)
        assert isinstance(boundary, UnboundedSpawn)
        assert "outside a git repository" in boundary.reason
