from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT_ROOT / "src"))

from agent_flow.adapters.hosted import HostedAdapter, _reviewer_jobs
from agent_flow.core.architecture_policy import ArchitectureContractError
from agent_flow.core.phase_workflow import load_phase_workflow_definition
from agent_flow.core.skill_resolver import PhaseSkills, resolve_phase_skills
from agent_flow.runner import Phase


def _selection(root: Path, mode: str) -> None:
    """Write an architecture selection fixture."""
    root.mkdir(parents=True, exist_ok=True)
    skill = "  skill: skills/architecture/SKILL.md\n" if mode == "local" else ""
    (root / ".agent-flow.project.yaml").write_text(
        f"schema_version: 1\narchitecture:\n  mode: {mode}\n{skill}", encoding="utf-8"
    )
    if mode == "local":
        _skill(root, "architecture", "", "Feature ownership may include framework dependencies.")


def _skill(root: Path, name: str, metadata: str, body: str = "Required behavior.") -> Path:
    """Write an installed skill fixture."""
    path = root / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\n{metadata}---\n\n{body}\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Provide an isolated home directory for skill resolution."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def test_local_selects_presentation_contract_before_dependency_expansion(tmp_path):
    """Verify that local selects presentation contract before dependency expansion."""
    _selection(tmp_path, "local")
    _skill(
        tmp_path,
        "react-clean-presentation-architecture",
        "workflowPhases: [implement]\npathGlobs: ['**/presentation/**']\nrequires: [clean-architecture-core]\n",
    )
    _skill(tmp_path, "clean-architecture-core", "")
    _skill(tmp_path, "audit-clean-architecture-notes", "")
    resolution = resolve_phase_skills(
        project_root=tmp_path,
        phase_id="implement",
        changed_files=("src/presentation/Screen.tsx",),
        phase_skills=PhaseSkills(
            required=("audit-clean-architecture-notes",),
            optional=("clean-architecture-core",),
        ),
        host="codex",
    )
    assert {skill.name for skill in resolution.required} == {
        "architecture", "audit-clean-architecture-notes"
    }
    assert not resolution.optional


@pytest.mark.parametrize("mode", ["local", "pending"])
def test_custom_required_dependency_is_not_silently_removed(tmp_path, mode):
    """Verify that custom required dependency is not silently removed."""
    _selection(tmp_path, mode)
    _skill(tmp_path, "custom-review", "requires: [clean-architecture-core]\n")
    _skill(tmp_path, "clean-architecture-core", "")
    with pytest.raises(ArchitectureContractError, match="incompatible.*required.*dependencies"):
        resolve_phase_skills(
            project_root=tmp_path,
            phase_id="implement",
            phase_skills=PhaseSkills(required=("custom-review",)),
            host="codex",
        )


@pytest.mark.parametrize("mode", ["local", "pending"])
@pytest.mark.parametrize("workflow_name", ["custom", "default"])
def test_custom_workflow_required_clean_blocks_incompatible_selection(tmp_path, mode, workflow_name):
    """Verify that custom workflow required clean blocks incompatible selection."""
    _selection(tmp_path, mode)
    _skill(tmp_path, "clean-architecture-core", "")
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / f"{workflow_name}.yaml").write_text(
        f"id: {workflow_name}\nphases:\n  - id: implement\n"
        "    skills:\n      required: [clean-architecture-core]\n",
        encoding="utf-8",
    )
    phase = load_phase_workflow_definition(tmp_path, workflow_name).phases[0]

    with pytest.raises(
        ArchitectureContractError, match="incompatible.*explicit required workflow.*clean-architecture-core"
    ):
        resolve_phase_skills(
            project_root=tmp_path,
            phase_id=phase.id,
            phase_skills=phase.skills,
            host="codex",
        )


@pytest.mark.parametrize("mode", ["clean", "local", "pending"])
@pytest.mark.parametrize("workflow_source", ["fallback", "package", "installed-copy"])
def test_bundled_workflow_resolves_selected_architecture(tmp_path, mode, workflow_source):
    """Verify that bundled workflow resolves selected architecture."""
    _selection(tmp_path, mode)
    _skill(tmp_path, "clean-architecture-core", "")
    package_root = KIT_ROOT / "src" / "agent_flow"
    if workflow_source == "installed-copy":
        kit_root = tmp_path / ".agent-flow"
        workflows = kit_root / "workflows"
        workflows.mkdir(parents=True)
        shutil.copyfile(package_root / "workflows" / "default.yaml", workflows / "default.yaml")
    else:
        kit_root = package_root if workflow_source == "package" else KIT_ROOT
    workflow = load_phase_workflow_definition(kit_root, "default")
    phase = next(phase for phase in workflow.phases if phase.id == "final-review")

    resolution = resolve_phase_skills(
        project_root=tmp_path,
        phase_id=phase.id,
        phase_skills=phase.skills,
        host="codex",
    )

    names = {skill.name for skill in resolution.required}
    assert {"code-generation-discipline", "code-review", "architecture-reviewer"} <= names
    expected = {
        "clean": {"clean-architecture-core"},
        "local": {"architecture"},
        "pending": set(),
    }
    assert names & {"clean-architecture", "clean-architecture-core", "architecture"} == expected[mode]


@pytest.mark.parametrize("declared_clean", [False, True])
def test_custom_workflow_required_clean_survives_legacy_and_explicit_clean(tmp_path, declared_clean):
    """Verify that custom workflow required clean survives legacy and explicit clean."""
    if declared_clean:
        _selection(tmp_path, "clean")
    _skill(tmp_path, "clean-architecture-core", "")
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "custom.yaml").write_text(
        "id: custom\nphases:\n  - id: implement\n"
        "    skills:\n      required: [clean-architecture-core]\n",
        encoding="utf-8",
    )
    phase = load_phase_workflow_definition(tmp_path, "custom").phases[0]

    resolution = resolve_phase_skills(
        project_root=tmp_path,
        phase_id=phase.id,
        phase_skills=phase.skills,
        host="codex",
    )

    assert {skill.name for skill in resolution.required} == {"clean-architecture-core"}
    assert not resolution.missing


def test_shared_tool_safety_keeps_mode_scoped_clean_dependency(tmp_path):
    """Verify that shared tool safety keeps mode scoped clean dependency."""
    _selection(tmp_path, "local")
    target = tmp_path / "skills" / "llm-tool-development"
    shutil.copytree(KIT_ROOT / "skills" / "llm-tool-development", target)
    _skill(tmp_path, "clean-architecture-core", "")
    arguments = dict(
        project_root=tmp_path,
        phase_id="implement",
        phase_skills=PhaseSkills(required=("llm-tool-development",)),
        host="codex",
    )
    local = resolve_phase_skills(**arguments)
    assert {skill.name for skill in local.required} == {"llm-tool-development", "architecture"}
    _selection(tmp_path, "clean")
    clean = resolve_phase_skills(**arguments)
    assert {skill.name for skill in clean.required} == {
        "llm-tool-development", "clean-architecture-core"
    }


def test_author_and_each_reviewer_receive_complete_bound_contract(tmp_path):
    """Verify that author and each reviewer receive complete bound contract."""
    leader = tmp_path / "leader"
    checkout = tmp_path / "checkout"
    _selection(leader, "local")
    _selection(checkout, "local")
    _skill(leader, "architecture", "", "LEADER_ONLY_NORM")
    normative = "Feature-colocated framework ownership is allowed.\n" * 80 + "BOUND_ROOT_END"
    root = _skill(
        checkout, "architecture", "requires_docs: [references/ownership.md]\n", normative
    )
    reference = root.parent / "references" / "ownership.md"
    reference.parent.mkdir()
    reference.write_text("Authorize writes at the feature boundary.\nBOUND_REFERENCE_END", encoding="utf-8")
    adapter = HostedAdapter("codex")
    adapter._config_root = leader
    phase = Phase(id="final-review", description="Review selected contract", skills=PhaseSkills())
    run_dir = checkout / ".agent-flow" / "runs" / "r1"
    author = adapter.render_envelope(phase, run_dir, checkout, skill_host="codex")
    jobs = _reviewer_jobs(phase, run_dir, checkout, adapter, providers=("codex",))
    assert {"clean-architecture", "architecture-design"} <= {job.angle_id for job in jobs}
    prompts = [author, *(job.prompt_by_provider["codex"] for job in jobs)]
    for prompt in prompts:
        assert normative in prompt
        assert reference.read_text(encoding="utf-8") in prompt
        assert "LEADER_ONLY_NORM" not in prompt


def test_clean_norm_manifest_changes_with_selected_reference_bytes(tmp_path):
    """Verify that clean norm manifest changes with selected reference bytes."""
    _selection(tmp_path, "clean")
    root = _skill(tmp_path, "clean-architecture-core", "")
    reference = root.parent / "references" / "rules.md"
    reference.parent.mkdir()
    reference.write_text("Original rule.", encoding="utf-8")
    arguments = dict(
        project_root=tmp_path,
        phase_id="implement",
        phase_skills=PhaseSkills(required=("clean-architecture-core",)),
        host="codex",
    )
    before = resolve_phase_skills(**arguments).architecture_norms
    reference.write_text("Changed required rule.", encoding="utf-8")
    after = resolve_phase_skills(**arguments).architecture_norms
    assert {document.path for document in after} == {str(root), str(reference)}
    assert before != after


def test_contract_delivery_and_dependencies_use_the_same_opened_bytes(tmp_path, monkeypatch):
    """Verify that contract delivery and dependencies use the same opened bytes."""
    from agent_flow.core import skill_resolver
    from agent_flow.core.local_skills import local_skill_prompt_block

    _selection(tmp_path, "local")
    root = _skill(
        tmp_path, "architecture",
        "requires: [immutable-rule]\nrequires_docs: [references/ownership.md]\n",
        "ORIGINAL_ROOT_NORM",
    )
    _skill(tmp_path, "immutable-rule", "")
    reference = root.parent / "references" / "ownership.md"
    reference.parent.mkdir()
    reference.write_text("ORIGINAL_REFERENCE_NORM", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("OUTSIDE_CONTENT", encoding="utf-8")
    snapshot = skill_resolver.architecture_snapshot

    def replace_after_snapshot(project_root):
        """Replace the contract path after its snapshot has been captured."""
        result = snapshot(project_root)
        root.unlink()
        root.symlink_to(outside)
        reference.unlink()
        reference.symlink_to(outside)
        return result

    monkeypatch.setattr(skill_resolver, "architecture_snapshot", replace_after_snapshot)
    prompt = local_skill_prompt_block(tmp_path, "implement", host="codex")

    assert "ORIGINAL_ROOT_NORM" in prompt
    assert "ORIGINAL_REFERENCE_NORM" in prompt
    assert "immutable-rule" in prompt
    assert "OUTSIDE_CONTENT" not in prompt


@pytest.mark.parametrize(
    ("prefix", "newline"), [("", "\n"), ("\ufeff", "\r\n")], ids=["lf", "bom-crlf"],
)
def test_whitespace_delimited_contract_keeps_required_dependencies(tmp_path, prefix, newline):
    """Verify that whitespace delimited contract keeps required dependencies."""
    _selection(tmp_path, "local")
    root = tmp_path / "skills/architecture/SKILL.md"
    root.write_bytes(
        (
            prefix + "--- \nname: architecture\nrequires: [clean-architecture-core]\n"
            "--- \t\n\nFeature-local ownership.\n"
        ).replace("\n", newline).encode("utf-8")
    )
    _skill(tmp_path, "clean-architecture-core", "")

    with pytest.raises(ArchitectureContractError, match="incompatible.*required.*dependencies"):
        resolve_phase_skills(
            project_root=tmp_path, phase_id="implement", host="codex",
        )


def test_duplicate_selected_dependency_key_cannot_erase_required_rules(tmp_path):
    """Verify that duplicate selected dependency key cannot erase required rules."""
    _selection(tmp_path, "local")
    _skill(
        tmp_path, "custom-review",
        "requires_by_architecture:\n  local: [required-rule]\n  local: []\n",
    )
    _skill(tmp_path, "required-rule", "")

    with pytest.raises(ValueError, match="duplicate key.*local"):
        resolve_phase_skills(
            project_root=tmp_path, phase_id="implement",
            phase_skills=PhaseSkills(required=("custom-review",)), host="codex",
        )


@pytest.mark.parametrize("key", ["requires", "dependencies"])
@pytest.mark.parametrize("value", ["{required-rule: true}", "[false]", "null", "required-rule", "['../rule']"])
def test_invalid_common_dependencies_block_resolution(tmp_path, key, value):
    """Verify that invalid common dependencies block resolution."""
    _selection(tmp_path, "pending")
    _skill(tmp_path, "custom-review", f"{key}: {value}\n")
    with pytest.raises(ValueError, match=key):
        resolve_phase_skills(
            project_root=tmp_path, phase_id="implement",
            phase_skills=PhaseSkills(required=("custom-review",)), host="codex",
        )


@pytest.mark.parametrize("required_via", ["named", "dependency", "architecture-dependency", "placement"])
def test_malformed_required_metadata_blocks_resolution(tmp_path, required_via):
    """Verify that malformed required metadata blocks resolution."""
    _selection(tmp_path, "clean")
    placement = ".agent-flow/local-skills" if required_via == "placement" else "skills"
    malformed = tmp_path / placement / "custom-rule/SKILL.md"
    malformed.parent.mkdir(parents=True)
    malformed.write_text(
        "---\nname: custom-rule\nrequires_by_architecture:\n"
        "  clean: [missing-obligation\n---\nRequired behavior.\n",
        encoding="utf-8",
    )
    required = ("custom-rule",) if required_via == "named" else ()
    if required_via in {"dependency", "architecture-dependency"}:
        metadata = (
            "requires: [custom-rule]\n"
            if required_via == "dependency"
            else "requires_by_architecture:\n  clean: [custom-rule]\n"
        )
        _skill(tmp_path, "custom-review", metadata)
        required = ("custom-review",)

    with pytest.raises(ValueError, match="frontmatter|metadata") as failure:
        resolve_phase_skills(
            project_root=tmp_path, phase_id="implement",
            phase_skills=PhaseSkills(required=required), host="codex",
        )
    assert "custom-rule" in str(failure.value)
    assert str(malformed) in str(failure.value)


def test_unrelated_malformed_metadata_does_not_activate_or_block_resolution(tmp_path):
    """Verify that unrelated malformed metadata does not activate or block resolution."""
    _selection(tmp_path, "pending")
    _skill(tmp_path, "custom-review", "requires: [required-rule]\n")
    _skill(tmp_path, "required-rule", "")
    malformed = tmp_path / "skills/optional-rule/SKILL.md"
    malformed.parent.mkdir(parents=True)
    malformed.write_text(
        "---\nworkflowPhases: [implement]\nrequires_by_architecture:\n"
        "  clean: [missing-obligation\n---\nOptional guidance.\n",
        encoding="utf-8",
    )

    resolution = resolve_phase_skills(
        project_root=tmp_path, phase_id="implement",
        phase_skills=PhaseSkills(required=("custom-review",)), host="codex",
    )
    assert {skill.name for skill in resolution.required} == {"custom-review", "required-rule"}
    assert not resolution.optional
    assert not resolution.missing


def test_required_skill_without_frontmatter_remains_satisfied(tmp_path):
    """Verify that required skill without frontmatter remains satisfied."""
    _selection(tmp_path, "pending")
    _skill(tmp_path, "custom-review", "requires: [plain-rule]\n")
    plain = _skill(tmp_path, "plain-rule", "")
    plain.write_text("# Plain rule\n\nRequired behavior without metadata.\n", encoding="utf-8")

    resolution = resolve_phase_skills(
        project_root=tmp_path, phase_id="implement",
        phase_skills=PhaseSkills(required=("custom-review",)), host="codex",
    )
    assert {skill.name for skill in resolution.required} == {"custom-review", "plain-rule"}
    assert not resolution.missing


@pytest.mark.parametrize("mode", ["clean", "local", "pending"])
def test_common_and_selected_dependencies_remain_required(tmp_path, mode):
    """Verify that common and selected dependencies remain required."""
    _selection(tmp_path, mode)
    _skill(
        tmp_path, "custom-review",
        "requires: [common-rule]\ndependencies: [legacy-rule]\n"
        "requires_by_architecture:\n  clean: [clean-rule]\n"
        "  local: [local-rule]\n  pending: [pending-rule]\n",
    )
    for name in ("common-rule", "legacy-rule", "clean-rule", "local-rule", "pending-rule"):
        _skill(tmp_path, name, "")
    resolution = resolve_phase_skills(
        project_root=tmp_path, phase_id="implement",
        phase_skills=PhaseSkills(required=("custom-review",)), host="codex",
    )
    expected = {"custom-review", "common-rule", "legacy-rule", f"{mode}-rule"}
    if mode == "local":
        expected.add("architecture")
    assert {skill.name for skill in resolution.required} == expected


@pytest.mark.parametrize("relative", ["SKILL.md", "references/rules.md"])
@pytest.mark.parametrize("kind", ["oversized", "fifo", "symlink"])
def test_unsafe_clean_norms_cannot_produce_prompts(tmp_path, relative, kind):
    """Verify that unsafe clean norms cannot produce prompts."""
    from agent_flow.core.architecture_policy import MAX_ARCHITECTURE_DOCUMENT_BYTES
    from agent_flow.core.local_skills import local_skill_prompt_block

    _selection(tmp_path, "clean")
    root = _skill(tmp_path, "clean-architecture-core", "workflowPhases: [implement]\n")
    path = root.parent / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    if kind == "oversized":
        with path.open("wb") as stream:
            stream.truncate(MAX_ARCHITECTURE_DOCUMENT_BYTES + 1)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        target = tmp_path / "outside.md"
        target.write_text("Outside norm.", encoding="utf-8")
        path.symlink_to(target)
    with pytest.raises(ArchitectureContractError):
        local_skill_prompt_block(
            tmp_path, "implement",
            phase_skills=PhaseSkills(required=("clean-architecture-core",)), host="codex",
        )


@pytest.mark.parametrize("stage", ["before", "snapshot", "norms"])
def test_interrupted_install_blocks_resolution_at_read_boundaries(tmp_path, monkeypatch, stage):
    """Verify that interrupted install blocks resolution at read boundaries."""
    from agent_flow.core import skill_resolver

    _selection(tmp_path, "local")

    def interrupt_install():
        """Simulate installation beginning during contract resolution."""
        marker = tmp_path / ".agent-flow/install-recovery/manifest.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("[]", encoding="utf-8")

    if stage == "before":
        interrupt_install()
    else:
        name = "architecture_snapshot" if stage == "snapshot" else "_architecture_norms"
        original = getattr(skill_resolver, name)

        def interrupt_after_read(*args, **kwargs):
            """Simulate installation beginning after contract bytes are read."""
            result = original(*args, **kwargs)
            interrupt_install()
            return result

        monkeypatch.setattr(skill_resolver, name, interrupt_after_read)

    with pytest.raises(ValueError, match="install recovery"):
        resolve_phase_skills(project_root=tmp_path, phase_id="implement", host="codex")
