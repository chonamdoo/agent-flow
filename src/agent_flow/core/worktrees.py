from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorktreePlan:
    name: str
    branch: str
    path: Path


@dataclass(frozen=True)
class WorktreeStatus:
    name: str
    branch: str
    path: Path
    exists: bool


def plan_worktree(*, root: Path, name: str, branch: str | None = None) -> WorktreePlan:
    safe_name = _safe_component(name)
    selected_branch = branch or f"agent-flow/{safe_name}"
    _validate_branch(selected_branch)
    return WorktreePlan(
        name=safe_name,
        branch=selected_branch,
        path=root / ".agent-flow" / "worktrees" / safe_name,
    )


def create_worktree(*, root: Path, plan: WorktreePlan, allow_dirty: bool = False) -> WorktreeStatus:
    if not allow_dirty and _git_dirty(root):
        raise RuntimeError("leader workspace is dirty; pass --allow-dirty to create a worktree anyway")
    if plan.path.exists():
        return get_worktree_status(root=root, name=plan.name)
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    if worktree_branch_exists(root=root, branch=plan.branch):
        _run_git(root, "worktree", "add", str(plan.path), plan.branch)
    else:
        _run_git(root, "worktree", "add", "-b", plan.branch, str(plan.path), "HEAD")
    status = WorktreeStatus(name=plan.name, branch=plan.branch, path=plan.path, exists=True)
    write_worktree_manifest(root=root, status=status)
    return status


def remove_worktree(*, root: Path, status: WorktreeStatus, delete_branch: bool = True) -> None:
    if status.path.exists():
        _run_git(root, "worktree", "remove", "--force", str(status.path))
    if delete_branch:
        _run_git(root, "branch", "-D", status.branch)


def worktree_branch_exists(*, root: Path, branch: str) -> bool:
    result = subprocess.run(
        ("git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"),
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def get_worktree_status(*, root: Path, name: str) -> WorktreeStatus:
    plan = plan_worktree(root=root, name=name)
    manifest = root / ".agent-flow" / "worktrees" / plan.name / "manifest.json"
    if manifest.exists():
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        return WorktreeStatus(
            name=payload["name"],
            branch=payload["branch"],
            path=Path(payload["path"]),
            exists=Path(payload["path"]).exists(),
        )
    return WorktreeStatus(name=plan.name, branch=plan.branch, path=plan.path, exists=plan.path.exists())


def write_worktree_manifest(*, root: Path, status: WorktreeStatus) -> Path:
    path = root / ".agent-flow" / "worktrees" / status.name / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(status)
    payload["path"] = str(status.path)
    path.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
    return path


def _git_dirty(root: Path) -> bool:
    result = _run_git(root, "status", "--porcelain")
    dirty_lines = [
        line
        for line in result.stdout.splitlines()
        if not _is_agent_flow_status_line(line)
    ]
    return bool(dirty_lines)


def _is_agent_flow_status_line(line: str) -> bool:
    path = line[3:] if len(line) > 3 else ""
    return path == ".agent-flow" or path.startswith(".agent-flow/")


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *args),
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )


def _safe_component(value: str) -> str:
    lowered = value.strip().lower()
    safe = re.sub(r"[^a-z0-9._-]+", "-", lowered).strip("-")
    if not safe:
        raise ValueError("worktree name must contain at least one safe character")
    return safe


def _validate_branch(value: str) -> None:
    if value.startswith("-") or ".." in value or value.endswith(".lock"):
        raise ValueError(f"unsafe worktree branch: {value}")
