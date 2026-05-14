from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


PROTECTED_WORKTREE_BRANCHES = {"main", "master", "develop"}


@dataclass(frozen=True)
class WorktreePlan:
    name: str
    branch: str
    path: Path
    base_ref: str
    branch_explicit: bool = False


@dataclass(frozen=True)
class WorktreeStatus:
    name: str
    branch: str
    path: Path
    exists: bool
    branch_created_by_agent_flow: bool = False


def plan_worktree(*, root: Path, name: str, branch: str | None = None) -> WorktreePlan:
    safe_name = _feature_worktree_name(name)
    selected_branch = branch or f"feat/{safe_name.removeprefix('feat-')}"
    _validate_branch(selected_branch)
    if selected_branch in PROTECTED_WORKTREE_BRANCHES:
        raise ValueError(f"protected worktree branch is not allowed: {selected_branch}")
    return WorktreePlan(
        name=safe_name,
        branch=selected_branch,
        path=root / ".agent-flow" / "worktrees" / safe_name,
        # leader worktree가 feature branch여도 새 작업은 기본 브랜치 commit에서 시작한다.
        base_ref=_default_base_ref(root),
        branch_explicit=branch is not None,
    )


def create_worktree(*, root: Path, plan: WorktreePlan, allow_dirty: bool = False) -> WorktreeStatus:
    if not allow_dirty and _git_dirty(root):
        raise RuntimeError("leader workspace is dirty; pass --allow-dirty to create a worktree anyway")
    if plan.path.exists():
        if not (plan.path / ".git").exists():
            raise RuntimeError(
                f"worktree path exists but is not a git worktree: {plan.path}. "
                f"Run `agent-flow worktree remove --name {plan.name}` to clear stale state."
            )
        existing = get_worktree_status(root=root, name=plan.name)
        if plan.branch_explicit and existing.branch != plan.branch:
            raise ValueError(
                f"worktree {plan.name} already uses branch {existing.branch}; "
                f"requested {plan.branch}"
            )
        return existing
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    if worktree_branch_exists(root=root, branch=plan.branch):
        _run_git(root, "worktree", "add", str(plan.path), plan.branch)
        branch_created = False
    else:
        _run_git(root, "worktree", "add", "-b", plan.branch, str(plan.path), plan.base_ref)
        branch_created = True
    status = WorktreeStatus(
        name=plan.name,
        branch=plan.branch,
        path=plan.path,
        exists=True,
        branch_created_by_agent_flow=branch_created,
    )
    try:
        write_worktree_manifest(root=root, status=status)
    except Exception:
        try:
            remove_worktree(root=root, status=status)
        except Exception:
            pass
        raise
    return status


def remove_worktree(*, root: Path, status: WorktreeStatus, delete_branch: bool = True) -> None:
    branch_to_delete = _owned_branch_for_live_worktree(root=root, status=status) if delete_branch else None
    if status.path.exists():
        _run_git(root, "worktree", "remove", "--force", str(status.path))
    if branch_to_delete is not None:
        _run_git(root, "branch", "-D", branch_to_delete)


def worktree_branch_exists(*, root: Path, branch: str) -> bool:
    result = subprocess.run(
        ("git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"),
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _default_base_ref(root: Path) -> str:
    for ref in ("main", "origin/main", "master", "origin/master", "develop", "origin/develop"):
        if _git_commit_ref_exists(root=root, ref=ref):
            return ref
    return "HEAD"


def _git_commit_ref_exists(*, root: Path, ref: str) -> bool:
    try:
        result = subprocess.run(
            ("git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"),
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        # git을 호출할 수 없으면 기본 ref 후보가 없는 것으로 보고 HEAD fallback을 쓴다.
        return False
    return result.returncode == 0


def get_worktree_status(*, root: Path, name: str) -> WorktreeStatus:
    plan = plan_worktree(root=root, name=name)
    manifest = root / ".agent-flow" / "worktrees" / plan.name / "manifest.json"
    if manifest.exists():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return WorktreeStatus(
                name=plan.name,
                branch=plan.branch,
                path=plan.path,
                exists=plan.path.exists(),
                branch_created_by_agent_flow=False,
            )
        manifest_name = payload.get("name", plan.name)
        status_name = plan.name
        if isinstance(manifest_name, str):
            try:
                manifest_name_safe = _safe_component(manifest_name)
            except ValueError:
                manifest_name_safe = ""
            if manifest_name_safe == plan.name:
                status_name = plan.name

        manifest_branch = payload.get("branch", plan.branch)
        status_branch = plan.branch
        manifest_branch_valid = False
        if isinstance(manifest_branch, str):
            try:
                _validate_branch(manifest_branch)
            except ValueError:
                manifest_branch_valid = False
            else:
                status_branch = manifest_branch
                manifest_branch_valid = True

        branch_created = (
            payload.get("branch_created_by_agent_flow") is True
            and manifest_branch_valid
            and status_branch == plan.branch
        )
        return WorktreeStatus(
            name=status_name,
            branch=status_branch,
            path=plan.path,
            exists=plan.path.exists(),
            branch_created_by_agent_flow=branch_created,
        )
    return WorktreeStatus(
        name=plan.name,
        branch=plan.branch,
        path=plan.path,
        exists=plan.path.exists(),
        branch_created_by_agent_flow=False,
    )


def write_worktree_manifest(*, root: Path, status: WorktreeStatus) -> Path:
    path = root / ".agent-flow" / "worktrees" / status.name / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(status)
    payload["path"] = str(status.path.relative_to(root))
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


def _owned_branch_for_live_worktree(*, root: Path, status: WorktreeStatus) -> str | None:
    planned_branch = plan_worktree(root=root, name=status.name).branch
    if not status.branch_created_by_agent_flow or status.branch != planned_branch:
        return None
    result = subprocess.run(
        ("git", "-C", str(status.path), "branch", "--show-current"),
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    current_branch = result.stdout.strip()
    return current_branch if current_branch == planned_branch else None


def _safe_component(value: str) -> str:
    lowered = value.strip().lower()
    safe = re.sub(r"[^a-z0-9._-]+", "-", lowered).strip("-")
    if not safe or safe.startswith(".") or ".." in safe:
        # 한글 등 비ASCII task도 기본 worktree 이름으로 쓸 수 있게 안정적인 fallback을 둔다.
        digest = hashlib.sha1(lowered.encode("utf-8")).hexdigest()[:8]
        safe = f"task-{digest}"
    return safe


def _feature_worktree_name(value: str) -> str:
    # worktree 디렉터리는 slash를 못 쓰므로 feat/<slug> 브랜치와 feat-<slug> 디렉터리를 짝지어 둔다.
    safe = _safe_component(value)
    return safe if safe.startswith("feat-") else f"feat-{safe}"


def _validate_branch(value: str) -> None:
    invalid_chars = set(" ~^:?*[\\")
    invalid = (
        not value
        or value.startswith("-")
        or value.startswith(".")
        or value.startswith("/")
        or value.endswith("/")
        or value.endswith(".")
        or value.endswith(".lock")
        or ".." in value
        or "//" in value
        or "@{" in value
        or any(ord(ch) < 32 or ch == "\x7f" or ch in invalid_chars for ch in value)
    )
    if invalid:
        raise ValueError(f"unsafe worktree branch: {value}")
