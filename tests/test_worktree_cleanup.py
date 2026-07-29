from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[1]
SRC = str(KIT_ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.artifact import create_run, mark_inactive, read_meta, write_meta
from agent_flow.core.commands import SafeCommandResult
from agent_flow.core import worktrees as W
from agent_flow.core import worktree_isolation as W_ISO
from agent_flow.runner import ResumeMode, Runner
import agent_flow.runner as runner_module
import agent_flow.cli as cli_module


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ("git", *args), cwd=str(cwd), capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return result


def _init_repo(root: Path, *, second_commit: bool = False) -> str:
    root.mkdir()
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "cleanup@example.com", cwd=root)
    _git("config", "user.name", "Cleanup Test", cwd=root)
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "init", cwd=root)
    first_oid = _git("rev-parse", "HEAD", cwd=root).stdout.strip()
    if second_commit:
        (root / "tracked.txt").write_text("base\nsecond\n", encoding="utf-8")
        _git("add", ".", cwd=root)
        _git("commit", "-m", "second", cwd=root)
    return first_oid


def _managed_run(root: Path, name: str = "cleanup") -> tuple[W.WorktreeStatus, Path]:
    status = W.create_worktree(root=root, plan=W.plan_worktree(root=root, name=name))
    state_root = W.worktree_runtime_root(root=root, name=status.name)
    run_dir = create_run(
        state_root,
        "default",
        "cleanup transaction",
        run_id="run-cleanup",
        checkout_identity=f"worktree:{status.name}",
        checkout_registration_identity=status.registration_identity,
    )
    return status, run_dir



def test_cleanup_completes_while_another_checkout_holds_a_provider_lease(
    tmp_path: Path,
) -> None:
    """반증: 워커가 상시 도는 정상 상태에서 cleanup이 영원히 진입하지 못했다.

    provider lease는 저장소 전역이라 워크트리를 수십 개 돌리면 비는 순간이
    없다. cleanup이 그 전역 idle을 요구하면 체크아웃이 무한 누적된다. 정리
    대상 checkout과 무관한 lease는 정리를 막을 이유가 없다.
    """
    root = tmp_path / "repo"
    _init_repo(root)
    status, run_dir = _managed_run(root, "lease-neighbor")

    with W_ISO.provider_lease(root, capacity=2):
        result = W.run_worktree_cleanup_transaction(
            root=root,
            checkout_path=status.path,
            run_dir=run_dir,
            target_branch="main",
            integration_strategy="merge",
        )
        W.complete_worktree_cleanup(result)

    assert not status.path.exists()
    assert not W.worktree_branch_exists(root=root, branch=status.branch)


def test_run_lifecycle_lease_is_shared_and_recovers_after_owner_crash(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    lock_path = state_root / ".agent-flow" / "runs" / "active.lock"
    holder = subprocess.Popen(
        (
            sys.executable,
            "-c",
            (
                "import sys,time;"
                "from pathlib import Path;"
                "from agent_flow.core.worktree_isolation import exclusive_file_lease;"
                "lock=Path(sys.argv[1]);"
                "lease=exclusive_file_lease(lock);"
                "lease.__enter__();"
                "print('ready',flush=True);"
                "time.sleep(60)"
            ),
            str(lock_path),
        ),
        env={**os.environ, "PYTHONPATH": SRC},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        with pytest.raises(W.CleanupBlockedError, match="run lifecycle"):
            with W._run_start_exclusion(state_root):
                pytest.fail("cleanup entered while create-run lease was held")
        with pytest.raises(RuntimeError, match="another agent-flow run"):
            create_run(state_root, "default", "blocked", run_id="blocked")
    finally:
        holder.kill()
        holder.wait(timeout=5)

    with W._run_start_exclusion(state_root):
        pass
    run_dir = create_run(state_root, "default", "recovered", run_id="recovered")
    assert (run_dir / "active").is_file()
    assert lock_path.is_file()


def test_run_activation_blocks_cleanup_until_active_marker_is_durable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    status = W.create_worktree(
        root=root,
        plan=W.plan_worktree(root=root, name="activation"),
    )
    state_root = W.worktree_runtime_root(root=root, name=status.name)
    release = tmp_path / "release-activation"
    holder = subprocess.Popen(
        (
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import sys,time",
                    "from pathlib import Path",
                    "from agent_flow.artifact import create_run",
                    "from agent_flow.core.worktrees import worktree_run_activation",
                    "root,path,state,identity,release = map(Path, sys.argv[1:])",
                    "with worktree_run_activation("
                    "root=root, path=path, registration_identity=str(identity)):",
                    "    print('ready', flush=True)",
                    "    while not release.exists(): time.sleep(0.01)",
                    "    create_run("
                    "state, 'default', 'activation', run_id='activation-run', "
                    "checkout_identity='worktree:activation', "
                    "checkout_registration_identity=str(identity))",
                )
            ),
            str(root),
            str(status.path),
            str(state_root),
            status.registration_identity or "",
            str(release),
        ),
        env={**os.environ, "PYTHONPATH": SRC},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        with pytest.raises(W.CleanupBlockedError, match="activating a run"):
            with W._cleanup_lease(root, status.path):
                pytest.fail("cleanup entered before the active marker was durable")
    finally:
        release.write_text("release\n", encoding="utf-8")
        stdout, stderr = holder.communicate(timeout=10)
    assert holder.returncode == 0, stdout + stderr
    assert (state_root / ".agent-flow" / "runs" / "activation-run" / "active").is_file()
    assert status.path.exists()


def test_creating_one_checkout_does_not_block_retiring_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """반증: 생성이 저장소 전역 cleanup 인터록을 쥐어 남의 정리를 막았다.

    수십 개가 상시 생성·정리되는 상태에서 전역 인터록은 양방향 기아를 만든다.
    서로 다른 checkout의 생성과 정리는 간섭하지 않아야 한다.
    """
    root = tmp_path / "repo"
    _init_repo(root)
    retiring = W.create_worktree(
        root=root, plan=W.plan_worktree(root=root, name="retiring")
    )
    original_add = W._add_worktree_locked
    observed = False

    def add_while_retiring_another(*, root: Path, plan: W.WorktreePlan) -> bool:
        nonlocal observed
        with W._cleanup_lease(root, retiring.path):
            observed = True
        return original_add(root=root, plan=plan)

    monkeypatch.setattr(W, "_add_worktree_locked", add_while_retiring_another)
    created = W.create_worktree(
        root=root, plan=W.plan_worktree(root=root, name="creating")
    )

    assert observed
    assert created.path.exists()


def test_retiring_a_checkout_excludes_a_second_retirement_of_the_same_one(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    status = W.create_worktree(
        root=root, plan=W.plan_worktree(root=root, name="same-target")
    )

    with W._cleanup_lease(root, status.path):
        with pytest.raises(W.CleanupBlockedError, match="already being retired"):
            with W._cleanup_lease(root, status.path):
                pytest.fail("two cleanups entered the same checkout")


def test_manual_removal_preserves_checkout_with_active_run(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    status, run_dir = _managed_run(root, "active")

    with pytest.raises(W.CleanupBlockedError, match="active run"):
        W.remove_worktree(root=root, status=status, allow_unmerged=True)

    assert status.path.exists()
    assert (run_dir / "active").exists()
    assert W.worktree_branch_exists(root=root, branch=status.branch)



def test_cleanup_preserves_checkout_with_ignored_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    (root / ".gitignore").write_text("cache/\n", encoding="utf-8")
    _git("add", ".gitignore", cwd=root)
    _git("commit", "-m", "chore: ignore cache", cwd=root)
    status, run_dir = _managed_run(root, "ignored")
    ignored = status.path / "cache" / "valuable.bin"
    ignored.parent.mkdir()
    ignored.write_bytes(b"not disposable")

    with pytest.raises(W.CleanupBlockedError, match="checkout is dirty"):
        W.run_worktree_cleanup_transaction(
            root=root,
            checkout_path=status.path,
            run_dir=run_dir,
            target_branch="main",
            integration_strategy="merge",
        )

    assert ignored.read_bytes() == b"not disposable"
    assert W.worktree_branch_exists(root=root, branch=status.branch)


def test_unmerged_head_is_archived_but_checkout_and_ref_are_preserved(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    status = W.create_worktree(root=root, plan=W.plan_worktree(root=root, name="unmerged"))
    (status.path / "feature.txt").write_text("not integrated\n", encoding="utf-8")
    _git("add", ".", cwd=status.path)
    _git("commit", "-m", "feat: unmerged", cwd=status.path)
    state_root = W.worktree_runtime_root(root=root, name=status.name)
    run_dir = create_run(
        state_root,
        "default",
        "unmerged cleanup",
        run_id="run-unmerged",
        checkout_identity=f"worktree:{status.name}",
        checkout_registration_identity=status.registration_identity,
    )

    with pytest.raises(W.CleanupBlockedError, match="cannot prove") as caught:
        W.run_worktree_cleanup_transaction(
            root=root,
            checkout_path=status.path,
            run_dir=run_dir,
            target_branch="main",
            integration_strategy="merge",
        )

    assert status.path.exists()
    assert W.worktree_branch_exists(root=root, branch=status.branch)
    assert caught.value.journal_path is not None
    journal = json.loads(caught.value.journal_path.read_text(encoding="utf-8"))
    assert journal["steps"]["archive"]["status"] == "done"
    assert journal["steps"]["integration_proof"]["status"] == "pending"


def test_cleanup_rejects_recreated_checkout_with_same_path_branch_and_head(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    status, run_dir = _managed_run(root, "recreated")
    _, journal = W._prepare_or_load_cleanup_journal(
        root=root,
        checkout_path=status.path,
        run_dir=run_dir,
        target_branch="main",
        integration_strategy="merge",
        delete_branch=True,
    )

    _git("worktree", "remove", "--force", str(status.path), cwd=root)
    _git("worktree", "add", str(status.path), status.branch, cwd=root)

    with pytest.raises(W.CleanupBlockedError, match="registration changed"):
        W._validate_cleanup_snapshot(
            root=root,
            journal=journal,
            require_clean=True,
        )


def test_branch_deletion_uses_expected_oid_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    replacement_oid = _init_repo(root, second_commit=True)
    status, run_dir = _managed_run(root, "cas")
    expected_oid = _git("rev-parse", status.branch, cwd=root).stdout.strip()
    real_run_git = W._run_git

    def swap_ref_after_checkout_removal(
        command_root: Path, *args: str
    ) -> subprocess.CompletedProcess[str]:
        result = real_run_git(command_root, *args)
        if args[:2] == ("worktree", "remove"):
            _git(
                "update-ref",
                f"refs/heads/{status.branch}",
                replacement_oid,
                expected_oid,
                cwd=root,
            )
        return result

    monkeypatch.setattr(W, "_run_git", swap_ref_after_checkout_removal)
    with pytest.raises(W.CleanupBlockedError, match="changed before CAS") as caught:
        W.run_worktree_cleanup_transaction(
            root=root,
            checkout_path=status.path,
            run_dir=run_dir,
            target_branch="main",
            integration_strategy="merge",
        )

    assert _git("rev-parse", status.branch, cwd=root).stdout.strip() == replacement_oid
    assert caught.value.journal_path is not None
    journal = json.loads(caught.value.journal_path.read_text(encoding="utf-8"))
    assert journal["steps"]["checkout_removal"]["status"] == "done"
    assert journal["steps"]["branch_ref_cas"]["status"] == "pending"
    assert W.worktree_runtime_root(root=root, name=status.name).exists()


@pytest.mark.parametrize("crash_step", W.CLEANUP_STEPS)
def test_cleanup_resumes_after_crash_after_each_durable_step(
    tmp_path: Path, crash_step: str
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    status, run_dir = _managed_run(root, f"resume-{crash_step}")

    class SimulatedCrash(RuntimeError):
        pass

    def crash_after(step: str) -> None:
        if step == crash_step:
            raise SimulatedCrash(step)

    with pytest.raises(SimulatedCrash, match=crash_step):
        W.run_worktree_cleanup_transaction(
            root=root,
            checkout_path=status.path,
            run_dir=run_dir,
            target_branch="main",
            integration_strategy="merge",
            after_step=crash_after,
        )

    pending = W.find_pending_worktree_cleanup(root=root, selector=status.name)
    assert pending is not None
    journal = json.loads(pending.journal_path.read_text(encoding="utf-8"))
    assert journal["steps"][crash_step]["status"] == "done"

    resumed_steps: list[str] = []
    resumed = W.run_worktree_cleanup_transaction(
        root=root,
        checkout_path=status.path,
        run_dir=pending.run_dir,
        target_branch="main",
        integration_strategy="merge",
        after_step=resumed_steps.append,
    )
    crash_index = W.CLEANUP_STEPS.index(crash_step)
    assert resumed_steps == list(W.CLEANUP_STEPS[crash_index + 1 :])
    archived_run = W.complete_worktree_cleanup(resumed)
    final = json.loads(resumed.journal_path.read_text(encoding="utf-8"))
    assert final["status"] == "complete"
    assert all(final["steps"][step]["status"] == "done" for step in W.CLEANUP_STEPS)
    assert not (archived_run / "active").exists()
    assert not status.path.exists()
    assert not W.worktree_branch_exists(root=root, branch=status.branch)


def test_cleanup_resume_rejects_journal_replayed_into_recreated_repository(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    status, run_dir = _managed_run(root, "repository-replay")

    def crash_after_archive(step: str) -> None:
        if step == "archive":
            raise RuntimeError("crash after archive")

    with pytest.raises(RuntimeError, match="crash after archive"):
        W.run_worktree_cleanup_transaction(
            root=root,
            checkout_path=status.path,
            run_dir=run_dir,
            target_branch="main",
            integration_strategy="merge",
            after_step=crash_after_archive,
        )

    old_git = tmp_path / "old-git"
    (root / ".git").rename(old_git)
    shutil.copytree(old_git, root / ".git")
    replayed = W.find_pending_worktree_cleanup(root=root, selector=status.name)
    assert replayed is not None

    with pytest.raises(
        W.CleanupBlockedError,
        match="cleanup journal repository identity changed",
    ):
        W.run_worktree_cleanup_transaction(
            root=root,
            checkout_path=status.path,
            run_dir=replayed.run_dir,
            target_branch="main",
            integration_strategy="merge",
        )

    assert status.path.exists()
    assert replayed.journal_path.exists()


@pytest.mark.parametrize("field", ("path", "expected_head_oid"))
def test_cleanup_resume_rejects_tampered_checkout_identity(
    tmp_path: Path,
    field: str,
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    status, run_dir = _managed_run(root, f"tampered-{field}")

    def crash_after_archive(step: str) -> None:
        if step == "archive":
            raise RuntimeError("crash after archive")

    with pytest.raises(RuntimeError, match="crash after archive"):
        W.run_worktree_cleanup_transaction(
            root=root,
            checkout_path=status.path,
            run_dir=run_dir,
            target_branch="main",
            integration_strategy="merge",
            after_step=crash_after_archive,
        )

    pending = W.find_pending_worktree_cleanup(
        root=root,
        selector=status.name,
    )
    assert pending is not None
    journal = json.loads(pending.journal_path.read_text(encoding="utf-8"))
    journal["checkout"][field] = (
        str(tmp_path / "different-worktree")
        if field == "path"
        else "0" * 40
    )
    pending.journal_path.write_text(
        json.dumps(journal),
        encoding="utf-8",
    )

    with pytest.raises(W.CleanupBlockedError):
        W.run_worktree_cleanup_transaction(
            root=root,
            checkout_path=status.path,
            run_dir=pending.run_dir,
            target_branch="main",
            integration_strategy="merge",
        )

    assert status.path.exists()
    assert pending.journal_path.exists()


def test_cleanup_archives_inactive_run_history_before_metadata_removal(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    status = W.create_worktree(
        root=root, plan=W.plan_worktree(root=root, name="history")
    )
    state_root = W.worktree_runtime_root(root=root, name=status.name)
    old_run = create_run(
        state_root,
        "default",
        "old run",
        run_id="run-old",
        checkout_identity=f"worktree:{status.name}",
        checkout_registration_identity=status.registration_identity,
    )
    (old_run / "evidence.txt").write_text("keep me\n", encoding="utf-8")
    mark_inactive(old_run)
    current_run = create_run(
        state_root,
        "default",
        "current run",
        run_id="run-current",
        checkout_identity=f"worktree:{status.name}",
        checkout_registration_identity=status.registration_identity,
    )

    result = W.run_worktree_cleanup_transaction(
        root=root,
        checkout_path=status.path,
        run_dir=current_run,
        target_branch="main",
        integration_strategy="merge",
    )
    archived_current = W.complete_worktree_cleanup(result)
    archived_old = archived_current.parent / "run-old"

    assert (archived_old / "evidence.txt").read_text(encoding="utf-8") == "keep me\n"
    assert read_meta(archived_old)["run_id"] == "run-old"
    assert not state_root.exists()

def test_status_prefers_pending_cleanup_runtime_while_checkout_still_exists(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    status, run_dir = _managed_run(root, "pending-status")

    def crash_after_archive(step: str) -> None:
        if step == "archive":
            raise RuntimeError("crash after archive")

    with pytest.raises(RuntimeError, match="crash after archive"):
        W.run_worktree_cleanup_transaction(
            root=root,
            checkout_path=status.path,
            run_dir=run_dir,
            target_branch="main",
            integration_strategy="merge",
            after_step=crash_after_archive,
        )

    assert status.path.exists()
    pending = W.find_pending_worktree_cleanup(root=root, selector=status.name)
    assert pending is not None
    checkout_root, state_root = cli_module._worktree_context(root, status.name)
    assert checkout_root == status.path
    assert state_root == W.cleanup_state_root(pending)
    assert cli_module.main(
        ["status", "--root", str(root), "--worktree", status.name]
    ) == 0
    assert "run-cleanup" in capsys.readouterr().out



def test_cleanup_resume_rejects_archive_payload_tampering(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    status, run_dir = _managed_run(root, "archive-tamper")
    (run_dir / "evidence.md").write_text("verified\n", encoding="utf-8")

    def crash_after_archive(step: str) -> None:
        if step == "archive":
            raise RuntimeError("crash after archive")

    with pytest.raises(RuntimeError, match="crash after archive"):
        W.run_worktree_cleanup_transaction(
            root=root,
            checkout_path=status.path,
            run_dir=run_dir,
            target_branch="main",
            integration_strategy="merge",
            after_step=crash_after_archive,
        )
    pending = W.find_pending_worktree_cleanup(root=root, selector=status.name)
    assert pending is not None
    journal = json.loads(pending.journal_path.read_text(encoding="utf-8"))
    assert len(journal["run"]["archive_digest"]) == 64
    (pending.run_dir / "evidence.md").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(W.CleanupBlockedError, match="checksum"):
        W.run_worktree_cleanup_transaction(
            root=root,
            checkout_path=status.path,
            run_dir=pending.run_dir,
            target_branch="main",
            integration_strategy="merge",
        )

    assert status.path.exists()
    assert W.worktree_branch_exists(root=root, branch=status.branch)


def test_cleanup_resume_preserves_checkout_when_archived_run_disappears(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    status, run_dir = _managed_run(root, "archive-missing")

    def crash_after_archive(step: str) -> None:
        if step == "archive":
            raise RuntimeError("crash after archive")

    with pytest.raises(RuntimeError, match="crash after archive"):
        W.run_worktree_cleanup_transaction(
            root=root,
            checkout_path=status.path,
            run_dir=run_dir,
            target_branch="main",
            integration_strategy="merge",
            after_step=crash_after_archive,
        )
    pending = W.find_pending_worktree_cleanup(root=root, selector=status.name)
    assert pending is not None
    shutil.rmtree(pending.run_dir)
    pending = W.find_pending_worktree_cleanup(root=root, selector=status.name)
    assert pending is not None

    with pytest.raises(W.CleanupBlockedError, match="archive identity"):
        W.run_worktree_cleanup_transaction(
            root=root,
            checkout_path=status.path,
            run_dir=pending.run_dir,
            target_branch="main",
            integration_strategy="merge",
        )

    assert run_dir.exists()
    assert status.path.exists()
    assert W.worktree_branch_exists(root=root, branch=status.branch)


def test_target_drift_after_archive_refreshes_proof_before_branch_cas(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    status, run_dir = _managed_run(root, "target-drift")
    advanced_oid = ""

    def drift_after_archive(step: str) -> None:
        nonlocal advanced_oid
        if step != "archive":
            return
        (root / "drift.txt").write_text("target moved\n", encoding="utf-8")
        _git("add", ".", cwd=root)
        _git("commit", "-m", "chore: move target", cwd=root)
        advanced_oid = _git("rev-parse", "main", cwd=root).stdout.strip()

    result = W.run_worktree_cleanup_transaction(
        root=root,
        checkout_path=status.path,
        run_dir=run_dir,
        target_branch="main",
        integration_strategy="merge",
        after_step=drift_after_archive,
    )
    W.complete_worktree_cleanup(result)

    journal = json.loads(result.journal_path.read_text(encoding="utf-8"))
    assert journal["target"]["expected_oid"] == advanced_oid
    assert journal["integration"]["proof"] == "verified"
    assert not status.path.exists()
    assert not W.worktree_branch_exists(root=root, branch=status.branch)


def test_target_drift_without_feature_integration_preserves_checkout_and_branch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    status, run_dir = _managed_run(root, "target-drift-unmerged")
    (status.path / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git("add", ".", cwd=status.path)
    _git("commit", "-m", "feat: unmerged", cwd=status.path)

    def drift_after_archive(step: str) -> None:
        if step != "archive":
            return
        (root / "drift.txt").write_text("target moved\n", encoding="utf-8")
        _git("add", ".", cwd=root)
        _git("commit", "-m", "chore: move target", cwd=root)

    with pytest.raises(W.CleanupBlockedError, match="cannot prove"):
        W.run_worktree_cleanup_transaction(
            root=root,
            checkout_path=status.path,
            run_dir=run_dir,
            target_branch="main",
            integration_strategy="merge",
            after_step=drift_after_archive,
        )

    assert status.path.exists()
    assert W.worktree_branch_exists(root=root, branch=status.branch)


def test_branch_cas_retries_lock_contention_and_remains_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    status, run_dir = _managed_run(root, "cas-retry")
    real_git_safe = W.git_safe
    attempts = 0

    def flaky_git(*args, **kwargs):
        nonlocal attempts
        if args[:2] == ("update-ref", "--stdin"):
            attempts += 1
            if attempts == 1:
                return SafeCommandResult(
                    args=("git", "update-ref", "--stdin"),
                    returncode=128,
                    stdout="",
                    stderr=(
                        "fatal: Unable to create '.git/packed-refs.lock': "
                        "File exists."
                    ),
                )
        return real_git_safe(*args, **kwargs)

    monkeypatch.setattr(W, "git_safe", flaky_git)
    result = W.run_worktree_cleanup_transaction(
        root=root,
        checkout_path=status.path,
        run_dir=run_dir,
        target_branch="main",
        integration_strategy="merge",
    )
    W.complete_worktree_cleanup(result)

    assert attempts == 2
    assert not W.worktree_branch_exists(root=root, branch=status.branch)
    W._delete_branch_ref_cas(
        root=root,
        branch=status.branch,
        expected_oid="0" * 40,
    )


def test_terminal_cleanup_resumes_after_journal_commit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    status, run_dir = _managed_run(root, "terminal-resume")
    result = W.run_worktree_cleanup_transaction(
        root=root,
        checkout_path=status.path,
        run_dir=run_dir,
        target_branch="main",
        integration_strategy="merge",
    )
    journal = json.loads(result.journal_path.read_text(encoding="utf-8"))
    journal["status"] = "complete"
    journal["terminal_completed_at"] = W._utc_now()
    W._write_cleanup_journal(result.journal_path, journal)

    pending = W.find_pending_worktree_cleanup(root=root, selector=status.name)
    assert pending is not None
    archived = W.complete_worktree_cleanup(pending)
    W.complete_worktree_cleanup(result)

    assert read_meta(archived)["cleanup_state"] == "complete"
    assert not (archived / "active").exists()


def test_runner_resume_rejects_recreated_checkout_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    status, run_dir = _managed_run(root, "runner-recreated")
    _git("worktree", "remove", "--force", str(status.path), cwd=root)
    _git("worktree", "add", str(status.path), status.branch, cwd=root)
    runner = Runner(
        status.path,
        state_root=W.worktree_runtime_root(root=root, name=status.name),
        config_root=root,
        workflow="default",
        run_dir=run_dir,
    )
    monkeypatch.setattr(
        runner_module,
        "assert_managed_hooks_registered",
        lambda *_args: None,
    )

    with pytest.raises(W.WorktreeIsolationError, match="registration changed"):
        runner.run(ResumeMode.RESUME)


def test_runner_does_not_complete_from_cleanup_artifact_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    status, run_dir = _managed_run(root, "runner")
    runner = Runner(
        status.path,
        state_root=W.worktree_runtime_root(root=root, name=status.name),
        config_root=root,
        workflow="default",
        run_dir=run_dir,
        checkout_identity=f"worktree:{status.name}",
    )
    meta = read_meta(run_dir)
    meta["phase_index"] = len(runner.phases)
    meta["current_phase"] = None
    write_meta(run_dir, meta)
    (run_dir / "cleanup.md").write_text("# cleanup\n\nstatus: complete\n", encoding="utf-8")

    class Adapter:
        name = "generic"

    monkeypatch.setattr(runner_module, "assert_managed_hooks_registered", lambda *_args: None)
    monkeypatch.setattr(runner_module, "detect_adapter", lambda: Adapter())
    monkeypatch.setattr(runner_module, "detect_available_clis", lambda: [])

    def write_report(path: Path) -> Path:
        report = path / "run-report.md"
        report.write_text("report\n", encoding="utf-8")
        return report

    monkeypatch.setattr(runner_module, "write_run_report", write_report)
    monkeypatch.setattr(
        runner_module,
        "run_worktree_cleanup_transaction",
        lambda **_kwargs: (_ for _ in ()).throw(
            W.CleanupBlockedError("integration proof is unknown")
        ),
    )

    runner.run(ResumeMode.RESUME)

    assert (run_dir / "active").exists()
    assert "status: blocked" in capsys.readouterr().out


def test_runner_completes_after_journaled_clean_integrated_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    status, run_dir = _managed_run(root, "runner-success")
    runner = Runner(
        status.path,
        state_root=W.worktree_runtime_root(root=root, name=status.name),
        config_root=root,
        workflow="default",
        run_dir=run_dir,
        checkout_identity=f"worktree:{status.name}",
    )
    meta = read_meta(run_dir)
    meta["phase_index"] = len(runner.phases)
    meta["current_phase"] = None
    write_meta(run_dir, meta)

    class Adapter:
        name = "generic"

    monkeypatch.setattr(runner_module, "assert_managed_hooks_registered", lambda *_args: None)
    monkeypatch.setattr(runner_module, "detect_adapter", lambda: Adapter())
    monkeypatch.setattr(runner_module, "detect_available_clis", lambda: [])

    def write_report(path: Path) -> Path:
        report = path / "run-report.md"
        report.write_text("report\n", encoding="utf-8")
        return report

    monkeypatch.setattr(runner_module, "write_run_report", write_report)

    runner.run(ResumeMode.RESUME)

    archived_meta = read_meta(runner.run_dir)
    journal_path = Path(archived_meta["cleanup_journal"])
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert "run complete" in capsys.readouterr().out
    assert runner.run_dir != run_dir
    assert archived_meta["cleanup_state"] == "complete"
    assert not (runner.run_dir / "active").exists()
    assert journal["status"] == "complete"
    assert all(journal["steps"][step]["status"] == "done" for step in W.CLEANUP_STEPS)
    assert not status.path.exists()
    assert not W.worktree_branch_exists(root=root, branch=status.branch)


def test_cli_remove_preserves_active_checkout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    status, run_dir = _managed_run(root, "cli-active")

    result = cli_module.main(
        [
            "worktree",
            "remove",
            "--root",
            str(root),
            "--name",
            status.name,
            "--allow-unmerged",
        ]
    )

    assert result == 2
    assert "active run" in capsys.readouterr().err
    assert status.path.exists()
    assert (run_dir / "active").exists()
