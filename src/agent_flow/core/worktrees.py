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
    WorktreeIsolationError,
    assert_worktree_mergeable,
    git_repo_state,
    git_safe,
    real_path,
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
    selected_branch = branch or _default_branch_for_name(safe_name)
    _validate_branch(selected_branch)
    if selected_branch in PROTECTED_WORKTREE_BRANCHES:
        raise ValueError(f"protected worktree branch is not allowed: {selected_branch}")
    if not selected_branch.startswith("feat/"):
        raise ValueError(f"worktree branch must start with feat/: {selected_branch}")
    return WorktreePlan(
        name=safe_name,
        branch=selected_branch,
        path=_managed_checkout_path(root=root, name=safe_name),
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
        # 재사용 경로도 생성 경로와 똑같은 증명을 통과해야 한다. `.git`이 있다는
        # 사실만으로는 이 저장소의 worktree라는 근거가 되지 않는다.
        verify_linked_worktree(root=root, path=plan.path)
        existing = get_worktree_status(root=root, name=plan.name)
        if plan.branch_explicit and existing.branch != plan.branch:
            raise ValueError(
                f"worktree {plan.name} already uses branch {existing.branch}; "
                f"requested {plan.branch}"
            )
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
        except Exception as cleanup_exc:
            # 롤백까지 실패하면 등록된 worktree가 manifest 없이 남는다. 원본
            # 예외를 가리지 않도록 경고로 표면화한다.
            print(
                f"warning: could not roll back worktree {status.name} at {status.path} "
                f"after the manifest write failed: {cleanup_exc}",
                file=sys.stderr,
            )
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
    if live:
        # manifest 없이 손으로 만든 checkout도 지울 수 있어야 하지만 소유 증명이 먼저다.
        # git 등록과 leader와 동일한 common dir을 요구한다 — 남의 저장소 worktree를
        # 이 경로에 심어 두는 것만으로 삭제되면 안 된다.
        try:
            verify_linked_worktree(root=root, path=status.path)
        except WorktreeIsolationError as exc:
            raise WorktreeIsolationError(
                f"refusing to remove {status.path}: not proven to be a worktree of this repository ({exc})"
            ) from exc
        if require_merged and not allow_unmerged:
            # Destructive: prove the work is already in the leader before removing.
            assert_worktree_mergeable(root=root, path=status.path)
    branch_to_delete = _owned_branch_for_live_worktree(root=root, status=status) if delete_branch else None
    if status.path.exists():
        force_args = ("--force",) if allow_unmerged else ()
        _run_git(root, "worktree", "remove", *force_args, str(status.path))
    if branch_to_delete is not None:
        _run_git(root, "branch", "-D", branch_to_delete)
    remove_worktree_metadata(root=root, name=status.name)


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
    resolved = resolve_worktree_name(root=root, name=name)
    path = _managed_checkout_path(root=root, name=resolved)
    default_branch = _default_branch_for_name(resolved)
    _validate_branch(default_branch)
    manifest = _runtime_state_root(root=root, name=resolved) / "manifest.json"
    legacy_manifest = path / "manifest.json"
    if not manifest.exists() and legacy_manifest.exists():
        manifest = legacy_manifest
    if manifest.exists():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return WorktreeStatus(
                name=resolved,
                branch=default_branch,
                path=path,
                exists=path.exists(),
                branch_created_by_agent_flow=False,
            )

        manifest_branch = payload.get("branch", default_branch)
        status_branch = default_branch
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
            and status_branch == default_branch
        )
        return WorktreeStatus(
            name=resolved,
            branch=status_branch,
            path=path,
            exists=path.exists(),
            branch_created_by_agent_flow=branch_created,
        )
    return WorktreeStatus(
        name=resolved,
        branch=_live_branch(root=root, path=path) or default_branch,
        path=path,
        exists=path.exists(),
        branch_created_by_agent_flow=False,
    )


def write_worktree_manifest(*, root: Path, status: WorktreeStatus) -> Path:
    path = _runtime_state_root(root=root, name=status.name) / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(status)
    payload["path"] = str(status.path.relative_to(root))
    payload["leader_root"] = str(root)
    path.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
    return path


def worktree_runtime_root(*, root: Path, name: str) -> Path:
    return _runtime_state_root(root=root, name=resolve_worktree_name(root=root, name=name))


def resolve_worktree_name(*, root: Path, name: str) -> str:
    """조회용 이름 해석. 실제로 존재하는 이름이 정규화보다 우선한다.

    워크플로가 `git worktree add`로 직접 만든 checkout은 agent-flow의 정규화를
    거치지 않는다. 조회까지 정규화를 강제하면 디스크에 멀쩡히 있는 그 checkout을
    영영 못 찾는다(issue #110). 정규화 강제는 생성 경로에만 남긴다.
    """
    candidates = frozenset(known_worktree_names(root=root))
    if name in candidates:
        return name
    if name.startswith("feat/"):
        dashed = f"feat-{name[len('feat/'):]}"
        if dashed in candidates:
            return dashed
    return _feature_worktree_name(name)


def known_worktree_names(*, root: Path) -> list[str]:
    names: set[str] = set()
    checkout_root = root / ".agent-flow" / "worktrees"
    if checkout_root.exists():
        names.update(path.name for path in checkout_root.iterdir() if path.is_dir())
    runtime_root = _agent_flow_git_dir(root) / "worktrees"
    if runtime_root.exists():
        names.update(path.name for path in runtime_root.iterdir() if path.is_dir())
    names.update(_registered_managed_worktree_names(root))
    return sorted(names)


def remove_worktree_metadata(*, root: Path, name: str) -> None:
    # `name`은 이미 해석된 이름이다. 여기서 다시 해석하면 checkout을 지운 뒤
    # 호출될 때 후보 목록이 비어 정규화로 흘러 엉뚱한 디렉터리를 가리킨다.
    runtime_root = _runtime_state_root(root=root, name=name)
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
    legacy_manifest = _managed_checkout_path(root=root, name=name) / "manifest.json"
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


def _managed_checkout_path(*, root: Path, name: str) -> Path:
    return root / ".agent-flow" / "worktrees" / name


def _runtime_state_root(*, root: Path, name: str) -> Path:
    return _agent_flow_git_dir(root) / "worktrees" / name


def _agent_flow_git_dir(root: Path) -> Path:
    result = git_safe("rev-parse", "--git-common-dir", cwd=root)
    if not result.ok:
        # git 저장소인데 common dir을 못 읽으면 state root가 leader 안으로
        # 들어와 워커 상태가 leader에 쌓인다. 위치를 확정하지 못하면 멈춘다.
        # 애초에 git 저장소가 아니면 지킬 leader가 없으므로 root/.agent-flow가
        # 옳은 자리다 — 이 경로까지 막으면 non-git 프로젝트와 복구 명령이 죽는다.
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
    planned_branch = _default_branch_for_name(status.name)
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


def _default_branch_for_name(name: str) -> str:
    return f"feat/{name.removeprefix('feat-')}"


def _live_branch(*, root: Path, path: Path) -> str | None:
    """manifest 없는 checkout의 실제 브랜치. 이름에서 유추하지 않고 git에 묻는다."""
    if not (path / ".git").exists():
        return None
    result = git_safe("-C", str(path), "branch", "--show-current", cwd=root)
    if not result.ok:
        return None
    branch = result.stdout.strip()
    if not branch:
        return None
    try:
        _validate_branch(branch)
    except ValueError:
        return None
    return branch


def _registered_managed_worktree_names(root: Path) -> set[str]:
    """git이 등록한 checkout 중 managed root 바로 아래 있는 것들의 이름.

    디렉터리가 지워져도 등록은 남으므로 디스크 스캔만으로는 안 보인다. 이름을
    다시 `<managed>/<name>`으로 되돌릴 수 있는 자리만 후보로 받는다 — 그 밖의
    등록은 이름만으로 경로를 복원할 수 없어 remove가 엉뚱한 곳을 가리킨다.
    """
    managed_root = real_path(root / ".agent-flow" / "worktrees")
    leader = real_path(root)
    return {
        path.name
        for path in _registered_worktree_paths(root)
        if path != leader and path.parent == managed_root
    }


def _registered_worktree_paths(root: Path) -> set[Path]:
    result = git_safe("worktree", "list", "--porcelain", cwd=root)
    if not result.ok:
        return set()
    prefix = "worktree "
    return {
        real_path(line[len(prefix):].strip())
        for line in result.stdout.splitlines()
        if line.startswith(prefix)
    }


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
