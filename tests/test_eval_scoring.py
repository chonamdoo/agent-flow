"""eval oracle 계약 테스트.

eval 자체는 모델과 네트워크가 필요해 스위트에 넣지 않는다. 하지만 **채점**은
결정적이어야 한다 - 채점이 틀리면 eval의 모든 숫자가 틀린다. 여기서는 모델 없이
채점기만 검사한다.
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parent.parent
CASE = KIT_ROOT / "evals" / "cases" / "comment_norm"

NAIVE = '''"""Upstream 429 재시도 백오프."""
from __future__ import annotations

import random


def next_delay(attempt: int, *, base_s: float = 0.5, cap_s: float = 30.0) -> float:
    # Calculate the exponential delay.
    delay = min(base_s * (2 ** attempt), cap_s)
    # Returns the jittered delay.
    return random.uniform(delay, min(delay * 2, cap_s))
'''

NORM = '''"""Upstream 429 재시도 백오프."""
from __future__ import annotations

import random


def next_delay(attempt: int, *, base_s: float = 0.5, cap_s: float = 30.0) -> float:
    delay = min(base_s * (2 ** attempt), cap_s)
    # 같은 attempt가 늘 같은 값이면 재시도가 한 틱에 몰려 상대를 다시 429로 민다.
    # 그래서 상한 안에서 흩뿌린다.
    return random.uniform(delay, min(delay * 2, cap_s))
'''

BROKEN = '''from __future__ import annotations


def next_delay(attempt: int, *, base_s: float = 0.5, cap_s: float = 30.0) -> float:
    return 1.0
'''


def _score(tmp_path: Path, body: str) -> dict[str, bool]:
    spec = importlib.util.spec_from_file_location("eval_comment_norm", CASE / "check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    project = tmp_path / "project"
    shutil.copytree(CASE / "seed", project)
    (project / "backoff.py").write_text(body, encoding="utf-8")
    return module.score(project)


@pytest.mark.parametrize(
    "label,body,expected",
    [
        ("naive", NAIVE, {"behavior": True, "norm": False}),
        ("norm", NORM, {"behavior": True, "norm": True}),
        ("broken", BROKEN, {"behavior": False, "norm": True}),
    ],
)
def test_oracle_separates_behavior_from_norm(tmp_path, label, body, expected):
    """두 축은 독립이어야 한다.

    합쳐 버리면 "동작은 하는데 규범을 어겼다"가 "실패"로 뭉개지고, 전달 방식이
    무엇을 바꿨는지 못 본다. 이번 eval에서 신호를 만드는 축은 `norm`이다.
    """
    assert _score(tmp_path, body) == expected


def test_seed_comments_are_not_charged_to_the_agent(tmp_path):
    """반증: seed에 이미 있던 주석까지 세면 아무것도 안 해도 감점된다."""
    project = tmp_path / "project"
    shutil.copytree(CASE / "seed", project)
    spec = importlib.util.spec_from_file_location("eval_comment_norm", CASE / "check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.score(project)["norm"] is True


def test_every_case_has_a_task_seed_and_oracle():
    """반증: oracle 없는 case는 채점되지 않고 조용히 만점처럼 보인다."""
    cases = [path for path in (KIT_ROOT / "evals" / "cases").iterdir() if path.is_dir()]
    assert cases
    for case in cases:
        assert (case / "task.md").is_file(), case
        assert (case / "check.py").is_file(), case
        assert (case / "seed").is_dir(), case


def test_configs_differ_only_in_how_context_is_delivered(tmp_path):
    """불변: 구성 간 차이는 전달 방식 하나뿐이어야 점수 차를 거기에 귀속할 수 있다."""
    sys.path.insert(0, str(KIT_ROOT / "evals"))
    from configs import CONFIGS

    seen = {}
    for name, apply_config in CONFIGS.items():
        project = tmp_path / name
        shutil.copytree(CASE / "seed", project)
        apply_config(project)
        seen[name] = {
            path.relative_to(project).as_posix()
            for path in project.rglob("*")
            if path.is_file() and not path.name.startswith(".")
        }

    seed_files = {path.name for path in (CASE / "seed").iterdir() if path.is_file()}
    for name, files in seen.items():
        assert seed_files <= files, f"{name}이 seed를 바꿨다"
    assert seen["baseline"] == seed_files, "baseline은 컨텍스트를 더하지 않는다"
    assert any(name.endswith("AGENTS.md") for name in seen["agents-index"])


NARRATION_CASE = KIT_ROOT / "evals" / "cases" / "no_narration"
_RANKING_HEAD = (NARRATION_CASE / "seed" / "ranking.py").read_text(encoding="utf-8").split("def top_n")[0]

NARRATED = _RANKING_HEAD + '''def top_n(entries: list[Entry], n: int) -> list[Entry]:
    """점수 내림차순 상위 `n`개. 동점이면 이름 오름차순."""
    # 점수는 내림차순, 이름은 오름차순으로 정렬한다.
    ordered = sorted(entries, key=lambda entry: (-entry.score, entry.name))
    return ordered[:n]
'''

CLEAN = _RANKING_HEAD + '''def top_n(entries: list[Entry], n: int) -> list[Entry]:
    """점수 내림차순 상위 `n`개. 동점이면 이름 오름차순."""
    return sorted(entries, key=lambda entry: (-entry.score, entry.name))[:n]
'''

HASH_IN_STRING = _RANKING_HEAD + '''def top_n(entries: list[Entry], n: int) -> list[Entry]:
    """점수 내림차순 상위 `n`개. 동점이면 이름 오름차순."""
    _tag = "# not a comment"
    return sorted(entries, key=lambda entry: (-entry.score, entry.name))[:n]
'''


def _score_narration(tmp_path: Path, body: str) -> dict[str, bool]:
    spec = importlib.util.spec_from_file_location("eval_no_narration", NARRATION_CASE / "check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    project = tmp_path / "project"
    shutil.copytree(NARRATION_CASE / "seed", project)
    (project / "ranking.py").write_text(body, encoding="utf-8")
    return module.score(project)


def test_narration_oracle_charges_only_new_hash_comments(tmp_path):
    """규범은 "Default to adding no comments"다. 이 과제엔 설명할 제약이 없다."""
    assert _score_narration(tmp_path / "a", NARRATED)["norm"] is False
    assert _score_narration(tmp_path / "b", CLEAN)["norm"] is True


def test_narration_oracle_keeps_docstrings(tmp_path):
    """반증: docstring을 서술 주석으로 세면 규범대로 쓴 구현이 감점된다.

    규범은 Python public API docstring을 **남기라**고 명시한다.
    """
    assert '"""' in CLEAN
    assert _score_narration(tmp_path, CLEAN)["norm"] is True


def test_narration_oracle_ignores_hashes_inside_strings(tmp_path):
    """반증: 문자열 안의 `#`를 주석으로 세면 오탐이 규범 위반으로 기록된다."""
    assert _score_narration(tmp_path, HASH_IN_STRING)["norm"] is True
