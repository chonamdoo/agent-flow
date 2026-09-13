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

    with pytest.raises(ValueError, match="migration required.*clean-architecture-core"):
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
