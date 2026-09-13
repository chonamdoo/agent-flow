from __future__ import annotations

import errno
import hashlib
import os
import stat
import subprocess
from pathlib import Path

import pytest

from tests.test_runner_smoke import (
    _init_git_project,
    _run_cli,
    _worktree_runtime_root,
)
from agent_flow import artifact
from agent_flow.adapters.generic import GenericAdapter
from agent_flow.core.atomic_io import atomic_write_text
from agent_flow.core.review_evidence import (
    ReviewerOutcome,
    load_review_evidence,
    review_evidence_record,
    review_results_path,
    review_route_evidence,
    serialize_review_results,
)
from agent_flow.runner import ResumeMode, Runner


def _git(project: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=project, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _publication_run(tmp_path: Path) -> tuple[Path, Path, str, str]:
    project = tmp_path / "project"
    project.mkdir()
    _init_git_project(project)
    base_oid = _git(project, "rev-parse", "HEAD")
    _git(project, "commit", "--allow-empty", "-m", "publication change")
    head_oid = _git(project, "rev-parse", "HEAD")
    run_dir = artifact.create_run(project, "full-feature", "publication scope", run_id="scope-run")
    runner = Runner(project, run_dir=run_dir)
    meta = artifact.read_meta(run_dir)
    meta.update({
        "current_phase": "fix-loop",
        "phase_index": next(i for i, phase in enumerate(runner.phases) if phase.id == "fix-loop"),
        "phase_entered_at": "2026-09-01T00:00:00+00:00",
        "fix_loop_rounds": {"fix-loop": 2, "refactor": 1},
    })
    artifact.write_meta(run_dir, meta)
    return project, run_dir, base_oid, head_oid


def _write_review(run_dir: Path, phase_id: str, verdict: str) -> None:
    binding = artifact.ensure_review_binding(run_dir)
    outcomes = []
    expected = {}
    for provider in ("claude", "codex"):
        name = f"{phase_id}-{provider}-generalist.md"
        text = f"reviewer-source: sub-agent\n## Reviewer verdict\nverdict: {verdict}\n"
        (run_dir / name).write_text(text, encoding="utf-8")
        job_id = f"{provider}:generalist"
        outcomes.append(ReviewerOutcome(
            job_id=job_id, provider=provider, model="test-model", effort="high",
            status="ok", verdict=verdict, required=False, artifact=name,
            artifact_sha256=hashlib.sha256(text.encode()).hexdigest(),
            prompt_digest="a" * 16, argv_digest="b" * 16,
        ))
        expected[provider] = [job_id]
    serialized = serialize_review_results(
        phase_id=phase_id, run_id=binding.run_id, nonce=binding.nonce,
        phase_entered_at=binding.phase_entered_at, outcomes=outcomes,
    )
    review_results_path(run_dir, phase_id).write_text(serialized, encoding="utf-8")
    record = review_evidence_record(
        nonce=binding.nonce, phase_entered_at=binding.phase_entered_at,
        serialized_results=serialized, outcomes=outcomes,
        expected_job_ids_by_provider=expected,
    )
    artifact.bind_review_evidence(
        run_dir, phase_id=phase_id, run_id=binding.run_id, nonce=binding.nonce,
        phase_entered_at=binding.phase_entered_at, record=record,
    )
    (run_dir / f"{phase_id}.md").write_text(
        f"## Overall\nverdict: {verdict}\n", encoding="utf-8",
    )


def _run_bytes(run_dir: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(run_dir)): path.read_bytes()
        for path in run_dir.rglob("*")
        if path.is_file() and (path.suffix in {".json", ".md", ".patch"} or path.name == "active")
    }


def test_publication_review_scope_preserves_rejections_and_invalidates_approvals(tmp_path: Path):
    project, run_dir, base_oid, head_oid = _publication_run(tmp_path)
    _write_review(run_dir, "final-review", "request-changes")
    _write_review(run_dir, "architecture-review", "approve")
    (run_dir / "final-review-review-input.patch").write_bytes(b"+pre-baseline defect\n")
    (run_dir / "fix-loop.md").write_text("status: complete\n", encoding="utf-8")
    meta = artifact.read_meta(run_dir)
    meta["phase_approval_request"] = {
        "phase_id": "fix-loop", "phase_entered_at": meta["phase_entered_at"],
        "artifact": "fix-loop.md",
    }
    artifact.write_meta(run_dir, meta)
    pending = artifact.pending_phase_approval(run_dir)
    assert pending is not None
    artifact.approve_phase_artifact(run_dir, token=pending["token"])
    old_meta = artifact.read_meta(run_dir)
    old_binding = artifact.ensure_review_binding(run_dir)
    old_bytes = _run_bytes(run_dir)
    evidence, route = review_route_evidence(
        run_dir, "final-review", old_bytes["final-review.md"].decode(), run_meta=old_meta,
    )
    assert evidence.validation == "verified"
    assert route == "request-changes"
    assert review_route_evidence(
        run_dir, "architecture-review", old_bytes["architecture-review.md"].decode(),
        run_meta=old_meta,
    )[1] == "approve"

    artifact.select_publication_review_scope(run_dir, project, base_oid=base_oid)

    selected = artifact.read_meta(run_dir)
    for key in ("current_phase", "phase_index", "phase_entered_at", "fix_loop_rounds", "review_evidence"):
        assert selected[key] == old_meta[key]
    assert selected["review_nonce"] != old_binding.nonce
    assert "phase_approval" not in selected
    assert "phase_approval_request" not in selected
    with pytest.raises(ValueError):
        artifact.approve_phase_artifact(run_dir, token=pending["token"])
    with pytest.raises(artifact.ReviewEvidenceBindingError):
        artifact.bind_review_evidence(
            run_dir, phase_id="final-review", run_id=old_binding.run_id,
            nonce=old_binding.nonce, phase_entered_at=old_binding.phase_entered_at,
            record=old_meta["review_evidence"]["final-review"],
        )

    archives = [path for path in (run_dir / "review-history").iterdir() if path.is_dir()]
    assert len(archives) == 1
    archive = archives[0]
    preserved = {
        name: content for name, content in old_bytes.items()
        if name == "meta.json" or name.startswith(("final-review", "architecture-review"))
    }
    for name, content in preserved.items():
        assert (archive / name).read_bytes() == content
        if name != "meta.json":
            (run_dir / name).write_bytes(content)
    assert load_review_evidence(
        artifact_root=run_dir, phase_id="architecture-review", run_meta=selected,
    ).validation == "invalid"
    assert review_route_evidence(
        run_dir, "architecture-review", old_bytes["architecture-review.md"].decode(),
        run_meta=selected,
    )[1] != "approve"

    _write_review(run_dir, "final-review", "approve")
    (run_dir / "final-review-review-input.patch").write_bytes(b"+publication-only change\n")
    for name, content in preserved.items():
        assert (archive / name).read_bytes() == content
    assert review_route_evidence(
        archive, "final-review", old_bytes["final-review.md"].decode(), run_meta=old_meta,
    )[1] == "request-changes"
    current_bytes = _run_bytes(run_dir)
    artifact.select_publication_review_scope(run_dir, project, base_oid=base_oid)
    assert _run_bytes(run_dir) == current_bytes
    with pytest.raises(ValueError):
        artifact.select_publication_review_scope(run_dir, project, base_oid=head_oid)
    assert _run_bytes(run_dir) == current_bytes


@pytest.mark.parametrize("failed_directory", ["archive", "history", "run"])
def test_publication_scope_preserves_prior_state_when_history_sync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_directory: str,
):
    project, run_dir, base_oid, _head_oid = _publication_run(tmp_path)
    _write_review(run_dir, "final-review", "request-changes")
    before = _run_bytes(run_dir)
    real_fsync = os.fsync

    def fail_history_sync(fd: int) -> None:
        descriptor = os.fstat(fd)
        if stat.S_ISDIR(descriptor.st_mode):
            history = run_dir / "review-history"
            targets = {
                "archive": next(history.iterdir()) if history.exists() else history,
                "history": history,
                "run": run_dir,
            }
            target = targets[failed_directory]
            if target.exists():
                expected = target.stat()
                if (descriptor.st_dev, descriptor.st_ino) == (expected.st_dev, expected.st_ino):
                    raise OSError(errno.EIO, "review history synchronization failed")
        real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_history_sync)
        with pytest.raises(OSError, match="review history synchronization failed"):
            artifact.select_publication_review_scope(run_dir, project, base_oid=base_oid)

    for name, content in before.items():
        assert (run_dir / name).read_bytes() == content
    artifact.select_publication_review_scope(run_dir, project, base_oid=base_oid)
    assert artifact.read_meta(run_dir)["review_scope"]["base_oid"] == base_oid
    archive = next((run_dir / "review-history").iterdir())
    assert (archive / artifact.META_FILE).read_bytes() == before[artifact.META_FILE]
    assert (archive / "final-review.md").read_bytes() == before["final-review.md"]


def test_atomic_write_keeps_committed_result_when_directory_sync_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "config.json"
    target.write_text("old", encoding="utf-8")
    real_fsync = os.fsync

    def fail_directory_sync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "directory synchronization unavailable")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_sync)
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize("baseline", ["HEAD", "malformed", "missing", "nonancestor"])
def test_publication_review_scope_rejects_invalid_baselines(tmp_path: Path, baseline: str):
    project, run_dir, _base_oid, _head_oid = _publication_run(tmp_path)
    candidates = {"HEAD": "HEAD", "malformed": "abc123", "missing": "0" * 40}
    if baseline == "nonancestor":
        tree = _git(project, "rev-parse", "HEAD^{tree}")
        base_oid = _git(project, "commit-tree", tree, "-m", "unrelated history")
    else:
        base_oid = candidates[baseline]
    before = _run_bytes(run_dir)
    with pytest.raises(ValueError):
        artifact.select_publication_review_scope(run_dir, project, base_oid=base_oid)
    assert _run_bytes(run_dir) == before


@pytest.mark.parametrize("phase_id", ["merge", "merge-approval"])
@pytest.mark.parametrize("completed_artifact", [False, True])
def test_publication_review_scope_blocks_merge_in_status_and_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    phase_id: str, completed_artifact: bool,
):
    import agent_flow.runner as runner_module

    project, run_dir, base_oid, _head_oid = _publication_run(tmp_path)
    artifact.select_publication_review_scope(run_dir, project, base_oid=base_oid)
    runner = Runner(project, run_dir=run_dir)
    phase_index = next(i for i, phase in enumerate(runner.phases) if phase.id == phase_id)
    phase = runner.phases[phase_index]
    meta = artifact.read_meta(run_dir)
    meta.update({"current_phase": phase_id, "phase_index": phase_index})
    if completed_artifact:
        artifact_path = runner._artifact_path(phase)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("status: complete\nverdict: approve\n", encoding="utf-8")
        meta["phase_approval_request"] = {
            "phase_id": phase_id, "phase_entered_at": meta["phase_entered_at"],
            "artifact": str(artifact_path.relative_to(run_dir)),
        }
    artifact.write_meta(run_dir, meta)
    if completed_artifact:
        pending = artifact.pending_phase_approval(run_dir)
        assert pending is not None
        artifact.approve_phase_artifact(run_dir, token=pending["token"])
    before_status = _run_bytes(run_dir)
    active = artifact.find_active_run(project)
    assert active is not None
    active.print_status(config_root=project, project_root=project)
    status = capsys.readouterr().out
    assert "status: blocked" in status
    assert "reason: publication_review_cannot_approve_merge" in status
    assert _run_bytes(run_dir) == before_status

    def forbidden_adapter_call(*args, **kwargs):
        pytest.fail("publication-only review reached merge adapter execution or rendering")

    adapter = GenericAdapter()
    monkeypatch.setattr(adapter, "execute", forbidden_adapter_call)
    monkeypatch.setattr(adapter, "render_envelope", forbidden_adapter_call)
    monkeypatch.setattr(runner_module, "detect_adapter", lambda: adapter)
    monkeypatch.setattr(runner_module, "detect_available_clis", lambda: [])
    runner.run(ResumeMode.RESUME)
    execution = capsys.readouterr().out
    assert "status: blocked" in execution
    assert "reason: publication_review_cannot_approve_merge" in execution
    after = artifact.read_meta(run_dir)
    assert after["current_phase"] == phase_id
    assert after["phase_index"] == phase_index
    assert (run_dir / "active").exists()
    assert not (run_dir / "artifacts" / "handoff.md").exists()
    if completed_artifact:
        assert artifact_path.read_bytes() == before_status[str(artifact_path.relative_to(run_dir))]


@pytest.mark.parametrize("active,phase_id", [(False, "fix-loop"), (True, "green"), (True, "multi-review")])
def test_publication_review_scope_requires_active_fix_loop(tmp_path: Path, active: bool, phase_id: str):
    project, run_dir, base_oid, _head_oid = _publication_run(tmp_path)
    meta = artifact.read_meta(run_dir)
    meta["current_phase"] = phase_id
    runner = Runner(project, run_dir=run_dir)
    meta["phase_index"] = next(i for i, phase in enumerate(runner.phases) if phase.id == phase_id)
    artifact.write_meta(run_dir, meta)
    if not active:
        artifact.mark_inactive(run_dir)
    before = _run_bytes(run_dir)
    with pytest.raises(ValueError):
        artifact.select_publication_review_scope(run_dir, project, base_oid=base_oid)
    assert _run_bytes(run_dir) == before


def test_publication_review_scope_cli_targets_existing_bound_run(tmp_path: Path):
    project = tmp_path / "cli-project"
    project.mkdir()
    _init_git_project(project)
    env = {"AGENT_FLOW_NO_UPDATE_CHECK": "1"}
    started = _run_cli(
        ["start", "full-feature", "--task", "publication scope", "--worktree", "scope"],
        project, env_extra=env,
    )
    assert started.returncode == 0, started.stderr
    runtime_root = _worktree_runtime_root(project, "feat-scope")
    active = artifact.find_active_run(runtime_root)
    assert active is not None
    run_dir = active.path
    meta = artifact.read_meta(run_dir)
    phases = Runner(project, run_dir=run_dir).phases
    meta.update({
        "current_phase": "fix-loop",
        "phase_index": next(i for i, phase in enumerate(phases) if phase.id == "fix-loop"),
        "phase_entered_at": "2026-09-01T00:00:00+00:00",
        "fix_loop_rounds": {"fix-loop": 2},
    })
    artifact.write_meta(run_dir, meta)
    base_oid = _git(project, "rev-parse", "HEAD")
    selected = _run_cli(
        ["review", "scope", "--publication-base", base_oid, "--root", str(project), "--worktree", "scope"],
        project, env_extra=env,
    )
    assert selected.returncode == 0, selected.stderr
    after = artifact.read_meta(run_dir)
    assert after["review_scope"] == {"kind": "publication", "base_oid": base_oid}
    assert after["current_phase"] == "fix-loop"
    assert after["fix_loop_rounds"] == {"fix-loop": 2}
    assert after["checkout_identity"] == meta["checkout_identity"]
    assert after["run_id"] == meta["run_id"]
    assert artifact.find_active_run(runtime_root).path == run_dir
    assert not (project / ".agent-flow" / "runs").exists()
