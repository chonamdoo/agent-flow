from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_flow.adapters.generic import GenericAdapter
from agent_flow.core import phase_workflow
from agent_flow.core.local_skills import (
    applicable_code_review_skill_docs,
    applicable_project_local_skill_docs,
    local_skill_prompt_block,
    missing_local_skill_markers,
    project_local_skill_plan_hash,
)
from agent_flow.core.phase_workflow import load_phase_workflow_definition
from agent_flow.core.profiles import active_profile_ids, load_profile_payload
from agent_flow.core.skill_plan import (
    SkillPlanSnapshotError,
    _changed_files,
    _configured_base_commit,
    hash_skill_tree,
    profile_skill_prompt_block,
    resolve_runtime_skill_plan,
)
from agent_flow.runner import (
    Phase,
    Runner,
    _gates_route_key,
    _multi_review_route_key,
    _route_key,
    _run_base_snapshot,
)


def test_changed_file_query_failure_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    original_run = subprocess.run

    def fail_diff(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(args, 2, "", "forced diff failure")
        return original_run(args, **kwargs)

    monkeypatch.setattr("agent_flow.core.skill_plan.subprocess.run", fail_diff)

    with pytest.raises(SkillPlanSnapshotError, match="changed-file query failed: git diff"):
        _changed_files(tmp_path)


@pytest.mark.parametrize(
    "phase_yaml, message",
    (
        ("  - id: review\n    multi_review: true\n", "multi_review"),
        ("  - id: gates\n", "gates"),
    ),
)
def test_workflow_loader_requires_routes_for_routing_phases(
    tmp_path: Path,
    phase_yaml: str,
    message: str,
) -> None:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "unsafe.yaml").write_text(
        "id: unsafe\nphases:\n" + phase_yaml,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        load_phase_workflow_definition(tmp_path, "unsafe")


@pytest.mark.parametrize(
    "phase",
    (
        Phase(id="review", description="", multi_review=True),
        Phase(id="gates", description=""),
    ),
)
def test_runner_does_not_advance_required_routing_phase_without_routes(
    tmp_path: Path,
    phase: Phase,
) -> None:
    runner = Runner.__new__(Runner)
    runner.run_dir = tmp_path
    runner.phases = [phase]

    assert runner._next_index(0, phase) == (0, True)


@pytest.mark.parametrize("overall_field", ("verdict", "status"))
def test_architecture_review_blocked_routes_to_refactor(
    tmp_path: Path,
    overall_field: str,
) -> None:
    artifact = tmp_path / "architecture-review.md"
    artifact.write_text(
        "## Reviewer A\nreviewer-source: sub-agent\nverdict: approve\n\n"
        "## Reviewer B\nreviewer-source: sub-agent\nverdict: approve\n\n"
        f"## Overall\n{overall_field}: blocked\n",
        encoding="utf-8",
    )
    refactor = Phase(id="refactor", description="")
    phase = Phase(
        id="architecture-review",
        description="",
        multi_review=True,
        routes={"approve": "gates", "request-changes": "refactor", "blocked": "refactor"},
    )
    gates = Phase(id="gates", description="", routes={"default": "block"})
    runner = Runner.__new__(Runner)
    runner.run_dir = tmp_path
    runner.phases = [refactor, phase, gates]

    assert runner._next_index(1, phase) == (0, False)


@pytest.mark.parametrize("route_line", ("verdict: blocked", "status: blocked"))
def test_architecture_review_top_level_blocked_route_does_not_fall_through(
    route_line: str,
) -> None:
    assert _multi_review_route_key(route_line + "\n", "architecture-review") == "blocked"


def test_route_parsers_reject_multiple_or_contradictory_fields() -> None:
    assert _route_key("status: green\nverdict: approve\n") == "invalid-route"
    assert _route_key("status: green\nstatus: comments\n", "pr-watch") == "invalid-route"
    assert _route_key("status: approve\n", "plan-review") == "invalid-route"
    assert _route_key("status: ???\nverdict: approve\n", "plan-review") == "invalid-route"
    assert _route_key("status: ???\nstatus: green\n", "pr-watch") == "green"
    assert _gates_route_key('{"passed":true,"passed":false}') == "invalid-route"
    assert _gates_route_key('{"passed":true,"results":[{"id":1,"id":2}]}') == "invalid-route"
    assert _gates_route_key('{"passed":true,"status":"blocked"}') == "invalid-route"
    assert _gates_route_key('{"passed":false,"status":"green"}') == "invalid-route"


@pytest.mark.parametrize(
    "artifact",
    (
        (
            "## Reviewer A\n"
            "reviewer-source: sub-agent\n"
            "verdict: approve\n"
            "verdict: approve\n"
        ),
        (
            "reviewer-a reviewer-source: sub-agent\n"
            "reviewer-a verdict: approve\n"
            "reviewer-a verdict: request-changes\n"
        ),
    ),
)
def test_multi_review_rejects_any_second_verdict_for_one_reviewer(
    artifact: str,
) -> None:
    with pytest.raises(RuntimeError, match="reviewer a has multiple verdict lines"):
        _multi_review_route_key(artifact + "\n## Overall\nverdict: approve\n")


def test_find_kit_root_prefers_the_pinned_install_over_a_host_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = tmp_path / "host"
    installed = host / ".agent-flow"
    module_path = installed / "runtime" / "python" / "agent_flow" / "core" / "phase_workflow.py"
    module_path.parent.mkdir(parents=True)
    module_path.write_text("# installed runtime\n", encoding="utf-8")
    for root in (host, installed):
        (root / "workflows").mkdir(parents=True)
        (root / "profiles").mkdir()
        (root / "workflows" / "probe.yaml").write_text(
            "id: probe\n"
            "phases:\n"
            "  - id: implement\n"
            f"    description: {'pinned' if root == installed else 'host'}\n",
            encoding="utf-8",
        )
    (host / "package.json").write_text("{}\n", encoding="utf-8")
    (host / "pyproject.toml").write_text("[project]\nname='host'\n", encoding="utf-8")

    monkeypatch.setattr(phase_workflow, "__file__", str(module_path))

    selected = phase_workflow.find_kit_root()
    exported = load_phase_workflow_definition(selected, "probe")

    assert selected == installed
    assert exported.phases[0].description == "pinned"
    assert Path(exported.source).parent == installed / "workflows"


def _write_skill(path: Path, name: str, *, activation: str | None = None) -> None:
    path.mkdir(parents=True)
    activation_line = f"activation: {activation}\n" if activation is not None else ""
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: local policy\n{activation_line}---\n\n# {name}\n",
        encoding="utf-8",
    )


def _write_routed_skill_index(tmp_path: Path) -> None:
    skills = [
        {
            "name": "legacy-policy",
            "source": "local",
            "path": ".agent-flow/local-skills/legacy-policy/SKILL.md",
        },
        {
            "name": "term-policy",
            "source": "local",
            "path": ".agent-flow/local-skills/term-policy/SKILL.md",
            "activation": "conditional",
            "workflowPhases": ["implement"],
            "taskTerms": ["deploy plan", "Straße"],
            "pathGlobs": [],
        },
        {
            "name": "path-policy",
            "source": "local",
            "path": ".agent-flow/local-skills/path-policy/SKILL.md",
            "activation": "conditional",
            "workflowPhases": ["implement"],
            "taskTerms": [],
            "pathGlobs": ["packages/**/*.widget"],
        },
        {
            "name": "review-policy",
            "source": "local",
            "path": ".agent-flow/local-skills/review-policy/SKILL.md",
            "activation": "conditional",
            "workflowPhases": ["review"],
            "taskTerms": ["deploy plan"],
            "pathGlobs": [],
        },
    ]
    for skill in skills:
        _write_skill(
            tmp_path / ".agent-flow" / "local-skills" / str(skill["name"]),
            str(skill["name"]),
        )
    index = tmp_path / ".agent-flow" / "skills" / "index.json"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({"skills": skills}), encoding="utf-8")


def test_project_local_skill_activation_is_deterministic_for_task_phase_and_path(
    tmp_path: Path,
) -> None:
    _write_routed_skill_index(tmp_path)

    task_docs = applicable_code_review_skill_docs(
        tmp_path,
        "implement",
        "execute the DEPLOY PLAN now",
        (),
    )
    path_docs = applicable_code_review_skill_docs(
        tmp_path,
        "implement",
        "unrelated task",
        ("packages/ui/Card.widget",),
    )
    review_docs = applicable_code_review_skill_docs(
        tmp_path,
        "review",
        "execute the deploy plan now",
        ("packages/ui/Card.widget",),
    )
    partial_word_docs = applicable_code_review_skill_docs(
        tmp_path,
        "implement",
        "redeploy planner",
        (),
    )
    unicode_docs = applicable_code_review_skill_docs(
        tmp_path,
        "implement",
        "STRASSE migration",
        (),
    )

    assert [doc.name for doc in task_docs] == ["term-policy"]
    assert [doc.name for doc in path_docs] == ["path-policy"]
    assert [doc.name for doc in review_docs] == ["review-policy"]
    assert [doc.name for doc in partial_word_docs] == []
    assert [doc.name for doc in unicode_docs] == ["term-policy"]


def test_project_local_skill_prompt_and_marker_gate_share_activation_scope(
    tmp_path: Path,
) -> None:
    _write_routed_skill_index(tmp_path)
    prompt = local_skill_prompt_block(
        tmp_path,
        "implement",
        "execute deploy plan",
        (),
    )
    incomplete = (
        "## Completion Gate\n"
        "project-local-skills: checked\n"
        "project-local-skills-used: legacy-policy\n"
        "project-local-skill-docs: applied\n"
    )
    complete = incomplete.replace(
        "project-local-skills-used: legacy-policy",
        "project-local-skills-used: term-policy",
    )

    assert "project-local-skills-used: term-policy" in prompt
    assert "legacy-policy/SKILL.md" not in prompt
    assert "path-policy/SKILL.md" not in prompt
    assert missing_local_skill_markers(
        incomplete,
        tmp_path,
        "implement",
        "execute deploy plan",
        (),
    ) == ["project-local-skills-used: <applicable local skill list>"]
    assert missing_local_skill_markers(
        complete,
        tmp_path,
        "implement",
        "execute deploy plan",
        (),
    ) == []


def test_project_local_skill_routes_to_matching_non_code_phase_without_markers(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path / ".agent-flow" / "local-skills" / "figma-screen-spec",
        "figma-screen-spec",
    )
    index = tmp_path / ".agent-flow" / "skills" / "index.json"
    index.parent.mkdir(parents=True)
    index.write_text(
        json.dumps(
            {
                "skills": [
                    {
                        "name": "figma-screen-spec",
                        "source": "local",
                        "path": ".agent-flow/local-skills/figma-screen-spec/SKILL.md",
                        "activation": "conditional",
                        "workflowPhases": ["domain-grill"],
                        "taskTerms": ["figma"],
                        "pathGlobs": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    docs = applicable_project_local_skill_docs(
        tmp_path,
        "domain-grill",
        "read the Figma screen before grilling",
    )
    prompt = local_skill_prompt_block(
        tmp_path,
        "domain-grill",
        "read the Figma screen before grilling",
    )

    assert [doc.name for doc in docs] == ["figma-screen-spec"]
    assert "figma-screen-spec/SKILL.md" in prompt
    assert "mandatory policy for this phase" in prompt
    assert "does not add local-skill completion markers" in prompt
    assert "project-local-skills: checked" not in prompt
    assert missing_local_skill_markers(
        "domain artifact without a completion gate",
        tmp_path,
        "domain-grill",
        "read the Figma screen before grilling",
    ) == []


@pytest.mark.parametrize(
    "metadata, message",
    (
        ({"activation": "conditional", "taskTerms": [], "pathGlobs": []}, "no selectors"),
        (
            {
                "activation": "conditional",
                "taskTerms": [],
                "pathGlobs": ["../outside/**"],
            },
            "pathGlobs",
        ),
    ),
)
def test_project_local_skill_activation_rejects_invalid_conditional_metadata(
    tmp_path: Path,
    metadata: dict[str, object],
    message: str,
) -> None:
    _write_skill(tmp_path / ".agent-flow" / "local-skills" / "invalid-policy", "invalid-policy")
    index = tmp_path / ".agent-flow" / "skills" / "index.json"
    index.parent.mkdir(parents=True)
    index.write_text(
        json.dumps(
            {
                "skills": [
                    {
                        "name": "invalid-policy",
                        "source": "local",
                        "path": ".agent-flow/local-skills/invalid-policy/SKILL.md",
                        **metadata,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match=message):
        applicable_code_review_skill_docs(tmp_path, "implement", "task", ())


def test_project_local_skill_discovery_unions_index_and_both_safe_trees(
    tmp_path: Path,
) -> None:
    indexed = tmp_path / ".agent-flow" / "local-skills" / "indexed"
    unindexed = tmp_path / ".agent-flow" / "local-skills" / "unindexed"
    project = tmp_path / "skills" / "project-policy"
    for path, name in (
        (indexed, "indexed"),
        (unindexed, "unindexed"),
        (project, "project-policy"),
    ):
        _write_skill(path, name, activation="always")
    index_path = tmp_path / ".agent-flow" / "skills" / "index.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text(
        json.dumps(
            {
                "skills": [
                        {
                            "name": "indexed",
                            "source": "local",
                            "path": ".agent-flow/local-skills/indexed/SKILL.md",
                            "activation": "always",
                        }
                ]
            }
        ),
        encoding="utf-8",
    )

    docs = applicable_code_review_skill_docs(tmp_path, "implement")

    assert [doc.name for doc in docs] == ["indexed", "project-policy", "unindexed"]


def test_unindexed_conditional_frontmatter_uses_task_phase_and_path_selectors(
    tmp_path: Path,
) -> None:
    skill = tmp_path / ".agent-flow" / "local-skills" / "unindexed-conditional"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: unindexed-conditional\n"
        "activation: conditional\n"
        "workflowPhases: [implement]\n"
        "taskTerms: [deploy plan, Straße]\n"
        "pathGlobs: [packages/**/*.widget]\n"
        "---\n\n# Policy\n",
        encoding="utf-8",
    )

    matching_task = applicable_code_review_skill_docs(
        tmp_path,
        "implement",
        "execute the DEPLOY PLAN",
    )
    matching_path = applicable_code_review_skill_docs(
        tmp_path,
        "implement",
        "unrelated",
        ("packages/ui/Card.widget",),
    )

    assert [doc.name for doc in matching_task] == ["unindexed-conditional"]
    assert [doc.name for doc in matching_path] == ["unindexed-conditional"]
    assert applicable_code_review_skill_docs(tmp_path, "review", "deploy plan") == ()
    assert applicable_code_review_skill_docs(tmp_path, "implement", "redeploy planner") == ()
    assert len(project_local_skill_plan_hash(tmp_path)) == 64


@pytest.mark.parametrize(
    "metadata, message",
    (
        ("workflowPhases: implement\ntaskTerms: []\npathGlobs: []", "workflowPhases"),
        ("taskTerms: deploy\npathGlobs: []", "taskTerms"),
        ("taskTerms: []\npathGlobs: packages/**", "pathGlobs"),
    ),
)
def test_unindexed_routing_fields_without_activation_still_fail_closed(
    tmp_path: Path,
    metadata: str,
    message: str,
) -> None:
    skill = tmp_path / ".agent-flow" / "local-skills" / "invalid-routing"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: invalid-routing\n{metadata}\n---\n\n# Policy\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match=message):
        applicable_code_review_skill_docs(tmp_path, "implement")


def test_project_local_skill_discovery_rejects_symlinked_tree(
    tmp_path: Path,
) -> None:
    skill = tmp_path / "skills" / "unsafe"
    _write_skill(skill, "unsafe")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (skill / "reference.txt").symlink_to(outside)

    with pytest.raises(RuntimeError, match="may not contain symlinks"):
        applicable_code_review_skill_docs(tmp_path, "implement")


@pytest.mark.parametrize("source_root", ("agent-flow", "project"))
def test_project_local_skill_discovery_rejects_symlinked_source_ancestors(
    tmp_path: Path,
    source_root: str,
) -> None:
    if source_root == "agent-flow":
        outside = tmp_path / "outside-agent-flow"
        _write_skill(outside / "local-skills" / "unsafe", "unsafe")
        (tmp_path / ".agent-flow").symlink_to(outside, target_is_directory=True)
    else:
        outside = tmp_path / "outside-project-skills"
        _write_skill(outside / "unsafe", "unsafe")
        (tmp_path / "skills").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="real directory"):
        applicable_code_review_skill_docs(tmp_path, "implement")


def test_project_local_skill_discovery_skips_kit_source_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_skill(tmp_path / "skills" / "bundled-source", "bundled-source")
    monkeypatch.setattr("agent_flow.core.local_skills.find_kit_root", lambda: tmp_path)

    assert applicable_code_review_skill_docs(tmp_path, "implement") == ()


def test_project_local_skill_discovery_rejects_overlong_unindexed_skill(
    tmp_path: Path,
) -> None:
    skill = tmp_path / ".agent-flow" / "local-skills" / "overlong-policy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "\n".join(f"line {index}" for index in range(208)),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="208 lines; max is 200"):
        applicable_code_review_skill_docs(tmp_path, "implement")


def test_project_local_skill_discovery_uses_safe_frontmatter_name(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / ".agent-flow" / "local-skills" / "directory-alias"
    canonical.mkdir(parents=True)
    (canonical / "SKILL.md").write_text(
        "---\nname: canonical-policy\ndescription: policy\nactivation: always\n---\n\n# Policy\n",
        encoding="utf-8",
    )
    sanitized = tmp_path / ".agent-flow" / "local-skills" / "unsafe-alias"
    sanitized.mkdir(parents=True)
    (sanitized / "SKILL.md").write_text(
        "---\nname: ../Bad Policy\ndescription: policy\nactivation: always\n---\n\n# Policy\n",
        encoding="utf-8",
    )

    docs = applicable_code_review_skill_docs(tmp_path, "implement")

    assert [doc.name for doc in docs] == ["bad-policy", "canonical-policy"]


def test_project_local_skill_discovery_prefers_local_and_rejects_equal_priority_conflicts(
    tmp_path: Path,
) -> None:
    local = tmp_path / ".agent-flow" / "local-skills" / "local-alias"
    project = tmp_path / "skills" / "project-alias"
    for skill in (local, project):
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: canonical-policy\ndescription: policy\nactivation: always\n---\n\n# Policy\n",
            encoding="utf-8",
        )

    docs = applicable_code_review_skill_docs(tmp_path, "implement")

    assert [doc.path for doc in docs] == [
        ".agent-flow/local-skills/local-alias/SKILL.md"
    ]

    second_local = tmp_path / ".agent-flow" / "local-skills" / "second-alias"
    second_local.mkdir()
    (second_local / "SKILL.md").write_text(
        "---\nname: Canonical-Policy\ndescription: policy\nactivation: always\n---\n\n# Policy\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="conflicting project-local skill paths"):
        applicable_code_review_skill_docs(tmp_path, "implement")


def test_project_local_skill_discovery_rejects_corrupt_existing_index(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path / ".agent-flow" / "local-skills" / "legacy-policy", "legacy-policy")
    index = tmp_path / ".agent-flow" / "skills" / "index.json"
    index.parent.mkdir(parents=True)
    index.write_text("{not-json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unreadable project-local skill index"):
        applicable_code_review_skill_docs(tmp_path, "implement")


def test_local_skill_prompt_lists_required_markers_outside_fence(tmp_path: Path) -> None:
    _write_skill(
        tmp_path / "skills" / "local-policy",
        "local-policy",
        activation="always",
    )

    prompt = local_skill_prompt_block(tmp_path, "implement")

    assert "```text" not in prompt
    assert "\nproject-local-skills: checked\n" in prompt
    assert "\nproject-local-skills-used: local-policy\n" in prompt
    assert "\nproject-local-skill-docs: applied\n" in prompt


@pytest.mark.parametrize(
    "workspace_rel",
    ("leader/.agent-flow/worktrees/internal", "external-worktree"),
)
def test_profile_skill_prompt_uses_verified_leader_snapshot_from_internal_and_external_worktree(
    tmp_path: Path,
    workspace_rel: str,
) -> None:
    leader = tmp_path / "leader"
    workspace = tmp_path / workspace_rel
    workspace.mkdir(parents=True)
    skill_dir = leader / ".agent-flow" / "skills" / "code-generation-discipline"
    _write_skill(skill_dir, "code-generation-discipline")
    index = leader / ".agent-flow" / "skills" / "index.json"
    index.write_text(
        json.dumps(
            {
                "selection": {
                    "profiles": ["generic"],
                    "skill_profiles": ["generic"],
                    "required_review": {"generic": []},
                    "conditional_skills": {},
                    "profile_routing": {},
                },
                "skills": [
                    {
                        "name": "code-generation-discipline",
                        "path": ".agent-flow/skills/code-generation-discipline/SKILL.md",
                        "source": "bundled",
                        "tree_hash": hash_skill_tree(skill_dir),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    prompt = profile_skill_prompt_block(
        leader,
        "implement",
        workspace,
        "generic task",
    )

    assert ".agent-flow/skills/code-generation-discipline/SKILL.md" in prompt
    assert f"`{skill_dir / 'SKILL.md'}`" in prompt
    assert (skill_dir / "SKILL.md").is_file()
    from agent_flow.adapters.hosted import HostedAdapter

    for host in ("claude", "codex", "omp"):
        adapter = HostedAdapter(host)
        adapter._config_root = leader
        adapter._task_scope = "generic task"
        hosted_prompt = adapter.render_envelope(
            Phase(id="implement", description=""),
            tmp_path / "runs" / host,
            workspace,
        )
        assert f"`{skill_dir / 'SKILL.md'}`" in hosted_prompt
    (skill_dir / "SKILL.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(SkillPlanSnapshotError, match="snapshot changed"):
        profile_skill_prompt_block(
            leader,
            "implement",
            workspace,
            "generic task",
        )


def test_empty_skill_profiles_is_fail_closed_like_node() -> None:
    base_selection = {
        "profiles": ["python"],
        "required_review": {"python": []},
    }
    skills = [{"name": "code-generation-discipline", "path": "skill", "tree_hash": "hash"}]

    empty = resolve_runtime_skill_plan(
        {"selection": {**base_selection, "skill_profiles": []}, "skills": skills},
        "implement",
    )
    absent = resolve_runtime_skill_plan(
        {"selection": base_selection, "skills": skills},
        "implement",
    )

    assert empty["missing_profiles"] == ["python"]
    assert absent["missing_profiles"] == []


def test_profile_loaders_reject_unsafe_kit_and_direct_names(tmp_path: Path) -> None:
    kit = tmp_path / ".agent-flow"
    kit.mkdir()
    (kit / "kit.json").write_text('{"profile":"../python"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="invalid profile name"):
        active_profile_ids(tmp_path)
    with pytest.raises(ValueError, match="invalid profile name"):
        load_profile_payload("../python")


@pytest.mark.parametrize("payload", ("{invalid", "[]\n"))
def test_active_profile_ids_does_not_treat_corrupt_kit_as_uninstalled(
    tmp_path: Path,
    payload: str,
) -> None:
    agent_flow = tmp_path / ".agent-flow"
    agent_flow.mkdir()
    (agent_flow / "kit.json").write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match="installed kit metadata"):
        active_profile_ids(tmp_path)


def test_configured_base_rejects_unsafe_profile_and_git_option_ref(
    tmp_path: Path,
) -> None:
    agent_flow = tmp_path / ".agent-flow"
    profiles = agent_flow / "profiles"
    profiles.mkdir(parents=True)
    kit_path = agent_flow / "kit.json"
    kit_path.write_text('{"profile":"../python"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid profile name"):
        _configured_base_commit(tmp_path, tmp_path)

    kit_path.write_text('{"profile":"python"}\n', encoding="utf-8")
    (profiles / "python.yaml").write_text(
        "id: python\nbranching:\n  base: --upload-pack=malicious\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillPlanSnapshotError, match="invalid profile base ref"):
        _configured_base_commit(tmp_path, tmp_path)

    with pytest.raises(ValueError, match="invalid profile base ref"):
        _run_base_snapshot(
            {"branching": {"base": "--upload-pack=malicious"}},
            tmp_path,
        )


def test_generic_stub_success_artifact_still_requires_all_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_skill(tmp_path / "skills" / "local-policy", "local-policy")
    phase = Phase(
        id="implement",
        description="",
        required_markers=(
            "skills_checked: true",
            "active-profiles:",
            "project-local-skills: checked|n/a",
            "project-local-skills-used:",
        ),
    )
    adapter = GenericAdapter()
    adapter._config_root = tmp_path
    adapter._profile_id = "python"
    monkeypatch.setenv("AGENT_FLOW_GENERIC_MODE", "stub-success")

    assert adapter.execute(phase, tmp_path, tmp_path) is True
    runner = Runner.__new__(Runner)
    runner.run_dir = tmp_path
    runner.config_root = tmp_path
    runner._adapter_name = "generic"
    assert runner._missing_required_markers(phase) == []

    artifact = tmp_path / "implement.md"
    artifact.write_text(
        artifact.read_text(encoding="utf-8").replace("skills_checked: true\n", ""),
        encoding="utf-8",
    )
    assert runner._missing_required_markers(phase) == ["skills_checked: true"]
