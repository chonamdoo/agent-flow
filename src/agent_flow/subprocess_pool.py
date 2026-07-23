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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from agent_flow.core.worktree_isolation import max_worker_capacity


@dataclass
class SubprocessJob:
    job_id: str             # stable id used for artifact naming
    binary: str             # CLI binary name (e.g. "claude", "codex", "omp")
    args: tuple[str, ...]   # full argv after binary (e.g. ("-p", "<prompt>"))
    cwd: Path
    timeout_s: int = 600    # default 10 minutes per job
    env: dict | None = None  # None inherits parent env; set to sanitize git leak


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
    try:
        proc = await asyncio.create_subprocess_exec(
            job.binary, *job.args,
            cwd=str(job.cwd),
            env=job.env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as e:
        return SubprocessResult(
            job_id=job.job_id,
            error=f"binary not found: {job.binary}",
            duration_s=time.monotonic() - started,
        )
    except OSError as e:
        return SubprocessResult(
            job_id=job.job_id,
            error=f"OSError starting subprocess: {e}",
            duration_s=time.monotonic() - started,
        )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=job.timeout_s,
        )
        return SubprocessResult(
            job_id=job.job_id,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            returncode=proc.returncode if proc.returncode is not None else -1,
            duration_s=time.monotonic() - started,
        )
    except asyncio.TimeoutError:
        _kill_process_tree(proc)
        try:
            stdout, stderr = await proc.communicate()
            captured_out = stdout.decode("utf-8", errors="replace")
            captured_err = stderr.decode("utf-8", errors="replace")
        except Exception:
            captured_out = ""
            captured_err = ""
        return SubprocessResult(
            job_id=job.job_id,
            stdout=captured_out,
            stderr=captured_err,
            timed_out=True,
            duration_s=time.monotonic() - started,
        )


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
