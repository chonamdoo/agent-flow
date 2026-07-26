"""gate-results.json 출처 검증.

이 층이 막는 것은 **손으로 쓴 gate 결과**다. nonce도 디스크에 있으니 읽어서
복사할 수 있으므로 적대적 위조는 못 막는다. 진짜 해법은 runner가 `run_gates`를
직접 부르는 것이고, 여기 테스트도 딱 그 경계까지만 주장한다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.artifact import create_run, read_meta
from agent_flow.core.artifacts import write_gate_results
from agent_flow.core.gates import GateResult
from agent_flow.runner import _gates_route_key

FORGED = json.dumps(
    {
        "passed": True,
        "status": "green",
        "results": [
            {"command": "true", "required": True, "passed": True, "exit_code": 0}
        ],
    }
)


def test_create_run_mints_a_gate_nonce(tmp_path):
    run_dir = create_run(tmp_path, "default", "task")
    nonce = read_meta(run_dir).get("gate_nonce")
    assert isinstance(nonce, str) and len(nonce) >= 32


def test_two_runs_get_different_nonces(tmp_path):
    first = create_run(tmp_path / "a", "default", "task")
    second = create_run(tmp_path / "b", "default", "task")
    assert read_meta(first)["gate_nonce"] != read_meta(second)["gate_nonce"]


def test_cli_written_results_carry_the_nonce(tmp_path):
    run_dir = create_run(tmp_path, "default", "task")
    path = write_gate_results(
        run_dir=run_dir,
        results=[GateResult("test", ("pytest", "-q"), True, 0, "ok", "")],
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["produced_by"]["nonce"] == read_meta(run_dir)["gate_nonce"]
    assert _gates_route_key(path.read_text(encoding="utf-8"), nonce=read_meta(run_dir)["gate_nonce"]) == "green"


def test_hand_written_green_does_not_route_green():
    """#102 F1의 실증 경로. 손으로 쓴 4개 키로 green → commit이 됐다."""
    assert _gates_route_key(FORGED, nonce="abc123") == "default"


def test_wrong_nonce_does_not_route_green():
    payload = json.loads(FORGED)
    payload["produced_by"] = {"tool": "agent-flow gates", "nonce": "not-the-one"}
    assert _gates_route_key(json.dumps(payload), nonce="abc123") == "default"


def test_approve_status_also_requires_provenance():
    payload = json.loads(FORGED)
    payload["status"] = "approve"
    assert _gates_route_key(json.dumps(payload), nonce="abc123") == "default"


def test_runs_without_a_nonce_are_not_blocked():
    """구버전 run과 CLI 직접 사용에는 대조할 기록이 없다. 없으면 위반이 아니다."""
    assert _gates_route_key(FORGED) == "green"
    assert _gates_route_key(FORGED, nonce="") == "green"


def test_failure_routing_never_needs_provenance():
    """실패/차단까지 막으면 복구 경로만 좁아진다. 앞으로 가는 길에만 건다."""
    for status in ("request-changes", "blocked", "error", "pending"):
        text = json.dumps({"passed": False, "status": status, "results": []})
        assert _gates_route_key(text, nonce="abc123") == status


def test_python_and_node_agree_on_provenance(tmp_path):
    """이중 구현이 갈라지면 한쪽 runner에서만 위조가 통한다."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    script = tmp_path / "probe.mjs"
    kit = (REPO / "bin" / "agent-flow-kit.mjs").read_text(encoding="utf-8")
    start = kit.index("function readGatesRouteKey(pathName, nonce")
    end = kit.index("\n}\n", kit.index("function hasGateEvidence(result)")) + 3
    helpers = kit[kit.index("function gateNonceMatches(data, nonce)"):end]
    script.write_text(
        "import fs from 'node:fs';\n"
        + helpers
        + "\nconst p = process.argv[2];\n"
        + "console.log(readGatesRouteKey(p, process.argv[3] ?? ''));\n",
        encoding="utf-8",
    )
    assert start > 0
    target = tmp_path / "gate-results.json"
    target.write_text(FORGED, encoding="utf-8")
    forged = subprocess.run(
        [node, str(script), str(target), "abc123"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert forged == "default"

    payload = json.loads(FORGED)
    payload["produced_by"] = {"tool": "agent-flow gates", "nonce": "abc123"}
    target.write_text(json.dumps(payload), encoding="utf-8")
    signed = subprocess.run(
        [node, str(script), str(target), "abc123"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert signed == "green"
