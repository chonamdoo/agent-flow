from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_flow.core.phase_workflow import load_phase_workflow_definition
from agent_flow.core.skill_plan import CODE_SKILL_PHASES
from agent_flow.runner import Phase, Runner, phase_contract_issues, phase_contract_route_key


KIT_ROOT = Path(__file__).resolve().parent.parent


def _artifact(*, skills: list[str], requirements: dict[str, str]) -> str:
    payload = json.dumps(
        {"applied_skills": skills, "requirements": requirements},
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"# result\n\nphase-contract: {payload}\n"


def _node_evaluation(phase: Phase, artifact: str) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    script = """
import fs from 'node:fs';
import { evaluatePhaseContract } from './lib/phase-contract.mjs';
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(evaluatePhaseContract(input.phase, input.artifact)));
"""
    result = subprocess.run(
        (node, "--input-type=module", "-e", script),
        cwd=KIT_ROOT,
        input=json.dumps(
            {
                "phase": {
                    "required_skills": list(phase.required_skills),
                    "requirements": list(phase.requirements),
                    "skill_compatibility": getattr(phase, "skill_compatibility", None),
                },
                "artifact": artifact,
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_phase_schema_normalizes_skills_requirements_artifacts_and_routes(tmp_path: Path) -> None:
    (tmp_path / "profiles").mkdir()
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "contract.yaml").write_text(
        """
id: contract
phases:
  - id: architecture
    description: design
    skills:
      required: [ddd-architecture, clean-architecture]
    requirements: [domain-modeled, boundaries-mapped]
    artifacts: [artifacts/design.md, artifacts/decisions.json]
    routes:
      success: done
      failure: architecture
  - id: done
    artifacts: [artifacts/done.md]
""".lstrip(),
        encoding="utf-8",
    )

    definition = load_phase_workflow_definition(tmp_path, "contract")
    phase = definition.phases[0]

    assert phase.required_skills == ("ddd-architecture", "clean-architecture")
    assert phase.requirements == ("domain-modeled", "boundaries-mapped")
    assert phase.artifacts == ("artifacts/design.md", "artifacts/decisions.json")
    assert phase.artifact == "artifacts/design.md"
    assert phase.routes == {"success": "done", "failure": "architecture"}


def test_every_declared_code_phase_has_requirements_and_failure_route() -> None:
    for workflow_path in sorted((KIT_ROOT / "workflows").glob("*.yaml")):
        definition = load_phase_workflow_definition(KIT_ROOT, workflow_path.stem)
        for phase in definition.phases:
            if phase.id not in CODE_SKILL_PHASES:
                continue
            assert phase.requirements, (workflow_path.name, phase.id)
            assert phase.routes is not None, (workflow_path.name, phase.id)
            assert "failure" in phase.routes, (workflow_path.name, phase.id)


def test_python_and_node_phase_contract_accept_success_and_route_failure() -> None:
    phase = Phase(
        id="architecture",
        description="",
        required_skills=("ddd-architecture", "clean-architecture"),
        requirements=("domain-modeled", "boundaries-mapped"),
        routes={"success": "done", "failure": "architecture"},
    )
    success = _artifact(
        skills=["clean-architecture", "ddd-architecture"],
        requirements={"boundaries-mapped": "pass", "domain-modeled": "pass"},
    )
    failure = _artifact(
        skills=["clean-architecture", "ddd-architecture"],
        requirements={"boundaries-mapped": "fail", "domain-modeled": "pass"},
    )

    assert phase_contract_issues(phase, success) == []
    assert phase_contract_route_key(phase, success) == "success"
    assert _node_evaluation(phase, success) == {"valid": True, "issues": [], "route": "success"}
    assert phase_contract_issues(phase, failure) == []
    assert phase_contract_route_key(phase, failure) == "failure"
    assert _node_evaluation(phase, failure) == {"valid": True, "issues": [], "route": "failure"}


def test_missing_required_skill_and_invalid_artifact_block_in_both_runtimes() -> None:
    phase = Phase(
        id="architecture",
        description="",
        required_skills=("ddd-architecture", "clean-architecture"),
        requirements=("domain-modeled",),
    )
    missing = _artifact(
        skills=["ddd-architecture"],
        requirements={"domain-modeled": "pass"},
    )
    invalid = "phase-contract: not-json\n"

    assert phase_contract_issues(phase, missing) == [
        "phase-contract missing required skills: clean-architecture"
    ]
    assert _node_evaluation(phase, missing) == {
        "valid": False,
        "issues": ["phase-contract missing required skills: clean-architecture"],
        "route": None,
    }
    assert phase_contract_issues(phase, invalid) == ["phase-contract payload is invalid"]
    assert _node_evaluation(phase, invalid) == {
        "valid": False,
        "issues": ["phase-contract payload is invalid"],
        "route": None,
    }



def test_phase_contract_accepts_declared_compatible_applied_skill_alias_in_both_runtimes() -> None:
    phase = Phase(
        id="architecture",
        description="",
        required_skills=("clean-architecture-core",),
        requirements=("domain-modeled",),
    )
    phase.skill_compatibility = {
        "skills": [
            {
                "canonical": "clean-architecture-core",
                "aliases": ["clean-architecture-boundaries"],
                "capabilities": ["architecture.clean.boundary"],
            }
        ]
    }
    artifact = _artifact(
        skills=["clean-architecture-boundaries"],
        requirements={"domain-modeled": "pass"},
    )

    assert phase_contract_issues(phase, artifact) == []
    assert _node_evaluation(phase, artifact) == {
        "valid": True,
        "issues": [],
        "route": "success",
    }


def test_invalid_phase_compatibility_metadata_is_rejected_identically_in_both_runtimes() -> None:
    phase = Phase(
        id="architecture",
        description="",
        required_skills=("clean-architecture-core",),
    )
    phase.skill_compatibility = {
        "skills": [
            {
                "canonical": "clean-architecture-core",
                "aliases": ["legacy-clean", "LEGACY-CLEAN"],
            }
        ]
    }
    artifact = _artifact(skills=["clean-architecture-core"], requirements={})
    expected = ["phase-contract skill compatibility is invalid"]

    assert phase_contract_issues(phase, artifact) == expected
    assert _node_evaluation(phase, artifact) == {
        "valid": False,
        "issues": expected,
        "route": None,
    }


def test_unresolved_phase_skills_report_all_structured_diagnostics_in_both_runtimes() -> None:
    phase = Phase(
        id="architecture",
        description="",
        required_skills=("removed-skill", "deprecated-skill"),
    )
    phase.skill_compatibility = {
        "skills": [
            {
                "canonical": "removed-skill",
                "status": "removed",
                "capabilities": ["obsolete.capability"],
            },
            {
                "canonical": "deprecated-skill",
                "status": "deprecated",
                "capabilities": ["legacy.capability"],
            },
        ]
    }
    artifact = _artifact(
        skills=["removed-skill", "deprecated-skill"],
        requirements={},
    )

    python_issues = phase_contract_issues(phase, artifact)
    node = _node_evaluation(phase, artifact)

    assert len(python_issues) == 1
    assert node["valid"] is False
    assert node["route"] is None
    node_issues = node["issues"]
    assert isinstance(node_issues, list)
    assert len(node_issues) == 1
    python_diagnostics = json.loads(python_issues[0].split(": ", 1)[1])
    node_diagnostics = json.loads(str(node_issues[0]).split(": ", 1)[1])
    assert python_diagnostics == node_diagnostics
    assert [diagnostic["reason"] for diagnostic in python_diagnostics] == [
        "removed_without_replacement",
        "deprecated_without_replacement",
    ]

def test_runner_blocks_missing_skill_and_repeats_declared_phase_on_requirement_failure(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    run_dir = project / ".agent-flow" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text("{}", encoding="utf-8")
    phase = Phase(
        id="architecture",
        description="",
        required_skills=("ddd-architecture", "clean-architecture"),
        requirements=("domain-modeled",),
        routes={"success": "done", "failure": "architecture"},
        artifact="architecture.md",
        artifacts=("architecture.md", "architecture-evidence.json"),
    )
    runner = Runner(project_root=project, run_dir=run_dir)
    runner.phases = [phase, Phase(id="done", description="")]
    runner._adapter_name = "codex"
    artifact = run_dir / "architecture.md"
    evidence = run_dir / "architecture-evidence.json"
    evidence.write_text("{}\n", encoding="utf-8")
    artifact.write_text(
        _artifact(
            skills=["ddd-architecture"],
            requirements={"domain-modeled": "pass"},
        ),
        encoding="utf-8",
    )

    assert runner._missing_required_markers(phase) == [
        "phase-contract missing required skills: clean-architecture"
    ]

    artifact.write_text(
        _artifact(
            skills=["ddd-architecture", "clean-architecture"],
            requirements={"domain-modeled": "fail"},
        ),
        encoding="utf-8",
    )
    assert runner._next_index(0, phase) == (0, False)
    assert not artifact.exists()
    assert not evidence.exists()


def test_runner_contract_expands_runtime_skill_dependency_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    index = {
        "selection": {},
        "skills": [
            {
                "name": "ddd-architecture",
                "path": ".agent-flow/skills/ddd-architecture/SKILL.md",
                "requires": ["clean-architecture-core"],
            },
            {
                "name": "clean-architecture-core",
                "path": ".agent-flow/skills/clean-architecture-core/SKILL.md",
            },
        ],
    }
    run_dir = project / ".agent-flow" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text('{"task":""}\n', encoding="utf-8")
    monkeypatch.setattr("agent_flow.core.phase_contract.runtime_changed_files", lambda *_args: ())
    monkeypatch.setattr(
        "agent_flow.core.phase_contract.authenticated_installed_skill_index",
        lambda _root: index,
    )
    runner = Runner.__new__(Runner)
    runner.config_root = project
    runner.project_root = project
    runner.run_dir = run_dir

    resolved = runner._runtime_contract_phase(
        Phase(
            id="ddd-design",
            description="",
            required_skills=("ddd-architecture",),
            requirements=("domain-model",),
        )
    )

    assert resolved.required_skills == (
        "clean-architecture-core",
        "ddd-architecture",
    )
    assert phase_contract_issues(
        resolved,
        _artifact(
            skills=["ddd-architecture"],
            requirements={"domain-model": "pass"},
        ),
    ) == ["phase-contract missing required skills: clean-architecture-core"]


def test_code_phase_without_static_skills_enforces_runtime_platform_skill_union(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    index: dict[str, object]
    routing = json.loads(
        (KIT_ROOT / "skills" / "profile-routing.json").read_text(encoding="utf-8")
    )
    index = {
        "selection": {
            "profiles": ["node"],
            "skill_profiles": ["node"],
            "explicit_skills": [],
            "required_review": {
                "node": ["typescript-development-guide"],
            },
            "profile_routing": routing,
        },
        "skills": [
            {
                "name": "code-generation-discipline",
                "path": "code-generation-discipline/SKILL.md",
            },
            {
                "name": "typescript-development-guide",
                "path": "typescript-development-guide/SKILL.md",
            },
        ],
    }
    run_dir = project / ".agent-flow" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text('{"task":""}\n', encoding="utf-8")
    monkeypatch.setattr(
        "agent_flow.core.phase_contract.runtime_changed_files",
        lambda *_args: ("src/index.ts",),
    )
    monkeypatch.setattr(
        "agent_flow.core.phase_contract.authenticated_installed_skill_index",
        lambda _root: index,
    )
    runner = Runner.__new__(Runner)
    runner.config_root = project
    runner.project_root = project
    runner.run_dir = run_dir

    resolved = runner._runtime_contract_phase(
        Phase(id="implement", description="")
    )

    assert resolved.required_skills == (
        "code-generation-discipline",
        "typescript-development-guide",
    )
    assert phase_contract_issues(
        resolved,
        _artifact(
            skills=["code-generation-discipline"],
            requirements={},
        ),
    ) == ["phase-contract missing required skills: typescript-development-guide"]


def test_secondary_declared_artifact_must_be_fresh_for_current_phase(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "meta.json").write_text(
        '{"phase_entered_at":"2026-07-15T00:00:00+00:00"}\n',
        encoding="utf-8",
    )
    (run_dir / "primary.md").write_text("complete\n", encoding="utf-8")
    evidence = run_dir / "evidence.json"
    evidence.write_text("{}\n", encoding="utf-8")
    os.utime(evidence, (1, 1))
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.config_root = tmp_path
    runner._adapter_name = "codex"
    phase = Phase(
        id="evidence",
        description="",
        artifact="primary.md",
        artifacts=("primary.md", "evidence.json"),
    )

    assert runner._missing_required_markers(phase) == [
        "stale declared artifact evidence.json"
    ]


def test_python_status_enforces_the_same_phase_contract_as_continue(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agent_flow.artifact import ActiveRun, write_meta

    project = tmp_path / "project"
    run_dir = project / ".agent-flow" / "runs" / "r1"
    artifact = run_dir / "implement.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("implementation complete\n", encoding="utf-8")
    meta = {
        "workflow": "development",
        "task": "demo",
        "current_phase": "implement",
        "started_at": "2020-01-01T00:00:00+00:00",
        "phase_entered_at": "2020-01-01T00:00:00+00:00",
    }
    write_meta(run_dir, meta)

    ActiveRun(
        path=run_dir,
        run_id="r1",
        workflow="development",
        task="demo",
        started_at=meta["started_at"],
    ).print_status(config_root=project)

    payload_line = next(
        line for line in capsys.readouterr().out.splitlines() if line.startswith("status_json: ")
    )
    payload = json.loads(payload_line.removeprefix("status_json: "))
    assert payload["status"] == "blocked"
    assert payload["reason"] == "missing_completion_markers"


def test_python_status_blocks_stale_secondary_declared_artifact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agent_flow.artifact import ActiveRun, write_meta

    project = tmp_path / "project"
    workflows = project / ".agent-flow" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "contract.yaml").write_text(
        """
id: contract
phases:
  - id: evidence
    artifacts: [primary.md, evidence.json]
""".lstrip(),
        encoding="utf-8",
    )
    run_dir = project / ".agent-flow" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "primary.md").write_text("complete\n", encoding="utf-8")
    evidence = run_dir / "evidence.json"
    evidence.write_text("{}\n", encoding="utf-8")
    os.utime(evidence, (1, 1))
    meta = {
        "workflow": "contract",
        "task": "demo",
        "current_phase": "evidence",
        "started_at": "2020-01-01T00:00:00+00:00",
        "phase_entered_at": "2020-01-01T00:00:00+00:00",
    }
    write_meta(run_dir, meta)

    ActiveRun(
        path=run_dir,
        run_id="r1",
        workflow="contract",
        task="demo",
        started_at=meta["started_at"],
    ).print_status(config_root=project)

    payload_line = next(
        line for line in capsys.readouterr().out.splitlines() if line.startswith("status_json: ")
    )
    payload = json.loads(payload_line.removeprefix("status_json: "))
    assert payload["status"] == "blocked"
    assert payload["reason"] == "missing_completion_markers"
