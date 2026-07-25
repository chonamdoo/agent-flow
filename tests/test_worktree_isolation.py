"""Fail-closed worktree isolation tests.

Every guard has a falsification case: a deliberately injected violation that must
raise. Detection-only guards pair "does not raise" with a mutation case, so a
dead assertion cannot pass silently.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

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
    real_path,
    sanitized_worker_env,
    verify_linked_worktree,
)


def _git(*args, cwd):
    return subprocess.run(("git", *args), cwd=str(cwd), capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "f.txt").write_text("base\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "init", cwd=root)


def _managed(root: Path) -> Path:
    return root / ".agent-flow" / "worktrees"



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


def test_e2e_team_run_next_confines_worker_writes_and_git(tmp_path):
    _init_repo(tmp_path)
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

    wt = tmp_path / ".agent-flow" / "worktrees" / "feat-t1-w1"
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


def test_e2e_non_git_scope_collision_is_rejected(tmp_path):
    # No git repo -> no worktree isolation -> overlapping scopes on concurrent
    # workers must be rejected fail-closed by the scope gate.
    assert _cli(["team", "init", "--root", ".", "--name", "ft"], cwd=tmp_path).returncode == 0
    for tid in ("t1", "t2"):
        assert _cli(["team", "task", "--root", ".", "--team", "ft", "--id", tid,
                     "--subject", "s", "--description", "d"], cwd=tmp_path).returncode == 0
    for w in ("wA", "wB"):
        assert _cli(["team", "worker", "--root", ".", "--team", "ft", "--name", w,
                     "--role", "impl"], cwd=tmp_path).returncode == 0
    for tid, w in (("t1", "wA"), ("t2", "wB")):
        assert _cli(["team", "brief", "--root", ".", "--team", "ft", "--task", tid,
                     "--worker", w, "--brief", "b", "--write-scope", "src/feature"], cwd=tmp_path).returncode == 0
        assert _cli(["team", "approve-worker", "--root", ".", "--team", "ft", "--task", tid,
                     "--worker", w, "--write-scope", "src/feature"], cwd=tmp_path).returncode == 0
    # wB claims t2 -> in_progress; t1 stays pending for wA.
    assert _cli(["team", "claim", "--root", ".", "--team", "ft", "--task", "t2",
                 "--worker", "wB"], cwd=tmp_path).returncode == 0
    res = _cli(["team", "run-next", "--root", ".", "--team", "ft", "--worker", "wA",
                "--command", sys.executable, "-c", "print('must not run')"], cwd=tmp_path)
    assert res.returncode == 2
    assert "overlapping write scope" in (res.stdout + res.stderr)


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
    """불변: 삭제한 guard-worktree.sh가 막던 leader 브랜치 전환을 tripwire가 대신 잡는다."""
    status, before = _isolated(tmp_path, "branch")
    _git("checkout", "-q", "-b", "sneaky", cwd=tmp_path)
    with pytest.raises(W_ISO.WorktreeIsolationError) as caught:
        W_ISO.assert_leader_unchanged(tmp_path, before)
    assert "branch switched" in str(caught.value)
    # 되돌리지 않는다. 사용자가 어디에 있는지 스스로 알 수 있어야 한다.
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=tmp_path).stdout.strip() == "sneaky"


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


def test_multi_review_reviewer_env_is_sanitized(tmp_path, monkeypatch):
    """불변: reviewer 자식 프로세스는 오염된 GIT_DIR을 물려받지 않는다."""
    from agent_flow import multi_review as MR
    from agent_flow.cli_detect import CliInfo

    _init_repo(tmp_path)
    out = tmp_path / "angle.md"
    probe = (
        "import os,sys,subprocess\n"
        "top=subprocess.run(['git','rev-parse','--show-toplevel'],capture_output=True,text=True).stdout.strip()\n"
        "print('GITDIR=%s' % os.environ.get('GIT_DIR','<unset>'))\n"
        "print('WORKTREE=%s' % os.environ.get('GIT_WORK_TREE','<unset>'))\n"
        "print('TOP=%s' % os.path.realpath(top) if top else 'TOP=')\n"
    )
    fake = CliInfo(name="probe", binaries=(sys.executable,), invoke=("-c", probe, "--prompt"))
    monkeypatch.setattr(MR, "cli_by_name", lambda name: fake)
    # 부모 환경을 오염시킨다. env를 넘기지 않으면 자식이 이걸 그대로 상속한다.
    monkeypatch.setenv("GIT_DIR", str(tmp_path / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))

    distribution = MR.Distribution(
        by_cli={"probe": [MR.ReviewerJob(angle_id="a1", prompt="review", output_path=out)]},
        host="host-cli",
    )
    results = MR.run_distribution(distribution, tmp_path, timeout_s=60)
    assert len(results) == 1
    rendered = out.read_text(encoding="utf-8")
    assert "GITDIR=<unset>" in rendered
    assert "WORKTREE=<unset>" in rendered
    # 오염된 env가 걸러졌으므로 자식의 git은 cwd에서 저장소를 다시 찾는다.
    assert f"TOP={real_path(tmp_path)}" in rendered


def test_run_blocks_when_git_state_unknown(tmp_path, monkeypatch):
    """불변: git 상태를 확정 못 하면 run은 leader에서 격리 없이 진행하지 않는다."""
    import contextlib
    import io

    from agent_flow import cli as CLI
    from agent_flow.core import commands as C

    _init_repo(tmp_path)

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(C.subprocess, "run", _timeout)
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


def _team_fixture(root: Path) -> None:
    assert _cli(["team", "init", "--root", ".", "--name", "ft"], cwd=root).returncode == 0
    for tid in ("t1", "t2"):
        assert _cli(["team", "task", "--root", ".", "--team", "ft", "--id", tid,
                     "--subject", "s", "--description", "d"], cwd=root).returncode == 0
    for worker in ("w1", "w2"):
        assert _cli(["team", "worker", "--root", ".", "--team", "ft", "--name", worker,
                     "--role", "impl"], cwd=root).returncode == 0
    for tid, worker, scope in (("t1", "w1", "src/a"), ("t2", "w2", "src/b")):
        assert _cli(["team", "brief", "--root", ".", "--team", "ft", "--task", tid,
                     "--worker", worker, "--brief", "Use the worker-brief contract.",
                     "--write-scope", scope], cwd=root).returncode == 0
        assert _cli(["team", "approve-worker", "--root", ".", "--team", "ft", "--task", tid,
                     "--worker", worker, "--write-scope", scope], cwd=root).returncode == 0


def test_capacity_gate_holds_when_a_slot_is_already_taken(tmp_path):
    """불변: capacity 검사는 claim과 같은 구간에서 최신 in-progress 수를 보고 판정한다."""
    _init_repo(tmp_path)
    _team_fixture(tmp_path)
    # w1이 t1을 잡아 슬롯 하나를 소진한다.
    assert _cli(["team", "claim", "--root", ".", "--team", "ft", "--task", "t1",
                 "--worker", "w1"], cwd=tmp_path).returncode == 0

    blocked = _cli(
        ["team", "run-next", "--root", ".", "--team", "ft", "--worker", "w2",
         "--command", sys.executable, "-c", "print('must not run')"],
        cwd=tmp_path, env={"AGENT_FLOW_MAX_WORKERS": "1"},
    )
    assert blocked.returncode == 2
    assert "worker capacity reached" in (blocked.stdout + blocked.stderr)
    # 막혔다면 t2는 아직 아무도 잡지 않았어야 한다.
    assert "must not run" not in blocked.stdout

    # falsification: 여유가 있으면 같은 명령이 실제로 통과한다.
    allowed = _cli(
        ["team", "run-next", "--root", ".", "--team", "ft", "--worker", "w2",
         "--command", sys.executable, "-c", "print('worker ran')"],
        cwd=tmp_path, env={"AGENT_FLOW_MAX_WORKERS": "4"},
    )
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert "t2 completed" in allowed.stdout


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
