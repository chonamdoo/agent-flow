"""Phase trace와 gate 판정 불가에 대한 반증 테스트.

고치기 전 동작: 주입된 프롬프트는 stdout으로만 나가 재현이 불가능했고, 읽을 수
없는 `gate-results.json`은 `default` route로 접혀 fix-loop가 근거 없이 돌았다.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agent_flow.core.context_contract import offload_tool_output  # noqa: E402
from agent_flow.core.observation import (  # noqa: E402
    PHASE_ENTERED,
    PHASE_EXITED,
    PROMPT_RENDERED,
    PhaseObservation,
    record_observation,
)
from agent_flow.core.route_verdicts import (  # noqa: E402
    GATE_MALFORMED,
    gate_parse_error,
    gates_route_key,
)


def _events(run_dir: Path) -> list[dict]:
    path = run_dir / "context" / "events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_every_visited_phase_is_traced_not_only_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """반증: `phase_entered`를 `phase_entered_at`이 빌 때만 방출하면 첫 phase에서만 난다.

    전이가 다음 phase를 **진입 전에** 이미 stamp하므로, 정상 전이한 두 번째 이후
    phase는 진입 기록이 아예 없었다 — 약속한 trace 순서가 사실이 아니었다.
    """
    import agent_flow.runner as runner_module
    from agent_flow.adapters.generic import GenericAdapter
    from agent_flow.runner import Phase, ResumeMode, Runner

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("AGENT_FLOW_GENERIC_MODE", "stub-success")
    monkeypatch.setattr(runner_module, "detect_adapter", GenericAdapter)
    monkeypatch.setattr(
        runner_module, "assert_managed_hooks_registered", lambda *a, **k: None
    )
    monkeypatch.setattr(runner_module, "detect_available_clis", lambda: [])

    runner = Runner(project, workflow="development")
    runner.phases = [
        Phase(id="alpha", description="first"),
        Phase(id="beta", description="second"),
        Phase(id="gamma", description="third"),
    ]
    runner.run(ResumeMode.START, task="trace every phase")

    run_dir = runner.run_dir
    assert run_dir is not None
    events = [(e["event"], e["details"]["phase"]) for e in _events(run_dir)]
    for phase_id in ("alpha", "beta", "gamma"):
        assert [kind for kind, phase in events if phase == phase_id] == [
            PHASE_ENTERED,
            PROMPT_RENDERED,
            PHASE_EXITED,
        ]


def test_large_payload_is_offloaded_and_the_event_keeps_only_a_reference(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    prompt = "envelope body\n" * 500
    record_observation(
        run_dir=run_dir,
        observation=PhaseObservation(
            kind=PROMPT_RENDERED,
            phase_id="review",
            payload=prompt,
            payload_name="prompt-review",
        ),
    )
    rendered = [e for e in _events(run_dir) if e["event"] == PROMPT_RENDERED]
    assert len(rendered) == 1
    details = rendered[0]["details"]
    assert details["phase"] == "review"
    assert details["payload_bytes"] == len(prompt.encode("utf-8"))
    assert len(details["payload_sha256"]) == 64
    # 참조만 남는다. 전문이 event에 들어가면 trace가 곧 프롬프트 사본이 된다.
    assert "envelope body" not in json.dumps(rendered[0])
    payload_path = run_dir / details["payload_path"]
    assert payload_path.read_text(encoding="utf-8") == prompt


def test_offloaded_event_path_is_relative_to_the_run(tmp_path: Path):
    """절대 경로를 남기면 호스트 레이아웃이 PR과 archive로 새어 나간다."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    offload_tool_output(root=tmp_path, run_dir=run_dir, name="out", content="x" * 64)
    offloaded = [e for e in _events(run_dir) if e["event"] == "tool_output_offloaded"]
    assert len(offloaded) == 1
    recorded = offloaded[0]["details"]["path"]
    assert not Path(recorded).is_absolute()
    assert str(tmp_path) not in recorded
    assert (run_dir / recorded).is_file()


def test_recording_never_raises_when_the_trace_cannot_be_written(tmp_path: Path):
    """관측 실패가 전이를 막으면 안 된다. trace는 증거지 전이 조건이 아니다."""
    run_dir = tmp_path / "run"
    run_dir.write_text("not a directory", encoding="utf-8")
    record_observation(
        run_dir=run_dir,
        observation=PhaseObservation(kind=PHASE_ENTERED, phase_id="explore"),
    )


def test_details_that_json_cannot_serialize_are_recorded_as_strings(tmp_path: Path):
    """`Path`/`set`/dataclass는 `json.dumps`에서 `TypeError`다.

    경계가 `OSError`만 닫으면 그 예외가 원장을 쓴 **뒤에** phase 루프를 죽인다.
    관측은 부수적 증거이므로 죽이지 말고 문자열로 강제한다.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    record_observation(
        run_dir=run_dir,
        observation=PhaseObservation(
            kind=PHASE_ENTERED,
            phase_id="explore",
            details={
                "artifact": run_dir / "explore.md",
                "angles": {"security", "design"},
                "depth": float("nan"),
            },
        ),
    )

    entered = [e for e in _events(run_dir) if e["event"] == PHASE_ENTERED]
    assert len(entered) == 1
    details = entered[0]["details"]
    assert details["artifact"].endswith("explore.md")
    assert details["angles"] == ["design", "security"]
    # NaN/Infinity는 `json.dumps`가 조용히 적지만 JSON이 아니다. 그 줄은 kit의
    # JS 쪽 파서에서 죽는다.
    assert details["depth"] == "nan"


def test_a_surrogate_in_a_payload_does_not_kill_the_phase_loop(tmp_path: Path):
    """짝 없는 surrogate는 utf-8 인코딩에서 `UnicodeEncodeError`(=`ValueError`)다."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    record_observation(
        run_dir=run_dir,
        observation=PhaseObservation(
            kind=PROMPT_RENDERED,
            phase_id="review",
            details={"note": "broken \ud800 name"},
            payload="envelope \ud800 body",
            payload_name="prompt-review",
        ),
    )

    rendered = [e for e in _events(run_dir) if e["event"] == PROMPT_RENDERED]
    assert len(rendered) == 1
    details = rendered[0]["details"]
    # 증거가 남아야 한다. 예외를 삼키고 event를 버리면 그 phase의 프롬프트가
    # trace에서 통째로 사라진다.
    assert (run_dir / details["payload_path"]).is_file()
    assert len(details["payload_sha256"]) == 64
    assert "\ud800" not in details["note"]


def test_unknown_observation_kind_is_refused(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(ValueError):
        record_observation(
            run_dir=run_dir,
            observation=PhaseObservation(kind="whatever", phase_id="explore"),
        )


def test_committing_a_transition_records_why_it_moved(tmp_path: Path):
    """route 판정은 stdout에만 있었다. trace에 없으면 사후에 왜 되돌아갔는지 모른다."""
    from agent_flow.runner import Phase, Runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "review.md").write_text("verdict: request-changes\n", encoding="utf-8")
    (run_dir / "fix.md").write_text("done\n", encoding="utf-8")

    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.config_root = tmp_path
    runner.phases = [
        Phase(id="fix", description="", routes={"default": "review"}),
        Phase(id="review", description="", routes={"request-changes": "fix"}),
    ]

    transition = runner._plan_transition(1, runner.phases[1])
    runner._commit_transition(transition)

    exited = [e for e in _events(run_dir) if e["event"] == "phase_exited"]
    assert len(exited) == 1
    details = exited[0]["details"]
    assert details["phase"] == "review"
    assert details["route_key"] == "request-changes"
    assert details["to_phase"] == "fix"
    assert sorted(details["invalidated"]) == ["fix.md", "review.md"]


@pytest.mark.parametrize(
    "text",
    [
        "status: pass",
        "",
        "[1, 2]",
        '{"results": []}',
        '{"passed": "true"}',
    ],
)
def test_unreadable_gate_results_are_not_a_gate_failure(text: str):
    assert gates_route_key(text) == GATE_MALFORMED
    assert gate_parse_error(text)


def test_readable_gate_results_still_route_as_before():
    assert gates_route_key('{"passed": false, "results": []}') == "request-changes"
    assert (
        gates_route_key(
            '{"passed": true, "results": [{"command": "pytest -q", "passed": true, "output": "ok"}]}'
        )
        == "green"
    )


def test_node_and_python_agree_that_unreadable_results_are_malformed(tmp_path: Path):
    """이중 구현이 갈라지면 한쪽 진입점에서만 깨진 파일이 fix-loop를 돌린다."""
    import shutil

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    kit = (REPO / "bin" / "agent-flow-kit.mjs").read_text(encoding="utf-8")
    end = kit.index("\n}\n", kit.index("function hasGateEvidence(result)")) + 3
    helpers = kit[kit.index("function gateNonceMatches(data, nonce)"):end]
    probe = tmp_path / "probe.mjs"
    probe.write_text(
        "import fs from 'node:fs';\n"
        + helpers
        + "\nconsole.log(readGatesRouteKey(process.argv[2], ''));\n",
        encoding="utf-8",
    )
    target = tmp_path / "gate-results.json"
    for text in ("status: pass", "[1, 2]", '{"results": []}'):
        target.write_text(text, encoding="utf-8")
        node_key = subprocess.run(
            [node, str(probe), str(target)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert node_key == GATE_MALFORMED
        assert gates_route_key(text) == GATE_MALFORMED

    # 파일 자체가 없는 것은 "아직 안 돌렸다"이지 깨진 것이 아니다.
    missing = subprocess.run(
        [node, str(probe), str(tmp_path / "absent.json")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert missing == "default"

    # 반대로 **존재하는데** 열 수 없는 결과(EACCES·EISDIR·ELOOP)를 "안 돌림"으로
    # 읽으면, 읽지도 못한 파일을 근거로 fix-loop가 돈다. ENOENT만 default다.
    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    blocked = subprocess.run(
        [node, str(probe), str(unreadable)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert blocked == GATE_MALFORMED
