"""run cursor 검증과 원자적 phase 전이에 대한 반증 테스트.

예전 resume는 `int(meta.get("phase_index", 0) or 0)` 하나였다. 음수는 마지막
phase를 돌리고, 길이 이상은 곧바로 완료로 빠졌으며, `current_phase`와 어긋나도
아무도 보지 않았다. 전이도 원자적이지 않아 backward route가 artifact를 먼저
지우고 cursor를 나중에 썼다 — 그 사이에 죽으면 되돌린 근거가 사라진 채 이전
phase를 다시 돌았다.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = KIT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent_flow.artifact import read_meta, write_meta
from agent_flow.core.phase_workflow import (
    ACCEPT_WORKFLOW_DRIFT_FLAG,
    CorruptRunCursorError,
    CursorScope,
    RunCursor,
    WorkflowDriftError,
    PhaseWorkflowDefinition,
    load_phase_workflow_definition,
)
from agent_flow.core.worktree_isolation import (
    HOST_PHASE_LEADER_BASELINE_KEY,
    LeaderSnapshot,
    WorktreeIsolationError,
    leader_snapshot_payload,
    leader_sweep_scope,
    real_path,
)
from agent_flow.runner import (
    TRANSITIONS_FILE,
    Phase,
    Runner,
    _phases_from_definition,
)
from agent_flow.core.host_phase_baseline import BASELINE_RECORD_VERSION


def _development() -> PhaseWorkflowDefinition:
    return load_phase_workflow_definition(KIT_ROOT, "development")


def _scope(workflow) -> CursorScope:
    return CursorScope.of(workflow)


def _runner(run_dir: Path, phases: list[Phase]) -> Runner:
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.config_root = run_dir
    runner.phases = phases
    runner.accept_workflow_drift = False
    return runner


def _development_runner(tmp_path: Path) -> tuple[Runner, list[Phase]]:
    run_dir = tmp_path / ".agent-flow" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    definition = _development()
    phases = _phases_from_definition(definition)
    runner = _runner(run_dir, phases)
    runner.workflow = definition
    return runner, phases


def test_negative_phase_index_stops_instead_of_running_the_last_phase():
    workflow = _development()
    last = workflow.phases[-1].id

    with pytest.raises(CorruptRunCursorError) as caught:
        RunCursor.from_meta({"phase_index": -1}, _scope(workflow))

    # 파이썬 음수 인덱스는 조용히 마지막 phase를 연다. 그 이름이 오류에 없다는
    # 것으로 "마지막 phase로 접히지 않았다"를 고정한다.
    assert last not in str(caught.value)
    assert "-1" in str(caught.value)


def test_phase_index_past_the_workflow_stops_instead_of_completing():
    workflow = _development()
    total = len(workflow.phases)

    with pytest.raises(CorruptRunCursorError):
        RunCursor.from_meta({"phase_index": total + 1}, _scope(workflow))

    # 완료 커서(정확히 total, phase 이름 없음)만 통과한다. 이름이 남아 있으면
    # 두 필드가 서로 다른 이야기를 하는 것이라 손상이다.
    with pytest.raises(CorruptRunCursorError):
        RunCursor.from_meta(
            {"phase_index": total, "current_phase": workflow.phases[0].id},
            _scope(workflow),
        )
    assert (
        RunCursor.from_meta({"phase_index": total}, _scope(workflow)).phase_index
        == total
    )


def test_phase_index_that_disagrees_with_current_phase_stops():
    workflow = _development()

    with pytest.raises(CorruptRunCursorError):
        RunCursor.from_meta(
            {"phase_index": 0, "current_phase": workflow.phases[1].id},
            _scope(workflow),
        )


def test_non_integer_phase_index_stops_instead_of_raising_value_error():
    workflow = _development()

    with pytest.raises(CorruptRunCursorError):
        RunCursor.from_meta({"phase_index": "two"}, _scope(workflow))
    # bool은 int의 하위형이다. 걸러지지 않으면 `True`가 index 1로 통과한다.
    with pytest.raises(CorruptRunCursorError):
        RunCursor.from_meta({"phase_index": True}, _scope(workflow))


def test_workflow_change_after_the_run_started_is_reported_as_drift():
    workflow = _development()
    meta = {"phase_index": 0, "workflow_digest": "0" * 64}

    with pytest.raises(WorkflowDriftError) as caught:
        RunCursor.from_meta(meta, _scope(workflow))

    # kit 업그레이드가 workflow YAML을 바꾼다. 탈출구를 지목하지 않으면 진행 중인
    # 모든 run이 exit 2로 굳고, "finish"는 이 예외가 막는 바로 그것이다.
    message = str(caught.value)
    assert ACCEPT_WORKFLOW_DRIFT_FLAG in message
    assert "Finish or abort" not in message


def test_accepting_workflow_drift_re_baselines_the_run(tmp_path: Path):
    runner, _phases = _development_runner(tmp_path)
    runner.accept_workflow_drift = True
    write_meta(
        runner.run_dir,
        {"run_id": "r1", "phase_index": 0, "workflow_digest": "0" * 64},
    )

    cursor = runner._run_cursor(read_meta(runner.run_dir))

    assert cursor.workflow_digest == runner.workflow.digest
    # 승인은 다시 찍어야 끝난다. meta에 남겨 두면 다음 실행이 같은 벽을 다시 만난다.
    assert read_meta(runner.run_dir)["workflow_digest"] == runner.workflow.digest


def test_accepted_drift_re_anchors_the_cursor_on_the_recorded_phase_name():
    """반증: 승인은 digest 비교만 우회했다. 새 정의가 현재 phase 앞에 phase를
    끼워 넣으면 옛 index는 다른 phase를 가리키고, 우리가 안내한
    `agent-flow continue --accept-workflow-drift`가 `CorruptRunCursorError`로
    죽어 복구할 길이 없었다."""
    workflow = _development()
    ids = tuple(phase.id for phase in workflow.phases)
    recorded = {
        "phase_index": 2,
        "current_phase": ids[2],
        "workflow_digest": workflow.digest,
    }
    inserted = CursorScope("development", "development.yaml", "1" * 64, ("bootstrap",) + ids)
    reordered = CursorScope("development", "development.yaml", "2" * 64, tuple(reversed(ids)))

    for scope in (inserted, reordered):
        cursor = RunCursor.from_meta(recorded, scope, accept_workflow_drift=True)

        assert cursor.phase_id == ids[2]
        assert cursor.phase_index == scope.phase_ids.index(ids[2])


def test_accepted_drift_that_cannot_place_the_phase_points_at_abort_not_the_flag():
    """이름이 새 정의에 아예 없으면 재배치할 자리가 없다. 그때 다시 flag를 권하면
    사용자는 방금 실패한 명령을 또 실행한다."""
    workflow = _development()
    ids = tuple(phase.id for phase in workflow.phases)
    dropped = CursorScope(
        "development", "development.yaml", "3" * 64, tuple(i for i in ids if i != ids[2])
    )

    with pytest.raises(CorruptRunCursorError) as caught:
        RunCursor.from_meta(
            {"phase_index": 2, "current_phase": ids[2], "workflow_digest": workflow.digest},
            dropped,
            accept_workflow_drift=True,
        )

    message = str(caught.value)
    assert ACCEPT_WORKFLOW_DRIFT_FLAG not in message
    assert "agent-flow abort" in message


def test_a_re_anchored_cursor_is_written_back_so_the_next_run_is_not_blocked(
    tmp_path: Path,
):
    """digest만 다시 찍고 index를 남겨 두면, drift가 사라진 다음 실행이 옛 index와
    이름의 불일치로 막힌다 — 승인은 한 번으로 끝나야 한다."""
    runner, phases = _development_runner(tmp_path)
    runner.accept_workflow_drift = True
    # 새 정의에서 첫 phase가 사라져 'review'가 한 칸 앞으로 온 상황.
    runner.phases = phases[1:]
    moved = [phase.id for phase in runner.phases].index("review")
    write_meta(
        runner.run_dir,
        {
            "run_id": "r1",
            "phase_index": 2,
            "current_phase": "review",
            "workflow_digest": "0" * 64,
        },
    )

    cursor = runner._run_cursor(read_meta(runner.run_dir))

    assert cursor.phase_index == moved
    persisted = read_meta(runner.run_dir)
    assert persisted["phase_index"] == moved
    assert persisted["current_phase"] == "review"
    runner.accept_workflow_drift = False
    assert runner._run_cursor(read_meta(runner.run_dir)).phase_index == moved


def test_a_progressed_cursor_without_a_current_phase_stops():
    """반증: 이름이 없는 손상 meta가 통과하면 runner는 숫자만 믿고 그 phase부터
    재개해 앞선 필수 phase를 건너뛴다. 빈 문자열은 이제 별도 진단으로 갈라졌다 —
    `test_an_empty_current_phase_is_corruption_not_an_absent_name`."""
    workflow = _development()
    scope = _scope(workflow)
    total = len(workflow.phases)

    with pytest.raises(CorruptRunCursorError) as caught:
        RunCursor.from_meta({"phase_index": 2}, scope)
    assert workflow.phases[2].id in str(caught.value)

    # 예외는 정확히 둘이다: 아직 아무 phase도 찍지 않은 새 run과 완료 커서.
    assert RunCursor.from_meta({"current_phase": None}, scope).phase_index == 0
    assert RunCursor.from_meta({"phase_index": total}, scope).phase_index == total


def test_cursor_scope_carries_the_digest_of_the_definition_it_came_from():
    """합성 정의는 원문 digest를 유지한 위조품이었다. scope는 위조할 형태가 없다."""
    workflow = _development()
    runner = Runner.__new__(Runner)
    runner.workflow = workflow
    runner.phases = _phases_from_definition(workflow)[:2]

    scope = runner._cursor_scope()

    assert scope.digest == workflow.digest
    assert scope.phase_ids == tuple(phase.id for phase in runner.phases)
    assert not hasattr(scope, "phases")


def test_a_run_without_a_recorded_digest_passes_and_is_backfilled(tmp_path: Path):
    runner, _phases = _development_runner(tmp_path)
    write_meta(runner.run_dir, {"run_id": "r1", "phase_index": 0})

    cursor = runner._run_cursor(read_meta(runner.run_dir))

    assert cursor.workflow_digest == runner.workflow.digest
    assert read_meta(runner.run_dir)["workflow_digest"] == runner.workflow.digest


def test_create_run_records_the_workflow_digest(tmp_path: Path):
    from agent_flow.artifact import create_run

    run_path = create_run(tmp_path, "development", "task")

    assert read_meta(run_path)["workflow_digest"] == _development().digest


def test_a_corrupt_meta_is_not_replaced_by_the_digest_backfill(tmp_path: Path):
    """반증: `read_meta`는 손상·OSError·decode 실패를 stderr로 알리고 빈 dict를
    돌려준다. 그 dict에 digest backfill을 걸면 `write_meta`가 **원자적 교체**를 해
    run_id·task·task_digest·gate_nonce·checkout identity가 첫 `continue`에서
    사라진다. 읽지 못한 meta는 덮어쓰는 대신 멈춰야 한다.
    """
    runner, _phases = _development_runner(tmp_path)
    meta_path = runner.run_dir / "meta.json"
    # append 중에 죽은 meta. 사람이 복구할 값이 아직 전부 들어 있다.
    corrupt = b'{"run_id": "r1", "task": "ship it", "gate_nonce": "n1", "phase_index": 0'
    meta_path.write_bytes(corrupt)

    with pytest.raises(CorruptRunCursorError):
        runner._run_cursor(read_meta(runner.run_dir))

    assert meta_path.read_bytes() == corrupt


def test_the_route_key_travels_with_the_decision_not_on_the_instance(tmp_path: Path):
    """반증: key를 인스턴스 속성으로 흘려보내면 `_plan_transition`이 `_next_index`
    **바로 다음에** 불려야만 원장의 route_key가 실제 판정과 같다. 원장의 route_key는
    재개가 왜 되돌아갔는지를 말하는 유일한 근거이므로 호출 순서에 걸 수 없다.
    """
    runner, phases = _development_runner(tmp_path)
    fix_index = [phase.id for phase in phases].index("fix-loop")
    (runner.run_dir / "fix-loop.md").write_text("fixed\n", encoding="utf-8")
    write_meta(
        runner.run_dir,
        {"run_id": "r1", "phase_index": fix_index, "current_phase": "fix-loop"},
    )

    decision = runner._next_index(fix_index, phases[fix_index])
    transition = runner._plan_transition(fix_index, phases[fix_index])

    assert decision.route_key == "default"
    assert (decision.to_index, decision.blocked) == (transition.to_index, transition.blocked)
    assert transition.route_key == decision.route_key
    # 판정을 흘려보내던 자리가 남아 있으면 순서 결합도 남아 있다.
    assert not hasattr(runner, "_last_route_key")


def test_backward_route_journals_what_it_invalidated_and_keeps_the_journal(
    tmp_path: Path,
):
    """development.yaml의 review 되돌림은 이제 이 한 경로에서만 집행된다."""
    runner, phases = _development_runner(tmp_path)
    ids = [phase.id for phase in phases]
    review_index = ids.index("review")
    fix_index = ids.index("fix-loop")
    for phase_id in ("explore", "implement", "review", "qa", "fix-loop"):
        (runner.run_dir / f"{phase_id}.md").write_text(
            "fixed\n", encoding="utf-8"
        )
    write_meta(
        runner.run_dir,
        {"phase_index": fix_index, "current_phase": "fix-loop"},
    )

    transition = runner._plan_transition(fix_index, phases[fix_index])
    assert (transition.to_index, transition.blocked) == (review_index, False)
    runner._commit_transition(transition)

    journal = runner.run_dir / TRANSITIONS_FILE
    record = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
    assert record["from_phase"] == "fix-loop"
    assert record["to_phase"] == "review"
    assert record["invalidated"] == ["review.md", "qa.md", "fix-loop.md"]
    # 무효화된 것은 phase 산출물뿐이다. 원장까지 지우면 복구 근거가 함께 사라진다.
    for name in record["invalidated"]:
        assert not (runner.run_dir / name).exists()
    assert journal.is_file()
    assert (runner.run_dir / "explore.md").is_file()
    assert read_meta(runner.run_dir)["current_phase"] == "review"


def test_an_interrupted_transition_is_completed_on_the_next_run_and_is_idempotent(
    tmp_path: Path,
):
    runner, phases = _development_runner(tmp_path)
    ids = [phase.id for phase in phases]
    review_index = ids.index("review")
    fix_index = ids.index("fix-loop")
    for phase_id in ("review", "qa", "fix-loop"):
        (runner.run_dir / f"{phase_id}.md").write_text("fixed\n", encoding="utf-8")
    write_meta(
        runner.run_dir,
        {"phase_index": fix_index, "current_phase": "fix-loop"},
    )

    # crash 재현: 원장만 적히고 무효화도 cursor도 아직 없는 상태.
    transition = runner._plan_transition(fix_index, phases[fix_index])
    runner._append_transition_journal(transition)
    assert (runner.run_dir / "review.md").is_file()
    assert read_meta(runner.run_dir)["phase_index"] == fix_index

    runner._resume_pending_transition()
    resumed = read_meta(runner.run_dir)
    assert resumed["phase_index"] == review_index
    assert resumed["current_phase"] == "review"
    assert not (runner.run_dir / "review.md").exists()
    journal_lines = (
        (runner.run_dir / TRANSITIONS_FILE).read_text(encoding="utf-8").splitlines()
    )

    # 두 번째 적용도 같은 결과여야 한다 — 재개는 멱등이다.
    runner._resume_pending_transition()
    assert read_meta(runner.run_dir)["phase_index"] == review_index
    assert read_meta(runner.run_dir)["current_phase"] == "review"
    assert not (runner.run_dir / "review.md").exists()
    assert (
        (runner.run_dir / TRANSITIONS_FILE).read_text(encoding="utf-8").splitlines()
        == journal_lines
    )


def _journal_record(
    runner: Runner,
    phases: list[Phase],
    *,
    invalidated: list[str] | None = None,
    skipped: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    """fix-loop → review 되돌림 한 줄. 부수 효과만 호출자가 정한다."""
    ids = [phase.id for phase in phases]
    return {
        "at": "2026-01-01T00:00:00+00:00",
        "from_index": ids.index("fix-loop"),
        "from_phase": "fix-loop",
        "route_key": "default",
        "to_index": ids.index("review"),
        "to_phase": "review",
        "blocked": False,
        "invalidated": invalidated or [],
        "skipped": skipped or [],
    }


def _stage_journal(runner: Runner, phases: list[Phase], *lines: str) -> None:
    ids = [phase.id for phase in phases]
    write_meta(
        runner.run_dir,
        {"phase_index": ids.index("fix-loop"), "current_phase": "fix-loop"},
    )
    (runner.run_dir / TRANSITIONS_FILE).write_text(
        "".join(f"{line}\n" for line in lines), encoding="utf-8"
    )


@pytest.mark.parametrize("escape", ["absolute", "parent"])
def test_a_journal_line_cannot_delete_a_file_outside_the_run(
    tmp_path: Path, escape: str
):
    """`run_dir / "/Users/me/.zshrc"`는 run_dir을 버리고 그 절대 경로가 된다.

    원장은 run 디렉터리 안에 있고 phase agent가 쓴다. 그 한 줄이 호스트 파일을
    지울 수 있으면 재개는 임의 삭제 원시함수다.
    """
    runner, phases = _development_runner(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me\n", encoding="utf-8")
    relative = (
        str(outside) if escape == "absolute" else "../../../outside.txt"
    )
    _stage_journal(
        runner,
        phases,
        json.dumps(_journal_record(runner, phases, invalidated=[relative])),
    )

    runner._resume_pending_transition()

    assert outside.read_text(encoding="utf-8") == "keep me\n"
    # 거부한 레코드는 cursor도 옮기지 않는다. 절반만 적용하면 남은 절반이
    # 무엇이었는지 아무도 모른다.
    meta = read_meta(runner.run_dir)
    assert meta["current_phase"] == "fix-loop"


def test_a_journal_line_cannot_overwrite_a_file_outside_the_run(tmp_path: Path):
    """skip 표식은 `mkdir(parents=True)` 뒤에 내용까지 쓴다."""
    runner, phases = _development_runner(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me\n", encoding="utf-8")
    _stage_journal(
        runner,
        phases,
        json.dumps(
            _journal_record(
                runner,
                phases,
                skipped=[{"path": str(outside), "content": "clobbered\n"}],
            )
        ),
    )

    runner._resume_pending_transition()

    assert outside.read_text(encoding="utf-8") == "keep me\n"
    assert read_meta(runner.run_dir)["current_phase"] == "fix-loop"


def test_a_declared_nested_artifact_is_still_applied(tmp_path: Path):
    """봉쇄가 `artifacts/gate-results.json` 같은 정상 경로를 막으면 안 된다."""
    runner, phases = _development_runner(tmp_path)
    nested = runner.run_dir / "artifacts" / "gate-results.json"
    nested.parent.mkdir(parents=True)
    nested.write_text("{}\n", encoding="utf-8")
    _stage_journal(
        runner,
        phases,
        json.dumps(
            _journal_record(
                runner,
                phases,
                invalidated=["artifacts/gate-results.json"],
                skipped=[
                    {"path": "artifacts/skipped/qa.md", "content": "skipped\n"}
                ],
            )
        ),
    )

    runner._resume_pending_transition()

    assert not nested.exists()
    assert (runner.run_dir / "artifacts" / "skipped" / "qa.md").read_text(
        encoding="utf-8"
    ) == "skipped\n"
    assert read_meta(runner.run_dir)["current_phase"] == "review"


def test_a_torn_last_journal_line_does_not_hide_the_recoverable_record(
    tmp_path: Path,
):
    """append 중에 죽으면 마지막 줄이 찢어진다. 그 한 줄로 근거를 버리면 안 된다."""
    runner, phases = _development_runner(tmp_path)
    (runner.run_dir / "review.md").write_text("stale\n", encoding="utf-8")
    _stage_journal(
        runner,
        phases,
        json.dumps(_journal_record(runner, phases, invalidated=["review.md"])),
        '{"at": "2026-01-01T00:00:0',
    )

    runner._resume_pending_transition()

    assert read_meta(runner.run_dir)["current_phase"] == "review"
    assert not (runner.run_dir / "review.md").exists()


def test_undecodable_journal_bytes_do_not_kill_the_runner_start(tmp_path: Path):
    """decode 오류는 `OSError`가 아니다. 그대로 올라가면 runner가 시작조차 못 한다."""
    runner, phases = _development_runner(tmp_path)
    _stage_journal(
        runner,
        phases,
        json.dumps(_journal_record(runner, phases)),
    )
    with (runner.run_dir / TRANSITIONS_FILE).open("ab") as handle:
        handle.write(b"\xff\xfe not utf-8\n")

    runner._resume_pending_transition()

    assert read_meta(runner.run_dir)["current_phase"] == "review"


def test_a_skip_marker_is_never_left_half_written(tmp_path: Path, monkeypatch):
    """찢어진 표식은 `exists()`로 건너뛰는 재개가 영영 고치지 않는다.

    `_has_artifact`가 그것을 그 phase의 결과로 읽으므로, 표식은 온전하거나
    아예 없어야 한다 — 즉 rename으로 나타나야 한다.
    """
    runner, phases = _development_runner(tmp_path)
    _stage_journal(
        runner,
        phases,
        json.dumps(
            _journal_record(
                runner, phases, skipped=[{"path": "qa.md", "content": "skipped\n"}]
            )
        ),
    )
    real_replace = os.replace

    def failing_replace(src, dst, **kwargs):
        if Path(dst).name == "qa.md":
            raise OSError("rename interrupted")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(
        "agent_flow.core.worktree_isolation.os.replace", failing_replace
    )

    with pytest.raises(OSError):
        runner._resume_pending_transition()

    assert not (runner.run_dir / "qa.md").exists()
    assert list(runner.run_dir.glob("*.tmp")) == []


def test_a_transition_the_workflow_cannot_place_inside_the_run_is_refused(
    tmp_path: Path,
):
    """workflow가 선언한 artifact 경로도 입력이다. run 밖이면 전이가 서야 한다."""
    runner, phases = _development_runner(tmp_path)
    ids = [phase.id for phase in phases]
    fix_index = ids.index("fix-loop")
    escaped = phases[ids.index("review")]
    phases[ids.index("review")] = Phase(
        id=escaped.id,
        description=escaped.description,
        routes=escaped.routes,
        artifact="../../../escaped.md",
    )
    for phase_id in ("review", "qa", "fix-loop"):
        (runner.run_dir / f"{phase_id}.md").write_text("done\n", encoding="utf-8")
    (tmp_path / "escaped.md").write_text("keep me\n", encoding="utf-8")
    write_meta(
        runner.run_dir, {"phase_index": fix_index, "current_phase": "fix-loop"}
    )

    transition = runner._plan_transition(fix_index, phases[fix_index])
    with pytest.raises(WorktreeIsolationError):
        runner._commit_transition(transition)

    assert (tmp_path / "escaped.md").read_text(encoding="utf-8") == "keep me\n"


def _armed_snapshot(scope: str) -> LeaderSnapshot:
    return LeaderSnapshot(
        head="0" * 40, branch="main", status="", armed=True, scope=scope
    )


def _leader_runner(tmp_path: Path) -> tuple[Runner, list[Phase], LeaderSnapshot]:
    """leader baseline 검증에 필요한 최소 runner. git은 건드리지 않는다 —
    검증 대상은 기록과 현재 phase의 대조이고, 실제 sweep은 대조를 통과한
    다음에야 돈다."""
    runner, phases = _development_runner(tmp_path)
    runner.project_root = tmp_path
    runner.accept_leader_drift = False
    runner._leader_include_ignored = True
    runner._leader_scope = leader_sweep_scope(True)
    return runner, phases, _armed_snapshot(runner._leader_scope)


def test_a_leader_baseline_survives_a_drift_re_anchor_of_the_same_phase(
    tmp_path: Path,
):
    """반증: baseline이 `phase_index`를 동일성 기준으로 들고 있으면, 승인된 drift가
    같은 이름의 phase를 다른 자리로 옮긴 순간 재개가 `WorktreeIsolationError`로
    죽는다. index는 이제 이름에서 나오는 파생값이라 phase가 그대로여도 정당하게
    움직인다. 동일성은 이름·run·leader 체크아웃이 진다."""
    runner, phases, snapshot = _leader_runner(tmp_path)
    phase = phases[2]
    meta = {
        "run_id": runner.run_dir.name,
        HOST_PHASE_LEADER_BASELINE_KEY: {
            "version": BASELINE_RECORD_VERSION,
            "run_id": runner.run_dir.name,
            "phase_id": phase.id,
            "leader_root": str(real_path(tmp_path)),
            "snapshot": leader_snapshot_payload(snapshot),
        },
    }
    compared: list[LeaderSnapshot] = []
    runner._assert_leader_unchanged = (
        lambda root, recorded, **kwargs: compared.append(recorded)
    )

    returned = runner._verify_host_phase_leader_baseline(
        meta=meta, phase=phase, leader_root=tmp_path
    )

    # 이름이 같으므로 기록은 살아 있고 leader 대조가 실제로 돈다.
    assert returned == snapshot
    assert compared == [snapshot]
    # 기록 어디에도 index가 없다. 있으면 그게 두 번째 권위가 된다.
    assert "phase_index" not in meta[HOST_PHASE_LEADER_BASELINE_KEY]


def test_a_leader_baseline_that_names_another_phase_still_stops(tmp_path: Path):
    """이름을 기준으로 옮겨도 기준 자체가 사라지면 안 된다."""
    runner, phases, snapshot = _leader_runner(tmp_path)
    meta = {
        "run_id": runner.run_dir.name,
        HOST_PHASE_LEADER_BASELINE_KEY: {
            "version": BASELINE_RECORD_VERSION,
            "run_id": runner.run_dir.name,
            "phase_id": phases[2].id,
            "leader_root": str(real_path(tmp_path)),
            "snapshot": leader_snapshot_payload(snapshot),
        },
    }

    with pytest.raises(WorktreeIsolationError):
        runner._verify_host_phase_leader_baseline(
            meta=meta, phase=phases[3], leader_root=tmp_path
        )


def test_a_leader_baseline_recorded_with_the_old_index_field_is_re_captured(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """`phase_index`를 담던 v1 레코드가 디스크에 남아 있다. 형식 차이를 오염으로
    보고하면 업그레이드를 걸친 run이 근거 없이 막히므로, 스냅샷 축과 같이 대조
    없이 다시 찍는다."""
    runner, phases, snapshot = _leader_runner(tmp_path)
    phase = phases[2]
    meta = {
        "run_id": runner.run_dir.name,
        HOST_PHASE_LEADER_BASELINE_KEY: {
            "version": 1,
            "run_id": runner.run_dir.name,
            "phase_id": phase.id,
            "phase_index": 2,
            "leader_root": str(real_path(tmp_path)),
            "snapshot": leader_snapshot_payload(snapshot),
        },
    }
    runner._assert_leader_unchanged = lambda *args, **kwargs: None

    assert (
        runner._verify_host_phase_leader_baseline(
            meta=meta, phase=phase, leader_root=tmp_path
        )
        is None
    )

    assert meta.get(HOST_PHASE_LEADER_BASELINE_KEY) is None
    out = capsys.readouterr().out
    assert "[migrate]" in out
    assert "record format v1" in out


def test_an_interrupted_transition_is_replayed_on_the_name_not_the_stale_index(
    tmp_path: Path,
):
    """반증: 재생이 `to_index`만 보면, 재배치된 정의에서 원장의 옛 index가 다른
    phase를 연다. 원장은 `to_phase`를 이미 들고 있고 멱등성 검사는 그걸 쓴다 —
    자리를 놓는 쪽도 같은 권위를 봐야 한다."""
    runner, phases = _development_runner(tmp_path)
    ids = [phase.id for phase in phases]
    review_index = ids.index("review")
    fix_index = ids.index("fix-loop")
    assert review_index != 0
    write_meta(
        runner.run_dir,
        {
            "run_id": runner.run_dir.name,
            "phase_index": fix_index,
            "current_phase": "fix-loop",
        },
    )
    # 원장은 phase가 앞으로 밀려나기 전 정의의 index를 들고 있다.
    record = _journal_record(runner, phases)
    record["to_index"] = 0
    (runner.run_dir / TRANSITIONS_FILE).write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )

    runner._resume_pending_transition()

    resumed = read_meta(runner.run_dir)
    assert resumed["current_phase"] == "review"
    assert resumed["phase_index"] == review_index


def test_a_journal_line_naming_a_phase_the_workflow_dropped_is_not_replayed(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """이름이 현재 정의에 없으면 놓을 자리가 없다. 그때 원장의 index를 믿고 놓으면
    아무 관계 없는 phase로 run을 옮긴다."""
    runner, phases = _development_runner(tmp_path)
    ids = [phase.id for phase in phases]
    fix_index = ids.index("fix-loop")
    write_meta(
        runner.run_dir,
        {
            "run_id": runner.run_dir.name,
            "phase_index": fix_index,
            "current_phase": "fix-loop",
        },
    )
    record = _journal_record(runner, phases)
    record["to_phase"] = "phase-the-workflow-dropped"
    record["to_index"] = 0
    (runner.run_dir / TRANSITIONS_FILE).write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )

    runner._resume_pending_transition()

    held = read_meta(runner.run_dir)
    assert held["current_phase"] == "fix-loop"
    assert held["phase_index"] == fix_index
    assert "[reject]" in capsys.readouterr().out


def test_a_re_anchored_cursor_says_so_instead_of_being_inferred(tmp_path: Path):
    """반증: shell이 `기록된 index != 커서 index`로 재배치를 추론했다. 그 비교는
    digest 불일치 밖에서는 언제나 거짓이라 분기의 근거가 될 수 없다."""
    runner, phases = _development_runner(tmp_path)
    ids = [phase.id for phase in phases]
    scope = CursorScope.of(runner.workflow, ids)
    recorded = {
        "phase_index": 0,
        "current_phase": ids[2],
        "workflow_digest": "stale-digest",
    }

    moved = RunCursor.from_meta(recorded, scope, accept_workflow_drift=True)
    assert moved.reanchored_from == 0
    assert moved.phase_index == 2

    stayed = RunCursor.from_meta(
        {"phase_index": 2, "current_phase": ids[2], "workflow_digest": scope.digest},
        scope,
    )
    assert stayed.reanchored_from is None


def test_a_re_anchor_prints_the_move_it_made(tmp_path: Path, capsys):
    """승인된 drift가 run을 몇 phase 앞뒤로 옮기는데 화면에 아무 줄도 없으면,
    사용자는 재개가 어디서 다시 시작했는지 알 수 없다."""
    runner, phases = _development_runner(tmp_path)
    runner.accept_workflow_drift = True
    ids = [phase.id for phase in phases]
    write_meta(
        runner.run_dir,
        {
            "run_id": runner.run_dir.name,
            "phase_index": 0,
            "current_phase": ids[2],
            "workflow_digest": "stale-digest",
        },
    )

    cursor = runner._run_cursor(read_meta(runner.run_dir))

    assert cursor.phase_index == 2
    out = capsys.readouterr().out
    assert "[re-anchor]" in out
    assert ids[2] in out
    assert "0 -> 2" in out


def test_an_empty_current_phase_is_corruption_not_an_absent_name():
    """`raw_phase or ""`는 "이름이 없다"와 "이름이 빈 문자열이다"를 한 값으로
    접었다. 이름 없는 phase를 정의할 수 있는 workflow는 없으므로 후자는 손상이다."""
    workflow = _development()
    scope = _scope(workflow)

    with pytest.raises(CorruptRunCursorError):
        RunCursor.from_meta({"phase_index": 0, "current_phase": ""}, scope)

    # 이름이 없는 두 정당한 자리는 그대로 통과하고, 값으로도 구분된다.
    assert RunCursor.from_meta({"current_phase": None}, scope).phase_id is None
    assert (
        RunCursor.from_meta({"phase_index": len(scope.phase_ids)}, scope).phase_id
        is None
    )
