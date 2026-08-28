from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agent_flow.artifact import (
    ACTIVE_MARKER,
    find_active_runs,
    mark_inactive,
    read_meta,
    write_meta,
)
from agent_flow.core.hook_integrity import (
    JSON_REGISTRATION_FILES,
    OMP_REGISTRATION_FILE,
    managed_path_hook_name,
)
from agent_flow.core.profiles import active_profile_ids, load_profile_payload
from agent_flow.core.security import validate_git_branch
from agent_flow.core.worktree_isolation import (
    FileLeaseUnavailable,
    RegisteredWorktree,
    WorktreeIsolationError,
    adopted_worktree_parent,
    assert_worktree_mergeable,
    exclusive_file_lease,
    forget_adopted_checkout,
    git_repo_state,
    git_safe,
    is_git_lock_contention,
    list_registered_worktrees,
    real_path,
    record_adopted_checkout,
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
# `dir_fd`는 POSIX 전용이다. 없는 플랫폼에서는 이름 기반 경로로 내려가고, 그 경우 부모
# 디렉터리 바꿔치기까지는 막지 못한다. `os.replace`는 macOS에서 `supports_dir_fd`에 없고
# `os.rename`만 있다 — POSIX의 `renameat`은 대상이 있어도 원자적으로 덮으므로 같은 것이다.
_DIR_FD_SUPPORTED = (
    os.open in os.supports_dir_fd
    and os.link in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
)
CLEANUP_JOURNAL_VERSION = 3
CLEANUP_STEPS = (
    "archive",
    "integration_proof",
    "checkout_removal",
    "branch_ref_cas",
    "metadata_cleanup",
)
_RUN_COORDINATION_FILES = frozenset({"active.lock", "lifecycle.lock"})


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


@dataclass(frozen=True)
class CleanupResumeResult:
    run_dir: Path
    aborted: bool



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
    with _checkout_activity_lease(root, plan.path), worktree_creation_lock(root):
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
        verify_linked_worktree(root=root, path=plan.path, managed_root=plan.path.parent)
        existing = get_worktree_status(root=root, name=plan.name)
        if plan.branch_explicit and existing.branch != plan.branch:
            raise ValueError(
                f"worktree {plan.name} already uses branch {existing.branch}; "
                f"requested {plan.branch}"
            )
        # 재사용도 leader가 내리는 채택이다. 기록하지 않으면 create가 성공한 직후의
        # `run --worktree <name>`이 미채택으로 거절된다 — 사용자에게는 방금 만든
        # worktree가 이유 없이 막힌 것으로 보인다.
        if existing.registration_identity is not None:
            record_adopted_checkout(
                root=root,
                name=existing.name,
                path=existing.path,
                registration_identity=existing.registration_identity,
            )
        return existing
    _ensure_creation_root(plan.path.parent)
    branch_created = _add_worktree_locked(root=root, plan=plan)
    # Fail closed: trust the path only after git confirms it is a linked
    # worktree of this repo on the expected branch.
    # containment는 생성 규약이 정한 checkout 부모를 그대로 넘긴다. 실질 증명은
    # 아래 gitdir 왕복 검증이다.
    verify_linked_worktree(
        root=root,
        path=plan.path,
        expected_branch=plan.branch,
        managed_root=plan.path.parent,
    )
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
        # 새 기본 자리는 marker 후보가 아니다. 경계가 claim의 소유 checkout을 고르는
        # 근거(`trusted_checkout_paths`)는 marker layout 아니면 채택 기록뿐이므로,
        # agent-flow가 만든 것도 스스로 기록해 둔다 — 이건 leader 쪽 행위다.
        record_adopted_checkout(
            root=root,
            name=status.name,
            path=status.path,
            registration_identity=status.registration_identity,
        )
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
        except Exception as cleanup_exc:  # noqa: BLE001
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
    adopt: bool = False,
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
    with _checkout_activity_lease(root, registered.path):
        return _attach_registered_worktree(
            root=root,
            selector=selector,
            registered=registered,
            branch=branch,
            allow_dirty=allow_dirty,
            expected_registration_identity=expected_registration_identity,
            adopt=adopt,
        )


def _attach_registered_worktree(
    *,
    root: Path,
    selector: str,
    registered: RegisteredWorktree,
    branch: str | None,
    allow_dirty: bool,
    expected_registration_identity: str | None,
    adopt: bool,
) -> WorktreeStatus | None:
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
    if (
        not adopt
        and not _is_managed_child(root=root, path=registered.path)
        and not _adopted(root=root, path=registered.path)
    ):
        # 관리 루트 밖 checkout은 등록만으로 들어오지 못한다. 등록은 git이 해 주는
        # 것이고 워커도 할 수 있으므로, leader가 서명한 manifest를 요구한다. 그렇다고
        # 생성 경로로 흘려보내면 selector를 디렉터리 이름으로 뭉개 엉뚱한 checkout을
        # 만든다(절대경로 selector는 경로 전체가 이름이 된다).
        raise ValueError(
            f"worktree {registered.path} is outside {managed_worktrees_root(root)} and is not "
            f"adopted; run `agent-flow worktree adopt --path {registered.path}` first"
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
        if current is None or not _same_registration(registered, current):
            raise ValueError(
                f"worktree registration changed while attaching {registered.path}; "
                "re-run the command"
            )
        status = _status_for_registered(
            root=root, registered=current, requested=selector
        )
        runtime_root = _runtime_root_for_status(root=root, status=status)
        if runtime_root is None:
            # 이름은 같지만 메타데이터가 다른 checkout의 것이다. 어느 자리가 막고
            # 있는지 말해 주지 않으면 사용자는 "채택하라"는 안내와 "채택 거부" 사이를
            # 무한히 왕복한다.
            raise ValueError(
                f"worktree {registered.path.name} has conflicting agent-flow metadata; "
                f"refusing attach. {_conflicting_metadata_hint(root=root, status=status)}"
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


def adopt_worktree(
    *,
    root: Path,
    path: Path,
    allow_dirty: bool = False,
) -> WorktreeStatus:
    """등록부가 아는 checkout 하나를 lifecycle metadata와 함께 채택한다.

    경로 모양은 묻지 않는다. git이 이 저장소의 linked worktree로 등록했고 gitdir 왕복
    검증을 통과하면 된다. 이 명령이 채택의 유일한 진입점인 이유는 인가를 사람이
    주어야 하기 때문이다 — `git worktree add`는 워커도 할 수 있고 그 호출은 host
    write boundary의 write 명령 집합에 없다.

    채택은 브랜치 소유권을 주장하지 않는다(`branch_created_by_agent_flow=False`).
    terminal cleanup은 checkout만 제거하고 브랜치는 보존한다.
    """
    checkout = real_path(path)
    registered = resolve_worktree(root=root, selector=str(checkout))
    if registered is None:
        raise ValueError(
            f"no linked worktree of {root} is registered at {path}; "
            "create it with `git worktree add` first"
        )
    with _checkout_activity_lease(root, registered.path):
        status = _attach_registered_worktree(
            root=root,
            selector=str(checkout),
            registered=registered,
            branch=None,
            allow_dirty=allow_dirty,
            expected_registration_identity=None,
            adopt=True,
        )
        if status is None:
            raise ValueError(
                f"no linked worktree of {root} is registered at {path}; "
                "create it with `git worktree add` first"
            )
        # 기록은 manifest가 아니라 워커 쓰기 구역 밖의 `adopted/`에 남긴다. 이후의 모든
        # 인가 판정(`adopted_worktree_parent`, `trusted_checkout_paths`)이 이 파일을 본다.
        try:
            record_adopted_checkout(
                root=root,
                name=status.name,
                path=status.path,
                registration_identity=status.registration_identity,
            )
        except (OSError, WorktreeIsolationError) as exc:
            # attach는 이미 끝났다. 그 사실을 말하지 않으면 사용자는 "채택하라"는 안내와
            # 원시 errno 사이에서 지금 상태가 무엇인지 알 수 없다.
            raise WorktreeIsolationError(
                f"attached {status.path} but could not record the adoption: {exc}. "
                "That checkout is still unadopted — clear the cause and run the same "
                "command again"
            ) from exc
        return status


def _adopted(*, root: Path, path: Path) -> bool:
    return adopted_worktree_parent(root=root, path=path) is not None


def _is_managed_child(*, root: Path, path: Path) -> bool:
    """marker 관리 루트의 직계 자식인가 — 모양 자체가 관리형임을 증명하는 자리다.

    외부 생성 자리는 여기 넣지 않는다. 그 경로는 누구나 `git worktree add`로 만들 수
    있어 모양이 근거가 되지 못한다. 그쪽 근거는 생성이 남긴 채택 기록이다 — 넣으면
    raw checkout이 미채택 차단을 우회하고, 나중에 host write boundary가 소유를
    증명하지 못해 run이 막힌다.
    """
    parent_key = worktree_path_key(path.parent)
    return any(
        parent_key == worktree_path_key(root / marker / "worktrees")
        for marker in (".agent-flow", ".codex", ".Codex", ".omp")
    )


def _is_creation_layout_child(*, root: Path, path: Path) -> bool:
    """생성 규약이 쓰는 신뢰 가능한 현재·이전 자리의 직계 자식인가."""
    parent_key = worktree_path_key(path.parent)
    return any(
        parent_key == worktree_path_key(candidate)
        for candidate in _trusted_existing_creation_layout_roots(root)
    )


def _assert_requestable_branch(branch: str) -> None:
    validate_git_branch(branch)
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
    force_metadata: bool = False,
) -> bool:
    """Remove one checkout only while repository-wide cleanup exclusion is held.

    Returns whether the runtime metadata for this name was cleared.
    """
    with _cleanup_lease(root, status.path):
        runtime_root = _runtime_root_for_status(root=root, status=status)
        with _run_start_exclusion(runtime_root):
            _assert_no_active_runs(runtime_root)
            return _remove_worktree_locked(
                root=root,
                status=status,
                delete_branch=delete_branch,
                require_merged=require_merged,
                allow_unmerged=allow_unmerged,
                force_metadata=force_metadata,
            )


def _remove_worktree_locked(
    *,
    root: Path,
    status: WorktreeStatus,
    delete_branch: bool,
    require_merged: bool,
    allow_unmerged: bool,
    force_metadata: bool = False,
) -> bool:
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
    if live:
        _retire_provisioned_host_hook_registrations(root=root, checkout=status.path)
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
    metadata_removed = remove_worktree_metadata(
        root=root,
        name=status.name,
        path=status.path,
        force=force_metadata,
    )
    if not live and status.path.is_dir() and _is_creation_layout_child(root=root, path=status.path):
        try:
            status.path.rmdir()
        except OSError:
            pass
    return metadata_removed


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
        # journal은 lease 밖에서 읽은 cache가 아니다. abort가 먼저 terminal intent를
        # publish했다면 여기서 반드시 최신 상태를 보고 destructive step을 재개하지 않는다.
        journal = _load_cleanup_journal(journal_path, require_checkout=False)
        if journal.get("status") == "aborted":
            raise CleanupBlockedError(
                "cleanup was aborted; destructive steps will not resume",
                journal_path=journal_path,
                run_dir=run_dir,
            )
        if journal.get("status") == "complete":
            return CleanupTransactionResult(
                journal_path=journal_path,
                run_dir=Path(journal["run"]["archive_dir"]),
            )
        _validate_cleanup_resume(
            root=root,
            run_dir=run_dir,
            target_branch=target_branch,
            journal_path=journal_path,
            journal=journal,
            checkout_path=checkout_path,
        )
        _refresh_cleanup_target_for_resume(
            root=root,
            journal_path=journal_path,
            journal=journal,
            target_branch=target_branch,
        )
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
                        _record_integration_proof(
                            root=root,
                            journal=journal,
                            target_branch=target_branch,
                        )
                    elif step == "checkout_removal":
                        _remove_journal_checkout(
                            root=root,
                            journal_path=journal_path,
                            journal=journal,
                            target_branch=target_branch,
                        )
                    elif step == "branch_ref_cas":
                        _delete_journal_branch(
                            root=root,
                            journal_path=journal_path,
                            journal=journal,
                            target_branch=target_branch,
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
        journal = _load_cleanup_journal(path, require_checkout=False)
        checkout = journal.get("checkout")
        if not isinstance(checkout, dict):
            continue
        selector_keys = ("name", "branch", "identity", "path")
        selectors_complete = all(
            isinstance(checkout.get(key), str) and bool(checkout.get(key))
            for key in selector_keys
        )
        recorded_selectors = {
            checkout[key]
            for key in selector_keys
            if isinstance(checkout.get(key), str) and checkout[key]
        }
        if selector not in recorded_selectors:
            continue
        run = journal.get("run")
        if (
            not selectors_complete
            or not isinstance(run, dict)
            or any(
                not isinstance(run.get(key), str)
                or not run.get(key)
                for key in ("archive_dir", "source_dir")
            )
        ):
            raise CleanupBlockedError(
                "cleanup journal contract is incomplete; preserving checkout",
                journal_path=path,
            )
        archive = Path(run["archive_dir"])
        source = Path(run["source_dir"])
        run_dir = archive if archive.exists() else source
        if (
            journal.get("status") == "complete"
            and _cleanup_terminal_publication_complete(
                run_dir=run_dir,
                journal_path=path,
            )
        ):
            continue
        # abort된 owner의 journal은 증거로 보존하되 pending 선택 대상에서는 뺀다.
        # source가 사라졌다면 cleanup 마지막 구간이므로 journal만이 복구 소유자다.
        if source.is_dir():
            source_meta = read_meta(source)
            owner_active = (source / ACTIVE_MARKER).is_file()
            owner_matches = (
                source_meta.get("cleanup_state") == "cleanup_pending"
                and source_meta.get("cleanup_journal") == str(path)
            )
            if not owner_active:
                # 0.2.6과 수동 복구는 journal을 terminal로 만들기 전에 marker부터
                # 걷었다. 그 legacy state를 영구 blocker로 만들지 않고 증거만 남긴다.
                continue
            if not owner_matches:
                raise CleanupBlockedError(
                    "cleanup journal owner metadata is mismatched; preserving checkout",
                    journal_path=path,
                    run_dir=source,
                )
        elif journal.get("status") == "aborted":
            # marker 제거 전 crash면 source owner가 위 분기에서 journal을 계속 대변한다.
            # source가 이미 사라진 terminal 증거만 pending selector에서 제외한다.
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


def run_tree_is_sealed(run_dir: Path | None) -> bool:
    """cleanup journal이 이 run tree의 digest를 기록한 뒤부터 참이다.

    그 시점부터 source와 archive 사본은 같은 해시 하나로 묶인다 - 어느 쪽에 한 줄만
    더해도 다음 resume이 그 바이트를 오염으로 읽고 정리를 영구히 막는다. digest가
    아직 없으면 run tree는 살아 있고, 기록은 그대로 run이 가져간다.
    """
    if run_dir is None:
        return False
    try:
        meta = read_meta(run_dir)
    except (OSError, ValueError):
        # 비필수 trace보다 archive 무결성이 우선이다. metadata를 읽지 못해 봉인
        # 여부가 불명확하면 쓰지 않는다.
        return True
    if not meta:
        # read_meta는 누락·손상을 예외 대신 빈 mapping으로 돌려준다. 유효한 run
        # metadata가 하나도 없으면 봉인 여부를 증명할 수 없으므로 쓰지 않는다.
        return True
    journal_path = meta.get("cleanup_journal")
    if not isinstance(journal_path, str) or not journal_path:
        return False
    try:
        journal = _load_cleanup_journal(Path(journal_path))
    except (CleanupBlockedError, OSError, ValueError):
        # journal이 있다고 기록돼 있는데 읽을 수 없다면 봉인 여부는 불명확하다.
        # observation append가 복구 가능한 읽기 실패를 영구 checksum 불일치로
        # 바꾸지 않게 fail-closed한다.
        return True
    run = journal.get("run")
    return isinstance(run, dict) and isinstance(run.get("archive_digest"), str)


def _abort_cleanup_context(
    *,
    root: Path,
    run_dir: Path,
    journal_path: Path,
    journal: dict[str, Any],
) -> tuple[Path, Path, Path]:
    """abort가 lock을 만들기 전에 journal 경로와 살아 있는 owner를 검증한다."""
    checkout = journal.get("checkout")
    run = journal.get("run")
    runtime_root = run_dir.parent.parent.parent
    archive = Path(run["archive_dir"]) if isinstance(run, dict) and isinstance(
        run.get("archive_dir"), str
    ) else None
    archive_state = (
        Path(run["archive_state_root"])
        if isinstance(run, dict) and isinstance(run.get("archive_state_root"), str)
        else None
    )
    state_root = real_path(_agent_flow_state_dir(root))
    if (
        not isinstance(checkout, dict)
        or not isinstance(checkout.get("name"), str)
        or not isinstance(checkout.get("identity"), str)
        or not isinstance(checkout.get("path"), str)
        or not isinstance(checkout.get("registration_identity"), str)
        or not isinstance(run, dict)
        or not isinstance(run.get("source_state_root"), str)
        or not isinstance(run.get("source_dir"), str)
        or archive is None
        or archive_state is None
        or real_path(journal_path.parent) != real_path(_cleanup_pending_root(root))
        or journal.get("repository") != _cleanup_repository_identity(root)
        or journal.get("run_id") != run_dir.name
        or run_dir.parent.name != "runs"
        or run_dir.parent.parent.name != ".agent-flow"
        or not real_path(runtime_root).is_relative_to(state_root)
        or real_path(Path(run["source_state_root"])) != real_path(runtime_root)
        or real_path(Path(run["source_dir"])) != real_path(run_dir)
        or archive.name != run_dir.name
        or archive.parent.name != "runs"
        or archive.parent.parent.name != ".agent-flow"
        or real_path(archive.parent.parent.parent) != real_path(archive_state)
        or not real_path(archive_state).is_relative_to(state_root)
    ):
        raise CleanupBlockedError(
            "cleanup journal does not belong to the active run",
            journal_path=journal_path,
            run_dir=run_dir,
        )
    status = get_worktree_status(root=root, name=checkout["name"])
    meta = read_meta(run_dir)
    registration_matches = (
        status.registration_identity == checkout["registration_identity"]
        if status.exists
        else status.registration_identity
        in {None, checkout["registration_identity"]}
    )
    digest = hashlib.sha256(
        f"{worktree_path_key(_git_common_dir(root))}\0"
        f"{worktree_path_key(status.path)}".encode()
    ).hexdigest()[:16]
    expected_archive_state = (
        _agent_flow_state_dir(root)
        / "archive"
        / "worktrees"
        / f"{_safe_component(status.name)}-{digest}"
    )
    expected_archive = (
        expected_archive_state / ".agent-flow" / "runs" / run_dir.name
    )
    if (
        status.name != checkout["name"]
        or real_path(status.path) != real_path(Path(checkout["path"]))
        or not registration_matches
        or meta.get("checkout_identity") != checkout["identity"]
        or archive_state != expected_archive_state
        or archive != expected_archive
        or _has_symlinked_component(state_root, archive)
        or meta.get("checkout_registration_identity")
        != checkout["registration_identity"]
        or meta.get("cleanup_state") != "cleanup_pending"
        or meta.get("cleanup_journal") != str(journal_path)
    ):
        raise CleanupBlockedError(
            "cleanup journal owner metadata is mismatched",
            journal_path=journal_path,
            run_dir=run_dir,
        )
    return status.path, runtime_root, archive


def abort_pending_worktree_cleanup(*, root: Path, run_dir: Path) -> bool:
    """살아 있는 cleanup owner와 그 journal을 하나의 회수 전이로 종결한다.

    journal을 먼저 terminal로 publish한다. 그 뒤 active marker 제거가 실패해도
    `abort` 재시도가 같은 journal을 다시 읽고 marker 제거를 끝낼 수 있다.
    """
    meta = read_meta(run_dir)
    journal_value = meta.get("cleanup_journal")
    if (
        meta.get("cleanup_state") != "cleanup_pending"
        or not isinstance(journal_value, str)
        or not journal_value
    ):
        return False
    journal_path = Path(journal_value)
    try:
        journal = _load_cleanup_journal(journal_path, require_checkout=False)
        checkout_path, runtime_root, archive_run = _abort_cleanup_context(
            root=root,
            run_dir=run_dir,
            journal_path=journal_path,
            journal=journal,
        )
    except (CleanupBlockedError, OSError, ValueError):
        # 신뢰할 수 없는 journal로 파일을 쓰지는 않는다. False는 CLI가 source의
        # active marker만 걷는 비파괴적 legacy escape를 실행하라는 뜻이다.
        return False
    with _cleanup_lease(root, checkout_path), _run_start_exclusion(runtime_root):
        journal = _load_cleanup_journal(journal_path, require_checkout=False)
        _, _, archive_run = _abort_cleanup_context(
            root=root,
            run_dir=run_dir,
            journal_path=journal_path,
            journal=journal,
        )
        status = journal.get("status")
        steps = journal.get("steps")
        destructive_started = isinstance(steps, dict) and any(
            isinstance(steps.get(step), dict)
            and steps[step].get("status") == "done"
            for step in ("checkout_removal", "branch_ref_cas", "metadata_cleanup")
        )
        if status in {"steps_complete", "complete"} or destructive_started:
            raise CleanupBlockedError(
                "cleanup already removed checkout state; run agent-flow continue",
                journal_path=journal_path,
                run_dir=run_dir,
            )
        if status == "cleanup_pending":
            now = _utc_now()
            journal["status"] = "aborted"
            journal["aborted_at"] = now
            journal["updated_at"] = now
            cleanup_lease = journal.get("leases", {}).get("cleanup")
            if isinstance(cleanup_lease, dict):
                cleanup_lease["state"] = "released"
            _write_cleanup_journal(journal_path, journal)
        elif status != "aborted":
            raise CleanupBlockedError(
                f"cleanup journal has invalid abort state: {status!r}",
                journal_path=journal_path,
                run_dir=run_dir,
            )
        if archive_run.is_dir():
            mark_inactive(archive_run)
        mark_inactive(run_dir)
    return True


def resume_pending_worktree_cleanup(
    *, root: Path, pending: CleanupTransactionResult
) -> CleanupResumeResult:
    """Resume a journal-owned cleanup without re-entering the phase runner."""
    journal = _load_cleanup_journal(pending.journal_path)
    if journal.get("status") == "aborted":
        run = journal.get("run")
        if not isinstance(run, dict) or not isinstance(run.get("source_dir"), str):
            raise CleanupBlockedError(
                "aborted cleanup journal contract is incomplete",
                journal_path=pending.journal_path,
                run_dir=pending.run_dir,
            )
        source = Path(run["source_dir"])
        if source.is_dir():
            if not abort_pending_worktree_cleanup(root=root, run_dir=source):
                raise CleanupBlockedError(
                    "aborted cleanup owner validation failed; "
                    "run agent-flow abort --worktree to retire its live marker",
                    journal_path=pending.journal_path,
                    run_dir=source,
                )
            return CleanupResumeResult(run_dir=source, aborted=True)
        return CleanupResumeResult(run_dir=pending.run_dir, aborted=True)
    target = journal.get("target")
    integration = journal.get("integration")
    checkout = journal.get("checkout")
    if (
        not isinstance(target, dict)
        or not isinstance(integration, dict)
        or not isinstance(checkout, dict)
    ):
        raise CleanupBlockedError(
            "cleanup journal contract is incomplete; preserving checkout"
        )
    target_branch = _cleanup_target_branch(target)
    strategy = integration.get("strategy")
    checkout_path = checkout.get("path")
    delete_branch = checkout.get("delete_branch")
    if (
        strategy not in {"merge", "squash", "rebase"}
        or not isinstance(checkout_path, str)
        or not checkout_path
        or not isinstance(delete_branch, bool)
    ):
        raise CleanupBlockedError(
            "cleanup journal integration contract is unknown; preserving checkout"
        )
    result = run_worktree_cleanup_transaction(
        root=root,
        checkout_path=Path(checkout_path),
        run_dir=pending.run_dir,
        target_branch=target_branch,
        integration_strategy=strategy,
        delete_branch=delete_branch,
    )
    return CleanupResumeResult(
        run_dir=complete_worktree_cleanup(result),
        aborted=False,
    )


def _cleanup_target_branch(target: dict[str, Any]) -> str:
    recorded = target.get("branch")
    if isinstance(recorded, str) and recorded:
        validate_git_branch(recorded)
        return recorded
    ref = target.get("ref")
    if isinstance(ref, str):
        if ref.startswith("refs/heads/"):
            branch = ref.removeprefix("refs/heads/")
        elif ref.startswith("refs/remotes/"):
            _remote, separator, branch = ref.removeprefix(
                "refs/remotes/"
            ).partition("/")
            if not separator:
                branch = ""
        else:
            branch = ""
        if branch:
            # Pre-upgrade journals recorded only the selected target ref.
            validate_git_branch(branch)
            return branch
    raise CleanupBlockedError(
        "cleanup journal target branch is unknown; preserving checkout"
    )


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
    validate_git_branch(target_branch)
    if target_branch == status.branch:
        raise CleanupBlockedError("cleanup target branch cannot be the worktree branch")
    branch_oid = _ref_oid(root=root, ref=f"refs/heads/{status.branch}")
    target_candidates: list[tuple[str, str]] = []
    for candidate in _branch_ref_candidates(root=root, branch=target_branch):
        try:
            _refresh_remote_branch_ref(
                root=root,
                ref=candidate,
                branch=target_branch,
            )
        except CleanupBlockedError:
            continue
        candidate_oid = _ref_oid(root=root, ref=candidate)
        if candidate_oid is not None:
            target_candidates.append((candidate, candidate_oid))
    if branch_oid is None or not target_candidates:
        raise CleanupBlockedError(
            "target or worktree branch OID is unknown; preserving checkout"
        )
    target_ref, target_oid = next(
        (
            candidate
            for candidate in target_candidates
            if _integration_method(
                root=root,
                head_oid=branch_oid,
                target_oid=candidate[1],
            )
            is not None
        ),
        target_candidates[0],
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
        f"{worktree_path_key(_git_common_dir(root))}\0{worktree_path_key(status.path)}".encode()
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
            "branch": target_branch,
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
    if journal["target"]["ref"] not in set(
        _branch_ref_candidates(root=root, branch=target_branch)
    ):
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
        if (
            source.name in _RUN_COORDINATION_FILES
            and stat.S_ISREG(identity.st_mode)
        ):
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
            # 0.2.6의 abort는 source marker만 걷었다. owner archive digest는 active를
            # 제외하므로, 다음 cleanup lease 아래에서 그 lifecycle marker만 맞춘다.
            # 이후 full digest를 다시 비교해 meta나 payload 차이는 그대로 차단한다.
            if (
                not (source / ACTIVE_MARKER).exists()
                and (destination / ACTIVE_MARKER).exists()
                and _run_tree_digest(source)
                == _run_tree_digest(destination)
            ):
                mark_inactive(destination)
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


def _refresh_cleanup_target_for_resume(
    *,
    root: Path,
    journal_path: Path,
    journal: dict[str, Any],
    target_branch: str,
) -> None:
    if journal["steps"]["integration_proof"]["status"] != "pending":
        return
    head_oid = journal["checkout"]["expected_head_oid"]
    for candidate in _branch_ref_candidates(root=root, branch=target_branch):
        try:
            _refresh_remote_branch_ref(
                root=root,
                ref=candidate,
                branch=target_branch,
            )
        except CleanupBlockedError:
            continue
        candidate_oid = _ref_oid(root=root, ref=candidate)
        if candidate_oid is None or not (
            _is_ancestor(root=root, ancestor=head_oid, descendant=candidate_oid)
            or _merge_tree_is_noop(
                root=root,
                head_oid=head_oid,
                target_oid=candidate_oid,
            )
        ):
            continue
        if (
            journal["target"]["ref"] == candidate
            and journal["target"]["expected_oid"] == candidate_oid
            and journal["target"].get("branch") == target_branch
        ):
            return
        journal["target"] = {
            "ref": candidate,
            "expected_oid": candidate_oid,
            "branch": target_branch,
        }
        journal["integration"]["proof"] = "pending"
        journal["integration"]["method"] = None
        journal["updated_at"] = _utc_now()
        _write_cleanup_journal(journal_path, journal)
        return


def _record_integration_proof(
    *, root: Path, journal: dict[str, Any], target_branch: str
) -> None:
    target_oid = _refresh_integration_target(
        root=root,
        journal=journal,
        target_branch=target_branch,
    )
    _validate_cleanup_snapshot(root=root, journal=journal, require_clean=True)
    _prove_recorded_integration(
        root=root,
        journal=journal,
        target_oid=target_oid,
    )


def _refresh_integration_target(
    *, root: Path, journal: dict[str, Any], target_branch: str
) -> str:
    target_ref = journal["target"]["ref"]
    _refresh_remote_branch_ref(root=root, ref=target_ref, branch=target_branch)
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
    method = _integration_method(root=root, head_oid=head_oid, target_oid=target_oid)
    if method is None:
        raise CleanupBlockedError(
            "cannot prove recorded head is integrated into recorded target; "
            "preserving checkout and branch"
        )
    journal["integration"]["proof"] = "verified"
    journal["integration"]["method"] = method
    journal["integration"]["verified_at"] = _utc_now()


def _integration_method(*, root: Path, head_oid: str, target_oid: str) -> str | None:
    if _is_ancestor(root=root, ancestor=head_oid, descendant=target_oid):
        return "head-ancestor-of-target"
    if _merge_tree_is_noop(root=root, head_oid=head_oid, target_oid=target_oid):
        return "merge-tree-noop"
    return None


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
    if target_oid is None or target_oid != journal["target"]["expected_oid"]:
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
        _assert_cleanup_checkout_clean(root=root, path=path)


def _assert_cleanup_checkout_clean(*, root: Path, path: Path) -> None:
    status = git_safe(
        "-C",
        str(path),
        "status",
        "--porcelain=v1",
        # `-z`는 경로를 인용하지 않으므로 비ASCII 경로도 그대로 온다. 이 저장소의 다른
        # status 파서(`core/local_skills.py`)와 같은 형태이기도 하다.
        "-z",
        "-uall",
        # ignore된 디렉터리를 파일 단위로 펴면(`--ignored=traditional`) node_modules를
        # 가진 트리에서 출력이 수만 행으로 늘고, 그 비용이 timeout과 겹치면 정리가
        # 영구히 막힌다. 접힌 형태로 싸게 받고 우리가 깐 등록만 아래에서 직접 증명해 뺀다.
        "--ignored=matching",
        cwd=path,
        optional_locks=False,
        # 같은 파일의 다른 git 호출과 같은 여유를 준다. 기본 30초를 넘기면 `status.ok`가
        # False가 되어 이 checkout은 정리 불가로 굳는다.
        timeout_s=GIT_WORKTREE_TIMEOUT_S,
    )
    if not status.ok:
        raise CleanupBlockedError(
            f"cannot inspect checkout status at {path}; preserving checkout"
        )
    # kit이 스스로 심은 것: host hook 등록 파일과 worktree setup 복사본. 같은 판정을
    # 쓴다 — "우리가 깔았고 지금도 우리 것임을 증명할 수 있는 파일".
    provisioned = _kit_owned_host_hook_registrations(root=root, checkout=path)
    kit_copied = _kit_copied_worktree_files(root=root, checkout=path)
    kit_owned = provisioned | kit_copied
    folded_dirs = _HOST_HOOK_REGISTRATION_DIRS | _folded_parent_dirs(kit_copied)
    for record, rel in _porcelain_z_records(status.stdout):
        if rel in provisioned:
            continue
        # 복사본은 추적되지 않는 상태(`!!`/`??`)일 때만 예외다. 추적 중인 파일의
        # 수정은 커밋되지 않은 작업이고, 그 내용이 leader 워킹트리와 우연히 같다는
        # 사실이 "지워도 된다"를 뜻하지 않는다.
        if rel in kit_copied and record[:2] in ("!!", "??"):
            continue
        if (
            record.startswith("!! ")
            and rel in folded_dirs
            and _dir_holds_only_provisioned_registrations(
                checkout=path, rel_dir=rel, provisioned=kit_owned
            )
        ):
            continue
        # 어느 경로 때문인지 말하지 않으면 사용자는 `git status`가 깨끗한 checkout을
        # 두고 무엇을 치워야 하는지 알 수 없다 — 그 조합이 실제로 존재한다.
        raise CleanupBlockedError(
            f"checkout is dirty at {path}: {rel or '(unnamed path)'}; preserving checkout"
        )


def _porcelain_z_records(stdout: str) -> Iterator[tuple[str, str]]:
    """`--porcelain=v1 -z` 출력을 (레코드, 경로)로 끊는다.

    `-z`는 rename/copy 레코드의 원래 경로를 NUL로 끊긴 **별도 필드**로 하나 더 붙인다.
    그 필드를 레코드로 읽으면 경로 앞 세 글자가 상태로 해석돼 임의 경로가 화이트리스트에
    걸릴 수 있다. 그래서 rename/copy 뒤 한 필드는 건너뛴다.
    """
    fields = stdout.split("\0")
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
        yield record, record[3:]
        if "R" in record[:2] or "C" in record[:2]:
            index += 1


def _dir_holds_only_provisioned_registrations(
    *, checkout: Path, rel_dir: str, provisioned: set[str]
) -> bool:
    """접힌 ignore 레코드가 정말 우리가 깐 등록 파일만 담고 있는가.

    git이 디렉터리 하나로 접어 준 레코드는 그 안에 무엇이 있는지 말해 주지 않는다.
    `.claude/settings.json.bak`이나 사용자가 남긴 파일이 같은 접힘 안에 숨는다. 그래서
    직접 walk해서 그 안의 **모든** 파일이 kit이 깐 등록임을 증명할 때만 dirty 집계에서
    뺀다. 증명 못 하면 dirty로 남긴다.
    """
    base = checkout / rel_dir.rstrip("/")
    try:
        if base.is_symlink() or not base.is_dir():
            return False
        proven = 0
        for entry in base.rglob("*"):
            # symlink는 따라간 곳이 checkout 밖일 수 있어 내용으로도 증명이 안 된다.
            if entry.is_symlink():
                return False
            if entry.is_dir():
                continue
            if entry.relative_to(checkout).as_posix() not in provisioned:
                return False
            proven += 1
        return proven > 0
    except OSError:
        return False


def _kit_owned_host_hook_registrations(*, root: Path, checkout: Path) -> set[str]:
    """checkout의 등록 파일 중 kit이 깐 것들. ``root``는 leader checkout이다.

    agent-flow가 스스로 깐 파일이다. 이걸 dirty로 세면 관리 worktree는 정리 자체가
    영영 막힌다.

    기준은 provision 쪽과 **같은** kit 소유 판정이다. "지금 leader 바이트와 동일"로
    보면 provision 이후 leader 등록이 갱신·삭제되는 순간 agent-flow가 스스로 깐 파일이
    곧바로 정리 차단 사유가 되고(그때 `git status`는 완전히 깨끗하다), pending cleanup이
    있는 checkout은 provision 지점을 다시 지나가지 않아 자가치유도 없다.
    """
    owned: set[str] = set()
    # 대소문자 무구분 FS에서 `.Codex/hooks.json`과 `.codex/hooks.json`은 같은 파일이다.
    # git index는 대소문자를 구분하므로 한쪽 이름으로 물으면 tracked가 아니라고 답하고,
    # 그 이름으로 unlink하면 **추적 중인 쌍둥이**가 지워져 checkout이 dirty가 된다.
    # 경로 문자열은 대소문자를 정규화하지 않으므로 inode로 같은 파일인지 본다.
    tracked_nodes: set[tuple[int, int]] = set()
    for rel in HOST_HOOK_REGISTRATION_FILES:
        if not _tracked_in_checkout(checkout=checkout, rel=rel):
            continue
        try:
            info = (checkout / rel).stat()
        except OSError:
            continue
        tracked_nodes.add((info.st_dev, info.st_ino))
    for rel in HOST_HOOK_REGISTRATION_FILES:
        target = checkout / rel
        try:
            if target.is_symlink() or not target.is_file():
                continue
            identity = target.stat()
            payload = target.read_bytes()
        except OSError:
            continue
        if not _host_hook_registration_is_kit_owned(leader=root, rel=rel, payload=payload):
            continue
        # git이 추적하는 파일은 provision이 쓰지도 않았고 정리가 지워서도 안 된다.
        # 지우면 그 checkout이 삭제된 tracked 파일로 dirty가 되어 정리가 막힌다 —
        # 커밋된 host 설정을 쓰는 저장소에서 실제로 그렇게 됐다.
        if (identity.st_dev, identity.st_ino) in tracked_nodes:
            continue
        owned.add(rel)
    return owned


def _kit_copied_worktree_files(*, root: Path, checkout: Path) -> set[str]:
    """checkout의 worktree setup 복사본 중 leader와 **바이트가 같은** 것들.
    ``root``는 leader checkout이다.

    checkout을 만들 때 kit이 직접 심는 파일이다(`ROOT_CONTEXT_FILES` + profile의
    `branching.worktree_setup.copy`). 그 이름이 gitignored인 저장소에서는 — 이 저장소의
    `CLAUDE.md`가 그렇다 — kit이 스스로 심은 파일 하나 때문에 모든 worktree가 영구히
    정리 불가가 된다.

    판정 기준은 이름이 아니라 내용이다. 여기서 예외를 받은 경로는 checkout과 함께
    지워지므로, 이름만 보고 통과시키면 사용자가 worktree에서 고친 `local.properties`가
    조용히 사라진다. 한 바이트라도 다르면 사용자 작업으로 보고 그대로 막는다.

    leader 쪽 파일이 없거나 symlink면 비교할 정본이 없으니 예외도 없다. checkout 쪽
    경로가 symlink 구성요소를 거치면 읽은 내용이 그 checkout의 것이 아니므로 같은
    이유로 예외를 주지 않는다.
    """
    owned: set[str] = set()
    leader_base = Path(os.path.normpath(str(root)))
    checkout_base = Path(os.path.normpath(str(checkout)))
    for name in _declared_worktree_copy_names(root):
        try:
            source = _worktree_setup_path(leader_base, name)
            target = _worktree_setup_path(checkout_base, name)
        except ValueError:
            # 저장소 밖을 가리키는 선언은 복사 쪽에서도 거부됐다. 심지 않은 파일이다.
            continue
        try:
            if target.is_symlink() or not target.is_file():
                continue
            if _has_symlinked_component(checkout_base, target):
                continue
            if (
                source.is_symlink()
                or _has_symlinked_component(leader_base, source)
                or not source.is_file()
            ):
                continue
            if source.read_bytes() != target.read_bytes():
                continue
        except OSError:
            continue
        owned.add(target.relative_to(checkout_base).as_posix())
    return owned


def _declared_worktree_copy_names(root: Path) -> tuple[str, ...]:
    """leader에 적용된 profile이 새 checkout으로 복사하라고 선언한 이름들.

    복사한 쪽(`copy_declared_worktree_files` 호출자)과 **같은 선언**을 봐야 한다.
    profile을 읽지 못하면 후보가 루트 컨텍스트 파일로 좁아질 뿐이다 — 내용 동일성
    판정은 그대로이므로 넓게 여는 실패가 아니다.
    """
    names: list[str] = list(ROOT_CONTEXT_FILES)
    # profile 선택 규칙은 소비자와 같다: `AGENT_FLOW_PROFILE`이 최우선이다.
    forced = os.environ.get("AGENT_FLOW_PROFILE")
    try:
        payloads = [
            load_profile_payload(profile_id, root, fallback_unknown_to_generic=True)
            for profile_id in ([forced] if forced else active_profile_ids(root))
        ]
    except (OSError, ValueError, yaml.YAMLError):
        # profile을 못 읽는 것은 정리를 막을 이유가 아니다. 읽지 못하는 방법은
        # 파일 접근 실패(OSError)와 파싱/스키마 실패(YAMLError, ValueError)뿐이다.
        return tuple(names)
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for name in declared_worktree_copies(payload):
            if name not in names:
                names.append(name)
    return tuple(names)


def _tracked_in_checkout(*, checkout: Path, rel: str) -> bool:
    return git_safe(
        "ls-files", "--error-unmatch", "--", rel, cwd=checkout, optional_locks=False
    ).ok


def _retire_provisioned_host_hook_registrations(*, root: Path, checkout: Path) -> None:
    """정리 직전에 kit이 깐 등록 파일만 걷어낸다. ``root``는 leader checkout이다.

    worktree는 HEAD/base_ref에서 나오므로 그 커밋의 `.gitignore`에 `.omp/`가 없을 수
    있다. 그러면 provision한 파일이 untracked로 남아 `assert_worktree_mergeable`과
    `--force` 없는 `git worktree remove`가 그 checkout을 영구히 정리 불가로 만든다.

    저장소 공유 `info/exclude`로 가리지 않는다. 그 파일은 git common dir에 있어 leader와
    모든 worktree가 함께 쓰고, 루트 고정 패턴은 **leader 루트의 같은 경로에도** 걸린다.
    `.claude/settings.json`은 installer가 사용자 내용에 병합해 넣는 사용자 파일이므로
    `status --worktree` 한 번으로 leader의 그 파일이 `git status`에서 사라지고
    `git add`가 거부된다 — worktree를 지워도 남는다.

    사용자 파일과 사용자가 손댄 등록은 남겨서 그대로 dirty로 걸리게 둔다. 정리가 막히는
    것이 이 게이트의 목적이다.
    """
    for rel in sorted(_kit_owned_host_hook_registrations(root=root, checkout=checkout)):
        target = checkout / rel
        try:
            target.unlink()
        except FileNotFoundError:
            # 대소문자 무구분 FS에서 `.Codex/hooks.json`과 `.codex/hooks.json`은 같은
            # 파일이라 두 번째 unlink가 여기로 온다. 이미 없는 것은 걷어낸 것이다.
            pass
        except OSError as exc:
            print(
                f"warning: cannot retire the provisioned host hook registration {target}:"
                f" {exc}; cleanup will refuse to remove this checkout",
                file=sys.stderr,
            )
            continue
        _prune_empty_host_hook_parents(checkout=checkout, target=target)


def _prune_empty_host_hook_parents(*, checkout: Path, target: Path) -> None:
    """등록 파일만 지우면 빈 `.omp/`가 남아 `?? .omp/`로 정리가 그대로 막힌다.

    `rmdir`은 비어 있지 않으면 실패하므로 사용자 잔재를 지울 수 없다 — 거기서 멈춘다.
    """
    for parent in target.parents:
        if parent == checkout or checkout not in parent.parents:
            return
        try:
            parent.rmdir()
        except OSError:
            return


def _remove_journal_checkout(
    *,
    root: Path,
    journal_path: Path,
    journal: dict[str, Any],
    target_branch: str,
) -> None:
    path = Path(journal["checkout"]["path"])
    registered = _registered_at_path(root=root, path=path)
    if not path.exists() and registered is None:
        return
    _assert_cleanup_owner_active(
        journal_path=journal_path,
        journal=journal,
    )
    _record_integration_proof(
        root=root,
        journal=journal,
        target_branch=target_branch,
    )
    journal["updated_at"] = _utc_now()
    _write_cleanup_journal(journal_path, journal)
    # `--force` 없는 `git worktree remove`는 untracked 파일 하나에도 거부한다. 우리가
    # 깐 등록만 걷어내고 사용자 잔재는 남긴다.
    _retire_provisioned_host_hook_registrations(root=root, checkout=path)
    _run_git(root, "worktree", "remove", str(path))
    if path.exists() or _registered_at_path(root=root, path=path) is not None:
        raise CleanupBlockedError(
            f"checkout removal did not retire registration and path: {path}"
        )


def _delete_journal_branch(
    *,
    root: Path,
    journal_path: Path,
    journal: dict[str, Any],
    target_branch: str,
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
    target_oid = _refresh_integration_target(
        root=root,
        journal=journal,
        target_branch=target_branch,
    )
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


def _load_cleanup_journal(
    path: Path,
    *,
    require_checkout: bool = True,
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CleanupBlockedError(
            f"cleanup journal is missing or unreadable at {path}"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != CLEANUP_JOURNAL_VERSION
        or payload.get("status")
        not in {"cleanup_pending", "steps_complete", "complete", "aborted"}
        or not _valid_cleanup_repository_identity(payload.get("repository"))
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
    checkout = payload.get("checkout")
    if require_checkout and (
        not isinstance(checkout, dict)
        or not isinstance(checkout.get("registration_identity"), str)
        or not checkout["registration_identity"]
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
def _checkout_activity_lease(root: Path, checkout_path: Path) -> Iterator[None]:
    lock_path = _checkout_lease_path(root, checkout_path)
    try:
        with shared_file_lease(lock_path):
            yield
    except FileLeaseUnavailable as exc:
        raise CleanupBlockedError(
            f"checkout {checkout_path} is being retired or its lease is unsafe; "
            "preserving checkout"
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


def _configured_remote_names(root: Path) -> list[str]:
    remotes = git_safe("remote", cwd=root, optional_locks=False)
    if not remotes.ok:
        return []
    names = sorted({name for name in remotes.stdout.splitlines() if name})
    if "origin" in names:
        names.remove("origin")
        names.insert(0, "origin")
    return names


def _branch_ref_candidates(*, root: Path, branch: str) -> Iterator[str]:
    yield f"refs/heads/{branch}"
    for remote in _configured_remote_names(root):
        yield f"refs/remotes/{remote}/{branch}"


def _fetch_remote_branch_ref(
    *, root: Path, remote: str, branch: str, ref: str
) -> bool:
    ref_existed = _git_commit_ref_exists(root=root, ref=ref)

    def fetch() -> bool:
        result = git_safe(
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            "--",
            remote,
            f"+refs/heads/{branch}:{ref}",
            cwd=root,
            timeout_s=GIT_WORKTREE_TIMEOUT_S,
            optional_locks=False,
        )
        if result.ok:
            return True
        detail = f"{result.stderr}\n{result.stdout}"
        if is_git_lock_contention(detail):
            if not ref_existed and _git_commit_ref_exists(root=root, ref=ref):
                return True
            raise WorktreeIsolationError(detail.strip())
        return False

    try:
        return with_git_lock_retry(
            fetch,
            is_retryable=lambda exc: isinstance(exc, WorktreeIsolationError)
            and is_git_lock_contention(str(exc)),
        )
    except WorktreeIsolationError:
        return not ref_existed and _git_commit_ref_exists(root=root, ref=ref)


def _refresh_remote_branch_ref(*, root: Path, ref: str, branch: str) -> None:
    if ref == f"refs/heads/{branch}":
        return
    for remote in _configured_remote_names(root):
        if ref != f"refs/remotes/{remote}/{branch}":
            continue
        if not _fetch_remote_branch_ref(
            root=root,
            remote=remote,
            branch=branch,
            ref=ref,
        ):
            raise CleanupBlockedError(
                f"cannot refresh cleanup target from remote {remote};"
                " preserving checkout"
            )
        return
    raise CleanupBlockedError(
        "cleanup remote target is not configured; preserving checkout"
    )


def _default_base_ref(root: Path) -> str:
    declared = _profile_base_ref(root)
    if declared:
        return declared
    for branch in ("main", "master", "develop"):
        for ref in _branch_ref_candidates(root=root, branch=branch):
            if _git_commit_ref_exists(root=root, ref=ref):
                return ref
    return "HEAD"


def _profile_base_ref(root: Path) -> str:
    """Resolve the profile-declared base ref when the repository can prove it.

    An explicit project/forced profile is a contract, so an unavailable base fails closed.
    An auto-detected profile is only a default and may fall through to the repository's
    conventional branch names.
    """
    forced_profile = os.environ.get("AGENT_FLOW_PROFILE")
    try:
        explicit_profile = bool(forced_profile) or (
            root / ".agent-flow" / "kit.json"
        ).is_file()
        fallback_unknown = bool(forced_profile) or (
            os.environ.get("AGENT_FLOW_FALLBACK_GENERIC") == "1"
        )
        profile_ids = [forced_profile] if forced_profile else active_profile_ids(root)
        payloads = [
            load_profile_payload(
                profile_id,
                root,
                fallback_unknown_to_generic=fallback_unknown,
            )
            for profile_id in profile_ids
        ]
    except (OSError, ValueError, yaml.YAMLError):
        # profile을 못 읽는 것은 worktree를 못 만들 이유가 아니다. 이름 목록으로 내려간다.
        return ""
    for payload in payloads:
        branching = payload.get("branching") if isinstance(payload, dict) else None
        if not isinstance(branching, dict):
            continue
        declared = branching.get("base")
        if not isinstance(declared, str):
            continue
        declared = declared.strip()
        if not declared or declared.startswith("-") or declared.split() != [declared]:
            continue
        local_ref = f"refs/heads/{declared}"
        if _git_commit_ref_exists(root=root, ref=local_ref):
            return local_ref
        for remote in _configured_remote_names(root):
            ref = f"refs/remotes/{remote}/{declared}"
            if _git_commit_ref_exists(root=root, ref=ref):
                return ref
            if _fetch_remote_branch_ref(
                root=root,
                remote=remote,
                branch=declared,
                ref=ref,
            ) and _git_commit_ref_exists(root=root, ref=ref):
                return ref
        if explicit_profile:
            raise WorktreeIsolationError(
                f"profile base branch is unavailable: {declared}"
            )
        return ""
    return ""


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
    # 등록이 없는 이름이다. 잔재는 예전 자리에 있을 수 있으므로 실제로 있는 자리를
    # 보고한다 — 현재 자리만 보면 정리가 없는 경로를 지웠다고 출력한다.
    checkout_path = existing_checkout_path(root=root, name=plan.name)
    return WorktreeStatus(
        name=plan.name,
        branch=manifest_branch or plan.branch,
        path=checkout_path,
        exists=checkout_path.exists(),
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
        validate_git_branch(branch)
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
    if manifest.exists():
        return _read_manifest(manifest)
    for legacy in _in_checkout_manifest_paths(root=root, key=key):
        if legacy.exists():
            return _read_manifest(legacy)
    return None


def _in_checkout_manifest_paths(*, root: Path, key: str) -> tuple[Path, ...]:
    """checkout 안에 manifest를 두던 시절의 안전한 자리들을 모두 본다.

    현재 자리만 보면 업그레이드 전 checkout의 branch ownership과 base를 잃는다.
    symlink layout은 manifest 조회·삭제가 외부 경로로 빠지므로 제외한다.
    """
    return tuple(
        candidate / key / "manifest.json"
        for candidate in _trusted_existing_creation_layout_roots(root)
    )


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
        validate_git_branch(value)
    except ValueError:
        return None
    return value


def _manifest_string(payload: dict | None, key: str) -> str | None:
    value = payload.get(key) if payload else None
    return value if isinstance(value, str) and value else None


def _manifest_oid(payload: dict | None, key: str) -> str | None:
    value = _manifest_string(payload, key)
    return value if value is not None and _is_oid(value) else None


def _manifest_path_value(*, root: Path, path: Path) -> str:
    # 관리 루트 안 checkout은 leader 기준 상대경로로 남긴다(설치본을 옮겨도 유효).
    # 관리 루트 밖 checkout은 상대화가 불가능하므로 절대경로를 쓴다 — 읽는 쪽
    # (`_metadata_belongs_to_path`)이 이미 두 형태를 모두 해석한다.
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(real_path(path))


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
        "path": _manifest_path_value(root=root, path=status.path),
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
    # 현재 중앙 자리와 이전 외부·내부 자리를 모두 보되 symlink나 교체 가능한
    # layout root는 따라가지 않는다.
    for checkout_root in _trusted_existing_creation_layout_roots(root):
        names.update(path.name for path in checkout_root.iterdir() if path.is_dir())
    runtime_root = _agent_flow_git_dir(root) / "worktrees"
    if runtime_root.exists():
        names.update(path.name for path in runtime_root.iterdir() if path.is_dir())
    # 디렉터리 스캔은 agent-flow가 만든 자리만 본다. 사용자가 raw git으로 만든
    # 체크아웃은 등록부에만 있으므로 그쪽도 아는 이름의 원천으로 함께 넣는다.
    names.update(entry.path.name for entry in removable_worktrees(root=root))
    return sorted(names)


def remove_worktree_metadata(
    *,
    root: Path,
    name: str,
    path: Path | None = None,
    force: bool = False,
) -> bool:
    """``name``의 런타임 메타데이터를 지운다. 실제로 지웠으면 True.

    ``path``를 주면 그 등록 경로의 소유임을 증명한 경우에만 지운다. 정규화된 키는
    서로 다른 등록 경로를 한 자리로 접는다 — 관리형 ``.../feat-demo``와 외부
    ``.../demo``가 둘 다 ``feat-demo``다. 증명 없이 지우면 외부 worktree를
    제거하다가 관리형 worktree의 활성 run과 manifest를 날린다.

    증명과 삭제는 **런타임 상태를 실제로 쌓는 같은 키**로 한다
    (`worktree_runtime_root`). 증명만 정규화하면 ``feat-issue#110``의 소유 판정을
    형제 ``feat-issue-110``의 manifest가 대신 내려 먼저 False가 되고, 대상 자신의
    죽은 active 마커가 남아 같은 자리의 다음 run을 `already active`로 막는다.

    ``force``는 증명을 건너뛰지만 **양쪽 경로가 모두 없을 때만** 그렇게 한다.
    다른 머신에서 복사돼 온 manifest처럼 이 머신의 어느 layout에도 속하지 않는
    기록은 증명이 영구히 실패해서, 그것 없이는 지울 방법이 없다. 살아 있는
    checkout의 상태는 force로도 지우지 않는다.
    """
    try:
        key = _runtime_state_key(root=root, name=name)
    except ValueError:
        # agent-flow 이름 규칙으로 정규화되지 않는 이름에는 애초에 메타데이터가 없다.
        return False
    if path is not None and not _metadata_belongs_to_path(root=root, key=key, path=path):
        if not (force and _metadata_paths_are_absent(root=root, key=key, path=path)):
            return False
    runtime_root = _runtime_state_root(root=root, name=key)
    removed = False
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
        removed = True
    for legacy_manifest in _in_checkout_manifest_paths(root=root, key=key):
        if legacy_manifest.exists():
            legacy_manifest.unlink()
            removed = True
    forget_adopted_checkout(root=root, name=key)
    return removed


def worktree_metadata_is_unreachable(*, root: Path, name: str, path: Path) -> bool:
    """이 이름의 런타임 메타데이터가 이 머신에서 소유 증명이 **영구히** 불가능한가.

    다른 머신에서 복사돼 온 기록처럼 양쪽 경로가 다 없으면 증명이 통과할 날이 오지
    않는다. 그런 기록은 `--force-metadata` 없이는 지울 방법이 없으므로 호출자가
    그 사실을 알려야 한다. 살아 있는 자리를 지키느라 보존한 경우는 여기 들지 않는다.
    """
    try:
        key = _runtime_state_key(root=root, name=name)
    except ValueError:
        return False
    if _metadata_belongs_to_path(root=root, key=key, path=path):
        return False
    return _metadata_paths_are_absent(root=root, key=key, path=path)


def _metadata_paths_are_absent(*, root: Path, key: str, path: Path) -> bool:
    """기록된 경로와 대상 경로가 **둘 다** 디스크에 없는가."""
    if path.exists():
        return False
    payload = _state_key_manifest(root=root, key=key)
    if payload is None:
        return True
    recorded = payload.get("path")
    if not isinstance(recorded, str) or not recorded:
        return True
    candidate = Path(recorded)
    if not candidate.is_absolute():
        candidate = root / candidate
    return not candidate.exists()


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
    manifest를 쓰기 전에 정리해야 하는 경우가 그 하나다. 현재·이전 생성 자리를
    모두 봐야 이전 자리 잔재의 런타임 메타데이터도 함께 정리된다.
    """
    managed_paths = tuple(
        candidate / key
        for candidate in _safe_creation_layout_roots(root, include_missing=True)
    )
    payload = _state_key_manifest(root=root, key=key)
    if payload is None:
        return any(same_worktree_path(candidate, path) for candidate in managed_paths)
    recorded = payload.get("path")
    if not isinstance(recorded, str) or not recorded:
        return False
    candidate = Path(recorded)
    if not candidate.is_absolute():
        candidate = root / candidate
    if same_worktree_path(candidate, path):
        return True
    # checkout이 사라진 뒤 layout이 legacy 자리에서 현재 자리로 바뀌어도 같은 key의
    # 두 관리 경로끼리는 소유권을 이어 간다. 존재하는 다른 경로는 절대 신뢰하지 않는다.
    return (
        not candidate.exists()
        and not path.exists()
        and any(same_worktree_path(candidate, managed) for managed in managed_paths)
        and any(same_worktree_path(path, managed) for managed in managed_paths)
    )


def _conflicting_metadata_hint(*, root: Path, status: WorktreeStatus) -> str:
    try:
        key = _runtime_state_key(root=root, name=status.name)
    except ValueError:
        return "the registry key for that name is unusable"
    runtime_root = _runtime_state_root(root=root, name=key)
    payload = _state_key_manifest(root=root, key=key)
    recorded = payload.get("path") if isinstance(payload, dict) else None
    owner = f" and records {recorded}" if isinstance(recorded, str) and recorded else ""
    return (
        f"registry key {runtime_root} belongs to another checkout{owner}; "
        "remove that metadata directory to free the name"
    )


def leader_dirty_paths(root: Path) -> tuple[str, ...]:
    """leader에 남아 있는 미커밋 작업의 status 레코드.

    "dirty"의 정의를 두 벌로 두지 않기 위해 진입 게이트(`_git_dirty`)와 경고가
    같은 값을 본다. 둘이 갈리면 게이트는 통과시키고 경고는 침묵하는 조합이 생기고,
    그 조합에서 사용자는 자기 작업이 보호 대상 밖이라는 사실을 끝까지 모른다.
    """
    # 관측이다. status는 기본적으로 index를 refresh하며 index.lock을 잡으므로
    # 동시에 도는 워커의 실제 쓰기와 경합을 만든다.
    result = git_safe(
        "status", "--porcelain", cwd=root, timeout_s=GIT_WORKTREE_TIMEOUT_S, optional_locks=False
    )
    if not result.ok:
        raise subprocess.CalledProcessError(
            result.returncode or 1, result.args, output=result.stdout, stderr=result.stderr
        )
    return tuple(
        line
        for line in result.stdout.splitlines()
        if not _is_agent_flow_status_line(line)
    )


def _git_dirty(root: Path) -> bool:
    return bool(leader_dirty_paths(root))


def user_worktrees_root() -> Path:
    """사용자 계정에 속한 저장소별 worktree 컨테이너."""
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".agent-flow"
    if not base.is_absolute():
        raise WorktreeIsolationError(f"XDG_STATE_HOME must be absolute: {base}")
    return base / "worktrees"


def managed_worktrees_root(root: Path) -> Path:
    """새 checkout이 태어나는 사용자 전용 중앙 자리.

    leader 안에 두면 IDE가 worker 변경에 반응해 leader 캐시를 건드릴 수 있다. 외부
    격리는 유지하되 저장소별 checkout은 ``~/.agent-flow/worktrees/<repo-id>`` 아래
    모아 leader 옆에 관리 폴더가 늘어나지 않게 한다.
    """
    return user_worktrees_root() / _repository_worktree_id(root)


def sibling_managed_root(root: Path) -> Path:
    """직전 기본 자리. 이미 만들어진 checkout이 있으므로 계속 인식한다."""
    repository_root = _repository_checkout_root(root)
    return repository_root.parent / f"{repository_root.name}.worktrees"


def legacy_managed_root(root: Path) -> Path:
    """최초 내부 기본 자리. 이미 만들어진 checkout이 있으므로 계속 인식한다."""
    return _repository_checkout_root(root) / ".agent-flow" / "worktrees"


def _repository_checkout_root(root: Path) -> Path:
    common_dir = _git_common_dir(root)
    return common_dir.parent if common_dir.name == ".git" else real_path(root)


def _repository_worktree_id(root: Path) -> str:
    common_dir = _git_common_dir(root)
    repository_name = common_dir.parent.name if common_dir.name == ".git" else common_dir.stem
    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "-", repository_name).strip("._-")
    safe_name = safe_name[:64] or "repository"
    digest = hashlib.sha256(worktree_path_key(common_dir).encode("utf-8")).hexdigest()[:12]
    return f"{safe_name}-{digest}"


def _creation_layout_roots(root: Path) -> tuple[Path, ...]:
    return (
        managed_worktrees_root(root),
        sibling_managed_root(root),
        legacy_managed_root(root),
    )


def _safe_creation_layout_roots(
    root: Path, *, include_missing: bool = False
) -> tuple[Path, ...]:
    safe: dict[str, Path] = {}
    for candidate in _creation_layout_roots(root):
        try:
            candidate.lstat()
        except FileNotFoundError:
            accepted = include_missing and _trusted_parent_dir(candidate.parent)
        except OSError:
            accepted = False
        else:
            accepted = _safe_creation_root(candidate) and _trusted_parent_dir(
                candidate.parent
            )
        if accepted:
            safe.setdefault(worktree_path_key(candidate), candidate)
    return tuple(safe.values())


def _trusted_existing_creation_layout_roots(root: Path) -> tuple[Path, ...]:
    return _safe_creation_layout_roots(root)


def _trusted_parent_dir(path: Path) -> bool:
    """이 디렉터리 안의 항목을 우리만 갈아끼울 수 있는가.

    rename·삭제 권한은 항목이 아니라 **부모**가 준다. 그래서 소유자와 쓰기 비트를
    본다: 우리(또는 root) 소유여야 하고, group/other가 쓸 수 있으면 sticky bit로
    남의 항목 교체가 막혀 있어야 한다(`/tmp`가 그 형태다).
    """
    if not (os.access(path, os.W_OK | os.X_OK) and _is_real_directory(path)):
        return False
    try:
        info = path.lstat()
    except OSError:
        return False
    getuid = getattr(os, "getuid", None)
    if getuid is not None and info.st_uid not in (0, getuid()):
        return False
    shared_write = info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    return not shared_write or bool(info.st_mode & stat.S_ISVTX)


def _is_real_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _safe_creation_root(path: Path) -> bool:
    """``path``를 생성 루트로 써도 되는가. 없으면 만들 수 있으니 참이다.

    ``lstat``으로 본다. symlink는 자체가 목적지를 감추고, 남의 소유 디렉터리는 우리가
    만든 것이 아니다. 둘 다 checkout을 우리 통제 밖으로 옮긴다.

    소유권 검사는 POSIX 전용이다. native Windows Python에는 `os.getuid`가 없어서
    무조건 부르면 첫 create 이후의 모든 조회가 AttributeError로 죽는다. 그쪽에서는
    symlink·디렉터리 검사만 남는다.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return False
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return True
    return info.st_uid == getuid()


def _ensure_creation_root(path: Path) -> None:
    """checkout 부모를 no-follow로 만들고 사용자 전용 권한을 강제한다."""
    central_root = user_worktrees_root()
    if worktree_path_key(path.parent) == worktree_path_key(central_root):
        for component in (central_root.parent, central_root, path):
            _ensure_private_directory(component)
        return
    try:
        path.mkdir(parents=True, mode=0o700)
        return
    except FileExistsError:
        pass
    if not _safe_creation_root(path):
        raise WorktreeIsolationError(
            f"refusing to create a worktree under an unsafe parent: {path}; "
            "it is a symlink, not a directory, or not owned by this user"
        )
    _harden_creation_root(path)


def _ensure_private_directory(path: Path) -> None:
    if not path.is_absolute():
        raise WorktreeIsolationError(f"private worktree directory must be absolute: {path}")
    if not path.exists():
        parent = path.parent
        if not parent.exists():
            _ensure_private_directory(parent)
        if not _trusted_parent_dir(parent):
            raise WorktreeIsolationError(
                f"refusing to create a worktree under an unsafe parent: {parent}"
            )
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    if not _safe_creation_root(path) or not _trusted_parent_dir(path.parent):
        raise WorktreeIsolationError(
            f"refusing to create a worktree under an unsafe parent: {path}; "
            "it is a symlink, not a directory, not owned by this user, or replaceable"
        )
    _harden_creation_root(path)


def _harden_creation_root(path: Path) -> None:
    """이미 있는 생성 루트의 group/other 권한을 걷어낸다.

    예전 버전이 umask 그대로 만든 자리가 남아 있을 수 있다. 소유는 이미 증명된
    상태이므로(위 검사) 여기서 권한만 좁힌다.
    """
    try:
        info = path.stat()
    except OSError:
        return
    exposed = info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    if not exposed:
        return
    try:
        os.chmod(path, info.st_mode & ~exposed)
    except OSError:
        # 권한을 못 좁히면 그 사실을 감추지 않는다. 이 자리는 leader가 만든 것이고
        # 못 좁히는 상태 자체가 통제 밖이라는 뜻이다.
        raise WorktreeIsolationError(
            f"cannot restrict the worktree root to owner-only access: {path}"
        )


def _managed_checkout_path(*, root: Path, name: str) -> Path:
    return managed_worktrees_root(root) / name


def existing_checkout_path(*, root: Path, name: str) -> Path:
    """``name``의 checkout이 실제로 있는 자리. 없으면 현재 생성 자리.

    조회·정리는 현재·이전 자리를 모두 본다. 하나라도 빠지면 이전 자리의 잔재를
    "정리했다"고 출력하면서 남겨 두고, 그 잔재가 목록에 계속 다시 나타난다.
    """
    for candidate_root in _trusted_existing_creation_layout_roots(root):
        candidate = candidate_root / name
        if candidate.exists():
            return candidate
    return managed_worktrees_root(root) / name


def _runtime_state_root(*, root: Path, name: str) -> Path:
    return _agent_flow_git_dir(root) / "worktrees" / name


def _worktree_manifest_path(*, root: Path, name: str) -> Path:
    return _runtime_state_root(root=root, name=_feature_worktree_name(name)) / "manifest.json"


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


def _register_git_exclude(leader: Path, *patterns: str) -> None:
    """`node_modules/`처럼 슬래시로 끝나는 항목은 디렉터리만 맞아서 symlink 자체를
    못 가린다. 루트 고정 패턴을 따로 적어야 worktree 정리가 막히지 않는다.

    worktree를 지울 때 이 항목은 지우지 않는다. 저장소가 공유하는 파일이라 다른
    worktree가 아직 같은 symlink를 쓰고 있을 수 있다. 추적 중인 경로는 exclude로
    가려지지 않으므로 남겨도 손해가 없다.

    그래서 사용자가 커밋할 수도 있는 경로는 여기 올리지 않는다. 이 파일은 git common
    dir에 있어 leader 루트의 같은 경로까지 영구히 숨긴다 — host hook 등록이 그래서
    빠졌고, 남은 것은 누구나 ignore하는 빌드 산출물뿐이다.
    """
    # leader가 그 자체로 linked worktree면 `.git`은 디렉터리가 아니라 파일이다.
    common = Path(_run_git(leader, "rev-parse", "--git-common-dir").stdout.strip())
    if not common.is_absolute():
        common = leader / common
    exclude = common / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    known = set(current.split())
    missing = [pattern for pattern in patterns if pattern not in known]
    if not missing:
        return
    prefix = "" if not current or current.endswith("\n") else "\n"
    exclude.write_text(
        current + prefix + "".join(f"{pattern}\n" for pattern in missing), encoding="utf-8"
    )


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


# 두 파일은 프로젝트가 커밋할 수도, gitignore할 수도 있다(예전 install이 넣어 둔 항목이
# 그대로 남아 있는 프로젝트도 많다). 추적하지 않는 쪽이면 `git worktree add`가 가져올
# 것도, worktree 안 install(= no-op)이 만들 것도 없어서 그 checkout에서 연 host 세션은
# agent-flow 계약을 한 글자도 받지 못한다. leader에서 복사해 그 공백을 닫는다.
ROOT_CONTEXT_FILES = ("AGENTS.md", "CLAUDE.md")


def declared_worktree_copies(profile: Mapping[str, Any]) -> list[str]:
    """새 checkout이 받아야 할 이름: 루트 컨텍스트 파일 + 단일/합성 profile 선언.

    `profile_resolution.load_profile_union`이 만드는 합성 dict에는 최상위 `branching`이 없다 —
    개별 profile은 `profiles` 아래에 들어가고 최상위에는 `review_angles`/`gates`/
    `skills`/`architecture`만 합쳐진다. 최상위만 보면 android+react-native처럼
    profile이 둘 이상인 프로젝트에서 선언이 조용히 빈 목록이 되고, `local.properties`가
    영영 복사되지 않는다.

    복사하는 쪽과 정리 쪽이 같은 목록을 봐야 한다. 두 벌을 두면 한쪽만 늘어난 순간
    kit이 심은 파일이 정리 차단 사유가 된다.

    `copy`는 schema가 목록으로 선언한 자리지만(`profiles/_schema.yaml`) 여기 들어오는
    payload는 검증되지 않은 YAML이다. `copy: local.properties` 한 줄이면 스칼라 문자열이
    되고, 순회하면 `l`/`o`/`c`/`.` 같은 **문자**가 파일 이름으로 풀린다. 그 목록은
    복사(`copy_declared_worktree_files`)와 정리 예외(`_kit_copied_worktree_files`)가
    함께 쓰므로, 잘못된 형태를 통과시키면 선언한 설정은 복사되지 않고 한 글자 이름이
    정리 예외 후보로 새어 들어간다.

    그래서 형태가 틀린 선언은 예외를 올리지 않고 **버리고 계속한다** — 같은 모듈의
    `_declared_worktree_copy_names`와 `core/local_skills.resolved_profile`이 profile
    읽기 실패를 다루는 방향이 그렇다. 여기서 raise하면 worktree 생성과 정리가 profile
    한 줄에 막힌다. 대신 무엇을 버렸는지 stderr로 남긴다. 조용히 빈 목록이 되면
    `local.properties` 누락의 원인을 아무도 짚지 못한다.
    """
    sources: list[Mapping[str, Any]] = [profile]
    nested = profile.get("profiles")
    if isinstance(nested, list):
        sources.extend(item for item in nested if isinstance(item, dict))
    names: list[str] = list(ROOT_CONTEXT_FILES)
    for source in sources:
        branching = source.get("branching")
        if not isinstance(branching, dict):
            continue
        setup = branching.get("worktree_setup")
        if not isinstance(setup, dict):
            continue
        declared = setup.get("copy")
        if declared is None:
            continue
        # str/bytes도 순회 가능하다. 그래서 Iterable이 아니라 목록인지를 본다.
        if not isinstance(declared, (list, tuple)):
            print(
                "warning: ignored branching.worktree_setup.copy: expected a list of file "
                f"names, got {type(declared).__name__} ({declared!r})",
                file=sys.stderr,
            )
            continue
        for name in declared:
            if not isinstance(name, str):
                print(
                    "warning: ignored branching.worktree_setup.copy entry: expected a "
                    f"file name, got {type(name).__name__} ({name!r})",
                    file=sys.stderr,
                )
                continue
            if name not in names:
                names.append(name)
    return names


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


# host 세션(Claude/Codex/OMP)이 managed hook을 찾는 등록 파일들.
# installer는 leader checkout에만 이 파일을 심는다.
#
# 목록은 `hook_integrity`에서 가져온다. 같은 언어 안에 두 벌을 두면 새 host를 한쪽에만
# 추가했을 때 등록 검증은 도는데 worktree에는 그 파일이 깔리지 않아 이 버그가 그 host에서
# 그대로 되살아난다. `hook_integrity`는 표준 라이브러리만 임포트하므로 순환이 없다.
HOST_HOOK_REGISTRATION_FILES: tuple[str, ...] = tuple(
    path.as_posix() for path in (*JSON_REGISTRATION_FILES, OMP_REGISTRATION_FILE)
)
_OMP_REGISTRATION_REL = OMP_REGISTRATION_FILE.as_posix()


def _folded_parent_dirs(rels: Iterable[str]) -> frozenset[str]:
    """git이 접어서 보고할 수 있는 상위 디렉터리 레코드들(`.claude/`, `.omp/` …)."""
    return frozenset(
        "/".join(rel.split("/")[:depth]) + "/"
        for rel in rels
        for depth in range(1, len(rel.split("/")))
    )


_HOST_HOOK_REGISTRATION_DIRS: frozenset[str] = _folded_parent_dirs(
    HOST_HOOK_REGISTRATION_FILES
)

# kit이 생성한 OMP 확장의 표지와 생성 서명. `lib/omp-hooks-extension.mjs`의
# `OMP_EXTENSION_MARKER`와 `lib/installer-shared.mjs`의 `ompExtensionIsKitOwned`가
# 쓰는 것과 같은 기준이다 — 같은 파일의 소유권을 두 곳이 다르게 판정하면 한쪽은 덮고
# 다른 쪽은 남겨서 checkout과 leader의 등록이 영구히 어긋난다. 표지는 이번 버전부터
# 붙으므로 그 이전 설치본을 위해 생성 서명도 함께 인정한다.
_OMP_EXTENSION_MARKER = "agent-flow: managed omp extension"
_OMP_EXTENSION_SIGNATURE = "export default function agentFlowHooks("
# provision skip 사유와 tracked 판정을 기억하는 파일 이름. checkout의 private git admin
# 디렉터리에 두므로 worktree를 dirty로 만들지 않고 `git worktree remove`가 함께 지운다.
_HOST_HOOK_STATE_NAME = "agent-flow-host-hooks.json"


@dataclass
class _HostHookProvisionState:
    """provision 호출 사이에 남는 기억.

    `status`/`continue`가 매 턴 이 경로를 탄다. 사유를 기억하지 않으면 고칠 수 없는
    구성에서 같은 경고가 끝없이 쌓이고, tracked 판정을 기억하지 않으면 구조적으로
    skip될 수밖에 없는 경로마다 `git ls-files`가 호출당 하나씩 상시 spawn된다.
    """

    path: Path | None
    skipped: dict[str, str]
    tracked: dict[str, bool]
    index_identity: str
    dirty: bool = False
    retirable: set[str] | None = None


def provision_registered_worktree_host_hooks(
    *, root: Path
) -> tuple[tuple[Path, tuple[str, ...]], ...]:
    """현재 등록된 managed/adopted checkout의 host hook 등록을 leader와 맞춘다."""
    synced: list[tuple[Path, tuple[str, ...]]] = []
    with worktree_creation_lock(root):
        registered_worktrees = tuple(
            registered
            for registered in removable_worktrees(root=root)
            if (
                _is_managed_child(root=root, path=registered.path)
                or _adopted(root=root, path=registered.path)
            )
        )
        if registered_worktrees and not _DIR_FD_SUPPORTED:
            raise WorktreeIsolationError(
                "registered worktree hook sync requires secure dir-fd operations"
            )
        for registered in registered_worktrees:
            if registered.prunable:
                continue
            if not registered.registration_identity:
                raise WorktreeIsolationError(
                    f"worktree registration identity is unavailable: {registered.path}"
                )
            try:
                before = registered.path.lstat()
            except FileNotFoundError:
                continue
            if registered.path.is_symlink() or not stat.S_ISDIR(before.st_mode):
                raise WorktreeIsolationError(
                    f"worktree path is not a trusted directory: {registered.path}"
                )
            checkout = verify_linked_worktree(
                root=root,
                path=registered.path,
                expected_branch=registered.branch,
                managed_root=registered.path.parent,
            )
            after = checkout.lstat()
            checkout_identity = (before.st_dev, before.st_ino)
            if (after.st_dev, after.st_ino) != checkout_identity:
                raise WorktreeIsolationError(
                    f"worktree path changed during hook sync: {checkout}"
                )
            _assert_worktree_registration_identity(
                root=root,
                checkout=checkout,
                expected=registered.registration_identity,
            )
            written = provision_host_hook_registrations(
                leader=root,
                checkout=checkout,
                expected_registration_identity=registered.registration_identity,
                expected_checkout_identity=checkout_identity,
            )
            synced.append((checkout, written))
    return tuple(synced)


def _assert_worktree_registration_identity(
    *, root: Path, checkout: Path, expected: str
) -> None:
    current = registered_worktree_at(root, checkout)
    if (
        current is None
        or current.prunable
        or current.registration_identity != expected
    ):
        raise WorktreeIsolationError(
            f"worktree registration changed during hook sync: {checkout}"
        )


def _assert_checkout_path_identity(
    *, checkout: Path, expected: tuple[int, int] | None
) -> None:
    if expected is None:
        raise WorktreeIsolationError("worktree checkout identity is unavailable")
    try:
        current = checkout.lstat()
    except OSError as exc:
        raise WorktreeIsolationError(
            f"worktree path changed during hook sync: {checkout}"
        ) from exc
    if (
        checkout.is_symlink()
        or not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != expected
    ):
        raise WorktreeIsolationError(
            f"worktree path changed during hook sync: {checkout}"
        )


def provision_host_hook_registrations(
    *,
    leader: Path,
    checkout: Path,
    expected_registration_identity: str | None = None,
    expected_checkout_identity: tuple[int, int] | None = None,
) -> tuple[str, ...]:
    """leader의 host hook 등록 파일을 managed checkout에도 깐다.

    등록 파일이 leader에만 있으면 worktree checkout을 cwd로 연 host 세션에는
    보호 브랜치와 worktree 경계 hook이 등록되지 않는다.

    등록 안의 command는 leader 절대경로를 가리키므로 파일을 그대로 복사하면 된다.
    leader 파일은 읽기만 한다. 내용이 이미 같으면 쓰지 않으므로 반복 호출해도 무해하다.

    이미 있는 target은 kit이 깐 모양일 때만 덮는다. 사용자가 쓴 것으로 보이면 손대지
    않고 사유를 말한다 — 같은 모듈의 `copy_declared_worktree_files`가 문서화한 계약이고,
    덮으면 그 checkout의 host 설정이 백업도 경고도 없이 사라진다.

    여기서 깐 파일은 `git worktree remove` 직전에 `_retire_provisioned_host_hook_registrations`가
    걷어낸다. 저장소 공유 exclude로 가리면 leader 루트의 같은 경로까지 영구히 숨는다.

    반환값은 실제로 변경한 상대경로들이다.
    """
    leader_base = Path(os.path.normpath(str(leader)))
    checkout_base = Path(os.path.normpath(str(checkout)))
    if same_worktree_path(leader_base, checkout_base):
        return ()
    if (expected_registration_identity is None) != (
        expected_checkout_identity is None
    ):
        raise WorktreeIsolationError(
            "worktree hook sync requires both registration and checkout identities"
        )
    checkout_fd: int | None = None
    if expected_checkout_identity is not None:
        if not expected_registration_identity or not _DIR_FD_SUPPORTED:
            raise WorktreeIsolationError(
                "worktree hook sync cannot bind the registered checkout securely"
            )
        checkout_fd = os.open(
            checkout_base,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(checkout_fd)
        if (opened.st_dev, opened.st_ino) != expected_checkout_identity:
            os.close(checkout_fd)
            raise WorktreeIsolationError(
                f"worktree path changed before hook sync: {checkout_base}"
            )
        _assert_worktree_registration_identity(
            root=leader_base,
            checkout=checkout_base,
            expected=expected_registration_identity,
        )
        _assert_checkout_path_identity(
            checkout=checkout_base,
            expected=expected_checkout_identity,
        )
    state = (
        _HostHookProvisionState(
            path=None,
            skipped={},
            tracked={},
            index_identity="",
        )
        if checkout_fd is not None
        else _load_host_hook_state(checkout_base)
    )
    written: list[str] = []
    try:
        for rel in HOST_HOOK_REGISTRATION_FILES:
            if expected_registration_identity is not None:
                _assert_worktree_registration_identity(
                    root=leader_base,
                    checkout=checkout_base,
                    expected=expected_registration_identity,
                )
                _assert_checkout_path_identity(
                    checkout=checkout_base,
                    expected=expected_checkout_identity,
                )
            try:
                done, reason = _provision_one_host_hook_registration(
                    leader_base=leader_base,
                    checkout_base=checkout_base,
                    rel=rel,
                    state=state,
                    checkout_fd=checkout_fd,
                )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                # 조용한 삼킴이 이 버그의 원인이었다. 등록이 빠졌으면 사유를 말한다.
                done, reason = False, str(exc)
            if expected_registration_identity is not None:
                _assert_worktree_registration_identity(
                    root=leader_base,
                    checkout=checkout_base,
                    expected=expected_registration_identity,
                )
                _assert_checkout_path_identity(
                    checkout=checkout_base,
                    expected=expected_checkout_identity,
                )
            if done:
                written.append(rel)
            _record_host_hook_skip(
                state=state, rel=rel, checkout=checkout_base, reason=reason
            )
    finally:
        if checkout_fd is not None:
            os.close(checkout_fd)
    _save_host_hook_state(state)
    return tuple(written)


def _record_host_hook_skip(
    *, state: _HostHookProvisionState, rel: str, checkout: Path, reason: str | None
) -> None:
    """등록이 빠진 채로 조용히 넘어가면 그 checkout의 채팅 `승인`은 아무 흔적 없이
    무시된다 — 이 버그의 원래 증상이 그것이다. 사유와 해결 방법을 함께 낸다.

    같은 사유는 한 번만 낸다. `status`는 host 세션이 매 턴 돌리는 명령이라 사유가
    고쳐질 때까지 같은 줄이 무한히 쌓이면 아무도 읽지 않는다. skip이 풀리면 기억도
    지운다 — 안 지우면 같은 사유가 다시 생겨도 침묵한다.
    """
    if reason is None:
        if state.skipped.pop(rel, None) is not None:
            state.dirty = True
        return
    if state.skipped.get(rel) == reason:
        return
    state.skipped[rel] = reason
    state.dirty = True
    print(
        f"warning: host hook registration {rel} not provisioned in {checkout}: {reason}",
        file=sys.stderr,
    )


def _provision_one_host_hook_registration(
    *,
    leader_base: Path,
    checkout_base: Path,
    rel: str,
    state: _HostHookProvisionState,
    checkout_fd: int | None,
) -> tuple[bool, str | None]:
    """(변경했는가, skip 사유). 사유가 None이면 skip이 아니다 — 보고는 호출자가 한다."""
    source = _worktree_setup_path(leader_base, rel)
    target = _worktree_setup_path(checkout_base, rel)
    if _has_symlinked_component(leader_base, source):
        return False, (
            "the leader path has a symlinked component; replace it with a real file"
            " so the registration can be copied"
        )
    if checkout_fd is not None:
        return _provision_one_host_hook_registration_at(
            leader=leader_base,
            checkout=checkout_base,
            source=source,
            rel=rel,
            state=state,
            checkout_fd=checkout_fd,
        )
    if not source.is_file():
        if (
            _has_symlinked_component(checkout_base, target)
            or target.is_symlink()
            or not target.is_file()
        ):
            return False, None
        if state.retirable is None:
            state.retirable = _kit_owned_host_hook_registrations(
                root=leader_base,
                checkout=checkout_base,
            )
        if rel not in state.retirable:
            return False, None
        if not _unlink_kit_owned_host_hook_registration(
            leader=leader_base,
            checkout=checkout_base,
            rel=rel,
            checkout_fd=checkout_fd,
        ):
            return False, None
        return True, None
    if _has_symlinked_component(checkout_base, target) or target.is_symlink():
        return False, (
            "the checkout path is or goes through a symlink, so the copy would land"
            " outside the checkout; remove the symlink"
        )
    payload = source.read_bytes()
    existing = target.read_bytes() if target.is_file() else None
    if existing == payload:
        return False, None
    # tracked 판정은 git 호출이라 쓸 일이 있을 때만 묻는다. `status`/`continue`가
    # 매번 이 함수를 타므로 정상 상태에서 git을 4번 더 돌리면 그게 상시 비용이 된다.
    #
    # 프로젝트가 이 경로를 추적하면 등록 파일은 사용자 소유다. 덮으면 사용자 설정이
    # 사라지고 worktree가 dirty가 되어 정리 게이트까지 막힌다.
    if _host_hook_path_is_tracked(checkout=checkout_base, rel=rel, state=state):
        return False, "tracked by git; commit the agent-flow hook entry or untrack the file"
    if existing is not None and not _host_hook_registration_is_kit_owned(
        leader=leader_base, rel=rel, payload=existing
    ):
        return False, (
            "the checkout already has a registration this kit did not write;"
            " merge the agent-flow hook entry into it by hand or delete the file"
        )
    _write_host_hook_registration(
        checkout=checkout_base,
        target=target,
        payload=payload,
        checkout_fd=checkout_fd,
    )
    return True, None




def _provision_one_host_hook_registration_at(
    *,
    leader: Path,
    checkout: Path,
    source: Path,
    rel: str,
    state: _HostHookProvisionState,
    checkout_fd: int,
) -> tuple[bool, str | None]:
    with _locked_verified_worktree_index(
        leader=leader,
        checkout=checkout,
        checkout_fd=checkout_fd,
    ) as gitdir_fd:
        return _provision_one_host_hook_registration_at_locked(
            leader=leader,
            checkout=checkout,
            source=source,
            rel=rel,
            state=state,
            checkout_fd=checkout_fd,
            gitdir_fd=gitdir_fd,
        )


@contextmanager
def _locked_verified_worktree_index(
    *, leader: Path, checkout: Path, checkout_fd: int
):
    gitdir_fd = _open_verified_worktree_gitdir(
        leader=leader,
        checkout=checkout,
        checkout_fd=checkout_fd,
    )
    lock_fd: int | None = None
    lock_identity: tuple[int, int] | None = None
    try:
        try:
            lock_fd = os.open(
                "index.lock",
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=gitdir_fd,
            )
        except FileExistsError as exc:
            raise WorktreeIsolationError(
                f"worktree index is being changed during hook sync: {checkout}"
            ) from exc
        identity = os.fstat(lock_fd)
        lock_identity = (identity.st_dev, identity.st_ino)
        yield gitdir_fd
    finally:
        try:
            if lock_fd is not None:
                os.close(lock_fd)
            if lock_identity is not None:
                try:
                    current = os.stat(
                        "index.lock",
                        dir_fd=gitdir_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError as exc:
                    raise WorktreeIsolationError(
                        f"worktree index lock disappeared during hook sync: {checkout}"
                    ) from exc
                if (current.st_dev, current.st_ino) != lock_identity:
                    raise WorktreeIsolationError(
                        f"worktree index lock changed during hook sync: {checkout}"
                    )
                os.unlink("index.lock", dir_fd=gitdir_fd)
        finally:
            os.close(gitdir_fd)


def _open_verified_worktree_gitdir(
    *, leader: Path, checkout: Path, checkout_fd: int
) -> int:
    pointer = _read_host_hook_registration_at(parent=checkout_fd, name=".git")
    if pointer is None or not pointer[0].startswith(b"gitdir:"):
        raise WorktreeIsolationError(
            f"verified worktree git pointer is unavailable: {checkout}"
        )
    try:
        value = pointer[0].decode("utf-8").partition(":")[2].strip()
    except UnicodeDecodeError as exc:
        raise WorktreeIsolationError(
            f"verified worktree git pointer is not UTF-8: {checkout}"
        ) from exc
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = checkout / candidate
    candidate = Path(os.path.normpath(candidate))
    admin_root = _git_common_dir(leader) / "worktrees"
    if candidate.parent != admin_root or not candidate.name:
        raise WorktreeIsolationError(
            f"worktree git pointer escaped the common git dir: {checkout}"
        )
    root_fd = os.open(
        admin_root,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        gitdir_fd = os.open(
            candidate.name,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
    finally:
        os.close(root_fd)
    try:
        backlink = _read_host_hook_registration_at(
            parent=gitdir_fd,
            name="gitdir",
        )
        if backlink is None:
            raise WorktreeIsolationError(
                f"worktree git admin backlink is unavailable: {checkout}"
            )
        try:
            backlink_path = Path(backlink[0].decode("utf-8").strip())
        except UnicodeDecodeError as exc:
            raise WorktreeIsolationError(
                f"worktree git admin backlink is not UTF-8: {checkout}"
            ) from exc
        if not backlink_path.is_absolute():
            backlink_path = candidate / backlink_path
        if real_path(backlink_path) != real_path(checkout / ".git"):
            raise WorktreeIsolationError(
                f"worktree git admin directory belongs to another checkout: {checkout}"
            )
        current_pointer = _read_host_hook_registration_at(
            parent=checkout_fd,
            name=".git",
        )
        if current_pointer != pointer:
            raise WorktreeIsolationError(
                f"worktree git pointer changed during hook sync: {checkout}"
            )
        return gitdir_fd
    except BaseException:
        os.close(gitdir_fd)
        raise


def _provision_one_host_hook_registration_at_locked(
    *,
    leader: Path,
    checkout: Path,
    source: Path,
    rel: str,
    state: _HostHookProvisionState,
    checkout_fd: int,
    gitdir_fd: int,
) -> tuple[bool, str | None]:
    source_exists = source.is_file()
    target = _worktree_setup_path(checkout, rel)
    try:
        parent = _open_host_hook_parent(
            checkout=checkout,
            target=target,
            checkout_fd=checkout_fd,
            create=source_exists,
        )
    except FileNotFoundError:
        return False, None
    try:
        existing = _read_host_hook_registration_at(parent=parent, name=target.name)
        if not source_exists:
            if existing is None:
                return False, None
            if _host_hook_path_is_tracked_at(
                gitdir_fd=gitdir_fd,
                rel=rel,
                state=state,
            ):
                return False, None
            if not _host_hook_registration_is_kit_owned(
                leader=leader,
                rel=rel,
                payload=existing[0],
            ):
                return False, None
            return _retire_host_hook_registration_at(
                parent=parent,
                name=target.name,
                leader=leader,
                rel=rel,
                expected=existing[1],
            ), None

        payload = source.read_bytes()
        if existing is not None and existing[0] == payload:
            return False, None
        if _host_hook_path_is_tracked_at(
            gitdir_fd=gitdir_fd,
            rel=rel,
            state=state,
        ):
            return (
                False,
                "tracked by git; commit the agent-flow hook entry or untrack the file",
            )
        if existing is not None and not _host_hook_registration_is_kit_owned(
            leader=leader,
            rel=rel,
            payload=existing[0],
        ):
            return (
                False,
                "the checkout already has a registration this kit did not write;"
                " merge the agent-flow hook entry into it by hand or delete the file",
            )
        changed = _replace_host_hook_registration_at(
            parent=parent,
            name=target.name,
            payload=payload,
            leader=leader,
            rel=rel,
            expected=existing[1] if existing is not None else None,
        )
        return (
            (True, None)
            if changed
            else (False, "the checkout registration changed concurrently; retry install")
        )
    finally:
        os.close(parent)


def _read_host_hook_registration_at(
    *, parent: int, name: str
) -> tuple[bytes, tuple[int, int]] | None:
    try:
        handle = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
    except FileNotFoundError:
        return None
    with os.fdopen(handle, "rb") as stream:
        identity = os.fstat(stream.fileno())
        payload = stream.read()
    if not stat.S_ISREG(identity.st_mode):
        raise WorktreeIsolationError(f"host hook registration is not a file: {name}")
    return payload, (identity.st_dev, identity.st_ino)


def _host_hook_path_is_tracked_at(
    *, gitdir_fd: int, rel: str, state: _HostHookProvisionState
) -> bool:
    cached = state.tracked.get(rel)
    if cached is not None:
        return cached
    result = git_safe(
        "--git-dir=.",
        "ls-files",
        "--",
        rel,
        cwd=Path("/"),
        optional_locks=False,
        pass_fds=(gitdir_fd,),
        cwd_fd=gitdir_fd,
    )
    if not result.ok:
        raise WorktreeIsolationError(
            "cannot inspect the verified worktree index during hook sync: "
            f"{result.stderr.strip() or result.error or 'git did not answer'}"
        )
    tracked = rel in result.stdout.splitlines()
    if state.index_identity:
        state.tracked[rel] = tracked
        state.dirty = True
    return tracked




def _retire_host_hook_registration_at(
    *,
    parent: int,
    name: str,
    leader: Path,
    rel: str,
    expected: tuple[int, int],
) -> bool:
    quarantine = f"{name}.agent-flow-retired.{os.getpid()}.{secrets.token_hex(8)}"
    try:
        os.rename(name, quarantine, src_dir_fd=parent, dst_dir_fd=parent)
    except FileNotFoundError:
        return False
    moved = _read_host_hook_registration_at(parent=parent, name=quarantine)
    if (
        moved is None
        or moved[1] != expected
        or not _host_hook_registration_is_kit_owned(
            leader=leader,
            rel=rel,
            payload=moved[0],
        )
    ):
        _restore_host_hook_registration_at(
            parent=parent,
            name=name,
            quarantine=quarantine,
        )
        return False
    os.unlink(quarantine, dir_fd=parent)
    return True


def _replace_host_hook_registration_at(
    *,
    parent: int,
    name: str,
    payload: bytes,
    leader: Path,
    rel: str,
    expected: tuple[int, int] | None,
) -> bool:
    staging = _write_host_hook_staging_at(parent=parent, name=name, payload=payload)
    quarantine: str | None = None
    try:
        if expected is not None:
            quarantine = (
                f"{name}.agent-flow-replaced.{os.getpid()}.{secrets.token_hex(8)}"
            )
            try:
                os.rename(name, quarantine, src_dir_fd=parent, dst_dir_fd=parent)
            except FileNotFoundError:
                return False
            moved = _read_host_hook_registration_at(
                parent=parent,
                name=quarantine,
            )
            if (
                moved is None
                or moved[1] != expected
                or not _host_hook_registration_is_kit_owned(
                    leader=leader,
                    rel=rel,
                    payload=moved[0],
                )
            ):
                _restore_host_hook_registration_at(
                    parent=parent,
                    name=name,
                    quarantine=quarantine,
                )
                quarantine = None
                return False
        try:
            os.link(
                staging,
                name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
                follow_symlinks=False,
            )
        except FileExistsError:
            if quarantine is not None:
                _restore_host_hook_registration_at(
                    parent=parent,
                    name=name,
                    quarantine=quarantine,
                )
                quarantine = None
            return False
        if quarantine is not None:
            os.unlink(quarantine, dir_fd=parent)
            quarantine = None
        return True
    finally:
        try:
            os.unlink(staging, dir_fd=parent)
        except FileNotFoundError:
            pass
        if quarantine is not None:
            raise WorktreeIsolationError(
                f"concurrent registration preserved as {quarantine}; retry install"
            )


def _write_host_hook_staging_at(*, parent: int, name: str, payload: bytes) -> str:
    staging = f"{name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    handle = os.open(
        staging,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o644,
        dir_fd=parent,
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
    except BaseException:
        try:
            os.unlink(staging, dir_fd=parent)
        except OSError:
            pass
        raise
    return staging


def _restore_host_hook_registration_at(
    *, parent: int, name: str, quarantine: str
) -> None:
    try:
        os.link(
            quarantine,
            name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
            follow_symlinks=False,
        )
    except FileExistsError as exc:
        raise WorktreeIsolationError(
            f"concurrent registration preserved as {quarantine}; retry install"
        ) from exc
    os.unlink(quarantine, dir_fd=parent)


def _unlink_kit_owned_host_hook_registration(
    *,
    leader: Path,
    checkout: Path,
    rel: str,
    checkout_fd: int | None,
) -> bool:
    """검사한 managed 파일과 같은 inode만 stable parent fd에서 지운다."""
    target = _worktree_setup_path(checkout, rel)
    if _DIR_FD_SUPPORTED:
        parent = _open_host_hook_parent(
            checkout=checkout,
            target=target,
            checkout_fd=checkout_fd,
        )
        try:
            try:
                handle = os.open(
                    target.name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent,
                )
            except FileNotFoundError:
                return False
            with os.fdopen(handle, "rb") as stream:
                observed = os.fstat(stream.fileno())
                payload = stream.read()
            if not stat.S_ISREG(observed.st_mode):
                return False
            if not _host_hook_registration_is_kit_owned(
                leader=leader,
                rel=rel,
                payload=payload,
            ):
                return False
            try:
                current = os.stat(
                    target.name,
                    dir_fd=parent,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return False
            if (current.st_dev, current.st_ino) != (observed.st_dev, observed.st_ino):
                return False
            os.unlink(target.name, dir_fd=parent)
            return True
        finally:
            os.close(parent)

    try:
        observed = target.lstat()
        payload = target.read_bytes()
        current = target.lstat()
    except OSError:
        return False
    if (
        target.is_symlink()
        or not stat.S_ISREG(observed.st_mode)
        or (current.st_dev, current.st_ino) != (observed.st_dev, observed.st_ino)
        or not _host_hook_registration_is_kit_owned(
            leader=leader,
            rel=rel,
            payload=payload,
        )
    ):
        return False
    target.unlink()
    return True


def _open_host_hook_parent(
    *,
    checkout: Path,
    target: Path,
    checkout_fd: int | None = None,
    create: bool = False,
) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = (
        os.dup(checkout_fd)
        if checkout_fd is not None
        else os.open(checkout, flags)
    )
    try:
        for component in target.parent.relative_to(checkout).parts:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _write_host_hook_registration(
    *,
    checkout: Path,
    target: Path,
    payload: bytes,
    checkout_fd: int | None,
) -> None:
    """등록 파일을 stable checkout fd 아래에 원자적으로 놓는다."""
    if _DIR_FD_SUPPORTED:
        parent = _open_host_hook_parent(
            checkout=checkout,
            target=target,
            checkout_fd=checkout_fd,
            create=True,
        )
        try:
            _replace_at(parent=parent, name=target.name, payload=payload)
        finally:
            os.close(parent)
        return
    if checkout_fd is not None:
        raise WorktreeIsolationError(
            "registered worktree hook sync requires secure dir-fd operations"
        )
    _make_host_hook_parents(base=checkout, target=target)
    handle, staging_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f"{target.name}.", suffix=".tmp"
    )
    staging = Path(staging_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        os.chmod(staging, 0o644)
        os.replace(staging, target)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise


def _replace_at(*, parent: int, name: str, payload: bytes) -> None:
    staging = f"{name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    handle = os.open(staging, flags, 0o644, dir_fd=parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        os.rename(staging, name, src_dir_fd=parent, dst_dir_fd=parent)
    except BaseException:
        try:
            os.unlink(staging, dir_fd=parent)
        except OSError:
            pass
        raise


def _host_hook_registration_is_kit_owned(
    *, leader: Path, rel: str, payload: bytes
) -> bool:
    """등록 파일이 kit이 깐 모양인가. ``leader``는 등록 command가 가리키는 checkout이다.

    kit이 깐 것이면 덮어야 등록 갱신이 checkout까지 번지고, 정리 때 걷어내도 사용자가
    잃는 것이 없다. 사용자가 쓴 것으로 보이면 손대지 않아야 그 설정이 살아남는다.
    판정할 수 없는 것은 사용자 소유로 본다 — 잘못 덮으면 사용자 데이터가 사라지고,
    잘못 남겨도 사유가 stderr로 나온다.

    JSON 판정은 `hook_integrity.managed_path_hook_name`에 맡긴다. 관리 hook 디렉터리를
    부분문자열로 찾으면 그 hook을 감싼 사용자 래퍼(`/bin/bash -c 'mylog; … .py'`)와
    **다른 설치본**의 hook을 가리키는 command가 둘 다 kit 소유로 읽혀 경고 없이 덮인다.
    그 함수는 이 leader가 실제로 생성하는 절대경로 하나만 인정한다.
    """
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if rel == _OMP_REGISTRATION_REL:
        return _OMP_EXTENSION_MARKER in text or _OMP_EXTENSION_SIGNATURE in text
    try:
        document = json.loads(text)
    except ValueError:
        return False
    commands = list(_json_hook_commands(document))
    # 등록이 하나도 없으면 kit이 쓴 결과일 수 없다. 하나라도 이 leader의 관리 hook
    # 호출이 아니면 사용자가 자기 hook을 넣어 둔 파일이다.
    if not commands or any(
        managed_path_hook_name(leader, command) is None for command in commands
    ):
        return False
    # hook 밖의 키(`permissions`, `env`, MCP 설정 …)는 installer가 병합해 보존하는
    # 사용자 소유다. 그 부분이 leader와 다르면 이 checkout에만 있는 설정이므로
    # 파일째 덮으면 조용히 사라진다 — 그때는 사용자 소유로 본다.
    leader_path = leader / rel
    leader_document = _load_registration_document(leader_path)
    if leader_document is None and not leader_path.is_file():
        return _non_hook_keys(document) == {}
    return _non_hook_keys(document) == _non_hook_keys(leader_document)


def _load_registration_document(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _non_hook_keys(document: Any) -> Any:
    if not isinstance(document, dict):
        return document
    return {key: value for key, value in sorted(document.items()) if key != "hooks"}


def _json_hook_commands(node: Any) -> Iterator[str]:
    """등록 JSON 어디에 있든 hook command 문자열만 모은다.

    host마다 이벤트 키가 다르고 사용자 키가 같은 파일에 섞이므로 모양을 고정할 수 없다.
    """
    if isinstance(node, dict):
        command = node.get("command")
        if isinstance(command, str):
            yield command
        for value in node.values():
            yield from _json_hook_commands(value)
    elif isinstance(node, list):
        for value in node:
            yield from _json_hook_commands(value)


def _make_host_hook_parents(*, base: Path, target: Path) -> None:
    missing: list[Path] = []
    for parent in [target.parent, *target.parent.parents]:
        if parent.exists() or parent == base or base not in parent.parents:
            break
        missing.append(parent)
    for parent in reversed(missing):
        parent.mkdir(mode=0o755)


def _host_hook_path_is_tracked(
    *, checkout: Path, rel: str, state: _HostHookProvisionState
) -> bool:
    """tracked 판정은 `git ls-files` spawn이다.

    tracked 경로는 구조적으로 매번 여기까지 온다 — leader 바이트와 다르니 동일성 skip에
    걸릴 수 없다. 캐시하지 않으면 `status` 호출마다 프로세스가 하나 뜬다. 판정이 바뀔 수
    있는 유일한 사건은 index 변경이므로 그때만 다시 묻는다.
    """
    cached = state.tracked.get(rel)
    if cached is not None:
        return cached
    tracked = git_safe(
        "ls-files", "--error-unmatch", "--", rel, cwd=checkout, optional_locks=False
    ).ok
    if state.index_identity:
        state.tracked[rel] = tracked
        state.dirty = True
    return tracked


def _load_host_hook_state(
    checkout: Path, *, gitdir: Path | None = None
) -> _HostHookProvisionState:
    """읽지 못하면 기억이 없는 것과 같다 — 경고를 한 번 더 내고 다시 기록한다."""
    path = (
        gitdir / _HOST_HOOK_STATE_NAME
        if gitdir is not None
        else _host_hook_state_path(checkout)
    )
    identity = _git_index_identity(path)
    payload: dict[str, Any] = {}
    if path is not None:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = None
        if isinstance(loaded, dict):
            payload = loaded
    skipped = {
        rel: value
        for rel, value in _host_hook_state_mapping(payload, "skipped").items()
        if isinstance(value, str)
    }
    # index가 그때와 다르면 tracked 판정은 더 이상 증명된 값이 아니다.
    tracked = (
        {
            rel: value
            for rel, value in _host_hook_state_mapping(payload, "tracked").items()
            if isinstance(value, bool)
        }
        if identity and payload.get("index_identity") == identity
        else {}
    )
    return _HostHookProvisionState(
        path=path, skipped=skipped, tracked=tracked, index_identity=identity
    )


def _host_hook_state_mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    """아는 등록 경로만 남긴다. 남은 키가 무한히 자라면 이 파일이 쓰레기통이 된다."""
    value = payload.get(key)
    if not isinstance(value, dict):
        return {}
    return {
        rel: entry
        for rel, entry in value.items()
        if isinstance(rel, str) and rel in HOST_HOOK_REGISTRATION_FILES
    }


def _save_host_hook_state(state: _HostHookProvisionState) -> None:
    if state.path is None or not state.dirty:
        return
    payload = {
        "index_identity": state.index_identity,
        "skipped": state.skipped,
        "tracked": state.tracked,
    }
    try:
        state.path.write_text(
            f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8"
        )
    except OSError as exc:
        print(
            f"warning: cannot remember host hook provisioning state at {state.path}: {exc};"
            " the warnings above will repeat every command",
            file=sys.stderr,
        )


def _host_hook_state_path(checkout: Path) -> Path | None:
    """checkout의 private git admin 디렉터리 안 상태 파일.

    `git rev-parse`를 부르지 않고 `.git` 포인터에서 직접 읽는다 — 이 상태를 두는 이유가
    `status` 핫패스의 git spawn을 없애는 것이라 여기서 spawn하면 목적이 뒤집힌다.
    포인터가 없으면(관리 worktree가 아니다) 기억할 자리가 없다.
    """
    pointer = checkout / ".git"
    try:
        if pointer.is_symlink() or not pointer.is_file():
            return None
        line = pointer.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = Path(line.partition(":")[2].strip()).expanduser()
    if not gitdir.is_absolute():
        gitdir = checkout / gitdir
    return gitdir / _HOST_HOOK_STATE_NAME if gitdir.is_dir() else None


def _git_index_identity(state_path: Path | None) -> str:
    """tracked 캐시의 유효 범위. index가 그대로면 어느 경로의 추적 여부도 그대로다."""
    if state_path is None:
        return ""
    try:
        info = (state_path.parent / "index").stat()
    except OSError:
        return ""
    return f"{info.st_mtime_ns}:{info.st_size}"


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
    safe = re.sub(r"[^a-z0-9._-]+", "-", lowered)
    # 버려진 글자마다 구분자를 하나씩 남기면 원문의 `-` 양옆이 같이 치환돼
    # `ui---cta`가 된다. 이어진 구분자는 하나로 접고 양끝에서는 걷어낸다.
    safe = re.sub(r"[-._]{2,}", "-", safe).strip("-._")
    # 토큰 단위로 생사를 보면 `로그인화면Figma구현`처럼 붙여 쓴 task가 통째로
    # 살아남은 것이 된다. 실제로 지워진 글자를 기준으로 센다.
    dropped = tuple(
        word
        for word in value.split()
        if any(char.isalnum() and not _SLUG_SAFE_CHAR_RE.match(char.lower()) for char in word)
    )
    # 글자가 하나도 없는 slug는 이름이 아니다. `1-1 홈 화면`은 `1-1`만 남겨
    # 브랜치를 만들지만 그 이름으로는 어떤 작업인지 누구도 알 수 없고, 다음 번호
    # 작업과 충돌한다. 비ASCII task와 같은 취급으로 digest fallback을 쓴다.
    if not any("a" <= char <= "z" for char in safe):
        safe = ""
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
    candidate = first_line.strip().strip("\"'")
    # 경로 모양의 출력은 이름이 아니라 자리다. 정규화가 `../../etc/passwd`를
    # `etc-passwd`로, `.hidden`을 `hidden`으로 세탁하면 host가 낸 쓰레기가
    # 그럴듯한 브랜치 이름이 되어 task를 대표하지 않는 이름이 남는다.
    if candidate.startswith(".") or ".." in candidate or "/" in candidate:
        return None
    try:
        quality = describe_slug(candidate)
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
        validate_git_branch(branch)
    except ValueError:
        return None
    return branch


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


