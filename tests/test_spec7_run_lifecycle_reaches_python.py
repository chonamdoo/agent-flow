"""SPEC-7: run lifecycle 명령(start/status/next/advance)이 Python CLI로 릴레이됨을 검증.

kit.mjs의 소스를 읽어 두 가지를 확인한다.
1. PYTHON_RUN_LIFECYCLE Set에 네 명령이 모두 있다.
2. relayPythonRunLifecycle이 runPythonCliCommand를 호출하고, runPythonCliCommand는
   agent_flow.cli를 subprocess로 실행한다 — Node 판정 경로는 없다.
"""
from __future__ import annotations

import re
from pathlib import Path

KIT = (Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs").read_text(encoding="utf-8")


def _extract_function(source: str, name: str) -> str:
    """함수 선언부터 첫 번째 짝 닫는 중괄호까지 추출."""
    start = source.find(f"function {name}(")
    assert start != -1, f"function {name} 없음"
    depth = 0
    for i, ch in enumerate(source[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise AssertionError(f"function {name} 끝을 찾지 못함")


def test_python_run_lifecycle_contains_all_four_subcommands():
    """불변: PYTHON_RUN_LIFECYCLE Set에 start/status/next/advance가 모두 있어야 한다."""
    match = re.search(r'PYTHON_RUN_LIFECYCLE\s*=\s*new Set\(\[([^\]]+)\]\)', KIT)
    assert match, "PYTHON_RUN_LIFECYCLE 선언을 찾지 못함"
    declared = set(re.findall(r'"(\w+)"', match.group(1)))
    missing = {"start", "status", "next", "advance"} - declared
    assert not missing, f"누락된 subcommand: {missing}"


def test_relay_calls_python_cli_module():
    """불변: relayPythonRunLifecycle은 agent_flow.cli를 subprocess로 실행한다."""
    body = _extract_function(KIT, "relayPythonRunLifecycle")
    assert "runPythonCliCommand(" in body, "relayPythonRunLifecycle가 runPythonCliCommand를 호출하지 않음"


def test_python_cli_spawns_agent_flow_cli():
    """불변: runPythonCliCommand는 agent_flow.cli 모듈을 subprocess로 실행한다."""
    # _extract_function은 중첩 구조에서 조기 종료하므로 전체 소스에서 확인한다.
    # runPythonCliCommand 선언 위치부터 뒤에 나오는 함수 선언 전까지 검색.
    start = KIT.find("function runPythonCliCommand(")
    assert start != -1, "runPythonCliCommand 선언 없음"
    next_fn = KIT.find("\nfunction ", start + 1)
    segment = KIT[start:next_fn] if next_fn != -1 else KIT[start:]
    assert '"agent_flow.cli"' in segment or "'agent_flow.cli'" in segment, (
        "runPythonCliCommand가 agent_flow.cli를 실행하지 않음"
    )


def test_run_lifecycle_short_circuits_before_node_judgement():
    """불변: runWorkflowCommand에서 PYTHON_RUN_LIFECYCLE 분기는 return으로 끝난다."""
    body = _extract_function(KIT, "runWorkflowCommand")
    # PYTHON_RUN_LIFECYCLE.has(...)를 찾고 그 블록에 return이 있어야 한다
    relay_block = re.search(
        r'PYTHON_RUN_LIFECYCLE\.has\(subcommand\)\s*\)\s*\{([^}]+relayPythonRunLifecycle[^}]+)\}',
        body,
        re.DOTALL,
    )
    assert relay_block, "PYTHON_RUN_LIFECYCLE.has 블록을 찾지 못함"
    assert "return;" in relay_block.group(1), "relay 블록이 return 없이 fall-through됨"
