"""L2 읽음 증거의 경로 동치 규칙.

강제 게이트의 **완화** 쪽 로직이라 반증 케이스가 본체다. worktree 사본은
통과해야 하고, 같은 꼬리를 가진 남의 경로는 통과하면 안 된다.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.local_skills import SkillReadEvidence
from agent_flow.core.skill_resolver import ResolvedSkill


def _skill(path: Path) -> ResolvedSkill:
    return ResolvedSkill(name=path.parent.name, path=path, source="project", exists=True)


@pytest.fixture()
def layout(tmp_path: Path) -> dict[str, Path]:
    leader = tmp_path / "leader"
    worktree = tmp_path / "leader" / ".agent-flow" / "worktrees" / "feat-x"
    for root in (leader, worktree):
        (root / "skills" / "alpha").mkdir(parents=True)
        (root / "skills" / "alpha" / "SKILL.md").write_text("x", encoding="utf-8")
        (root / ".agent-flow" / "skills" / "alpha").mkdir(parents=True)
        (root / ".agent-flow" / "skills" / "alpha" / "SKILL.md").write_text("stale", encoding="utf-8")
    return {"leader": leader, "worktree": worktree, "outside": tmp_path / "outside"}


def _evidence(read: Path, roots: tuple[Path, ...]) -> SkillReadEvidence:
    return SkillReadEvidence(
        available=True,
        read_paths=frozenset({str(read.resolve())}),
        checkout_roots=tuple(str(r.resolve()) for r in roots),
    )


def test_exact_path_counts(layout):
    skill_path = layout["leader"] / "skills" / "alpha" / "SKILL.md"
    evidence = _evidence(skill_path, (layout["leader"],))
    assert evidence.covers(_skill(skill_path))


def test_worktree_copy_counts(layout):
    """불변: worktree에서 읽어도 leader 기준으로 resolve된 같은 skill로 인정한다."""
    resolved = layout["leader"] / "skills" / "alpha" / "SKILL.md"
    read = layout["worktree"] / "skills" / "alpha" / "SKILL.md"
    evidence = _evidence(read, (layout["worktree"], layout["leader"]))
    assert evidence.covers(_skill(resolved))


def test_stale_bundled_copy_does_not_count(layout):
    """반증: 같은 이름의 다른 root 사본은 다른 파일이다. 통과시키면 강제가 무너진다."""
    resolved = layout["leader"] / "skills" / "alpha" / "SKILL.md"
    read = layout["leader"] / ".agent-flow" / "skills" / "alpha" / "SKILL.md"
    evidence = _evidence(read, (layout["leader"],))
    assert not evidence.covers(_skill(resolved))


def test_path_outside_every_checkout_does_not_count(layout):
    """반증: agent가 즉석에서 만든 `<어디든>/skills/alpha/SKILL.md`는 증거가 아니다."""
    resolved = layout["leader"] / "skills" / "alpha" / "SKILL.md"
    outside = layout["outside"] / "skills" / "alpha" / "SKILL.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("forged", encoding="utf-8")
    evidence = _evidence(outside, (layout["leader"],))
    assert not evidence.covers(_skill(resolved))


def test_sibling_file_in_skill_dir_does_not_count(layout):
    """반증: 같은 폴더의 다른 파일(`SKILL.md.bak`, `notes.md`)은 SKILL.md가 아니다."""
    resolved = layout["leader"] / "skills" / "alpha" / "SKILL.md"
    sibling = layout["leader"] / "skills" / "alpha" / "SKILL.md.bak"
    sibling.write_text("bak", encoding="utf-8")
    evidence = _evidence(sibling, (layout["leader"],))
    assert not evidence.covers(_skill(resolved))


def test_other_skill_does_not_count(layout):
    resolved = layout["leader"] / "skills" / "alpha" / "SKILL.md"
    other = layout["leader"] / "skills" / "beta" / "SKILL.md"
    other.parent.mkdir(parents=True)
    other.write_text("y", encoding="utf-8")
    evidence = _evidence(other, (layout["leader"],))
    assert not evidence.covers(_skill(resolved))


def test_no_checkout_roots_falls_back_to_exact_match(layout):
    """root를 못 구하면 완화 없이 정확 일치만 인정한다 — 열어 두지 않는다."""
    resolved = layout["leader"] / "skills" / "alpha" / "SKILL.md"
    read = layout["worktree"] / "skills" / "alpha" / "SKILL.md"
    assert not SkillReadEvidence(
        available=True, read_paths=frozenset({str(read.resolve())}), checkout_roots=()
    ).covers(_skill(resolved))
    assert SkillReadEvidence(
        available=True, read_paths=frozenset({str(resolved.resolve())}), checkout_roots=()
    ).covers(_skill(resolved))


# --- read_skill_evidence를 실제로 통과하는 경로 -----------------------------
#
# 위 테스트들은 checkout_roots를 손으로 넘긴다. 그러면 `_checkout_roots`를
# 통째로 `return ()`로 바꿔도 전부 통과한다 — 4라운드가 고친 바로 그 코드가
# 무방비다. 아래는 실제 git leader + linked worktree를 만들어 배선을 지킨다.


def _git(*args, cwd: Path):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _leader_with_worktree(tmp_path: Path):
    leader = tmp_path / "leader"
    leader.mkdir()
    _git("init", "-q", cwd=leader)
    _git("config", "user.email", "a@b", cwd=leader)
    _git("config", "user.name", "t", cwd=leader)
    skill = leader / "skills" / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# alpha\n", encoding="utf-8")
    (leader / ".gitignore").write_text(".agent-flow/\n", encoding="utf-8")
    _git("add", "-A", cwd=leader)
    _git("commit", "-qm", "init", cwd=leader)
    checkout = leader / ".agent-flow" / "worktrees" / "feat-x"
    checkout.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-q", "-b", "feat/x", str(checkout), "HEAD", cwd=leader)
    return leader, checkout


def _record(leader: Path, path: Path) -> None:
    from agent_flow.core.local_skills import record_skill_read

    record_skill_read(leader, path)


def test_worktree_read_is_credited_through_read_skill_evidence(tmp_path):
    """불변: worktree 사본을 읽어도 leader 기준 게이트가 인정한다.

    hook은 leader 로그에 쓰고 게이트도 leader root로 불린다. 체크아웃 목록이
    한쪽만 보면 정당한 읽음이 전부 미인정으로 차단된다.
    """
    from agent_flow.core.local_skills import read_skill_evidence

    leader, checkout = _leader_with_worktree(tmp_path)
    _record(leader, checkout / "skills" / "alpha" / "SKILL.md")
    evidence = read_skill_evidence(leader)
    assert evidence.available
    assert evidence.covers(_skill(leader / "skills" / "alpha" / "SKILL.md"))


def test_stale_worktree_directory_is_not_a_trusted_root(tmp_path):
    """반증: 삭제 잔재 디렉터리를 체크아웃으로 신뢰하면 엉뚱한 파일이 증거가 된다."""
    from agent_flow.core.local_skills import read_skill_evidence

    leader, _ = _leader_with_worktree(tmp_path)
    stale = leader / ".agent-flow" / "worktrees" / "gone"
    decoy = stale / "skills" / "alpha" / "SKILL.md"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("완전히 다른 내용\n", encoding="utf-8")
    _record(leader, decoy)
    evidence = read_skill_evidence(leader)
    assert not evidence.covers(_skill(leader / "skills" / "alpha" / "SKILL.md"))


def test_symlinked_worktree_entry_is_not_a_trusted_root(tmp_path):
    """반증: symlink를 체크아웃으로 받으면 저장소 밖 파일 읽기가 통과한다.

    대상 디렉터리에 `.git`까지 두어 존재 검사만으로는 못 거르게 한다.
    symlink 자체를 거부해야 막힌다.
    """
    from agent_flow.core.local_skills import read_skill_evidence

    leader, _ = _leader_with_worktree(tmp_path)
    outside = tmp_path / "outside"
    decoy = outside / "skills" / "alpha" / "SKILL.md"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("forged\n", encoding="utf-8")
    (outside / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
    (leader / ".agent-flow" / "worktrees" / "linked").symlink_to(outside)
    _record(leader, decoy)
    evidence = read_skill_evidence(leader)
    assert not evidence.covers(_skill(leader / "skills" / "alpha" / "SKILL.md"))


def test_checkout_roots_link_leader_and_worktree_both_ways(tmp_path):
    """불변: 체크아웃 목록은 양방향이다.

    게이트는 leader root로 불리지만(`cli.py`가 `config_root=root`) agent는
    worktree cwd에서 읽는다. 한쪽만 담으면 정당한 읽음이 미인정으로 차단된다.
    """
    from agent_flow.core.local_skills import _checkout_roots

    leader, checkout = _leader_with_worktree(tmp_path)
    from_leader = _checkout_roots(leader)
    from_worktree = _checkout_roots(checkout)

    assert str(leader.resolve()) in from_leader
    assert str(checkout.resolve()) in from_leader
    assert str(leader.resolve()) in from_worktree
    assert str(checkout.resolve()) in from_worktree


def test_adopted_external_worktree_counts_as_a_checkout_root(tmp_path):
    """불변: 채택된 checkout만 증거 root다.

    관리 루트 밖 linked worktree(Orca 워크스페이스 등)는 `.agent-flow/worktrees`
    디렉터리 스캔에 안 잡힌다. 목록에서 빠지면 그 checkout에서 읽은 skill이 전부
    미인정으로 차단된다. 반대로 등록만으로 인정하면 활성 워커가 `git worktree add`로
    만든 자리에 변조한 `SKILL.md`를 두고 게이트를 통과시킬 수 있다.
    """
    from agent_flow.core.local_skills import _checkout_roots
    from agent_flow.core.worktrees import adopt_worktree

    leader, _ = _leader_with_worktree(tmp_path)
    external = tmp_path / "orca" / "feat-y"
    external.parent.mkdir(parents=True)
    _git("worktree", "add", "-q", "-b", "feat/y", str(external), "HEAD", cwd=leader)

    assert str(external.resolve()) not in _checkout_roots(leader)

    adopt_worktree(root=leader, path=external, allow_dirty=True)

    assert str(external.resolve()) in _checkout_roots(leader)
