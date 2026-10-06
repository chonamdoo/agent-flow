from pathlib import Path
import shutil

import pytest

from agent_flow.adapters.hosted import HostedAdapter
from agent_flow.artifact import _missing_completion_markers, create_run
from agent_flow.core.local_skills import missing_local_skill_markers, phase_skill_resolution
from agent_flow.core.phase_workflow import parse_phase_workflow_definition
from agent_flow.core.skill_resolver import ResolutionContext
from agent_flow.runner import Phase, Runner


ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "evals/phase-cases/web"


@pytest.fixture
def planned_project(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    shutil.copytree(CASE / "modes/team/overlay/skills", project / "skills")
    run_dir = project / ".agent-flow/runs/probe"
    run_dir.mkdir(parents=True)
    shutil.copyfile(CASE / "author/team/artifacts/slice-plan.md", run_dir / "slice-plan.md")
    return project, run_dir


def required(project, run_dir, phase="green", **kwargs):
    return phase_skill_resolution(
        project, phase, run_dir=run_dir,
        changed_files=("tests/orders.test.ts",), **kwargs,
    )


@pytest.mark.parametrize("phase", ["implement", "red", "green", "refactor", "fix-loop"])
def test_team_skill_is_required_before_planned_source_files_exist(planned_project, phase):
    project, run_dir = planned_project
    assert not (project / "src").exists()
    assert "web-endpoint-registry" in {s.name for s in required(project, run_dir, phase).required}


@pytest.mark.parametrize("phase", ["review", "multi-review", "architecture-review", "commit", "design"])
def test_other_phases_use_actual_changes_only(planned_project, phase):
    project, run_dir = planned_project
    assert "web-endpoint-registry" not in {s.name for s in required(project, run_dir, phase).required}


@pytest.mark.parametrize("value", [
    "src/**/*.ts", "src/../outside.ts", "/src/file.ts", "https://host/src/file.ts",
    "C:\\src\\file.ts", "src/folder/", "src/file.ts (optional)",
])
def test_nonconcrete_or_nonrelative_plan_entries_do_not_select_skills(planned_project, value):
    project, run_dir = planned_project
    (run_dir / "slice-plan.md").write_text(f"- Files: {value}\n", encoding="utf-8")
    assert "web-endpoint-registry" not in {s.name for s in required(project, run_dir).required}


def test_plan_edits_refresh_cached_resolution_and_missing_plan_preserves_scope(planned_project):
    project, run_dir = planned_project
    context = ResolutionContext()
    assert "web-endpoint-registry" in {s.name for s in required(project, run_dir, context=context).required}
    (run_dir / "slice-plan.md").unlink()
    assert "web-endpoint-registry" not in {s.name for s in required(project, run_dir, context=context).required}


def test_adapter_prompt_includes_planned_team_skill(planned_project):
    project, run_dir = planned_project
    adapter = HostedAdapter("codex")
    adapter._changed_files = ("tests/orders.test.ts",)
    prompt = adapter.render_envelope(Phase(id="green", description="Implement"), run_dir, project)
    assert "web-endpoint-registry" in prompt


def test_backticks_and_wrapped_file_lists_are_supported(planned_project):
    project, run_dir = planned_project
    (run_dir / "slice-plan.md").write_text(
        "- Files: `tests/orders.test.ts`,\n  `src/shared/api/endpoints.ts`\n", encoding="utf-8",
    )
    assert "web-endpoint-registry" in {s.name for s in required(project, run_dir).required}


def test_runner_and_marker_gate_require_the_same_planned_skill(planned_project, monkeypatch):
    project, run_dir = planned_project
    monkeypatch.setattr("agent_flow.runner.changed_files", lambda *args: ("tests/orders.test.ts",))
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.config_root = runner.project_root = project
    runner.profile = None
    phase = Phase(id="green", description="Implement")
    resolution = runner._required_skill_resolution(phase, {})
    assert "web-endpoint-registry" in {s.name for s in resolution.required}
    missing = missing_local_skill_markers(
        "## Completion Gate\nproject-local-skills-used: n/a\n",
        project, phase.id, run_dir=run_dir, changed_files=("tests/orders.test.ts",),
    )
    assert any("web-endpoint-registry" in marker for marker in missing)


def test_plan_does_not_select_a_skill_for_unrelated_paths(planned_project):
    project, run_dir = planned_project
    (run_dir / "slice-plan.md").write_text("- Files: scripts/task.py\n", encoding="utf-8")
    assert "web-endpoint-registry" not in {s.name for s in required(project, run_dir).required}


@pytest.mark.parametrize("path", ["Dockerfile", "Makefile", "scripts/deploy", ".env"])
def test_extensionless_planned_files_select_their_skills(planned_project, path):
    project, run_dir = planned_project
    skill = project / "skills/planned-file-rule/SKILL.md"
    skill.parent.mkdir()
    skill.write_text(
        f'---\nname: planned-file-rule\npathGlobs: ["{path}"]\n---\nPlanned file rule.\n',
        encoding="utf-8",
    )
    (run_dir / "slice-plan.md").write_text(f"- Files: {path}\n", encoding="utf-8")
    assert "planned-file-rule" in {s.name for s in required(project, run_dir).required}


def test_missing_planned_profile_skill_is_reported_in_prompt_and_gate(planned_project):
    project, run_dir = planned_project
    profile = {"id": "probe", "skills": {"required_review": [{
        "group": "baseline", "skills": ["missing-planned-rule"],
        "path_globs": ["src/**/*.ts"],
    }]}}
    adapter = HostedAdapter("codex")
    adapter._profile_snapshot = profile
    adapter._changed_files = ("tests/orders.test.ts",)
    phase = Phase(id="green", description="Implement")
    resolution = adapter.phase_resolution(phase, project, run_dir=run_dir)
    assert "missing-planned-rule" in {s.name for s in resolution.missing}
    prompt = adapter.render_envelope(phase, run_dir, project, resolution=resolution)
    assert "missing-required-profile-skills: missing-planned-rule" in prompt
    artifact = (
        "## Completion Gate\nskill-availability: degraded\nskill-use-evidence: unavailable\n"
        "project-local-skills: checked\nproject-local-skills-used: web-endpoint-registry\n"
        "project-local-skill-docs: applied\nmissing-required-profile-skills: none\n"
    )
    missing = missing_local_skill_markers(
        artifact, project, phase.id, run_dir=run_dir, profile=profile,
        changed_files=adapter._changed_files,
    )
    assert "missing-required-profile-skills: missing-planned-rule" in missing
    corrected = artifact.replace("missing-required-profile-skills: none", "missing-required-profile-skills: missing-planned-rule")
    assert not missing_local_skill_markers(
        corrected, project, phase.id, run_dir=run_dir, profile=profile,
        changed_files=adapter._changed_files,
    )


@pytest.mark.parametrize("workflow,relative", [
    ("full-feature", "artifacts/slice-plan.md"),
    ("default", "slice-plan.md"),
])
def test_real_workflow_artifact_paths_select_planned_skills(planned_project, workflow, relative):
    project, _ = planned_project
    run_dir = create_run(project, workflow, "Implement orders", run_id="real")
    plan = run_dir / relative
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("- Files: src/shared/api/endpoints.ts\n", encoding="utf-8")
    assert "web-endpoint-registry" in {s.name for s in required(project, run_dir).required}


def custom_run(project):
    definition = parse_phase_workflow_definition(
        b"id: probe\nphases:\n  - id: slice-plan\n    artifact: plans/files.md\n"
        b"  - id: green\n    artifact: output/green.md\n",
        source=project / "probe.yaml", name="probe",
    )
    return create_run(project, "probe", "Implement orders", run_id="custom", workflow_definition=definition)


def test_custom_pinned_plan_path_takes_precedence_over_legacy_file(planned_project):
    project, _ = planned_project
    run_dir = custom_run(project)
    (run_dir / "plans").mkdir()
    (run_dir / "plans/files.md").write_text("- Files: src/shared/api/endpoints.ts\n", encoding="utf-8")
    (run_dir / "slice-plan.md").write_text("- Files: scripts/unrelated.py\n", encoding="utf-8")
    assert "web-endpoint-registry" in {s.name for s in required(project, run_dir).required}


@pytest.mark.parametrize("plan", [
    "- Files:\n  - `src/shared/api/endpoints.ts`\n- Verification: tests\n",
    "- Expected files: src/shared/api/endpoints.ts\n",
])
def test_existing_plan_file_lists_are_supported(planned_project, plan):
    project, run_dir = planned_project
    (run_dir / "slice-plan.md").write_text(plan, encoding="utf-8")
    assert "web-endpoint-registry" in {s.name for s in required(project, run_dir).required}


def test_status_marker_check_includes_the_active_run_plan(planned_project, monkeypatch):
    project, _ = planned_project
    run_dir = custom_run(project)
    (run_dir / "plans").mkdir()
    (run_dir / "plans/files.md").write_text("- Files: src/shared/api/endpoints.ts\n", encoding="utf-8")
    artifact = run_dir / "output/green.md"
    artifact.parent.mkdir()
    artifact.write_text("## Completion Gate\nproject-local-skills-used: n/a\n", encoding="utf-8")
    monkeypatch.setattr("agent_flow.artifact.changed_files", lambda *args: ("tests/orders.test.ts",))
    missing = _missing_completion_markers(run_dir, "probe", "green", config_root=project, project_root=project)
    assert any("web-endpoint-registry" in marker for marker in missing)
