from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from agent_flow.adapters.hosted import HostedAdapter, _reviewer_jobs
from agent_flow.artifact import create_run
from agent_flow.cli import main
from agent_flow.core.architecture_policy import ArchitectureMode, architecture_snapshot
from agent_flow.core.local_skills import architecture_contract_required, changed_files
from agent_flow.core.phase_workflow import load_phase_workflow_definition
from agent_flow.core.skill_resolver import (
    SkillResolution,
    active_host_roots,
    discover_skill_catalog,
    expand_dependencies,
    resolve_phase_skills,
    skill_roots,
)
from agent_flow.runner import Phase, Runner, _phases_from_definition
from tests.test_architecture_selection import (
    AGENCY_ROOT,
    AGENCY_SHARED,
    _agency_project,
    _git_project,
    _local_contract,
    _track,
    _write,
)
from tests.test_custom_skill_install import _install_with


@pytest.fixture(autouse=True)
def isolated_install_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in (
        "AGENT_FLOW_PROFILE", "AGENT_FLOW_HOST", "AGENT_FLOW_GENERIC_MODE",
        "CLAUDECODE", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "CODEX_SANDBOX",
        "PI_CODING_AGENT_DIR", "OMP_SESSION_ID", "NODE_OPTIONS",
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    assert shutil.which("node") is not None, "Node is required for real installer scenarios"


def _installed_runner(
    project: Path, binary: str, profile: str, mode: str,
    host: str, monkeypatch: pytest.MonkeyPatch,
) -> Runner:
    monkeypatch.setenv("AGENT_FLOW_HOST", host)
    flags = ["--profile", profile, "--architecture-mode", mode]
    if mode == "local":
        flags.extend(("--architecture-skill", "skills/architecture/SKILL.md"))
    result = _install_with(binary, project, *flags, env=dict(os.environ))
    assert result.returncode == 0, result.stdout + result.stderr
    _track(project, ".agent-flow.project.yaml")
    run_dir = create_run(project, "development", "Update catalog behavior", run_id="installed-contract")
    runner = Runner(project, workflow="development", run_dir=run_dir)
    assert runner.profile_id == profile
    assert (project / ".agent-flow/profiles" / f"{profile}.yaml").is_file()
    return runner


def _phase(runner: Runner, phase_id: str) -> Phase:
    return next(phase for phase in runner.phases if phase.id == phase_id)


def _resolve(runner: Runner, phase: Phase, host: str) -> SkillResolution:
    return resolve_phase_skills(
        project_root=runner.project_root,
        phase_id=phase.id,
        phase_skills=phase.skills,
        profile=runner.profile,
        changed_files=changed_files(runner.project_root),
        task_text="Update catalog behavior",
        host=host,
    )


def _adapter(runner: Runner, host: str) -> HostedAdapter:
    adapter = HostedAdapter(host)
    adapter._profile_id = runner.profile_id
    adapter._profile_snapshot = runner.profile
    adapter._config_root = runner.config_root
    adapter._changed_files = changed_files(runner.project_root)
    adapter._task_text = "Update catalog behavior"
    return adapter


def _gate_text(resolution: SkillResolution, *, omit: str | None = None) -> str:
    used = ", ".join(skill.name for skill in resolution.available_required if skill.name != omit)
    return (
        "## Completion Gate\n"
        f"skill-availability: {'degraded' if resolution.missing else 'pass'}\n"
        "skill-use-evidence: verified\n"
        "test-run-evidence: unavailable\n"
        "project-local-skills: checked\n"
        f"project-local-skills-used: {used}\n"
        "project-local-skill-docs: applied\n"
        f"missing-required-profile-skills: {', '.join(skill.name for skill in resolution.missing) or 'none'}\n"
        "must-avoid-check: pass\n"
    )


def _assert_application_gate(
    runner: Runner, phase: Phase, resolution: SkillResolution, obligations: set[str],
) -> None:
    assert runner.run_dir is not None
    artifact = runner.run_dir / (phase.artifact or f"{phase.id}.md")
    complete = _gate_text(resolution)
    artifact.write_text(complete, encoding="utf-8")
    assert runner._missing_required_markers(phase) == []
    for obligation in sorted(obligations):
        assert obligation in {skill.name for skill in resolution.available_required}
        artifact.write_text(_gate_text(resolution, omit=obligation), encoding="utf-8")
        missing = runner._missing_required_markers(phase)
        assert len(missing) == 1, missing
        assert missing[0].startswith("project-local-skills-used: "), missing
        assert obligation in missing[0].split(": ", 1)[1].split(", ")
    artifact.write_text(complete.replace("project-local-skill-docs: applied\n", ""), encoding="utf-8")
    assert runner._missing_required_markers(phase) == ["project-local-skill-docs: applied"]
    if architecture_contract_required(resolution):
        artifact.write_text(complete.replace("must-avoid-check: pass", "must-avoid-check: n/a"), encoding="utf-8")
        assert "must-avoid-check: pass|fail" in runner._missing_required_markers(phase)
    artifact.write_text(complete, encoding="utf-8")
    assert runner._missing_required_markers(phase) == []


@pytest.mark.parametrize(
    ("binary", "profile", "host", "source", "platform_skill"),
    [
        ("agent-flow-kit.mjs", "python", "codex", "src/domain/catalog/model.py", "python-api-clean-architecture"),
        ("agent-flow-install.mjs", "nextjs", "claude", "src/core/domain/catalog/model.ts", "react-clean-architecture"),
    ],
    ids=["kit-python-codex", "install-nextjs-claude"],
)
def test_clean_install_delivers_and_enforces_platform_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    binary: str, profile: str, host: str, source: str, platform_skill: str,
) -> None:
    project = _git_project(tmp_path)
    _write(project, source, "class Catalog: pass\n" if profile == "python" else "export type Catalog = { name: string };\n")
    runner = _installed_runner(project, binary, profile, "clean", host, monkeypatch)
    assert main(["architecture", "export", "--root", str(project)]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "clean"
    obligations = {"clean-architecture-core", platform_skill, "code-generation-discipline", "write-for-work"}
    adapter = _adapter(runner, host)
    for phase_id in ("implement", "review"):
        phase = _phase(runner, phase_id)
        resolution = _resolve(runner, phase, host)
        required = {skill.name: skill for skill in resolution.required}
        assert obligations <= required.keys()
        assert not resolution.missing
        roots = active_host_roots(skill_roots(project, profile=runner.profile, host=host), host)
        catalog = discover_skill_catalog(project, roots)
        closure = set(expand_dependencies([platform_skill, "code-generation-discipline"], catalog, architecture_mode=ArchitectureMode.CLEAN))
        assert obligations <= closure <= required.keys()
        for name in closure:
            skill = required[name]
            assert skill.path is not None
            installed = project / ".agent-flow" / "skills" / name / "SKILL.md"
            assert skill.path.resolve(strict=True) == installed.resolve(strict=True)
        norm_paths = {Path(document.path).resolve() for document in resolution.architecture_norms}
        assert required[platform_skill].path.resolve() in norm_paths
        assert required["clean-architecture-core"].path.resolve() in norm_paths
        prompt = adapter.render_envelope(phase, runner.run_dir, project, skill_host=host)
        for name in obligations:
            assert str(required[name].path) in prompt
        _assert_application_gate(runner, phase, resolution, obligations)
    jobs = _reviewer_jobs(_phase(runner, "review"), runner.run_dir, project, adapter, providers=(host,))
    assert {"clean-architecture", "architecture-design"} <= {job.angle_id for job in jobs}
    review_resolution = _resolve(runner, _phase(runner, "review"), host)
    for job in jobs:
        for skill in review_resolution.required:
            assert str(skill.path) in job.prompt_by_provider[host]


def test_local_install_delivers_anonymous_route_contract_to_author_and_reviewers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _agency_project(tmp_path, separate_hooks=True)
    _write(project, "package.json", json.dumps({"dependencies": {"next": "16.0.0", "react": "19.0.0", "react-hook-form": "7.0.0", "@tanstack/react-query": "5.0.0"}}))
    references = ("references/ownership.md", "references/transport.md")
    _local_contract(project, references=references)
    runner = _installed_runner(project, "agent-flow-kit.mjs", "nextjs", "local", "omp", monkeypatch)
    snapshot = architecture_snapshot(project)
    assert snapshot.contract is not None
    assert snapshot.contract.untracked == ()
    assert {document.path for document in snapshot.contract.documents} == {
        "skills/architecture/SKILL.md", *(f"skills/architecture/{name}" for name in references),
    }
    assert {
        f"{AGENCY_ROOT}/page.tsx", f"{AGENCY_ROOT}/_ui/CatalogForm.tsx",
        f"{AGENCY_ROOT}/hooks/useCatalogForm.ts", f"{AGENCY_ROOT}/_lib/toPayload.ts",
        f"{AGENCY_ROOT}/_lib/__tests__/toPayload.test.ts", AGENCY_SHARED,
    } <= set(changed_files(project))
    adapter = _adapter(runner, "omp")
    obligations = {"architecture", "code-generation-discipline", "write-for-work"}
    for phase_id in ("implement", "review"):
        phase = _phase(runner, phase_id)
        resolution = _resolve(runner, phase, "omp")
        assert obligations <= {skill.name for skill in resolution.available_required}
        assert resolution.architecture_contract == ("architecture",)
        assert not any("clean-architecture" in skill.name or "clean-presentation-architecture" in skill.name for skill in resolution.required)
        assert architecture_contract_required(resolution)
        prompt = adapter.render_envelope(phase, runner.run_dir, project, skill_host="omp")
        for content in snapshot.contract.contents:
            assert content in prompt
        _assert_application_gate(runner, phase, resolution, obligations)
    jobs = _reviewer_jobs(_phase(runner, "review"), runner.run_dir, project, adapter, providers=("claude", "codex"))
    assert {"clean-architecture", "architecture-design"} <= {job.angle_id for job in jobs}
    for provider in ("claude", "codex"):
        resolution = _resolve(runner, _phase(runner, "review"), provider)
        assert resolution.architecture_snapshot.digest == snapshot.digest
        for job in jobs:
            prompt = job.prompt_by_provider[provider]
            for content in snapshot.contract.contents:
                assert content in prompt
            assert "Do not introduce repository ports or use-case layers merely to satisfy Clean." in prompt
            for name in obligations:
                skill = next(skill for skill in resolution.required if skill.name == name)
                assert str(skill.path) in prompt


@pytest.mark.parametrize(
    ("binary", "profile", "host"),
    [("agent-flow-kit.mjs", "spring", "claude"), ("agent-flow-install.mjs", "generic", "codex")],
    ids=["kit-spring-role-scope", "install-generic-explicit-scope"],
)
def test_pending_install_keeps_discipline_but_refuses_structural_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, binary: str, profile: str, host: str,
) -> None:
    project = _git_project(tmp_path)
    local_path = "src/main/kotlin/com/example/Title.kt" if profile == "spring" else "app/title.txt"
    _write(project, local_path, 'const val TITLE = "Catalog"\n' if profile == "spring" else "Catalog\n")
    runner = _installed_runner(project, binary, profile, "pending", host, monkeypatch)
    implement = _phase(runner, "implement")
    resolution = _resolve(runner, implement, host)
    assert not resolution.architecture_contract
    assert not resolution.architecture_norms
    assert not any("clean-architecture" in skill.name for skill in resolution.required)
    assert runner._architecture_decision_block_reason(implement) is None
    _assert_application_gate(runner, implement, resolution, {"code-generation-discipline", "write-for-work"})
    adapter = _adapter(runner, host)
    author = adapter.render_envelope(implement, runner.run_dir, project, skill_host=host)
    assert resolution.architecture_snapshot is not None
    assert resolution.architecture_snapshot.digest in author
    for skill in resolution.available_required:
        assert str(skill.path) in author
    jobs = _reviewer_jobs(_phase(runner, "review"), runner.run_dir, project, adapter, providers=(host,))
    assert not {"clean-architecture", "architecture-design"} & {job.angle_id for job in jobs}
    assert {"generalist", "types"} <= {job.angle_id for job in jobs}
    review = _phase(runner, "review")
    review_resolution = _resolve(runner, review, host)
    for job in jobs:
        assert resolution.architecture_snapshot.digest in job.prompt_by_provider[host]
        for skill in review_resolution.available_required:
            assert str(skill.path) in job.prompt_by_provider[host]
    _assert_application_gate(runner, review, review_resolution, {"code-generation-discipline", "write-for-work"})
    if profile == "spring":
        assert all(group["group"] != "architecture" for group in runner.profile["skills"]["required_review"])
        _write(project, "src/main/kotlin/com/example/domain/Catalog.kt", "data class Catalog(val name: String)\n")
        assert runner._architecture_decision_block_reason(implement) == "architecture_decision_pending"
        assert runner._architecture_decision_block_reason(_phase(runner, "review")) == "architecture_decision_pending"
    else:
        _write(project, "src/domain/catalog.txt", "Catalog owns its name.\n")
        assert runner._architecture_decision_block_reason(implement) is None
        definition = load_phase_workflow_definition(project / ".agent-flow", "full-feature")
        structural = next(phase for phase in _phases_from_definition(definition) if phase.id == "ddd-design")
        assert structural.architecture_decision == "required"
        assert runner._architecture_decision_block_reason(structural) == "architecture_decision_pending"


def test_missing_local_reference_refuses_install_then_restored_contract_is_consumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _agency_project(tmp_path, separate_hooks=True)
    _local_contract(project, references=("references/ownership.md",))
    declaration = project / ".agent-flow.project.yaml"
    contract = project / "skills/architecture/SKILL.md"
    reference = project / "skills/architecture/references/ownership.md"
    before = {path: path.read_bytes() for path in (declaration, contract, reference)}
    reference.unlink()
    result = _install_with(
        "agent-flow-install.mjs", project, "--profile", "nextjs",
        "--architecture-mode", "local", "--architecture-skill", "skills/architecture/SKILL.md",
        env=dict(os.environ),
    )
    assert result.returncode != 0
    assert "ownership.md" in result.stdout + result.stderr
    assert not (project / ".agent-flow/kit.json").exists()
    assert declaration.read_bytes() == before[declaration]
    assert contract.read_bytes() == before[contract]
    assert not reference.exists()
    reference.write_bytes(before[reference])
    runner = _installed_runner(project, "agent-flow-install.mjs", "nextjs", "local", "codex", monkeypatch)
    phase = _phase(runner, "implement")
    resolution = _resolve(runner, phase, "codex")
    assert {document.path for document, _ in resolution.architecture_documents} == {
        "skills/architecture/SKILL.md", "skills/architecture/references/ownership.md",
    }
    prompt = _adapter(runner, "codex").render_envelope(
        phase, runner.run_dir, project, skill_host="codex",
    )
    assert before[reference].decode("utf-8") in prompt
    _assert_application_gate(runner, phase, resolution, {"architecture", "code-generation-discipline"})
