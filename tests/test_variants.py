"""Reduced-workflow search variants (#409 program v2 §5)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src"))
import variants  # noqa: E402
from agent_flow.core.phase_workflow import parse_phase_workflow_definition  # noqa: E402


def _parse(definition: dict):
    text = yaml.safe_dump(definition, sort_keys=False, allow_unicode=True).encode("utf-8")
    return parse_phase_workflow_definition(text, source=Path(f"{definition['id']}.yaml"), name=definition["id"])


@pytest.mark.parametrize("spec", [{}, {"planning": False}, {"tdd": False, "reviewers": 2},
                                  {"planning": False, "design": False, "tdd": False, "architecture_review": False}])
def test_every_variant_is_a_workflow_the_kit_accepts(spec):
    parsed = _parse(variants.workflow(ROOT, spec))
    ids = [phase.id for phase in parsed.phases]
    for key, dropped in variants.DROPS.items():
        assert all((phase in ids) == (spec.get(key, True)) for phase in dropped)


def test_routes_into_a_dropped_phase_go_to_the_next_kept_one():
    definition = variants.workflow(ROOT, {"architecture_review": False})
    multi = next(phase for phase in definition["phases"] if phase["id"] == "multi-review")
    assert multi["routes"]["approve"] == "gates"
    definition = variants.workflow(ROOT, {"planning": False, "design": False})
    assert definition["phases"][0]["id"] == "worktree"
    assert not any(to in variants.DROPS["planning"] for phase in definition["phases"]
                   for to in (phase.get("routes") or {}).values())


def test_the_name_depends_on_the_spec_only_and_defaults_do_not_count():
    assert variants.name({"planning": True, "reviewers": 2}) == variants.name({"reviewers": 2})
    assert variants.name({"reviewers": 2}) != variants.name({"reviewers": 5})


def test_unknown_or_free_form_dimensions_are_refused():
    with pytest.raises(ValueError):
        variants.normalize({"prompt": "be careful"})
    with pytest.raises(ValueError):
        variants.normalize({"reviewers": 3})
    with pytest.raises(ValueError):
        variants.normalize({"tdd": "off"})


def test_the_reviewer_count_is_the_generalist_plus_the_first_angles():
    override = variants.profile_override(ROOT, "android", {"reviewers": 2})
    assert [angle["id"] for angle in override["review_angles"]] == ["architecture-design"]
    assert len(variants.profile_override(ROOT, "android", {"reviewers": 5})["review_angles"]) == 4
    assert variants.profile_override(ROOT, "android", {"planning": False}) is None


def test_write_puts_the_variant_in_the_project_only(tmp_path):
    record = variants.write(ROOT, tmp_path, "android", {"tdd": False, "reviewers": 2})
    assert (tmp_path / ".agent-flow" / "workflows" / f"{record['name']}.yaml").is_file()
    profile = yaml.safe_load((tmp_path / ".agent-flow" / "profiles" / "android.local.yaml").read_text(encoding="utf-8"))
    assert len(profile["review_angles"]) == 1
    assert "red" not in record["phases"] and record["spec"] == {"tdd": False, "reviewers": 2}
