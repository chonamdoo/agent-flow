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
    RegisteredWorktree,
    WorktreeIsolationError,
    assert_worktree_mergeable,
    git_repo_state,
    git_safe,
    list_registered_worktrees,
    real_path,
    same_worktree_path,
    verify_linked_worktree,
    with_git_lock_retry,
    worktree_creation_lock,
    worktree_path_key,
)


PROTECTED_WORKTREE_BRANCHES = {"main", "master", "develop"}
GIT_WORKTREE_TIMEOUT_S = 300


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


def attach_worktree(
    *, root: Path, selector: str, branch: str | None = None, allow_dirty: bool = False
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
    verify_linked_worktree(root=root, path=registered.path, expected_branch=registered.branch)
    # manifest는 쓰지 않는다. 소유권(`branch_created_by_agent_flow`)은 만든 쪽만
    # 주장할 수 있다. attach가 덮어쓰면 정리 단계가 사용자 브랜치를 지우거나
    # 우리가 만든 브랜치를 영구히 남긴다.
    return _status_for_registered(root=root, registered=registered, requested=selector)


def _is_managed_child(*, root: Path, path: Path) -> bool:
    return worktree_path_key(path.parent) == worktree_path_key(_managed_root(root))


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
    leader = leader_worktree_path(root)
    if leader is None:
        # leader를 지목하지 못한 채 제거를 진행하면 "leader가 아님"을 증명하지 못한
        # 상태로 파괴적 명령을 돌리는 것이다. 파일시스템이 non-git을 증명한 경우만
        # 지킬 leader가 없다고 보고 통과시킨다.
        if git_repo_state(root) != "non-repo":
            raise WorktreeIsolationError(
                f"cannot resolve the leader checkout for {root}; refusing to remove {status.path}"
            )
    elif same_worktree_path(leader, status.path):
        raise WorktreeIsolationError(f"refusing to remove the leader checkout: {status.path}")
    registered = _registered_at_path(root=root, path=status.path)
    _assert_not_locked(path=status.path, entry=registered)
    live = status.path.exists() and (status.path / ".git").exists()
    if live:
        # manifest 없이 손으로 만든 checkout도 지울 수 있어야 하지만 소유 증명이
        # 먼저다. 증명은 **경로 모양이 아니라 git 등록부**다 — 남의 저장소가 우리
        # 관리 루트 안에 심어 둔 checkout은 등록부에 없으므로 걸러지고, 관리 루트
        # 밖이라도 등록돼 있으면 이 저장소의 worktree이므로 지울 수 있다.
        # `verify_linked_worktree`는 여기에 맞지 않는다. 그건 생성 경로의 불변식
        # (관리 루트 안에 있어야 한다)을 함께 요구해서, 소유권과 생성 주체를
        # 같은 조건으로 묶어 버린다.
        if registered is None:
            raise WorktreeIsolationError(
                f"refusing to remove {status.path}: "
                f"not registered as a worktree of this repository"
            )
        if require_merged and not allow_unmerged:
            # Destructive: prove the work is already in the leader before removing.
            assert_worktree_mergeable(root=root, path=status.path)
    branch_to_delete = _owned_branch_for_live_worktree(root=root, status=status) if delete_branch else None
    if status.path.exists():
        # 관측과 실행 사이에 등록이 바뀔 수 있다. git을 부르기 직전에 다시 읽어
        # 우리가 증명한 그 경로가 여전히 같은 상태인지 확인한다.
        recheck = _registered_at_path(root=root, path=status.path)
        _assert_not_locked(path=status.path, entry=recheck)
        if registered is not None and not _same_registration(registered, recheck):
            # 경로가 여전히 등록돼 있다는 것만으로는 부족하다. 다른 프로세스가 그
            # 사이에 같은 경로를 제거하고 새 worktree를 등록했으면 우리가 증명한
            # 대상이 아니다. --force까지 붙으면 남의 미커밋 변경을 지운다.
            raise WorktreeIsolationError(
                f"worktree registration changed while removing {status.path}; re-run the command"
            )
        force_args = ("--force",) if allow_unmerged else ()
        _run_git(root, "worktree", "remove", *force_args, str(status.path))
    if branch_to_delete is not None:
        _run_git(root, "branch", "-D", branch_to_delete)
    remove_worktree_metadata(root=root, name=status.name, path=status.path)


def _same_registration(
    before: RegisteredWorktree, after: RegisteredWorktree | None
) -> bool:
    """같은 등록인가. 경로가 같아도 가리키는 대상이 바뀌었으면 다른 worktree다."""
    if after is None:
        return False
    return before.branch == after.branch and before.head == after.head


def _registered_at_path(*, root: Path, path: Path) -> RegisteredWorktree | None:
    for entry in list_registered_worktrees(root):
        if same_worktree_path(entry.path, path):
            return entry
    return None


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
    """선택자를 git이 등록한 worktree 하나로 해석한다.

    이름에서 경로를 역산하지 않는다. ``git worktree list``가 보고한 항목만 후보이고
    선택자는 그 목록과 대조만 한다 — 그래야 agent-flow가 만들지 않은 체크아웃도
    사용자가 목록에서 본 그대로 지목할 수 있다.
    """
    wanted = selector.strip()
    if not wanted:
        return None
    matched: dict[str, RegisteredWorktree] = {}
    for entry in removable_worktrees(root=root):
        if _selector_matches(root=root, selector=wanted, entry=entry):
            matched[worktree_path_key(entry.path)] = entry
    if len(matched) > 1:
        raise AmbiguousWorktreeSelector(
            wanted, tuple(sorted(matched.values(), key=lambda item: str(item.path)))
        )
    return next(iter(matched.values()), None)


def _selector_matches(*, root: Path, selector: str, entry: RegisteredWorktree) -> bool:
    if selector == entry.branch or selector == entry.path.name:
        return True
    derived = _derived_worktree_identity(selector)
    if derived is not None and (derived[0] == entry.path.name or derived[1] == entry.branch):
        return True
    return any(
        same_worktree_path(candidate, entry.path)
        for candidate in _selector_path_candidates(root=root, selector=selector)
    )


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
    # 반면 manifest/런타임 상태의 **키**는 생성 시점의 정규화된 이름이다. 그쪽만
    # 정규화해서 찾는다. 정규화할 수 없는 이름이면 애초에 agent-flow가 만든
    # 자리가 아니므로 manifest도 없다.
    planned_branch = _planned_branch(name)
    payload = _load_worktree_manifest(root=root, name=name)
    manifest_branch = _manifest_branch(payload)
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
    )


def _status_for_planned_name(*, root: Path, name: str) -> WorktreeStatus:
    plan = plan_worktree(root=root, name=name)
    payload = _load_worktree_manifest(root=root, name=plan.name)
    manifest_branch = _manifest_branch(payload)
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
        manifest = _worktree_manifest_path(root=root, name=name)
        legacy = _legacy_worktree_manifest_path(root=root, name=name)
    except ValueError:
        return None
    if not manifest.exists() and legacy.exists():
        manifest = legacy
    if not manifest.exists():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
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
    # 디렉터리 스캔은 agent-flow가 만든 자리만 본다. 사용자가 raw git으로 만든
    # 체크아웃은 등록부에만 있으므로 그쪽도 아는 이름의 원천으로 함께 넣는다.
    names.update(entry.path.name for entry in removable_worktrees(root=root))
    return sorted(names)


def remove_worktree_metadata(*, root: Path, name: str, path: Path | None = None) -> None:
    """``name``의 런타임 메타데이터를 지운다.

    ``path``를 주면 그 등록 경로의 소유임을 증명한 경우에만 지운다. 메타데이터
    키는 정규화된 이름이라 서로 다른 등록 경로가 같은 키로 접힌다 — 관리형
    ``.../feat-demo``와 외부 ``.../demo``가 둘 다 ``feat-demo``다. 증명 없이
    지우면 외부 worktree를 제거하다가 관리형 worktree의 활성 run과 manifest를
    날린다.
    """
    if path is not None and not _metadata_belongs_to_path(root=root, name=name, path=path):
        return
    try:
        # 런타임 상태를 쓸 때와 같은 키로 지운다(`worktree_runtime_root`). 여기서만
        # 정규화하면 `feat-issue#110` 같은 이름의 상태가 지워지지 않고 남아, 같은
        # 자리에 새 run을 시작할 때 죽은 active 마커가 되살아난다.
        runtime_root = _runtime_state_root(root=root, name=_runtime_state_key(root=root, name=name))
        legacy_manifest = _legacy_worktree_manifest_path(root=root, name=name)
    except ValueError:
        # agent-flow 이름 규칙으로 정규화되지 않는 이름에는 애초에 메타데이터가 없다.
        return
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
        return resolve_worktree_name(root=root, name=name)
    except (OSError, RuntimeError, ValueError):
        return _feature_worktree_name(name)



def _metadata_belongs_to_path(*, root: Path, name: str, path: Path) -> bool:
    """``name`` 키의 메타데이터가 이 등록 경로의 것인가.

    manifest가 있으면 그 안에 기록된 경로가 진실이다. manifest가 없으면 생성
    규약(관리 루트 아래 ``<name>`` 디렉터리)으로만 인정한다 — 롤백 경로처럼
    manifest를 쓰기 전에 정리해야 하는 경우가 그 하나다.
    """
    payload = _load_worktree_manifest(root=root, name=name)
    if payload is None:
        return same_worktree_path(root / ".agent-flow" / "worktrees" / name, path)
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
