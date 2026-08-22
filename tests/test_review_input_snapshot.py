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
