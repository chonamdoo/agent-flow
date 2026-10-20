"""autoresearch 루프의 유지/되돌림 판정과 편집 범위. 틀리면 루프가 회귀나 범위 밖 편집을 남긴다."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("autoresearch_loop", REPO / "tools" / "autoresearch" / "loop.py")
assert _SPEC is not None and _SPEC.loader is not None
loop = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(loop)


def _routing(passing: dict[str, bool]) -> dict:
    return {"score": (sum(passing.values()),), "passing": passing}


def test_routing_keeps_a_higher_score_and_rejects_any_lost_case():
    best = _routing({"a": True, "b": False, "c": False})
    assert loop.better(_routing({"a": True, "b": True, "c": False}), best, "routing", 10, 10) == (True, "improved")
    keep, reason = loop.better(_routing({"a": False, "b": True, "c": True}), best, "routing", 10, 10)
    assert not keep and "regressed ['a']" in reason


def test_routing_keeps_an_equal_score_only_when_shorter():
    best = _routing({"a": True, "b": False})
    assert loop.better(_routing({"a": True, "b": False}), best, "routing", 9, 10) == (True, "same score, shorter")
    assert loop.better(_routing({"a": True, "b": False}), best, "routing", 10, 10) == (False, "no improvement")


def test_model_track_ignores_token_only_gains_and_read_rate_drops():
    best = {"score": (2, 3, -1000), "read_rate": 1.0}
    assert loop.better({"score": (2, 3, -10), "read_rate": 1.0}, best, "model", 10, 10)[0] is False
    assert loop.better({"score": (3, 3, -2000), "read_rate": 1.0}, best, "model", 10, 10) == (True, "improved")
    assert loop.better({"score": (3, 3, -1), "read_rate": 0.9}, best, "model", 10, 10) == (
        False, "required-read rate dropped")


def test_model_track_never_keeps_a_deletion_for_being_shorter():
    best = {"score": (3, 3, -1000), "read_rate": 1.0}
    assert loop.better({"score": (3, 3, -900), "read_rate": 1.0}, best, "model", 5, 10) == (
        False, "no improvement")


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "kit"
    (root / "skills" / "demo").mkdir(parents=True)
    (root / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: d\ntaskTerms: [alpha]\n---\n\nBody.\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed"], cwd=root, check=True)
    return root


def test_routing_surface_accepts_only_vocabulary_lines(tmp_path):
    root = _repo(tmp_path)
    skill = root / "skills" / "demo" / "SKILL.md"
    skill.write_text(skill.read_text().replace("[alpha]", "[alpha, beta]"), encoding="utf-8")
    assert loop.surface_violations(root, "routing", []) == []
    skill.write_text(skill.read_text().replace("Body.", "Body changed."), encoding="utf-8")
    assert any("non-vocabulary line" in problem for problem in loop.surface_violations(root, "routing", []))
    (root / "README.md").write_text("x", encoding="utf-8")
    assert any("README.md: outside" in problem for problem in loop.surface_violations(root, "routing", []))


def test_model_surface_freezes_frontmatter_and_other_skills(tmp_path):
    root = _repo(tmp_path)
    skill = root / "skills" / "demo" / "SKILL.md"
    skill.write_text(skill.read_text().replace("Body.", "Read the slice plan first."), encoding="utf-8")
    assert loop.surface_violations(root, "model", ["demo"]) == []
    assert any("outside the model surface" in p for p in loop.surface_violations(root, "model", ["other"]))
    skill.write_text(skill.read_text().replace("description: d", "description: e"), encoding="utf-8")
    assert any("frontmatter is frozen" in p for p in loop.surface_violations(root, "model", ["demo"]))
