"""Async parallel subprocess pool for multi-CLI fan-out.

Multi-reviewer phases delegate review angles across whichever AI CLIs are
installed. Each CLI subprocess is independent: response times differ, any one
can fail or time out, and partial results must survive.

Design points:
  - asyncio.create_subprocess_exec for non-blocking I/O.
  - asyncio.gather(..., return_exceptions=True) so one failure does not
    abort the others.
  - Per-job timeout + soft kill + drained stderr.
  - Results are durable: each job writes its artifact independently, so a
    timed-out angle leaves the other artifacts intact and the host AI can
    aggregate whatever completed.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_flow.core.worktree_isolation import (
    WorktreeIsolationError,
    max_worker_capacity,
)
from agent_flow.providers.subprocess import (
    _PROVIDER_POLL_S,
    _PROVIDER_REAP_TIMEOUT_S,
    MAX_PROVIDER_OUTPUT_BYTES,
    _provider_output_exceeded,
    _read_provider_outputs,
    confined_provider_launch,
)


@dataclass
class SubprocessJob:
    job_id: str             # stable id used for artifact naming
    binary: str             # CLI binary name (e.g. "claude", "codex", "omp")
    args: tuple[str, ...]   # full argv after binary (e.g. ("-p", "<prompt>"))
    cwd: Path
    timeout_s: int = 600    # default 10 minutes per job
    env: Mapping[str, str] | None = None
    allow_workspace_writes: bool = True


@dataclass
class SubprocessResult:
    job_id: str
    stdout: str = ""
    stderr: str = ""
    returncode: int = -1
    duration_s: float = 0.0
    timed_out: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.error is None and self.returncode == 0


async def _run_one(job: SubprocessJob) -> SubprocessResult:
    started = time.monotonic()
    proc: asyncio.subprocess.Process | None = None
    wait_task: asyncio.Task[int] | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    captured_stdout = ""
    captured_stderr = ""
    try:
        with confined_provider_launch(
            argv=(job.binary, *job.args),
            cwd=job.cwd,
            env=job.env,
            allow_workspace_writes=job.allow_workspace_writes,
        ) as launch:
            stdout_path = launch.scratch / "stdout"
            stderr_path = launch.scratch / "stderr"
            try:
                with stdout_path.open("xb") as stdout_file, stderr_path.open(
                    "xb"
                ) as stderr_file:
                    proc = await asyncio.create_subprocess_exec(
                        *launch.argv,
                        stdin=asyncio.subprocess.DEVNULL,
                        cwd=str(launch.cwd),
                        env=launch.env,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        start_new_session=True,
                        close_fds=True,
                        pass_fds=launch.lease.process_lifetime_fds,
                    )
                    wait_task = asyncio.create_task(proc.wait())
                    outcome = await _wait_for_provider(
                        wait_task,
                        timeout_s=job.timeout_s,
                        stdout_path=stdout_path,
                        stderr_path=stderr_path,
                    )
                    if outcome != "completed":
                        _kill_process_tree(proc)
                    await _bounded_reap(proc, wait_task)
                    # 출력 한계 초과로 `outcome`을 덮지 않는다. 덮으면 스스로
                    # 종료한 프로세스의 exit status와 timeout 사실이 같은 자리에서
                    # 사라진다.
                    output_limited = outcome == "output-limit" or (
                        _provider_output_exceeded(stdout_path, stderr_path)
                    )
            except FileNotFoundError:
                return SubprocessResult(
                    job_id=job.job_id,
                    error=f"binary not found: {job.binary}",
                    duration_s=time.monotonic() - started,
                )
            except OSError as exc:
                return SubprocessResult(
                    job_id=job.job_id,
                    error=f"OSError starting subprocess: {exc}",
                    duration_s=time.monotonic() - started,
                )
            finally:
                try:
                    await _cleanup_provider_process(proc, wait_task)
                finally:
                    captured_stdout, captured_stderr = _read_provider_outputs(
                        stdout_path,
                        stderr_path,
                    )

            stdout, stderr = captured_stdout, captured_stderr
            return SubprocessResult(
                job_id=job.job_id,
                stdout=stdout,
                stderr=stderr,
                returncode=(
                    proc.returncode
                    if outcome == "completed" and proc.returncode is not None
                    else -1
                ),
                duration_s=time.monotonic() - started,
                timed_out=outcome == "timeout",
                error=(
                    f"provider output exceeded {MAX_PROVIDER_OUTPUT_BYTES} bytes"
                    if output_limited
                    else None
                ),
            )
    except (OSError, WorktreeIsolationError) as exc:
        stdout, stderr = captured_stdout, captured_stderr
        return SubprocessResult(
            job_id=job.job_id,
            stdout=stdout,
            stderr=stderr,
            error=str(exc),
            duration_s=time.monotonic() - started,
        )
    finally:
        await _cleanup_provider_process(proc, wait_task)


async def _cleanup_provider_process(
    proc: asyncio.subprocess.Process | None,
    wait_task: asyncio.Task[int] | None,
) -> None:
    if proc is None:
        return
    try:
        _kill_process_tree(proc)
        await _bounded_reap(proc, wait_task)
    except (OSError, WorktreeIsolationError):
        # Cleanup must not mask the launch, timeout, or reap error being reported.
        pass


async def _wait_for_provider(
    wait_task: asyncio.Task[int],
    *,
    timeout_s: float,
    stdout_path: Path,
    stderr_path: Path,
) -> str:
    deadline = time.monotonic() + max(timeout_s, 0)
    while True:
        if _provider_output_exceeded(stdout_path, stderr_path):
            return "output-limit"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout"
        done, _ = await asyncio.wait(
            {wait_task}, timeout=min(_PROVIDER_POLL_S, remaining)
        )
        if done:
            return "completed"


async def _bounded_reap(
    proc: asyncio.subprocess.Process,
    wait_task: asyncio.Task[int] | None,
) -> None:
    if proc.returncode is not None:
        return
    task = wait_task if wait_task is not None else asyncio.create_task(proc.wait())
    try:
        await asyncio.wait_for(
            asyncio.shield(task), timeout=_PROVIDER_REAP_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        _kill_process_tree(proc)
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=_PROVIDER_REAP_TIMEOUT_S
            )
        except asyncio.TimeoutError as exc:
            raise WorktreeIsolationError(
                "provider process could not be reaped within the bounded deadline"
            ) from exc


def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    pid = proc.pid
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def run_parallel_async(
    jobs: Sequence[SubprocessJob], *, max_concurrency: int | None = None,
) -> list[SubprocessResult]:
    """Run all jobs concurrently. Returns results in the same order as jobs.

    Uses return_exceptions=True for partial-survival: an unexpected `Exception`
    in one job is captured as a SubprocessResult with `error` set, not aborting
    siblings. `BaseException` (KeyboardInterrupt, asyncio.CancelledError,
    SystemExit) is re-raised — those signal genuine cancellation/shutdown that
    must propagate.
    """
    if not jobs:
        return []
    limit = max_concurrency if max_concurrency and max_concurrency > 0 else max_worker_capacity()
    sem = asyncio.Semaphore(limit)

    async def _bounded(job: SubprocessJob) -> SubprocessResult:
        async with sem:
            return await _run_one(job)

    tasks = [asyncio.create_task(_bounded(j)) for j in jobs]
    raw = await asyncio.gather(*tasks, return_exceptions=True)
    out: list[SubprocessResult] = []
    for job, item in zip(jobs, raw):
        if isinstance(item, BaseException) and not isinstance(item, Exception):
            # KeyboardInterrupt / CancelledError / SystemExit — propagate.
            raise item
        if isinstance(item, Exception):
            out.append(SubprocessResult(
                job_id=job.job_id,
                error=f"unexpected: {type(item).__name__}: {item}",
            ))
        else:
            out.append(item)
    return out


def run_parallel(jobs: Sequence[SubprocessJob]) -> list[SubprocessResult]:
    """Sync wrapper around run_parallel_async. Preferred entry point for
    callers that aren't already inside an event loop.
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        # Caller is inside a loop already; use run_coroutine_threadsafe-like
        # path. For simplicity, assume CLI usage where we're not in a loop.
        raise RuntimeError(
            "run_parallel called from inside a running event loop; "
            "use `await run_parallel_async(...)` instead."
        )
    return asyncio.run(run_parallel_async(jobs))


# Note: artifact rendering and writing live in `multi_review.py` (the only
# caller). Earlier this module duplicated them; removed in cleanup.
