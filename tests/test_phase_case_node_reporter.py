"""Node 테스트를 세는 사례 oracle은 TAP과 spec reporter 출력을 모두 센다. 못 세면 통과한 구현이 실패로 채점된다."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

CASES = Path(__file__).resolve().parents[1] / "evals" / "phase-cases"
TAP = "TAP version 13\nok 1 - a\n# tests 3\n# pass 3\n# fail 0\n"
SPEC = "✔ a (1.2ms)\nℹ tests 3\nℹ suites 0\nℹ pass 3\nℹ fail 0\n"


def _oracle(case: str):
    spec = importlib.util.spec_from_file_location(f"oracle_{case.replace('-', '_')}", CASES / case / "oracle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("case", ["commerce", "fixloop-ts"])
def test_node_pass_count_reads_both_reporters(case):
    oracle = _oracle(case)
    assert [int(n) for n in oracle.NODE_PASS.findall(TAP)] == [3]
    assert [int(n) for n in oracle.NODE_PASS.findall(SPEC)] == [3]
    assert oracle.NODE_PASS.findall("ℹ fail 0\n# fail 0\n") == []
