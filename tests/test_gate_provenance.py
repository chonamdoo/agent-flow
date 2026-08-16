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
from agent_flow.core.artifacts import run_gate_nonce, write_gate_results
from agent_flow.core.gates import GateCommand, GateResult, run_gate
from agent_flow.core.route_verdicts import gates_route_key

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
    assert gates_route_key(path.read_text(encoding="utf-8"), nonce=read_meta(run_dir)["gate_nonce"]) == "green"


def test_hand_written_green_does_not_route_green():
    """#102 F1의 실증 경로. 손으로 쓴 4개 키로 green → commit이 됐다."""
    assert gates_route_key(FORGED, nonce="abc123") == "default"


def test_wrong_nonce_does_not_route_green():
    payload = json.loads(FORGED)
    payload["produced_by"] = {"tool": "agent-flow gates", "nonce": "not-the-one"}
    assert gates_route_key(json.dumps(payload), nonce="abc123") == "default"


def test_approve_status_also_requires_provenance():
    payload = json.loads(FORGED)
    payload["status"] = "approve"
    assert gates_route_key(json.dumps(payload), nonce="abc123") == "default"


def test_runs_without_a_nonce_are_not_blocked():
    """구버전 run과 CLI 직접 사용에는 대조할 기록이 없다. 없으면 위반이 아니다."""
    assert gates_route_key(FORGED) == "green"
    assert gates_route_key(FORGED, nonce="") == "green"


def test_failure_routing_never_needs_provenance():
    """실패/차단까지 막으면 복구 경로만 좁아진다. 앞으로 가는 길에만 건다."""
    for status in ("request-changes", "blocked", "error", "pending"):
        text = json.dumps({"passed": False, "status": status, "results": []})
        assert gates_route_key(text, nonce="abc123") == status


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


def test_pre_commit_gate_phase_does_not_route_green():
    """build/test는 `pre-push`다. `--phase pre-commit`으로 돈 결과는 그 게이트가
    목록에 오르지도 않은 실행이라 QA 통과의 증거가 아니다."""
    payload = json.loads(FORGED)
    payload["produced_by"] = {
        "tool": "agent-flow gates",
        "nonce": "abc123",
        "gate_phase": "pre-commit",
    }
    assert gates_route_key(json.dumps(payload), nonce="abc123") == "default"


def test_all_gate_phase_routes_green():
    payload = json.loads(FORGED)
    payload["produced_by"] = {
        "tool": "agent-flow gates",
        "nonce": "abc123",
        "gate_phase": "all",
    }
    assert gates_route_key(json.dumps(payload), nonce="abc123") == "green"


def test_results_without_a_recorded_gate_phase_are_not_blocked():
    """구버전 파일에는 기록이 없다. nonce와 같은 규칙 — 없으면 위반이 아니다."""
    payload = json.loads(FORGED)
    payload["produced_by"] = {"tool": "agent-flow gates", "nonce": "abc123"}
    assert gates_route_key(json.dumps(payload), nonce="abc123") == "green"


def test_cli_written_results_record_the_gate_phase(tmp_path):
    run_dir = create_run(tmp_path, "default", "task")
    path = write_gate_results(
        run_dir=run_dir,
        results=[GateResult("test", ("pytest", "-q"), True, 0, "ok", "")],
        phase="all",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["produced_by"]["gate_phase"] == "all"
    assert (
        gates_route_key(
            path.read_text(encoding="utf-8"), nonce=read_meta(run_dir)["gate_nonce"]
        )
        == "green"
    )


@pytest.mark.parametrize(
    "phase,expected_route",
    [("all", "green"), ("pre-commit", "default")],
)
def test_gates_cli_records_the_phase_it_filtered_on(tmp_path, phase, expected_route):
    """CLI가 `--phase`를 결과 파일로 옮기지 않으면 runner는 대조할 것이 없다.

    `all` 한 방향만 보면 `phase="all"` 하드코딩이 살아남는다. 그 변이는
    `--phase pre-commit` 결과에 `gate_phase: "all"`을 붙여 이 검사를 통째로
    무력화한다.
    """
    from unittest import mock

    from agent_flow.cli import main

    run_dir = create_run(tmp_path, "default", "task")
    results = [GateResult("lint", ("ruff", "check", "."), True, 0, "ok", "")]
    with mock.patch("agent_flow.cli.run_gates", return_value=results):
        exit_code = main(
            [
                "gates",
                "--root",
                str(tmp_path),
                "--profile",
                "generic",
                "--run-dir",
                str(run_dir),
                "--phase",
                phase,
            ]
        )

    assert exit_code == 0
    text = (run_dir / "artifacts" / "gate-results.json").read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["produced_by"]["gate_phase"] == phase
    assert (
        gates_route_key(text, nonce=read_meta(run_dir)["gate_nonce"]) == expected_route
    )


def test_gates_cli_says_why_a_partial_phase_will_not_pass(capsys, tmp_path):
    """green인 파일이 fix-loop로 되돌려지는 이유는 결과 목록에 안 보인다."""
    from unittest import mock

    from agent_flow.cli import main

    run_dir = create_run(tmp_path, "default", "task")
    results = [GateResult("lint", ("ruff", "check", "."), True, 0, "ok", "")]
    with mock.patch("agent_flow.cli.run_gates", return_value=results):
        main(
            [
                "gates",
                "--root",
                str(tmp_path),
                "--profile",
                "generic",
                "--run-dir",
                str(run_dir),
                "--phase",
                "pre-commit",
            ]
        )

    assert "--phase all" in capsys.readouterr().err


def test_python_and_node_agree_on_gate_phase(tmp_path):
    """이중 구현이 갈라지면 한쪽 runner에서만 pre-commit 결과가 QA로 통과한다."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    script = tmp_path / "probe.mjs"
    kit = (REPO / "bin" / "agent-flow-kit.mjs").read_text(encoding="utf-8")
    end = kit.index("\n}\n", kit.index("function hasGateEvidence(result)")) + 3
    helpers = kit[kit.index("function gateNonceMatches(data, nonce)"):end]
    script.write_text(
        "import fs from 'node:fs';\n"
        + helpers
        + "\nconst p = process.argv[2];\n"
        + "console.log(readGatesRouteKey(p, process.argv[3] ?? ''));\n",
        encoding="utf-8",
    )
    target = tmp_path / "gate-results.json"
    for phase, expected in (("pre-commit", "default"), ("all", "green")):
        payload = json.loads(FORGED)
        payload["produced_by"] = {
            "tool": "agent-flow gates",
            "nonce": "abc123",
            "gate_phase": phase,
        }
        target.write_text(json.dumps(payload), encoding="utf-8")
        node_key = subprocess.run(
            [node, str(script), str(target), "abc123"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert node_key == expected
        assert gates_route_key(target.read_text(encoding="utf-8"), nonce="abc123") == expected


def test_node_run_manifest_nonce_is_read(tmp_path):
    """Node runner는 manifest.json에 nonce를 쓴다. 못 읽으면 그 run은 영영 green이 안 된다."""
    run_dir = tmp_path / "run"
    (run_dir).mkdir()
    (run_dir / "manifest.json").write_text(
        json.dumps({"gate_nonce": "node-side-nonce"}), encoding="utf-8"
    )

    assert run_gate_nonce(run_dir) == "node-side-nonce"


def test_python_run_meta_nonce_wins_over_manifest(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "meta.json").write_text(
        json.dumps({"gate_nonce": "python-side"}), encoding="utf-8"
    )
    (run_dir / "manifest.json").write_text(
        json.dumps({"gate_nonce": "node-side"}), encoding="utf-8"
    )

    assert run_gate_nonce(run_dir) == "python-side"


def test_results_written_into_a_node_run_route_green(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "artifacts").mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps({"gate_nonce": "node-side-nonce"}), encoding="utf-8"
    )
    path = write_gate_results(
        run_dir=run_dir,
        results=[
            GateResult(
                gate_id="lint",
                command=("ruff", "check", "."),
                passed=True,
                exit_code=0,
                stdout="ok",
                stderr="",
            )
        ],
    )

    assert (
        gates_route_key(path.read_text(encoding="utf-8"), nonce="node-side-nonce")
        == "green"
    )


def test_gate_timeout_is_recorded_as_a_gate_result(tmp_path):
    """게이트 자체 timeout은 게이트 결과다. CLI 오류로 새어 나가면 안 된다."""
    result = run_gate(
        GateCommand(gate_id="slow", command=(sys.executable, "-c", "import time; time.sleep(5)")),
        cwd=tmp_path,
        timeout_s=1,
    )

    assert result.timed_out is True
    assert result.passed is False
    assert result.exit_code is None
    assert "timed out" in result.stderr


def test_gate_failure_is_not_marked_as_timeout(tmp_path):
    result = run_gate(
        GateCommand(gate_id="fail", command=(sys.executable, "-c", "raise SystemExit(3)")),
        cwd=tmp_path,
        timeout_s=30,
    )

    assert result.timed_out is False
    assert result.exit_code == 3


def _timed_out_payload(required: bool) -> str:
    return json.dumps(
        {
            "passed": not required,
            "status": "green" if not required else "request-changes",
            "produced_by": {"tool": "agent-flow gates", "nonce": "abc123"},
            "results": [
                {
                    "gate_id": "slow",
                    "command": "pytest -q",
                    "passed": False,
                    "required": required,
                    "exit_code": None,
                    "timed_out": True,
                    "stdout": "",
                    "stderr": "gate timed out after 600s",
                }
            ],
        }
    )


@pytest.mark.parametrize("required", [True, False])
def test_timed_out_gate_routes_to_error(required):
    """반증: optional 게이트가 상한을 다 쓰고 죽어도 passed 집계는 green이다."""
    assert gates_route_key(_timed_out_payload(required), nonce="abc123") == "error"


def test_node_and_python_agree_on_timeout_routing(tmp_path):
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    kit = (REPO / "bin" / "agent-flow-kit.mjs").read_text(encoding="utf-8")
    end = kit.index("\n}\n", kit.index("function hasGateEvidence(result)")) + 3
    helpers = kit[kit.index("function gateNonceMatches(data, nonce)"):end]
    script = tmp_path / "probe.mjs"
    script.write_text(
        "import fs from 'node:fs';\n"
        + helpers
        + "\nconsole.log(readGatesRouteKey(process.argv[2], process.argv[3] ?? ''));\n",
        encoding="utf-8",
    )
    target = tmp_path / "gate-results.json"
    target.write_text(_timed_out_payload(False), encoding="utf-8")

    routed = subprocess.run(
        [node, str(script), str(target), "abc123"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert routed == "error"


def test_written_timeout_survives_serialization_and_routes_to_error(tmp_path):
    """반증: timeout을 optional 실패로 집계하면 검증이 끊긴 실행이 green으로 남는다.

    artifact의 `passed`/`status`, 라우팅 키, CLI 종료 코드가 서로 다른 말을 하면
    셋 중 하나만 읽는 소비자가 timeout을 통과로 본다.
    """
    run_dir = tmp_path / "run"
    (run_dir / "artifacts").mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps({"gate_nonce": "abc123"}), encoding="utf-8"
    )
    path = write_gate_results(
        run_dir=run_dir,
        results=[
            GateResult(
                gate_id="required-ok",
                command=("true",),
                passed=True,
                exit_code=0,
                stdout="ok",
                stderr="",
            ),
            GateResult(
                gate_id="optional-slow",
                command=("pytest", "-q"),
                passed=False,
                exit_code=None,
                stdout="",
                stderr="gate timed out after 600s",
                required=False,
                timed_out=True,
            ),
        ],
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["passed"] is False
    assert payload["status"] == "error"
    assert any(entry["timed_out"] is True for entry in payload["results"])
    assert gates_route_key(
        path.read_text(encoding="utf-8"),
        nonce=run_gate_nonce(run_dir),
    ) == "error"
