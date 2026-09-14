from pathlib import Path

import pytest

from agent_flow.core.phase_workflow import load_phase_workflow_definition


def test_unknown_completion_disposition_cannot_fall_back_to_cleanup(tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "custom.yaml").write_text(
        "id: custom\ncompletion_disposition: local-hanoff\nphases:\n  - id: implement\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="completion_disposition"):
        load_phase_workflow_definition(tmp_path, "custom")


def test_custom_workflow_cannot_declare_bundled_replacement_authority(tmp_path: Path) -> None:
    """Verify that custom workflow cannot declare bundled replacement authority."""
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "default.yaml").write_text(
        "id: default\nphases:\n  - id: implement\n    skills:\n"
        "      required: [clean-architecture-core]\n"
        "      replaceable_architecture: true\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown keys.*replaceable_architecture"):
        load_phase_workflow_definition(tmp_path, "default")


@pytest.mark.parametrize("name", ["custom", "default"])
@pytest.mark.parametrize("alias_installed", [False, True])
def test_obsolete_required_alias_requires_explicit_migration(
    tmp_path: Path, name: str, alias_installed: bool
) -> None:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    source = (
        f"id: {name}\nphases:\n  - id: implement\n"
        "    skills:\n      required: [clean-architecture]\n"
    )
    path = workflows / f"{name}.yaml"
    path.write_text(source, encoding="utf-8")
    if alias_installed:
        alias = tmp_path / "skills" / "clean-architecture"
        alias.mkdir(parents=True)
        (alias / "SKILL.md").write_text(
            "---\nname: clean-architecture\n---\nCompatibility alias.\n",
            encoding="utf-8",
        )

    with pytest.raises(ValueError, match=r"migration required.*clean-architecture-core"):
        load_phase_workflow_definition(tmp_path, name)
    assert path.read_text(encoding="utf-8") == source


def test_custom_canonical_consumer_has_no_kit_replacement_authority(tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "default.yaml").write_text(
        "id: default\nphases:\n  - id: implement\n    skills:\n"
        "      required: [clean-architecture-core]\n",
        encoding="utf-8",
    )

    definition = load_phase_workflow_definition(tmp_path, "default")

    skills = definition.phases[0].skills
    assert skills is not None
    assert skills.required == ("clean-architecture-core",)
    assert not skills.replaceable_architecture


def test_bundled_copy_retains_kit_replacement_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packaged = tmp_path / "package" / "workflows" / "default.yaml"
    packaged.parent.mkdir(parents=True)
    source = (
        "id: default\nphases:\n  - id: implement\n    skills:\n"
        "      required: [clean-architecture-core]\n"
    )
    packaged.write_text(source, encoding="utf-8")
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "default.yaml").write_text(source, encoding="utf-8")
    monkeypatch.setattr(
        "agent_flow.core.phase_workflow._packaged_workflow_path", lambda name: packaged
    )

    definition = load_phase_workflow_definition(tmp_path, "default")

    skills = definition.phases[0].skills
    assert skills is not None
    assert skills.replaceable_architecture


@pytest.mark.parametrize("mode", ["clean", "local", "pending"])
def test_conditional_markers_enforce_only_selected_architecture(mode: str) -> None:
    from agent_flow.core.markers import missing_markers
    from agent_flow.core.phase_workflow import (
        effective_phase_markers,
        parse_phase_workflow_definition,
    )

    definition = parse_phase_workflow_definition(
        b"id: custom\nphases:\n  - id: review\n"
        b"    required_markers: ['architecture-contract: applied']\n"
        b"    required_markers_by_architecture:\n"
        b"      clean: ['repository-boundary: pass|fail']\n"
        b"      local: ['local-boundary: pass|fail']\n"
        b"      pending: ['existing-pattern: checked']\n",
        source=Path("custom.yaml"),
        name="custom",
    )
    markers = effective_phase_markers(definition.phases[0], mode)
    evidence = "## Completion Gate\narchitecture-contract: applied\n"
    expected = {
        "clean": ("repository-boundary: pass|fail", "repository-boundary: pass"),
        "local": ("local-boundary: pass|fail", "local-boundary: pass"),
        "pending": ("existing-pattern: checked", "existing-pattern: checked"),
    }
    requirement, completion = expected[mode]
    assert missing_markers(evidence, markers) == [requirement]
    assert missing_markers(evidence + completion + "\n", markers) == []


@pytest.mark.parametrize(
    "conditional",
    [
        None,
        [],
        "clean",
        {"unknown": []},
        {1: []},
        {"clean": None},
        {"local": "marker: applied"},
        {"pending": [False]},
        {"clean": [""]},
        {"clean": ["   "]},
        {"clean": [{"marker": "applied"}]},
    ],
)
def test_malformed_conditional_marker_schema_is_rejected(conditional: object) -> None:
    import yaml
    from agent_flow.core.phase_workflow import parse_phase_workflow_definition

    source = yaml.safe_dump({
        "id": "custom",
        "phases": [{
            "id": "review",
            "required_markers_by_architecture": conditional,
        }],
    }).encode()
    with pytest.raises(ValueError, match="required_markers_by_architecture"):
        parse_phase_workflow_definition(source, source=Path("custom.yaml"), name="custom")


def test_absent_conditional_field_preserves_legacy_export_and_source_digest() -> None:
    import hashlib
    from agent_flow.core.phase_workflow import (
        effective_phase_markers,
        parse_phase_workflow_definition,
    )

    source = b"id: custom\nphases:\n  - id: review\n    required_markers: ['clean-architecture: applied|n/a']\n"
    definition = parse_phase_workflow_definition(
        source, source=Path("custom.yaml"), name="custom", pinned_legacy=True
    )
    assert definition.to_json_dict() == {
        "id": "custom",
        "source": "custom.yaml",
        "digest": hashlib.sha256(source).hexdigest(),
        "completion_disposition": "integrated-cleanup",
        "phases": [{
            "id": "review", "description": "", "prompt": None,
            "pause_after": False, "optional": False, "multi_review": False,
            "routes": None,
            "required_markers": ("clean-architecture: applied|n/a",),
            "artifact": "review.md", "skills": None, "architecture_decision": "existing",
        }],
    }
    for mode in ("clean", "local", "pending"):
        assert effective_phase_markers(definition.phases[0], mode) == (
            "clean-architecture: applied|n/a",
        )


def test_explicit_empty_conditional_field_is_preserved_in_export() -> None:
    from agent_flow.core.phase_workflow import parse_phase_workflow_definition

    definition = parse_phase_workflow_definition(
        b"id: custom\nphases:\n  - id: review\n    required_markers_by_architecture: {}\n",
        source=Path("custom.yaml"), name="custom",
    )
    assert definition.to_json_dict()["phases"][0]["required_markers_by_architecture"] == {}


@pytest.mark.parametrize("workflow,phase_id", [
    ("default", "design"), ("full-feature", "ddd-design"),
])
def test_fresh_design_requires_clean_boundary_evidence_only_in_clean_mode(
    workflow: str, phase_id: str
) -> None:
    from agent_flow.core.markers import missing_markers
    from agent_flow.core.phase_workflow import effective_phase_markers

    definition = load_phase_workflow_definition(Path(__file__).resolve().parents[1], workflow)
    phase = next(phase for phase in definition.phases if phase.id == phase_id)
    evidence = (
        "## Architecture Boundary Map\nSelected contract ownership evidence.\n"
        "## Composition Root\nExplicit construction.\n"
        "## Testability Boundary\nConsumer seam.\n"
        "## Spec Items\nSPEC-1: preserve boundaries\n"
        "## Design Values\n"
        "## Completion Gate\n"
        "architecture-contract: applied\n"
        "solid-srp-change-reason: ownership\n"
        "solid-ocp-extension-points: stable ports\n"
        "solid-lsp-contracts: preserved\n"
        "solid-isp-consumer-ports: narrow\n"
        "solid-dip-dependency-direction: inward\n"
        "spec-items: SPEC-1\n"
        "design-values: none\n"
    )
    assert missing_markers(evidence, effective_phase_markers(phase, "local")) == []
    assert missing_markers(evidence, effective_phase_markers(phase, "pending")) == []
    assert missing_markers(evidence, effective_phase_markers(phase, "clean")) == [
        "## Dependency Rule",
        "## Use Case Boundaries",
        "usecase-interface: required|optional|n/a",
        "usecase-composition: none|domain-service|application-service|orchestrator|justified",
        "## Repository Boundaries",
        "## Cache Boundary",
        "cache-required: yes|no",
        "memory-cache: required|optional|n/a",
        "disk-cache: required|optional|n/a",
        "cache-invalidation-policy:",
        "## Mapping Boundary",
        "remote-dto-domain-mapper: required|optional|n/a",
        "entity-domain-mapper: required|optional|n/a",
        "domain-ui-mapper: required|optional|n/a",
    ]


@pytest.mark.parametrize("workflow,phase_id", [
    ("default", "final-review"), ("full-feature", "architecture-review"),
])
def test_fresh_clean_review_preserves_boundary_exceptions(
    workflow: str, phase_id: str
) -> None:
    from agent_flow.core.markers import missing_markers
    from agent_flow.core.phase_workflow import effective_phase_markers

    definition = load_phase_workflow_definition(Path(__file__).resolve().parents[1], workflow)
    phase = next(phase for phase in definition.phases if phase.id == phase_id)
    markers = effective_phase_markers(phase, "clean")
    evidence = "## Completion Gate\nusecase-boundary: n/a\nusecase-calls-usecase: n/a\n"
    missing = missing_markers(evidence, markers)
    assert "usecase-boundary: pass|fail|n/a" not in missing
    assert "usecase-calls-usecase: pass|fail|n/a" not in missing
    assert "repository-boundary: pass|fail" in missing
    assert "repository-boundary: pass|fail" in missing_markers(
        evidence + "repository-boundary: n/a\n", markers
    )
    assert "repository-boundary: pass|fail" not in missing_markers(
        evidence + "repository-boundary: pass\n", markers
    )
