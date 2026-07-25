"""L2 읽음 증거의 경로 동치 규칙.

강제 게이트의 **완화** 쪽 로직이라 반증 케이스가 본체다. worktree 사본은
통과해야 하고, 같은 꼬리를 가진 남의 경로는 통과하면 안 된다.
"""
from __future__ import annotations

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
