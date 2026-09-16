"""Run storage locations and active markers, without workflow dependencies."""
from __future__ import annotations

from pathlib import Path

from agent_flow.core.worktree_isolation import git_repo_state, git_safe

RUNS_DIRNAME = ".agent-flow/runs"
ACTIVE_MARKER = "active"
ACTIVE_LOCK = "active.lock"


def active_run_paths(project_root: Path) -> tuple[Path, ...]:
    runs_dir = project_root / RUNS_DIRNAME
    if not runs_dir.exists():
        return ()
    return tuple(sorted(
        (path for path in runs_dir.iterdir() if (path / ACTIVE_MARKER).exists()),
        key=lambda path: path.name,
    ))


def repository_state_dir(root: Path) -> Path:
    result = git_safe("rev-parse", "--git-common-dir", cwd=root)
    if not result.ok:
        # Unknown Git state must not redirect worker state into the leader.
        if git_repo_state(root) == "non-repo":
            return root / ".agent-flow"
        raise RuntimeError(
            f"cannot resolve the git common dir for {root}; "
            f"refusing to place agent-flow state inside the leader checkout: "
            f"{result.stderr.strip() or 'git did not answer'}"
        )
    git_common = Path(result.stdout.strip())
    if not git_common.is_absolute():
        git_common = root / git_common
    return git_common / "agent-flow"


def worktree_runtime_directory(root: Path) -> Path:
    return repository_state_dir(root) / "worktrees"
