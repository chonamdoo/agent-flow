"""`skills/qa`의 작성 품질(A) 구조 검사. 기준은 chonamdoo/work-skills의 `tests/test_package.py`다.

행동(B)은 이 검사로 증명되지 않는다. `evals/skill_behavior.py --skill qa`가 새 맥락의 실행자로 잰다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "skills" / "qa"
EVALS = REPO / "evals" / "skill-behavior" / "qa"
# work-skills는 name·description만 허용한다. workflowPhases는 Agent Flow runner가 phase 라우팅에
# 실제로 요구하는 host metadata라 허용한다(work-to-skill: 검증된 host 요구일 때만).
ALLOWED_KEYS = {"name", "description", "workflowPhases"}


def _frontmatter() -> tuple[dict, str]:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, header, body = text.split("---", 2)
    return yaml.safe_load(header), body


def test_frontmatter_follows_the_work_skills_rules():
    metadata, body = _frontmatter()
    assert set(metadata) <= ALLOWED_KEYS and {"name", "description"} <= set(metadata)
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", metadata["name"]) and len(metadata["name"]) <= 64
    assert metadata["name"] == SKILL.name
    assert 0 < len(metadata["description"]) <= 1024
    assert metadata["workflowPhases"] == ["qa"]
    assert body.strip()


def test_every_reference_resolves_inside_the_skill_and_is_linked_with_a_reason():
    entry = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    for path in SKILL.rglob("*"):
        assert not path.is_symlink(), path
        if path.suffix != ".md":
            continue
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            url = urlsplit(target)
            if url.scheme or not url.path:
                continue
            resolved = (path.parent / unquote(url.path)).resolve()
            assert resolved.is_relative_to(SKILL.resolve()) and resolved.is_file(), target
    # Each reference has a concrete use: the entrypoint links it in a "when to read" row.
    for reference in (SKILL / "references").glob("*.md"):
        row = next((line for line in entry.splitlines() if f"references/{reference.name})" in line), None)
        assert row is not None and row.count("|") >= 3, reference.name


def test_evaluator_material_stays_outside_the_installable_skill():
    assert not {"tests", "evals", "fixtures"} & {part for path in SKILL.rglob("*") for part in path.parts}
    tasks = json.loads((EVALS / "tasks.json").read_text(encoding="utf-8"))
    criteria = json.loads((EVALS / "expectations.json").read_text(encoding="utf-8"))["criteria"]
    ids = [task["id"] for task in tasks]
    assert len(ids) == len(set(ids)) and {task["split"] for task in tasks} == {"dev", "heldout"}
    for task in tasks:
        assert (EVALS / "fixtures" / task["fixture"] / "CHANGE.md").is_file()
        # Executor input never carries the expected answer.
        assert "expect" not in task and "acceptance" not in task
    for criterion in criteria:
        assert {"id", "case", "required", "method", "result", "basis", "conditions"} <= set(criterion)
        assert {"kind", "locator", "statement"} <= set(criterion["basis"])


def test_qa_phases_require_the_skill():
    from agent_flow.core.phase_workflow import load_phase_workflow_definition

    for workflow in ("development", "bugfix"):
        qa = next(p for p in load_phase_workflow_definition(REPO, workflow).phases if p.id == "qa")
        assert "qa" in qa.skills.required, workflow
