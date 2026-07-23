from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from agent_flow.core.commands import run_safe_command
from agent_flow.core.worktree_isolation import (
    assert_worktree_mergeable,
    git_safe,
    verify_linked_worktree,
    with_git_lock_retry,
    worktree_creation_lock,
)


PROTECTED_WORKTREE_BRANCHES = {"main", "master", "develop"}
GIT_WORKTREE_TIMEOUT_S = 300


@dataclass(frozen=True)
class WorktreePlan:
    name: str
    branch: str
    path: Path
    base_ref: str
    branch_explicit: bool = False
    requested_name: str = ""


@dataclass(frozen=True)
class WorktreeStatus:
    name: str
    branch: str
    path: Path
    exists: bool
    branch_created_by_agent_flow: bool = False
    requested_name: str = ""


def plan_worktree(
    *, root: Path, name: str, branch: str | None = None, unique: str | None = None
) -> WorktreePlan:
    # A per-worker unique token keeps two workers on the same task from
    # normalizing to one shared worktree, which would silently break isolation.
    base_name = name if unique is None else f"{name}-{unique}"
    safe_name = _feature_worktree_name(base_name)
    selected_branch = branch or f"feat/{safe_name.removeprefix('feat-')}"
    _validate_branch(selected_branch)
    if selected_branch in PROTECTED_WORKTREE_BRANCHES:
        raise ValueError(f"protected worktree branch is not allowed: {selected_branch}")
    if not selected_branch.startswith("feat/"):
        raise ValueError(f"worktree branch must start with feat/: {selected_branch}")
    return WorktreePlan(
        name=safe_name,
        branch=selected_branch,
        path=root / ".agent-flow" / "worktrees" / safe_name,
        # leader worktree가 feature branch여도 새 작업은 기본 브랜치 commit에서 시작한다.
        base_ref=_default_base_ref(root),
        branch_explicit=branch is not None,
        requested_name=name,
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
        _assert_same_requested_name(root=root, plan=plan)
        return existing
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    branch_created = _add_worktree_locked(root=root, plan=plan)
    # Fail closed: trust the path only after git confirms it is a linked
    # worktree of this repo on the expected branch.
    verify_linked_worktree(root=root, path=plan.path, expected_branch=plan.branch)
    status = WorktreeStatus(
        name=plan.name,
        branch=plan.branch,
        path=plan.path,
        exists=True,
        branch_created_by_agent_flow=branch_created,
        requested_name=plan.requested_name,
    )
    try:
        write_worktree_manifest(root=root, status=status)
    except Exception:
        try:
            remove_worktree(root=root, status=status, allow_unmerged=True)
        except Exception:
            pass
        raise
    return status


def _add_worktree_locked(*, root: Path, plan: WorktreePlan) -> bool:
    created = {"branch": False}

    def _prune_and_add() -> None:
        # Prune first so a stale registration from a crashed run cannot make
        # `worktree add` fail; both run under the creation lock + bounded retry.
        _run_git(root, "worktree", "prune")
        if worktree_branch_exists(root=root, branch=plan.branch):
            _run_git(root, "worktree", "add", str(plan.path), plan.branch)
            created["branch"] = False
        else:
            _run_git(root, "worktree", "add", "-b", plan.branch, str(plan.path), plan.base_ref)
            created["branch"] = True

    with worktree_creation_lock(root):
        with_git_lock_retry(_prune_and_add)
    return created["branch"]


def remove_worktree(
    *,
    root: Path,
    status: WorktreeStatus,
    delete_branch: bool = True,
    require_merged: bool = True,
    allow_unmerged: bool = False,
) -> None:
    live = status.path.exists() and (status.path / ".git").exists()
    if live and require_merged and not allow_unmerged:
        # Destructive: prove the work is already in the leader before removing.
        assert_worktree_mergeable(root=root, path=status.path)
    branch_to_delete = _owned_branch_for_live_worktree(root=root, status=status) if delete_branch else None
    if status.path.exists():
        force_args = ("--force",) if allow_unmerged else ()
        _run_git(root, "worktree", "remove", *force_args, str(status.path))
    if branch_to_delete is not None:
        _run_git(root, "branch", "-D", branch_to_delete)
    remove_worktree_metadata(root=root, name=status.name)


def _assert_same_requested_name(*, root: Path, plan: WorktreePlan) -> None:
    # 서로 다른 이름이 같은 safe name으로 정규화되면 기존 worktree를 조용히
    # 재사용해 상태가 섞일 수 있다. 명시적 별칭 재사용(--worktree)은 지원되는
    # 플로우라 차단하지 않고, 기록된 원본 이름과 다르면 경고만 남긴다.
    manifest = _worktree_manifest_path(root=root, name=plan.name)
    if not manifest.exists():
        return
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    recorded = payload.get("requested_name")
    if not isinstance(recorded, str) or not recorded or not plan.requested_name:
        return
    if recorded != plan.requested_name:
        print(
            f"warning: worktree {plan.name} was created for '{recorded}'; "
            f"reusing it for '{plan.requested_name}'",
            file=sys.stderr,
        )


def worktree_branch_exists(*, root: Path, branch: str) -> bool:
    result = git_safe("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", cwd=root)
    return result.ok


def _default_base_ref(root: Path) -> str:
    for ref in ("main", "origin/main", "master", "origin/master", "develop", "origin/develop"):
        if _git_commit_ref_exists(root=root, ref=ref):
            return ref
    return "HEAD"


def _git_commit_ref_exists(*, root: Path, ref: str) -> bool:
    result = git_safe("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=root)
    # git을 호출할 수 없으면 기본 ref 후보가 없는 것으로 보고 HEAD fallback을 쓴다.
    return result.ok


def get_worktree_status(*, root: Path, name: str) -> WorktreeStatus:
    plan = plan_worktree(root=root, name=name)
    manifest = _worktree_manifest_path(root=root, name=plan.name)
    legacy_manifest = _legacy_worktree_manifest_path(root=root, name=plan.name)
    if not manifest.exists() and legacy_manifest.exists():
        manifest = legacy_manifest
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
    path = _worktree_manifest_path(root=root, name=status.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(status)
    payload["path"] = str(status.path.relative_to(root))
    payload["leader_root"] = str(root)
    path.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
    return path


def worktree_runtime_root(*, root: Path, name: str) -> Path:
    return _agent_flow_git_dir(root) / "worktrees" / _feature_worktree_name(name)


def known_worktree_names(*, root: Path) -> list[str]:
    names: set[str] = set()
    checkout_root = root / ".agent-flow" / "worktrees"
    if checkout_root.exists():
        names.update(path.name for path in checkout_root.iterdir() if path.is_dir())
    runtime_root = _agent_flow_git_dir(root) / "worktrees"
    if runtime_root.exists():
        names.update(path.name for path in runtime_root.iterdir() if path.is_dir())
    return sorted(names)


def remove_worktree_metadata(*, root: Path, name: str) -> None:
    runtime_root = worktree_runtime_root(root=root, name=name)
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
    legacy_manifest = _legacy_worktree_manifest_path(root=root, name=name)
    if legacy_manifest.exists():
        legacy_manifest.unlink()


def _git_dirty(root: Path) -> bool:
    result = _run_git(root, "status", "--porcelain")
    dirty_lines = [
        line
        for line in result.stdout.splitlines()
        if not _is_agent_flow_status_line(line)
    ]
    return bool(dirty_lines)


def _worktree_manifest_path(*, root: Path, name: str) -> Path:
    return worktree_runtime_root(root=root, name=name) / "manifest.json"


def _legacy_worktree_manifest_path(*, root: Path, name: str) -> Path:
    return root / ".agent-flow" / "worktrees" / _feature_worktree_name(name) / "manifest.json"


def _agent_flow_git_dir(root: Path) -> Path:
    result = git_safe("rev-parse", "--git-common-dir", cwd=root)
    if not result.ok:
        return root / ".agent-flow"
    git_common = Path(result.stdout.strip())
    if not git_common.is_absolute():
        git_common = root / git_common
    return git_common / "agent-flow"


def _is_agent_flow_status_line(line: str) -> bool:
    path = line[3:] if len(line) > 3 else ""
    return path == ".agent-flow" or path.startswith(".agent-flow/")


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # worktree add/remove는 큰 저장소나 느린 디스크에서 30초를 넘을 수 있어 별도 여유를 둔다.
    result = git_safe(*args, cwd=root, timeout_s=GIT_WORKTREE_TIMEOUT_S)
    if not result.ok:
        # 호출자는 기존 subprocess 예외 경로로 처리하므로 형태를 유지한다.
        raise subprocess.CalledProcessError(
            result.returncode or 1,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )
    return subprocess.CompletedProcess(result.args, result.returncode or 0, result.stdout, result.stderr)


def _owned_branch_for_live_worktree(*, root: Path, status: WorktreeStatus) -> str | None:
    planned_branch = plan_worktree(root=root, name=status.name).branch
    if not status.branch_created_by_agent_flow or status.branch != planned_branch:
        return None
    result = git_safe("-C", str(status.path), "branch", "--show-current", cwd=root)
    if not result.ok:
        return None
    current_branch = result.stdout.strip()
    return current_branch if current_branch == planned_branch else None


def _safe_component(value: str) -> str:
    lowered = value.strip().lower()
    safe = re.sub(r"[^a-z0-9._-]+", "-", lowered).strip("-")
    if not safe or safe.startswith(".") or ".." in safe:
        if not any(char.isalnum() for char in lowered):
            raise ValueError(f"worktree name must contain at least one safe character: {value}")
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
