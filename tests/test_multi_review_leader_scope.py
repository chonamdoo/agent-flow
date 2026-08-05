"""multi-review phase의 leader tripwire sweep 범위가 profile 선언에 도달하는가.

이 파일이 있는 이유는 실측된 배선 누락이다. sweep 범위 해석이 `runner`의 private
함수로 갇혀 있던 동안 `run_distribution`은 그 함수를 부를 수 없어 기본값 전수
sweep으로 돌았다. `tracked-only`를 선언한 프로젝트도 이 경로에서만 leader의
gitignored 산출물에 걸려 막혔고, 어떤 테스트도 그것을 반증하지 못했다.

reviewer subprocess를 실제로 띄우지는 않는다. 확인하려는 것은 **범위가 넘어가는가**
하나뿐이고, 그것은 snapshot/assert에 도달한 `include_ignored`로 관측된다.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_runner_smoke import _leader_tripwire_project


@pytest.mark.parametrize(
    "declared, include_ignored",
    [(None, True), ("all", True), ("tracked-only", False)],
)
def test_multi_review_sweeps_the_scope_the_profile_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declared, include_ignored
):
    """반증 짝이 파라미터에 있다. `tracked-only`에서 전수 sweep이 나가면 배선이
    빠진 것이고, 미선언·`all`에서 좁은 sweep이 나가면 탐지가 통째로 사라진 것이다.
    """
    from agent_flow import multi_review
    from agent_flow.cli_detect import CliInfo
    from agent_flow.core.worktree_isolation import LeaderSnapshot, leader_sweep_scope
    from agent_flow.subprocess_pool import SubprocessResult

    project, checkout, _state_root, _artifact = _leader_tripwire_project(
        tmp_path,
        monkeypatch,
        f"multi-review-{declared or 'default'}",
        declared=declared,
    )
    project_root = checkout.path.resolve()

    captured: list[bool] = []
    asserted: list[bool] = []

    def record_capture(leader_root, *, include_ignored=True):
        assert leader_root == project
        captured.append(include_ignored)
        return LeaderSnapshot(
            head="h",
            branch="main",
            status="",
            scope=leader_sweep_scope(include_ignored),
        )

    def record_assert(leader_root, before, *, include_ignored=True, **_kwargs):
        asserted.append(include_ignored)

    monkeypatch.setattr(multi_review, "capture_leader_snapshot", record_capture)
    monkeypatch.setattr(multi_review, "assert_leader_unchanged", record_assert)
    monkeypatch.setattr(
        multi_review, "assert_managed_hooks_registered", lambda *a, **k: None
    )
    monkeypatch.setattr(
        multi_review,
        "cli_by_name",
        lambda name: CliInfo(name, (name,), ("exec",)),
    )
    monkeypatch.setattr(
        multi_review,
        "run_parallel",
        lambda jobs: [
            SubprocessResult(
                job_id=job.job_id,
                returncode=0,
                stdout="reviewer-source: sub-agent\nclean",
            )
            for job in jobs
        ],
    )

    distribution = multi_review.Distribution(
        by_cli={
            "codex": [
                multi_review.ReviewerJob(
                    "generalist",
                    "prompt",
                    project_root / "codex-generalist.md",
                    project_root,
                )
            ]
        }
    )
    multi_review.run_distribution(distribution, project_root)

    assert captured == [include_ignored]
    # 기준선과 대조가 같은 범위여야 한다. 다르면 leader를 아무도 건드리지 않아도
    # 항상 diff가 나온다.
    assert asserted == [include_ignored]


def test_multi_review_ignores_a_narrowing_the_narrowed_sweep_cannot_see(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """불변: 추적되지 않은 `tracked-only` 선언은 이 경로에서도 효력이 없다.

    runner만 그 규칙을 지키고 multi-review가 무르면, 워커는 phase 경계를 피해
    리뷰 창에서만 감시를 끌 수 있다.
    """
    from agent_flow import multi_review
    from agent_flow.core.worktree_isolation import WorktreeIsolationError

    project, checkout, _state_root, _artifact = _leader_tripwire_project(
        tmp_path,
        monkeypatch,
        "multi-review-untracked",
        declared="tracked-only",
        track_declaration=False,
    )
    with pytest.raises(WorktreeIsolationError, match="could not see"):
        multi_review.leader_sweep_include_ignored_for(project)

    # 좁힘을 읽는 자리는 leader다. 워커가 자기 체크아웃에 흘린 선언은 실효 값이
    # 되어선 안 된다 — 그 자리라면 leader의 추적 검사가 통과하고도 감시가 꺼진다.
    worktree_declaration = checkout.path / ".agent-flow" / "profiles"
    worktree_declaration.mkdir(parents=True, exist_ok=True)
    (worktree_declaration / "generic.local.yaml").write_text(
        "branching:\n  leader_tripwire: tracked-only\n", encoding="utf-8"
    )
    (project / ".agent-flow" / "profiles" / "generic.local.yaml").write_text(
        "branching:\n  leader_tripwire: all\n", encoding="utf-8"
    )
    assert multi_review.leader_sweep_include_ignored_for(project) is True
