from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

KIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_ROOT / "src"))

from agent_flow.adapters.hosted import HostedAdapter, _reviewer_jobs  # noqa: E402
from agent_flow.core.profile_resolution import resolve_profile  # noqa: E402
from agent_flow.core.skill_resolver import PhaseSkills  # noqa: E402
from agent_flow.runner import Phase  # noqa: E402


def _review_project(root: Path, override: list[dict[str, object]] | None) -> HostedAdapter:
    profiles = root / ".agent-flow/profiles"
    profiles.mkdir(parents=True)
    (profiles / "probe.yaml").write_text(yaml.safe_dump({
        "id": "probe",
        "review_angles": [{"id": "shipped", "prompt": "templates/_shared/review/types.md"}],
    }), encoding="utf-8")
    (root / ".agent-flow/kit.json").write_text(
        json.dumps({"profile": "probe"}), encoding="utf-8",
    )
    if override is not None:
        (profiles / "probe.local.yaml").write_text(
            yaml.safe_dump({"review_angles": override}), encoding="utf-8",
        )
    adapter = HostedAdapter("codex")
    adapter._profile_id, adapter._profile_snapshot = resolve_profile(KIT_ROOT, root)
    return adapter


@pytest.mark.parametrize(
    "override,expected",
    [
        (None, ["generalist", "types", "shipped"]),
        ([], ["generalist", "types"]),
        ([{"id": "local", "prompt": "templates/_shared/review/types.md"}], ["generalist", "types", "local"]),
    ],
)
def test_loaded_local_profile_controls_actual_review_jobs(
    tmp_path: Path, override: list[dict[str, object]] | None, expected: list[str],
) -> None:
    adapter = _review_project(tmp_path, override)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    jobs, _ = _reviewer_jobs(Phase(id="final-review", description="", multi_review=True),
    run_dir, tmp_path, adapter,)

    assert [job.angle_id for job in jobs] == expected


def test_local_baseline_prompt_override_cannot_gate_mandatory_jobs(tmp_path: Path) -> None:
    adapter = _review_project(tmp_path, [
        {
            "id": angle_id,
            "prompt": prompt,
            "requires": "missing-skill",
            "task_terms": ["never matches"],
            "path_globs": ["never/**"],
        }
        for angle_id, prompt in (
            ("generalist", "templates/_shared/review/project-generalist.md"),
            ("types", "templates/_shared/review/types.md"),
        )
    ])
    prompts = tmp_path / "templates/_shared/review"
    prompts.mkdir(parents=True)
    (prompts / "project-generalist.md").write_text("PROJECT_GENERALIST_RULE", encoding="utf-8")
    (prompts / "types.md").write_text("SHADOWED_BUILTIN_RULE", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    jobs, _ = _reviewer_jobs(Phase(id="final-review", description="", multi_review=True),
    run_dir, tmp_path, adapter,)

    assert [job.angle_id for job in jobs] == ["generalist", "types"]
    for provider in ("claude", "codex"):
        assert "PROJECT_GENERALIST_RULE" in jobs[0].prompt_for(provider)
        assert "SHADOWED_BUILTIN_RULE" not in jobs[1].prompt_for(provider)


def test_local_selectors_keep_or_matching_and_required_skill_precedence(tmp_path: Path) -> None:
    adapter = _review_project(tmp_path, [
        {
            "id": "task-selected",
            "prompt": "templates/_shared/review/types.md",
            "requires": "python-development-guide",
            "task_terms": ["endpoint"],
            "path_globs": ["never/**"],
        },
        {
            "id": "path-selected",
            "prompt": "templates/_shared/review/types.md",
            "task_terms": ["never matches"],
            "path_globs": ["**/*.py"],
        },
        {
            "id": "missing-requirement",
            "prompt": "templates/_shared/review/types.md",
            "requires": "missing-skill",
            "task_terms": ["endpoint"],
        },
        {
            "id": "empty-selectors",
            "prompt": "templates/_shared/review/types.md",
            "task_terms": [],
            "path_globs": [],
        },
        {
            "id": "architecture-design",
            "prompt": "templates/_shared/review/types.md",
        },
    ])
    adapter._task_text = "Update endpoint response"
    adapter._changed_files = ("service.py",)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    phase = Phase(
        id="final-review", description="", multi_review=True,
        skills=PhaseSkills(required=("python-development-guide",)),
    )

    jobs, _ = _reviewer_jobs(phase, run_dir, tmp_path, adapter)

    assert [job.angle_id for job in jobs] == [
        "generalist", "types", "task-selected", "path-selected",
    ]


def test_local_prompt_replacement_cannot_escape_review_template_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="review_angles.*prompt"):
        _review_project(tmp_path, [
            {"id": "generalist", "prompt": "../../../outside.md"},
        ])


def test_misspelled_local_selector_cannot_become_unconditional(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="path_glob"):
        _review_project(tmp_path, [
            {
                "id": "scoped",
                "prompt": "templates/_shared/review/types.md",
                "path_glob": ["apps/main/**"],
            },
        ])
