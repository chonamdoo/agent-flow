"""gate-results.json 출처 검증.

실행은 runner가 한다(`runner.Runner._run_project_gates`). 그래서 `gates`는
`RUNNER_OWNED_PHASES`이고, 디스크에 이미 있는 결과 파일은 입력이 아니라 덮어쓸
출력이다 — 검증 결과의 작성자가 검증 대상 본인이 될 수 있는 경로를 없앤 것이다.

아래 nonce/`gate_phase` 테스트가 지키는 범위는 그만큼 좁아졌다. gates phase가
정상으로 돌면 라우팅 입력은 항상 runner가 방금 쓴 파일이므로, 남은 유효 범위는
gates가 도중에 끊긴 뒤(예: profile 오류로 `ValueError`) host가 더 새 파일을 써
넣는 잔여 경로다. 그 경로가 실재하므로 단언은 유지한다.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.artifact import create_run, read_meta, write_meta
from agent_flow.core.artifacts import (
    GATE_RESULTS_MAX_BYTES,
    read_deferred_ci_checks,
    run_gate_nonce,
    write_gate_results,
)
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
        phase="all",
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


def test_gate_writer_rejects_unknown_execution(tmp_path):
    run_dir = create_run(tmp_path, "default", "task")

    with pytest.raises(ValueError, match="gate execution must be one of"):
        write_gate_results(
            run_dir=run_dir,
            results=[GateResult("test", ("pytest", "-q"), True, 0, "ok", "")],
            phase="all",
            execution="remote",
        )


def test_gate_writer_rejects_unknown_phase(tmp_path):
    run_dir = create_run(tmp_path, "default", "task")

    with pytest.raises(ValueError, match="gate phase must be one of"):
        write_gate_results(
            run_dir=run_dir,
            results=[GateResult("test", ("pytest", "-q"), True, 0, "ok", "")],
            phase="everything",
        )


def test_legacy_gate_ledger_without_deferred_field_is_compatible(tmp_path):
    path = tmp_path / "artifacts" / "gate-results.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"passed": true}', encoding="utf-8")

    assert read_deferred_ci_checks(tmp_path) == ()


def test_gate_ledger_reader_rejects_fifo(tmp_path):
    path = tmp_path / "artifacts" / "gate-results.json"
    path.parent.mkdir(parents=True)
    os.mkfifo(path)

    with pytest.raises(OSError, match="not a regular file"):
        read_deferred_ci_checks(tmp_path)


def test_gate_ledger_reader_rejects_oversize_file(tmp_path):
    path = tmp_path / "artifacts" / "gate-results.json"
    path.parent.mkdir(parents=True)
    with path.open("wb") as handle:
        handle.truncate(GATE_RESULTS_MAX_BYTES + 1)

    with pytest.raises(OSError, match="too large"):
        read_deferred_ci_checks(tmp_path)


def test_ci_gate_reproduction_preserves_local_gate_ledger(tmp_path):
    run_dir = create_run(tmp_path, "default", "task")
    result = GateResult("pytest", ("pytest", "-q"), True, 0, "ok", "")
    local_path = write_gate_results(
        run_dir=run_dir,
        results=[result],
        execution="local",
        phase="all",
        deferred_ci_checks=("pytest",),
    )
    local_content = local_path.read_bytes()

    ci_path = write_gate_results(
        run_dir=run_dir,
        results=[result],
        phase="all",
        execution="ci",
    )

    assert ci_path.name == "gate-results-ci-all.json"
    assert local_path.read_bytes() == local_content
    assert json.loads(local_path.read_text(encoding="utf-8"))[
        "deferred_ci_checks"
    ] == ["pytest"]


def test_local_focused_reproduction_preserves_canonical_gate_ledger(tmp_path):
    run_dir = create_run(tmp_path, "default", "task")
    result = GateResult("lint", ("ruff", "check", "."), True, 0, "ok", "")
    canonical_path = write_gate_results(
        run_dir=run_dir,
        results=[result],
        execution="local",
        phase="all",
        deferred_ci_checks=("pytest",),
    )
    canonical_content = canonical_path.read_bytes()

    focused_path = write_gate_results(
        run_dir=run_dir,
        results=[result],
        execution="local",
        phase="pre-commit",
    )

    assert focused_path.name == "gate-results-local-pre-commit.json"
    assert canonical_path.read_bytes() == canonical_content


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
    artifact_name = (
        "gate-results.json"
        if phase == "all"
        else f"gate-results-local-{phase}.json"
    )
    text = (run_dir / "artifacts" / artifact_name).read_text(encoding="utf-8")
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
        phase="all",
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
        phase="all",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["passed"] is False
    assert payload["status"] == "error"
    assert any(entry["timed_out"] is True for entry in payload["results"])
    assert gates_route_key(
        path.read_text(encoding="utf-8"),
        nonce=run_gate_nonce(run_dir),
    ) == "error"


def _gates_runner(run_dir: Path, project_root: Path, config_root: Path):
    """`_run_project_gates`가 보는 최소 조립. 이 메서드는 세 경로만 본다."""
    from agent_flow.runner import Runner

    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.project_root = project_root
    runner.config_root = config_root
    return runner


def _declare_two_phase_gates(root: Path) -> None:
    """`pre-commit` 하나와 `pre-push` 하나를 선언한다. `--phase all`만 둘 다 고른다.

    두 명령을 다르게 두는 것은 필수다 — `profile_gate_commands`는 같은 argv를
    한 번만 담으므로 같은 명령이면 phase 필터와 무관하게 하나로 접힌다.
    """
    (root / ".agent-flow").mkdir(parents=True, exist_ok=True)
    (root / ".agent-flow" / "kit.json").write_text(
        json.dumps({"profile": "generic", "profiles": ["generic"]}), encoding="utf-8"
    )
    profiles = root / ".agent-flow" / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "generic.local.yaml").write_text(
        "gates:\n"
        "  - id: quick-lint\n"
        '    command: ["true"]\n'
        "    required: true\n"
        "    phase: pre-commit\n"
        "  - id: slow-build\n"
        '    command: ["true", "build"]\n'
        "    required: true\n"
        "    phase: pre-push\n",
        encoding="utf-8",
    )


def test_a_route_blocked_gates_phase_stays_blocked_instead_of_routing_a_forgery(tmp_path):
    """불변: fix-loop 상한에 막힌 gates는 멈춘 채 있고, 디스크의 파일로 라우팅하지 않는다.

    반증: 그 자리에서 파일을 읽어 라우팅하면 막힌 run이 green 한 줄로 상한을
    우회한다. 그 통로를 쓸 동기가 가장 큰 것이 바로 여기서 막힌 agent다. 재실행으로
    막는 것도 답이 아니다 — 같은 상한에 다시 걸리는 데 android 기준 수십 분이 든다.

    반증(예외): 판독 불가까지 멈추면 그 run은 gate를 다시 돌릴 방법이 없다. 남는
    안내는 "artifact를 고쳐라"인데 같은 run의 phase prompt가 그 행위를 금지한다.
    """
    from agent_flow.runner import Phase, Runner

    run_dir = create_run(tmp_path, "default", "task")
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    nonce = read_meta(run_dir)["gate_nonce"]

    gates = Phase(
        id="gates", description="", prompt="", artifact="artifacts/gate-results.json"
    )
    blocked = {
        "current_phase": "gates",
        "phase_blocked_reason": "route_blocked",
        "gate_nonce": nonce,
    }

    # 막히지 않은 자리는 언제나 다시 만든다.
    assert runner._runner_owned_phase_is_stuck(gates, {}) is False
    # 다른 phase가 막혀 있는 것은 gates와 무관하다.
    assert (
        runner._runner_owned_phase_is_stuck(
            gates, {"current_phase": "commit", "phase_blocked_reason": "route_blocked"}
        )
        is False
    )

    target = run_dir / "artifacts" / "gate-results.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    # nonce까지 맞춘 green 위조본. provenance 층은 이것을 통과시킨다.
    target.write_text(
        json.dumps(
            {
                "passed": True,
                "status": "green",
                "results": [
                    {
                        "gate_id": "forged",
                        "command": "true",
                        "required": True,
                        "passed": True,
                        "exit_code": 0,
                    }
                ],
                "produced_by": {
                    "tool": "agent-flow gates",
                    "nonce": nonce,
                    "gate_phase": "all",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # 이 파일은 라우팅되면 green이다. 그래서 라우팅에 닿기 전에 멈춰야 한다.
    assert gates_route_key(target.read_text(encoding="utf-8"), nonce=nonce) == "green"
    assert runner._runner_owned_phase_is_stuck(gates, blocked) is True

    # 판독 불가는 안정된 상태가 아니다 — 멈추지 않고 다시 만든다.
    target.write_text("{not json", encoding="utf-8")
    assert runner._runner_owned_phase_is_stuck(gates, blocked) is False

    # 깨진 UTF-8도 같다. 엄격하게 읽으면 여기서 UnicodeDecodeError가 나고 자동
    # 복구가 아예 돌지 않는다.
    target.write_bytes(b'{"passed": true, "results": [\xff\xfe]}')
    assert runner._runner_owned_phase_is_stuck(gates, blocked) is False


def test_runner_run_gates_overwrite_a_model_authored_result(tmp_path, monkeypatch):
    from agent_flow import runner as runner_module

    run_dir = create_run(tmp_path, "default", "task")
    _declare_two_phase_gates(tmp_path)
    target = run_dir / "artifacts" / "gate-results.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(FORGED, encoding="utf-8")
    seen: dict[str, object] = {}

    def fake_run_gates(commands, *, cwd, timeout_s=None, on_start=None):
        seen["cwd"] = cwd
        seen["gate_ids"] = [command.gate_id for command in commands]
        return [GateResult("slow-build", ("ruff", "check", "."), True, 0, "ok", "")]

    monkeypatch.setattr(runner_module, "run_gates", fake_run_gates)
    _gates_runner(run_dir, tmp_path, tmp_path)._run_project_gates()

    payload = json.loads(target.read_text(encoding="utf-8"))
    # cwd는 checkout이다. leader에서 돌면 빌드 산출물이 leader tripwire에 걸린다.
    assert seen["cwd"] == tmp_path
    # `--phase all`은 파일에 적힌 주장이 아니라 실행에 넘긴 필터다. 이 단언이 없으면
    # 필터를 `pre-commit`으로 좁혀도 파일에는 `all`이 찍혀 아무도 알아채지 못한다 —
    # android/ios의 build·test가 `pre-push`라 통째로 빠진 green이 된다.
    assert sorted(seen["gate_ids"]) == ["quick-lint", "slow-build"]
    assert payload["results"][0]["gate_id"] == "slow-build"
    assert "ruff" in payload["results"][0]["command"]
    assert payload["produced_by"]["gate_phase"] == "all"
    assert payload["produced_by"]["nonce"] == read_meta(run_dir)["gate_nonce"]
    assert (
        gates_route_key(
            target.read_text(encoding="utf-8"), nonce=read_meta(run_dir)["gate_nonce"]
        )
        == "green"
    )


def test_runner_gate_planning_honours_the_profile_override(tmp_path, monkeypatch):
    """불변: gate 계획의 profile 선택은 형제 소비자와 같다 — `AGENT_FLOW_PROFILE`이 최우선.

    반증: kit.json만 보면 `AGENT_FLOW_PROFILE=python`으로 도는 run이 `▶ profile: python`을
    찍고 python skill/branching을 쓰면서 QA는 kit.json의 generic gate 하나만 통과한
    green으로 commit까지 라우팅된다. 새 phase prompt가 `agent-flow gates` 수동 실행과
    결과 파일 작성을 금지하므로 그 차이를 메울 우회로도 없다.
    """
    from agent_flow import runner as runner_module

    run_dir = create_run(tmp_path, "default", "task")
    (tmp_path / ".agent-flow").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".agent-flow" / "kit.json").write_text(
        json.dumps({"profile": "generic", "profiles": ["generic"]}), encoding="utf-8"
    )
    monkeypatch.setenv("AGENT_FLOW_PROFILE", "python")
    seen: dict[str, object] = {}

    def fake_run_gates(commands, *, cwd, timeout_s=None, on_start=None):
        seen["gate_ids"] = [command.gate_id for command in commands]
        return [GateResult("type", ("true",), True, 0, "ok", "")]

    monkeypatch.setattr(runner_module, "run_gates", fake_run_gates)
    _gates_runner(run_dir, tmp_path, tmp_path)._run_project_gates()

    gate_ids = seen["gate_ids"]
    # python profile 고유 local gate. full pytest는 PR CI에서만 돈다.
    assert "type" in gate_ids
    assert "test" not in gate_ids
    payload = json.loads(
        (run_dir / "artifacts" / "gate-results.json").read_text(encoding="utf-8")
    )
    assert payload["deferred_ci_checks"] == ["pytest"]


def test_a_declared_gate_timeout_beats_the_default(tmp_path):
    slow = GateCommand(
        "build", (sys.executable, "-c", "import time; time.sleep(30)"), timeout_s=1
    )

    result = run_gate(slow, cwd=tmp_path)

    assert result.timed_out
    assert "after 1s" in result.stderr


def test_an_explicit_caller_timeout_beats_the_declared_one(tmp_path):
    """불변: 순서는 명시 > 선언 > 기본값이다.

    반증: 선언이 명시를 이기면 `agent-flow gates --timeout 60`이 android build에
    아무 효과가 없다. node wrapper는 그 플래그로 총예산을 계산하므로
    (`bin/agent-flow-kit.mjs`의 `relayTimeoutForSubcommand`) 상한을 낮추면 예산이
    실제 gate 상한보다 작아져 wrapper가 먼저 SIGKILL한다.
    """
    declared_long = GateCommand(
        "build", (sys.executable, "-c", "import time; time.sleep(30)"), timeout_s=30
    )

    result = run_gate(declared_long, cwd=tmp_path, timeout_s=1)

    assert result.timed_out
    assert "after 1s" in result.stderr


def test_the_resolved_timeout_order_is_explicit_then_declared_then_default(
    tmp_path, monkeypatch
):
    """불변: `subprocess.run`이 받는 상한은 명시 > 선언 > 기본값 순으로 결정된다.

    반증: 선언도 명시도 없을 때 상한이 사라지면(`timeout=None`) 멈춘 gate가 run을
    영구히 붙잡는다. 이 조합이 `--timeout` 기본값을 `None`으로 바꾼 뒤의 **기본
    경로**이고, runner는 timeout을 아예 넘기지 않으므로 항상 이 경로로 돈다.
    실제 시간을 쓰는 테스트로는 600s 기본값을 관측할 수 없어 인자를 직접 본다.
    """
    from agent_flow.core import gates as gates_module

    seen: list[object] = []

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        seen.append(kwargs["timeout"])
        return _Completed()

    monkeypatch.setattr(gates_module.subprocess, "run", fake_run)

    run_gate(GateCommand("lint", ("true",)), cwd=tmp_path)
    run_gate(GateCommand("build", ("true",), timeout_s=1800), cwd=tmp_path)
    run_gate(GateCommand("build", ("true",), timeout_s=1800), cwd=tmp_path, timeout_s=60)

    assert seen == [gates_module.DEFAULT_GATE_TIMEOUT_S, 1800, 60]


def test_slow_profile_gates_declare_their_own_timeout():
    """불변: gradle/xcodebuild gate는 자기 상한을 선언한다.

    반증: 기본 600s로 두면 콜드 캐시 빌드가 timeout=판정 불가로 기록된다. run에
    남는 문장이 "빌드가 깨졌다"가 아니라 "검증이 끊겼다"가 되고, gates는 error로
    라우팅되어 고칠 것이 없는 fix-loop 라운드를 태운다.
    """
    from agent_flow.core.gate_plan import profile_gate_commands

    android = {gate.gate_id: gate for gate in profile_gate_commands(["android"], phase="all")}
    ios = {gate.gate_id: gate for gate in profile_gate_commands(["ios"], phase="all")}

    assert android["build"].timeout_s == 1800
    assert android["test"].timeout_s == 1800
    assert ios["build"].timeout_s == 2400
    assert ios["test"].timeout_s == 2400
    # 빠른 gate는 선언하지 않는다. 선언하지 않은 값은 호출자 기본값이다.
    assert android["architecture-lint"].timeout_s is None


def test_run_gates_announces_each_gate_before_running_it(tmp_path):
    """불변: gate 시작이 관측된다.

    반증: `run_gate`는 출력을 캡처한다. 알림이 없으면 분 단위 gate가 도는 동안
    신호가 0이고 멈춘 것과 구분되지 않는다.
    """
    from agent_flow.core.gates import run_gates

    commands = [
        GateCommand("first", (sys.executable, "-c", "print(1)")),
        GateCommand("second", (sys.executable, "-c", "print(2)")),
    ]
    announced: list[tuple[str, int, int]] = []

    results = run_gates(
        commands,
        cwd=tmp_path,
        on_start=lambda gate, index, total: announced.append((gate.gate_id, index, total)),
    )

    assert announced == [("first", 1, 2), ("second", 2, 2)]
    assert [result.gate_id for result in results] == ["first", "second"]


def test_a_gate_result_written_before_the_phase_is_replaced_by_the_runner(
    tmp_path, monkeypatch
):
    """불변: gates에 **도달하기 전에** 놓인 결과 파일은 라우팅 입력이 되지 않는다.

    반증: `runner.py`의 loop guard에서 `_phase_regenerates_artifact` 항을 빼면
    (= `if self._has_artifact(phase):`) 이 위조본이 그대로 읽혀 green으로 라우팅되고
    run은 다음 phase까지 전진한다. 그 파일을 쓸 수 있는 것은 검증 대상 본인이고,
    위조를 막던 nonce는 run meta에 있어 읽어 복사할 수 있다 — 이 변경이 닫으려던
    구멍이 그대로 열린다.

    fixture가 meta의 `current_phase`/`phase_entered_at`을 미리 찍는 것은 필수다.
    찍지 않으면 runner가 진입 시각을 지금으로 갱신해 심어 둔 파일이 항상 과거가
    되고, guard를 빼도 라우팅이 아니라 `stale_artifact` 차단으로 멈춘다 — mutant는
    죽지만 반증 문장이 가리키는 경로는 재현되지 않는다. 여기서 재현하는 것은 모듈
    docstring이 말하는 "gates가 도중에 끊긴 뒤 host가 더 새 파일을 써 넣는" 경로다.

    predicate 단위 테스트로는 이 배선이 잡히지 않는다. gates가 뒤로 라우팅될 때는
    `_plan_transition`이 자기 artifact를 무효화하므로 재진입 시점에 파일이 없고,
    guard가 있으나 없으나 같은 분기를 탄다.
    """
    import agent_flow.runner as runner_module
    from agent_flow.adapters.generic import GenericAdapter
    from agent_flow.runner import Phase, ResumeMode, Runner

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("AGENT_FLOW_GENERIC_MODE", "emit")
    monkeypatch.setattr(runner_module, "detect_adapter", GenericAdapter)
    monkeypatch.setattr(
        runner_module, "assert_managed_hooks_registered", lambda *a, **k: None
    )
    monkeypatch.setattr(runner_module, "detect_available_clis", lambda: [])

    run_dir = create_run(project, "development", "replace the planted result")
    # gates에 이미 진입한 뒤 끊긴 run을 재현한다. 진입 시각이 과거여야 심어 둔
    # 파일이 stale로 걸러지지 않고 라우팅 입력 후보가 된다.
    meta = read_meta(run_dir)
    meta["current_phase"] = "gates"
    meta["phase_index"] = 0
    meta["phase_entered_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=5)
    ).isoformat()
    write_meta(run_dir, meta)
    planted = run_dir / "artifacts" / "gate-results.json"
    planted.parent.mkdir(parents=True, exist_ok=True)
    # nonce까지 맞춘 위조본. 현재 provenance 층이 통과시키는 형태다.
    planted.write_text(
        json.dumps(
            {
                "passed": True,
                "status": "green",
                "results": [
                    {
                        "gate_id": "planted",
                        "command": "true",
                        "required": True,
                        "passed": True,
                        "exit_code": 0,
                    }
                ],
                "produced_by": {
                    "tool": "agent-flow gates",
                    "nonce": read_meta(run_dir)["gate_nonce"],
                    "gate_phase": "all",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_run_gates(commands, *, cwd, timeout_s=None, on_start=None):
        return [GateResult("architecture-lint", ("true",), True, 0, "ran", "")]

    monkeypatch.setattr(runner_module, "run_gates", fake_run_gates)

    runner = Runner(project, run_dir=run_dir, workflow="development")
    runner.phases = [
        Phase(
            id="gates",
            description="gates",
            artifact="artifacts/gate-results.json",
            routes={"green": "after"},
        ),
        Phase(id="after", description="stop here"),
    ]
    runner.run(ResumeMode.RESUME)

    payload = json.loads(planted.read_text(encoding="utf-8"))
    gate_ids = [entry["gate_id"] for entry in payload["results"]]
    assert "planted" not in gate_ids
    assert gate_ids == ["architecture-lint"]
