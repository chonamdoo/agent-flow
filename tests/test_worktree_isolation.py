"""Fail-closed worktree isolation tests.

Every guard has a falsification case: a deliberately injected violation that must
make the guard raise. A guard that always passes proves nothing, so each check is
paired with a "this really fails" assertion.
"""
from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core import worktrees as W
from agent_flow.core.worktree_isolation import (
    WorkerScope,
    WorktreeIsolationError,
    assert_cwd_bound,
    assert_scopes_isolated,
    assert_worktree_mergeable,
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
    assert_scopes_isolated([
        WorkerScope("a", ("src/x.py",), True),
        WorkerScope("b", ("src/x.py",), True),
    ])


def test_scope_gate_allows_disjoint():
    assert_scopes_isolated([
        WorkerScope("a", ("src/a.py",), False),
        WorkerScope("b", ("src/b.py",), False),
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
    _init_repo(tmp_path)
    plan = W.plan_worktree(root=tmp_path, name="epsilon")
    status = W.create_worktree(root=tmp_path, plan=plan)
    # fresh worktree tip == base == leader HEAD -> ancestor, clean -> mergeable
    assert_worktree_mergeable(root=tmp_path, path=status.path)


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
    _init_repo(tmp_path)
    baseline = _git("status", "--porcelain", cwd=tmp_path).stdout

    def _boom(root, *args):
        if args[:2] == ("worktree", "add"):
            raise subprocess.CalledProcessError(1, ("git",) + args, stderr="fatal: index.lock exists")
        return W._run_git(tmp_path, *args) if False else _orig(root, *args)

    _orig = W._run_git
    monkeypatch.setattr(W, "_run_git", _boom)
    plan = W.plan_worktree(root=tmp_path, name="theta")
    with pytest.raises((WorktreeIsolationError, subprocess.CalledProcessError)):
        W.create_worktree(root=tmp_path, plan=plan)
    monkeypatch.undo()
    # falsification: main is untouched and no leftover checkout was trusted.
    assert _git("status", "--porcelain", cwd=tmp_path).stdout == baseline
    assert not (plan.path / ".git").is_file() or plan.path not in _registered(tmp_path)



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
