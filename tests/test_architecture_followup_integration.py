from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

KIT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT_ROOT / "src"))

from agent_flow import runner as runner_module
from agent_flow.adapters.hosted import (
    HostedAdapter,
    _prepare_reviewer_jobs,
    _reviewer_jobs,
    _snapshot_document_scope,
    _write_review_input_snapshot,
    review_document_scope,
)
from agent_flow.artifact import read_meta, write_meta
from agent_flow.core.phase_workflow import parse_phase_workflow_definition
from agent_flow.core.skill_resolver import PhaseSkills
from agent_flow.core.skill_scope import scope_document_ids, scope_names
from agent_flow.runner import Phase, ResumeMode, Runner, _phases_from_definition


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in tuple(os.environ):
        if key.startswith("AGENT_FLOW_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLOW_ADAPTER", "codex")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _write(root, ".gitignore", ".agent-flow/\n")
    _write(root, ".agent-flow.project.yaml", "schema_version: 1\narchitecture:\n  mode: local\n  skill: skills/architecture/SKILL.md\n")
    _write(
        root, "skills/architecture/SKILL.md",
        "---\nname: architecture\nrequires_docs:\n"
        "  - references/common.md\n"
        "  - path: references/unconditional.md\n"
        "  - path: references/a.md\n    pathGlobs: ['apps/a/**']\n"
        "  - path: references/b.md\n    pathGlobs: ['apps/b/**']\n"
        "---\nROOT_NORM_BODY\n",
    )
    for name in ("common", "unconditional", "a", "b"):
        _write(root, f"skills/architecture/references/{name}.md", f"REFERENCE_BODY_{name}\n")
    _write(root, "apps/a/model.py", "value = 1\n")
    _write(root, "apps/b/model.py", "value = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Track complete architecture contract and application baseline")
    _git(root, "switch", "-c", "feat/scoped-documents")
    return root


def _runner(root: Path) -> Runner:
    runner = Runner(root, workflow="development")
    definition = runner.workflow
    payload = yaml.safe_load(definition.source_bytes)
    payload["phases"] = [{"id": "implement", "description": "Apply existing behavior"}]
    runner.workflow = parse_phase_workflow_definition(
        yaml.safe_dump(payload).encode(), source=definition.source,
        name="development", kit_owned=definition.kit_owned,
    )
    runner.phases = _phases_from_definition(runner.workflow)
    runner.profile = {"id": "generic", "branching": {"base": "main"}}
    runner._adapter_name = "codex"
    return runner


def _pin_runner(root: Path, phase: Phase) -> Runner:
    runner = _runner(root)
    runner.phases = [phase]
    runner.run_dir = root / ".agent-flow" / "runs" / "r1"
    runner.run_dir.mkdir(parents=True)
    write_meta(runner.run_dir, {"task": "Apply existing behavior"})
    runner._initialize_architecture_policy(ResumeMode.START)
    assert runner._refresh_architecture_norms(phase, pin=True) is None
    return runner


def _assert_documents(prompt: str, selected: set[str]) -> None:
    assert "ROOT_NORM_BODY" in prompt
    assert "REFERENCE_BODY_common" in prompt
    assert "REFERENCE_BODY_unconditional" in prompt
    for name in ("a", "b"):
        assert (f"REFERENCE_BODY_{name}" in prompt) == (name in selected)


def _assert_reviewer_documents(root: Path, run_dir: Path, snapshot, selected: set[str], *, adapter=None) -> None:
    adapter = adapter or HostedAdapter("codex")
    phase = Phase(id="final-review", description="Review the captured change", skills=PhaseSkills())
    jobs = _prepare_reviewer_jobs(
        phase, run_dir, root, adapter, review_input=snapshot, providers=("claude", "codex"),
    )
    assert {"generalist", "types"} <= {job.angle_id for job in jobs}
    for job in jobs:
        for provider in ("claude", "codex"):
            _assert_documents(job.prompt_by_provider[provider], selected)
            assert snapshot.digest in job.prompt_by_provider[provider]


@pytest.mark.parametrize("dirty", [False, True], ids=["committed-only", "committed-and-dirty"])
def test_runner_author_and_reviewers_include_committed_scope(project, monkeypatch, capsys, dirty):
    _write(project, "apps/a/model.py", "value = 2\n")
    _git(project, "add", "apps/a/model.py")
    _git(project, "commit", "-m", "Change application A")
    if dirty:
        _write(project, "apps/b/model.py", "value = 3\n")
        _write(project, 'apps/b/new "한글" file.py', "value = 4\n")
    monkeypatch.setattr(runner_module, "assert_managed_hooks_registered", lambda *args: None)
    runner = _runner(project)

    runner.run(ResumeMode.START, task="Apply existing behavior")

    selected = {"a", "b"} if dirty else {"a"}
    _assert_documents(capsys.readouterr().out, selected)
    assert runner.run_dir is not None
    snapshot = _write_review_input_snapshot(project, runner.run_dir, "final-review", base_branch="main")
    assert snapshot.document_scope is not None
    assert "apps/a/model.py" in snapshot.document_scope
    if dirty:
        assert 'apps/b/new "한글" file.py' in snapshot.document_scope
    _assert_reviewer_documents(project, runner.run_dir, snapshot, selected)
    _write(project, "skills/architecture/references/b.md", "Changed even when not delivered\n")
    assert runner._architecture_policy_block_reason() == "architecture_policy_drift"


def test_reviewer_snapshot_matches_both_rename_paths_and_deleted_paths(project):
    old = 'apps/a/old "한글" file.py'
    new = 'apps/b/new "한글" file.py'
    deleted = "apps/a/deleted file.py"
    _write(project, old, "value = 1\n")
    _write(project, deleted, "value = 2\n")
    _git(project, "add", "apps")
    _git(project, "commit", "-m", "Establish unusual path baseline")
    _git(project, "branch", "snapshot-base", "HEAD")
    _git(project, "mv", old, new)
    _git(project, "rm", deleted)
    run_dir = project / ".agent-flow" / "runs" / "r1"
    run_dir.mkdir(parents=True)

    snapshot = _write_review_input_snapshot(project, run_dir, "final-review", base_branch="snapshot-base")

    assert set(snapshot.document_scope or ()) == {old, new, deleted}
    _assert_reviewer_documents(project, run_dir, snapshot, {"a", "b"})


def test_review_scope_keeps_dirty_rename_paths_when_baseline_diff_cancels_rename(project):
    old, renamed = "apps/a/model.py", "apps/b/moved.py"
    _git(project, "mv", old, renamed)
    _git(project, "commit", "-m", "Move model to the other application")
    _git(project, "mv", renamed, old)
    _write(project, "apps/b/model.py", "value = 2\n")
    run_dir = project / ".agent-flow" / "runs" / "r1"
    run_dir.mkdir(parents=True)

    snapshot = _write_review_input_snapshot(project, run_dir, "final-review", base_branch="main")

    assert set(snapshot.document_scope or ()) == {old, renamed, "apps/b/model.py"}
    _assert_reviewer_documents(project, run_dir, snapshot, {"a", "b"})


@pytest.mark.parametrize("known", [True, False], ids=["verified-empty", "unknown-baseline"])
def test_reviewer_scope_distinguishes_verified_empty_from_unknown(project, known):
    if not known:
        _write(project, "apps/a/model.py", "value = 2\n")
    run_dir = project / ".agent-flow" / "runs" / "r1"
    run_dir.mkdir(parents=True)

    snapshot = _write_review_input_snapshot(
        project, run_dir, "final-review", base_branch="main" if known else "release/absent",
    )

    assert snapshot.document_scope == (() if known else None)
    _assert_reviewer_documents(project, run_dir, snapshot, set() if known else {"a", "b"})


def test_reviewers_use_captured_scope_not_author_history_or_later_git_changes(project):
    phase = Phase(id="implement", description="Apply existing behavior")
    runner = _pin_runner(project, phase)
    _write(project, "apps/a/model.py", "value = 2\n")
    snapshot = _write_review_input_snapshot(project, runner.run_dir, "final-review", base_branch="main")
    _write(project, "apps/b/model.py", "value = 3\n")
    runner._grown_skill_names(phase)
    adapter = HostedAdapter("codex")
    resolution = runner._required_skill_resolution(phase, read_meta(runner.run_dir))
    _assert_documents(
        adapter.render_envelope(phase, runner.run_dir, project, resolution=resolution), {"a", "b"},
    )

    _assert_reviewer_documents(project, runner.run_dir, snapshot, {"a"}, adapter=adapter)
    _assert_documents(
        adapter.render_envelope(phase, runner.run_dir, project, resolution=resolution), {"a", "b"},
    )
    grown = _write_review_input_snapshot(project, runner.run_dir, "final-review", base_branch="main")
    _assert_reviewer_documents(project, runner.run_dir, grown, {"a", "b"}, adapter=adapter)
    _write(project, "apps/b/model.py", "value = 1\n")
    shrunk = _write_review_input_snapshot(project, runner.run_dir, "final-review", base_branch="main")
    assert shrunk.document_scope == ("apps/a/model.py",)
    _assert_reviewer_documents(project, runner.run_dir, shrunk, {"a", "b"}, adapter=HostedAdapter("codex"))


def test_scope_queries_preserve_published_snapshot_after_same_document_code_edits(project):
    phase = Phase(id="final-review", description="Review existing behavior")
    runner = _pin_runner(project, phase)
    _write(project, "apps/a/model.py", "value = 2\n")
    runner._grown_skill_names(phase)
    snapshot = _write_review_input_snapshot(project, runner.run_dir, phase.id, base_branch="main")
    published = snapshot.path.read_bytes()
    artifact = runner.run_dir / "final-review.md"
    artifact.write_text("verdict: approve\n", encoding="utf-8")
    _write(project, "apps/a/model.py", "value = 3\n")

    assert runner._grown_skill_names(phase) == ()
    assert runner._phase_document_scope(phase) == ("apps/a/model.py",)
    assert runner._phase_document_scope(Phase(id="implement", description="Implement")) == ("apps/a/model.py",)
    assert snapshot.path.read_bytes() == published
    assert artifact.read_text(encoding="utf-8") == "verdict: approve\n"
    assert not (runner.run_dir / "implement-review-input.patch").exists()


def test_unknown_next_phase_receives_all_documents_after_narrow_code_phase(project, monkeypatch, capsys):
    monkeypatch.setattr(runner_module, "assert_managed_hooks_registered", lambda *args: None)
    _write(project, "apps/a/model.py", "value = 2\n")
    runner = _runner(project)
    payload = yaml.safe_load(runner.workflow.source_bytes)
    payload["phases"].append({"id": "comment-authoring", "description": "Review comments"})
    runner.workflow = parse_phase_workflow_definition(
        yaml.safe_dump(payload).encode(), source=runner.workflow.source,
        name="development", kit_owned=runner.workflow.kit_owned,
    )
    runner.phases = _phases_from_definition(runner.workflow)
    runner.run(ResumeMode.START, task="Apply existing behavior")
    _assert_documents(capsys.readouterr().out, {"a"})
    (runner.run_dir / "implement.md").write_text(_completion("architecture"), encoding="utf-8")

    runner.run(ResumeMode.RESUME)

    prompt = capsys.readouterr().out
    _assert_documents(prompt, {"a", "b"})


def test_porcelain_status_selects_untracked_document_paths_with_always_color(project):
    _git(project, "config", "color.status", "always")
    _write(project, "apps/a/new.py", "value = 2\n")
    run_dir = project / ".agent-flow" / "runs" / "r1"
    run_dir.mkdir(parents=True)

    snapshot = _write_review_input_snapshot(project, run_dir, "final-review", base_branch="main")

    assert snapshot.document_scope == ("apps/a/new.py",)
    assert review_document_scope(project, run_dir, base_branch="main") == snapshot.document_scope
    _assert_reviewer_documents(project, run_dir, snapshot, {"a"})


@pytest.mark.parametrize("status", ["\x1b[31m??\x1b[m apps/a/new.py\n", "?? \"apps/a/\\033bad.py\"\n", "?"])
def test_unparseable_status_cannot_prove_a_document_subset(status):
    assert _snapshot_document_scope(status, ()) is None


def test_reviewer_inspection_does_not_record_delivery_and_provider_history_is_separate(project):
    phase = Phase(id="final-review", description="Review the captured change")
    runner = _pin_runner(project, phase)
    adapter = HostedAdapter("codex")
    _write(project, "apps/b/model.py", "value = 2\n")
    earlier = _write_review_input_snapshot(project, runner.run_dir, phase.id, base_branch="main")
    before = (runner.run_dir / "meta.json").read_bytes()
    preview, _ = _reviewer_jobs(
        phase, runner.run_dir, project, adapter, review_input=earlier, providers=("codex",),
    )
    _assert_documents(preview[0].prompt_for("codex"), {"b"})
    assert (runner.run_dir / "meta.json").read_bytes() == before
    _prepare_reviewer_jobs(
        phase, runner.run_dir, project, adapter, review_input=earlier, providers=("claude",),
    )
    _write(project, "apps/b/model.py", "value = 1\n")
    _write(project, "apps/a/model.py", "value = 2\n")
    current = _write_review_input_snapshot(project, runner.run_dir, phase.id, base_branch="main")

    jobs = _prepare_reviewer_jobs(
        phase, runner.run_dir, project, adapter, review_input=current, providers=("claude", "codex"),
    )

    for job in jobs:
        _assert_documents(job.prompt_for("claude"), {"a", "b"})
        _assert_documents(job.prompt_for("codex"), {"a"})


def test_document_growth_reenters_before_approval_and_shrink_keeps_delivered_docs(project, monkeypatch, capsys):
    monkeypatch.setattr(runner_module, "assert_managed_hooks_registered", lambda *args: None)
    _write(project, "apps/a/model.py", "value = 2\n")
    runner = _runner(project)
    runner.run(ResumeMode.START, task="Apply existing behavior")
    _assert_documents(capsys.readouterr().out, {"a"})
    phase = runner.phases[0]
    before = read_meta(runner.run_dir)
    names = scope_names(before, phase.id)
    documents = scope_document_ids(before, phase.id)
    artifact = runner.run_dir / "implement.md"
    original = "# Implementation\n\nCompleted before application B changed.\nverdict: approve\n"
    artifact.write_text(original, encoding="utf-8")
    before["phase_approval"] = {"approved": True}
    before["phase_approval_request"] = {"phase": phase.id}
    write_meta(runner.run_dir, before)
    _write(project, "apps/b/model.py", "value = 3\n")

    runner.run(ResumeMode.RESUME)

    assert "reason: skill_scope_grew" in capsys.readouterr().out
    assert not artifact.exists()
    grown = read_meta(runner.run_dir)
    assert scope_names(grown, phase.id) == names
    assert set(documents) < set(scope_document_ids(grown, phase.id))
    assert "phase_approval" not in grown
    assert "phase_approval_request" not in grown
    assert any(path.read_text(encoding="utf-8") == original for path in runner.run_dir.glob("implement-norms-*.md"))
    _write(project, "apps/b/model.py", "value = 1\n")

    runner.run(ResumeMode.RESUME)

    _assert_documents(capsys.readouterr().out, {"a", "b"})
    assert not artifact.exists()
    assert scope_document_ids(read_meta(runner.run_dir), phase.id) == scope_document_ids(grown, phase.id)
    artifact.write_text(_completion("architecture"), encoding="utf-8")
    assert runner._stale_artifact_block_reason(artifact, read_meta(runner.run_dir)) is None
    assert runner._missing_required_markers(phase) == []


def _completion(skills: str, architecture_key: str = "architecture-contract", value: str = "applied") -> str:
    return (
        "# Implementation\n\n## Architecture Boundary Map\nExisting feature ownership is preserved.\n\n"
        "## Completion Gate\n"
        "skill-availability: pass\nskill-use-evidence: unavailable\ntest-run-evidence: unavailable\n"
        "project-local-skills: checked\n"
        f"project-local-skills-used: {skills}\n"
        "project-local-skill-docs: applied\nmissing-required-profile-skills: none\n"
        f"{architecture_key}: {value}\nmust-avoid-check: pass\n"
    )


@pytest.mark.parametrize("mode", ["local", "clean"])
def test_runner_conditional_markers_require_clean_sections_only_in_clean_mode(project, mode):
    required = ()
    skills = "architecture"
    if mode == "clean":
        _write(project, ".agent-flow.project.yaml", "schema_version: 1\narchitecture:\n  mode: clean\n")
        _write(project, "skills/clean-architecture-core/SKILL.md", "---\nname: clean-architecture-core\n---\nKeep domain behavior independent.\n")
        _git(project, "add", ".agent-flow.project.yaml", "skills")
        _git(project, "commit", "-m", "Select Clean contract")
        required = ("clean-architecture-core",)
        skills = "clean-architecture-core"
    phase = Phase(
        id="implement", description="Apply selected contract", skills=PhaseSkills(required=required),
        required_markers=("## Architecture Boundary Map", "architecture-contract: applied"),
        required_markers_by_architecture={"clean": ("## Use Case Boundaries", "cache-required: yes|no")},
    )
    runner = _pin_runner(project, phase)
    artifact = runner.run_dir / "implement.md"
    artifact.write_text(_completion(skills), encoding="utf-8")

    assert runner._missing_required_markers(phase) == (
        ["## Use Case Boundaries", "cache-required: yes|no"] if mode == "clean" else []
    )
    artifact.write_text(_completion(skills) + "cache-required: no\n\n## Use Case Boundaries\nExisting boundary retained.\n", encoding="utf-8")
    assert runner._missing_required_markers(phase) == []
    artifact.write_text(_completion(skills, value="n/a"), encoding="utf-8")
    assert "architecture-contract: applied" in runner._missing_required_markers(phase)


@pytest.mark.parametrize("required", [False, True], ids=["not-required", "required"])
def test_old_phase_without_conditional_field_keeps_legacy_na_guard(project, required):
    _write(project, ".agent-flow.project.yaml", "schema_version: 1\narchitecture:\n  mode: clean\n")
    _write(project, "skills/alpha/SKILL.md", "---\nname: alpha\n---\nPreserve current behavior.\n")
    _write(project, "skills/clean-architecture-core/SKILL.md", "---\nname: clean-architecture-core\n---\nKeep domain behavior independent.\n")
    _git(project, "add", ".agent-flow.project.yaml", "skills")
    _git(project, "commit", "-m", "Track legacy completion contract")
    skills = ("alpha", "clean-architecture-core") if required else ("alpha",)
    phase = Phase(
        id="implement", description="Legacy completion", skills=PhaseSkills(required=skills),
        required_markers=("clean-architecture: applied|n/a", "must-avoid-check: pass|fail|n/a"),
    )
    runner = _pin_runner(project, phase)
    artifact = runner.run_dir / "implement.md"
    artifact.write_text(
        _completion(", ".join(skills), architecture_key="clean-architecture", value="n/a").replace("must-avoid-check: pass", "must-avoid-check: n/a"),
        encoding="utf-8",
    )

    assert runner._missing_required_markers(phase) == (
        ["clean-architecture: applied", "must-avoid-check: pass|fail"] if required else []
    )
    artifact.write_text(_completion(", ".join(skills), architecture_key="clean-architecture"), encoding="utf-8")
    assert runner._missing_required_markers(phase) == []
