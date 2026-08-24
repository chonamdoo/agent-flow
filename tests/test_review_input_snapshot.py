"""Reviewer input snapshot — 리뷰어가 받는 유일한 증거의 계약.

`tests/test_cli.py`가 아니라 별도 파일인 이유: 여기서 검증하는 것은 CLI 표면이
아니라 adapter가 만드는 diff 스냅샷이고, 임시 git 저장소 픽스처를 요구한다.
`test_cli.py`는 unittest.TestCase 기반의 CLI/installer 스위트라 이 픽스처가
들어갈 자리가 아니다.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


KIT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = KIT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent_flow.adapters.hosted import (  # noqa: E402
    HostedAdapter,
    _base_candidate_refs,
    _profile_base_branch,
    _write_review_input_snapshot as _capture_review_input_snapshot,
)
from agent_flow.core.commands import SafeCommandResult  # noqa: E402
from agent_flow.core.worktree_isolation import WorktreeIsolationError  # noqa: E402


def _write_review_input_snapshot(
    project: Path,
    run_dir: Path,
    phase_id: str,
    *,
    base_branch: str | None = None,
) -> Path:
    return _capture_review_input_snapshot(
        project,
        run_dir,
        phase_id,
        base_branch=base_branch,
    ).path


def _git(project: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=project, check=True, capture_output=True, text=True
    )
    return result.stdout


def _init_repo(project: Path) -> None:
    project.mkdir(parents=True, exist_ok=True)
    _git(project, "init", "-b", "main")
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "Test User")
    _git(project, "config", "commit.gpgsign", "false")
    (project / "app.py").write_text("base\n", encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "init")


def _run_dir(project: Path) -> Path:
    run_dir = project / ".agent-flow" / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _committed_feature_repo(project: Path) -> None:
    _init_repo(project)
    _git(project, "checkout", "-b", "feat-x")
    (project / "app.py").write_text("committed\n", encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "feature")


def _push_initial_main(project: Path) -> None:
    remote = project.parent / "remote.git"
    _git(project, "init", "--bare", "-q", str(remote))
    _git(project, "remote", "add", "origin", str(remote))
    _git(project, "push", "-q", "-u", "origin", "main")


def _remote_ahead_repo(project: Path) -> None:
    """upstream이 앞선 상태. 로컬 `main`은 분기 지점에 멈춰 있다."""
    _init_repo(project)
    _push_initial_main(project)
    _git(project, "checkout", "-q", "-b", "upstream-work")
    (project / "upstream.txt").write_text("merged upstream\n", encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "upstream work")
    _git(project, "push", "-q", "origin", "upstream-work:main")
    _git(project, "checkout", "-q", "main")
    _git(project, "branch", "-D", "upstream-work")
    _git(project, "checkout", "-q", "-b", "feat-x")
    _git(project, "merge", "-q", "--no-edit", "origin/main")
    (project / "app.py").write_text("mine\n", encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "my change")


def _local_ahead_repo(project: Path) -> None:
    """로컬 `main`이 remote보다 앞선 상태. remote merge-base는 과거다."""
    _init_repo(project)
    _push_initial_main(project)
    (project / "shared.txt").write_text("local base advance\n", encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "local base advance")
    _git(project, "checkout", "-q", "-b", "feat-x")
    (project / "app.py").write_text("mine\n", encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "my change")


def _fork_tracking_repo(project: Path) -> None:
    """fork 체크아웃. 선언된 `main`은 `upstream/main`을 추적하고 `origin`은 fork다.

    `origin/main`은 fork에만 있는 커밋을 담아 더 앞서 있다. 그 커밋은 선언된 base
    대비 변경이므로 diff에 남아야 한다.
    """
    _init_repo(project)
    upstream = project.parent / "upstream.git"
    fork = project.parent / "fork.git"
    _git(project, "init", "--bare", "-q", str(upstream))
    _git(project, "init", "--bare", "-q", str(fork))
    _git(project, "remote", "add", "upstream", str(upstream))
    _git(project, "remote", "add", "origin", str(fork))
    _git(project, "push", "-q", "-u", "upstream", "main")
    _git(project, "checkout", "-q", "-b", "fork-work")
    (project / "fork-only.txt").write_text("fork only\n", encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "fork only")
    _git(project, "push", "-q", "origin", "fork-work:main")
    _git(project, "checkout", "-q", "main")
    _git(project, "branch", "-D", "fork-work")
    _git(project, "checkout", "-q", "-b", "feat-x")
    _git(project, "merge", "-q", "--no-edit", "origin/main")
    (project / "app.py").write_text("mine\n", encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "my change")


def _diverged_bases_repo(project: Path) -> None:
    """로컬 `main`과 `origin/main`이 갈라지고 브랜치가 둘 다 머지한 상태.

    두 merge-base는 서로의 조상이 아니다 — rev 하나로는 둘 다 뺄 수 없다.
    """
    _init_repo(project)
    _push_initial_main(project)
    _git(project, "checkout", "-q", "-b", "upstream-work")
    (project / "upstream.txt").write_text("merged upstream\n", encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "upstream work")
    _git(project, "push", "-q", "origin", "upstream-work:main")
    _git(project, "checkout", "-q", "main")
    _git(project, "branch", "-D", "upstream-work")
    (project / "local-only.txt").write_text("local base only\n", encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "local base only")
    _git(project, "checkout", "-q", "-b", "feat-x")
    _git(project, "merge", "-q", "--no-edit", "origin/main")
    (project / "app.py").write_text("mine\n", encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "my change")


def test_snapshot_carries_committed_work_of_the_branch(tmp_path: Path):
    project = tmp_path / "project"
    _committed_feature_repo(project)

    snapshot = _write_review_input_snapshot(
        project, _run_dir(project), "final-review", base_branch="main"
    )

    content = snapshot.read_text(encoding="utf-8")
    assert "git merge-base HEAD main" in content
    assert "-base" in content
    assert "+committed" in content


def test_snapshot_carries_uncommitted_work_alongside_committed(tmp_path: Path):
    project = tmp_path / "project"
    _committed_feature_repo(project)
    (project / "app.py").write_text("committed\ndirty\n", encoding="utf-8")
    (project / "new.txt").write_text("new\n", encoding="utf-8")

    snapshot = _write_review_input_snapshot(
        project, _run_dir(project), "final-review", base_branch="main"
    )

    content = snapshot.read_text(encoding="utf-8")
    assert "+committed" in content
    assert "+dirty" in content
    assert "?? new.txt" in content


def test_snapshot_excludes_commits_already_merged_upstream(tmp_path: Path):
    project = tmp_path / "project"
    _remote_ahead_repo(project)

    snapshot = _write_review_input_snapshot(
        project, _run_dir(project), "final-review", base_branch="main"
    )

    content = snapshot.read_text(encoding="utf-8")
    assert "git merge-base HEAD origin/main" in content
    assert "+mine" in content
    # 이미 upstream에 머지된 작업은 이 브랜치의 변경이 아니다.
    assert "upstream.txt" not in content
    # 기준점이 왜 로컬 base가 아닌지 리뷰어가 읽을 수 있어야 한다.
    assert "is behind `origin/main`" in content


def test_snapshot_keeps_the_local_base_when_it_is_ahead_of_the_remote(tmp_path: Path):
    project = tmp_path / "project"
    _local_ahead_repo(project)

    snapshot = _write_review_input_snapshot(
        project, _run_dir(project), "final-review", base_branch="main"
    )

    content = snapshot.read_text(encoding="utf-8")
    assert "git merge-base HEAD main" in content
    assert "origin/main" not in content
    assert "+mine" in content
    # 로컬 base가 이미 담고 있는 커밋은 diff에 없다.
    assert "shared.txt" not in content


def test_snapshot_follows_the_declared_base_upstream_not_origin(tmp_path: Path):
    project = tmp_path / "project"
    _fork_tracking_repo(project)

    # 후보는 선언된 base가 추적하는 remote다. `origin`은 fork라 정본이 아니다.
    assert _base_candidate_refs(project, "main", max_output_bytes=1 << 20) == (
        "main",
        "upstream/main",
    )

    snapshot = _write_review_input_snapshot(
        project, _run_dir(project), "final-review", base_branch="main"
    )

    content = snapshot.read_text(encoding="utf-8")
    assert "git merge-base HEAD origin/main" not in content
    assert "+mine" in content
    # fork에만 있는 커밋은 선언된 base 대비 변경이므로 diff에 남아야 한다.
    # `origin/`을 정본으로 두면 그 커밋의 merge-base가 기준점이 되어 조용히 빠진다.
    assert "fork-only.txt" in content



def test_snapshot_admits_what_an_unordered_baseline_cannot_exclude(tmp_path: Path):
    project = tmp_path / "project"
    _diverged_bases_repo(project)

    snapshot = _write_review_input_snapshot(
        project, _run_dir(project), "final-review", base_branch="main"
    )

    content = snapshot.read_text(encoding="utf-8")
    assert "git merge-base HEAD origin/main" in content
    assert "could not be ordered" in content
    # rev 하나로 두 base를 동시에 뺄 수 없다. 남는 쪽을 머리말이 인정해야 한다.
    assert "commits reachable only from `main` are still in the diff below" in content
    assert "local-only.txt" in content


def test_snapshot_prefers_the_remote_base_when_ordering_cannot_be_proven(
    tmp_path: Path,
    monkeypatch,
):
    from agent_flow.adapters import hosted

    project = tmp_path / "project"
    _remote_ahead_repo(project)
    monkeypatch.setattr(hosted, "git_proves_ancestor", lambda **kwargs: False)

    snapshot = _write_review_input_snapshot(
        project, _run_dir(project), "final-review", base_branch="main"
    )

    content = snapshot.read_text(encoding="utf-8")
    # 순서를 증명하지 못한 상태의 안전한 방향은 이미 통합된 쪽이다.
    assert "git merge-base HEAD origin/main" in content
    assert "could not be ordered" in content



def test_snapshot_names_the_head_fallback_when_base_is_absent(tmp_path: Path):
    project = tmp_path / "project"
    _committed_feature_repo(project)
    (project / "app.py").write_text("committed\ndirty\n", encoding="utf-8")

    snapshot = _write_review_input_snapshot(
        project, _run_dir(project), "final-review", base_branch="release/absent"
    )

    content = snapshot.read_text(encoding="utf-8")
    head_line = next(
        line for line in content.splitlines() if line.startswith("- diff baseline:")
    )
    assert "`HEAD`" in head_line
    assert "release/absent" in head_line
    assert "NOT below" in head_line
    # fallback이어도 미커밋 변경은 그대로 담긴다.
    assert "+dirty" in content
    # 커밋된 변경은 빠졌고, 그 사실이 머리말에 적혔다.
    assert "+committed" not in content


def test_snapshot_refuses_to_ship_no_evidence_when_base_is_unusable(tmp_path: Path):
    project = tmp_path / "project"
    _committed_feature_repo(project)

    with pytest.raises(WorktreeIsolationError, match="no diff is available"):
        _write_review_input_snapshot(
            project, _run_dir(project), "final-review", base_branch="release/absent"
        )


def test_snapshot_states_a_verified_empty_diff_for_review_only_work(tmp_path: Path):
    project = tmp_path / "project"
    _init_repo(project)

    snapshot = _write_review_input_snapshot(
        project, _run_dir(project), "review", base_branch="main"
    )

    content = snapshot.read_text(encoding="utf-8")
    assert "verified empty diff" in content
    assert "git merge-base HEAD main" in content


def test_snapshot_refuses_a_tracked_change_without_a_diff(tmp_path: Path, monkeypatch):
    from agent_flow.adapters import hosted

    def fake_git_safe(*args, **kwargs):
        stdout = " M app.py\n" if args[0] == "status" else ""
        return SafeCommandResult(
            args=("git", *args), returncode=0, stdout=stdout, stderr=""
        )

    monkeypatch.setattr(hosted, "git_safe", fake_git_safe)

    with pytest.raises(WorktreeIsolationError, match="tracked change"):
        hosted._write_review_input_snapshot(tmp_path, _run_dir(tmp_path), "review")


def test_snapshot_reports_truncation_instead_of_failing_the_round(
    tmp_path: Path,
    monkeypatch,
):
    from agent_flow.adapters import hosted

    def fake_git_safe(*args, **kwargs):
        stdout = " M app.py\n" if args[0] == "status" else "+partial hunk\n"
        return SafeCommandResult(
            args=("git", *args),
            returncode=-9,
            stdout=stdout,
            stderr="command output exceeded 2 bytes",
            output_truncated=True,
        )

    monkeypatch.setattr(hosted, "git_safe", fake_git_safe)

    snapshot = hosted._write_review_input_snapshot(
        tmp_path, _run_dir(tmp_path), "review"
    ).path

    content = snapshot.read_text(encoding="utf-8")
    assert "- note: truncated at" in content
    assert "+partial hunk" in content


def test_snapshot_still_fails_closed_on_a_broken_git(tmp_path: Path, monkeypatch):
    from agent_flow.adapters import hosted

    def fake_git_safe(*args, **kwargs):
        return SafeCommandResult(
            args=("git", *args),
            returncode=None,
            stdout="",
            stderr="git is gone",
            error="git is gone",
        )

    monkeypatch.setattr(hosted, "git_safe", fake_git_safe)

    with pytest.raises(WorktreeIsolationError, match="could not precompute"):
        hosted._write_review_input_snapshot(tmp_path, _run_dir(tmp_path), "review")


def test_profile_base_branch_prefers_branching_base():
    adapter = HostedAdapter("codex")
    adapter._profile_snapshot = {
        "branching": {"base": "release/26.7.10.x"},
        "pr": {"target_branch": "main"},
    }

    assert _profile_base_branch(adapter) == "release/26.7.10.x"


def test_profile_base_branch_falls_back_to_pr_target():
    adapter = HostedAdapter("codex")
    adapter._profile_snapshot = {"pr": {"target_branch": "develop"}}

    assert _profile_base_branch(adapter) == "develop"


def test_profile_base_branch_is_none_without_a_declaration():
    adapter = HostedAdapter("codex")
    adapter._profile_snapshot = {"review_angles": []}

    assert _profile_base_branch(adapter) is None
