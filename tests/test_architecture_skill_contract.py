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
    (root / ".agent-flow").mkdir(exist_ok=True)
    (root / ".agent-flow/project.yaml").write_text(
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
    # Clean으로 돌아가려면 프로젝트 계약이 없어야 한다. 옆에 두면 거부된다.
    shutil.rmtree(tmp_path / "skills" / "architecture")
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
    jobs, _ = _reviewer_jobs(phase, run_dir, checkout, adapter, providers=("codex",))
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


def _scoped_contract(root):
    _selection(root, "local")
    contract = _skill(
        root, "architecture",
        "requires_docs:\n"
        "  - references/common.md\n"
        "  - path: references/unconditional.md\n"
        "  - path: references/a.md\n"
        "    pathGlobs: ['apps/a/**', 'shared/**', '**/*.shared']\n"
        "  - path: references/b.md\n"
        "    pathGlobs: ['apps/b/**', 'shared/**']\n",
        "SCOPED_ROOT_BODY",
    )
    (contract.parent / "references").mkdir()
    for name in ("common", "unconditional", "a", "b"):
        (contract.parent / "references" / f"{name}.md").write_text(
            f"NORM_BODY_{name}", encoding="utf-8",
        )
    return contract


@pytest.mark.parametrize(("scope", "selected"), [
    (None, {"a", "b"}),
    ((), set()),
    (("apps/a/model.py",), {"a"}),
    (("apps/a/old.py", "apps/b/new.py"), {"a", "b"}),
    (("shared/model.py",), {"a", "b"}),
    (("ROOT.SHARED",), {"a"}),
    ((".agent-flow/project.yaml",), {"a", "b"}),
    (("skills/architecture/SKILL.md",), {"a", "b"}),
    (("skills/architecture/references/a.md",), {"a", "b"}),
])
@pytest.mark.parametrize("role", ["author", "reviewer"])
def test_scoped_contract_delivers_only_required_bodies(tmp_path, scope, selected, role):
    from agent_flow.core.local_skills import local_skill_prompt_block

    _scoped_contract(tmp_path)
    prompt = local_skill_prompt_block(
        tmp_path, "implement", host="codex", document_scope=scope,
        changed_files=("apps/b/unrelated-activation.py",), role=role,
    )
    assert "SCOPED_ROOT_BODY" in prompt
    assert "NORM_BODY_common" in prompt
    assert "NORM_BODY_unconditional" in prompt
    for name in ("a", "b"):
        assert (f"NORM_BODY_{name}" in prompt) == (name in selected)


def test_unselected_reference_remains_pinned_and_blocks_drift(tmp_path):
    from agent_flow.core.architecture_policy import architecture_snapshot_block_reason

    contract = _scoped_contract(tmp_path)
    arguments = dict(project_root=tmp_path, phase_id="implement", host="codex",
                     document_scope=("apps/a/model.py",))
    before = resolve_phase_skills(**arguments)
    reference = contract.parent / "references/b.md"
    assert str(reference) in {item.path for item in before.architecture_norms}
    reference.write_text("UNSELECTED_CHANGED_BODY", encoding="utf-8")
    after = resolve_phase_skills(**arguments)
    assert architecture_snapshot_block_reason(
        after.architecture_snapshot, before.architecture_snapshot.digest,
    ) == "architecture_policy_drift"
    reference.unlink()
    with pytest.raises(ArchitectureContractError):
        resolve_phase_skills(**arguments)


def test_unselected_reference_still_requires_git_tracking(tmp_path):
    import subprocess
    from agent_flow.core.architecture_policy import architecture_snapshot_block_reason

    _scoped_contract(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "rm", "--cached", "skills/architecture/references/b.md"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    resolution = resolve_phase_skills(
        project_root=tmp_path, phase_id="implement", host="codex",
        document_scope=("apps/a/model.py",),
    )
    snapshot = resolution.architecture_snapshot
    assert snapshot.untracked == ("skills/architecture/references/b.md",)
    assert architecture_snapshot_block_reason(
        snapshot, snapshot.digest,
    ) == "architecture_contract_untracked"


def test_scope_growth_keeps_route_identity_despite_equal_bytes_and_scope_shrink(tmp_path):
    from agent_flow.core.local_skills import local_skill_prompt_block
    from agent_flow.core.skill_resolver import ResolutionContext

    contract = _scoped_contract(tmp_path)
    for name in ("a", "b"):
        (contract.parent / "references" / f"{name}.md").write_text(
            "EQUAL_SCOPED_BODY", encoding="utf-8",
        )
    arguments = dict(project_root=tmp_path, phase_id="implement", host="codex",
                     context=ResolutionContext())
    first = resolve_phase_skills(**arguments, document_scope=("apps/a/model.py",))
    grown = resolve_phase_skills(**arguments, document_scope=("apps/b/model.py",),
                                 required_document_ids=first.required_document_ids)
    assert set(first.required_document_ids) < set(grown.required_document_ids)
    shrunk = resolve_phase_skills(**arguments, document_scope=(),
                                  required_document_ids=grown.required_document_ids)
    assert shrunk.required_document_ids == grown.required_document_ids
    prompt = local_skill_prompt_block(tmp_path, "implement", resolution=shrunk)
    assert prompt.count("EQUAL_SCOPED_BODY") == 1
    assert "route: architecture-reference / architecture / skills/architecture/references/a.md" in prompt
    assert "route: architecture-reference / architecture / skills/architecture/references/b.md" in prompt


def test_changed_dependency_norm_selects_every_contract_document(tmp_path):
    from agent_flow.core.local_skills import local_skill_prompt_block

    root = _scoped_contract(tmp_path)
    root.write_text(
        root.read_text(encoding="utf-8").replace(
            "requires_docs:", "requires: [shared-policy]\nrequires_docs:",
        ),
        encoding="utf-8",
    )
    _skill(tmp_path, "shared-policy", "")
    prompt = local_skill_prompt_block(
        tmp_path, "implement", host="codex", document_scope=("skills/shared-policy/SKILL.md",),
    )
    assert "NORM_BODY_a" in prompt
    assert "NORM_BODY_b" in prompt


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
    """Verify that unrelated malformed metadata does not activate or block resolution.

    `skills/`와 drop-box는 배치만으로 켜지므로 거기의 깨진 YAML은 "무관"이 아니다.
    vendor 루트(`.claude/skills`)는 선언이 있어야 켜지므로 그쪽이 무관한 자리다.
    """
    _selection(tmp_path, "pending")
    _skill(tmp_path, "custom-review", "requires: [required-rule]\n")
    _skill(tmp_path, "required-rule", "")
    malformed = tmp_path / ".claude/skills/optional-rule/SKILL.md"
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


@pytest.mark.parametrize("location", ["skills", ".agent-flow/local-skills"])
def test_undeclared_repo_owned_skill_activates_by_placement(tmp_path, location):
    """`skills/`(팀)와 drop-box(개인)는 파일을 둔 것 자체가 선언이다.

    반증: `skills/`에만 `workflowPhases`를 요구하면 같은 파일을 폴더 사이로 옮길 때
    코드 작성·리뷰에서 조용히 빠진다.
    """
    _selection(tmp_path, "pending")
    path = tmp_path / location / "dev-conventions" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: dev-conventions\n---\n\nUse `type`, never `interface`.\n", encoding="utf-8")

    for phase in ("implement", "review"):
        resolution = resolve_phase_skills(project_root=tmp_path, phase_id=phase, host="codex")
        assert "dev-conventions" in {skill.name for skill in resolution.required}, phase
    design = resolve_phase_skills(project_root=tmp_path, phase_id="design", host="codex")
    assert "dev-conventions" not in {skill.name for skill in design.required}


@pytest.mark.parametrize("location", ["skills", ".agent-flow/local-skills"])
def test_project_architecture_blocks_clean_and_loads_under_pending(tmp_path, location):
    """`architecture/`가 있으면 clean은 거부된다(규범이 둘). pending에서는 일반 스킬로 붙는다."""
    path = tmp_path / location / "architecture" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: architecture\n---\n\nDomain never imports adapters.\n", encoding="utf-8")
    _skill(tmp_path, "clean-architecture-core", "")

    _selection(tmp_path, "clean")
    with pytest.raises(ArchitectureContractError, match="project architecture contract exists"):
        resolve_phase_skills(project_root=tmp_path, phase_id="implement", host="codex")
    _selection(tmp_path, "pending")
    pending = resolve_phase_skills(project_root=tmp_path, phase_id="implement", host="codex")
    assert "architecture" in {skill.name for skill in pending.required}


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
    # 규칙 스킬은 의존성 전개로만 들어와야 한다. 배치 활성화가 섞이면 이 테스트의
    # 대상(모드별 requires 전개)이 가려지므로 이 phase에 걸리지 않는 선언을 둔다.
    for name in ("common-rule", "legacy-rule", "clean-rule", "local-rule", "pending-rule"):
        _skill(tmp_path, name, "workflowPhases: [design]\n")
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
