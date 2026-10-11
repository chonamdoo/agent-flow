from __future__ import annotations

import re
from itertools import chain
from pathlib import Path

import pytest
import yaml

KIT_ROOT = Path(__file__).resolve().parent.parent


def _unpinned_action_refs(text: str) -> list[str]:
    workflow = yaml.safe_load(text)
    unpinned = []
    for job in workflow.get("jobs", {}).values():
        for entry in chain((job,), job.get("steps", [])):
            if "uses" not in entry:
                continue
            uses = entry["uses"]
            if not isinstance(uses, str):
                unpinned.append(repr(uses))
                continue
            if uses.startswith(("./", "docker://")):
                continue
            repository, _, ref = uses.partition("@")
            if repository.split("/", 1)[0].lower() == "actions":
                continue
            if re.fullmatch(r"[0-9a-fA-F]{40}", ref) is None:
                unpinned.append(uses)
    return unpinned


def test_repository_actions_use_full_commit_shas():
    workflows = KIT_ROOT / ".github" / "workflows"
    for path in sorted(workflows.iterdir()):
        if path.suffix in (".yml", ".yaml"):
            unpinned = _unpinned_action_refs(path.read_text(encoding="utf-8"))
            assert unpinned == [], f"{path.relative_to(KIT_ROOT)}: {unpinned}"


@pytest.mark.parametrize(
    "reference",
    (
        "pnpm/action-setup@v4",
        "vendor/tool@main",
        "vendor/tool@" + "a" * 39,
        "vendor/tool@" + "a" * 41,
        "vendor/tool@" + "a" * 39 + "g",
        "vendor/tool",
        "actions-evil/tool/subdir@v1",
    ),
)
def test_policy_rejects_mutable_external_action_refs(reference):
    text = yaml.safe_dump({"jobs": {"check": {"steps": [{"uses": reference}]}}})
    assert _unpinned_action_refs(text) == [reference]


@pytest.mark.parametrize(
    "reference",
    (
        "vendor/tool@" + "a" * 40,
        "vendor/tool/subdir@" + "A" * 40,
        "actions/checkout@v4",
        "./.github/actions/local",
        "docker://alpine:3.8",
        "docker://alpine@sha256:" + "a" * 64,
    ),
)
def test_policy_preserves_non_repository_action_refs(reference):
    text = yaml.safe_dump(
        {
            "jobs": {
                "check": {
                    "steps": [
                        {"uses": reference, "with": {"uses": "vendor/tool@main"}},
                        {"run": "echo 'uses: vendor/tool@main'"},
                    ]
                }
            }
        }
    )
    assert _unpinned_action_refs(text) == []


@pytest.mark.parametrize(
    "ref, expected",
    (
        ("main", ["vendor/tool/.github/workflows/check.yml@main"]),
        ("a" * 40, []),
    ),
)
def test_policy_checks_reusable_workflow_refs(ref, expected):
    reference = f"vendor/tool/.github/workflows/check.yml@{ref}"
    text = yaml.safe_dump({"jobs": {"check": {"uses": reference}}})
    assert _unpinned_action_refs(text) == expected
