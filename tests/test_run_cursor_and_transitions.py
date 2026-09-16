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
    exclusive_file_lease,
    leader_snapshot_payload,
    leader_sweep_scope,
    real_path,
)
from agent_flow.runner import (
    FIX_LOOP_MAX_ROUNDS,
    TRANSITIONS_FILE,
    Phase,
    Runner,
    _FIX_COLLECTOR_ROUTE_KEYS,
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

    with pytest.raises(WorkflowDriftError):
        RunCursor.from_meta(meta, _scope(workflow))


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


def test_create_run_records_the_workflow_digest(tmp_path: Path):
    from agent_flow.artifact import create_run

    run_path = create_run(tmp_path, "development", "task")

    assert read_meta(run_path)["workflow_digest"] == _development().digest


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


def test_concurrent_resume_cannot_commit_stale_transition(tmp_path: Path):
    runner, phases = _development_runner(tmp_path)
    ids = [phase.id for phase in phases]
    fix_index = ids.index("fix-loop")
    review_index = ids.index("review")
    write_meta(
        runner.run_dir,
        {"phase_index": fix_index, "current_phase": "fix-loop"},
    )
    stale = runner._plan_transition(fix_index, phases[fix_index])
    write_meta(
        runner.run_dir,
        {"phase_index": review_index, "current_phase": "review"},
    )

    assert hasattr(runner, "_lifecycle_lock_path")
    assert runner._lifecycle_lock_path().name == "lifecycle.lock"
    with pytest.raises(WorktreeIsolationError, match="stale transition"):
        runner._commit_transition(stale)

    current = read_meta(runner.run_dir)
    assert current["phase_index"] == review_index
    assert current["current_phase"] == "review"

def test_independent_runs_have_independent_lifecycle_leases(tmp_path: Path):
    definition = _development()
    phases = _phases_from_definition(definition)
    first_dir = tmp_path / ".agent-flow" / "runs" / "r1"
    second_dir = tmp_path / ".agent-flow" / "runs" / "r2"
    first_dir.mkdir(parents=True)
    second_dir.mkdir(parents=True)
    first = _runner(first_dir, phases)
    second = _runner(second_dir, phases)

    assert first._lifecycle_lock_path() == first_dir / "lifecycle.lock"
    assert second._lifecycle_lock_path() == second_dir / "lifecycle.lock"
    with exclusive_file_lease(first._lifecycle_lock_path()):
        with exclusive_file_lease(second._lifecycle_lock_path()):
            assert first._lifecycle_lock_path() != second._lifecycle_lock_path()


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


def test_the_fix_collector_targets_come_from_the_declared_route_keys(tmp_path: Path):
    """반증: 이 집합이 비면 fix-loop 상한이 **전부** 조용히 꺼진다.

    실측: 비었을 때 `agent-flow start development`는 review→fix-loop를 돌며 90초
    뒤에도 끝나지 않았고, 고친 뒤 2초 안에 `reason: route_blocked`로 멈췄다.
    상한은 전이를 커밋할 때 소비한다. 여기서는 상한이 볼 대상을 고정한다.
    """
    runner, phases = _development_runner(tmp_path)
    declared = {
        target
        for phase in phases
        for key, target in (phase.routes or {}).items()
        if key in _FIX_COLLECTOR_ROUTE_KEYS and isinstance(target, str)
    }

    collectors = runner._fix_collector_targets()

    assert collectors == declared
    # workflow가 rejection route를 선언하는 한 이 집합은 비지 않는다. 비교식이
    # 어긋나 항상 False가 되면 여기서 걸린다.
    assert "fix-loop" in collectors
    assert FIX_LOOP_MAX_ROUNDS > 0


def test_fix_round_is_not_consumed_before_journal_and_replays_once(tmp_path):
    phases = [
        Phase(id="fix", description="", routes={"default": "review"}),
        Phase(id="review", description="", routes={"request-changes": "fix"}),
    ]
    runner = _runner(tmp_path, phases)
    write_meta(tmp_path, {"phase_index": 1, "current_phase": "review", "phase_entered_at": "attempt-1"})
    (tmp_path / "review.md").write_text("verdict: request-changes\n", encoding="utf-8")
    first = runner._plan_transition(1, phases[1])
    assert runner._plan_transition(1, phases[1]) == first
    assert "fix_loop_rounds" not in read_meta(tmp_path)
    runner._append_transition_journal(first)
    runner._resume_pending_transition()
    runner._resume_pending_transition()
    meta = read_meta(tmp_path)
    assert meta["current_phase"] == "fix"
    assert meta["fix_loop_rounds"] == {"fix": 1}


def test_same_phase_new_attempt_rejects_stale_transition(tmp_path):
    phases = [Phase(id="review", description="", routes={"request-changes": "review"})]
    runner = _runner(tmp_path, phases)
    write_meta(tmp_path, {"phase_index": 0, "current_phase": "review", "phase_entered_at": "first"})
    (tmp_path / "review.md").write_text("verdict: request-changes\n", encoding="utf-8")
    transition = runner._plan_transition(0, phases[0])
    runner._append_transition_journal(transition)
    runner._resume_pending_transition()
    assert read_meta(tmp_path)["fix_loop_rounds"] == {"review": 1}
    with pytest.raises(WorktreeIsolationError, match="stale transition"):
        runner._commit_transition(transition)


def _ci_repair_runner(tmp_path, monkeypatch):
    from types import SimpleNamespace

    phases = [
        Phase(id="review", description="", routes={"default": "push-pr"}),
        Phase(id="push-pr", description=""),
        Phase(id="pr-watch", description="", routes={
            "ci_failed": "pr-ci-fix", "green": "merge",
            "has_comments": "pr-comment-fix", "pending": "block", "error": "block",
        }),
        Phase(id="pr-ci-fix", description="", routes={"default": "pr-watch", "blocked": "block"}),
        Phase(id="pr-comment-fix", description="", routes={"default": "pr-watch"}),
        Phase(id="merge", description=""),
    ]
    runner = _runner(tmp_path, phases)
    runner.project_root = tmp_path
    current = {"head": "head-0", "code": "code-0"}
    monkeypatch.setattr("agent_flow.runner.test_code_baseline", lambda root: current["code"])
    monkeypatch.setattr(
        "agent_flow.runner.git_safe",
        lambda *args, **kwargs: SimpleNamespace(ok=True, stdout=current["head"]),
    )
    write_meta(tmp_path, {
        "phase_index": 2, "current_phase": "pr-watch", "phase_entered_at": "watch-0",
        "fix_loop_rounds": {"fix-loop": 3},
    })
    return runner, current


def _ci_pr_data(current, outcomes, *, proof=True):
    return {
        "state": "OPEN", "headRefOid": current["head"],
        "url": "https://github.com/owner/repo/pull/7",
        "statusCheckRollup": [
            {
                "name": name, "workflowName": "CI", "conclusion": outcome,
                "status": "IN_PROGRESS" if outcome is None else "COMPLETED",
                **({
                    "detailsUrl": f"https://github.com/owner/repo/actions/runs/{current.get('execution', 0)}",
                    "completedAt": current.get("completed_at", "2026-09-11T01:00:00Z") if outcome else None,
                } if proof else {}),
            }
            for name, outcome in outcomes.items()
        ],
    }


def _observe_ci(runner, current, outcomes, *, comments=False, proof=True, required_checks=()):
    from agent_flow.pr_watch import _classify, _record_feedback_observation

    data = _ci_pr_data(current, outcomes, proof=proof)
    data["comments"] = [{"id": "question", "body": "please explain"}] if comments else []
    snapshot = _classify(7, data, repo="github.com/owner/repo", required_checks=required_checks)
    _record_feedback_observation(runner.run_dir, snapshot)
    (runner.run_dir / "pr-watch.md").write_text(f"status: {snapshot.status}\n", encoding="utf-8")
    return snapshot


def _ci_step(runner, text="finished\n", *, replay=False):
    meta = read_meta(runner.run_dir)
    index = meta["phase_index"]
    phase = runner.phases[index]
    if phase.id != "pr-watch":
        (runner.run_dir / f"{phase.id}.md").write_text(text, encoding="utf-8")
    transition = runner._plan_transition(index, phase)
    if replay:
        runner._append_transition_journal(transition)
        runner._resume_pending_transition()
        runner._resume_pending_transition()
    else:
        runner._commit_transition(transition)
    return transition


def _publish_ci_repair(runner, current, number):
    current.update(code=f"code-{number}", head=f"head-{number}")
    assert _ci_step(runner).to_phase == "review"
    assert _ci_step(runner).to_phase == "push-pr"
    assert _ci_step(runner).to_phase == "pr-watch"


@pytest.mark.parametrize("conclusion", ["NEUTRAL", "SKIPPED", "STALE"])
@pytest.mark.parametrize("required", [False, True])
def test_ci_repair_new_accepted_terminal_result_settles_only_optional_checks(
    tmp_path, monkeypatch, conclusion, required,
):
    runner, current = _ci_repair_runner(tmp_path, monkeypatch)
    required_checks = ("unit",) if required else ()
    _observe_ci(runner, current, {"unit": "FAILURE"}, required_checks=required_checks)
    assert _ci_step(runner).to_phase == "pr-ci-fix"
    assert _ci_step(runner).to_phase == "pr-watch"

    _observe_ci(runner, current, {"unit": conclusion}, required_checks=required_checks)
    assert _ci_step(runner, replay=True).route_key == "ci-repair-evidence"
    assert list(read_meta(tmp_path)["ci_repair_state"]["counts"].values()) == [0]

    current["execution"] = 1
    _observe_ci(runner, current, {"unit": None}, required_checks=required_checks)
    assert _ci_step(runner, replay=True).route_key == "ci-repair-pending"
    snapshot = _observe_ci(
        runner, current, {"unit": conclusion}, required_checks=required_checks,
    )
    assert snapshot.status == ("ci_failed" if required else "green")
    transition = _ci_step(runner, replay=True)
    assert transition.to_phase == ("pr-ci-fix" if required else "merge")
    state = read_meta(tmp_path)["ci_repair_state"]
    if required:
        assert list(state["counts"].values()) == [1]
    else:
        assert state["active"] is None
        assert state["counts"] == {}


def test_ci_repair_blocks_after_third_completed_repair_and_replays_once(tmp_path, monkeypatch):
    runner, current = _ci_repair_runner(tmp_path, monkeypatch)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    first = runner._plan_transition(2, runner.phases[2])
    assert runner._plan_transition(2, runner.phases[2]) == first
    assert "ci_repair_state" not in read_meta(tmp_path)
    assert _ci_step(runner, replay=True).to_phase == "pr-ci-fix"
    for number in range(1, 4):
        _publish_ci_repair(runner, current, number)
        _observe_ci(runner, current, {"unit": "FAILURE"})
        before = read_meta(tmp_path)
        planned = runner._plan_transition(2, runner.phases[2])
        assert runner._next_index(2, runner.phases[2]).blocked == (number == 3)
        assert runner._plan_transition(2, runner.phases[2]) == planned
        assert read_meta(tmp_path) == before
        transition = _ci_step(runner, replay=True)
        assert transition.blocked == (number == 3)
    assert transition.route_key == "ci-repair-limit"
    assert read_meta(tmp_path)["current_phase"] == "pr-watch"
    assert read_meta(tmp_path)["fix_loop_rounds"] == {"fix-loop": 3}
    for _ in range(3):
        _observe_ci(runner, current, {"unit": "FAILURE"})
        assert _ci_step(runner, replay=True).route_key == "ci-repair-limit"


def test_ci_repair_ignores_unrelated_heads_and_requires_publication(tmp_path, monkeypatch):
    runner, current = _ci_repair_runner(tmp_path, monkeypatch)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    _ci_step(runner)
    current.update(code="repair", head="head-repair")
    assert _ci_step(runner).to_phase == "review"
    assert _ci_step(runner).to_phase == "push-pr"
    assert all(count == 0 for count in read_meta(tmp_path)["ci_repair_state"]["counts"].values())
    assert _ci_step(runner).to_phase == "pr-watch"
    for number in range(4):
        current["head"] = f"unrelated-{number}"
        _observe_ci(runner, current, {"unit": "FAILURE"})
        assert _ci_step(runner, replay=True).route_key == "ci-repair-evidence"
    current["head"] = "head-repair"
    _observe_ci(runner, current, {"unit": "FAILURE"})
    assert _ci_step(runner).to_phase == "pr-ci-fix"
    _publish_ci_repair(runner, current, 2)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    assert _ci_step(runner).to_phase == "pr-ci-fix"
    _publish_ci_repair(runner, current, 3)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    assert _ci_step(runner).route_key == "ci-repair-limit"


def test_ci_repair_separates_checks_and_resets_only_success(tmp_path, monkeypatch):
    runner, current = _ci_repair_runner(tmp_path, monkeypatch)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    _ci_step(runner)
    _publish_ci_repair(runner, current, 1)
    _observe_ci(runner, current, {"unit": "FAILURE", "lint": "FAILURE"})
    assert _ci_step(runner).to_phase == "pr-ci-fix"
    _publish_ci_repair(runner, current, 2)
    _observe_ci(runner, current, {"unit": "SUCCESS", "lint": "FAILURE"})
    assert _ci_step(runner).to_phase == "pr-ci-fix"
    _publish_ci_repair(runner, current, 3)
    _observe_ci(runner, current, {"unit": "FAILURE", "lint": "FAILURE"})
    assert _ci_step(runner).to_phase == "pr-ci-fix"
    _publish_ci_repair(runner, current, 4)
    _observe_ci(runner, current, {"unit": "FAILURE", "lint": "SUCCESS"})
    assert _ci_step(runner).to_phase == "pr-ci-fix"
    _publish_ci_repair(runner, current, 5)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    assert _ci_step(runner).to_phase == "pr-ci-fix"
    _publish_ci_repair(runner, current, 6)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    assert _ci_step(runner).route_key == "ci-repair-limit"


def test_ci_repair_preserves_pending_results_and_ordinary_comment_budget(tmp_path, monkeypatch):
    runner, current = _ci_repair_runner(tmp_path, monkeypatch)
    _observe_ci(runner, current, {"unit": "FAILURE", "lint": "FAILURE"})
    _ci_step(runner)
    _publish_ci_repair(runner, current, 1)
    for _ in range(4):
        _observe_ci(runner, current, {"unit": "FAILURE", "lint": None})
        assert _ci_step(runner, replay=True).route_key == "ci-repair-pending"
    _observe_ci(runner, current, {"unit": None, "lint": "SUCCESS"})
    assert _ci_step(runner).blocked
    for _ in range(4):
        _observe_ci(runner, current, {"unit": None}, comments=True)
        assert _ci_step(runner).to_phase == "pr-comment-fix"
        assert _ci_step(runner).to_phase == "pr-watch"
    _observe_ci(runner, current, {"unit": "FAILURE"})
    assert _ci_step(runner).to_phase == "pr-ci-fix"
    _publish_ci_repair(runner, current, 2)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    assert _ci_step(runner).to_phase == "pr-ci-fix"
    _publish_ci_repair(runner, current, 3)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    assert _ci_step(runner).route_key == "ci-repair-limit"


@pytest.mark.parametrize("evidence", ["missing", "malformed", "empty", "unknown"])
def test_ci_repair_never_clears_streak_on_missing_evidence(tmp_path, monkeypatch, evidence):
    runner, current = _ci_repair_runner(tmp_path, monkeypatch)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    _ci_step(runner)
    _publish_ci_repair(runner, current, 1)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    _ci_step(runner)
    _publish_ci_repair(runner, current, 2)
    path = tmp_path / "pr-feedback.json"
    if evidence == "missing":
        path.unlink()
    elif evidence == "malformed":
        path.write_text('{"ci_checks": []}', encoding="utf-8")
    elif evidence == "empty":
        _observe_ci(runner, current, {})
    else:
        _observe_ci(runner, current, {"unit": "UNRECOGNIZED"})
    (tmp_path / "pr-watch.md").write_text("status: green\n", encoding="utf-8")
    assert _ci_step(runner, replay=True).blocked
    assert list(read_meta(tmp_path)["ci_repair_state"]["counts"].values()) == [1]


def test_ci_repair_rejects_observation_changed_between_plan_and_commit(tmp_path, monkeypatch):
    runner, current = _ci_repair_runner(tmp_path, monkeypatch)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    transition = runner._plan_transition(2, runner.phases[2])
    _observe_ci(runner, current, {"unit": "SUCCESS"})
    with pytest.raises(WorktreeIsolationError, match="CI observation changed"):
        runner._commit_transition(transition)
    assert read_meta(tmp_path)["current_phase"] == "pr-watch"


@pytest.mark.parametrize("revision_field", ["execution", "completed_at"])
def test_ci_repair_direct_retry_requires_distinct_completed_execution(tmp_path, monkeypatch, revision_field):
    runner, current = _ci_repair_runner(tmp_path, monkeypatch)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    _ci_step(runner)
    assert _ci_step(runner).to_phase == "pr-watch"
    for number in range(1, 4):
        for _ in range(4):
            _observe_ci(runner, current, {"unit": "FAILURE"})
            assert _ci_step(runner, replay=True).route_key == "ci-repair-evidence"
            assert list(read_meta(tmp_path)["ci_repair_state"]["counts"].values()) == [number - 1]
        current[revision_field] = number if revision_field == "execution" else f"2026-09-11T0{number + 1}:00:00Z"
        _observe_ci(runner, current, {"unit": None})
        assert _ci_step(runner, replay=True).blocked
        assert list(read_meta(tmp_path)["ci_repair_state"]["counts"].values()) == [number - 1]
        _observe_ci(runner, current, {"unit": "FAILURE"})
        result = _ci_step(runner, replay=True)
        assert list(read_meta(tmp_path)["ci_repair_state"]["counts"].values()) == [number]
        if number < 3:
            assert result.to_phase == "pr-ci-fix"
            assert _ci_step(runner).to_phase == "pr-watch"
        else:
            assert result.route_key == "ci-repair-limit"


def test_ci_repair_does_not_recount_delayed_initial_or_consumed_executions(tmp_path, monkeypatch):
    runner, current = _ci_repair_runner(tmp_path, monkeypatch)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    _ci_step(runner)
    assert _ci_step(runner).to_phase == "pr-watch"
    current["execution"] = 1
    _observe_ci(runner, current, {"unit": "FAILURE"})
    assert _ci_step(runner, replay=True).to_phase == "pr-ci-fix"
    assert _ci_step(runner).to_phase == "pr-watch"
    for execution in (0, 1, 0, 1):
        current["execution"] = execution
        _observe_ci(runner, current, {"unit": "FAILURE"})
        assert _ci_step(runner, replay=True).route_key == "ci-repair-evidence"
    for execution in (2, 3):
        current["execution"] = execution
        _observe_ci(runner, current, {"unit": "FAILURE"})
        result = _ci_step(runner, replay=True)
        if execution == 2:
            assert result.to_phase == "pr-ci-fix"
            assert _ci_step(runner).to_phase == "pr-watch"
        else:
            assert result.route_key == "ci-repair-limit"


@pytest.mark.parametrize("switch_pr", [False, True])
@pytest.mark.parametrize("conclusion", ["SUCCESS", "NEUTRAL", "SKIPPED", "STALE"])
def test_ci_repair_replayed_success_cannot_clear_a_newer_failure_streak(
    tmp_path, monkeypatch, switch_pr, conclusion,
):
    runner, current = _ci_repair_runner(tmp_path, monkeypatch)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    _ci_step(runner)
    assert _ci_step(runner).to_phase == "pr-watch"
    current["execution"] = 1
    _observe_ci(runner, current, {"unit": conclusion}, comments=True)
    assert _ci_step(runner).to_phase == "pr-comment-fix"
    assert _ci_step(runner).to_phase == "pr-watch"
    for execution in (2, 3):
        current["execution"] = execution
        _observe_ci(runner, current, {"unit": "FAILURE"})
        assert _ci_step(runner).to_phase == "pr-ci-fix"
        assert _ci_step(runner).to_phase == "pr-watch"
    current["execution"] = 1
    _observe_ci(runner, current, {"unit": conclusion})
    assert _ci_step(runner, replay=True).route_key == "ci-repair-evidence"
    for execution in (4, 5):
        current["execution"] = execution
        _observe_ci(runner, current, {"unit": "FAILURE"})
        result = _ci_step(runner, replay=True)
        if execution == 4:
            assert result.to_phase == "pr-ci-fix"
            assert _ci_step(runner).to_phase == "pr-watch"
        else:
            assert result.route_key == "ci-repair-limit"
    if switch_pr:
        from agent_flow.pr_watch import fetch_pr

        other = _ci_pr_data(current, {"unit": conclusion})
        other["url"] = "https://github.com/owner/repo/pull/8"
        monkeypatch.setattr("agent_flow.pr_watch._fetch_pr_data", lambda number, repo: other)
        monkeypatch.setattr("agent_flow.pr_watch._fetch_review_threads", lambda number, repo: [])
        assert fetch_pr(8, repo="owner/repo", run_dir=tmp_path).status == "error"
    current["execution"] = 1
    _observe_ci(runner, current, {"unit": conclusion})
    assert _ci_step(runner, replay=True).blocked
    current["execution"] = 6
    _observe_ci(runner, current, {"unit": conclusion})
    assert _ci_step(runner).to_phase == "merge"


def test_ci_repair_delayed_success_cannot_overwrite_unconsumed_success(tmp_path, monkeypatch):
    runner, current = _ci_repair_runner(tmp_path, monkeypatch)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    _ci_step(runner)
    assert _ci_step(runner).to_phase == "pr-watch"
    current["execution"] = 1
    _observe_ci(runner, current, {"unit": "SUCCESS"}, comments=True)
    assert _ci_step(runner).to_phase == "pr-comment-fix"
    assert _ci_step(runner).to_phase == "pr-watch"
    for execution in (2, 3, 4):
        current["execution"] = execution
        _observe_ci(runner, current, {"unit": "FAILURE"})
        assert _ci_step(runner).to_phase == "pr-ci-fix"
        assert _ci_step(runner).to_phase == "pr-watch"
    for execution, outcome in ((5, "SUCCESS"), (1, "SUCCESS"), (6, "FAILURE")):
        current["execution"] = execution
        _observe_ci(runner, current, {"unit": outcome})
    assert _ci_step(runner, replay=True).to_phase == "pr-ci-fix"
    assert _ci_step(runner).to_phase == "pr-watch"
    for execution in (7, 8, 9):
        current["execution"] = execution
        _observe_ci(runner, current, {"unit": "FAILURE"})
        result = _ci_step(runner, replay=True)
        if execution < 9:
            assert result.to_phase == "pr-ci-fix"
            assert _ci_step(runner).to_phase == "pr-watch"
        else:
            assert result.route_key == "ci-repair-limit"


@pytest.mark.parametrize("outcome", ["SUCCESS", "FAILURE"])
def test_ci_repair_superseded_observation_cannot_settle_later_repair(tmp_path, monkeypatch, outcome):
    runner, current = _ci_repair_runner(tmp_path, monkeypatch)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    _ci_step(runner)
    assert _ci_step(runner).to_phase == "pr-watch"
    for execution, result in ((1, outcome), (2, "SUCCESS"), (3, "FAILURE")):
        current["execution"] = execution
        _observe_ci(runner, current, {"unit": result})
    assert _ci_step(runner).to_phase == "pr-ci-fix"
    assert _ci_step(runner).to_phase == "pr-watch"
    for execution in (4, 5):
        current["execution"] = execution
        _observe_ci(runner, current, {"unit": "FAILURE"})
        assert _ci_step(runner).to_phase == "pr-ci-fix"
        assert _ci_step(runner).to_phase == "pr-watch"
    current["execution"] = 1
    _observe_ci(runner, current, {"unit": outcome})
    assert _ci_step(runner, replay=True).route_key == "ci-repair-evidence"
    current["execution"] = 6
    _observe_ci(runner, current, {"unit": "FAILURE"})
    assert _ci_step(runner, replay=True).route_key == "ci-repair-limit"


def test_ci_repair_keeps_success_observed_before_pending_transition_recovery(tmp_path, monkeypatch):
    runner, current = _ci_repair_runner(tmp_path, monkeypatch)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    transition = runner._plan_transition(2, runner.phases[2])
    runner._append_transition_journal(transition)
    current["execution"] = 1
    _observe_ci(runner, current, {"unit": "SUCCESS"})
    current.update(head="unrelated-head", execution=2)
    _observe_ci(runner, current, {"unit": "SUCCESS"})
    current.update(head="head-0", execution=3)
    runner._resume_pending_transition()
    assert _ci_step(runner).to_phase == "pr-watch"
    _observe_ci(runner, current, {"unit": "FAILURE"})
    assert _ci_step(runner).to_phase == "pr-ci-fix"
    assert _ci_step(runner).to_phase == "pr-watch"
    for execution in (4, 5, 6):
        current["execution"] = execution
        _observe_ci(runner, current, {"unit": "FAILURE"})
        result = _ci_step(runner, replay=True)
        if execution < 6:
            assert result.to_phase == "pr-ci-fix"
            assert _ci_step(runner).to_phase == "pr-watch"
        else:
            assert result.route_key == "ci-repair-limit"


@pytest.mark.parametrize("proof", [False, True])
def test_ci_repair_same_head_missing_revision_or_status_changes_cannot_settle(tmp_path, monkeypatch, proof):
    runner, current = _ci_repair_runner(tmp_path, monkeypatch)
    _observe_ci(runner, current, {"unit": "FAILURE"}, proof=proof)
    _ci_step(runner)
    assert _ci_step(runner).to_phase == "pr-watch"
    for outcome in (None, "FAILURE", "SUCCESS", "FAILURE"):
        _observe_ci(runner, current, {"unit": outcome}, proof=proof)
        assert _ci_step(runner, replay=True).blocked
        assert list(read_meta(tmp_path)["ci_repair_state"]["counts"].values()) == [0]
    _observe_ci(runner, current, {"unit": "FAILURE"})
    assert _ci_step(runner).route_key == "ci-repair-evidence"


@pytest.mark.parametrize("same_head", [False, True])
def test_ci_repair_consumes_intermediate_watcher_success_once(tmp_path, monkeypatch, same_head):
    from agent_flow.pr_watch import watch_pr

    runner, current = _ci_repair_runner(tmp_path, monkeypatch)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    _ci_step(runner)
    for number in (1, 2):
        _publish_ci_repair(runner, current, number)
        _observe_ci(runner, current, {"unit": "FAILURE"})
        assert _ci_step(runner).to_phase == "pr-ci-fix"
    if same_head:
        assert _ci_step(runner).to_phase == "pr-watch"
        current["execution"] = 1
    else:
        _publish_ci_repair(runner, current, 3)
    polls = iter([
        _ci_pr_data(current, {"unit": "SUCCESS", "lint": None}),
        _ci_pr_data(current, {"unit": None, "lint": None}),
        _ci_pr_data({**current, "execution": 2}, {"unit": "FAILURE", "lint": "SUCCESS"}),
    ])
    monkeypatch.setattr("agent_flow.pr_watch._fetch_pr_data", lambda *args: next(polls))
    monkeypatch.setattr("agent_flow.pr_watch._fetch_review_threads", lambda *args: [])
    monkeypatch.setattr("agent_flow.pr_watch.time.sleep", lambda _: None)
    result = watch_pr(7, repo="owner/repo", run_dir=tmp_path, max_poll_count=3)
    assert result.status == "ci_failed"
    (tmp_path / "pr-watch.md").write_text(f"status: {result.status}\n", encoding="utf-8")
    assert _ci_step(runner, replay=True).to_phase == "pr-ci-fix"
    assert list(read_meta(tmp_path)["ci_repair_state"]["counts"].values()) == [0]
    assert _ci_step(runner).to_phase == "pr-watch"
    current["execution"] = 2
    _observe_ci(runner, current, {"unit": "FAILURE"})
    assert _ci_step(runner, replay=True).route_key == "ci-repair-evidence"
    assert list(read_meta(tmp_path)["ci_repair_state"]["counts"].values()) == [0]


@pytest.mark.parametrize("active", [False, True])
def test_ci_repair_consumes_success_before_unrelated_head_change(tmp_path, monkeypatch, active):
    from agent_flow.pr_watch import watch_pr

    runner, current = _ci_repair_runner(tmp_path, monkeypatch)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    _ci_step(runner)
    for number in (1, 2, 3):
        _publish_ci_repair(runner, current, number)
        if active and number == 3:
            break
        _observe_ci(runner, current, {"unit": "FAILURE"})
        assert _ci_step(runner).blocked == (number == 3)
    current["execution"] = 1
    succeeded = _ci_pr_data(current, {"unit": "SUCCESS", "lint": None})
    current.update(head="manual-head", code="manual-code")
    current_result = _ci_pr_data(current, {"unit": "SUCCESS" if active else "FAILURE", "lint": "SUCCESS"})
    polls = iter([succeeded, current_result])
    monkeypatch.setattr("agent_flow.pr_watch._fetch_pr_data", lambda *args: next(polls))
    monkeypatch.setattr("agent_flow.pr_watch._fetch_review_threads", lambda *args: [])
    monkeypatch.setattr("agent_flow.pr_watch.time.sleep", lambda _: None)
    result = watch_pr(7, repo="owner/repo", run_dir=tmp_path, max_poll_count=2)
    assert result.status == ("green" if active else "ci_failed")
    (tmp_path / "pr-watch.md").write_text(f"status: {result.status}\n", encoding="utf-8")
    if active:
        assert _ci_step(runner, replay=True).to_phase == "merge"
        return
    assert _ci_step(runner, replay=True).to_phase == "pr-ci-fix"
    for number in (4, 5, 6):
        _publish_ci_repair(runner, current, number)
        _observe_ci(runner, current, {"unit": "FAILURE"})
        assert _ci_step(runner).blocked == (number == 6)


@pytest.mark.parametrize("body", ["[]", "null", '"not an observation"'])
def test_ci_repair_commit_rejects_non_object_observation(tmp_path, monkeypatch, body):
    runner, current = _ci_repair_runner(tmp_path, monkeypatch)
    _observe_ci(runner, current, {"unit": "FAILURE"})
    transition = runner._plan_transition(2, runner.phases[2])
    (tmp_path / "pr-feedback.json").write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match="recorded CI observation"):
        runner._commit_transition(transition)
    assert "ci_repair_state" not in read_meta(tmp_path)


@pytest.mark.parametrize("corrupt", [None, [], {"counts": "three"}])
def test_ci_repair_malformed_accounting_blocks_without_reset(tmp_path, monkeypatch, corrupt):
    runner, current = _ci_repair_runner(tmp_path, monkeypatch)
    _observe_ci(runner, current, {"unit": "SUCCESS"})
    meta = read_meta(tmp_path)
    meta["ci_repair_state"] = corrupt
    write_meta(tmp_path, meta)
    assert _ci_step(runner).route_key == "ci-repair-evidence"
    assert read_meta(tmp_path)["ci_repair_state"] == corrupt


@pytest.mark.parametrize("change", ["bytes", "phase_entered_at", "run_id"])
def test_phase_approval_is_bound_to_artifact_and_attempt(tmp_path, change):
    from agent_flow.artifact import approve_phase_artifact, create_run, pending_phase_approval

    tmp_path = create_run(tmp_path, "default", "Bind approval to the artifact and attempt")
    artifact = tmp_path / "design.md"
    artifact.write_text("approved scope\n", encoding="utf-8")
    meta = read_meta(tmp_path)
    meta.update({
        "current_phase": "design", "phase_index": 0, "phase_entered_at": "first",
        "phase_approval_request": {"phase_id": "design", "phase_entered_at": "first", "artifact": "design.md"},
    })
    write_meta(tmp_path, meta)
    token = pending_phase_approval(tmp_path)["token"]
    approve_phase_artifact(tmp_path, token=token)
    assert pending_phase_approval(tmp_path) is None
    if change == "bytes":
        artifact.write_text("different scope\n", encoding="utf-8")
    else:
        meta = read_meta(tmp_path)
        meta[change] = "second"
        if change == "phase_entered_at":
            meta["phase_approval_request"]["phase_entered_at"] = "second"
        write_meta(tmp_path, meta)
    with pytest.raises(ValueError, match="does not match"):
        approve_phase_artifact(tmp_path, token=token)
    assert pending_phase_approval(tmp_path)["token"] != token


def test_existing_pause_artifact_still_requires_explicit_approval(tmp_path, monkeypatch):
    from agent_flow.artifact import approve_phase_artifact, create_run, pending_phase_approval

    tmp_path = create_run(tmp_path, "default", "Bind design approval to the exact artifact")
    phase = Phase(id="design", description="", pause_after=True)
    runner = _runner(tmp_path, [phase, Phase(id="implement", description="")])
    runner.profile = {}
    runner.next_command = "agent-flow continue"
    monkeypatch.setattr(runner, "_print_structured_status", lambda **kwargs: None)
    meta = read_meta(tmp_path)
    meta.update({
        "phase_index": 0, "current_phase": "design", "phase_entered_at": "first",
        "task": "Bind design approval to the exact artifact.",
    })
    write_meta(tmp_path, meta)
    (tmp_path / "design.md").write_text("approved scope\n", encoding="utf-8")
    assert runner._pause_for_approval(phase)
    assert runner._pause_for_approval(phase)
    approve_phase_artifact(tmp_path, token=pending_phase_approval(tmp_path)["token"])
    assert not runner._pause_for_approval(phase)
    transition = runner._plan_transition(0, phase)
    (tmp_path / "design.md").write_text("unapproved scope\n", encoding="utf-8")
    with pytest.raises(WorktreeIsolationError, match="approval"):
        runner._commit_transition(transition)
