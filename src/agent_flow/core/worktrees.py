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
    if live and require_merged and not allow_unmerged:
        # Destructive: prove the work is already in the leader before removing.
        assert_worktree_mergeable(root=root, path=status.path)
    branch_to_delete = _owned_branch_for_live_worktree(root=root, status=status) if delete_branch else None
    if status.path.exists():
        # 관측과 실행 사이에 등록이 바뀔 수 있다. git을 부르기 직전에 다시 읽어
        # 우리가 증명한 그 경로가 여전히 같은 상태인지 확인한다.
        recheck = _registered_at_path(root=root, path=status.path)
        _assert_not_locked(path=status.path, entry=recheck)
        if registered is not None and recheck is None:
            raise WorktreeIsolationError(
                f"worktree registration changed while removing {status.path}; re-run the command"
            )
        force_args = ("--force",) if allow_unmerged else ()
        _run_git(root, "worktree", "remove", *force_args, str(status.path))
    if branch_to_delete is not None:
        _run_git(root, "branch", "-D", branch_to_delete)
    remove_worktree_metadata(root=root, name=status.name)


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
    try:
        name = _feature_worktree_name(registered.path.name)
    except ValueError:
        # 정규화되지 않는 디렉터리 이름이어도 목록에서 사라지면 안 된다. 경로가
        # 진실이므로 표시 이름만 경로에서 그대로 가져온다.
        name = registered.path.name
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
    # 디렉터리 스캔은 agent-flow가 만든 자리만 본다. 사용자가 raw git으로 만든
    # 체크아웃은 등록부에만 있으므로 그쪽도 아는 이름의 원천으로 함께 넣는다.
    names.update(entry.path.name for entry in removable_worktrees(root=root))
    return sorted(names)


def remove_worktree_metadata(*, root: Path, name: str) -> None:
    try:
        runtime_root = worktree_runtime_root(root=root, name=name)
        legacy_manifest = _legacy_worktree_manifest_path(root=root, name=name)
    except ValueError:
        # agent-flow 이름 규칙으로 정규화되지 않는 이름에는 애초에 메타데이터가 없다.
        return
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
    if legacy_manifest.exists():
        legacy_manifest.unlink()


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


def _worktree_manifest_path(*, root: Path, name: str) -> Path:
    return worktree_runtime_root(root=root, name=name) / "manifest.json"


def _legacy_worktree_manifest_path(*, root: Path, name: str) -> Path:
    return root / ".agent-flow" / "worktrees" / _feature_worktree_name(name) / "manifest.json"


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
