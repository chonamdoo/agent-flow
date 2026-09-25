"""중단된 리뷰 시도에서 완료된 reviewer 결과를 언제 공개하는가.

공개 조건은 "그 시도에서 띄운 reviewer가 모두 끝났다"이다. 살아 있는 reviewer가
먼저 끝난 결과를 읽으면 독립 리뷰가 아니게 된다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from agent_flow import multi_review, subprocess_pool
from agent_flow.cli_detect import CliInfo
from agent_flow.core.query import query_run
from agent_flow.core.worktree_isolation import WorktreeIsolationError
from agent_flow.subprocess_pool import SubprocessResult

_REVIEWER = """
import json, os, sys, time
from pathlib import Path
config = json.loads(sys.argv[1])
if config["role"] == "slow":
    Path(config["pid_file"]).write_text(str(os.getpid()))
    time.sleep(60)
if config["wait_for"]:
    deadline = time.monotonic() + 10
    while not Path(config["wait_for"]).exists():
        if time.monotonic() > deadline:
            raise SystemExit("slow reviewer never started")
        time.sleep(0.01)
print("reviewer-source: sub-agent")
print("FINDING-" + config["finding"])
print("verdict: approve")
"""


def _wait_for_file(path: Path, timeout_s: float = 10.0) -> str:
    deadline = time.monotonic() + timeout_s
    while not path.exists() or not path.read_text(encoding="utf-8"):
        if time.monotonic() > deadline:
            raise AssertionError(f"{path.name} was not written")
        time.sleep(0.01)
    return path.read_text(encoding="utf-8")


@contextmanager
def _unconfined_launch(*, argv, cwd, env, allow_workspace_writes):
    # 이 파일은 공개 조건만 본다. sandbox 적용은 test_isolation_fail_closed가 본다.
    with TemporaryDirectory(prefix="review-interruption-") as scratch:
        yield SimpleNamespace(
            argv=argv,
            cwd=cwd,
            env=env,
            scratch=Path(scratch),
            lease=SimpleNamespace(process_lifetime_fds=()),
        )


def test_process_ledger_requires_whole_process_group_to_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess_pool, "_PROVIDER_REAP_TIMEOUT_S", 0.3)
    ledger = subprocess_pool.ProviderProcessLedger()
    assert ledger.all_exited()

    marker = tmp_path / "grandchild.pid"
    reviewer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({str(marker)!r}, 'w').write(str(child.pid))\n",
        ],
        start_new_session=True,
    )
    reviewer.wait(timeout=10)
    grandchild = int(_wait_for_file(marker))
    ledger.launch_started()
    ledger.launch_finished(reviewer.pid)
    try:
        assert not ledger.all_exited()
    finally:
        os.kill(grandchild, signal.SIGKILL)

    monkeypatch.setattr(subprocess_pool, "_PROVIDER_REAP_TIMEOUT_S", 5.0)
    assert ledger.all_exited()


def test_process_ledger_treats_unsignalable_group_as_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess_pool, "_PROVIDER_REAP_TIMEOUT_S", 0.2)

    def not_permitted(_pgid: int, _signal: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(subprocess_pool.os, "killpg", not_permitted)
    ledger = subprocess_pool.ProviderProcessLedger()
    ledger.launch_started()
    ledger.launch_finished(424242)

    assert not ledger.all_exited()


def test_pool_records_every_launched_reviewer_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess_pool, "confined_provider_launch", _unconfined_launch)
    monkeypatch.setattr(subprocess_pool, "_PROVIDER_REAP_TIMEOUT_S", 0.3)
    started = tmp_path / "started"
    job = subprocess_pool.SubprocessJob(
        job_id="claude-generalist",
        binary=sys.executable,
        args=(
            "-c",
            f"import time; open({str(started)!r}, 'w').write('1'); time.sleep(60)",
        ),
        cwd=tmp_path,
        timeout_s=60,
    )
    ledger = subprocess_pool.ProviderProcessLedger()

    async def review_then_cancel() -> bool:
        task = asyncio.create_task(
            subprocess_pool.run_parallel_async([job], process_ledger=ledger)
        )
        while not started.exists():
            await asyncio.sleep(0.01)
        running_reviewer_seen = not ledger.all_exited()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return running_reviewer_seen

    assert asyncio.run(review_then_cancel())
    assert ledger.all_exited()


def _reviewer_job(
    tmp_path: Path, provider: str, angle: str, config: dict[str, str]
) -> multi_review.ReviewerJob:
    return multi_review.ReviewerJob(
        angle,
        json.dumps(config),
        tmp_path / f"review-{angle}-{provider}.md",
        tmp_path,
    )


def _use_synthetic_reviewers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        multi_review, "assert_managed_hooks_registered", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(multi_review, "leader_root_for", lambda _root: None)
    monkeypatch.setattr(
        multi_review, "cli_by_name", lambda name: CliInfo(name, (sys.executable,), ())
    )
    monkeypatch.setattr(multi_review, "_cli_version", lambda _binary: None)


def _cancel_after(
    monkeypatch: pytest.MonkeyPatch, job_id: str
) -> None:
    """Run the real pool, cancelling the whole review once `job_id` completes."""

    def run_parallel(jobs, *, on_result=None, **kwargs):
        async def drive():
            def observe(result: SubprocessResult) -> None:
                if on_result is not None:
                    on_result(result)
                if result.job_id == job_id:
                    review.cancel()

            review = asyncio.create_task(
                subprocess_pool.run_parallel_async(jobs, on_result=observe, **kwargs)
            )
            return await review

        return asyncio.run(drive())

    monkeypatch.setattr(multi_review, "run_parallel", run_parallel)


def _interrupted_files(tmp_path: Path) -> list[str]:
    return sorted(path.name for path in tmp_path.glob("*-interrupted.md"))


@pytest.mark.parametrize("stage", ["single-provider", "probe", "remaining"])
def test_interrupted_review_preserves_completed_results_after_reviewers_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    _use_synthetic_reviewers(monkeypatch)
    monkeypatch.setattr(
        multi_review,
        "_resolve_reviewer_launch",
        lambda *, cli, prompt, **_kwargs: multi_review.ResolvedLaunch(
            provider=cli.name,
            model="synthetic",
            effort=None,
            argv=("-u", "-c", _REVIEWER, prompt),
            cli_version="synthetic",
        ),
    )
    monkeypatch.setattr(subprocess_pool, "confined_provider_launch", _unconfined_launch)
    slow_pid = tmp_path / "slow.pid"
    quick = {"role": "quick", "wait_for": ""}
    slow = {"role": "slow", "wait_for": "", "pid_file": str(slow_pid)}
    waits = {"role": "quick", "wait_for": str(slow_pid)}
    if stage == "single-provider":
        by_cli = {
            "claude": [
                _reviewer_job(tmp_path, "claude", "generalist", {**waits, "finding": "A"}),
                _reviewer_job(tmp_path, "claude", "types", {**slow, "finding": "B"}),
            ]
        }
        cancel_at = "claude-generalist"
        completed = {"review-generalist-claude": "A"}
    elif stage == "probe":
        by_cli = {
            "claude": [
                _reviewer_job(tmp_path, "claude", "generalist", {**waits, "finding": "A"}),
                _reviewer_job(tmp_path, "claude", "types", {**quick, "finding": "C"}),
            ],
            "codex": [
                _reviewer_job(tmp_path, "codex", "generalist", {**slow, "finding": "B"}),
                _reviewer_job(tmp_path, "codex", "types", {**quick, "finding": "D"}),
            ],
        }
        cancel_at = "claude-generalist"
        completed = {"review-generalist-claude": "A"}
    else:
        by_cli = {
            "claude": [
                _reviewer_job(tmp_path, "claude", "generalist", {**quick, "finding": "A"}),
                _reviewer_job(tmp_path, "claude", "types", {**waits, "finding": "C"}),
            ],
            "codex": [
                _reviewer_job(tmp_path, "codex", "generalist", {**quick, "finding": "B"}),
                _reviewer_job(tmp_path, "codex", "types", {**slow, "finding": "D"}),
            ],
        }
        cancel_at = "claude-types"
        completed = {
            "review-generalist-claude": "A",
            "review-generalist-codex": "B",
            "review-types-claude": "C",
        }
    _cancel_after(monkeypatch, cancel_at)
    distribution = multi_review.Distribution(by_cli=by_cli, phase_id="review")

    with pytest.raises(asyncio.CancelledError):
        multi_review.run_distribution(distribution, tmp_path, timeout_s=60)

    assert _interrupted_files(tmp_path) == sorted(
        f"{stem}-interrupted.md" for stem in completed
    )
    for stem, finding in completed.items():
        hits = query_run(tmp_path, f"FINDING-{finding}")
        assert [hit.path.name for hit in hits] == [f"{stem}-interrupted.md"]
        assert not (tmp_path / f"{stem}.md").exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(slow_pid.read_text(encoding="utf-8")), 0)


def _interrupt_after_completion(
    monkeypatch: pytest.MonkeyPatch,
    completed: SubprocessResult,
    *,
    running_process_group: int | None = None,
    interruption: BaseException | None = None,
) -> None:
    def run_parallel(_jobs, *, on_result=None, process_ledger=None, **_kwargs):
        if running_process_group is not None and process_ledger is not None:
            process_ledger.launch_started()
            process_ledger.launch_finished(running_process_group)
        if on_result is not None:
            on_result(completed)
        raise interruption if interruption is not None else KeyboardInterrupt

    monkeypatch.setattr(multi_review, "run_parallel", run_parallel)


def _raised_by(action) -> BaseException | None:
    # KeyboardInterrupt를 pytest.raises 밖으로 흘리면 테스트 세션 전체가 멈춘다.
    try:
        action()
    except BaseException as exc:
        return exc
    return None


def test_cancelled_reviewer_launch_keeps_exit_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess_pool, "confined_provider_launch", _unconfined_launch)
    monkeypatch.setattr(subprocess_pool, "_PROVIDER_REAP_TIMEOUT_S", 0.2)
    spawning: list[bool] = []

    async def spawn_that_is_cancelled(*_args, **_kwargs):
        spawning.append(True)
        await asyncio.sleep(3600)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_that_is_cancelled)
    job = subprocess_pool.SubprocessJob(
        job_id="claude-generalist",
        binary=sys.executable,
        args=("-c", "pass"),
        cwd=tmp_path,
        timeout_s=60,
    )
    ledger = subprocess_pool.ProviderProcessLedger()

    async def cancel_while_spawning() -> None:
        task = asyncio.create_task(
            subprocess_pool.run_parallel_async([job], process_ledger=ledger)
        )
        while not spawning:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_while_spawning())

    assert not ledger.all_exited()


@pytest.mark.parametrize("second_interrupt_at", ["exit-check", "write"])
def test_second_interrupt_during_preservation_keeps_original_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    second_interrupt_at: str,
) -> None:
    _use_synthetic_reviewers(monkeypatch)
    original = SystemExit(3)
    _interrupt_after_completion(
        monkeypatch, _completed_review("claude-generalist"), interruption=original
    )

    def second_ctrl_c(*_args, **_kwargs):
        raise KeyboardInterrupt

    if second_interrupt_at == "exit-check":
        monkeypatch.setattr(
            subprocess_pool.ProviderProcessLedger, "all_exited", second_ctrl_c
        )
    else:
        monkeypatch.setattr(multi_review, "write_run_artifact_text", second_ctrl_c)

    with caplog.at_level(logging.WARNING, logger="agent_flow.multi_review"):
        raised = _raised_by(
            lambda: multi_review.run_distribution(
                _single_claude_review(tmp_path), tmp_path
            )
        )

    assert raised is original
    assert len(caplog.records) == 1


def test_second_interrupt_while_warning_keeps_original_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_synthetic_reviewers(monkeypatch)
    original = SystemExit(3)
    _interrupt_after_completion(
        monkeypatch, _completed_review("claude-generalist"), interruption=original
    )

    def second_ctrl_c(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(logging.getLogger("agent_flow.multi_review"), "warning", second_ctrl_c)

    raised = _raised_by(
        lambda: multi_review.run_distribution(_single_claude_review(tmp_path), tmp_path)
    )

    assert raised is original
    assert _interrupted_files(tmp_path) == ["review-generalist-claude-interrupted.md"]


@pytest.mark.parametrize(
    "interruption",
    [KeyboardInterrupt(), SystemExit(3), asyncio.CancelledError()],
    ids=["keyboard-interrupt", "system-exit", "cancelled"],
)
@pytest.mark.parametrize(
    ("completed", "status"),
    [
        (
            SubprocessResult(job_id="claude-generalist", timed_out=True),
            "TIMEOUT",
        ),
        (
            SubprocessResult(
                job_id="claude-generalist", returncode=1, stderr="authentication failed"
            ),
            "ERROR",
        ),
    ],
    ids=["timeout", "error"],
)
def test_interrupted_review_preserves_failed_reviewer_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: BaseException,
    completed: SubprocessResult,
    status: str,
) -> None:
    _use_synthetic_reviewers(monkeypatch)
    _interrupt_after_completion(monkeypatch, completed, interruption=interruption)

    raised = _raised_by(
        lambda: multi_review.run_distribution(_single_claude_review(tmp_path), tmp_path)
    )

    assert raised is interruption
    preserved = tmp_path / "review-generalist-claude-interrupted.md"
    assert _interrupted_files(tmp_path) == [preserved.name]
    assert f"- status: {status}" in preserved.read_text(encoding="utf-8")


def _completed_review(job_id: str) -> SubprocessResult:
    return SubprocessResult(
        job_id=job_id,
        returncode=0,
        stdout="reviewer-source: sub-agent\nFINDING-A\nverdict: approve\n",
    )


def _single_claude_review(tmp_path: Path) -> multi_review.Distribution:
    return multi_review.Distribution(
        by_cli={
            "claude": [
                _reviewer_job(tmp_path, "claude", angle, {"role": "quick"})
                for angle in ("generalist", "types")
            ]
        },
        phase_id="review",
    )


def test_interrupted_review_does_not_touch_approval_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_synthetic_reviewers(monkeypatch)
    _interrupt_after_completion(monkeypatch, _completed_review("claude-generalist"))
    earlier_artifact = tmp_path / "review-generalist-claude.md"
    earlier_artifact.write_text("# claude-generalist\n\n- status: OK\n", encoding="utf-8")
    earlier_results = tmp_path / "review-review-results.json"
    earlier_results.write_text('{"schema_version": 1}\n', encoding="utf-8")
    before = {path.name: path.read_bytes() for path in (earlier_artifact, earlier_results)}

    with pytest.raises(KeyboardInterrupt):
        multi_review.run_distribution(_single_claude_review(tmp_path), tmp_path)

    assert {name: (tmp_path / name).read_bytes() for name in before} == before
    preserved = tmp_path / "review-generalist-claude-interrupted.md"
    assert _interrupted_files(tmp_path) == [preserved.name]
    first_line = preserved.read_text(encoding="utf-8").splitlines()[0]
    assert "not approval evidence" in first_line


def test_interrupted_review_withholds_results_when_reviewer_exit_is_unconfirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _use_synthetic_reviewers(monkeypatch)
    monkeypatch.setattr(subprocess_pool, "_PROVIDER_REAP_TIMEOUT_S", 0.2)
    running = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        _interrupt_after_completion(
            monkeypatch,
            _completed_review("claude-generalist"),
            running_process_group=running.pid,
        )
        with caplog.at_level(logging.WARNING, logger="agent_flow.multi_review"):
            with pytest.raises(KeyboardInterrupt):
                multi_review.run_distribution(_single_claude_review(tmp_path), tmp_path)
    finally:
        os.killpg(running.pid, signal.SIGKILL)
        running.wait(timeout=10)

    assert _interrupted_files(tmp_path) == []
    assert len(caplog.records) == 1


def test_interrupted_review_keeps_original_interruption_when_preservation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _use_synthetic_reviewers(monkeypatch)
    _interrupt_after_completion(monkeypatch, _completed_review("claude-generalist"))

    def disk_full(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(multi_review, "write_run_artifact_text", disk_full)

    with caplog.at_level(logging.WARNING, logger="agent_flow.multi_review"):
        with pytest.raises(KeyboardInterrupt):
            multi_review.run_distribution(_single_claude_review(tmp_path), tmp_path)

    assert _interrupted_files(tmp_path) == []
    assert len(caplog.records) == 1


def _complete_every_review(monkeypatch: pytest.MonkeyPatch) -> None:
    def run_parallel(jobs, *, on_result=None, **_kwargs):
        results = [_completed_review(job.job_id) for job in jobs]
        for result in results:
            if on_result is not None:
                on_result(result)
        return results

    monkeypatch.setattr(multi_review, "run_parallel", run_parallel)


def _interrupt_leader_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, interrupt
) -> None:
    monkeypatch.setattr(multi_review, "leader_root_for", lambda _root: tmp_path)
    monkeypatch.setattr(
        multi_review, "leader_sweep_include_ignored_for", lambda _leader: False
    )
    monkeypatch.setattr(
        multi_review,
        "capture_leader_snapshot",
        lambda _leader, **_kwargs: SimpleNamespace(scope="tracked-only"),
    )
    monkeypatch.setattr(multi_review, "assert_leader_unchanged", interrupt)


@pytest.mark.parametrize("interrupted_at", ["second-artifact-write", "leader-check"])
def test_cancelled_attempt_restores_normal_artifacts_and_preserves_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted_at: str
) -> None:
    _use_synthetic_reviewers(monkeypatch)
    _complete_every_review(monkeypatch)
    interruption = KeyboardInterrupt()
    if interrupted_at == "second-artifact-write":
        real_write = multi_review._write_review_artifact
        written: list[str] = []

        def ctrl_c_after_first_artifact(job, content):
            if written:
                raise interruption
            real_write(job, content)
            written.append(job.output_path.name)

        monkeypatch.setattr(
            multi_review, "_write_review_artifact", ctrl_c_after_first_artifact
        )
    else:
        def ctrl_c_during_leader_check(*_args, **_kwargs):
            raise interruption

        _interrupt_leader_check(monkeypatch, tmp_path, ctrl_c_during_leader_check)
    earlier_artifact = tmp_path / "review-generalist-claude.md"
    earlier_artifact.write_text("# claude-generalist\n\n- status: OK\n", encoding="utf-8")
    earlier_results = tmp_path / "review-review-results.json"
    earlier_results.write_text('{"schema_version": 1}\n', encoding="utf-8")
    distribution = _single_claude_review(tmp_path)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    raised = _raised_by(lambda: multi_review.run_distribution(distribution, tmp_path))

    assert raised is interruption
    assert {name: (tmp_path / name).read_bytes() for name in before} == before
    assert {path.name for path in tmp_path.iterdir()} == set(before) | {
        "review-generalist-claude-interrupted.md",
        "review-types-claude-interrupted.md",
    }


@pytest.mark.parametrize(
    "restore_failure",
    [OSError(28, "No space left on device"), KeyboardInterrupt()],
    ids=["disk-full", "second-interrupt"],
)
def test_failed_restore_keeps_the_original_and_restores_the_rest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    restore_failure: BaseException,
) -> None:
    _use_synthetic_reviewers(monkeypatch)
    _complete_every_review(monkeypatch)
    interruption = KeyboardInterrupt()
    restoring: list[bool] = []

    def ctrl_c_during_leader_check(*_args, **_kwargs):
        restoring.append(True)
        raise interruption

    _interrupt_leader_check(monkeypatch, tmp_path, ctrl_c_during_leader_check)
    real_replace = os.replace

    def generalist_restore_fails(src, dst, **kwargs):
        if restoring and Path(dst).name == "review-generalist-claude.md":
            raise restore_failure
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", generalist_restore_fails)
    earlier_generalist = b"# earlier generalist\n"
    earlier_types = b"# earlier types\n"
    (tmp_path / "review-generalist-claude.md").write_bytes(earlier_generalist)
    (tmp_path / "review-types-claude.md").write_bytes(earlier_types)
    distribution = _single_claude_review(tmp_path)

    with caplog.at_level(logging.WARNING, logger="agent_flow.multi_review"):
        raised = _raised_by(lambda: multi_review.run_distribution(distribution, tmp_path))

    assert raised is interruption
    assert (tmp_path / "review-types-claude.md").read_bytes() == earlier_types
    kept = [
        path
        for path in tmp_path.iterdir()
        if path.name != "review-generalist-claude.md"
        and path.read_bytes() == earlier_generalist
    ]
    assert len(kept) == 1
    assert _interrupted_files(tmp_path) == [
        "review-generalist-claude-interrupted.md",
        "review-types-claude-interrupted.md",
    ]
    assert len(caplog.records) == 1
    assert kept[0].name in caplog.records[0].getMessage()


def test_later_attempt_keeps_an_original_an_earlier_restore_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_synthetic_reviewers(monkeypatch)
    _complete_every_review(monkeypatch)
    interruption = KeyboardInterrupt()
    first_attempt = [True]
    restoring: list[bool] = []

    def ctrl_c_during_first_leader_check(*_args, **_kwargs):
        if first_attempt:
            restoring.append(True)
            raise interruption

    _interrupt_leader_check(monkeypatch, tmp_path, ctrl_c_during_first_leader_check)
    real_replace = os.replace

    def generalist_restore_fails(src, dst, **kwargs):
        if restoring and Path(dst).name == "review-generalist-claude.md":
            raise OSError(28, "No space left on device")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", generalist_restore_fails)
    earlier = b"# earlier generalist\n"
    (tmp_path / "review-generalist-claude.md").write_bytes(earlier)
    first = _raised_by(
        lambda: multi_review.run_distribution(_single_claude_review(tmp_path), tmp_path)
    )
    assert first is interruption
    first_attempt.clear()
    restoring.clear()

    multi_review.run_distribution(_single_claude_review(tmp_path), tmp_path)

    kept = [
        path
        for path in tmp_path.iterdir()
        if path.name != "review-generalist-claude.md" and path.read_bytes() == earlier
    ]
    assert len(kept) == 1


def test_cancellation_after_the_attempt_completed_keeps_its_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_synthetic_reviewers(monkeypatch)
    _complete_every_review(monkeypatch)
    interruption = KeyboardInterrupt()
    completed: list[bool] = []
    _interrupt_leader_check(
        monkeypatch, tmp_path, lambda *_args, **_kwargs: completed.append(True)
    )
    real_unlink = os.unlink

    def ctrl_c_while_cleaning_up(path, *args, **kwargs):
        if completed:
            raise interruption
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", ctrl_c_while_cleaning_up)
    earlier = b"# earlier generalist\n"
    (tmp_path / "review-generalist-claude.md").write_bytes(earlier)

    raised = _raised_by(
        lambda: multi_review.run_distribution(_single_claude_review(tmp_path), tmp_path)
    )

    assert raised is interruption
    assert (tmp_path / "review-generalist-claude.md").read_bytes() != earlier
    assert (tmp_path / "review-types-claude.md").exists()
    assert _interrupted_files(tmp_path) == []


def test_backup_cleanup_failure_does_not_hide_the_leader_check_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _use_synthetic_reviewers(monkeypatch)
    _complete_every_review(monkeypatch)
    tripwire = WorktreeIsolationError("leader checkout changed during review")
    checked: list[bool] = []

    def leader_changed(*_args, **_kwargs):
        checked.append(True)
        raise tripwire

    _interrupt_leader_check(monkeypatch, tmp_path, leader_changed)
    real_unlink = os.unlink

    def cleanup_denied(path, *args, **kwargs):
        if checked:
            raise PermissionError(13, "Permission denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", cleanup_denied)
    (tmp_path / "review-generalist-claude.md").write_bytes(b"# earlier generalist\n")

    with caplog.at_level(logging.WARNING, logger="agent_flow.multi_review"):
        raised = _raised_by(
            lambda: multi_review.run_distribution(_single_claude_review(tmp_path), tmp_path)
        )

    assert raised is tripwire
    assert len(caplog.records) == 1


def test_cancellation_while_copying_an_original_survives_a_failed_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _use_synthetic_reviewers(monkeypatch)
    _complete_every_review(monkeypatch)
    interruption = KeyboardInterrupt()
    real_copy = shutil.copyfileobj
    copies: list[str] = []

    def ctrl_c_during_second_copy(source, target, *args, **kwargs):
        if copies:
            copies.append("interrupted")
            raise interruption
        copies.append("copied")
        return real_copy(source, target, *args, **kwargs)

    real_unlink = os.unlink
    cleanup_attempts: list[bool] = []

    def partial_copy_cleanup_denied(path, *args, **kwargs):
        if "interrupted" in copies and not cleanup_attempts:
            cleanup_attempts.append(True)
            raise PermissionError(13, "Permission denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "copyfileobj", ctrl_c_during_second_copy)
    monkeypatch.setattr(os, "unlink", partial_copy_cleanup_denied)
    earlier_generalist = b"# earlier generalist\n"
    earlier_types = b"# earlier types\n"
    (tmp_path / "review-generalist-claude.md").write_bytes(earlier_generalist)
    (tmp_path / "review-types-claude.md").write_bytes(earlier_types)
    distribution = _single_claude_review(tmp_path)

    with caplog.at_level(logging.WARNING, logger="agent_flow.multi_review"):
        raised = _raised_by(lambda: multi_review.run_distribution(distribution, tmp_path))

    assert cleanup_attempts == [True]
    assert raised is interruption
    assert (tmp_path / "review-generalist-claude.md").read_bytes() == earlier_generalist
    assert (tmp_path / "review-types-claude.md").read_bytes() == earlier_types
    assert _interrupted_files(tmp_path) == [
        "review-generalist-claude-interrupted.md",
        "review-types-claude-interrupted.md",
    ]
    assert len(caplog.records) == 1


def test_cancellation_while_writing_survives_a_failed_writer_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _use_synthetic_reviewers(monkeypatch)
    _complete_every_review(monkeypatch)
    interruption = KeyboardInterrupt()
    real_fsync = os.fsync
    fsyncs: list[str] = []

    def ctrl_c_during_second_artifact_fsync(fd):
        if len(fsyncs) == 1:
            fsyncs.append("interrupted")
            raise interruption
        fsyncs.append("synced")
        return real_fsync(fd)

    real_unlink = os.unlink
    cleanup_attempts: list[bool] = []

    def writer_cleanup_denied(path, *args, **kwargs):
        if "interrupted" in fsyncs and not cleanup_attempts:
            cleanup_attempts.append(True)
            raise PermissionError(13, "Permission denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", ctrl_c_during_second_artifact_fsync)
    monkeypatch.setattr(os, "unlink", writer_cleanup_denied)
    earlier = b"# earlier generalist\n"
    (tmp_path / "review-generalist-claude.md").write_bytes(earlier)
    distribution = _single_claude_review(tmp_path)

    with caplog.at_level(logging.WARNING, logger="agent_flow.multi_review"):
        raised = _raised_by(lambda: multi_review.run_distribution(distribution, tmp_path))

    assert cleanup_attempts == [True]
    assert raised is interruption
    assert (tmp_path / "review-generalist-claude.md").read_bytes() == earlier
    assert not (tmp_path / "review-types-claude.md").exists()
    assert _interrupted_files(tmp_path) == [
        "review-generalist-claude-interrupted.md",
        "review-types-claude-interrupted.md",
    ]
    assert len(caplog.records) == 1


def test_error_deliberately_raised_from_a_cancellation_stays_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_synthetic_reviewers(monkeypatch)
    _complete_every_review(monkeypatch)

    def leader_check_times_out(*_args, **_kwargs):
        try:
            raise asyncio.CancelledError
        except asyncio.CancelledError as cancelled:
            raise TimeoutError("leader check timed out") from cancelled

    _interrupt_leader_check(monkeypatch, tmp_path, leader_check_times_out)

    raised = _raised_by(
        lambda: multi_review.run_distribution(_single_claude_review(tmp_path), tmp_path)
    )

    assert isinstance(raised, TimeoutError)
    assert (tmp_path / "review-generalist-claude.md").exists()
    assert (tmp_path / "review-types-claude.md").exists()
    assert _interrupted_files(tmp_path) == []


def test_second_cancellation_during_cleanup_keeps_the_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _use_synthetic_reviewers(monkeypatch)
    _complete_every_review(monkeypatch)
    original = SystemExit(3)
    real_copy = shutil.copyfileobj
    copies: list[str] = []

    def exit_during_second_copy(source, target, *args, **kwargs):
        if copies:
            copies.append("exited")
            raise original
        copies.append("copied")
        return real_copy(source, target, *args, **kwargs)

    real_unlink = os.unlink
    second_interrupts: list[bool] = []

    def ctrl_c_during_cleanup(path, *args, **kwargs):
        if "exited" in copies and not second_interrupts:
            second_interrupts.append(True)
            raise KeyboardInterrupt
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "copyfileobj", exit_during_second_copy)
    monkeypatch.setattr(os, "unlink", ctrl_c_during_cleanup)
    earlier_generalist = b"# earlier generalist\n"
    earlier_types = b"# earlier types\n"
    (tmp_path / "review-generalist-claude.md").write_bytes(earlier_generalist)
    (tmp_path / "review-types-claude.md").write_bytes(earlier_types)
    distribution = _single_claude_review(tmp_path)

    with caplog.at_level(logging.WARNING, logger="agent_flow.multi_review"):
        raised = _raised_by(lambda: multi_review.run_distribution(distribution, tmp_path))

    assert second_interrupts == [True]
    assert raised is original
    assert (tmp_path / "review-generalist-claude.md").read_bytes() == earlier_generalist
    assert (tmp_path / "review-types-claude.md").read_bytes() == earlier_types
    assert _interrupted_files(tmp_path) == [
        "review-generalist-claude-interrupted.md",
        "review-types-claude-interrupted.md",
    ]
    assert len(caplog.records) == 1


def test_ctrl_c_translated_by_asyncio_stays_a_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_synthetic_reviewers(monkeypatch)

    def asyncio_run_after_ctrl_c(_jobs, *, on_result=None, **_kwargs):
        if on_result is not None:
            on_result(_completed_review("claude-generalist"))
        try:
            raise asyncio.CancelledError
        except asyncio.CancelledError:
            raise KeyboardInterrupt

    monkeypatch.setattr(multi_review, "run_parallel", asyncio_run_after_ctrl_c)

    raised = _raised_by(
        lambda: multi_review.run_distribution(_single_claude_review(tmp_path), tmp_path)
    )

    assert type(raised) is KeyboardInterrupt
    assert _interrupted_files(tmp_path) == ["review-generalist-claude-interrupted.md"]


def test_uninterrupted_review_writes_only_normal_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_synthetic_reviewers(monkeypatch)
    monkeypatch.setattr(
        multi_review,
        "run_parallel",
        lambda jobs, **_kwargs: [_completed_review(job.job_id) for job in jobs],
    )
    (tmp_path / "review-generalist-claude.md").write_text("# earlier\n", encoding="utf-8")
    distribution = _single_claude_review(tmp_path)
    before = {path.name for path in tmp_path.iterdir()}

    multi_review.run_distribution(distribution, tmp_path)

    assert {path.name for path in tmp_path.iterdir()} == before | {
        "review-generalist-claude.md",
        "review-types-claude.md",
    }
    assert sorted(path.name for path in tmp_path.glob("review-*.md")) == [
        "review-generalist-claude.md",
        "review-types-claude.md",
    ]
