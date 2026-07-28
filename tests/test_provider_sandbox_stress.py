"""Twenty concurrent sandboxed workers against one repository.

Concurrency is where the original bug lived: one worker in twenty writing an
absolute path is enough to contaminate the leader, and the odds grow with the
fan-out. Each worker here attacks the leader, a sibling, and another worker's
worktree; the assertion is that every protected path is byte-identical after.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.provider_sandbox import prove_sandbox  # noqa: E402
from agent_flow.core.worktrees import sandbox_policy_for_worktree  # noqa: E402
from agent_flow.providers.seatbelt import SeatbeltBackend  # noqa: E402
from agent_flow.providers.subprocess import ProviderCommand, run_provider  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="seatbelt is macOS only")

WORKERS = 20


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _real(path: Path) -> Path:
    return Path(os.path.realpath(str(path)))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def fleet(tmp_path: Path):
    leader = _real(tmp_path) / "repo"
    leader.mkdir(parents=True)
    for args in (("init", "-b", "main"), ("config", "user.email", "t@t"), ("config", "user.name", "t")):
        _git(*args, cwd=leader)
    (leader / "README.md").write_text("leader\n")
    _git("add", "-A", cwd=leader)
    _git("commit", "-m", "base", cwd=leader)

    root = leader / ".agent-flow" / "worktrees"
    root.mkdir(parents=True)
    worktrees = []
    for index in range(WORKERS):
        name = f"feat-w{index}"
        path = root / name
        _git("worktree", "add", "-b", f"feat/w{index}", str(path), "main", cwd=leader)
        worktrees.append((name, path))
    return leader, worktrees


def test_twenty_concurrent_workers_are_os_isolated(fleet) -> None:
    leader, worktrees = fleet
    protected = {str(leader / "README.md"): _sha(leader / "README.md")}
    leader_head = _git("rev-parse", "HEAD", cwd=leader).stdout.strip()

    def work(item):
        index, (name, path) = item
        victim = worktrees[(index + 1) % WORKERS][1]
        script = (
            f'echo mine{index} > own.txt; echo "own=$?"; '
            f'echo x > "{leader}/README.md" 2>/dev/null; echo "leader=$?"; '
            f'echo x > "{victim}/own.txt" 2>/dev/null; echo "victim=$?"'
        )
        boundary = prove_sandbox(
            SeatbeltBackend(),
            sandbox_policy_for_worktree(
                root=leader, worktree_path=path, name=name, branch=f"feat/w{index}"
            ),
        )
        return index, run_provider(
            ProviderCommand(name=name, argv=("/bin/sh", "-c", script), prompt_via_stdin=False),
            prompt="",
            cwd=path,
            sandbox=boundary,
        )

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(work, enumerate(worktrees)))

    for index, result in results:
        codes = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        assert codes["own"] == "0", (index, result.stdout, result.stderr)
        assert codes["leader"] != "0", (index, result.stdout)
        assert codes["victim"] != "0", (index, result.stdout)

    for path, digest in protected.items():
        assert _sha(Path(path)) == digest
    assert _git("rev-parse", "HEAD", cwd=leader).stdout.strip() == leader_head
    assert _git("status", "--porcelain", cwd=leader).stdout.strip() in ("", "?? .agent-flow/")

    for index, (_, path) in enumerate(worktrees):
        assert (path / "own.txt").read_text() == f"mine{index}\n"
