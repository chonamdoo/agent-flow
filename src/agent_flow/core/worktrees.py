from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import shutil
import subprocess
import tempfile
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from agent_flow.artifact import ACTIVE_MARKER, find_active_runs, read_meta, write_meta
from agent_flow.core.commands import run_safe_command
from agent_flow.core.worktree_isolation import (
    RegisteredWorktree,
    FileLeaseUnavailable,
    WorktreeIsolationError,
    assert_worktree_mergeable,
    exclusive_file_lease,
    git_repo_state,
    git_safe,
    list_registered_worktrees,
    real_path,
    registered_worktree_at,
    same_worktree_path,
    shared_file_lease,
    verify_linked_worktree,
    with_git_lock_retry,
    worktree_creation_lock,
    worktree_path_key,
)


PROTECTED_WORKTREE_BRANCHES = {"main", "master", "develop"}
GIT_WORKTREE_TIMEOUT_S = 300
CLEANUP_JOURNAL_VERSION = 3
CLEANUP_STEPS = (
    "archive",
    "integration_proof",
    "checkout_removal",
    "branch_ref_cas",
    "metadata_cleanup",
)


class CleanupBlockedError(WorktreeIsolationError):
    """Cleanup could not prove that destructive progress is safe."""

    def __init__(
        self,
        message: str,
        *,
        journal_path: Path | None = None,
        run_dir: Path | None = None,
    ) -> None:
        self.journal_path = journal_path
        self.run_dir = run_dir
        super().__init__(message)


@dataclass(frozen=True)
class CleanupTransactionResult:
    journal_path: Path
    run_dir: Path



class AmbiguousWorktreeSelector(ValueError):
    """선택자가 등록된 worktree 여러 개와 맞았다.

    퍼지 매칭으로 하나를 고르면 사용자가 지목하지 않은 체크아웃이 지워진다.
    후보를 그대로 노출하고 멈춘다.
    """

    def __init__(self, selector: str, candidates: tuple[RegisteredWorktree, ...]) -> None:
        self.selector = selector
        self.candidates = candidates
        rendered = ", ".join(f"{item.path} ({item.branch or 'detached'})" for item in candidates)
        super().__init__(
            f"worktree selector is ambiguous: {selector}; candidates: {rendered}; "
            f"re-run with the exact path or branch"
        )


class WorktreeLockedError(RuntimeError):
    """git이 잠근 worktree다.

    lock은 저장소 바깥에서 맺은 안전 계약이라 agent-flow의 dirty force 경로에
    접어 넣으면 안 된다. 사용자가 ``git worktree unlock``으로 직접 풀어야 한다.
    """


@dataclass(frozen=True)
class WorktreePlan:
    name: str
    branch: str
    path: Path
    base_ref: str
    branch_explicit: bool = False
    requested_name: str = ""


class WorktreeAlreadyExistsError(ValueError):
    pass


@dataclass(frozen=True)
class WorktreeStatus:
    name: str
    branch: str
    path: Path
    exists: bool
    branch_created_by_agent_flow: bool = False
    requested_name: str = ""
    base_ref: str = ""
    base_oid: str = ""
    registration_identity: str | None = None


def plan_worktree(
    *, root: Path, name: str, branch: str | None = None, unique: str | None = None
) -> WorktreePlan:
    # A per-worker unique token keeps two workers on the same task from
    # normalizing to one shared worktree, which would silently break isolation.
    base_name = name if unique is None else f"{name}-{unique}"
    safe_name = _feature_worktree_name(base_name)
    selected_branch = branch or _default_branch_for_name(safe_name)
    _assert_requestable_branch(selected_branch)
    return WorktreePlan(
        name=safe_name,
        branch=selected_branch,
        path=_managed_checkout_path(root=root, name=safe_name),
        # leader worktree가 feature branch여도 새 작업은 기본 브랜치 commit에서 시작한다.
        base_ref=_default_base_ref(root),
        branch_explicit=branch is not None,
        requested_name=name,
    )


def create_worktree(
    *,
    root: Path,
    plan: WorktreePlan,
    allow_dirty: bool = False,
    reuse_existing: bool = True,
) -> WorktreeStatus:
    with worktree_creation_lock(root):
        return _create_worktree(
            root=root,
            plan=plan,
            allow_dirty=allow_dirty,
            reuse_existing=reuse_existing,
        )


def _create_worktree(
    *,
    root: Path,
    plan: WorktreePlan,
    allow_dirty: bool = False,
    reuse_existing: bool = True,
) -> WorktreeStatus:
    if not allow_dirty and _git_dirty(root):
        raise RuntimeError("leader workspace is dirty; pass --allow-dirty to create a worktree anyway")
    if plan.path.exists():
        if not reuse_existing:
            raise WorktreeAlreadyExistsError(
                f"worktree already exists: {plan.path}"
            )
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
    registered = _registered_at_path(root=root, path=plan.path)
    if registered is None or registered.registration_identity is None:
        raise WorktreeIsolationError(
            f"cannot prove worktree registration identity after creation: {plan.path}"
        )
    base_oid = _merge_base_oid(root=plan.path, left="HEAD", right=plan.base_ref)
    status = WorktreeStatus(
        name=plan.name,
        branch=plan.branch,
        path=plan.path,
        exists=True,
        branch_created_by_agent_flow=branch_created,
        requested_name=plan.requested_name,
        base_ref=plan.base_ref,
        base_oid=base_oid,
        registration_identity=registered.registration_identity,
    )
    try:
        write_worktree_manifest(root=root, status=status)
    except Exception:
        try:
            # create_worktree still owns the shared repository lease here. Calling
            # public remove_worktree would try to upgrade that lease to exclusive
            # and fail, leaving an unpublished checkout behind.
            _remove_worktree_locked(
                root=root,
                status=status,
                delete_branch=True,
                require_merged=False,
                allow_unmerged=False,
            )
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


def attach_worktree(
    *,
    root: Path,
    selector: str,
    branch: str | None = None,
    allow_dirty: bool = False,
    expected_registration_identity: str | None = None,
) -> WorktreeStatus | None:
    """등록부가 아는 관리형 checkout에 그대로 붙는다. 붙을 대상이 없으면 ``None``.

    조회 계열(`status`/`continue`/`worktree remove`)은 이미 `resolve_worktree`로
    등록부를 본다. 진입 경로만 이름을 정규화해 경로를 유도하므로, 등록부가 보고한
    이름이 그 규칙과 다르면 같은 selector가 두 번째 checkout과 브랜치를 만들어
    낸다. 그 비대칭을 여기서 닫는다.

    ``None``은 "붙을 대상이 없으니 생성 경로로 가라"는 뜻이다. 반대로 붙을 대상이
    있는데 안전하지 않으면 ``None`` 대신 raise한다 — 사용자가 지목한 checkout을
    그대로 두고 조용히 다른 것을 만드는 것이 이 함수가 막으려는 사고다.
    """
    registered = resolve_worktree(root=root, selector=selector)
    if registered is None:
        return None
    if registered.registration_identity is None:
        raise WorktreeIsolationError(
            f"worktree registration identity is unavailable: {registered.path}"
        )
    if (
        expected_registration_identity is not None
        and registered.registration_identity != expected_registration_identity
    ):
        raise WorktreeIsolationError(
            f"worktree registration changed after reuse consent: {registered.path}"
        )
    if not allow_dirty and _git_dirty(root):
        # 생성 경로와 같은 계약이다. 재사용도 leader가 더러우면 멈춘다
        # (`create_worktree`가 관리형 checkout 재사용 앞에서 거는 것과 같은 문)
        raise RuntimeError(
            "leader workspace is dirty; pass --allow-dirty to use a worktree anyway"
        )
    if not _is_managed_child(root=root, path=registered.path):
        # 관리 루트 밖 checkout에 붙는 것은 지원 범위가 아니다. 그렇다고 생성
        # 경로로 흘려보내면 selector를 디렉터리 이름으로 뭉개 엉뚱한 checkout을
        # 만든다(절대경로 selector는 경로 전체가 이름이 된다).
        raise ValueError(
            f"worktree {registered.path} is not a direct child of {_managed_root(root)}; "
            f"attaching to a checkout there is not supported"
        )
    if not (registered.path / ".git").is_file():
        # 등록은 남았는데 checkout이 사라졌다. 생성 경로가 prune 후 다시 만든다.
        return None
    if registered.branch is None:
        # detached HEAD면 등록부가 브랜치를 주지 못한다. 이름에서 유도한 브랜치로
        # 메우면 존재하지도 않는 브랜치로 commit/push 단계가 돈다.
        raise ValueError(
            f"worktree {registered.path.name} is on a detached HEAD; "
            f"check out a branch before starting a run there"
        )
    if registered.branch in PROTECTED_WORKTREE_BRANCHES:
        raise ValueError(f"protected worktree branch is not allowed: {registered.branch}")
    if branch is not None:
        # 요청된 브랜치는 생성 경로와 같은 규칙을 통과해야 한다. 붙는 대상의
        # 기존 브랜치가 아니라 사용자가 방금 넘긴 값에 대한 검사다.
        _assert_requestable_branch(branch)
        if branch != registered.branch:
            raise ValueError(
                f"worktree {registered.path.name} already uses branch {registered.branch}; "
                f"requested {branch}"
            )
    # 등록부를 읽은 시점과 실제로 쓰는 시점 사이를 좁힌다. 같은 저장소의 linked
    # worktree이고 여전히 그 브랜치 위에 있다는 것을 git으로 다시 증명한다.
    verify_linked_worktree(
        root=root,
        path=registered.path,
        expected_branch=registered.branch,
        managed_root=registered.path.parent,
    )
    status = _status_for_registered(
        root=root, registered=registered, requested=selector
    )
    runtime_root = _runtime_root_for_status(root=root, status=status)
    if runtime_root is not None and runtime_root.exists() and status.base_oid:
        return status

    # `git worktree add`로 먼저 만든 관리 루트의 checkout도 명시적 재사용 동의를
    # 받았으면 lifecycle metadata를 새로 기록해 채택할 수 있다. 브랜치 소유권은
    # 주장하지 않으므로 terminal cleanup은 checkout만 제거하고 branch는 보존한다.
    with worktree_creation_lock(root):
        current = _registered_at_path(root=root, path=registered.path)
        if not _same_registration(registered, current):
            raise ValueError(
                f"worktree registration changed while attaching {registered.path}; "
                "re-run the command"
            )
        status = _status_for_registered(
            root=root, registered=current, requested=selector
        )
        runtime_root = _runtime_root_for_status(root=root, status=status)
        if runtime_root is None:
            raise ValueError(
                f"worktree {registered.path.name} has conflicting agent-flow metadata; "
                "refusing attach"
            )
        if runtime_root.exists():
            if status.base_oid:
                return status
            raise ValueError(
                f"worktree {registered.path.name} has incomplete agent-flow metadata; "
                "refusing to overwrite it"
            )
        base_ref = _default_base_ref(root)
        status = WorktreeStatus(
            name=registered.path.name,
            branch=current.branch or "",
            path=current.path,
            exists=True,
            branch_created_by_agent_flow=False,
            requested_name=selector,
            base_ref=base_ref,
            base_oid=_merge_base_oid(
                root=current.path,
                left="HEAD",
                right=base_ref,
            ),
            registration_identity=current.registration_identity,
        )
        write_worktree_manifest(root=root, status=status)
        return status


def _is_managed_child(*, root: Path, path: Path) -> bool:
    parent_key = worktree_path_key(path.parent)
    return any(
        parent_key == worktree_path_key(root / marker / "worktrees")
        for marker in (".agent-flow", ".codex", ".Codex", ".omp")
    )


def _assert_requestable_branch(branch: str) -> None:
    _validate_branch(branch)
    if branch in PROTECTED_WORKTREE_BRANCHES:
        raise ValueError(f"protected worktree branch is not allowed: {branch}")
    if not branch.startswith("feat/"):
        raise ValueError(f"worktree branch must start with feat/: {branch}")


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

    # 생성 전체가 이미 `create_worktree`의 creation lock 안이다. 같은 파일을
    # 다시 flock하면 fd가 달라 같은 프로세스에서도 블로킹된다.
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
    """Remove one checkout only while repository-wide cleanup exclusion is held."""
    with _cleanup_lease(root, status.path):
        runtime_root = _runtime_root_for_status(root=root, status=status)
        with _run_start_exclusion(runtime_root):
            _assert_no_active_runs(runtime_root)
            _remove_worktree_locked(
                root=root,
                status=status,
                delete_branch=delete_branch,
                require_merged=require_merged,
                allow_unmerged=allow_unmerged,
            )


def _remove_worktree_locked(
    *,
    root: Path,
    status: WorktreeStatus,
    delete_branch: bool,
    require_merged: bool,
    allow_unmerged: bool,
) -> None:
    leader = leader_worktree_path(root)
    if leader is None:
        if git_repo_state(root) != "non-repo":
            raise WorktreeIsolationError(
                f"cannot resolve the leader checkout for {root}; refusing to remove {status.path}"
            )
    elif same_worktree_path(leader, status.path):
        raise WorktreeIsolationError(f"refusing to remove the leader checkout: {status.path}")

    registered = _registered_at_path(root=root, path=status.path)
    _assert_not_locked(path=status.path, entry=registered)
    live = status.path.exists() and (status.path / ".git").exists()
    if live and registered is None:
        raise WorktreeIsolationError(
            f"refusing to remove {status.path}: "
            f"not registered as a worktree of this repository"
        )
    if live and require_merged and not allow_unmerged:
        assert_worktree_mergeable(root=root, path=status.path)

    branch_delete = (
        _branch_delete_identity(root=root, status=status, registered=registered)
        if live and delete_branch
        else None
    )
    _assert_registration_and_ref_unchanged(
        root=root,
        status=status,
        expected_registration=registered,
        branch_delete=branch_delete,
    )

    if live:
        force_args = ("--force",) if allow_unmerged else ()
        _run_git(root, "worktree", "remove", *force_args, str(status.path))
    else:
        if registered is not None:
            if status.path.exists():
                raise WorktreeIsolationError(
                    f"stale worktree path is occupied at {status.path}; preserving it"
                )
            _run_git(root, "worktree", "remove", str(status.path))
            if _registered_at_path(root=root, path=status.path) is not None:
                raise WorktreeIsolationError(
                    f"stale worktree registration remains for {status.path}; preserving metadata"
                )

    if branch_delete is not None:
        branch, expected_oid = branch_delete
        _delete_branch_ref_cas(root=root, branch=branch, expected_oid=expected_oid)
    remove_worktree_metadata(root=root, name=status.name, path=status.path)
    if not live and status.path.is_dir() and _is_managed_child(root=root, path=status.path):
        try:
            status.path.rmdir()
        except OSError:
            pass


def _assert_registration_and_ref_unchanged(
    *,
    root: Path,
    status: WorktreeStatus,
    expected_registration: RegisteredWorktree | None,
    branch_delete: tuple[str, str] | None,
) -> None:
    current = _registered_at_path(root=root, path=status.path)
    _assert_not_locked(path=status.path, entry=current)
    if expected_registration is not None and not _same_registration(expected_registration, current):
        raise WorktreeIsolationError(
            f"worktree registration changed while removing {status.path}; re-run the command"
        )
    if expected_registration is None and current is not None:
        raise WorktreeIsolationError(
            f"worktree registration appeared while removing {status.path}; re-run the command"
        )
    if branch_delete is not None:
        branch, expected_oid = branch_delete
        current_oid = _ref_oid(root=root, ref=f"refs/heads/{branch}")
        if current_oid != expected_oid:
            raise WorktreeIsolationError(
                f"branch ref changed while removing {status.path}; "
                f"expected {expected_oid}, found {current_oid or 'missing'}"
            )



def run_worktree_cleanup_transaction(
    *,
    root: Path,
    checkout_path: Path,
    run_dir: Path,
    target_branch: str,
    integration_strategy: str,
    delete_branch: bool = True,
    after_step: Callable[[str], None] | None = None,
) -> CleanupTransactionResult:
    """Run or resume the ordered, durable retirement of a workflow checkout."""
    journal_path, journal = _prepare_or_load_cleanup_journal(
        root=root,
        checkout_path=checkout_path,
        run_dir=run_dir,
        target_branch=target_branch,
        integration_strategy=integration_strategy,
        delete_branch=delete_branch,
    )
    archived_run = Path(journal["run"]["archive_dir"])
    if journal.get("status") == "complete":
        return CleanupTransactionResult(journal_path=journal_path, run_dir=archived_run)

    runtime_root = Path(journal["run"]["source_state_root"])
    lock_root = runtime_root if runtime_root.exists() else Path(journal["run"]["archive_state_root"])
    with _cleanup_lease(root, checkout_path) as cleanup_lease:
        journal["leases"]["cleanup"] = {
            "path": str(cleanup_lease),
            "state": "held",
        }
        _write_cleanup_journal(journal_path, journal)
        with _run_start_exclusion(lock_root):
            _assert_cleanup_owner_active(journal_path=journal_path, journal=journal)
            for step in CLEANUP_STEPS:
                try:
                    if journal["steps"][step]["status"] == "done":
                        if step == "archive":
                            _assert_archived_run_identity(
                                archive=archived_run,
                                journal_path=journal_path,
                                journal=journal,
                            )
                        continue
                    if step == "archive":
                        _archive_cleanup_run(journal_path=journal_path, journal=journal)
                    elif step == "integration_proof":
                        _record_integration_proof(root=root, journal=journal)
                    elif step == "checkout_removal":
                        _remove_journal_checkout(
                            root=root,
                            journal_path=journal_path,
                            journal=journal,
                        )
                    elif step == "branch_ref_cas":
                        _delete_journal_branch(
                            root=root,
                            journal_path=journal_path,
                            journal=journal,
                        )
                    else:
                        _remove_journal_metadata(root=root, journal=journal)
                except CleanupBlockedError as exc:
                    _record_cleanup_failure(journal_path, journal, str(exc))
                    exc.journal_path = journal_path
                    exc.run_dir = (
                        archived_run if archived_run.exists() else Path(journal["run"]["source_dir"])
                    )
                    raise
                except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
                    message = f"cleanup step {step} failed: {exc}; preserving remaining state"
                    _record_cleanup_failure(journal_path, journal, message)
                    raise CleanupBlockedError(
                        message,
                        journal_path=journal_path,
                        run_dir=(
                            archived_run
                            if archived_run.exists()
                            else Path(journal["run"]["source_dir"])
                        ),
                    ) from exc
                journal["steps"][step] = {
                    "status": "done",
                    "completed_at": _utc_now(),
                }
                journal["step"] = step
                journal["last_error"] = None
                journal["updated_at"] = _utc_now()
                if step == CLEANUP_STEPS[-1]:
                    journal["status"] = "steps_complete"
                _write_cleanup_journal(journal_path, journal)
                if after_step is not None:
                    after_step(step)

    journal["leases"]["cleanup"]["state"] = "released"
    journal["updated_at"] = _utc_now()
    _write_cleanup_journal(journal_path, journal)
    return CleanupTransactionResult(journal_path=journal_path, run_dir=archived_run)


def complete_worktree_cleanup(result: CleanupTransactionResult) -> Path:
    """Publish terminal completion in a crash-resumable order."""
    journal = _load_cleanup_journal(result.journal_path)
    if (
        journal.get("status") not in {"steps_complete", "complete"}
        or journal.get("integration", {}).get("proof") != "verified"
        or any(
            journal["steps"][step]["status"] != "done"
            for step in CLEANUP_STEPS
        )
    ):
        raise CleanupBlockedError(
            "cleanup transaction is incomplete; active run remains retryable",
            journal_path=result.journal_path,
            run_dir=result.run_dir,
        )
    if not result.run_dir.is_dir():
        raise CleanupBlockedError(
            f"archived run is missing at {result.run_dir}; refusing terminal completion",
            journal_path=result.journal_path,
            run_dir=result.run_dir,
        )
    _assert_archived_run_identity(
        archive=result.run_dir,
        journal_path=result.journal_path,
        journal=journal,
    )
    meta = read_meta(result.run_dir)
    if (
        meta.get("run_id") != journal["run_id"]
        or meta.get("checkout_identity") != journal["checkout"]["identity"]
    ):
        raise CleanupBlockedError(
            "archived run identity does not match cleanup owner; refusing terminal completion",
            journal_path=result.journal_path,
            run_dir=result.run_dir,
        )
    if journal["status"] != "complete":
        journal["status"] = "complete"
        journal["terminal_completed_at"] = _utc_now()
        journal["updated_at"] = journal["terminal_completed_at"]
        journal["leases"]["cleanup"]["state"] = "released"
        _write_cleanup_journal(result.journal_path, journal)
    meta["cleanup_state"] = "complete"
    meta["cleanup_journal"] = str(result.journal_path)
    write_meta(result.run_dir, meta)
    marker = result.run_dir / ACTIVE_MARKER
    if marker.exists():
        marker.unlink()
    return result.run_dir

def _cleanup_terminal_publication_complete(
    *, run_dir: Path, journal_path: Path
) -> bool:
    if not run_dir.is_dir():
        return False
    try:
        meta = read_meta(run_dir)
    except (OSError, ValueError):
        return False
    return (
        meta.get("cleanup_state") == "complete"
        and meta.get("cleanup_journal") == str(journal_path)
        and not (run_dir / ACTIVE_MARKER).exists()
    )



def find_pending_worktree_cleanup(
    *, root: Path, selector: str
) -> CleanupTransactionResult | None:
    pending_root = _cleanup_pending_root(root)
    if not pending_root.is_dir():
        return None
    matches: list[CleanupTransactionResult] = []
    try:
        candidates = tuple(pending_root.glob("*.json"))
    except OSError as exc:
        raise CleanupBlockedError(
            f"cannot inspect pending cleanup journals at {pending_root}"
        ) from exc
    for path in candidates:
        journal = _load_cleanup_journal(path)
        checkout = journal["checkout"]
        if selector not in {
            checkout["name"],
            checkout["branch"],
            checkout["identity"],
            checkout["path"],
        }:
            continue
        archive = Path(journal["run"]["archive_dir"])
        source = Path(journal["run"]["source_dir"])
        run_dir = archive if archive.exists() else source
        if (
            journal.get("status") == "complete"
            and _cleanup_terminal_publication_complete(
                run_dir=run_dir,
                journal_path=path,
            )
        ):
            continue
        matches.append(
            CleanupTransactionResult(
                journal_path=path,
                run_dir=run_dir,
            )
        )
    if len(matches) > 1:
        raise CleanupBlockedError(
            f"multiple cleanup journals match {selector!r}; refusing to choose one"
        )
    return matches[0] if matches else None


def cleanup_state_root(result: CleanupTransactionResult) -> Path:
    run_dir = result.run_dir
    if run_dir.parent.name != "runs" or run_dir.parent.parent.name != ".agent-flow":
        raise CleanupBlockedError(f"invalid archived run path: {run_dir}")
    return run_dir.parent.parent.parent


def _prepare_or_load_cleanup_journal(
    *,
    root: Path,
    checkout_path: Path,
    run_dir: Path,
    target_branch: str,
    integration_strategy: str,
    delete_branch: bool,
) -> tuple[Path, dict[str, Any]]:
    meta = read_meta(run_dir)
    pointer = meta.get("cleanup_journal")
    if isinstance(pointer, str) and pointer:
        journal_path = Path(pointer)
        journal = _load_cleanup_journal(journal_path)
        _validate_cleanup_resume(
            root=root,
            run_dir=run_dir,
            target_branch=target_branch,
            journal_path=journal_path,
            journal=journal,
            checkout_path=checkout_path,
        )
        return journal_path, journal

    registered = _registered_at_path(root=root, path=checkout_path)
    if (
        registered is None
        or registered.branch is None
        or registered.head is None
        or registered.registration_identity is None
    ):
        raise CleanupBlockedError(
            f"checkout registration identity is unknown for {checkout_path}; preserving it"
        )
    status = _status_for_registered(
        root=root, registered=registered, requested=str(checkout_path)
    )
    checkout_identity = f"worktree:{status.name}"
    if meta.get("checkout_identity") != checkout_identity:
        raise CleanupBlockedError(
            f"run checkout identity is unknown or mismatched for {checkout_path}; preserving it"
        )
    if (
        meta.get("checkout_registration_identity")
        != registered.registration_identity
    ):
        raise CleanupBlockedError(
            f"run checkout registration changed for {checkout_path}; preserving it"
        )
    if meta.get("run_id") != run_dir.name:
        raise CleanupBlockedError(
            f"run identity is unknown or mismatched at {run_dir}; preserving checkout"
        )
    if not status.base_oid or not _is_oid(status.base_oid):
        raise CleanupBlockedError(
            f"recorded base OID is missing for {checkout_path}; preserving it"
        )
    _validate_branch(target_branch)
    target_ref = f"refs/heads/{target_branch}"
    if target_branch == status.branch:
        raise CleanupBlockedError("cleanup target branch cannot be the worktree branch")
    target_oid = _ref_oid(root=root, ref=target_ref)
    branch_oid = _ref_oid(root=root, ref=f"refs/heads/{status.branch}")
    if target_oid is None or branch_oid is None:
        raise CleanupBlockedError(
            "target or worktree branch OID is unknown; preserving checkout"
        )
    if registered.head != branch_oid:
        raise CleanupBlockedError(
            "worktree registration and branch ref disagree; preserving checkout"
        )
    runtime_root = _runtime_root_for_status(root=root, status=status)
    if runtime_root is None:
        raise CleanupBlockedError(
            f"runtime metadata does not belong to {checkout_path}; preserving checkout"
        )
    expected_run_parent = runtime_root / ".agent-flow" / "runs"
    if run_dir.parent != expected_run_parent:
        raise CleanupBlockedError(
            f"run directory is outside checkout runtime state: {run_dir}"
        )

    digest = hashlib.sha256(
        f"{worktree_path_key(_git_common_dir(root))}\0{worktree_path_key(status.path)}".encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    archive_state = (
        _agent_flow_state_dir(root)
        / "archive"
        / "worktrees"
        / f"{_safe_component(status.name)}-{digest}"
    )
    archive_run = archive_state / ".agent-flow" / "runs" / run_dir.name
    journal_path = _cleanup_pending_root(root) / f"{digest}-{run_dir.name}.json"
    now = _utc_now()
    journal: dict[str, Any] = {
        "version": CLEANUP_JOURNAL_VERSION,
        "status": "cleanup_pending",
        "step": "prepared",
        "run_id": run_dir.name,
        "repository": _cleanup_repository_identity(root),
        "checkout": {
            "identity": checkout_identity,
            "name": status.name,
            "path": str(real_path(status.path)),
            "registration_identity": registered.registration_identity,
            "branch": status.branch,
            "expected_base_ref": status.base_ref,
            "expected_base_oid": status.base_oid,
            "expected_head_oid": branch_oid,
            "branch_owned": status.branch_created_by_agent_flow,
            "delete_branch": delete_branch,
        },
        "target": {
            "ref": target_ref,
            "expected_oid": target_oid,
        },
        "integration": {
            "strategy": integration_strategy,
            "proof": "pending",
            "method": None,
        },
        "leases": {
            "cleanup": {
                "path": str(
                    _agent_flow_state_dir(root)
                    / "cleanup-leases"
                    / "repository.lock"
                ),
                "state": "pending",
            },
            "provider": {"state": "inactive"},
        },
        "run": {
            "source_dir": str(run_dir),
            "source_state_root": str(runtime_root),
            "archive_dir": str(archive_run),
            "archive_state_root": str(archive_state),
            "archive_digest": None,
        },
        "steps": {
            step: {"status": "pending", "completed_at": None}
            for step in CLEANUP_STEPS
        },
        "created_at": now,
        "updated_at": now,
        "last_error": None,
        "recovery": "agent-flow continue --worktree " + status.name,
    }
    _write_cleanup_journal(journal_path, journal)
    _bind_run_to_cleanup(
        run_dir=run_dir,
        journal_path=journal_path,
        checkout_identity=checkout_identity,
        checkout_registration_identity=registered.registration_identity,
    )
    return journal_path, journal


def _validate_cleanup_resume(
    *,
    root: Path,
    checkout_path: Path,
    run_dir: Path,
    target_branch: str,
    journal_path: Path,
    journal: dict[str, Any],
) -> None:
    expected_root = _cleanup_pending_root(root)
    if real_path(journal_path.parent) != real_path(expected_root):
        raise CleanupBlockedError("cleanup journal escapes the repository state root")
    if journal["repository"] != _cleanup_repository_identity(root):
        raise CleanupBlockedError("cleanup journal repository identity changed")
    if journal["target"]["ref"] != f"refs/heads/{target_branch}":
        raise CleanupBlockedError("cleanup target branch changed since journal preparation")
    if real_path(Path(journal["checkout"]["path"])) != real_path(checkout_path):
        raise CleanupBlockedError("cleanup checkout path changed since journal preparation")
    allowed = {
        Path(journal["run"]["source_dir"]),
        Path(journal["run"]["archive_dir"]),
    }
    if run_dir not in allowed:
        raise CleanupBlockedError("cleanup resume run directory does not match journal owner")
    _bind_run_to_cleanup(
        run_dir=run_dir,
        journal_path=journal_path,
        checkout_identity=journal["checkout"]["identity"],
        checkout_registration_identity=journal["checkout"][
            "registration_identity"
        ],
    )


def _bind_run_to_cleanup(
    *,
    run_dir: Path,
    journal_path: Path,
    checkout_identity: str,
    checkout_registration_identity: str,
) -> None:
    meta = read_meta(run_dir)
    if (
        meta.get("run_id") != run_dir.name
        or meta.get("checkout_identity") != checkout_identity
        or meta.get("checkout_registration_identity")
        != checkout_registration_identity
    ):
        raise CleanupBlockedError(
            f"run identity is unknown or mismatched at {run_dir}; preserving checkout"
        )
    meta["cleanup_state"] = "cleanup_pending"
    meta["cleanup_journal"] = str(journal_path)
    write_meta(run_dir, meta)


def _archive_historical_runs(
    *, journal_path: Path, journal: dict[str, Any]
) -> None:
    source_runs = Path(journal["run"]["source_state_root"]) / ".agent-flow" / "runs"
    archive_runs = Path(journal["run"]["archive_state_root"]) / ".agent-flow" / "runs"
    owner = Path(journal["run"]["source_dir"])
    try:
        candidates = sorted(source_runs.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise CleanupBlockedError(
            f"cannot enumerate checkout run history: {source_runs}"
        ) from exc
    records = journal["run"].setdefault("historical_archives", {})
    if not isinstance(records, dict):
        raise CleanupBlockedError("cleanup historical archive records are malformed")
    for source in candidates:
        if source == owner:
            continue
        try:
            identity = source.lstat()
        except OSError as exc:
            raise CleanupBlockedError(
                f"cannot inspect historical run: {source}"
            ) from exc
        if source.name == "active.lock" and stat.S_ISREG(identity.st_mode):
            continue
        if not stat.S_ISDIR(identity.st_mode) or stat.S_ISLNK(identity.st_mode):
            raise CleanupBlockedError(
                f"historical run is not a real directory: {source}"
            )
        digest = _run_tree_digest(source, exclude_lifecycle=False)
        record = records.get(source.name)
        destination = archive_runs / source.name
        expected_record = {
            "source": str(source),
            "archive": str(destination),
            "digest": digest,
        }
        if record is None:
            records[source.name] = expected_record
            journal["updated_at"] = _utc_now()
            _write_cleanup_journal(journal_path, journal)
        elif record != expected_record:
            raise CleanupBlockedError(
                f"historical run changed after cleanup preparation: {source}"
            )
        if destination.exists():
            if _run_tree_digest(destination, exclude_lifecycle=False) != digest:
                raise CleanupBlockedError(
                    f"historical run archive checksum mismatch: {destination}"
                )
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.name}.tmp-{os.getpid()}"
        )
        if temporary.exists():
            shutil.rmtree(temporary)
        try:
            shutil.copytree(source, temporary, symlinks=True)
            if _run_tree_digest(source, exclude_lifecycle=False) != digest:
                raise CleanupBlockedError(
                    f"historical run changed during archive copy: {source}"
                )
            if _run_tree_digest(temporary, exclude_lifecycle=False) != digest:
                raise CleanupBlockedError(
                    f"historical run archive copy checksum mismatch: {temporary}"
                )
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def _archive_cleanup_run(
    *, journal_path: Path, journal: dict[str, Any]
) -> None:
    _archive_historical_runs(journal_path=journal_path, journal=journal)
    source = Path(journal["run"]["source_dir"])
    archive = Path(journal["run"]["archive_dir"])
    expected_digest = journal["run"].get("archive_digest")
    if not isinstance(expected_digest, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_digest
    ):
        if archive.exists():
            raise CleanupBlockedError(
                f"archive checksum is missing for existing archive: {archive}"
            )
        if not source.is_dir():
            raise CleanupBlockedError(
                f"source run disappeared before archive: {source}"
            )
        expected_digest = _run_tree_digest(source)
        journal["run"]["archive_digest"] = expected_digest
        journal["updated_at"] = _utc_now()
        _write_cleanup_journal(journal_path, journal)
    if archive.exists():
        _assert_archived_run_identity(
            archive=archive, journal_path=journal_path, journal=journal
        )
        if source.is_dir() and _run_tree_digest(source) != expected_digest:
            raise CleanupBlockedError(
                f"source run changed after archive snapshot: {source}"
            )
        return
    if not source.is_dir():
        raise CleanupBlockedError(
            f"source run disappeared before archive: {source}"
        )
    if _run_tree_digest(source) != expected_digest:
        raise CleanupBlockedError(
            f"source run changed before archive copy: {source}"
        )
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_name(f".{archive.name}.tmp-{os.getpid()}")
    if temporary.exists():
        shutil.rmtree(temporary)
    try:
        shutil.copytree(source, temporary, symlinks=True)
        if _run_tree_digest(source) != expected_digest:
            raise CleanupBlockedError(
                f"source run changed during archive copy: {source}"
            )
        if _run_tree_digest(temporary) != expected_digest:
            raise CleanupBlockedError(
                f"archive copy checksum mismatch: {temporary}"
            )
        os.replace(temporary, archive)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    _assert_archived_run_identity(
        archive=archive, journal_path=journal_path, journal=journal
    )


def _assert_archived_run_identity(
    *, archive: Path, journal_path: Path, journal: dict[str, Any]
) -> None:
    meta = read_meta(archive)
    if (
        meta.get("run_id") != journal["run_id"]
        or meta.get("checkout_identity") != journal["checkout"]["identity"]
        or meta.get("cleanup_journal") != str(journal_path)
    ):
        raise CleanupBlockedError(
            f"archive identity is unknown or mismatched at {archive}"
        )
    expected_digest = journal["run"].get("archive_digest")
    if (
        not isinstance(expected_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
        or _run_tree_digest(archive) != expected_digest
    ):
        raise CleanupBlockedError(
            f"archive checksum is missing or mismatched at {archive}"
        )


def _run_tree_digest(root: Path, *, exclude_lifecycle: bool = True) -> str:
    """Hash immutable run payload while excluding lifecycle publication files."""
    try:
        root_identity = root.lstat()
    except OSError as exc:
        raise CleanupBlockedError(f"cannot inspect run archive: {root}") from exc
    if not stat.S_ISDIR(root_identity.st_mode) or stat.S_ISLNK(root_identity.st_mode):
        raise CleanupBlockedError(f"run archive is not a real directory: {root}")
    digest = hashlib.sha256(b"agent-flow-run-archive-v1\0")
    try:
        paths = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    except OSError as exc:
        raise CleanupBlockedError(f"cannot enumerate run archive: {root}") from exc
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if exclude_lifecycle and relative in {"active", "meta.json"}:
            continue
        try:
            identity = path.lstat()
        except OSError as exc:
            raise CleanupBlockedError(
                f"run archive entry changed during checksum: {path}"
            ) from exc
        if stat.S_ISDIR(identity.st_mode):
            kind = "directory"
            size = 0
        elif stat.S_ISREG(identity.st_mode):
            kind = "file"
            size = identity.st_size
        else:
            raise CleanupBlockedError(
                f"run archive contains a non-regular entry: {path}"
            )
        header = json.dumps(
            [kind, relative, stat.S_IMODE(identity.st_mode), size],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(header).to_bytes(8, "big"))
        digest.update(header)
        if kind == "file":
            _digest_run_file(path=path, expected=identity, digest=digest)
    return digest.hexdigest()


def _digest_run_file(
    *, path: Path, expected: os.stat_result, digest: Any
) -> None:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise CleanupBlockedError(
            "run archive checksum requires no-follow file opening"
        )
    try:
        fd = os.open(path, os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise CleanupBlockedError(
            f"cannot open run archive entry safely: {path}"
        ) from exc
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino, opened.st_size)
            != (expected.st_dev, expected.st_ino, expected.st_size)
        ):
            raise CleanupBlockedError(
                f"run archive entry identity changed during checksum: {path}"
            )
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
        if (
            after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
        ):
            raise CleanupBlockedError(
                f"run archive entry changed during checksum: {path}"
            )
    finally:
        os.close(fd)


def _assert_cleanup_owner_active(
    *, journal_path: Path, journal: dict[str, Any]
) -> None:
    source_state = Path(journal["run"]["source_state_root"])
    source_run = Path(journal["run"]["source_dir"])
    archive_state = Path(journal["run"]["archive_state_root"])
    archive_run = Path(journal["run"]["archive_dir"])
    if source_state.exists():
        state_root, owner_run = source_state, source_run
    else:
        state_root, owner_run = archive_state, archive_run
    try:
        active = find_active_runs(state_root)
    except Exception as exc:
        raise CleanupBlockedError(
            f"active run state is unknown at {state_root}; preserving checkout"
        ) from exc
    if len(active) != 1 or active[0].path != owner_run:
        raise CleanupBlockedError(
            "cleanup owner is not the only exact active run; preserving checkout"
        )
    meta = read_meta(owner_run)
    if (
        meta.get("run_id") != journal["run_id"]
        or meta.get("checkout_identity") != journal["checkout"]["identity"]
        or meta.get("cleanup_state") != "cleanup_pending"
        or meta.get("cleanup_journal") != str(journal_path)
    ):
        raise CleanupBlockedError(
            "active run owner identity or cleanup phase is unknown; preserving checkout"
        )


def _record_integration_proof(*, root: Path, journal: dict[str, Any]) -> None:
    target_oid = _refresh_integration_target(root=root, journal=journal)
    _validate_cleanup_snapshot(root=root, journal=journal, require_clean=True)
    _prove_recorded_integration(
        root=root,
        journal=journal,
        target_oid=target_oid,
    )


def _refresh_integration_target(*, root: Path, journal: dict[str, Any]) -> str:
    target_ref = journal["target"]["ref"]
    current = _ref_oid(root=root, ref=target_ref)
    if current is None:
        raise CleanupBlockedError(
            "integration target disappeared; preserving checkout and branch"
        )
    if current != journal["target"]["expected_oid"]:
        journal["target"]["expected_oid"] = current
        journal["integration"]["proof"] = "pending"
        journal["integration"]["method"] = None
        journal["integration"].pop("verified_at", None)
        journal["integration"]["target_refreshed_at"] = _utc_now()
    return current


def _prove_recorded_integration(
    *, root: Path, journal: dict[str, Any], target_oid: str
) -> None:
    base_oid = journal["checkout"]["expected_base_oid"]
    head_oid = journal["checkout"]["expected_head_oid"]
    for oid in (base_oid, head_oid, target_oid):
        _assert_commit_oid(root=root, oid=oid)
    if not _is_ancestor(root=root, ancestor=base_oid, descendant=head_oid):
        raise CleanupBlockedError(
            "recorded base is not an ancestor of recorded head; proof is unknown"
        )
    if _is_ancestor(root=root, ancestor=head_oid, descendant=target_oid):
        method = "head-ancestor-of-target"
    elif _merge_tree_is_noop(root=root, head_oid=head_oid, target_oid=target_oid):
        method = "merge-tree-noop"
    else:
        raise CleanupBlockedError(
            "cannot prove recorded head is integrated into recorded target; "
            "preserving checkout and branch"
        )
    journal["integration"]["proof"] = "verified"
    journal["integration"]["method"] = method
    journal["integration"]["verified_at"] = _utc_now()


def _validate_cleanup_snapshot(
    *, root: Path, journal: dict[str, Any], require_clean: bool
) -> None:
    checkout = journal["checkout"]
    path = Path(checkout["path"])
    registered = _registered_at_path(root=root, path=path)
    if (
        registered is None
        or registered.branch != checkout["branch"]
        or registered.head != checkout["expected_head_oid"]
        or registered.registration_identity
        != checkout["registration_identity"]
    ):
        raise CleanupBlockedError(
            "worktree registration changed since cleanup preparation; preserving checkout"
        )
    _assert_not_locked(path=path, entry=registered)
    branch_oid = _ref_oid(root=root, ref=f"refs/heads/{checkout['branch']}")
    if branch_oid != checkout["expected_head_oid"]:
        raise CleanupBlockedError(
            "worktree branch ref changed since cleanup preparation; preserving checkout"
        )
    target_oid = _ref_oid(root=root, ref=journal["target"]["ref"])
    if target_oid != journal["target"]["expected_oid"]:
        raise CleanupBlockedError(
            "integration target drifted since cleanup preparation; preserving checkout"
        )
    for oid in (
        checkout["expected_base_oid"],
        checkout["expected_head_oid"],
        target_oid,
    ):
        _assert_commit_oid(root=root, oid=oid)
    if require_clean:
        status = git_safe(
            "-C",
            str(path),
            "status",
            "--porcelain",
            "--ignored=matching",
            cwd=path,
            optional_locks=False,
        )
        if not status.ok:
            raise CleanupBlockedError(
                f"cannot inspect checkout status at {path}; preserving checkout"
            )
        if status.stdout.strip():
            raise CleanupBlockedError(
                f"checkout is dirty at {path}; preserving checkout"
            )


def _remove_journal_checkout(
    *, root: Path, journal_path: Path, journal: dict[str, Any]
) -> None:
    path = Path(journal["checkout"]["path"])
    registered = _registered_at_path(root=root, path=path)
    if not path.exists() and registered is None:
        return
    _assert_cleanup_owner_active(
        journal_path=journal_path,
        journal=journal,
    )
    _record_integration_proof(root=root, journal=journal)
    journal["updated_at"] = _utc_now()
    _write_cleanup_journal(journal_path, journal)
    _run_git(root, "worktree", "remove", str(path))
    if path.exists() or _registered_at_path(root=root, path=path) is not None:
        raise CleanupBlockedError(
            f"checkout removal did not retire registration and path: {path}"
        )


def _delete_journal_branch(
    *, root: Path, journal_path: Path, journal: dict[str, Any]
) -> None:
    checkout = journal["checkout"]
    if not checkout["delete_branch"] or not checkout["branch_owned"]:
        return
    branch = checkout["branch"]
    expected_oid = checkout["expected_head_oid"]
    current_oid = _ref_oid(root=root, ref=f"refs/heads/{branch}")
    if current_oid is None:
        return
    if current_oid != expected_oid:
        raise CleanupBlockedError(
            "worktree branch ref changed before CAS; preserving branch ref"
        )
    target_oid = _refresh_integration_target(root=root, journal=journal)
    _prove_recorded_integration(
        root=root,
        journal=journal,
        target_oid=target_oid,
    )
    journal["updated_at"] = _utc_now()
    _write_cleanup_journal(journal_path, journal)
    _delete_branch_ref_cas(
        root=root,
        branch=branch,
        expected_oid=expected_oid,
        target_ref=journal["target"]["ref"],
        target_oid=target_oid,
    )


def _remove_journal_metadata(*, root: Path, journal: dict[str, Any]) -> None:
    checkout = journal["checkout"]
    path = Path(checkout["path"])
    if path.exists() or _registered_at_path(root=root, path=path) is not None:
        raise CleanupBlockedError(
            "checkout still exists before metadata cleanup; preserving metadata"
        )
    remove_worktree_metadata(
        root=root,
        name=checkout["name"],
        path=path,
    )
    runtime_root = Path(journal["run"]["source_state_root"])
    if runtime_root.exists():
        raise CleanupBlockedError(
            f"checkout runtime metadata still exists at {runtime_root}"
        )


def _assert_commit_oid(*, root: Path, oid: str) -> None:
    if not _is_oid(oid):
        raise CleanupBlockedError(f"invalid recorded commit OID: {oid!r}")
    result = git_safe(
        "rev-parse",
        "--verify",
        "--quiet",
        f"{oid}^{{commit}}",
        cwd=root,
        optional_locks=False,
    )
    if not result.ok or result.stdout.strip() != oid:
        raise CleanupBlockedError(
            f"recorded commit OID cannot be resolved: {oid}"
        )


def _is_ancestor(*, root: Path, ancestor: str, descendant: str) -> bool:
    result = git_safe(
        "merge-base",
        "--is-ancestor",
        ancestor,
        descendant,
        cwd=root,
        optional_locks=False,
    )
    if result.ok:
        return True
    if result.returncode == 1 and not result.stderr.strip():
        return False
    raise CleanupBlockedError(
        f"git cannot prove ancestry from {ancestor} to {descendant}"
    )


def _merge_tree_is_noop(*, root: Path, head_oid: str, target_oid: str) -> bool:
    merged = git_safe(
        "merge-tree",
        "--write-tree",
        target_oid,
        head_oid,
        cwd=root,
        optional_locks=False,
    )
    if not merged.ok:
        return False
    merged_tree = merged.stdout.splitlines()[0].strip() if merged.stdout else ""
    target_tree = _tree_oid(root=root, oid=target_oid)
    return _is_oid(merged_tree) and merged_tree == target_tree


def _tree_oid(*, root: Path, oid: str) -> str:
    result = git_safe(
        "rev-parse",
        "--verify",
        f"{oid}^{{tree}}",
        cwd=root,
        optional_locks=False,
    )
    tree = result.stdout.strip() if result.ok else ""
    if not _is_oid(tree):
        raise CleanupBlockedError(f"cannot resolve tree for recorded OID {oid}")
    return tree


def _cleanup_pending_root(root: Path) -> Path:
    return _agent_flow_state_dir(root) / "cleanup-pending"


def _git_common_dir(root: Path) -> Path:
    return real_path(_agent_flow_git_dir(root).parent)


def _agent_flow_state_dir(root: Path) -> Path:
    return _git_common_dir(root) / "agent-flow"


def _cleanup_repository_identity(root: Path) -> dict[str, str | int]:
    common_dir = _git_common_dir(root)
    try:
        identity = common_dir.lstat()
    except OSError as exc:
        raise CleanupBlockedError(
            f"cleanup repository identity is unavailable: {common_dir}"
        ) from exc
    if not stat.S_ISDIR(identity.st_mode) or common_dir.is_symlink():
        raise CleanupBlockedError(
            f"cleanup repository identity is not a real directory: {common_dir}"
        )
    return {
        "common_dir": str(common_dir),
        "common_dir_device": identity.st_dev,
        "common_dir_inode": identity.st_ino,
        "leader_root": str(real_path(root)),
    }



def _valid_cleanup_repository_identity(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        isinstance(value.get("common_dir"), str)
        and bool(value["common_dir"])
        and isinstance(value.get("leader_root"), str)
        and bool(value["leader_root"])
        and isinstance(value.get("common_dir_device"), int)
        and not isinstance(value["common_dir_device"], bool)
        and value["common_dir_device"] >= 0
        and isinstance(value.get("common_dir_inode"), int)
        and not isinstance(value["common_dir_inode"], bool)
        and value["common_dir_inode"] > 0
    )


def _load_cleanup_journal(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CleanupBlockedError(
            f"cleanup journal is missing or unreadable at {path}"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != CLEANUP_JOURNAL_VERSION
        or payload.get("status")
        not in {"cleanup_pending", "steps_complete", "complete"}
        or not _valid_cleanup_repository_identity(payload.get("repository"))
        or not isinstance(payload.get("checkout"), dict)
        or not isinstance(
            payload["checkout"].get("registration_identity"), str
        )
        or not payload["checkout"]["registration_identity"]
        or not isinstance(payload.get("steps"), dict)
        or any(
            not isinstance(payload["steps"].get(step), dict)
            or payload["steps"][step].get("status") not in {"pending", "done"}
            for step in CLEANUP_STEPS
        )
    ):
        raise CleanupBlockedError(
            f"cleanup journal schema is unknown at {path}; preserving checkout"
        )
    return payload


def _write_cleanup_journal(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{json.dumps(payload, indent=2, sort_keys=True)}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _record_cleanup_failure(
    path: Path, journal: dict[str, Any], message: str
) -> None:
    journal["status"] = "cleanup_pending"
    journal["last_error"] = {
        "message": message,
        "at": _utc_now(),
    }
    journal["updated_at"] = journal["last_error"]["at"]
    _write_cleanup_journal(path, journal)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _same_registration(
    before: RegisteredWorktree, after: RegisteredWorktree | None
) -> bool:
    """같은 등록인가. 경로·branch·HEAD가 재사용돼도 checkout 인스턴스는 다르다."""
    if (
        after is None
        or before.registration_identity is None
        or after.registration_identity is None
    ):
        return False
    return (
        before.branch == after.branch
        and before.head == after.head
        and before.registration_identity == after.registration_identity
    )


def _registered_at_path(*, root: Path, path: Path) -> RegisteredWorktree | None:
    return registered_worktree_at(root, path)


def _assert_not_locked(*, path: Path, entry: RegisteredWorktree | None) -> None:
    if entry is None or not entry.locked:
        return
    raise WorktreeLockedError(
        f"worktree is locked by git: {path}; agent-flow will not override a git lock "
        f"(run `git worktree unlock {path}` first)"
    )


def assert_worktree_unlocked(*, root: Path, path: Path) -> None:
    """git이 잠근 경로면 하드 실패. 잔재 정리 경로도 이 잠금을 넘지 않는다."""
    _assert_not_locked(path=path, entry=_registered_at_path(root=root, path=path))


def _checkout_lease_path(root: Path, path: Path) -> Path:
    """One lease file per checkout. 저장소 전역 lease는 워커가 상시 도는
    상태에서 cleanup을 영원히 굶긴다. 정리 대상과 무관한 checkout의 활동은
    정리를 막을 이유가 없다."""
    digest = hashlib.sha256(
        worktree_path_key(real_path(path)).encode("utf-8")
    ).hexdigest()[:16]
    return _agent_flow_state_dir(root) / "cleanup-leases" / f"checkout-{digest}.lock"


@contextmanager
def worktree_run_activation(
    *,
    root: Path,
    path: Path,
    registration_identity: str | None,
) -> Iterator[None]:
    """Pin one checkout registration until its active run marker is durable."""
    if registration_identity is None:
        raise WorktreeIsolationError(
            f"worktree registration identity is unavailable: {path}"
        )
    lock_path = _checkout_lease_path(root, path)
    try:
        with shared_file_lease(lock_path):
            current = _registered_at_path(root=root, path=path)
            if (
                current is None
                or current.registration_identity != registration_identity
            ):
                raise WorktreeIsolationError(
                    f"worktree registration changed before run activation: {path}"
                )
            verify_linked_worktree(
                root=root,
                path=path,
                expected_branch=current.branch,
                managed_root=path.parent,
            )
            yield
    except FileLeaseUnavailable as exc:
        raise WorktreeIsolationError(
            f"repository cleanup is active or unsafe at {lock_path}; "
            "run activation is blocked"
        ) from exc


@contextmanager
def _cleanup_lease(root: Path, checkout_path: Path) -> Iterator[Path]:
    lock_path = _checkout_lease_path(root, checkout_path)
    try:
        with exclusive_file_lease(lock_path):
            yield lock_path
    except FileLeaseUnavailable as exc:
        raise CleanupBlockedError(
            f"this checkout is activating a run or already being retired "
            f"at {lock_path}; preserving checkout"
        ) from exc


@contextmanager
def _run_start_exclusion(runtime_root: Path | None) -> Iterator[None]:
    if runtime_root is None:
        yield
        return
    lock_path = runtime_root / ".agent-flow" / "runs" / "active.lock"
    try:
        with exclusive_file_lease(lock_path):
            yield
    except FileLeaseUnavailable as exc:
        raise CleanupBlockedError(
            f"run lifecycle is changing or unsafe at {runtime_root}; preserving checkout"
        ) from exc





def _runtime_root_for_status(*, root: Path, status: WorktreeStatus) -> Path | None:
    try:
        key = _runtime_state_key(root=root, name=status.name)
        runtime_root = _runtime_state_root(root=root, name=key)
        if runtime_root.exists() and not _metadata_belongs_to_path(
            root=root, key=key, path=status.path
        ):
            return None
        return runtime_root
    except (OSError, RuntimeError, ValueError) as exc:
        raise CleanupBlockedError(
            f"cannot resolve runtime state for {status.path}; preserving checkout"
        ) from exc


def _assert_no_active_runs(runtime_root: Path | None) -> None:
    if runtime_root is None:
        return
    try:
        active = find_active_runs(runtime_root)
    except Exception as exc:
        raise CleanupBlockedError(
            f"active run state is unknown at {runtime_root}; preserving checkout"
        ) from exc
    if active:
        run_ids = ", ".join(run.run_id for run in active)
        raise CleanupBlockedError(
            f"active run exists for checkout ({run_ids}); preserving checkout"
        )


def _branch_delete_identity(
    *,
    root: Path,
    status: WorktreeStatus,
    registered: RegisteredWorktree | None,
) -> tuple[str, str] | None:
    planned_branch = _planned_branch(status.name)
    if (
        planned_branch is None
        or not status.branch_created_by_agent_flow
        or status.branch != planned_branch
    ):
        return None
    expected_oid = _ref_oid(root=root, ref=f"refs/heads/{planned_branch}")
    if expected_oid is None:
        return None
    if registered is not None and (
        registered.branch != planned_branch or registered.head != expected_oid
    ):
        raise WorktreeIsolationError(
            f"worktree registration changed for owned branch at {status.path}; "
            "preserving checkout"
        )
    return planned_branch, expected_oid


def _delete_branch_ref_cas(
    *,
    root: Path,
    branch: str,
    expected_oid: str,
    target_ref: str | None = None,
    target_oid: str | None = None,
) -> None:
    if (target_ref is None) != (target_oid is None):
        raise ValueError("target ref and OID must be supplied together")
    ref = f"refs/heads/{branch}"

    def delete_once() -> None:
        current_oid = _ref_oid(root=root, ref=ref)
        if current_oid is None:
            return
        if current_oid != expected_oid:
            raise WorktreeIsolationError(
                f"branch ref changed before CAS delete: {ref}; preserving it"
            )
        commands: list[str] = []
        if target_ref is not None and target_oid is not None:
            current_target = _ref_oid(root=root, ref=target_ref)
            if current_target != target_oid:
                raise WorktreeIsolationError(
                    f"integration target changed before CAS delete: {target_ref}; "
                    "preserving branch ref"
                )
            commands.append(f"verify {target_ref} {target_oid}")
        commands.append(f"delete {ref} {expected_oid}")
        result = git_safe(
            "update-ref",
            "--stdin",
            cwd=root,
            timeout_s=GIT_WORKTREE_TIMEOUT_S,
            input_text="\n".join(commands) + "\n",
        )
        if result.ok:
            return
        current_oid = _ref_oid(root=root, ref=ref)
        if current_oid is None:
            return
        if current_oid != expected_oid:
            raise WorktreeIsolationError(
                f"branch ref changed during CAS delete: {ref}; preserving it"
            )
        if target_ref is not None and _ref_oid(root=root, ref=target_ref) != target_oid:
            raise WorktreeIsolationError(
                f"integration target changed during CAS delete: {target_ref}; "
                "preserving branch ref"
            )
        detail = result.stderr.strip() or result.error or f"git exited {result.returncode}"
        raise WorktreeIsolationError(
            f"branch ref CAS failed for {ref} at {expected_oid}: {detail}; "
            "the ref was preserved"
        )

    with_git_lock_retry(
        delete_once,
        is_retryable=lambda exc: (
            isinstance(exc, WorktreeIsolationError)
            and any(
                marker in str(exc).lower()
                for marker in (
                    "index.lock",
                    "config.lock",
                    "packed-refs.lock",
                    "unable to create",
                    "another git process seems to be running",
                    "file exists",
                )
            )
        ),
    )
    if _ref_oid(root=root, ref=ref) is not None:
        raise WorktreeIsolationError(
            f"branch ref still exists after CAS delete: {ref}; preserving it"
        )


def _ref_oid(*, root: Path, ref: str) -> str | None:
    result = git_safe(
        "rev-parse",
        "--verify",
        "--quiet",
        "--end-of-options",
        ref,
        cwd=root,
        optional_locks=False,
    )
    if result.ok:
        oid = result.stdout.strip()
        if _is_oid(oid):
            return oid
        raise WorktreeIsolationError(f"git returned an invalid object id for {ref}")
    if result.returncode == 1 and not result.stderr.strip():
        return None
    raise WorktreeIsolationError(
        f"cannot resolve {ref}; preserving checkout: "
        f"{result.stderr.strip() or result.stdout.strip() or 'git did not answer'}"
    )


def _merge_base_oid(*, root: Path, left: str, right: str) -> str:
    result = git_safe(
        "merge-base", left, right, cwd=root, optional_locks=False
    )
    oid = result.stdout.strip() if result.ok else ""
    if not _is_oid(oid):
        raise WorktreeIsolationError(
            f"cannot record worktree base OID for {left} and {right}"
        )
    return oid


def _is_oid(value: object) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", value)
    )


def worktree_branch_exists(*, root: Path, branch: str) -> bool:
    # 관측이다. ref 조회에 index.lock을 잡으면 동시에 도는 워커와 경합을 만든다.
    result = git_safe(
        "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", cwd=root, optional_locks=False
    )
    return result.ok


def _default_base_ref(root: Path) -> str:
    for ref in ("main", "origin/main", "master", "origin/master", "develop", "origin/develop"):
        if _git_commit_ref_exists(root=root, ref=ref):
            return ref
    return "HEAD"


def _git_commit_ref_exists(*, root: Path, ref: str) -> bool:
    result = git_safe(
        "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=root, optional_locks=False
    )
    # git을 호출할 수 없으면 기본 ref 후보가 없는 것으로 보고 HEAD fallback을 쓴다.
    return result.ok


def leader_worktree_path(root: Path) -> Path | None:
    """이 저장소의 main 체크아웃. 어떤 선택자로도 제거 대상이 되지 않는다.

    git이 대답하지 못하면 raise한다. None으로 강등하면 "leader가 여기 없다"와
    "leader가 어딘지 못 물어봤다"가 같은 값이 되고, leader 보호는 그 값 하나에
    걸려 있다. 지킬 leader가 실제로 없는 경우(비-git)만 None이다.

    porcelain 첫 항목을 main으로 보는 위치 기반 추정은 쓰지 않는다. 그건 git의
    관행이지 검증한 사실이 아니고, 순서가 흔들리면 leader를 놓치는 게 아니라
    **엉뚱한 worktree를 leader로 오인해 제거 불가로 만든다.**
    """
    result = git_safe("rev-parse", "--git-common-dir", cwd=root, optional_locks=False)
    if not result.ok or not result.stdout.strip():
        if git_repo_state(root) == "non-repo":
            return None
        raise WorktreeIsolationError(
            f"cannot resolve the leader checkout for {root}: "
            f"{result.stderr.strip() or 'git did not answer'}"
        )
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = root / common
    common = real_path(common)
    if common.name != ".git":
        # bare 저장소나 separate-git-dir 배치다. 부모를 leader로 단정할 수 없다.
        raise WorktreeIsolationError(
            f"cannot derive the leader checkout from git common dir {common}"
        )
    return common.parent


def removable_worktrees(*, root: Path) -> list[RegisteredWorktree]:
    """조회·제거 후보가 되는 등록 worktree. leader와 bare는 절대 포함하지 않는다."""
    try:
        entries = list_registered_worktrees(root)
        leader = leader_worktree_path(root)
    except WorktreeIsolationError:
        # 파일시스템이 "여긴 저장소가 아니다"를 증명했을 때만 빈 목록으로 접는다.
        # 판정 불가는 그대로 올린다 — 등록이 없다는 결론과 조회에 실패했다는
        # 사실이 같은 값이 되는 순간 제거가 근거 없이 통과한다.
        if git_repo_state(root) != "non-repo":
            raise
        return []
    excluded = set() if leader is None else {worktree_path_key(leader)}
    return [
        entry
        for entry in entries
        if not entry.bare and worktree_path_key(entry.path) not in excluded
    ]


def resolve_worktree(*, root: Path, selector: str) -> RegisteredWorktree | None:
    """Resolve one registered worktree without letting aliases beat exact IDs."""
    wanted = selector.strip()
    if not wanted:
        return None
    ranked: dict[int, dict[str, RegisteredWorktree]] = {}
    for entry in removable_worktrees(root=root):
        rank = _selector_match_rank(root=root, selector=wanted, entry=entry)
        if rank is not None:
            ranked.setdefault(rank, {})[worktree_path_key(entry.path)] = entry
    if not ranked:
        return None
    matched = ranked[min(ranked)]
    if len(matched) > 1:
        raise AmbiguousWorktreeSelector(
            wanted, tuple(sorted(matched.values(), key=lambda item: str(item.path)))
        )
    return next(iter(matched.values()))


def _selector_match_rank(
    *, root: Path, selector: str, entry: RegisteredWorktree
) -> int | None:
    if any(
        same_worktree_path(candidate, entry.path)
        for candidate in _selector_path_candidates(root=root, selector=selector)
    ):
        return 0
    if selector == entry.branch:
        return 1
    if selector == entry.path.name:
        return 2
    derived = _derived_worktree_identity(selector)
    if derived is not None and (
        derived[0] == entry.path.name or derived[1] == entry.branch
    ):
        return 3
    return None


def _derived_worktree_identity(selector: str) -> tuple[str, str] | None:
    """agent-flow 생성 규칙으로 유도한 (디렉터리 이름, 브랜치). 기존 이름 호환용이다."""
    try:
        name = _feature_worktree_name(selector)
    except ValueError:
        return None
    return name, f"feat/{name.removeprefix('feat-')}"


def _selector_path_candidates(*, root: Path, selector: str) -> tuple[Path, ...]:
    """선택자를 경로로 읽었을 때의 후보. 상대경로는 leader root와 cwd 양쪽에서 푼다."""
    raw = Path(selector)
    if raw.is_absolute():
        return (raw,)
    try:
        return (root / raw, Path.cwd() / raw)
    except OSError:
        return (root / raw,)


def get_worktree_status(*, root: Path, name: str) -> WorktreeStatus:
    registered = resolve_worktree(root=root, selector=name)
    if registered is not None:
        return _status_for_registered(root=root, registered=registered, requested=name)
    return _status_for_planned_name(root=root, name=name)


def _status_for_registered(
    *, root: Path, registered: RegisteredWorktree, requested: str
) -> WorktreeStatus:
    # 표시·조회 이름은 등록부가 보고한 디렉터리 이름 그대로다. 여기서 정규화하면
    # `feat-issue#110` 같은 합법적인 디렉터리가 `feat-issue-110`으로 뭉개져,
    # 목록이 보여 준 이름을 그대로 넣어도 다시 못 찾는 원래 버그로 되돌아간다.
    name = registered.path.name
    # manifest와 런타임 상태도 등록부의 exact 이름을 우선한다. 정규화된 형제가
    # 존재해도 이 checkout의 상태를 읽어야 resume와 terminal cleanup이 같은
    # 소유권 기록을 사용한다.
    planned_branch = _planned_branch(name)
    payload = _load_worktree_manifest(root=root, name=name)
    manifest_branch = _manifest_branch(payload)
    manifest_base_ref = _manifest_string(payload, "base_ref")
    manifest_base_oid = _manifest_oid(payload, "base_oid")
    # 소유권은 manifest가 계획 브랜치를 주장할 때만 인정한다. 등록부가 보고한 실제
    # 브랜치까지 같아야 위조된 manifest가 남의 브랜치를 지우지 못한다.
    owned = (
        payload is not None
        and payload.get("branch_created_by_agent_flow") is True
        and manifest_branch is not None
        and manifest_branch == planned_branch
        and registered.branch == planned_branch
    )
    return WorktreeStatus(
        name=name,
        branch=registered.branch or manifest_branch or planned_branch or "",
        path=registered.path,
        exists=registered.path.exists(),
        branch_created_by_agent_flow=owned,
        requested_name=requested,
        base_ref=manifest_base_ref or "",
        base_oid=manifest_base_oid or "",
        registration_identity=registered.registration_identity,
    )


def _status_for_planned_name(*, root: Path, name: str) -> WorktreeStatus:
    plan = plan_worktree(root=root, name=name)
    payload = _load_worktree_manifest(root=root, name=plan.name)
    manifest_branch = _manifest_branch(payload)
    manifest_base_ref = _manifest_string(payload, "base_ref")
    manifest_base_oid = _manifest_oid(payload, "base_oid")
    owned = (
        payload is not None
        and payload.get("branch_created_by_agent_flow") is True
        and manifest_branch == plan.branch
    )
    return WorktreeStatus(
        name=plan.name,
        branch=manifest_branch or plan.branch,
        path=plan.path,
        exists=plan.path.exists(),
        branch_created_by_agent_flow=owned,
        requested_name=name,
        base_ref=manifest_base_ref or plan.base_ref,
        base_oid=manifest_base_oid or "",
    )


def _planned_branch(name: str) -> str | None:
    """``name``에 대응하는 agent-flow 생성 브랜치. 규칙에 맞지 않으면 None."""
    try:
        safe = _feature_worktree_name(name)
    except ValueError:
        return None
    branch = f"feat/{safe.removeprefix('feat-')}"
    try:
        _validate_branch(branch)
    except ValueError:
        return None
    return None if branch in PROTECTED_WORKTREE_BRANCHES else branch


def _load_worktree_manifest(*, root: Path, name: str) -> dict | None:
    try:
        key = _runtime_state_key(root=root, name=name)
    except ValueError:
        return None
    return _state_key_manifest(root=root, key=key)


def _state_key_manifest(*, root: Path, key: str) -> dict | None:
    """정규화 없이 ``key`` 자리의 manifest만 읽는다."""
    manifest = _runtime_state_root(root=root, name=key) / "manifest.json"
    legacy = _managed_checkout_path(root=root, name=key) / "manifest.json"
    if not manifest.exists() and legacy.exists():
        manifest = legacy
    return _read_manifest(manifest)


def _read_manifest(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _manifest_branch(payload: dict | None) -> str | None:
    value = payload.get("branch") if payload else None
    if not isinstance(value, str):
        return None
    try:
        _validate_branch(value)
    except ValueError:
        return None
    return value


def _manifest_string(payload: dict | None, key: str) -> str | None:
    value = payload.get(key) if payload else None
    return value if isinstance(value, str) and value else None


def _manifest_oid(payload: dict | None, key: str) -> str | None:
    value = _manifest_string(payload, key)
    return value if value is not None and _is_oid(value) else None


def write_worktree_manifest(*, root: Path, status: WorktreeStatus) -> Path:
    """소비되는 키만 쓴다.

    `asdict(status)` 전체를 쓰면 읽는 쪽이 없는 필드가 함께 박제된다.
    `exists`는 쓰기 시점의 `path.exists()`라 읽는 시점에 의미가 없고,
    `registration_identity`는 등록부가 호출마다 다시 계산하는 값이라 사본이
    화석이 된다 — 소비자는 전부 `registered.registration_identity`만 쓴다.
    """
    path = _runtime_state_root(root=root, name=status.name) / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "path": str(status.path.relative_to(root)),
        "branch": status.branch,
        "base_ref": status.base_ref,
        "base_oid": status.base_oid,
        "branch_created_by_agent_flow": status.branch_created_by_agent_flow,
    }
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
    # 디렉터리 스캔은 agent-flow가 만든 자리만 본다. 사용자가 raw git으로 만든
    # 체크아웃은 등록부에만 있으므로 그쪽도 아는 이름의 원천으로 함께 넣는다.
    names.update(entry.path.name for entry in removable_worktrees(root=root))
    return sorted(names)


def remove_worktree_metadata(*, root: Path, name: str, path: Path | None = None) -> None:
    """``name``의 런타임 메타데이터를 지운다.

    ``path``를 주면 그 등록 경로의 소유임을 증명한 경우에만 지운다. 정규화된 키는
    서로 다른 등록 경로를 한 자리로 접는다 — 관리형 ``.../feat-demo``와 외부
    ``.../demo``가 둘 다 ``feat-demo``다. 증명 없이 지우면 외부 worktree를
    제거하다가 관리형 worktree의 활성 run과 manifest를 날린다.

    증명과 삭제는 **런타임 상태를 실제로 쌓는 같은 키**로 한다
    (`worktree_runtime_root`). 증명만 정규화하면 ``feat-issue#110``의 소유 판정을
    형제 ``feat-issue-110``의 manifest가 대신 내려 먼저 False가 되고, 대상 자신의
    죽은 active 마커가 남아 같은 자리의 다음 run을 `already active`로 막는다.
    """
    try:
        key = _runtime_state_key(root=root, name=name)
    except ValueError:
        # agent-flow 이름 규칙으로 정규화되지 않는 이름에는 애초에 메타데이터가 없다.
        return
    if path is not None and not _metadata_belongs_to_path(root=root, key=key, path=path):
        return
    runtime_root = _runtime_state_root(root=root, name=key)
    legacy_manifest = _managed_checkout_path(root=root, name=key) / "manifest.json"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
    if legacy_manifest.exists():
        legacy_manifest.unlink()


def _runtime_state_key(*, root: Path, name: str) -> str:
    """런타임 상태 디렉터리 키. 등록·상태 디렉터리에 실재하는 이름이 정규화보다 우선한다.

    조회가 실패해도 정리는 계속돼야 하므로 정규화로 접는다. 여기서 raise하면
    복구 명령이 메타데이터를 남긴 채 죽는다.
    """
    try:
        resolved = resolve_worktree_name(root=root, name=name)
    except (OSError, RuntimeError, ValueError):
        resolved = _feature_worktree_name(name)
    # 정규화를 건너뛰는 자리라 경로 안전은 여기서 증명한다. 키는 상태 루트와 관리
    # 루트의 직계 자식 이름이어야 한다 — `../`가 섞인 이름은 그 밖을 가리킨다.
    if not resolved or Path(resolved).name != resolved:
        raise ValueError(f"unsafe worktree state key: {resolved!r}")
    return resolved


def _metadata_belongs_to_path(*, root: Path, key: str, path: Path) -> bool:
    """``key`` 자리의 메타데이터가 이 등록 경로의 것인가.

    manifest가 있으면 그 안에 기록된 경로가 진실이다. manifest가 없으면 생성
    규약(관리 루트 아래 ``<key>`` 디렉터리)으로만 인정한다 — 롤백 경로처럼
    manifest를 쓰기 전에 정리해야 하는 경우가 그 하나다.
    """
    payload = _state_key_manifest(root=root, key=key)
    if payload is None:
        return same_worktree_path(_managed_checkout_path(root=root, name=key), path)
    recorded = payload.get("path")
    if not isinstance(recorded, str) or not recorded:
        return False
    candidate = Path(recorded)
    if not candidate.is_absolute():
        candidate = root / candidate
    return same_worktree_path(candidate, path)


def _git_dirty(root: Path) -> bool:
    # 관측이다. status는 기본적으로 index를 refresh하며 index.lock을 잡으므로
    # 동시에 도는 워커의 실제 쓰기와 경합을 만든다.
    result = git_safe(
        "status", "--porcelain", cwd=root, timeout_s=GIT_WORKTREE_TIMEOUT_S, optional_locks=False
    )
    if not result.ok:
        raise subprocess.CalledProcessError(
            result.returncode or 1, result.args, output=result.stdout, stderr=result.stderr
        )
    dirty_lines = [
        line
        for line in result.stdout.splitlines()
        if not _is_agent_flow_status_line(line)
    ]
    return bool(dirty_lines)


def _managed_root(root: Path) -> Path:
    return root / ".agent-flow" / "worktrees"


def _managed_checkout_path(*, root: Path, name: str) -> Path:
    return _managed_root(root) / name


def _runtime_state_root(*, root: Path, name: str) -> Path:
    return _agent_flow_git_dir(root) / "worktrees" / name


def _worktree_manifest_path(*, root: Path, name: str) -> Path:
    return _runtime_state_root(root=root, name=_feature_worktree_name(name)) / "manifest.json"


def _legacy_worktree_manifest_path(*, root: Path, name: str) -> Path:
    # 정규화를 거치는 이유는 경로 안전이다. 호출자가 넘긴 이름을 그대로 이어
    # 붙이면 `../`가 섞인 이름이 관리 루트 밖을 가리킨다.
    return _managed_checkout_path(root=root, name=_feature_worktree_name(name)) / "manifest.json"


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


def _worktree_setup_path(base: Path, name: str) -> Path:
    """선언된 이름을 base 안의 경로로 푼다.

    선언은 설정 **파일 이름**이지 임의 경로가 아니다. `../../.ssh/id_rsa` 한 줄이면
    profile이 저장소 밖을 읽고 쓰게 된다. 봉쇄 판정은 lexical로 한다 — 실제 경로를
    따라가면 base 자체가 symlink인 환경(macOS `/tmp`)에서 판정이 흔들리고, 마지막
    구성요소가 symlink인 경우는 호출부가 따로 거부한다.
    """
    if not name or not name.strip():
        raise ValueError("worktree setup entry must not be empty")
    candidate = Path(name)
    if candidate.is_absolute():
        raise ValueError(f"worktree setup entry must be relative: {name}")
    base_norm = os.path.normpath(str(base))
    resolved = os.path.normpath(os.path.join(base_norm, name))
    if resolved != base_norm and not resolved.startswith(base_norm + os.sep):
        raise ValueError(f"worktree setup entry escapes {base}: {name}")
    if resolved == base_norm:
        raise ValueError(f"worktree setup entry must name a file: {name}")
    return Path(resolved)


def _has_symlinked_component(base: Path, target: Path) -> bool:
    """base 아래 어느 구성요소든 symlink인가.

    마지막 구성요소만 보면 중간 디렉터리 symlink로 봉쇄가 뚫린다. lexical 판정은
    통과하고, leaf는 symlink가 아니며, `is_file()`은 따라간 곳을 보고 참을 낸다.
    git은 symlink를 커밋할 수 있으므로 `config` -> `/etc`가 저장소에 들어와 있을 수 있고,
    그러면 `config/passwd` 선언 한 줄로 저장소 밖 파일이 복사된다. 쓰는 쪽도 같다 —
    checkout의 중간 디렉터리가 symlink면 복사본이 worktree 밖에 떨어진다.
    """
    current = base
    for part in target.relative_to(base).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


class UnknownWorktreeSetupAction(ValueError):
    """선언에 없는 이름. 조용히 넘기면 선언했는데 아무 일도 일어나지 않는다."""


def _register_git_exclude(leader: Path, pattern: str) -> None:
    """`node_modules/`처럼 슬래시로 끝나는 항목은 디렉터리만 맞아서 symlink 자체를
    못 가린다. 루트 고정 패턴을 따로 적어야 worktree 정리가 막히지 않는다.

    worktree를 지울 때 이 항목은 지우지 않는다. 저장소가 공유하는 파일이라 다른
    worktree가 아직 같은 symlink를 쓰고 있을 수 있다. 추적 중인 경로는 exclude로
    가려지지 않으므로 남겨도 손해가 없다.
    """
    # leader가 그 자체로 linked worktree면 `.git`은 디렉터리가 아니라 파일이다.
    common = Path(_run_git(leader, "rev-parse", "--git-common-dir").stdout.strip())
    if not common.is_absolute():
        common = leader / common
    exclude = common / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if pattern in current.split():
        return
    prefix = "" if not current or current.endswith("\n") else "\n"
    exclude.write_text(f"{current}{prefix}{pattern}\n", encoding="utf-8")


def _link_node_modules(*, leader: Path, checkout: Path) -> bool:
    source = leader / "node_modules"
    target = checkout / "node_modules"
    # 이미 있는 트리를 symlink로 갈아치우면 그 안의 작업이 사라진다.
    if target.exists() or target.is_symlink():
        return False
    if not source.is_dir() or source.is_symlink():
        return False
    target.symlink_to(source, target_is_directory=True)
    _register_git_exclude(leader, "/node_modules")
    return True


# profile은 이 이름들만 고른다. 명령 문자열이 들어올 자리가 없다.
#
# npm 설치는 뺐다. 이름만 고르더라도 그 함수가 저장소의 package.json에서
# preinstall/postinstall을 끌어다 실행하므로 위 성질이 그 동작에서만 깨진다.
WORKTREE_SETUP_ACTIONS: dict[str, Any] = {
    "link_node_modules": _link_node_modules,
}


def run_declared_worktree_actions(
    *, leader: Path, checkout: Path, declared: dict[str, Any]
) -> tuple[str, ...]:
    """선언이 켜 둔 동작만 돌린다. 반환값은 실제로 수행된 이름들.

    실패는 이름 짓기와 같은 규율이다 — 경고만 내고 worktree 생성을 되돌리지 않는다.
    설정 하나 없어 빌드가 한 번 실패하는 것과 작업 자리가 없어지는 것은 무게가 다르다.
    """
    unknown = sorted(set(declared) - set(WORKTREE_SETUP_ACTIONS))
    if unknown:
        raise UnknownWorktreeSetupAction(
            f"unknown worktree setup action(s): {', '.join(unknown)}; "
            f"known: {', '.join(sorted(WORKTREE_SETUP_ACTIONS))}"
        )
    ran: list[str] = []
    for name in sorted(declared):
        if not declared[name]:
            continue
        try:
            if WORKTREE_SETUP_ACTIONS[name](leader=leader, checkout=checkout):
                ran.append(name)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"warning: worktree setup action {name} failed: {exc}", file=sys.stderr)
    return tuple(ran)


def copy_declared_worktree_files(
    *, leader: Path, checkout: Path, names: Iterable[str]
) -> tuple[str, ...]:
    """gitignored 머신 설정을 leader에서 새 checkout으로 옮긴다.

    `git worktree add`는 추적 파일만 가져온다. Android `local.properties`의 `sdk.dir`
    처럼 머신마다 다른 값은 gitignored라 새 worktree에 없고, 그래서 빌드가 leader에서는
    되고 worktree에서는 안 된다.

    symlink가 아니라 **복사**다. 작고 머신 고정인 설정은 worktree 안에서 고쳐도 leader로
    새면 안 된다. 큰 의존성 디렉터리 공유는 성격이 달라 여기서 다루지 않는다.

    이미 있는 파일은 건드리지 않는다 — 사용자가 손댄 설정을 덮으면 그 수정이 조용히
    사라진다. leader 쪽이 symlink면 건너뛴다. 따라간 곳이 저장소 밖일 수 있고, 그
    내용을 worktree로 실어 나를 이유가 없다.

    반환값은 실제로 복사한 선언 이름들이다.
    """
    leader_base = Path(os.path.normpath(str(leader)))
    checkout_base = Path(os.path.normpath(str(checkout)))
    copied: list[str] = []
    for name in names:
        source = _worktree_setup_path(leader, name)
        target = _worktree_setup_path(checkout, name)
        if target.exists() or target.is_symlink():
            continue
        if _has_symlinked_component(checkout_base, target):
            continue
        if _has_symlinked_component(leader_base, source) or not source.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(name)
    return tuple(copied)


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
    planned_branch = _planned_branch(status.name)
    if planned_branch is None:
        return None
    if not status.branch_created_by_agent_flow or status.branch != planned_branch:
        return None
    result = git_safe(
        "-C", str(status.path), "branch", "--show-current", cwd=root, optional_locks=False
    )
    if not result.ok:
        return None
    current_branch = result.stdout.strip()
    return current_branch if current_branch == planned_branch else None


@dataclass(frozen=True)
class SlugQuality:
    """slug와 그것을 믿어도 되는지.

    `partial`이 가장 위험하다 — 그럴듯해 보이지만 task를 대표하지 않는다.
    비율로 판정하지 않는다. 임계값을 두면 그 숫자가 곧 정책이 된다.
    """

    slug: str
    kind: str
    dropped: tuple[str, ...]


_SLUG_SAFE_CHAR_RE = re.compile(r"[a-z0-9]")


def describe_slug(value: str) -> SlugQuality:
    lowered = value.strip().lower()
    safe = re.sub(r"[^a-z0-9._-]+", "-", lowered).strip("-")
    # 토큰 단위로 생사를 보면 `로그인화면Figma구현`처럼 붙여 쓴 task가 통째로
    # 살아남은 것이 된다. 실제로 지워진 글자를 기준으로 센다.
    dropped = tuple(
        word
        for word in value.split()
        if any(char.isalnum() and not _SLUG_SAFE_CHAR_RE.match(char.lower()) for char in word)
    )
    if not safe or safe.startswith(".") or ".." in safe:
        if not any(char.isalnum() for char in lowered):
            raise ValueError(f"worktree name must contain at least one safe character: {value}")
        # 한글 등 비ASCII task도 기본 worktree 이름으로 쓸 수 있게 안정적인 fallback을 둔다.
        digest = hashlib.sha1(lowered.encode("utf-8")).hexdigest()[:8]
        return SlugQuality(slug=f"task-{digest}", kind="digest", dropped=dropped)
    return SlugQuality(
        slug=safe, kind="partial" if dropped else "ascii", dropped=dropped
    )


DEFAULT_SLUG_MAX_LENGTH = 60
DEFAULT_SLUG_TIMEOUT_S = 20


def delegated_slug(
    *,
    task: str,
    command: Sequence[str],
    timeout_s: int = DEFAULT_SLUG_TIMEOUT_S,
    max_length: int = DEFAULT_SLUG_MAX_LENGTH,
) -> str | None:
    """profile이 선언한 명령에 이름 짓기를 위임한다. 실패하면 `None`.

    셸을 거치지 않는다 — task는 사용자 문자열이라 명령에 끼워 넣으면 명령이 된다.
    host 출력은 제안일 뿐이라 slug 규칙으로 다시 검증한다.
    어떤 실패도 worktree 생성을 막지 않는다.
    """
    if not command:
        return None
    argv = [task if part == "{task}" else part for part in command]
    if all(part != task for part in argv):
        argv = [*argv, task]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    first_line = next(
        (line for line in (result.stdout or "").splitlines() if line.strip()), ""
    )
    try:
        quality = describe_slug(first_line.strip().strip("\"'"))
    except ValueError:
        return None
    if quality.kind != "ascii":
        return None
    return _truncate_slug(quality.slug, max_length)


def _truncate_slug(slug: str, max_length: int) -> str:
    """길이 제한은 되도록 단어 경계에서 건다. 첫 낱말이 이미 길면 잘라 쓴다."""
    if len(slug) <= max_length:
        return slug
    parts: list[str] = []
    for part in slug.split("-"):
        candidate = "-".join([*parts, part])
        if parts and len(candidate) > max_length:
            break
        parts.append(part)
    return "-".join(parts)[:max_length].strip("-")


def _safe_component(value: str) -> str:
    return describe_slug(value).slug


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
