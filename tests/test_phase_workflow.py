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
