from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_flow.core.skill_plan import resolve_runtime_skill_plan


KIT_ROOT = Path(__file__).resolve().parent.parent


def _node_plan(index: dict[str, object], *, phase: str, files: list[str], task: str) -> dict[str, object]:
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
})));
"""
    result = subprocess.run(
        (node, "--input-type=module", "-e", script),
        cwd=KIT_ROOT,
        input=json.dumps({"index": index, "phase": phase, "files": files, "task": task}),
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
