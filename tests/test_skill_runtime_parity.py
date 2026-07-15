from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_flow.core.skill_plan import resolve_runtime_skill_plan


KIT_ROOT = Path(__file__).resolve().parent.parent


def _node_plan(
    index: dict[str, object],
    *,
    phase: str,
    files: list[str],
    task: str,
    required_skills: list[str] | None = None,
) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    script = """
import fs from 'node:fs';
import { resolveRuntimeSkillPlan } from './lib/skill-selection.mjs';
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(resolveRuntimeSkillPlan(input.index, {
  phaseId: input.phase,
  changedFiles: input.files,
  taskScope: input.task,
  requiredSkills: input.required_skills,
})));
"""
    result = subprocess.run(
        (node, "--input-type=module", "-e", script),
        cwd=KIT_ROOT,
        input=json.dumps(
            {
                "index": index,
                "phase": phase,
                "files": files,
                "task": task,
                "required_skills": required_skills or [],
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _index(host: str, *, explicit: bool = False) -> dict[str, object]:
    return {
        "version": 2,
        "revision": "f" * 64,
        "selection": {
            "profiles": [],
            "explicit_skills": ["testing-localization"] if explicit else [],
            "profile_routing": {"profiles": {}, "escalations": {}},
        },
        "skills": [
            {"name": "code-generation-discipline", "path": "code", "tree_hash": "a" * 64},
            {
                "name": "always-code",
                "path": "always",
                "tree_hash": "b" * 64,
                "source": "host-bootstrap",
                "source_host": host,
                "activation": "always",
                "requires": ["testing-localization"],
            },
            {
                "name": "conditional",
                "path": "conditional",
                "tree_hash": "c" * 64,
                "source": "host-bootstrap",
                "source_host": host,
                "activation": "conditional",
                "workflowPhases": ["implement"],
                "taskTerms": ["payment"],
                "pathGlobs": ["src/pay/**"],
            },
            {
                "name": "testing-localization",
                "path": "testing",
                "tree_hash": "d" * 64,
                "activation": "always",
            },
        ],
    }


@pytest.mark.parametrize("host", ["claude", "codex", "omp"])
def test_node_and_python_share_runtime_plan_for_each_real_host(host: str) -> None:
    index = _index(host)
    node = _node_plan(index, phase="implement", files=["src/pay/api.ts"], task="payment retry")
    python = resolve_runtime_skill_plan(
        index,
        phase_id="implement",
        changed_files=["src/pay/api.ts"],
        task_scope="payment retry",
    )

    assert python == node
    assert [skill["name"] for skill in python["skills"]] == [
        "always-code",
        "code-generation-discipline",
        "conditional",
    ]


def test_testing_localization_enters_both_runtimes_only_when_explicit() -> None:
    implicit = _index("codex")
    explicit = _index("codex", explicit=True)

    implicit_node = _node_plan(implicit, phase="implement", files=[], task="")
    implicit_python = resolve_runtime_skill_plan(implicit, phase_id="implement")
    explicit_node = _node_plan(explicit, phase="implement", files=[], task="")
    explicit_python = resolve_runtime_skill_plan(explicit, phase_id="implement")

    assert implicit_python == implicit_node
    assert explicit_python == explicit_node
    assert "testing-localization" not in {skill["name"] for skill in implicit_python["skills"]}
    assert "testing-localization" in {skill["name"] for skill in explicit_python["skills"]}


def test_architecture_phase_includes_declared_cross_platform_skills_in_both_runtimes() -> None:
    index = _index("codex")
    index["skills"].extend(
        [
            {"name": "clean-architecture-core", "path": "clean", "tree_hash": "e" * 64},
            {
                "name": "ddd-architecture",
                "path": "ddd",
                "tree_hash": "f" * 64,
                "requires": ["clean-architecture-core"],
            },
        ]
    )
    required = ["ddd-architecture"]

    node = _node_plan(
        index,
        phase="ddd-design",
        files=[],
        task="architecture",
        required_skills=required,
    )
    python = resolve_runtime_skill_plan(
        index,
        phase_id="ddd-design",
        task_scope="architecture",
        required_skills=required,
    )

    assert python == node
    assert [skill["name"] for skill in python["skills"]] == [
        "always-code",
        "clean-architecture-core",
        "ddd-architecture",
    ]

    tuple_python = resolve_runtime_skill_plan(
        index,
        phase_id="ddd-design",
        task_scope="architecture",
        required_skills=tuple(required),
    )
    assert tuple_python == python


@pytest.mark.parametrize(
    ("files", "touched_profiles", "included", "excluded"),
    [
        (
            ["src/App.tsx"],
            ["react-native"],
            {"react-native-clean-architecture", "react-native-development-guide"},
            {"android-clean-architecture", "ios-clean-architecture"},
        ),
        (
            ["android/app/src/main/java/demo/Main.kt"],
            ["android", "react-native"],
            {"android-clean-architecture", "android-code-review", "react-native-clean-architecture"},
            {"ios-clean-architecture"},
        ),
        (
            ["ios/App/App.swift"],
            ["ios", "react-native"],
            {"ios-clean-architecture", "react-native-clean-architecture"},
            {"android-clean-architecture"},
        ),
    ],
)
def test_react_native_native_roots_inject_only_touched_platform_skills_in_both_runtimes(
    files: list[str],
    touched_profiles: list[str],
    included: set[str],
    excluded: set[str],
) -> None:
    routing = json.loads((KIT_ROOT / "skills" / "profile-routing.json").read_text(encoding="utf-8"))
    skills = [
        {"name": "code-generation-discipline", "path": "code"},
        {"name": "clean-architecture-core", "path": "core"},
        {
            "name": "react-native-clean-architecture",
            "path": "rn-clean",
            "requires": ["clean-architecture-core"],
        },
        {"name": "react-native-development-guide", "path": "rn-guide"},
        {
            "name": "android-clean-architecture",
            "path": "android-clean",
            "requires": ["clean-architecture-core"],
        },
        {"name": "android-code-review", "path": "android-review"},
        {
            "name": "ios-clean-architecture",
            "path": "ios-clean",
            "requires": ["clean-architecture-core"],
        },
    ]
    for index, skill in enumerate(skills):
        skill["tree_hash"] = format(index, "x") * 64
    index = {
        "selection": {
            "profiles": ["react-native"],
            "skill_profiles": ["android", "ios", "react-native"],
            "explicit_skills": [],
            "required_review": {
                "react-native": [
                    "react-native-development-guide",
                    "react-native-clean-architecture",
                ],
                "android": ["android-code-review", "android-clean-architecture"],
                "ios": ["ios-clean-architecture"],
            },
            "profile_routing": routing,
        },
        "skills": skills,
    }

    node = _node_plan(index, phase="implement", files=files, task="")
    python = resolve_runtime_skill_plan(index, phase_id="implement", changed_files=files)

    assert python == node
    assert python["touched_profiles"] == touched_profiles
    selected = {skill["name"] for skill in python["skills"]}
    assert included <= selected
    assert excluded.isdisjoint(selected)
