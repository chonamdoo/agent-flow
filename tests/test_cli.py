from __future__ import annotations

import contextlib
import hashlib
import functools
import io
import os
import site
import shlex
import shutil
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path
import subprocess
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from importlib import resources

from tests.test_hook_integrity import _install as _install_managed_hooks

from agent_flow.cli import main
from agent_flow.core.gates import GateCommand, run_gate
from agent_flow.core.design_ledger import capture_design_ledger
from agent_flow.core.phase_workflow import load_phase_workflow_definition
from agent_flow.core.hook_integrity import find_install_root
from agent_flow.core.kit_digest import kit_source_digest
from agent_flow.core.profiles import load_profile
from agent_flow.core.local_skills import (
    local_skill_prompt_block,
    missing_local_skill_markers,
    phase_skill_resolution,
    record_skill_read,
)
from agent_flow.core.skill_resolver import PhaseSkills
from agent_flow.core.review import _parse_verdict
from agent_flow.core.team import ShutdownSignal
from agent_flow.core.worktrees import (
    get_worktree_status,
    legacy_managed_root,
    managed_worktrees_root,
    plan_worktree,
    worktree_runtime_root,
)


os.environ.setdefault("AGENT_FLOW_SKIP_CODEX_TRUST", "1")


def _node_test_env(**overrides: str) -> dict[str, str]:
    env = {**os.environ, **overrides}
    python_paths = [
        env.get("PYTHONPATH"),
        site.getusersitepackages(),
    ]
    env["PYTHONPATH"] = os.pathsep.join(path for path in python_paths if path)
    return env


_SPEC_CONFIRM_ARTIFACT = """# design

## Spec Items

SPEC-1: Empty search results show the empty state.
verify: test:test_empty_search_results_show_the_empty_state

## Design Values

## Completion Gate

spec-items: SPEC-1
design-values: none
"""


def _strip_markdown_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    return text if end == -1 else text[end + len("\n---\n") :].lstrip("\n")




class CliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._previous_xdg_state_home = os.environ.get("XDG_STATE_HOME")
        cls._xdg_state_home = tempfile.TemporaryDirectory()
        os.environ["XDG_STATE_HOME"] = cls._xdg_state_home.name

    @classmethod
    def tearDownClass(cls) -> None:
        cls._xdg_state_home.cleanup()
        if cls._previous_xdg_state_home is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = cls._previous_xdg_state_home

    def test_init_creates_agent_flow_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            self.assertEqual(main(["init", "--root", str(root)]), 0)
            self.assertTrue((root / ".agent-flow" / "runs").is_dir())
            self.assertTrue((root / ".agent-flow" / "handoffs").is_dir())

    def test_cli_runs_from_outside_source_tree_with_packaged_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(__file__).resolve().parents[1]
            external_cwd = Path(temp_dir) / "outside"
            project_root = Path(temp_dir) / "project"
            install_target = Path(temp_dir) / "site"
            package_python = _python_for_package_install()
            external_cwd.mkdir()
            install = subprocess.run(
                (
                    package_python,
                    "-m",
                    "pip",
                    "install",
                    "--quiet",
                    "--target",
                    str(install_target),
                    str(repo_root),
                ),
                text=True,
                capture_output=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            init = subprocess.run(
                (
                    package_python,
                    "-m",
                    "agent_flow.cli",
                    "init",
                    "--root",
                    str(project_root),
                ),
                cwd=external_cwd,
                env={**os.environ, "PYTHONPATH": str(install_target)},
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(init.returncode, 0, init.stderr)
            _init_git_repo(project_root)
            result = subprocess.run(
                (
                    package_python,
                    "-m",
                    "agent_flow.cli",
                    "start",
                    "development",
                    "--root",
                    str(project_root),
                    "--task",
                    "demo",
                    "--run-id",
                    "r1",
                ),
                cwd=external_cwd,
                env={**os.environ, "PYTHONPATH": str(install_target)},
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state_root = worktree_runtime_root(
                root=project_root,
                name=plan_worktree(root=project_root, name="demo").name,
            )
            run_dir = state_root / ".agent-flow" / "runs" / "r1"
            self.assertTrue((project_root / ".agent-flow").is_dir())
            self.assertTrue((run_dir / "meta.json").is_file())

    def test_workflow_kit_resources_are_packaged(self) -> None:
        package_root = resources.files("agent_flow")
        self.assertTrue(package_root.joinpath("workflows", "development.yaml").is_file())
        self.assertTrue(package_root.joinpath("profiles", "generic.yaml").is_file())
        self.assertTrue(package_root.joinpath("roles", "default.yaml").is_file())

    def test_all_packaged_workflows_export_phase_artifacts(self) -> None:
        workflows_root = resources.files("agent_flow").joinpath("workflows")
        workflow_names = sorted(
            path.name.removesuffix(".yaml")
            for path in workflows_root.iterdir()
            if path.name.endswith(".yaml")
        )
        self.assertGreaterEqual(set(workflow_names), {"bugfix", "default", "development", "full-feature", "review"})

        for workflow_name in workflow_names:
            with self.subTest(workflow=workflow_name):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["workflow", "export", "--workflow", workflow_name]), 0)
                payload = json.loads(output.getvalue())
                self.assertEqual(payload["id"], workflow_name)
                phase_ids = [phase["id"] for phase in payload["phases"]]
                self.assertEqual(len(phase_ids), len(set(phase_ids)))
                self.assertGreater(len(phase_ids), 0)
                phase_set = set(phase_ids)
                for phase in payload["phases"]:
                    artifact = phase["artifact"]
                    self.assertFalse(Path(artifact).is_absolute(), artifact)
                    self.assertNotIn("..", Path(artifact).parts, artifact)
                    self.assertRegex(artifact, r"\.(md|json|log)$")
                    prompt = phase.get("prompt") or ""
                    output_paths = [
                        segment.strip().split()[0].strip("`.,")
                        for segment in prompt.replace("\n", " ").split("Output:")[1:]
                        if segment.strip()
                    ]
                    if output_paths:
                        self.assertIn(artifact, output_paths)
                    for target in (phase.get("routes") or {}).values():
                        if target != "block":
                            self.assertIn(target, phase_set)

    def test_full_feature_workflow_keeps_python_runner_routes(self) -> None:
        import yaml

        workflow_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "agent_flow"
            / "workflows"
            / "full-feature.yaml"
        )
        payload = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        phases = {phase["id"]: phase for phase in payload["phases"]}
        # Python runner가 verdict/status에 따라 재작업 phase로 되돌아가는지 고정한다.
        self.assertEqual(phases["plan-review"]["routes"]["request-changes"], "slice-plan")
        self.assertEqual(phases["gates"]["routes"]["request-changes"], "fix-loop")
        self.assertEqual(phases["gates"]["routes"]["green"], "commit")
        self.assertEqual(phases["gates"]["routes"]["approve"], "commit")
        self.assertEqual(phases["comment-authoring"]["routes"]["default"], "multi-review")
        self.assertIn("comment-authoring: applied", phases["comment-authoring"]["required_markers"])
        self.assertIn("Do not refactor", phases["comment-authoring"]["prompt"])
        self.assertEqual(phases["multi-review"]["routes"]["request-changes"], "fix-loop")
        self.assertTrue(phases["multi-review"]["multi_review"])
        self.assertIn("Reviewers are installed Claude and Codex CLIs only", phases["multi-review"]["prompt"])
        self.assertIn("Never use OMP or controller-session work", phases["multi-review"]["prompt"])
        self.assertIn("reviewer-source: sub-agent", phases["multi-review"]["prompt"])
        self.assertIn("## Overall", phases["multi-review"]["prompt"])
        self.assertIn("verdict: approve", phases["multi-review"]["prompt"])
        self.assertIn("verdict: request-changes", phases["multi-review"]["prompt"])
        self.assertEqual(phases["fix-loop"]["routes"]["default"], "comment-authoring")
        self.assertEqual(phases["architecture-review"]["routes"]["approve"], "gates")
        self.assertNotIn("blocked", phases["architecture-review"]["routes"])
        self.assertEqual(phases["pr-watch"]["routes"]["comments"], "pr-comment-fix")
        self.assertEqual(phases["pr-watch"]["routes"]["ci-failed"], "pr-ci-fix")
        self.assertEqual(phases["pr-comment-fix"]["routes"]["default"], "pr-watch")
        self.assertEqual(phases["pr-ci-fix"]["routes"]["default"], "pr-watch")
        self.assertEqual(phases["merge-approval"]["routes"]["default"], "block")
        self.assertIn("Output: artifacts/red.log.", phases["red"]["prompt"])
        self.assertIn("Output: artifacts/green.log.", phases["green"]["prompt"])
        self.assertIn("Output: artifacts/gate-results.json.", phases["gates"]["prompt"])

        default_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "agent_flow"
            / "workflows"
            / "default.yaml"
        )
        default_payload = yaml.safe_load(default_path.read_text(encoding="utf-8"))
        default_phases = {phase["id"]: phase for phase in default_payload["phases"]}
        self.assertEqual(
            default_phases["implement"]["required_markers"],
            [
                "skills_checked: true",
                "profile-skill-selection: applied",
                "active-profiles:",
                "changed-file-skill-resolution: applied",
                "required-profile-skills: checked",
                "missing-required-profile-skills:",
                "clean-architecture: applied|n/a",
                "project-local-skills: checked|n/a",
                "project-local-skills-used:",
                "presentation-skill: android|flutter|react|react-native|ios|n/a",
                "presentation-state-based-development: applied|n/a",
                "presentation-state-review: pass|fail|n/a",
                "ui-state-modeling: explicit|n/a",
                "presentation-mapping-boundary: domain-to-uimodel|n/a",
                "di-boundary: hilt|context-provider|tsyringe|swift-environment|factory|swift-dependencies|swinject|needle|riverpod|get-it|direct|existing|n/a",
                "regression-test:",
                "red-observed:",
                "test-run-evidence: verified|unavailable",
            ],
        )
        self.assertEqual(default_phases["final-review"]["routes"]["request-changes"], "fix-loop")
        self.assertEqual(default_phases["final-review"]["routes"]["approve"], "gates")
        self.assertEqual(default_phases["gates"]["routes"]["green"], "commit")
        self.assertEqual(default_phases["gates"]["routes"]["request-changes"], "fix-loop")
        self.assertEqual(default_phases["fix-loop"]["routes"]["default"], "comment-authoring")
        self.assertEqual(default_phases["comment-authoring"]["routes"]["default"], "final-review")
        self.assertIn("comment-authoring: applied", default_phases["comment-authoring"]["required_markers"])
        self.assertIn("comment-checker: checked|unavailable|n/a", default_phases["comment-authoring"]["required_markers"])
        self.assertIn("Do not refactor", default_phases["comment-authoring"]["prompt"])
        self.assertIn("skills_checked: true", default_phases["final-review"]["required_markers"])
        self.assertIn("codex-claude-parity-check: pass|fail", default_phases["final-review"]["required_markers"])
        self.assertIn("hook-parity-check: pass|fail", default_phases["final-review"]["required_markers"])
        self.assertIn("codex-claude-parity-check: pass|fail", default_phases["final-review"]["prompt"])
        self.assertIn("hook-parity-check: pass|fail", default_phases["final-review"]["prompt"])
        self.assertIn("reviewer-source: sub-agent", default_phases["final-review"]["prompt"])
        self.assertIn("## Overall", default_phases["final-review"]["prompt"])
        self.assertIn("verdict: approve", default_phases["final-review"]["prompt"])
        self.assertIn("verdict: request-changes", default_phases["final-review"]["prompt"])
        self.assertIn("skills/clean-architecture-core/SKILL.md", default_phases["final-review"]["prompt"])
        self.assertIn("skills/clean-architecture/SKILL.md", default_phases["final-review"]["prompt"])
        self.assertIn("must-avoid or failing checklist", default_phases["final-review"]["prompt"])
        self.assertIn("core skill is present", default_phases["final-review"]["prompt"])
        self.assertEqual(default_phases["pr-watch"]["routes"]["green"], "merge")
        self.assertEqual(default_phases["pr-watch"]["routes"]["has_comments"], "pr-comment-fix")
        self.assertEqual(default_phases["pr-watch"]["routes"]["ci_failed"], "pr-ci-fix")
        self.assertEqual(default_phases["pr-watch"]["routes"]["pending"], "block")
        self.assertEqual(default_phases["pr-comment-fix"]["routes"]["default"], "pr-watch")
        self.assertEqual(default_phases["pr-ci-fix"]["routes"]["default"], "pr-watch")

    def test_default_final_review_uses_claude_codex_provider_policy(self) -> None:
        import yaml

        workflow_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "agent_flow"
            / "workflows"
            / "default.yaml"
        )
        payload = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        phases = {phase["id"]: phase for phase in payload["phases"]}
        prompt = phases["final-review"]["prompt"]

        self.assertIn("installed Claude and Codex CLIs only", prompt)
        self.assertIn("Do not launch reviewer CLIs", prompt)
        self.assertIn("dropped from its remaining angles", prompt)
        self.assertIn("Never use OMP or controller-session work", prompt)

    def test_workflow_export_outputs_normalized_phase_contract(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["workflow", "export", "--workflow", "full-feature"]), 0)
        payload = json.loads(output.getvalue())
        phases = {phase["id"]: phase for phase in payload["phases"]}
        self.assertEqual(payload["id"], "full-feature")
        # drift 예외가 지목하는 값이 digest다. export가 그것을 빼면 사용자는
        # `meta.workflow_digest`와 대조할 기계 가독 뷰가 하나도 없다.
        self.assertEqual(
            payload["digest"],
            load_phase_workflow_definition(
                Path(__file__).resolve().parents[1], "full-feature"
            ).digest,
        )
        self.assertEqual(phases["domain-grill"]["artifact"], "artifacts/domain-grill.md")
        self.assertIn("domain-grill: complete", phases["domain-grill"]["required_markers"])
        self.assertEqual(phases["red"]["artifact"], "artifacts/red.log")
        self.assertEqual(phases["green"]["artifact"], "artifacts/green.log")
        for phase_id in ("red", "green", "refactor", "fix-loop", "multi-review", "architecture-review"):
            self.assertIn("skills_checked: true", phases[phase_id]["required_markers"])
            self.assertIn("profile-skill-selection: applied", phases[phase_id]["required_markers"])
            self.assertIn("changed-file-skill-resolution: applied", phases[phase_id]["required_markers"])
            self.assertIn("required-profile-skills: checked", phases[phase_id]["required_markers"])
            self.assertIn("missing-required-profile-skills:", phases[phase_id]["required_markers"])
            self.assertIn("project-local-skills: checked|n/a", phases[phase_id]["required_markers"])
            self.assertIn("project-local-skills-used:", phases[phase_id]["required_markers"])
        self.assertIn("clean-architecture: applied|n/a", phases["green"]["required_markers"])
        self.assertIn("clean-architecture: applied|n/a", phases["fix-loop"]["required_markers"])
        self.assertIn("clean-architecture-review: applied", phases["multi-review"]["required_markers"])
        self.assertIn("must-avoid-check: pass|fail|n/a", phases["multi-review"]["required_markers"])
        self.assertIn("must-avoid-check: pass|fail|n/a", phases["architecture-review"]["required_markers"])
        for phase_id in ("multi-review", "architecture-review"):
            self.assertIn("codex-claude-parity-check: pass|fail", phases[phase_id]["required_markers"])
            self.assertIn("hook-parity-check: pass|fail", phases[phase_id]["required_markers"])
            self.assertIn("codex-claude-parity-check: pass|fail", phases[phase_id]["prompt"])
            self.assertIn("hook-parity-check: pass|fail", phases[phase_id]["prompt"])
        multi_review_prompt = phases["multi-review"]["prompt"]
        self.assertIn("skills/clean-architecture-core/SKILL.md", multi_review_prompt)
        self.assertIn("skills/clean-architecture/SKILL.md", multi_review_prompt)
        self.assertIn("must-avoid or failing", multi_review_prompt)
        self.assertIn("core skill makes the overall verdict", multi_review_prompt)
        self.assertIn("dependency-rule: pass|fail", phases["architecture-review"]["required_markers"])
        architecture_review_prompt = phases["architecture-review"]["prompt"]
        self.assertIn("skills/clean-architecture-core/SKILL.md", architecture_review_prompt)
        self.assertIn("skills/clean-architecture/SKILL.md", architecture_review_prompt)
        self.assertIn("must-avoid or failing checklist", architecture_review_prompt)
        self.assertIn("skill makes the verdict", architecture_review_prompt)
        self.assertIn("presentation-skill: android|flutter|react|react-native|ios|n/a", phases["green"]["required_markers"])
        self.assertIn("Android/Kotlin/Compose/KMP changes require Android profile skills", phases["green"]["prompt"])
        self.assertEqual(phases["gates"]["artifact"], "artifacts/gate-results.json")
        self.assertEqual(phases["gates"]["routes"]["green"], "commit")
        self.assertEqual(phases["comment-authoring"]["routes"]["default"], "multi-review")

    def test_phase_declared_skills_appear_in_prompt_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = _write_resolver_skills(temp_dir)
            block = local_skill_prompt_block(
                root, "green", phase_skills=PhaseSkills(required=("alpha-guide",))
            )
            self.assertIn("alpha-guide", block)
            self.assertIn("skills/alpha-guide/SKILL.md", block)
            self.assertIn("skill-availability: pass", block)

    def test_frontmatter_skill_activates_only_on_matching_phase_and_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = _write_resolver_skills(temp_dir)

            matched = phase_skill_resolution(root, "green", task_text="build the widget catalog")
            self.assertIn("catalog-guide", [skill.name for skill in matched.required])

            wrong_task = phase_skill_resolution(root, "green", task_text="change a button colour")
            self.assertEqual([skill.name for skill in wrong_task.required], [])

            wrong_phase = phase_skill_resolution(root, "commit", task_text="build the widget catalog")
            self.assertEqual([skill.name for skill in wrong_phase.required], [])

    def test_frontmatter_skill_activates_on_changed_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = _write_resolver_skills(temp_dir)
            resolution = phase_skill_resolution(
                root, "green", changed_files=("feature/home/catalog/Renderer.kt",)
            )
            self.assertIn("catalog-guide", [skill.name for skill in resolution.required])

    def test_skill_dependencies_are_pulled_in(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = _write_resolver_skills(temp_dir)
            resolution = phase_skill_resolution(root, "green", task_text="widget catalog work")
            self.assertIn("alpha-guide", [skill.name for skill in resolution.required])

    def test_absent_required_skill_degrades_instead_of_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = _write_resolver_skills(temp_dir)
            phase_skills = PhaseSkills(required=("alpha-guide", "not-installed-anywhere"))
            missing = missing_local_skill_markers(
                "## Completion Gate\n"
                "skill-availability: degraded\n"
                "skill-use-evidence: unavailable\n"
                "project-local-skills: checked\n"
                "project-local-skills-used: alpha-guide\n"
                "project-local-skill-docs: applied\n",
                root,
                "green",
                phase_skills=phase_skills,
            )
            self.assertEqual(missing, [])

    def test_use_evidence_takes_the_self_report_even_when_a_skill_was_not_read(self) -> None:
        """원래 이 테스트는 "관측된 미독은 차단된다"를 지켰다.

        그 강제가 hook을 로드하지 않은 host 세션을 영원히 막았기 때문에, 계약을
        자기신고로 낮췄다. 지키는 것은 이제 "관측 상태와 무관하게 자기신고가
        결정한다"이다.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            root = _write_resolver_skills(temp_dir)
            phase_skills = PhaseSkills(required=("alpha-guide", "beta-guide"))
            gate = (
                "## Completion Gate\n"
                "skill-availability: pass\n"
                "skill-use-evidence: verified\n"
                "project-local-skills: checked\n"
                "project-local-skills-used: alpha-guide, beta-guide\n"
                "project-local-skill-docs: applied\n"
            )

            # hook이 없으면 관측 자체가 불가능하다. 자기신고로 통과시킨다.
            self.assertEqual(
                missing_local_skill_markers(gate, root, "green", phase_skills=phase_skills), []
            )

            # alpha만 관측돼도 자기신고가 verified면 더 이상 막지 않는다.
            record_skill_read(root, root / "skills" / "alpha-guide" / "SKILL.md")
            self.assertEqual(
                missing_local_skill_markers(gate, root, "green", phase_skills=phase_skills), []
            )

            # 자기신고가 없으면 관측이 전부 채워져 있어도 막는다.
            record_skill_read(root, root / "skills" / "beta-guide" / "SKILL.md")
            missing = missing_local_skill_markers(
                gate.replace("skill-use-evidence: verified\n", ""),
                root,
                "green",
                phase_skills=phase_skills,
            )
            self.assertEqual(len(missing), 1)
            self.assertTrue(missing[0].startswith("skill-use-evidence: verified|unavailable"))

    def test_no_required_skills_never_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = _write_resolver_skills(temp_dir)
            self.assertEqual(local_skill_prompt_block(root, "commit"), "")
            self.assertEqual(
                missing_local_skill_markers("## Completion Gate\n", root, "commit"), []
            )

    def test_clean_architecture_review_template_routes_policy_to_core_skill(self) -> None:
        template = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "_shared"
            / "review"
            / "clean-architecture.md"
        ).read_text(encoding="utf-8")
        self.assertIn("skills/clean-architecture-core/SKILL.md", template)
        self.assertIn("must-avoid rule", template)
        self.assertIn("failing required checklist item", template)
        self.assertIn("skills/clean-architecture/SKILL.md", template)
        self.assertIn("compatibility markers", template)
        self.assertNotIn("must-fix in `skills/clean-architecture/SKILL.md`", template)
        self.assertNotIn("listed as a must-fix in `skills/clean-architecture/SKILL.md`", template)

    def test_python_runner_route_key_understands_gate_results(self) -> None:
        from agent_flow.core.route_verdicts import gates_route_key, route_key

        # gates 통과 JSON은 실제 command 결과가 있을 때만 green으로 정규화된다.
        self.assertEqual(gates_route_key('{"passed": true}'), "default")
        self.assertEqual(gates_route_key('{"passed": true, "results": []}'), "default")
        self.assertEqual(
            gates_route_key('{"passed": true, "results": [{"command": "npm test", "passed": true, "output": "ok"}]}'),
            "green",
        )
        self.assertEqual(
            gates_route_key('{"passed": true, "results": [{"command": "npm test", "passed": true, "exit_code": 0}]}'),
            "green",
        )
        self.assertEqual(
            gates_route_key(
                '{"passed": true, "results": ['
                '{"command": "npm test", "passed": true, "exit_code": 0, "required": true},'
                '{"command": "npm run lint", "passed": false, "stderr": "missing", "required": false}'
                "]}"
            ),
            "green",
        )
        ci_only = {
            "passed": True,
            "results": [
                {
                    "command": "python -m pytest -q",
                    "passed": True,
                    "exit_code": 0,
                    "required": True,
                }
            ],
            "produced_by": {
                "gate_phase": "all",
                "gate_execution": "ci",
            },
        }
        self.assertEqual(gates_route_key(json.dumps(ci_only)), "default")
        ci_only["produced_by"]["gate_execution"] = "local"
        self.assertEqual(gates_route_key(json.dumps(ci_only)), "green")
        self.assertEqual(
            gates_route_key('{"passed": true, "status": "approve", "results": [{"command": "npm test", "passed": true, "output": "ok"}]}'),
            "approve",
        )
        self.assertEqual(gates_route_key('{"passed": false, "results": []}'), "request-changes")
        self.assertEqual(
            gates_route_key('{"passed": false, "results": [{"id": "lint", "passed": true}]}'),
            "request-changes",
        )
        self.assertEqual(
            gates_route_key('{"passed": false, "status": "request-changes", "results": []}'),
            "request-changes",
        )
        self.assertEqual(gates_route_key('{"passed": false, "status": "blocked", "results": []}'), "blocked")
        self.assertEqual(gates_route_key('{"passed": false, "status": "error", "results": []}'), "error")
        self.assertEqual(gates_route_key('{"passed": false, "status": "pending", "results": []}'), "pending")
        self.assertEqual(route_key("status: failed"), "default")
        self.assertEqual(route_key("status: pass"), "default")
        self.assertEqual(route_key("- status: green"), "default")
        self.assertEqual(route_key("note: status: green"), "default")
        self.assertEqual(route_key("  status: green"), "default")
        self.assertEqual(route_key("- verdict: approve"), "default")
        self.assertEqual(route_key("status: green"), "green")
        self.assertEqual(route_key("status: ci_failed"), "ci_failed")
        self.assertEqual(route_key("status: has_comments"), "has_comments")
        self.assertEqual(route_key("status: has-comments"), "default")
        # JSON이 아닌 파일은 "게이트 실패"가 아니라 "판정 불가"다. default로 접으면
        # fix-loop가 근거 없이 돌기 시작한다.
        self.assertEqual(gates_route_key("status: pass"), "malformed-results")

    def test_multi_review_route_policy_is_pure(self) -> None:
        from agent_flow.core.review_evidence import (
            ReviewEvidence,
            ReviewerOutcome,
            multi_review_route_key,
        )

        def outcome(job_id: str, verdict: str) -> ReviewerOutcome:
            return ReviewerOutcome(
                job_id=job_id,
                provider=job_id.split("-", 1)[0],
                model="test-model",
                effort="xhigh",
                status="ok",
                verdict=verdict,
                required=True,
                artifact=f"{job_id}.md",
                artifact_sha256="a" * 64,
                prompt_digest="b" * 16,
                argv_digest="c" * 16,
            )

        approve = ReviewEvidence(
            "verified",
            (
                outcome("claude-generalist", "approve"),
                outcome("codex-generalist", "approve"),
            ),
        )
        request_changes = ReviewEvidence(
            "verified",
            (
                outcome("claude-generalist", "approve"),
                outcome("codex-generalist", "request-changes"),
            ),
        )

        self.assertEqual(multi_review_route_key(approve), "approve")
        self.assertEqual(
            multi_review_route_key(request_changes),
            "request-changes",
        )

    def test_review_evidence_invalid_utf8_fails_closed(self) -> None:
        from agent_flow.core.review_evidence import load_review_evidence

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "final-review-review-results.json").write_bytes(b"\xff")

            evidence = load_review_evidence(
                artifact_root=root,
                phase_id="final-review",
                run_meta={},
            )

        self.assertEqual(evidence.validation, "invalid")

    def test_review_evidence_boolean_schema_version_fails_closed(self) -> None:
        """반증: `bool`은 `int`의 하위형이고 `True == 1`이라, 동등비교만으로는
        JSON `true`가 `Literal[1]` 스키마를 통과한다."""
        from agent_flow.core.review_evidence import load_review_evidence

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "final-review-review-results.json").write_text(
                json.dumps(
                    {
                        "schema_version": True,
                        "phase_id": "final-review",
                        "outcomes": [],
                    }
                ),
                encoding="utf-8",
            )

            evidence = load_review_evidence(
                artifact_root=root,
                phase_id="final-review",
                run_meta={},
            )

        self.assertEqual(evidence.validation, "invalid")
        self.assertEqual(
            evidence.detail,
            "review results schema does not match",
        )

    def test_review_evidence_record_shape_rejects_boolean_schema_version(
        self,
    ) -> None:
        """같은 결함이 meta 바인딩 쪽 TypeGuard에도 있었다. 여기서 통과하면
        타입 검사기에는 `Literal[1]`이라고 보증한 값이 `True`가 된다."""
        from agent_flow.core.review_evidence import (
            _review_evidence_record_shape,
        )

        record = {
            "schema_version": 1,
            "nonce": "c" * 32,
            "results_sha256": "d" * 64,
            "phase_entered_at": "2026-08-21T00:00:00+00:00",
            "observed_job_ids": ["claude-generalist"],
            "blocking_job_ids": ["claude-generalist"],
            "accept_any_provider": False,
            "expected_job_ids_by_provider": {"claude": ["claude-generalist"]},
            "complete_providers": ["claude"],
        }

        self.assertTrue(_review_evidence_record_shape(record))
        self.assertFalse(
            _review_evidence_record_shape({**record, "schema_version": True})
        )

    def test_review_evidence_recursive_json_fails_closed(self) -> None:
        from agent_flow.core.review_evidence import load_review_evidence

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "final-review-review-results.json").write_text(
                "{}",
                encoding="utf-8",
            )
            with mock.patch(
                "agent_flow.core.review_evidence.json.loads",
                side_effect=RecursionError,
            ):
                evidence = load_review_evidence(
                    artifact_root=root,
                    phase_id="final-review",
                    run_meta={},
                )

        self.assertEqual(evidence.validation, "invalid")

    def test_review_evidence_fifo_fails_closed_without_blocking(self) -> None:
        from agent_flow.core.review_evidence import load_review_evidence

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            os.mkfifo(root / "final-review-review-results.json")

            evidence = load_review_evidence(
                artifact_root=root,
                phase_id="final-review",
                run_meta={},
            )

        self.assertEqual(evidence.validation, "invalid")

    def test_review_evidence_oversize_file_fails_closed(self) -> None:
        from agent_flow.core.review_evidence import load_review_evidence

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "final-review-review-results.json").write_text(
                "{}",
                encoding="utf-8",
            )
            with mock.patch(
                "agent_flow.core.review_evidence.REVIEW_EVIDENCE_MAX_BYTES",
                1,
            ):
                evidence = load_review_evidence(
                    artifact_root=root,
                    phase_id="final-review",
                    run_meta={},
                )

        self.assertEqual(evidence.validation, "invalid")
        self.assertIn("cannot be read safely", evidence.detail)

    def test_host_multi_review_requires_confined_reviewer_processes(self) -> None:
        from agent_flow.adapters.hosted import HostedAdapter, _multi_reviewer_block

        adapter = HostedAdapter("codex")
        self.assertIn("independent OS-confined", adapter._hint)
        self.assertIn("do not replace them with", adapter._hint)
        self.assertIn("reviewer-source: sub-agent", adapter._hint)
        omp_adapter = HostedAdapter("omp")
        self.assertIn("independent OS-confined", omp_adapter._hint)
        self.assertIn("in-session task sub-agents", omp_adapter._hint)

        block = _multi_reviewer_block()
        self.assertIn("Confined reviewer subprocesses", block)
        self.assertIn("reviewer-source: sub-agent", block)
        self.assertIn("Do not spawn or substitute in-session", block)

    def test_only_required_host_reviewer_failures_block_aggregation(self) -> None:
        from agent_flow.adapters.hosted import _required_reviewer_failures
        from agent_flow.multi_review import Distribution
        from agent_flow.subprocess_pool import SubprocessResult

        distribution = Distribution(
            host="codex",
            required_job_ids=frozenset({"codex-a", "codex-b"}),
        )
        results = [
            SubprocessResult(
                job_id="codex-a",
                returncode=0,
                stdout="reviewer-source: sub-agent\nNo findings\nverdict: approve",
            ),
            SubprocessResult(job_id="codex-b", returncode=1),
            SubprocessResult(job_id="claude-a-extra", returncode=1),
        ]
        self.assertEqual(
            _required_reviewer_failures(distribution, results),
            ["codex-b: exit 1"],
        )

        missing = _required_reviewer_failures(distribution, results[:1])
        self.assertEqual(missing, ["codex-b: missing result"])

        invalid = [
            SubprocessResult(
                job_id="codex-a",
                returncode=0,
                stdout="No findings\nverdict: approve",
            ),
            SubprocessResult(
                job_id="codex-b",
                returncode=0,
                stdout="reviewer-source: sub-agent\nNo findings\nverdict: approve",
            ),
        ]
        self.assertEqual(
            _required_reviewer_failures(distribution, invalid),
            ["codex-a: reviewer output is missing provenance marker"],
        )

        markdown_bold = [
            SubprocessResult(
                job_id="codex-a",
                returncode=0,
                stdout="**reviewer-source: sub-agent**\nNo findings\nverdict: approve",
            ),
            SubprocessResult(
                job_id="codex-b",
                returncode=0,
                stdout="reviewer-source: sub-agent\nNo findings\nverdict: approve",
            ),
        ]
        self.assertEqual(
            _required_reviewer_failures(distribution, markdown_bold),
            ["codex-a: reviewer output is missing provenance marker"],
        )

    def test_reviewer_pool_fans_out_and_can_be_narrowed(self) -> None:
        from agent_flow.cli_detect import CliInfo
        from agent_flow.core.worktree_isolation import WorktreeIsolationError
        from agent_flow.multi_review import (
            ReviewerJob,
            distribute,
            residual_host_jobs,
            resolve_review_clis,
        )

        jobs = [
            ReviewerJob(
                "generalist", "prompt", Path("generalist.md"), Path.cwd()
            ),
            ReviewerJob(
                "architecture-design",
                "prompt",
                Path("architecture-design.md"),
                Path.cwd(),
            ),
        ]
        codex = CliInfo("codex", ("codex",), ("exec",))
        claude = CliInfo("claude", ("claude",), ("-p",))
        omp = CliInfo("omp", ("omp",), ("-p",))
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch(
                "agent_flow.multi_review.detect_available_clis",
                return_value=[codex],
            ),
            mock.patch("agent_flow.multi_review.detect_host_cli", return_value="codex"),
        ):
            self.assertEqual(resolve_review_clis(), [])
            distribution = distribute(jobs)
            self.assertFalse(distribution.fallback_to_generic)
            self.assertEqual(distribution.by_cli, {"codex": jobs})
            self.assertEqual(residual_host_jobs(distribution), [])
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch(
                "agent_flow.multi_review.detect_available_clis",
                return_value=[],
            ),
            mock.patch("agent_flow.multi_review.detect_host_cli", return_value=None),
        ):
            distribution = distribute(jobs)
            self.assertTrue(distribution.fallback_to_generic)
            self.assertEqual(residual_host_jobs(distribution), jobs)
        with mock.patch.dict(os.environ, {"AGENT_FLOW_REVIEWERS": "codex"}, clear=True):
            self.assertEqual([cli.name for cli in resolve_review_clis()], ["codex"])
        with mock.patch.dict(os.environ, {"AGENT_FLOW_REVIEWERS": "omp"}, clear=True):
            self.assertEqual(resolve_review_clis(), [])
        with (
            mock.patch.dict(
                os.environ,
                {"AGENT_FLOW_REVIEWERS": "claude,codex"},
                clear=True,
            ),
            mock.patch(
                "agent_flow.multi_review.detect_available_clis",
                return_value=[claude, codex],
            ),
        ):
            distribution = distribute(jobs, host="codex")
            self.assertEqual(residual_host_jobs(distribution), [])
            self.assertEqual(distribution.by_cli["codex"], jobs)
            self.assertEqual(len(distribution.by_cli["claude"]), len(jobs))
            self.assertEqual(
                distribution.required_job_ids,
                {"codex-generalist", "codex-architecture-design"},
            )
            outputs = [
                job.output_path
                for assigned in distribution.by_cli.values()
                for job in assigned
            ]
            self.assertEqual(len(outputs), len(set(outputs)))
            self.assertFalse(distribution.insufficient_reviewers)
            collision_jobs = [
                ReviewerJob(
                    "foo-claude",
                    "prompt",
                    Path("foo-claude.md"),
                    Path.cwd(),
                ),
                ReviewerJob("foo", "prompt", Path("foo.md"), Path.cwd()),
            ]
            collision_safe = distribute(collision_jobs, host="codex")
            collision_outputs = [
                job.output_path
                for assigned in collision_safe.by_cli.values()
                for job in assigned
            ]
            self.assertEqual(
                len(collision_outputs),
                len(set(collision_outputs)),
            )
            self.assertIn(Path("foo-extra-claude.md"), collision_outputs)


            duplicate_jobs = [
                ReviewerJob("first", "prompt", Path("same.md"), Path.cwd()),
                ReviewerJob("second", "prompt", Path("same.md"), Path.cwd()),
            ]
            with self.assertRaisesRegex(
                WorktreeIsolationError,
                "reviewer output path collision",
            ):
                distribute(duplicate_jobs, host="codex")
        with (
            mock.patch.dict(
                os.environ,
                {"AGENT_FLOW_REVIEWERS": "codex"},
                clear=True,
            ),
            mock.patch(
                "agent_flow.multi_review.detect_available_clis",
                return_value=[claude, codex],
            ),
        ):
            narrowed = distribute(jobs, host="claude")
            self.assertEqual(tuple(narrowed.by_cli), ("codex",))
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch(
                "agent_flow.multi_review.detect_available_clis",
                return_value=[codex],
            ),
        ):
            one_process = distribute(jobs[:1], host="codex")
            self.assertTrue(one_process.insufficient_reviewers)
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch(
                "agent_flow.multi_review.detect_available_clis",
                return_value=[claude, codex, omp],
            ),
        ):
            distribution = distribute(jobs, host="omp")
            self.assertEqual(tuple(distribution.by_cli), ("claude", "codex"))
            self.assertNotIn("omp", distribution.by_cli)

    def test_final_review_uses_only_claude_and_codex(self) -> None:
        from agent_flow.cli_detect import CliInfo
        from agent_flow.multi_review import ReviewerJob, distribute_final_review

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            jobs = [
                ReviewerJob("generalist", "prompt", root / "generalist.md", root),
                ReviewerJob("architecture", "prompt", root / "architecture.md", root),
            ]
            clis = [
                CliInfo("claude", ("claude",), ("-p",)),
                CliInfo("codex", ("codex",), ("exec",)),
                CliInfo("omp", ("omp",), ("-p",)),
            ]
            with mock.patch(
                "agent_flow.multi_review.detect_available_clis",
                return_value=clis,
            ):
                distribution = distribute_final_review(jobs, host="omp")

            self.assertEqual(tuple(distribution.by_cli), ("claude", "codex"))
            self.assertTrue(distribution.accept_any_provider)
            self.assertEqual(distribution.required_job_ids, frozenset())
            self.assertTrue(all(
                "-omp" not in str(job.output_path)
                for assigned in distribution.by_cli.values()
                for job in assigned
            ))
            # angle 1개라도 provider 2개면 독립 process 2개다. angle 수로 세면
            # 정상 배분이 blocked로 뒤집힌다.
            with mock.patch(
                "agent_flow.multi_review.detect_available_clis",
                return_value=clis,
            ):
                one_angle = distribute_final_review(jobs[:1], host="omp")
            self.assertFalse(one_angle.insufficient_reviewers)
            with mock.patch(
                "agent_flow.multi_review.detect_available_clis",
                return_value=[clis[0], clis[2]],
            ):
                lone_provider = distribute_final_review(jobs[:1], host="omp")
            self.assertTrue(lone_provider.insufficient_reviewers)

            distribution.by_cli["claude"][0].output_path.write_text(
                "# claude-generalist\n\n- status: ERROR\n",
                encoding="utf-8",
            )
            with mock.patch(
                "agent_flow.multi_review.detect_available_clis",
                return_value=clis,
            ):
                retry = distribute_final_review(jobs, host="omp")
            self.assertEqual(tuple(retry.by_cli), ("claude", "codex"))
            self.assertFalse(hasattr(retry, "skipped_providers"))

            # 이전 실패 artifact가 둘 다 남아도 새 retry는 둘 다 다시 실행한다.
            distribution.by_cli["codex"][0].output_path.write_text(
                "# codex-generalist\n\n- status: TIMEOUT\n",
                encoding="utf-8",
            )
            with mock.patch(
                "agent_flow.multi_review.detect_available_clis",
                return_value=clis,
            ):
                deadlocked = distribute_final_review(jobs, host="omp")
            self.assertEqual(tuple(deadlocked.by_cli), ("claude", "codex"))
            self.assertFalse(deadlocked.fallback_to_generic)

            # 리뷰 본문이 실패 marker를 인용해도 provider는 살아 있어야 한다.
            for cli_name in ("claude", "codex"):
                distribution.by_cli[cli_name][0].output_path.write_text(
                    f"# {cli_name}-generalist\n\n- status: OK\n\n"
                    "## review output\n\n- note: 앞 라운드 artifact는 `- status: ERROR`였다\n",
                    encoding="utf-8",
                )
            with mock.patch(
                "agent_flow.multi_review.detect_available_clis",
                return_value=clis,
            ):
                quoted = distribute_final_review(jobs, host="omp")
            self.assertEqual(tuple(quoted.by_cli), ("claude", "codex"))

    def test_final_review_honors_reviewer_pool_narrowing(self) -> None:
        from agent_flow.cli_detect import CliInfo
        from agent_flow.multi_review import ReviewerJob, distribute_final_review

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            jobs = [
                ReviewerJob("generalist", "prompt", root / "generalist.md", root),
                ReviewerJob("architecture", "prompt", root / "architecture.md", root),
            ]
            clis = [
                CliInfo("claude", ("claude",), ("-p",)),
                CliInfo("codex", ("codex",), ("exec",)),
            ]
            with (
                mock.patch.dict(
                    os.environ, {"AGENT_FLOW_REVIEWERS": "codex"}, clear=True
                ),
                mock.patch(
                    "agent_flow.multi_review.detect_available_clis",
                    return_value=clis,
                ),
            ):
                narrowed = distribute_final_review(jobs, host="claude")

            self.assertEqual(tuple(narrowed.by_cli), ("codex",))
            self.assertFalse(narrowed.fallback_to_generic)

    def test_invalid_reviewer_pool_does_not_expand_to_all_providers(self) -> None:
        from agent_flow.cli_detect import CliInfo
        from agent_flow.multi_review import (
            ReviewerJob,
            distribute,
            distribute_final_review,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            jobs = [ReviewerJob("generalist", "prompt", root / "generalist.md", root)]
            clis = [
                CliInfo("claude", ("claude",), ("-p",)),
                CliInfo("codex", ("codex",), ("exec",)),
            ]
            with (
                mock.patch.dict(
                    os.environ, {"AGENT_FLOW_REVIEWERS": "omp"}, clear=True
                ),
                mock.patch(
                    "agent_flow.multi_review.detect_available_clis",
                    return_value=clis,
                ),
            ):
                review = distribute(jobs, host="claude")
                final_review = distribute_final_review(jobs, host="claude")

            for distribution in (review, final_review):
                self.assertEqual(distribution.by_cli, {})
                self.assertTrue(distribution.fallback_to_generic)
                self.assertTrue(distribution.insufficient_reviewers)

    def test_final_review_stops_failed_provider_after_probe(self) -> None:
        from agent_flow.adapters.hosted import (
            _required_reviewer_failures,
            _write_review_results,
        )
        from agent_flow.cli_detect import CliInfo
        from agent_flow.multi_review import Distribution, ReviewerJob, run_distribution
        from agent_flow.subprocess_pool import SubprocessResult

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            by_cli = {
                cli_name: [
                    ReviewerJob(
                        angle_id,
                        "prompt",
                        root / f"{cli_name}-{angle_id}.md",
                        root,
                    )
                    for angle_id in ("generalist", "architecture")
                ]
                for cli_name in ("claude", "codex")
            }
            distribution = Distribution(
                by_cli=by_cli,
                required_job_ids=frozenset({
                    f"{cli_name}-{job.angle_id}"
                    for cli_name, jobs in by_cli.items()
                    for job in jobs
                }),
                accept_any_provider=True,
                phase_id="final-review",
            )
            clis = {
                "claude": CliInfo("claude", ("claude",), ("-p",)),
                "codex": CliInfo("codex", ("codex",), ("exec",)),
            }
            (root / "meta.json").write_text(
                json.dumps(
                    {
                        "run_id": "r1",
                        "review_nonce": "c" * 32,
                        "phase_entered_at": "2026-08-21T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            launches: list[list[str]] = []

            def fake_parallel(jobs):
                job_ids = [job.job_id for job in jobs]
                launches.append(job_ids)
                if len(launches) == 1:
                    return [
                        # 정상 리뷰. sandbox 문구가 stderr 잡음으로 섞여도 결과는 유효하다.
                        SubprocessResult(
                            job_id="claude-generalist",
                            returncode=0,
                            stdout="reviewer-source: sub-agent\nclean\nverdict: approve",
                            stderr='+ stderr="sandbox-exec: sandbox_apply: Operation not permitted"',
                        ),
                        # 실제 sandbox 실패. CLI가 실행되지 못해 provenance가 없다.
                        SubprocessResult(
                            job_id="codex-generalist",
                            returncode=1,
                            stdout="sandbox-exec: sandbox_apply: Operation not permitted",
                        ),
                    ]
                return [
                    SubprocessResult(
                        job_id=job.job_id,
                        returncode=0,
                        stdout="reviewer-source: sub-agent\nclean\nverdict: approve",
                    )
                    for job in jobs
                ]

            with (
                mock.patch(
                    "agent_flow.multi_review.cli_by_name",
                    side_effect=lambda name: clis[name],
                ),
                mock.patch(
                    "agent_flow.multi_review.run_parallel",
                    side_effect=fake_parallel,
                ),
                mock.patch(
                    "agent_flow.multi_review.assert_managed_hooks_registered",
                ),
                mock.patch(
                    "agent_flow.multi_review.leader_root_for",
                    return_value=None,
                ),
            ):
                execution = run_distribution(distribution, root)
            _write_review_results(distribution, execution.outcomes)
            self.assertEqual(execution.skipped_providers, ("codex",))
            result_payload = json.loads(
                (root / "final-review-review-results.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(
                all(
                    outcome["required"] is False
                    for outcome in result_payload["outcomes"]
                )
            )
            evidence = json.loads(
                (root / "meta.json").read_text(encoding="utf-8")
            )["review_evidence"]["final-review"]
            self.assertEqual(evidence.get("complete_providers"), ["claude"])

            self.assertEqual(
                launches,
                [
                    ["claude-generalist", "codex-generalist"],
                    ["claude-architecture"],
                ],
            )
            self.assertFalse((root / "codex-architecture.md").exists())
            self.assertIn(
                "reviewer sandbox unavailable",
                (root / "codex-generalist.md").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                _required_reviewer_failures(distribution, execution.results),
                [],
            )

    def test_reviewer_result_error_ignores_quoted_sandbox_diagnostics(self) -> None:
        """반증: 리뷰 본문의 인용을 실패 신호로 읽으면 이 저장소 리뷰가 자기 자신을 탈락시킨다."""
        from agent_flow.multi_review import reviewer_result_error
        from agent_flow.subprocess_pool import SubprocessResult

        quoted = SubprocessResult(
            job_id="codex-generalist",
            returncode=0,
            stdout=(
                "reviewer-source: sub-agent\n"
                "- note, src/agent_flow/multi_review.py:42, "
                "'sandbox_apply: Operation not permitted' 문자열을 실패 신호로 쓴다\n"
                "verdict: approve\n"
            ),
        )
        real_failure = SubprocessResult(
            job_id="codex-generalist",
            returncode=1,
            stdout="sandbox-exec: sandbox_apply: Operation not permitted",
        )
        stderr_failure = SubprocessResult(
            job_id="codex-generalist",
            returncode=1,
            stderr="sandbox-exec: sandbox_apply: Operation not permitted",
        )
        timed_failure = SubprocessResult(
            job_id="codex-generalist",
            returncode=-1,
            stdout="sandbox-exec: sandbox_apply: Operation not permitted",
            timed_out=True,
        )

        self.assertIsNone(reviewer_result_error(quoted))
        self.assertEqual(
            reviewer_result_error(real_failure),
            "reviewer sandbox unavailable",
        )
        self.assertEqual(
            reviewer_result_error(stderr_failure),
            "reviewer sandbox unavailable",
        )
        self.assertEqual(reviewer_result_error(timed_failure), "timeout")

    def test_review_angle_artifact_path_is_confined_to_run_dir(self) -> None:
        from agent_flow.adapters.hosted import _review_angle_output
        from agent_flow.core.worktree_isolation import WorktreeIsolationError
        from agent_flow.multi_review import ReviewerJob, _write_review_artifact

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            run_dir.mkdir()
            expected = run_dir / "final-review-generalist.md"
            self.assertEqual(
                _review_angle_output(run_dir, "final-review", "generalist"),
                expected.resolve(),
            )
            for angle_id in ("../escape", "nested/escape", ".hidden", "UPPER"):
                with self.assertRaises(ValueError):
                    _review_angle_output(run_dir, "final-review", angle_id)

            escaped = Path(temp_dir) / "escaped.md"
            escaped.write_text("preserve", encoding="utf-8")
            expected.symlink_to(escaped)
            with self.assertRaises(ValueError):
                _review_angle_output(run_dir, "final-review", "generalist")
            with self.assertRaises(WorktreeIsolationError):
                _write_review_artifact(
                    ReviewerJob(
                        angle_id="generalist",
                        prompt="review",
                        output_path=expected,
                        artifact_root=run_dir,
                    ),
                    "overwrite",
                )
            self.assertEqual(escaped.read_text(encoding="utf-8"), "preserve")


    def test_review_phase_available_clis_excludes_omp(self) -> None:
        from agent_flow.cli_detect import CliInfo
        from agent_flow.runner import Phase, _phase_available_clis

        clis = [
            CliInfo("claude", ("claude",), ("-p",)),
            CliInfo("codex", ("codex",), ("exec",)),
            CliInfo("omp", ("omp",), ("-p",)),
        ]
        self.assertEqual(_phase_available_clis(clis, Phase("implement", "")), clis)
        self.assertEqual(
            [
                cli.name
                for cli in _phase_available_clis(
                    clis,
                    Phase("final-review", "", multi_review=True),
                )
            ],
            ["claude", "codex"],
        )

    def test_reviewer_cli_args_defer_writes_to_outer_sandbox_and_disable_sessions(self) -> None:
        from agent_flow.cli_detect import CliInfo
        from agent_flow.multi_review import _reviewer_cli_args

        project = Path("/tmp/reviewer-project")
        codex = _reviewer_cli_args(
            CliInfo("codex", ("codex",), ("exec",)),
            prompt="review",
            project_root=project,
        )
        self.assertIn("--ephemeral", codex)
        self.assertEqual(codex[codex.index("--sandbox") + 1], "danger-full-access")
        self.assertEqual(codex[codex.index("--cd") + 1], str(project.resolve()))

        claude = _reviewer_cli_args(
            CliInfo("claude", ("claude",), ("-p",)),
            prompt="review",
            project_root=project,
        )
        self.assertIn("--no-session-persistence", claude)
        self.assertIn("--safe-mode", claude)
        self.assertNotIn("--bare", claude)
        self.assertEqual(claude[claude.index("--permission-mode") + 1], "plan")

        # OMP는 reviewer provider pool 밖이다. 전용 인자 분기를 남기면 pool이
        # 다시 열릴 때 그 분기가 근거처럼 읽힌다.
        omp = _reviewer_cli_args(
            CliInfo("omp", ("omp",), ("-p",)),
            prompt="review",
            project_root=project,
        )
        self.assertEqual(omp, ("-p", "review"))

    def test_adapter_completion_prompt_uses_status_next_command(self) -> None:
        from agent_flow.adapters.generic import GenericAdapter
        from agent_flow.runner import Phase

        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            prompt = GenericAdapter().render_envelope(
                Phase(id="implement", description=""),
                project_root / ".agent-flow" / "runs" / "default" / "r1",
                project_root,
            )
        self.assertIn("agent-flow status", prompt)
        self.assertIn("next_command", prompt)
        self.assertNotIn("agent-flow continue", prompt)

    def test_adapter_local_skill_prompt_uses_config_root(self) -> None:
        from agent_flow.adapters.generic import GenericAdapter
        from agent_flow.runner import Phase

        with tempfile.TemporaryDirectory() as temp_dir:
            config_root = Path(temp_dir) / "leader"
            project_root = Path(temp_dir) / "worktree"
            config_root.mkdir()
            project_root.mkdir()
            _write_local_skill_files(config_root)
            adapter = GenericAdapter()
            adapter._config_root = config_root

            prompt = adapter.render_envelope(
                Phase(id="implement", description=""),
                project_root / ".agent-flow" / "runs" / "default" / "r1",
                project_root,
            )

        self.assertIn("samantha-architecture-guide", prompt)
        self.assertIn(
            str(config_root / ".agent-flow" / "local-skills" / "samantha-architecture-guide" / "SKILL.md"),
            prompt,
        )

    def test_multi_review_route_uses_runner_owned_outcomes(self) -> None:
        from agent_flow.runner import Phase, Runner

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            phase = Phase(
                id="multi-review",
                description="",
                multi_review=True,
                routes={
                    "approve": "architecture-review",
                    "request-changes": "fix-loop",
                },
            )
            runner = Runner.__new__(Runner)
            runner.run_dir = run_dir
            runner.phases = [
                phase,
                Phase(id="fix-loop", description=""),
                Phase(id="architecture-review", description=""),
            ]
            (run_dir / "multi-review.md").write_text(
                "## Reviewer fake-a\n"
                "reviewer-source: sub-agent\n"
                "verdict: approve\n\n"
                "## Reviewer fake-b\n"
                "reviewer-source: sub-agent\n"
                "verdict: approve\n\n"
                "## Overall\n"
                "verdict: approve\n",
                encoding="utf-8",
            )
            outcomes = []
            for job_id, verdict in (
                ("claude-generalist", "request-changes"),
                ("codex-generalist", "approve"),
            ):
                artifact = run_dir / f"{job_id}.md"
                artifact.write_text(
                    "## Reviewer\n"
                    "reviewer-source: sub-agent\n"
                    f"verdict: {verdict}\n",
                    encoding="utf-8",
                )
                outcomes.append(
                    {
                        "job_id": job_id,
                        "provider": job_id.split("-", 1)[0],
                        "model": "test-model",
                        "effort": "xhigh",
                        "status": "ok",
                        "verdict": verdict,
                        "required": job_id.startswith("claude"),
                        "artifact": artifact.name,
                        "artifact_sha256": hashlib.sha256(
                            artifact.read_bytes()
                        ).hexdigest(),
                        "prompt_digest": "a" * 16,
                        "argv_digest": "b" * 16,
                    }
                )
            results_path = run_dir / "multi-review-review-results.json"
            review_nonce = "c" * 32
            phase_entered_at = "2026-08-21T00:00:00+00:00"

            def write_results(selected):
                payload = {
                    "schema_version": 1,
                    "phase_id": "multi-review",
                    "produced_by": {
                        "run_id": "r1",
                        "nonce": review_nonce,
                        "phase_entered_at": phase_entered_at,
                    },
                    "outcomes": selected,
                }
                results_path.write_text(json.dumps(payload), encoding="utf-8")

            def bind_results(selected):
                (run_dir / "meta.json").write_text(
                    json.dumps(
                        {
                            "run_id": "r1",
                            "review_nonce": review_nonce,
                            "phase_entered_at": phase_entered_at,
                            "review_evidence": {
                                "multi-review": {
                                    "schema_version": 1,
                                    "nonce": review_nonce,
                                    "phase_entered_at": phase_entered_at,
                                    "results_sha256": hashlib.sha256(
                                        results_path.read_bytes()
                                    ).hexdigest(),
                                    "observed_job_ids": [
                                        outcome["job_id"] for outcome in selected
                                    ],
                                    "blocking_job_ids": [
                                        outcome["job_id"]
                                        for outcome in selected
                                        if outcome["required"]
                                    ],
                                    "accept_any_provider": False,
                                    "expected_job_ids_by_provider": {
                                        outcome["provider"]: [
                                            outcome["job_id"]
                                        ]
                                        for outcome in outcomes
                                    },
                                    "complete_providers": [
                                        outcome["provider"]
                                        for outcome in selected
                                    ],
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )

            write_results(outcomes)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(runner._next_index(0, phase)[:2], (0, True))
            self.assertIn("runner-owned review evidence", output.getvalue())

            bind_results(outcomes)
            self.assertEqual(runner._next_index(0, phase)[:2], (1, False))

            outcomes[0]["verdict"] = "approve"
            write_results(outcomes)
            bind_results(outcomes)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(runner._next_index(0, phase)[:2], (0, True))
            self.assertIn("runner-owned review evidence", output.getvalue())

            first_artifact = run_dir / outcomes[0]["artifact"]
            first_artifact.write_text(
                "## Reviewer\n"
                "reviewer-source: sub-agent\n"
                "verdict: approve\n",
                encoding="utf-8",
            )
            outcomes[0]["artifact_sha256"] = hashlib.sha256(
                first_artifact.read_bytes()
            ).hexdigest()
            write_results(outcomes)
            bind_results(outcomes)

            self.assertEqual(runner._next_index(0, phase)[:2], (2, False))
            (run_dir / "multi-review.md").write_text(
                "## Overall\nverdict: request-changes\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase)[:2], (1, False))

            write_results(outcomes[:1])
            bind_results(outcomes[:1])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(runner._next_index(0, phase)[:2], (0, True))
            self.assertIn(
                "requires 2+ independent sub-agent reviewer verdicts",
                output.getvalue(),
            )

            results_path.unlink()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(runner._next_index(0, phase)[:2], (0, True))
            self.assertIn(
                "requires a complete independent sub-agent reviewer set",
                output.getvalue(),
            )




    def test_architecture_lint_validates_android_roles_and_packages(self) -> None:
        from agent_flow.core.architecture_lint import changed_files, lint_profiles, lint_project

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            good = root / "core" / "domain" / "chat" / "src" / "main" / "java" / "com" / "example" / "app" / "core" / "domain" / "chat" / "Chat.kt"
            good.parent.mkdir(parents=True, exist_ok=True)
            good.write_text("package com.example.app.core.domain.chat\nclass Chat\n", encoding="utf-8")
            pair = root / "core" / "data" / "chat"
            pair.mkdir(parents=True)
            self.assertEqual(lint_project(root, "android", files=[str(good.relative_to(root))]), [])

            settings = root / "settings.gradle.kts"
            settings.write_text(
                'include(":core:platform")\n'
                'include(":core:platform:camera")\n'
                'include(":core:domain:chat", ":core:data:chat")\n',
                encoding="utf-8",
            )
            adapter = root / "core" / "platform" / "camera" / "src" / "main" / "java" / "com" / "example" / "app" / "core" / "platform" / "camera" / "Camera.kt"
            adapter.parent.mkdir(parents=True, exist_ok=True)
            adapter.write_text("package com.example.app.core.platform.camera\nclass Camera\n", encoding="utf-8")
            self.assertEqual(lint_project(root, "android", files=[str(adapter.relative_to(root))]), [])

            platform_root_file = root / "core" / "platform" / "src" / "main" / "java" / "com" / "example" / "app" / "core" / "platform" / "Platform.kt"
            platform_root_file.parent.mkdir(parents=True, exist_ok=True)
            platform_root_file.write_text("package com.example.app.core.platform\nclass Platform\n", encoding="utf-8")
            self.assertEqual(lint_project(root, "android", files=[str(platform_root_file.relative_to(root))]), [])

            bad_adapter = root / "core" / "platform" / "location" / "src" / "main" / "java" / "com" / "example" / "app" / "core" / "platform" / "Location.kt"
            bad_adapter.parent.mkdir(parents=True, exist_ok=True)
            bad_adapter.write_text("package com.example.app.core.platform\nclass Location\n", encoding="utf-8")
            adapter_findings = lint_project(root, "android", files=[str(bad_adapter.relative_to(root))])
            adapter_messages = "\n".join(finding.message for finding in adapter_findings)
            self.assertIn("does not match role suffix core.platform.location", adapter_messages)
            self.assertIn("Gradle module :core:platform:location is not declared in settings", adapter_messages)

            domain_build = root / "core" / "domain" / "chat" / "build.gradle.kts"
            domain_build.write_text(
                'namespace = "com.example.app.core.data.chat"\n'
                "dependencies { implementation(projects.core.data.chat) }\n",
                encoding="utf-8",
            )
            direction_findings = lint_project(root, "android", files=[str(good.relative_to(root))])
            direction_messages = "\n".join(f.message for f in direction_findings)
            self.assertIn("forbidden Gradle dependency :core:data", direction_messages)
            self.assertIn("namespace com.example.app.core.data.chat does not match role suffix core.domain.chat", direction_messages)
            direct_build_findings = lint_project(root, "android", files=[str(domain_build.relative_to(root))])
            direct_build_messages = "\n".join(f.message for f in direct_build_findings)
            self.assertIn("forbidden Gradle dependency :core:data", direct_build_messages)
            self.assertIn("namespace com.example.app.core.data.chat does not match role suffix core.domain.chat", direct_build_messages)
            self.assertNotIn("requires package declaration", direct_build_messages)

            java_file = root / "core" / "domain" / "news" / "src" / "main" / "java" / "com" / "example" / "app" / "core" / "domain" / "news" / "News.java"
            java_file.parent.mkdir(parents=True, exist_ok=True)
            java_file.write_text("package com.example.app.core.domain.news;\nclass News {}\n", encoding="utf-8")
            (root / "core" / "data" / "news").mkdir(parents=True)
            settings.write_text(settings.read_text(encoding="utf-8") + 'include(":core:domain:news")\n', encoding="utf-8")
            self.assertEqual(lint_project(root, "android", files=[str(java_file.relative_to(root))]), [])

            domain_entity = root / "core" / "domain" / "orders" / "src" / "main" / "java" / "com" / "example" / "app" / "core" / "domain" / "orders" / "OrderEntity.kt"
            domain_entity.parent.mkdir(parents=True, exist_ok=True)
            domain_entity.write_text("package com.example.app.core.domain.orders\nclass OrderEntity\n", encoding="utf-8")
            (root / "core" / "data" / "orders").mkdir(parents=True)
            settings.write_text(settings.read_text(encoding="utf-8") + 'include(":core:domain:orders")\n', encoding="utf-8")
            self.assertEqual(lint_project(root, "android", files=[str(domain_entity.relative_to(root))]), [])

            missing_package = root / "core" / "domain" / "profile" / "src" / "main" / "java" / "com" / "example" / "app" / "core" / "domain" / "profile" / "Profile.kt"
            missing_package.parent.mkdir(parents=True, exist_ok=True)
            missing_package.write_text("class Profile\n", encoding="utf-8")
            (root / "core" / "data" / "profile").mkdir(parents=True)
            missing_package_findings = lint_project(root, "android", files=[str(missing_package.relative_to(root))])
            self.assertIn("core-domain requires package declaration", "\n".join(f.message for f in missing_package_findings))

            data_file = root / "core" / "data" / "chat" / "src" / "main" / "java" / "com" / "example" / "app" / "core" / "data" / "chat" / "ChatData.kt"
            data_file.parent.mkdir(parents=True, exist_ok=True)
            data_file.write_text("package com.example.app.core.data.chat\nclass ChatData\n", encoding="utf-8")
            data_build = root / "core" / "data" / "chat" / "build.gradle.kts"
            data_build.write_text("dependencies {}\n", encoding="utf-8")
            data_findings = lint_project(root, "android", files=[str(data_file.relative_to(root))])
            self.assertIn("core-data must depend on :core:domain:chat", "\n".join(f.message for f in data_findings))

            hyphen_source = root / "core" / "data" / "user-profile" / "src" / "main" / "java" / "com" / "example" / "app" / "core" / "data" / "user_profile" / "UserProfileData.kt"
            hyphen_source.parent.mkdir(parents=True, exist_ok=True)
            hyphen_source.write_text("package com.example.app.core.data.user_profile\nclass UserProfileData\n", encoding="utf-8")
            (root / "core" / "domain" / "user-profile").mkdir(parents=True)
            hyphen_build = root / "core" / "data" / "user-profile" / "build.gradle.kts"
            hyphen_build.write_text("dependencies { implementation(projects.core.domain.userProfile) }\n", encoding="utf-8")
            settings.write_text(settings.read_text(encoding="utf-8") + 'include(":core:domain:user-profile", ":core:data:user-profile")\n', encoding="utf-8")
            self.assertEqual(lint_project(root, "android", files=[str(hyphen_source.relative_to(root))]), [])

            groovy_domain = root / "core" / "domain" / "payments"
            groovy_domain.mkdir(parents=True)
            groovy_build = root / "core" / "data" / "payments" / "build.gradle"
            groovy_build.parent.mkdir(parents=True)
            groovy_build.write_text("namespace 'com.example.app.core.domain.payments'\ndependencies {}\n", encoding="utf-8")
            settings.write_text(
                settings.read_text(encoding="utf-8")
                + 'include(":core:domain:payments", ":core:data:payments")\n',
                encoding="utf-8",
            )
            groovy_findings = lint_project(root, "android", files=[str(groovy_build.relative_to(root))])
            groovy_messages = "\n".join(f.message for f in groovy_findings)
            self.assertIn("namespace com.example.app.core.domain.payments does not match role suffix core.data.payments", groovy_messages)
            self.assertIn("core-data must depend on :core:domain:payments", groovy_messages)

            bad = root / "core" / "domain" / "billing" / "src" / "main" / "java" / "com" / "example" / "app" / "core" / "data" / "billing" / "BillingDto.kt"
            bad.parent.mkdir(parents=True, exist_ok=True)
            bad.write_text("package com.example.app.core.data.billing\nclass BillingDto\n", encoding="utf-8")
            findings = lint_project(root, "android", files=[str(bad.relative_to(root))])
            messages = "\n".join(finding.message for finding in findings)
            self.assertIn("forbidden token Dto", messages)
            self.assertIn("does not match role suffix core.domain.billing", messages)
            self.assertIn("requires paired role core-data", messages)

            presentation = root / "feature" / "checkout" / "presentation" / "src" / "main" / "java" / "com" / "example" / "app" / "feature" / "checkout" / "presentation" / "CheckoutScreen.kt"
            presentation.parent.mkdir(parents=True, exist_ok=True)
            presentation.write_text(
                "package com.example.app.feature.checkout.presentation\n"
                "import com.example.app.core.data.checkout.CheckoutDTO\n"
                "class CheckoutScreen\n",
                encoding="utf-8",
            )
            (root / "feature" / "checkout" / "api").mkdir(parents=True)
            presentation_findings = lint_project(root, "android", files=[str(presentation.relative_to(root))])
            self.assertIn("feature-presentation contains forbidden token Dto", "\n".join(f.message for f in presentation_findings))

            test_file = root / "tests" / "test_billing.py"
            test_file.parent.mkdir(parents=True, exist_ok=True)
            test_file.write_text("def test_billing(): pass\n", encoding="utf-8")
            self.assertEqual(lint_project(root, "python", files=[str(test_file.relative_to(root))]), [])

            ordinary_python = root / "src" / "agent_flow" / "cli.py"
            ordinary_python.parent.mkdir(parents=True, exist_ok=True)
            ordinary_python.write_text("def main(): pass\n", encoding="utf-8")
            self.assertEqual(lint_project(root, "python", files=[str(ordinary_python.relative_to(root))]), [])

            # 아래 python 단정은 그 profile의 strict lint가 켜져 있어야 성립한다.
            # `src/core/`가 있다는 사실만으로는 켜지지 않고, 빈 디렉터리도 근거가
            # 아니다 - role 패턴이 맞는 자리에 **소스가 실제로 있어야** 계약을
            # 채택한 저장소다.
            adopted = root / "src" / "core" / "domain" / "billing"
            adopted.mkdir(parents=True)
            (adopted / "invoice.py").write_text("value = 1\n", encoding="utf-8")

            python_wrong = root / "src" / "core" / "wrong" / "thing.py"
            python_wrong.parent.mkdir(parents=True, exist_ok=True)
            python_wrong.write_text("value = 1\n", encoding="utf-8")
            python_wrong_findings = lint_project(root, "python", files=[str(python_wrong.relative_to(root))])
            self.assertIn("path is outside profile architecture role mapping", "\n".join(f.message for f in python_wrong_findings))

            unmanaged = root / "components" / "Button.tsx"
            unmanaged.parent.mkdir(parents=True, exist_ok=True)
            unmanaged.write_text("export function Button() { return null }\n", encoding="utf-8")
            unmanaged_findings = lint_project(root, "nextjs", files=[str(unmanaged.relative_to(root))])
            self.assertEqual(unmanaged_findings, [])
            self.assertEqual(lint_project(root, "android", files=["settings.gradle.kts"]), [])

            managed_outside_role = root / "src" / "core" / "wrong" / "Thing.ts"
            managed_outside_role.parent.mkdir(parents=True, exist_ok=True)
            managed_outside_role.write_text("export const thing = 1\n", encoding="utf-8")
            managed_findings = lint_project(root, "nextjs", files=[str(managed_outside_role.relative_to(root))])
            self.assertIn("path is outside profile architecture role mapping", "\n".join(f.message for f in managed_findings))

            web_entity = root / "src" / "core" / "domain" / "orders" / "OrderEntity.ts"
            web_entity.parent.mkdir(parents=True, exist_ok=True)
            web_entity.write_text("export class OrderEntity {}\n", encoding="utf-8")
            (root / "src" / "core" / "data" / "orders").mkdir(parents=True)
            self.assertEqual(lint_project(root, "nextjs", files=[str(web_entity.relative_to(root))]), [])

            web_presentation_dto = root / "src" / "features" / "checkout" / "presentation" / "CheckoutScreen.tsx"
            web_presentation_dto.parent.mkdir(parents=True, exist_ok=True)
            web_presentation_dto.write_text('import { CheckoutDTO } from "../../data/checkout/CheckoutDTO"\n', encoding="utf-8")
            (root / "src" / "features" / "checkout" / "api").mkdir(parents=True, exist_ok=True)
            web_presentation_findings = lint_project(root, "nextjs", files=[str(web_presentation_dto.relative_to(root))])
            self.assertIn("feature-presentation contains forbidden token Dto", "\n".join(f.message for f in web_presentation_findings))

            # 필수 pre-commit gate의 오탐 회귀. `useIdentity`는 `Entity`를 부분문자열로
            # 포함해 samantha 프로덕션 소스 70건을 위반으로 잡고 커밋을 막았다.
            web_presentation_ok = root / "src" / "features" / "checkout" / "presentation" / "useIdentity.ts"
            web_presentation_ok.write_text("export const identity = 1\n", encoding="utf-8")
            self.assertEqual(lint_project(root, "nextjs", files=[str(web_presentation_ok.relative_to(root))]), [])

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            android_file = root / "core" / "domain" / "chat" / "src" / "main" / "java" / "com" / "example" / "app" / "core" / "domain" / "chat" / "Chat.kt"
            android_file.parent.mkdir(parents=True, exist_ok=True)
            android_file.write_text("package com.example.app.core.domain.chat\nclass Chat\n", encoding="utf-8")
            (root / "core" / "data" / "chat").mkdir(parents=True)
            partitioned = lint_profiles(root, ["android", "react-native"], files=[str(android_file.relative_to(root))])
            self.assertEqual(partitioned["android"], [])
            self.assertEqual(partitioned["react-native"], [])

            rn_screen = root / "src" / "features" / "checkout" / "presentation" / "Checkout.tsx"
            rn_screen.parent.mkdir(parents=True, exist_ok=True)
            rn_screen.write_text("export function Checkout() { return null }\n", encoding="utf-8")
            (root / "src" / "features" / "checkout" / "api").mkdir(parents=True)
            rn_partitioned = lint_profiles(root, ["android", "react-native"], files=[str(rn_screen.relative_to(root))])
            self.assertEqual(rn_partitioned["android"], [])
            self.assertEqual(rn_partitioned["react-native"], [])

            rn_android = root / "android" / "app" / "src" / "main" / "java" / "com" / "example" / "MainApplication.kt"
            rn_android.parent.mkdir(parents=True, exist_ok=True)
            rn_android.write_text("package com.example\nclass MainApplication\n", encoding="utf-8")
            self.assertEqual(lint_profiles(root, ["react-native"], files=[str(rn_android.relative_to(root))]), {"react-native": [], "android": []})

            rn_bad = root / "android" / "app" / "src" / "main" / "java" / "com" / "example" / "CheckoutDTO.kt"
            rn_bad.write_text("package com.example\nclass CheckoutDTO\n", encoding="utf-8")
            rn_bad_findings = lint_profiles(root, ["react-native"], files=[str(rn_bad.relative_to(root))])
            self.assertIn("app-shell contains forbidden token Dto", "\n".join(f.message for f in rn_bad_findings["android"]))

            outside = root / "components" / "Button.tsx"
            outside.parent.mkdir(parents=True, exist_ok=True)
            outside.write_text("export function Button() { return null }\n", encoding="utf-8")
            partitioned_outside = lint_profiles(root, ["nextjs", "android"], files=[str(outside.relative_to(root))])
            self.assertTrue(all(not findings for findings in partitioned_outside.values()))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rn_entity = root / "src" / "core" / "domain" / "orders" / "OrderEntity.ts"
            rn_entity.parent.mkdir(parents=True, exist_ok=True)
            rn_entity.write_text("export class OrderEntity {}\n", encoding="utf-8")
            (root / "src" / "core" / "data" / "orders").mkdir(parents=True)
            self.assertEqual(lint_project(root, "react-native", files=[str(rn_entity.relative_to(root))]), [])

            rn_bad = root / "src" / "features" / "checkout" / "presentation" / "CheckoutScreen.tsx"
            rn_bad.parent.mkdir(parents=True, exist_ok=True)
            rn_bad.write_text('import { CheckoutEntity } from "../../data/checkout/CheckoutEntity"\n', encoding="utf-8")
            (root / "src" / "features" / "checkout" / "api").mkdir(parents=True, exist_ok=True)
            rn_bad_findings = lint_project(root, "react-native", files=[str(rn_bad.relative_to(root))])
            self.assertIn("feature-presentation contains forbidden token Entity", "\n".join(f.message for f in rn_bad_findings))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ios_entity = root / "Sources" / "Core" / "Domain" / "Orders" / "OrderEntity.swift"
            ios_entity.parent.mkdir(parents=True, exist_ok=True)
            ios_entity.write_text("struct OrderEntity {}\n", encoding="utf-8")
            (root / "Sources" / "Core" / "Data" / "Orders").mkdir(parents=True)
            self.assertEqual(lint_project(root, "ios", files=[str(ios_entity.relative_to(root))]), [])

            ios_bad = root / "Sources" / "Features" / "Checkout" / "Presentation" / "CheckoutView.swift"
            ios_bad.parent.mkdir(parents=True, exist_ok=True)
            ios_bad.write_text("struct CheckoutDTOView {}\n", encoding="utf-8")
            (root / "Sources" / "Features" / "Checkout" / "API").mkdir(parents=True, exist_ok=True)
            ios_bad_findings = lint_project(root, "ios", files=[str(ios_bad.relative_to(root))])
            self.assertIn("feature-presentation contains forbidden token DTO", "\n".join(f.message for f in ios_bad_findings))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            py_entity = root / "src" / "core" / "domain" / "orders" / "order_entity.py"
            py_entity.parent.mkdir(parents=True, exist_ok=True)
            py_entity.write_text("class OrderEntity: pass\n", encoding="utf-8")
            (root / "src" / "core" / "data" / "orders").mkdir(parents=True)
            self.assertEqual(lint_project(root, "python", files=[str(py_entity.relative_to(root))]), [])

            py_bad = root / "src" / "features" / "checkout" / "presentation" / "checkout_view.py"
            py_bad.parent.mkdir(parents=True, exist_ok=True)
            py_bad.write_text("from src.core.data.checkout.dto import CheckoutDTO\n", encoding="utf-8")
            (root / "src" / "features" / "checkout" / "api").mkdir(parents=True, exist_ok=True)
            py_bad_findings = lint_project(root, "python", files=[str(py_bad.relative_to(root))])
            self.assertIn("feature-presentation contains forbidden token DTO", "\n".join(f.message for f in py_bad_findings))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            subprocess.run(("git", "init", "-q", "-b", "main"), cwd=root, check=True)
            untracked = root / "core" / "domain" / "chat" / "New.kt"
            untracked.parent.mkdir(parents=True, exist_ok=True)
            untracked.write_text("class New\n", encoding="utf-8")
            self.assertIn("core/domain/chat/New.kt", changed_files(root))

    def test_python_runner_fix_loop_cap_counts_all_rejection_routes_to_collector(self) -> None:
        from agent_flow.artifact import read_meta, write_meta
        from agent_flow.runner import Phase, Runner

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            write_meta(run_dir, {})
            runner = Runner.__new__(Runner)
            runner.run_dir = run_dir
            # gates routes several rejection keys to fix-loop, so fix-loop is a fix
            # collector; every gate-failure key (not only request-changes) then counts
            # as one round, which is what bounds the documented gate-retry loop.
            runner.phases = [
                Phase(id="implement", description=""),
                Phase(id="gates", description="", routes={
                    "request-changes": "fix-loop", "blocked": "fix-loop",
                    "error": "fix-loop", "default": "fix-loop", "green": "commit",
                }),
                Phase(id="fix-loop", description="", routes={"default": "implement"}),
                Phase(id="commit", description=""),
            ]
            gates = runner.phases[1]

            # request-changes, then blocked, then default (passed:true but unproven):
            # each is a distinct gate-failure key that still routes to the collector.
            rejections = (
                '{"passed": false}',
                '{"passed": false, "status": "blocked"}',
                '{"passed": true, "results": []}',
            )
            for expected_round, content in enumerate(rejections, start=1):
                (run_dir / "gates.md").write_text(content, encoding="utf-8")
                self.assertEqual(runner._next_index(1, gates)[:2], (2, False))
                self.assertEqual(read_meta(run_dir)["fix_loop_rounds"]["fix-loop"], expected_round)

            # the fourth rejection (any key) is blocked for user intervention.
            (run_dir / "gates.md").write_text('{"passed": false, "status": "error"}', encoding="utf-8")
            self.assertEqual(runner._next_index(1, gates)[:2], (1, True))
            self.assertEqual(read_meta(run_dir)["fix_loop_rounds"]["fix-loop"], 3)

    def test_python_runner_fix_loop_cap_bounds_renamed_loops_and_skips_pr_loop(self) -> None:
        from agent_flow.artifact import read_meta, write_meta
        from agent_flow.runner import Phase, Runner

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            write_meta(run_dir, {})
            runner = Runner.__new__(Runner)
            runner.run_dir = run_dir
            # request-changes marks refactor and implement-fix as fix collectors, so
            # both renamed loops are bounded per target. pr-watch is only ever a
            # "default" target (the PR event loop), so it is not a collector and that
            # loop is never capped.
            runner.phases = [
                Phase(id="refactor", description=""),
                Phase(id="implement-fix", description=""),
                Phase(id="review", description="", routes={"request-changes": "implement-fix", "approve": "qa"}),
                Phase(id="architecture-review", description="", routes={"request-changes": "refactor", "approve": "gates"}),
                Phase(id="qa", description=""),
                Phase(id="gates", description=""),
                Phase(id="pr-watch", description="", routes={"comments": "pr-comment-fix", "green": "merge"}),
                Phase(id="pr-comment-fix", description="", routes={"default": "pr-watch"}),
                Phase(id="merge", description=""),
            ]
            review = runner.phases[2]
            architecture_review = runner.phases[3]
            pr_comment_fix = runner.phases[7]

            for expected_round in (1, 2, 3):
                (run_dir / "review.md").write_text("verdict: request-changes\n", encoding="utf-8")
                self.assertEqual(runner._next_index(2, review)[:2], (1, False))
                self.assertEqual(read_meta(run_dir)["fix_loop_rounds"]["implement-fix"], expected_round)
                (run_dir / "architecture-review.md").write_text("verdict: request-changes\n", encoding="utf-8")
                self.assertEqual(runner._next_index(3, architecture_review)[:2], (0, False))
                self.assertEqual(read_meta(run_dir)["fix_loop_rounds"]["refactor"], expected_round)

            # three rounds on each target coexist; the fourth on either blocks it.
            (run_dir / "review.md").write_text("verdict: request-changes\n", encoding="utf-8")
            self.assertEqual(runner._next_index(2, review)[:2], (2, True))
            (run_dir / "architecture-review.md").write_text("verdict: request-changes\n", encoding="utf-8")
            self.assertEqual(runner._next_index(3, architecture_review)[:2], (3, True))

            # pr-watch is never a rejection target, so the PR event loop is uncapped.
            for _ in range(6):
                self.assertEqual(runner._next_index(7, pr_comment_fix)[:2], (6, False))
            self.assertNotIn("pr-watch", read_meta(run_dir).get("fix_loop_rounds", {}))

    def test_python_runner_fix_loop_cap_migrates_legacy_integer_count(self) -> None:
        from agent_flow.artifact import read_meta, write_meta
        from agent_flow.runner import Phase, Runner

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            runner = Runner.__new__(Runner)
            runner.run_dir = run_dir
            runner.phases = [
                Phase(id="implement", description=""),
                Phase(id="gates", description="", routes={"request-changes": "fix-loop", "green": "commit"}),
                Phase(id="fix-loop", description="", routes={"default": "implement"}),
                Phase(id="commit", description=""),
            ]
            gates = runner.phases[1]

            # A run upgraded mid fix-loop stored fix_loop_rounds as a bare int (the
            # old format counted only literal "fix-loop" entries). It migrates to the
            # per-target count and keeps counting rather than resetting.
            write_meta(run_dir, {"fix_loop_rounds": 1})
            (run_dir / "gates.md").write_text('{"passed": false}', encoding="utf-8")
            self.assertEqual(runner._next_index(1, gates)[:2], (2, False))
            self.assertEqual(read_meta(run_dir)["fix_loop_rounds"]["fix-loop"], 2)

            # A legacy int already at the cap still blocks the next round; the
            # upgrade must not hand it three fresh rounds.
            write_meta(run_dir, {"fix_loop_rounds": 3})
            (run_dir / "gates.md").write_text('{"passed": false}', encoding="utf-8")
            self.assertEqual(runner._next_index(1, gates)[:2], (1, True))

    def test_python_runner_uses_default_route_like_node_runner(self) -> None:
        from agent_flow.runner import Phase, Runner

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            runner = Runner.__new__(Runner)
            runner.run_dir = run_dir
            runner.phases = [
                Phase(id="fix-loop", description="", routes={"default": "gates"}),
                Phase(id="gates", description=""),
            ]
            (run_dir / "fix-loop.md").write_text("status: done\n", encoding="utf-8")

            self.assertEqual(runner._next_index(0, runner.phases[0])[:2], (1, False))

    def test_source_profiles_use_argv_command_lists(self) -> None:
        import yaml

        profiles_root = Path(__file__).resolve().parents[1] / "src" / "agent_flow" / "profiles"
        for profile_path in profiles_root.glob("*.yaml"):
            if profile_path.name.startswith("_"):
                continue
            payload = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
            for gate in payload.get("gates", []):
                command = gate.get("command")
                # source profile도 packaged profile과 같은 subprocess argv 계약을 따른다.
                self.assertIsInstance(command, list, profile_path.name)
                self.assertTrue(command, profile_path.name)
                self.assertTrue(all(isinstance(part, str) and part for part in command), profile_path.name)

    def test_node_installer_initializes_current_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            result = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"),
                    "install",
                ),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.strip(),
                f"agent-flow installed profile=generic root={project_root.resolve()}",
            )
            kit = json.loads((project_root / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
            self.assertEqual(kit["profile"], "generic")
            self.assertEqual(kit["install_scope"], "project")
            self.assertNotIn("graphify", kit)
            self.assertTrue((project_root / ".agent-flow" / "runs").is_dir())
            self.assertTrue((project_root / ".agent-flow" / "workflows" / "full-feature.yaml").is_file())
            self.assertTrue((project_root / ".agent-flow" / "bootstrap" / "AGENTS.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "bootstrap" / "CLAUDE.md").is_file())
            runtime = project_root / ".agent-flow" / "runtime" / "python"
            self.assertTrue((runtime / "agent_flow" / "core" / "architecture_lint.py").is_file())
            runtime_env = {**os.environ, "PYTHONPATH": str(runtime)}
            lint_result = subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "agent_flow.core.architecture_lint",
                    "--root",
                    str(project_root),
                    "--profile",
                    "generic",
                ),
                cwd=project_root,
                env=runtime_env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(lint_result.returncode, 0, lint_result.stderr)
            run_dir = project_root / ".agent-flow" / "runs" / "runtime-check"
            gates_result = subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "agent_flow.cli",
                    "gates",
                    "--root",
                    str(project_root),
                    "--profile",
                    "generic",
                    "--run-dir",
                    str(run_dir),
                    "--phase",
                    "all",
                ),
                cwd=project_root,
                env=runtime_env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(gates_result.returncode, 0, gates_result.stderr)
            self.assertIn("generic:", gates_result.stdout)
            gate_payload = json.loads((run_dir / "artifacts" / "gate-results.json").read_text(encoding="utf-8"))
            self.assertTrue(gate_payload["passed"])
            gate_payload_text = json.dumps(gate_payload)
            self.assertIn("agent_flow.core.architecture_lint", gate_payload_text)
            self.assertNotIn("No module named 'agent_flow'", gate_payload_text)
            architecture_result = next(result for result in gate_payload["results"] if result["gate_id"] == "architecture-lint")
            self.assertTrue(architecture_result["passed"])
            self.assertTrue(
                (
                    project_root
                    / ".agent-flow"
                    / "skills"
                    / "full-feature-workflow"
                    / "SKILL.md"
                ).is_file()
            )
            self.assertTrue((project_root / ".agent-flow" / "prompts" / "prd.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "prompts" / "domain-grill.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "prompts" / "product-brief.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "prompts" / "plan-review.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "prompts" / "ddd-design.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "prompts" / "architecture-review.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "prompts" / "pr-watch.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "prompts" / "merge.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "domain-modeling" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "grill-with-docs" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "product-brief" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "plan-reviewer" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "clean-architecture-core" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "clean-architecture" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "android-clean-architecture" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "ios-clean-architecture" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "react-clean-architecture" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "react-native-clean-architecture" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "python-api-clean-architecture" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "architecture-reviewer" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "android-code-review" / "SKILL.md").is_file())
            self.assertFalse((project_root / ".agent-flow" / "skills" / "android-mvi-feature").exists())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "android-module-creator").exists())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "android-debugging").exists())
            self.assertFalse((project_root / ".agent-flow" / "skills" / "graphify").exists())
            self.assertTrue(
                (
                    project_root
                    / ".agent-flow"
                    / "skills"
                    / "android-guides"
                    / "references"
                    / "architecture-rules-guide.md"
                ).is_file()
            )
            self.assertTrue((project_root / ".agent-flow" / "prompts" / "push-watch.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "prompts" / "push-watch-tick.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "push-watch" / "SKILL.md").is_file())
            self.assertTrue(
                (project_root / ".agent-flow" / "templates" / "_shared" / "review" / "architecture-design.md").is_file()
            )
            # stage prompt 렌더러와 그 자산은 삭제됐다. 설치는 `templates` 트리를
            # 통째로 복사하므로, 자산이 되살아나면 죽은 파일이 다시 깔린다.
            self.assertFalse((project_root / ".agent-flow" / "templates" / "generic" / "stage.md").exists())
            self.assertFalse((project_root / ".agent-flow" / "templates" / "omp" / "stage.md").exists())
            self.assertTrue((project_root / ".Codex" / "agents" / "code-reviewer.md").is_file())
            code_reviewer = (project_root / ".Codex" / "agents" / "code-reviewer.md").read_text(encoding="utf-8")
            self.assertTrue((project_root / ".claude" / "agents" / "code-reviewer.md").is_file())
            claude_code_reviewer = (project_root / ".claude" / "agents" / "code-reviewer.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(code_reviewer, _strip_markdown_frontmatter(claude_code_reviewer))
            self.assertIn("name: code-reviewer", claude_code_reviewer)
            self.assertTrue((project_root / ".Codex" / "context" / "tree.jsonl").is_file())
            self.assertIn("verdict: approve | request-changes", code_reviewer)
            self.assertIn("project-local-skills: checked|n/a", code_reviewer)
            self.assertIn("dependency-rule: pass|fail", code_reviewer)
            self.assertIn("repository-boundary: pass|fail", code_reviewer)
            self.assertIn(
                "verdict: approve",
                (project_root / ".agent-flow" / "prompts" / "plan-review.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "code-generation-discipline",
                (project_root / ".agent-flow" / "skills" / "full-feature-workflow" / "SKILL.md").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertTrue(
                (project_root / ".agent-flow" / "skills" / "code-generation-discipline" / "SKILL.md").is_file()
            )
            self.assertTrue(
                (project_root / ".agent-flow" / "skills" / "comment-authoring-discipline" / "SKILL.md").is_file()
            )
            self.assertTrue((project_root / ".agent-flow" / "skills" / "comment-checker" / "SKILL.md").is_file())
            expected_comment_checker = (
                f"'{project_root.resolve() / '.agent-flow' / 'bin' / 'agent-flow-hook'}' "
                f"'{project_root.resolve() / '.agent-flow' / 'scripts' / 'hooks' / 'comment-checker.py'}'"
            )
            for hooks_path in (
                project_root / ".Codex" / "hooks.json",
                project_root / ".codex" / "hooks.json",
            ):
                self.assertTrue(hooks_path.is_file())
                codex_hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
                codex_hook_commands = [
                    hook["command"]
                    for entry in codex_hooks["hooks"]["PostToolUse"]
                    for hook in entry["hooks"]
                ]
                self.assertIn(expected_comment_checker, codex_hook_commands)
                self.assertNotIn(str(Path(__file__).resolve().parents[1]), "\n".join(codex_hook_commands))
                self.assertNotIn("UserPromptSubmit", codex_hooks["hooks"])
            omp_extension = project_root / ".omp" / "extensions" / "agent-flow-hooks.ts"
            self.assertTrue(omp_extension.is_file())
            omp_extension_text = omp_extension.read_text(encoding="utf-8")
            self.assertNotIn("guard-worktree.sh", omp_extension_text)
            self.assertIn("guard-protected-branch.sh", omp_extension_text)
            self.assertIn("guard-host-worktree.sh", omp_extension_text)
            self.assertNotIn("guard-spec-approval.sh", omp_extension_text)
            self.assertNotIn("prepare-spec-user-prompt.py", omp_extension_text)
            self.assertIn("comment-checker.py", omp_extension_text)
            self.assertIn("show-phase-status.sh", omp_extension_text)
            self.assertNotIn("confirm-spec-user-prompt.py", omp_extension_text)
            self.assertIn("bind-host-worktree.py", omp_extension_text)
            self.assertNotIn('pi.on("input"', omp_extension_text)
            self.assertNotIn('pi.on("before_agent_start"', omp_extension_text)
            self.assertIn("export default function agentFlowHooks", omp_extension_text)
            self.assertIn("session_shutdown", omp_extension_text)
            self.assertIn('pi.on("context"', omp_extension_text)
            self.assertIn('message?.customType === "agent-flow-model-context"', omp_extension_text)
            self.assertIn('message?.details?.source === "agent-flow-omp-model-context"', omp_extension_text)
            self.assertIn('message?.role === "user"', omp_extension_text)
            self.assertIn('text.startsWith("<context>")', omp_extension_text)
            self.assertIn('/<file\\b[^>]*\\bsource="agent-flow-omp-model-context"/.test(text)', omp_extension_text)
            self.assertNotIn("modelSpecificProjectContext", omp_extension_text)
            self.assertNotIn("contextMessage(", omp_extension_text)
            self.assertNotIn("content.trimEnd()", omp_extension_text)
            # 루트 두 파일의 블록은 install이 같은 템플릿으로 관리하고 블록 밖은
            # 프로젝트 소유 산문이다. 전체 파일 미러는 그 산문까지 덮어쓴다.
            self.assertNotIn("syncRootContextFiles", omp_extension_text)
            self.assertNotIn("CLAUDE.md", omp_extension_text)
            self.assertNotIn(str(Path(__file__).resolve().parents[1]), omp_extension_text)
            self.assertTrue((project_root / ".omp" / "skills" / "agent-flow" / "SKILL.md").exists())
            self.assertTrue(
                os.access(project_root / ".agent-flow" / "scripts" / "hooks" / "comment-checker.py", os.X_OK)
            )
            self.assertTrue(
                os.access(
                    project_root / ".agent-flow" / "scripts" / "hooks" / "guard-host-worktree.sh",
                    os.X_OK,
                )
            )
            self.assertTrue(
                os.access(
                    project_root / ".agent-flow" / "scripts" / "hooks" / "bind-host-worktree.py",
                    os.X_OK,
                )
            )
            self.assertFalse((project_root / "scripts" / "hooks" / "comment-checker.py").exists())
            self.assertTrue(
                (project_root / ".agent-flow" / "skills" / "agent-flow-concise-output" / "SKILL.md").is_file()
            )
            concise_rule = project_root / ".Codex" / "rules" / "concise-output.md"
            self.assertTrue(concise_rule.is_file())
            self.assertIn("verdict: approve", concise_rule.read_text(encoding="utf-8"))
            self.assertIn("verdict: request-changes", concise_rule.read_text(encoding="utf-8"))
            self.assertIn("next_command", concise_rule.read_text(encoding="utf-8"))
            self.assertTrue(
                (project_root / ".agent-flow" / "skills" / "react-development-guide" / "SKILL.md").is_file()
            )
            self.assertTrue(
                (project_root / ".agent-flow" / "skills" / "react-native-development-guide" / "SKILL.md").is_file()
            )
            # 이 이름의 정본은 bootstrap 산문이 아니라 installer가 만드는 skill
            # index다. 산문 사본을 지운 뒤에도 always skill 계약이 실제로
            # 프로젝트 컨텍스트에 도착하는지를 그 index에서 확인한다.
            self.assertIn(
                "always:{code-generation-discipline",
                (project_root / "AGENTS.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "requires at least two installed Claude/Codex CLI reviewer subprocesses",
                (project_root / ".agent-flow" / "bootstrap" / "AGENTS.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "status: ci-failed",
                (project_root / ".agent-flow" / "prompts" / "pr-watch.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "merge requires explicit approval",
                (project_root / ".agent-flow" / "skills" / "push-watch" / "SKILL.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                'agent-flow run "<task>"',
                (project_root / "AGENTS.md").read_text(encoding="utf-8"),
            )
            claude_root = (project_root / "CLAUDE.md").read_text(encoding="utf-8")
            # 계약은 `AGENTS.md` 한 벌이고 `CLAUDE.md`는 그것을 가리킨다. Claude CLI는
            # 루트 `CLAUDE.md`만 자동 로드하므로 이 import가 유일한 전달 경로다.
            # 본문까지 여기 심으면 `@path`가 파일 전체를 끌어오므로 두 번 받는다.
            self.assertIn("@AGENTS.md", claude_root)
            self.assertNotIn("### Workflow Contract", claude_root)
            self.assertNotIn("[agent-flow skill index]", claude_root)
            # `AGENTS.md`에는 없어야 한다 — 자기 자신을 import하는 줄이다.
            self.assertNotIn("@AGENTS.md", (project_root / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertIn(
                "requires at least two installed Claude/Codex CLI reviewer subprocesses",
                (project_root / "AGENTS.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                'agent-flow run "<task>"',
                (project_root / ".agent-flow" / "bootstrap" / "AGENTS.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "requires at least two installed Claude/Codex CLI reviewer subprocesses",
                (project_root / "AGENTS.md").read_text(encoding="utf-8"),
            )
            agent_flow_skill = (project_root / ".agent-flow" / "skills" / "agent-flow" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("Treat the status command output as the only source of truth.", agent_flow_skill)
            self.assertIn("Do not run install just because a new session started.", agent_flow_skill)

    def test_node_installers_write_the_managed_launcher(self) -> None:
        """install이 심는 launcher가 고정된 managed Python CLI를 실행한다."""
        for installer in ("agent-flow-kit.mjs", "agent-flow-install.mjs"):
            with self.subTest(installer=installer):
                with tempfile.TemporaryDirectory() as temp_dir:
                    project_root = Path(temp_dir) / "project"
                    project_root.mkdir()
                    result = subprocess.run(
                        (
                            _node_executable(),
                            str(Path(__file__).resolve().parents[1] / "bin" / installer),
                            "install",
                        ),
                        cwd=project_root,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    launcher = project_root / ".agent-flow" / "bin" / "agent-flow"
                    self.assertTrue(launcher.is_file())
                    mode = launcher.stat().st_mode
                    self.assertTrue(mode & stat.S_IXUSR)
                    self.assertFalse(mode & (stat.S_IWGRP | stat.S_IWOTH))
                    kit = json.loads(
                        (project_root / ".agent-flow" / "kit.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(
                        kit["project_launcher_digest"],
                        hashlib.sha256(launcher.read_bytes()).hexdigest(),
                    )
                    launched = subprocess.run(
                        (str(launcher), "spec", "confirm", "--help"),
                        cwd=temp_dir,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(launched.returncode, 0, launched.stderr)
                    self.assertIn("spec confirm", launched.stdout)
                    source = launcher.read_text(encoding="utf-8")
                    # 실행할 인터프리터는 install이 정한 절대경로로 고정한다.
                    # 실행 시점의 PATH나 환경으로 runtime을 바꿀 수 없어야 한다.
                    self.assertNotIn("AGENT_FLOW_PYTHON", source)
                    self.assertNotIn("command -v", source)
                    self.assertRegex(source, r"\npython='/[^']+'\n")
                    # 상속 환경으로 실행 대상을 바꿀 수 없어야 한다: PYTHONPATH에
                    # 가짜 agent_flow를 심어도 관리 runtime이 이긴다.
                    decoy = Path(temp_dir) / "decoy"
                    (decoy / "agent_flow").mkdir(parents=True)
                    (decoy / "agent_flow" / "__init__.py").write_text("", encoding="utf-8")
                    (decoy / "agent_flow" / "cli.py").write_text(
                        "def main(argv=None):\n    print('decoy')\n    return 0\n",
                        encoding="utf-8",
                    )
                    hijacked = subprocess.run(
                        (str(launcher), "spec", "confirm", "--help"),
                        cwd=temp_dir,
                        env={**os.environ, "PYTHONPATH": str(decoy)},
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(hijacked.returncode, 0, hijacked.stderr)
                    self.assertIn("spec confirm", hijacked.stdout)
                    self.assertNotIn("decoy", hijacked.stdout)
                    kit_python = kit["project_launcher_python"]
                    self.assertEqual(
                        kit_python["sha256"],
                        hashlib.sha256(Path(kit_python["path"]).read_bytes()).hexdigest(),
                    )
                    # user-site를 끄는 `-I`가 가능하면 그걸 써야 한다. `-E`는
                    # user-site를 남겨 `usercustomize`가 이 프로세스에서 돈다.
                    isolated = subprocess.run(
                        (kit_python["path"], "-I", "-c", "import yaml"),
                        capture_output=True,
                        check=False,
                    )
                    if isolated.returncode == 0:
                        self.assertEqual(kit_python["flag"], "-I")
                        self.assertIn(" -I -c ", source)
                    # managed CLI의 cwd는 쓰기 가능한 checkout이다. `-c`가
                    # sys.path에 남기는 cwd를 지우지 않으면 저장소의 `argparse.py`
                    # 같은 파일이 CLI 프로세스에서 실행된다.
                    (project_root / "argparse.py").write_text(
                        "import sys\n"
                        "sys.stderr.write('cwd-shadow ran\\n')\n"
                        "raise SystemExit(0)\n",
                        encoding="utf-8",
                    )
                    unisolated = launcher.parent / "agent-flow-unisolated"
                    unisolated.write_text(
                        source.replace(
                            f" {kit_python['flag']} -c ", " -E -c "
                        ),
                        encoding="utf-8",
                    )
                    unisolated.chmod(0o755)
                    for variant in (launcher, unisolated):
                        shadowed = subprocess.run(
                            (str(variant), "spec", "confirm", "--help"),
                            cwd=project_root,
                            text=True,
                            capture_output=True,
                            check=False,
                        )
                        self.assertNotIn("cwd-shadow ran", shadowed.stderr)
                        self.assertEqual(shadowed.returncode, 0, shadowed.stderr)
                        self.assertIn("spec confirm", shadowed.stdout)

    def test_managed_hooks_route_through_portable_launcher(self) -> None:
        """hook 실행이 하드코딩 /usr/bin/python3·/bin/bash 대신 install이 pin한
        managed python을 태우는 portable launcher alias로만 이뤄진다 (hook exec EPERM)."""
        for installer in ("agent-flow-kit.mjs", "agent-flow-install.mjs"):
            with self.subTest(installer=installer):
                with tempfile.TemporaryDirectory() as temp_dir:
                    project_root = Path(temp_dir) / "project"
                    project_root.mkdir()
                    result = subprocess.run(
                        (
                            _node_executable(),
                            str(Path(__file__).resolve().parents[1] / "bin" / installer),
                            "install",
                        ),
                        cwd=project_root,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    hook_launcher = project_root / ".agent-flow" / "bin" / "agent-flow-hook"
                    self.assertTrue(hook_launcher.is_file(), "managed hook launcher missing")
                    mode = hook_launcher.stat().st_mode
                    self.assertTrue(mode & stat.S_IXUSR)
                    self.assertFalse(mode & (stat.S_IWGRP | stat.S_IWOTH))
                    kit = json.loads(
                        (project_root / ".agent-flow" / "kit.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(
                        kit["hook_launcher_digest"],
                        hashlib.sha256(hook_launcher.read_bytes()).hexdigest(),
                    )
                    # 관리 hook은 CLI의 `-E` fallback을 물려받지 않고 항상 `-I`로 실행한다:
                    # user-site의 sitecustomize/usercustomize가 보호 hook을 선점하지 못하게.
                    launcher_src = hook_launcher.read_text(encoding="utf-8")
                    self.assertIn('exec "$python" -I "$script"', launcher_src)
                    self.assertNotIn("${flag}", launcher_src)
                    settings = json.loads(
                        (project_root / ".claude" / "settings.json").read_text(encoding="utf-8")
                    )
                    commands = [
                        hook["command"]
                        for blocks in settings["hooks"].values()
                        for block in blocks
                        for hook in block["hooks"]
                    ]
                    self.assertTrue(commands)
                    alias = str(hook_launcher)
                    for command in commands:
                        self.assertIn(alias, command, command)
                        self.assertFalse(
                            command.startswith("/usr/bin/python3 ")
                            or command.startswith("/bin/bash "),
                            f"hook still hardcodes an absolute interpreter: {command}",
                        )
                    # launcher가 태우는 인터프리터는 install이 pin한 managed python이다 —
                    # /usr/bin/python3 존재 여부와 무관하다.
                    probe = Path(temp_dir) / "probe.py"
                    probe.write_text("import sys; print(sys.executable)\n", encoding="utf-8")
                    launched = subprocess.run(
                        (str(hook_launcher), str(probe)),
                        cwd=temp_dir,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(launched.returncode, 0, launched.stderr)
                    self.assertEqual(
                        Path(launched.stdout.strip()).resolve(),
                        Path(kit["project_launcher_python"]["path"]).resolve(),
                    )
                    # shell hook 내부의 python 호출도 pin된 인터프리터를 쓴다.
                    for shell_hook in (
                        "guard-host-worktree.sh",
                        "guard-protected-branch.sh",
                        "show-phase-status.sh",
                    ):
                        text = (
                            project_root / ".agent-flow" / "scripts" / "hooks" / shell_hook
                        ).read_text(encoding="utf-8")
                        self.assertNotIn("/usr/bin/python3", text, shell_hook)
                        self.assertIn("AGENT_FLOW_HOOK_PYTHON", text, shell_hook)

    def test_node_installer_writes_cwd_independent_hook_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project with space"
            project_root.mkdir()
            settings_path = project_root / ".claude" / "settings.json"
            settings_path.parent.mkdir()
            settings_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": "Bash",
                                    "hooks": [
                                        {"type": "command", "command": "scripts/hooks/guard-protected-branch.sh"},
                                    ],
                                }
                            ],
                            "Stop": [
                                {
                                    "matcher": "",
                                    "hooks": [{"type": "command", "command": "scripts/hooks/show-phase-status.sh"}],
                                }
                            ],
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            node = _node_executable()
            result = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"),
                    "install",
                ),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            commands = [
                hook["command"]
                for event in ("PreToolUse", "PostToolUse", "Stop")
                for entry in settings["hooks"][event]
                for hook in entry["hooks"]
            ]
            resolved_root = project_root.resolve()
            hook_launcher = resolved_root / ".agent-flow" / "bin" / "agent-flow-hook"
            expected = [
                f"'{hook_launcher}' '{resolved_root / '.agent-flow' / 'scripts' / 'hooks' / 'guard-protected-branch.sh'}'",
                f"'{hook_launcher}' '{resolved_root / '.agent-flow' / 'scripts' / 'hooks' / 'guard-host-worktree.sh'}'",
                f"'{hook_launcher}' '{resolved_root / '.agent-flow' / 'scripts' / 'hooks' / 'comment-checker.py'}'",
                f"'{hook_launcher}' '{resolved_root / '.agent-flow' / 'scripts' / 'hooks' / 'guard-host-worktree.sh'}'",
                f"'{hook_launcher}' '{resolved_root / '.agent-flow' / 'scripts' / 'hooks' / 'record-skill-read.py'}'",
                f"'{hook_launcher}' '{resolved_root / '.agent-flow' / 'scripts' / 'hooks' / 'record-command-run.py'}'",
                f"'{hook_launcher}' '{resolved_root / '.agent-flow' / 'scripts' / 'hooks' / 'bind-host-worktree.py'}'",
                f"'{hook_launcher}' '{resolved_root / '.agent-flow' / 'scripts' / 'hooks' / 'guard-host-worktree.sh'}'",
                f"'{hook_launcher}' '{resolved_root / '.agent-flow' / 'scripts' / 'hooks' / 'worktree-tripwire.py'}'",
                f"'{hook_launcher}' '{resolved_root / '.agent-flow' / 'scripts' / 'hooks' / 'show-phase-status.sh'}'",
            ]
            self.assertEqual(commands, expected)
            stop_hook = subprocess.run(
                ("/bin/sh", "-c", commands[-1]),
                cwd=temp_dir,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(stop_hook.returncode, 0, stop_hook.stderr)

    def test_legacy_node_installer_writes_claude_comment_checker_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "legacy project with space"
            project_root.mkdir()
            node = _node_executable()
            result = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-install.mjs"),
                    "install",
                ),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            settings = json.loads((project_root / ".claude" / "settings.json").read_text(encoding="utf-8"))
            commands = [
                hook["command"]
                for event in ("PreToolUse", "PostToolUse", "Stop")
                for entry in settings["hooks"][event]
                for hook in entry["hooks"]
            ]
            expected_checker = (
                f"'{project_root.resolve() / '.agent-flow' / 'bin' / 'agent-flow-hook'}' "
                f"'{project_root.resolve() / '.agent-flow' / 'scripts' / 'hooks' / 'comment-checker.py'}'"
            )
            self.assertIn(expected_checker, commands)
            self.assertTrue(
                os.access(project_root / ".agent-flow" / "scripts" / "hooks" / "comment-checker.py", os.X_OK)
            )
            self.assertFalse((project_root / "scripts" / "hooks" / "comment-checker.py").exists())

    def test_node_installers_remove_managed_legacy_root_scripts(self) -> None:
        for installer in ("agent-flow-kit.mjs", "agent-flow-install.mjs"):
            with self.subTest(installer=installer):
                with tempfile.TemporaryDirectory() as temp_dir:
                    project_root = Path(temp_dir) / "project"
                    project_root.mkdir()
                    shutil.copytree(Path(__file__).resolve().parents[1] / "scripts", project_root / "scripts")
                    node = _node_executable()
                    result = subprocess.run(
                        (
                            node,
                            str(Path(__file__).resolve().parents[1] / "bin" / installer),
                            "install",
                        ),
                        cwd=project_root,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(
                        (project_root / ".agent-flow" / "scripts" / "hooks" / "comment-checker.py").is_file()
                    )
                    self.assertFalse((project_root / "scripts").exists())

    def test_node_installers_treat_help_as_a_question_not_an_install(self) -> None:
        """`install --help`은 아무것도 쓰지 않는다.

        모르는 토큰을 무시하는 파서에서는 `--help`가 전체 설치를 수행했다. 오타 하나도
        같은 결과를 냈으므로 두 진입점에서 둘을 함께 고정한다.
        """
        node = _node_executable()
        for installer in ("agent-flow-kit.mjs", "agent-flow-install.mjs"):
            for args, expected_code, expected_text in (
                (("install", "--help"), 0, "usage:"),
                (("install", "-h"), 0, "usage:"),
                (("install", "--hlep"), 1, "unknown install argument: --hlep"),
                # root 해석이 먼저 돌면 usage 대신 root 오류로 죽는다.
                (("install", "--root", "no-such-dir", "--help"), 0, "usage:"),
                (("install", "--profile"), 1, "--profile requires a value"),
                (("install", "--profile="), 1, "--profile requires a value"),
                # 다음 플래그를 값으로 삼키면 그 플래그가 skill 이름이 되면서 동시에
                # 전역 플래그로도 적용된다.
                (("install", "--skill", "--no-hooks"), 1, "--skill requires a value"),
                (("install", "install"), 1, "unknown install argument: install"),
            ):
                with self.subTest(installer=installer, args=args):
                    with tempfile.TemporaryDirectory() as temp_dir:
                        project_root = Path(temp_dir) / "project"
                        project_root.mkdir()
                        result = subprocess.run(
                            (
                                node,
                                str(Path(__file__).resolve().parents[1] / "bin" / installer),
                                *args,
                            ),
                            cwd=project_root,
                            text=True,
                            capture_output=True,
                            check=False,
                        )
                        self.assertEqual(result.returncode, expected_code, result.stderr)
                        self.assertIn(expected_text, result.stdout + result.stderr)
                        self.assertEqual(list(project_root.iterdir()), [])

    def test_node_installers_merge_existing_codex_hooks(self) -> None:
        for installer in ("agent-flow-kit.mjs", "agent-flow-install.mjs"):
            for seed_dir, custom_command in (
                (".Codex", "custom-upper-post-hook"),
                (".codex", "custom-lower-post-hook"),
            ):
                with self.subTest(installer=installer, seed_dir=seed_dir):
                    with tempfile.TemporaryDirectory() as temp_dir:
                        project_root = Path(temp_dir) / "project with existing codex hook"
                        project_root.mkdir()
                        hooks_path = project_root / seed_dir / "hooks.json"
                        hooks_path.parent.mkdir()
                        hooks_path.write_text(
                            json.dumps(
                                {
                                    "hooks": {
                                        "PostToolUse": [
                                            {
                                                "matcher": "CustomTool",
                                                "hooks": [{"type": "command", "command": custom_command}],
                                            }
                                        ]
                                    }
                                },
                                indent=2,
                            )
                            + "\n",
                            encoding="utf-8",
                        )
                        node = _node_executable()
                        result = subprocess.run(
                            (
                                node,
                                str(Path(__file__).resolve().parents[1] / "bin" / installer),
                                "install",
                            ),
                            cwd=project_root,
                            text=True,
                            capture_output=True,
                            check=False,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        expected_checker = (
                            f"'{project_root.resolve() / '.agent-flow' / 'bin' / 'agent-flow-hook'}' "
                            f"'{project_root.resolve() / '.agent-flow' / 'scripts' / 'hooks' / 'comment-checker.py'}'"
                        )
                        for installed_hooks_path in (
                            project_root / ".Codex" / "hooks.json",
                            project_root / ".codex" / "hooks.json",
                        ):
                            codex_hooks = json.loads(installed_hooks_path.read_text(encoding="utf-8"))
                            commands = [
                                hook["command"]
                                for entries in codex_hooks["hooks"].values()
                                for entry in entries
                                for hook in entry["hooks"]
                            ]
                            self.assertIn(custom_command, commands)
                            self.assertIn(expected_checker, commands)

    def test_node_installers_preserve_existing_claude_custom_hooks(self) -> None:
        for installer in ("agent-flow-kit.mjs", "agent-flow-install.mjs"):
            with self.subTest(installer=installer):
                with tempfile.TemporaryDirectory() as temp_dir:
                    project_root = Path(temp_dir) / "project with existing claude hook"
                    project_root.mkdir()
                    settings_path = project_root / ".claude" / "settings.json"
                    settings_path.parent.mkdir()
                    settings_path.write_text(
                        json.dumps(
                            {
                                "hooks": {
                                    "PostToolUse": [
                                        {
                                            "matcher": "CustomTool",
                                            "hooks": [{"type": "command", "command": "custom-post-hook"}],
                                        }
                                    ]
                                }
                            },
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    node = _node_executable()
                    result = subprocess.run(
                        (
                            node,
                            str(Path(__file__).resolve().parents[1] / "bin" / installer),
                            "install",
                        ),
                        cwd=project_root,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    settings = json.loads(settings_path.read_text(encoding="utf-8"))
                    commands = [
                        hook["command"]
                        for entries in settings["hooks"].values()
                        for entry in entries
                        for hook in entry["hooks"]
                    ]
                    expected_checker = (
                        f"'{project_root.resolve() / '.agent-flow' / 'bin' / 'agent-flow-hook'}' "
                        f"'{project_root.resolve() / '.agent-flow' / 'scripts' / 'hooks' / 'comment-checker.py'}'"
                    )
                    self.assertIn("custom-post-hook", commands)
                    self.assertIn(expected_checker, commands)

    def test_node_installers_dedupe_stop_hook_on_upgrade(self) -> None:
        # 과거 설치본은 Stop entry에 matcher: ""를 기록했다. 재설치 시 중복되면 안 된다.
        for installer in ("agent-flow-kit.mjs", "agent-flow-install.mjs"):
            for scenario, codex_dir in (("root-script", ".Codex"), ("cd-script", ".codex")):
                with self.subTest(installer=installer, scenario=scenario):
                    with tempfile.TemporaryDirectory() as temp_dir:
                        project_root = Path(temp_dir) / "project"
                        project_root.mkdir()
                        node = _node_executable()
                        stop_command = f"'{project_root.resolve() / 'scripts' / 'hooks' / 'show-phase-status.sh'}'"
                        cd_stop_command = (
                            f"cd '{project_root.resolve()}' && "
                            f"'{project_root.resolve() / '.agent-flow' / 'scripts' / 'hooks' / 'show-phase-status.sh'}'"
                        )
                        expected_stop_command = (
                            f"'{project_root.resolve() / '.agent-flow' / 'bin' / 'agent-flow-hook'}' "
                            f"'{project_root.resolve() / '.agent-flow' / 'scripts' / 'hooks' / 'show-phase-status.sh'}'"
                        )
                        legacy_command = stop_command if scenario == "root-script" else cd_stop_command
                        seeded = {
                            "hooks": {
                                "Stop": [
                                    {
                                        "matcher": "",
                                        "hooks": [{"type": "command", "command": legacy_command}],
                                    }
                                ]
                            }
                        }
                        for seeded_path in (
                            project_root / codex_dir / "hooks.json",
                            project_root / ".claude" / "settings.json",
                        ):
                            seeded_path.parent.mkdir(parents=True, exist_ok=True)
                            seeded_path.write_text(json.dumps(seeded, indent=2) + "\n", encoding="utf-8")
                        result = subprocess.run(
                            (
                                node,
                                str(Path(__file__).resolve().parents[1] / "bin" / installer),
                                "install",
                            ),
                            cwd=project_root,
                            text=True,
                            capture_output=True,
                            check=False,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        for settings_path in (
                            project_root / ".Codex" / "hooks.json",
                            project_root / ".codex" / "hooks.json",
                            project_root / ".claude" / "settings.json",
                        ):
                            settings = json.loads(settings_path.read_text(encoding="utf-8"))
                            self.assertEqual(
                                len(settings["hooks"]["Stop"]), 1, f"{installer}: {settings_path}"
                            )
                            stop_commands = [
                                hook["command"]
                                for entry in settings["hooks"]["Stop"]
                                for hook in entry["hooks"]
                            ]
                            self.assertEqual(
                                stop_commands.count(expected_stop_command), 1, f"{installer}: {settings_path}"
                            )
                            self.assertNotIn(stop_command, stop_commands)
                            self.assertNotIn(cd_stop_command, stop_commands)

    def test_stop_hook_emits_valid_json_for_active_run(self) -> None:
        # Stop hook stdout은 JSON이어야 한다. 평문은 invalid stop hook json output 에러를 만든다.
        hook = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "show-phase-status.sh"
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            (project_root / ".agent-flow").mkdir(parents=True)
            (project_root / ".agent-flow" / "kit.json").write_text("{}\n", encoding="utf-8")
            fake_bin = Path(temp_dir) / "bin"
            fake_bin.mkdir()
            fake_cli = fake_bin / "agent-flow"
            fake_cli.write_text(
                "#!/bin/sh\nprintf 'status: running\\nnext_command: agent-flow run advance\\n'\n",
                encoding="utf-8",
            )
            fake_cli.chmod(0o755)
            env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}
            result = subprocess.run(
                ("/bin/bash", str(hook)),
                cwd=project_root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIn("[agent-flow]", payload["systemMessage"])
            self.assertIn("status: running", payload["systemMessage"])
            self.assertIn("next_command", payload["systemMessage"])

    def test_guard_hooks_report_block_reason_on_stderr(self) -> None:
        # exit 2일 때 Claude/Codex/OMP는 stderr만 모델에 전달한다. stdout은 무시된다.
        hooks_dir = Path(__file__).resolve().parents[1] / "scripts" / "hooks"
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            git_env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.com",
            }
            subprocess.run(("git", "init", "-q", "-b", "main", str(repo)), check=True)
            subprocess.run(
                ("git", "-C", str(repo), "commit", "--allow-empty", "-q", "-m", "init"),
                check=True,
                env=git_env,
            )
            cases = (
                (
                    hooks_dir / "guard-protected-branch.sh",
                    {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}},
                ),
            )
            for hook, payload in cases:
                with self.subTest(hook=hook.name):
                    result = subprocess.run(
                        ("/bin/bash", str(hook)),
                        cwd=repo,
                        input=json.dumps(payload),
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("BLOCKED", result.stderr)
                    self.assertEqual(result.stdout, "")

    def test_guard_protected_branch_ignores_unparseable_command(self) -> None:
        # shlex가 중간까지 토큰을 내보내고 실패해도 부분 토큰으로 차단하면 안 된다.
        hook = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "guard-protected-branch.sh"
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            git_env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.com",
            }
            subprocess.run(("git", "init", "-q", "-b", "main", str(repo)), check=True)
            subprocess.run(
                ("git", "-C", str(repo), "commit", "--allow-empty", "-q", "-m", "init"),
                check=True,
                env=git_env,
            )
            result = subprocess.run(
                ("/bin/bash", str(hook)),
                cwd=repo,
                input=json.dumps(
                    {"tool_name": "Bash", "tool_input": {"command": 'git commit -m "unclosed'}}
                ),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_node_installers_remove_legacy_graphify_artifacts(self) -> None:
        installers = ("agent-flow-kit.mjs", "agent-flow-install.mjs")
        managed_skill_roots = (
            ".agent-flow/skills",
            ".claude/skills",
            ".codex/skills",
            ".Codex/skills",
            ".omp/skills",
            ".gemini/skills",
            ".gemini/antigravity/skills",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            node = _node_executable()
            for installer_name in installers:
                with self.subTest(installer=installer_name):
                    project_root = root / installer_name
                    project_root.mkdir()
                    (project_root / ".gitignore").write_text(
                        "node_modules/\n"
                        "graphify/\n"
                        "graphify-out/manifest.json\n"
                        "graphify-out/cost.json\n",
                        encoding="utf-8",
                    )
                    for skill_root in managed_skill_roots:
                        skill_dir = project_root / skill_root / "graphify"
                        skill_dir.mkdir(parents=True, exist_ok=True)
                        (skill_dir / "SKILL.md").write_text("---\nname: graphify\n---\n", encoding="utf-8")

                    result = subprocess.run(
                        (
                            node,
                            str(Path(__file__).resolve().parents[1] / "bin" / installer_name),
                            "install",
                        ),
                        cwd=project_root,
                        text=True,
                        capture_output=True,
                        check=False,
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    kit = json.loads((project_root / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
                    self.assertNotIn("graphify", kit)
                    gitignore = (project_root / ".gitignore").read_text(encoding="utf-8")
                    self.assertNotIn("graphify/", gitignore)
                    self.assertNotIn("graphify-out/manifest.json", gitignore)
                    self.assertNotIn("graphify-out/cost.json", gitignore)
                    for skill_root in managed_skill_roots:
                        self.assertFalse((project_root / skill_root / "graphify").exists(), skill_root)

    def test_node_installers_remove_legacy_antigravity_skill_links(self) -> None:
        installers = ("agent-flow-kit.mjs", "agent-flow-install.mjs")
        legacy_link_paths = {
            "antigravity": ".gemini/antigravity/skills/agent-flow",
            "gemini": ".gemini/skills/agent-flow",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            node = _node_executable()
            for installer_name in installers:
                with self.subTest(installer=installer_name):
                    project_root = root / installer_name
                    project_root.mkdir()
                    source_dir = project_root / ".agent-flow" / "skills" / "agent-flow"
                    source_dir.mkdir(parents=True)
                    (source_dir / "SKILL.md").write_text("---\nname: agent-flow\n---\n", encoding="utf-8")
                    for link_path in legacy_link_paths.values():
                        target = project_root / link_path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.symlink_to(source_dir)
                    index = {
                        "skills": [
                            {
                                "name": "agent-flow",
                                "source": "bundled",
                                "hosts": ["claude", "codex"],
                                "path": ".agent-flow/skills/agent-flow",
                            }
                        ],
                        "links": [
                            {"name": "agent-flow", "host": host, "path": link_path, "status": "linked"}
                            for host, link_path in legacy_link_paths.items()
                        ],
                    }
                    (project_root / ".agent-flow" / "skills" / "index.json").write_text(
                        json.dumps(index), encoding="utf-8"
                    )

                    result = subprocess.run(
                        (
                            node,
                            str(Path(__file__).resolve().parents[1] / "bin" / installer_name),
                            "install",
                        ),
                        cwd=project_root,
                        text=True,
                        capture_output=True,
                        check=False,
                    )

                    # 제거된 host의 legacy symlink가 ensureChildPath에서 throw하면
                    # install 전체가 중단된다. 정리는 성공하고 link만 사라져야 한다.
                    self.assertEqual(result.returncode, 0, result.stderr)
                    for link_path in legacy_link_paths.values():
                        self.assertFalse((project_root / link_path).is_symlink(), link_path)
                        self.assertFalse((project_root / link_path).exists(), link_path)

    def test_node_installer_accepts_run_install_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            result = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"),
                    "run",
                    "install",
                ),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.strip(),
                f"agent-flow installed profile=generic root={project_root.resolve()}",
            )
            self.assertTrue((project_root / ".agent-flow" / "kit.json").is_file())

    def test_node_installer_skips_managed_worktree_reinstall(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            worktree_root = legacy_managed_root(project_root) / "feat-task"
            worktree_root.mkdir(parents=True)
            node = _node_executable()
            installer = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            initial = subprocess.run(
                (node, installer, "install"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(initial.returncode, 0, initial.stderr)

            result = subprocess.run(
                (node, installer, "install"),
                cwd=worktree_root,
                env={**os.environ, "PATH": ""},
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("worktree install skipped", result.stdout)
            self.assertFalse((worktree_root / ".agent-flow").exists())
            self.assertFalse((worktree_root / "AGENTS.md").exists())

    def test_legacy_node_installer_skips_managed_worktree_reinstall(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            worktree_root = legacy_managed_root(project_root) / "feat-task"
            worktree_root.mkdir(parents=True)
            node = _node_executable()
            kit_installer = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            legacy_installer = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-install.mjs")
            initial = subprocess.run(
                (node, kit_installer, "install"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(initial.returncode, 0, initial.stderr)

            result = subprocess.run(
                (node, legacy_installer, "install"),
                cwd=worktree_root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("worktree install skipped", result.stdout)
            self.assertFalse((worktree_root / ".agent-flow").exists())
            self.assertFalse((worktree_root / "AGENTS.md").exists())

    def test_node_runner_uses_parent_install_from_agent_flow_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            install = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"),
                    "install",
                ),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            # git 초기화는 install 뒤에 온다. 앞서면 픽스처 stub hook이 자리를
            # 차지해 installer가 진짜 hook을 못 쓰고, kit.json digest와 갈라진다.
            _init_git_repo(project_root)
            subprocess.run(("git", "branch", "feat/slice"), cwd=project_root, check=True)
            worktree = legacy_managed_root(project_root) / "slice"
            worktree.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ("git", "worktree", "add", "-q", str(worktree), "feat/slice"),
                cwd=project_root,
                check=True,
            )

            denied = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"),
                    "run",
                    "start",
                    "--task",
                    "ship slice",
                    "--run-id",
                    "denied",
                ),
                cwd=worktree,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(denied.returncode, 2)
            self.assertIn("Refusing implicit reuse", denied.stderr)
            self.assertFalse(
                _node_phase_run_dir(project_root, worktree="slice").exists()
            )

            start = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"),
                    "run",
                    "start",
                    "--task",
                    "ship slice",
                    "--run-id",
                    "r1",
                    "--reuse-existing-worktree",
                ),
                cwd=worktree,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(start.returncode, 0, start.stderr)
            run_dir = _node_phase_run_dir(project_root, worktree="slice")
            self.assertTrue(run_dir.is_dir())
            artifact = run_dir / "artifacts" / "domain-grill.md"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(_node_phase_content("domain-grill"), encoding="utf-8")
            status = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"),
                    "run",
                    "status",
                ),
                cwd=worktree,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("reason: phase_artifact_written_continue_required", status.stdout)
            self.assertNotIn("reason: missing_phase_artifact", status.stdout)

    def test_node_runner_blocks_missing_manual_spec_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            kit_root = Path(__file__).resolve().parents[1]
            node = _node_executable()
            install = subprocess.run(
                (node, str(kit_root / "bin" / "agent-flow-kit.mjs"), "install"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            _init_git_repo(project_root)
            # run은 leader가 아니라 managed worktree 안에서 돈다. 이름/경로는
            # 프로덕션 planner에서 유도한다 — 문자열로 박으면 slug 규칙이 바뀔 때 깨진다.
            plan = plan_worktree(root=project_root, name="confirm rendered copy")
            start = subprocess.run(
                (
                    node,
                    str(kit_root / "bin" / "agent-flow-kit.mjs"),
                    "run",
                    "start",
                    "--task",
                    "confirm rendered copy",
                    "--run-id",
                    "r1",
                ),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(start.returncode, 0, start.stderr)
            run_dir = _node_phase_run_dir(project_root, worktree=plan.name)
            source_artifact = """## Spec Items

SPEC-1: Confirm the rendered copy.
verify: manual

## Design Values
"""
            _capture_node_spec_source(run_dir, source_artifact)
            definition = load_phase_workflow_definition(kit_root, "full-feature")
            _, phase = next(
                (index, candidate)
                for index, candidate in enumerate(definition.phases)
                if candidate.id == "multi-review"
            )
            artifact = _set_node_phase(run_dir, phase.id)
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(_node_phase_content("multi-review"), encoding="utf-8")

            result = subprocess.run(
                (
                    node,
                    str(kit_root / "bin" / "agent-flow-kit.mjs"),
                    "run",
                    "advance",
                ),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("status: blocked", result.stdout)
            self.assertIn("reason: missing_completion_markers", result.stdout)
            self.assertIn("SPEC-1: manual (no user approval record)", result.stdout)

    @mock.patch.dict(
        os.environ,
        {"AGENT_FLOW_ADAPTER": "generic", "AGENT_FLOW_GENERIC_MODE": "emit"},
        clear=False,
    )
    def test_node_spec_check_uses_project_root_from_a_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            source = project_root / "SearchResults.kt"
            source.write_text("class SearchResults\n", encoding="utf-8")
            for command in (
                ("git", "init", "-b", "main"),
                ("git", "config", "user.email", "test@example.com"),
                ("git", "config", "user.name", "Test"),
                ("git", "add", "SearchResults.kt"),
                ("git", "commit", "-m", "base"),
            ):
                completed = subprocess.run(
                    command,
                    cwd=project_root,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

            kit_root = Path(__file__).resolve().parents[1]
            node = _node_executable()
            install = subprocess.run(
                (node, str(kit_root / "bin" / "agent-flow-kit.mjs"), "install"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            subprocess.run(
                ("git", "add", ".gitignore"),
                cwd=project_root,
                check=True,
            )
            subprocess.run(
                ("git", "commit", "-q", "-m", "record agent-flow install"),
                cwd=project_root,
                check=True,
            )
            start = subprocess.run(
                (
                    node,
                    str(kit_root / "bin" / "agent-flow-kit.mjs"),
                    "run",
                    "start",
                    "--task",
                    "show empty search results",
                    "--run-id",
                    "r1",
                ),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(start.returncode, 0, start.stderr)
            worktree_name = "feat-show-empty-search-results"
            checkout = managed_worktrees_root(project_root) / worktree_name
            run_dir = _node_phase_run_dir(
                project_root,
                worktree=worktree_name,
            )
            source = checkout / "SearchResults.kt"
            source_artifact = """## Spec Items

SPEC-1: Show the empty search state.
verify: symbol:SearchResults=No results

## Design Values
"""
            _capture_node_spec_source(run_dir, source_artifact)
            source.write_text(
                'class SearchResults {\n    val emptyLabel = "No results"\n}\n',
                encoding="utf-8",
            )
            definition = load_phase_workflow_definition(kit_root, "full-feature")
            _, phase = next(
                (index, candidate)
                for index, candidate in enumerate(definition.phases)
                if candidate.id == "multi-review"
            )
            artifact = _set_node_phase(run_dir, phase.id)
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(
                _node_phase_content("multi-review"),
                encoding="utf-8",
            )
            nested = checkout / "nested"
            nested.mkdir()

            result = subprocess.run(
                (
                    node,
                    str(kit_root / "bin" / "agent-flow-kit.mjs"),
                    "run",
                    "advance",
                ),
                cwd=nested,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_node_runner_captures_and_injects_spec_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            kit_root = Path(__file__).resolve().parents[1]
            node = _node_executable()
            install = subprocess.run(
                (node, str(kit_root / "bin" / "agent-flow-kit.mjs"), "install"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            _init_git_repo(project_root)
            # run은 leader가 아니라 managed worktree 안에서 돈다. 이름/경로는
            # 프로덕션 planner에서 유도한다 — 문자열로 박으면 slug 규칙이 바뀔 때 깨진다.
            plan = plan_worktree(root=project_root, name="confirm rendered copy")
            start = subprocess.run(
                (
                    node,
                    str(kit_root / "bin" / "agent-flow-kit.mjs"),
                    "run",
                    "start",
                    "--task",
                    "confirm rendered copy",
                    "--run-id",
                    "r1",
                ),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(start.returncode, 0, start.stderr)
            definition = load_phase_workflow_definition(kit_root, "full-feature")
            _, phase = next(
                (index, candidate)
                for index, candidate in enumerate(definition.phases)
                if candidate.id == "prd"
            )
            run_dir = _node_phase_run_dir(project_root, worktree=plan.name)
            artifact = _set_node_phase(run_dir, phase.id)
            artifact.parent.mkdir(parents=True, exist_ok=True)
            source_artifact = (
                """# prd

## Spec Items

SPEC-1: Confirm the rendered copy.
verify: manual

## Design Values

## Completion Gate

spec-items: SPEC-1
design-values: none
design-values-confirmed: n/a
"""
                + _node_project_local_gate("prd")
            )
            artifact.write_text(source_artifact, encoding="utf-8")

            result = subprocess.run(
                (
                    node,
                    str(kit_root / "bin" / "agent-flow-kit.mjs"),
                    "run",
                    "advance",
                ),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            ledger = (run_dir / "design-spec.md").read_text(encoding="utf-8")
            self.assertIn("SPEC-1: Confirm the rendered copy.", ledger)
            self.assertIn("SPEC-1: Confirm the rendered copy.", result.stdout)

            (run_dir / "design-spec.md").unlink()
            blocked_prompt = subprocess.run(
                (
                    node,
                    str(kit_root / "bin" / "agent-flow-kit.mjs"),
                    "run",
                    "advance",
                ),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(
                blocked_prompt.returncode,
                0,
                blocked_prompt.stdout + blocked_prompt.stderr,
            )
            self.assertIn("design-spec.md is missing", blocked_prompt.stderr)

    def test_node_runner_uses_parent_install_from_codex_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            install = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"),
                    "install",
                ),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            _init_git_repo(project_root)
            for index, marker in enumerate((".codex", ".Codex"), start=1):
                with self.subTest(marker=marker):
                    run_id = f"r{index}"
                    worktree_name = f"slice-{index}"
                    worktree = project_root / marker / "worktrees" / worktree_name
                    worktree.parent.mkdir(parents=True, exist_ok=True)
                    branch = f"feat/{worktree_name}"
                    subprocess.run(("git", "branch", branch), cwd=project_root, check=True)
                    subprocess.run(
                        ("git", "worktree", "add", "-q", str(worktree), branch),
                        cwd=project_root,
                        check=True,
                    )

                    start = subprocess.run(
                        (
                            node,
                            str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"),
                            "run",
                            "start",
                            "--task",
                            "ship slice",
                            "--run-id",
                            run_id,
                            "--reuse-existing-worktree",
                        ),
                        cwd=worktree,
                        text=True,
                        capture_output=True,
                        check=False,
                    )

                    self.assertEqual(start.returncode, 0, start.stderr)
                    run_dir = _node_phase_run_dir(
                        project_root,
                        run_id,
                        worktree=worktree_name,
                    )
                    self.assertTrue(run_dir.is_dir())
                    self.assertFalse((worktree / ".agent-flow" / "runs" / run_id).exists())

    def test_python_cli_status_from_managed_worktree_uses_parent_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            root.mkdir()
            _init_git_repo(root)
            with mock.patch.dict(os.environ, {
                "AGENT_FLOW_ADAPTER": "generic",
                "AGENT_FLOW_GENERIC_MODE": "stub-success",
            }):
                self.assertEqual(main(["run", "slice", "--root", str(root)]), 0)
            worktree = managed_worktrees_root(root) / "feat-slice"
            self.assertTrue((worktree / ".git").exists())

            old_cwd = Path.cwd()
            try:
                os.chdir(worktree)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["status"]), 0)
            finally:
                os.chdir(old_cwd)

            status = output.getvalue()
            self.assertIn("Run id", status)
            self.assertIn("current_phase: slice-plan", status)
            self.assertIn(f"next_command: agent-flow continue --root {root.resolve()} --worktree feat-slice", status)
            self.assertFalse((worktree / ".agent-flow" / "runs").exists())

    def test_python_cli_run_from_managed_worktree_reuses_parent_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            root.mkdir()
            _init_git_repo(root)
            self.assertEqual(main(["run", "slice", "--root", str(root)]), 0)
            worktree = managed_worktrees_root(root) / "feat-slice"

            old_cwd = Path.cwd()
            try:
                os.chdir(worktree)
                output = io.StringIO()
                with (
                    contextlib.redirect_stdout(output),
                    mock.patch.object(sys.stdin, "isatty", return_value=True),
                    mock.patch("builtins.input", return_value="yes") as confirm,
                ):
                    self.assertEqual(main(["run", "other"]), 2)
                confirm.assert_called_once()
            finally:
                os.chdir(old_cwd)

            self.assertIn("already active", output.getvalue())
            self.assertFalse((managed_worktrees_root(root) / "feat-other").exists())
            self.assertEqual(
                managed_worktrees_root(worktree), managed_worktrees_root(root)
            )

    def test_python_cli_consent_starts_run_in_current_managed_worktree(self) -> None:
        from agent_flow.core.worktrees import create_worktree

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            root.mkdir()
            _init_git_repo(root)
            status = create_worktree(
                root=root,
                plan=plan_worktree(root=root, name="existing"),
            )

            runners = []

            def capture_run(runner, **_kwargs):
                runners.append(runner)

            old_cwd = Path.cwd()
            try:
                os.chdir(status.path)
                with (
                    mock.patch.object(sys.stdin, "isatty", return_value=True),
                    mock.patch("builtins.input", return_value="yes"),
                    mock.patch("agent_flow.cli.Runner.run", new=capture_run),
                ):
                    self.assertEqual(main(["run", "new task"]), 0)
            finally:
                os.chdir(old_cwd)

            self.assertEqual(len(runners), 1)
            self.assertEqual(
                runners[0].project_root.resolve(),
                status.path.resolve(),
            )
            self.assertFalse(
                (managed_worktrees_root(root) / "feat-new-task").exists()
            )

    def test_python_cli_start_from_worktree_requires_and_accepts_reuse_consent(
        self,
    ) -> None:
        from agent_flow.core.worktrees import create_worktree

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            root.mkdir()
            _init_git_repo(root)
            status = create_worktree(
                root=root,
                plan=plan_worktree(root=root, name="existing"),
            )
            runners = []

            def capture_run(runner, **_kwargs):
                runners.append(runner)

            command = [
                "start",
                "development",
                "--task",
                "new task",
                "--checkout-identity",
                "worktree:feat-existing",
            ]
            old_cwd = Path.cwd()
            try:
                os.chdir(status.path)
                stderr = io.StringIO()
                with (
                    contextlib.redirect_stderr(stderr),
                    mock.patch.object(sys.stdin, "isatty", return_value=False),
                    mock.patch("agent_flow.cli.Runner.run", new=capture_run),
                ):
                    self.assertEqual(main(command), 2)
                self.assertIn(
                    "--reuse-existing-worktree",
                    stderr.getvalue(),
                )
                self.assertEqual(runners, [])

                with (
                    mock.patch(
                        "builtins.input",
                        side_effect=AssertionError("unexpected prompt"),
                    ),
                    mock.patch("agent_flow.cli.Runner.run", new=capture_run),
                ):
                    self.assertEqual(
                        main([*command, "--reuse-existing-worktree"]),
                        0,
                    )
            finally:
                os.chdir(old_cwd)

            self.assertEqual(len(runners), 1)
            self.assertEqual(
                runners[0].project_root.resolve(),
                status.path.resolve(),
            )

    def test_python_cli_run_from_managed_worktree_requires_reuse_consent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            root.mkdir()
            _init_git_repo(root)
            self.assertEqual(main(["run", "slice", "--root", str(root)]), 0)
            worktree = managed_worktrees_root(root) / "feat-slice"

            old_cwd = Path.cwd()
            try:
                os.chdir(worktree)
                stderr = io.StringIO()
                with (
                    contextlib.redirect_stderr(stderr),
                    mock.patch.object(sys.stdin, "isatty", return_value=False),
                ):
                    self.assertEqual(main(["run", "other"]), 2)
                self.assertIn("--reuse-existing-worktree", stderr.getvalue())

                output = io.StringIO()
                with (
                    contextlib.redirect_stdout(output),
                    mock.patch("builtins.input", side_effect=AssertionError("unexpected prompt")),
                ):
                    self.assertEqual(
                        main(["run", "other", "--reuse-existing-worktree"]),
                        2,
                    )
                self.assertIn("already active", output.getvalue())
            finally:
                os.chdir(old_cwd)

            self.assertFalse((managed_worktrees_root(root) / "feat-other").exists())

    def test_node_lifecycle_relay_recognizes_an_adopted_external_checkout(self) -> None:
        """반증: JS relay가 cwd를 leader/관리 경로로만 분류하면, 채택된 외부 checkout에서
        `--worktree` 없이 부른 lifecycle 명령이 전부 `identity is unknown`으로 거절된다.
        `agent-flow`는 이 JS 진입점이므로 Python 쪽만 고쳐도 사용자에게는 안 고쳐진다."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            _init_git_repo(project_root)
            node = _node_executable()
            kit = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            env = _node_test_env()
            # relay는 설치된 Python runtime을 부른다. stub kit.json만으로는 그 앞에서 멈춘다.
            install = subprocess.run(
                (node, kit, "install"),
                cwd=project_root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            external = Path(temp_dir) / "outside" / "ext"
            external.parent.mkdir(parents=True)
            subprocess.run(
                ("git", "worktree", "add", "-q", "-b", "feat/ext", str(external), "HEAD"),
                cwd=project_root,
                check=True,
            )

            before = subprocess.run(
                (node, kit, "run", "status"),
                cwd=external,
                env=_node_test_env(),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertIn("identity is unknown", before.stdout + before.stderr)

            self.assertEqual(
                main(
                    [
                        "worktree",
                        "adopt",
                        "--path",
                        str(external),
                        "--allow-dirty",
                        "--root",
                        str(project_root),
                    ]
                ),
                0,
            )

            after = subprocess.run(
                (node, kit, "run", "status"),
                cwd=external,
                env=_node_test_env(),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotIn("identity is unknown", after.stdout + after.stderr)
            self.assertNotIn("identity mismatch", after.stdout + after.stderr)

            # leader 디렉터리 **아래**이지만 관리 경로 밖인 자리. containment 판정이
            # 먼저 오면 JS는 "leader", Python은 채택 기록으로 `worktree:<name>`을 내
            # 두 값이 어긋난다.
            inside = project_root / ".worktrees" / "nested"
            inside.parent.mkdir(parents=True)
            subprocess.run(
                ("git", "worktree", "add", "-q", "-b", "feat/nested", str(inside), "HEAD"),
                cwd=project_root,
                check=True,
            )
            self.assertEqual(
                main(
                    [
                        "worktree",
                        "adopt",
                        "--path",
                        str(inside),
                        "--allow-dirty",
                        "--root",
                        str(project_root),
                    ]
                ),
                0,
            )
            nested = subprocess.run(
                (node, kit, "run", "status"),
                cwd=inside,
                env=_node_test_env(),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotIn("identity mismatch", nested.stdout + nested.stderr)
            self.assertNotIn("identity is unknown", nested.stdout + nested.stderr)

    def test_node_installer_from_agent_flow_worktree_without_root_install_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            worktree = legacy_managed_root(project_root) / "slice"
            worktree.mkdir(parents=True)

            result = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"),
                    "install",
                ),
                cwd=worktree,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("managed worktree install blocked", result.stderr)
            self.assertFalse((project_root / ".agent-flow" / "kit.json").exists())
            self.assertFalse((worktree / ".agent-flow" / "kit.json").exists())

    def test_node_installer_from_codex_worktree_without_root_install_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            for marker in (".codex", ".Codex"):
                with self.subTest(marker=marker):
                    worktree = project_root / marker / "worktrees" / "slice"
                    worktree.mkdir(parents=True, exist_ok=True)

                    result = subprocess.run(
                        (
                            node,
                            str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"),
                            "install",
                        ),
                        cwd=worktree,
                        text=True,
                        capture_output=True,
                        check=False,
                    )

                    self.assertEqual(result.returncode, 1)
                    self.assertIn("managed worktree install blocked", result.stderr)
                    self.assertFalse((project_root / ".agent-flow" / "kit.json").exists())
                    self.assertFalse((worktree / ".agent-flow" / "kit.json").exists())

    def test_node_installer_from_external_codex_worktree_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            _init_git_repo(project_root)
            home = Path(temp_dir) / "home"
            worktree = home / ".codex" / "worktrees" / "slice" / "project"
            worktree.parent.mkdir(parents=True)
            subprocess.run(("git", "worktree", "add", "-q", "--detach", str(worktree), "HEAD"), cwd=project_root, check=True)
            node = _node_executable()
            env = _node_test_env(HOME=str(home))
            leader_kit = (project_root / ".agent-flow" / "kit.json").read_bytes()

            result = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"),
                    "install",
                ),
                cwd=worktree,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            # leader를 PROJECT로 잡아 install을 끝내는 예전 경로는 leader의
            # CLAUDE.md/AGENTS.md를 백업 없이 덮고, tracked `.gitignore`를 고쳐 leader를
            # dirty로 만들고, 미선택 profile을 지운다. 그래서 여기서는 아무것도 쓰지
            # 않는다. 다만 leader에 설치본이 이미 있으면 "할 일이 없음"이지 실패가
            # 아니라서 managed worktree 분기와 같은 rc 0으로 건너뛴다 - 같은 정책인데
            # rc만 갈라지면 install을 CI/부트스트랩에 넣은 사용자가 worktree 안에서만
            # 스크립트 전진이 죽는다.
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("worktree install skipped", result.stdout)
            self.assertEqual((project_root / ".agent-flow" / "kit.json").read_bytes(), leader_kit)
            self.assertFalse((worktree / ".agent-flow" / "kit.json").exists())

    def test_legacy_node_installer_from_external_codex_worktree_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            _init_git_repo(project_root)
            home = Path(temp_dir) / "home"
            worktree = home / ".codex" / "worktrees" / "slice" / "project"
            worktree.parent.mkdir(parents=True)
            subprocess.run(("git", "worktree", "add", "-q", "--detach", str(worktree), "HEAD"), cwd=project_root, check=True)
            node = _node_executable()
            env = _node_test_env(HOME=str(home))
            leader_kit = (project_root / ".agent-flow" / "kit.json").read_bytes()

            result = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-install.mjs"),
                    "install",
                ),
                cwd=worktree,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("worktree install skipped", result.stdout)
            self.assertEqual((project_root / ".agent-flow" / "kit.json").read_bytes(), leader_kit)
            self.assertFalse((project_root / "CLAUDE.md").exists())
            self.assertFalse((worktree / ".agent-flow" / "kit.json").exists())

    def test_node_installers_from_linked_worktree_without_leader_install_are_blocked(self) -> None:
        # 위 두 테스트는 leader에 설치본이 있는 분기(rc 0 skip)를 고정한다. 없는 분기는
        # 여전히 fail-closed여야 한다 - 조용히 leader를 PROJECT로 잡으면 다른 checkout의
        # install이 leader의 tracked 파일을 갈아치운다. `_init_git_repo`는 stub kit.json을
        # 만들므로 여기서는 맨 git repo로 세운다.
        installers = ("agent-flow-kit.mjs", "agent-flow-install.mjs")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            node = _node_executable()
            for installer_name in installers:
                with self.subTest(installer=installer_name):
                    project_root = root / f"{installer_name}-bare-leader"
                    project_root.mkdir()
                    subprocess.run(("git", "init", "-q", "-b", "main"), cwd=project_root, check=True)
                    subprocess.run(("git", "config", "user.email", "test@example.com"), cwd=project_root, check=True)
                    subprocess.run(("git", "config", "user.name", "Test User"), cwd=project_root, check=True)
                    (project_root / "README.md").write_text("# test\n", encoding="utf-8")
                    subprocess.run(("git", "add", "-A"), cwd=project_root, check=True)
                    subprocess.run(("git", "commit", "-q", "-m", "init"), cwd=project_root, check=True)
                    home = root / f"{installer_name}-home"
                    worktree = home / ".codex" / "worktrees" / "slice" / "project"
                    worktree.parent.mkdir(parents=True)
                    subprocess.run(
                        ("git", "worktree", "add", "-q", "--detach", str(worktree), "HEAD"),
                        cwd=project_root,
                        check=True,
                    )

                    result = subprocess.run(
                        (
                            node,
                            str(Path(__file__).resolve().parents[1] / "bin" / installer_name),
                            "install",
                        ),
                        cwd=worktree,
                        env=_node_test_env(HOME=str(home)),
                        text=True,
                        capture_output=True,
                        check=False,
                    )

                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("linked worktree install blocked", result.stderr)
                    self.assertFalse((project_root / ".agent-flow" / "kit.json").exists())
                    self.assertFalse((worktree / ".agent-flow" / "kit.json").exists())

    def test_node_installers_from_leader_subdirectory_install_into_leader(self) -> None:
        # `git rev-parse --git-common-dir`은 cwd 기준 상대경로를 낸다: leader 루트에서는
        # `.git`, `<leader>/src`에서는 `../.git`. 그래서 leader 판정은 cwd(start) 기준으로
        # 풀어야 한다. toplevel 기준으로 풀면 `<leader>/src`에서 leader의 부모가 leader로
        # 잡혀 toplevel과 갈라지고, worktree가 하나도 없는데도 install이
        # "linked worktree install blocked"로 죽는다.
        installers = ("agent-flow-kit.mjs", "agent-flow-install.mjs")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            node = _node_executable()
            for installer_name in installers:
                with self.subTest(installer=installer_name):
                    project_root = root / f"{installer_name}-leader-subdir"
                    source_dir = project_root / "src"
                    source_dir.mkdir(parents=True)
                    _init_git_repo(project_root)

                    result = subprocess.run(
                        (
                            node,
                            str(Path(__file__).resolve().parents[1] / "bin" / installer_name),
                            "install",
                        ),
                        cwd=source_dir,
                        env=_node_test_env(),
                        text=True,
                        capture_output=True,
                        check=False,
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn("worktree install blocked", result.stderr)
                    self.assertTrue((project_root / ".agent-flow" / "kit.json").is_file(), result.stdout)
                    self.assertTrue((project_root / ".claude" / "settings.json").is_file(), result.stdout)
                    # 하위 디렉터리가 자기 설치본을 갖게 되면 leader와 두 벌이 된다.
                    self.assertFalse((source_dir / ".agent-flow").exists())
                    self.assertFalse((source_dir / ".claude").exists())

    def test_node_runner_rejects_unbound_external_codex_worktree_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            _init_git_repo(project_root)
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            install = subprocess.run((node, cli, "install"), cwd=project_root, text=True, capture_output=True, check=False)
            self.assertEqual(install.returncode, 0, install.stderr)
            home = Path(temp_dir) / "home"
            worktree = home / ".codex" / "worktrees" / "slice" / "project"
            worktree.parent.mkdir(parents=True)
            subprocess.run(("git", "worktree", "add", "-q", "--detach", str(worktree), "HEAD"), cwd=project_root, check=True)
            env = _node_test_env(HOME=str(home))

            start = subprocess.run(
                (
                    node,
                    cli,
                    "run",
                    "start",
                    "--task",
                    "ship slice",
                    "--run-id",
                    "r1",
                ),
                cwd=worktree,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(start.returncode, 1)
            self.assertIn(
                "checkout identity is unknown; refusing to relay lifecycle state",
                start.stderr,
            )
            self.assertFalse(_node_phase_run_dir(project_root).exists())

    def test_node_installers_index_project_skills_that_collide_with_external_names(self) -> None:
        installers = ("agent-flow-kit.mjs", "agent-flow-install.mjs")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            node = _node_executable()
            for installer_name in installers:
                project_root = root / installer_name
                project_root.mkdir()
                alias_dir = project_root / ".agent-flow" / "local-skills" / "aliased-compose"
                alias_dir.mkdir(parents=True)
                (alias_dir / "SKILL.md").write_text(
                    "---\n"
                    "name: compose-state-authoring\n"
                    "hosts: [codex]\n"
                    "---\n"
                    "# Compose State Authoring\n",
                    encoding="utf-8",
                )
                edge_dir = project_root / "skills" / "edge-to-edge"
                edge_dir.mkdir(parents=True)
                (edge_dir / "SKILL.md").write_text(
                    "---\n"
                    "name: edge-to-edge\n"
                    "hosts: [codex]\n"
                    "---\n"
                    "# Edge To Edge\n",
                    encoding="utf-8",
                )
                result = subprocess.run(
                    (
                        node,
                        str(Path(__file__).resolve().parents[1] / "bin" / installer_name),
                        "install",
                    ),
                    cwd=project_root,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                index = json.loads((project_root / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8"))
                skill_names = {skill["name"] for skill in index["skills"]}
                # 프로젝트가 직접 쓴 파일이다. 조용히 버리면 사용자는 자기 skill이
                # 왜 안 쓰이는지 알 수 없다. 색인하고 `skills doctor`가 충돌을 보고한다.
                self.assertIn("compose-state-authoring", skill_names)
                self.assertIn("edge-to-edge", skill_names)
                # frontmatter가 `hosts: [codex]`라 codex 경로에만 링크된다. codex link는
                # `hostSkillRoot`가 `.Codex`로 고정한다 - case-insensitive FS에서 두
                # 이름이 같은 디렉터리라 `.codex`까지 단언하면 그 FS에서만 통과한다.
                codex_root = project_root / ".Codex" / "skills"
                self.assertTrue((codex_root / "compose-state-authoring").exists())
                self.assertTrue((codex_root / "edge-to-edge").exists())
                for host_root in (project_root / ".omp" / "skills", project_root / ".claude" / "skills"):
                    self.assertFalse((host_root / "compose-state-authoring").exists())
                    self.assertFalse((host_root / "edge-to-edge").exists())

    def test_node_installers_copy_claude_code_reviewer_from_codex_source(self) -> None:
        installers = ("agent-flow-kit.mjs", "agent-flow-install.mjs")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            node = _node_executable()
            for installer_name in installers:
                with self.subTest(installer=installer_name):
                    project_root = root / f"{installer_name}-reviewer-agent"
                    project_root.mkdir()
                    result = subprocess.run(
                        (
                            node,
                            str(Path(__file__).resolve().parents[1] / "bin" / installer_name),
                            "install",
                        ),
                        cwd=project_root,
                        text=True,
                        capture_output=True,
                        check=False,
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    codex_reviewer = (project_root / ".Codex" / "agents" / "code-reviewer.md").read_text(
                        encoding="utf-8"
                    )
                    claude_reviewer = (project_root / ".claude" / "agents" / "code-reviewer.md").read_text(
                        encoding="utf-8"
                    )
                    self.assertEqual(codex_reviewer, _strip_markdown_frontmatter(claude_reviewer))
                    self.assertIn("name: code-reviewer", claude_reviewer)

    def test_node_installers_omp_hook_leaves_root_context_files_alone(self) -> None:
        """불변: hook은 루트 AGENTS.md/CLAUDE.md를 쓰지 않는다.

        예전에는 이 자리가 두 파일의 바이트 동일성을 요구했다. 루트 CLAUDE.md가
        AGENTS.md를 가리키는 한 줄 포인터가 된 뒤로 그 미러는 포인터를 첫 write에서
        전문 사본으로 되돌리는 값만 있다. OMP는 루트 CLAUDE.md를 읽지도 않는다 —
        `claude` discovery provider의 경로는 `.claude/CLAUDE.md`와
        `~/.claude/CLAUDE.md`뿐이고, 루트 AGENTS.md는 `agents-md` provider가 읽는다.
        """
        installers = ("agent-flow-kit.mjs", "agent-flow-install.mjs")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            node = _node_executable()
            for installer_name in installers:
                with self.subTest(installer=installer_name):
                    project_root = root / f"{installer_name}-omp-context-sync"
                    project_root.mkdir()
                    result = subprocess.run(
                        (
                            node,
                            str(Path(__file__).resolve().parents[1] / "bin" / installer_name),
                            "install",
                        ),
                        cwd=project_root,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

                    (project_root / "CLAUDE.md").write_text("claude-updated\n", encoding="utf-8")
                    (project_root / "AGENTS.md").write_text("agents-old\n", encoding="utf-8")
                    extension_ts = project_root / ".omp" / "extensions" / "agent-flow-hooks.ts"
                    extension_mjs = project_root / ".omp" / "extensions" / "agent-flow-hooks.mjs"
                    extension_mjs.write_text(extension_ts.read_text(encoding="utf-8"), encoding="utf-8")
                    exercise = project_root / "exercise-omp-hooks.mjs"
                    exercise.write_text(
                        """
import fs from "node:fs";
import path from "node:path";
import agentFlowHooks from "./.omp/extensions/agent-flow-hooks.mjs";

const handlers = new Map();
agentFlowHooks({
  setLabel() {},
  on(name, handler) {
    handlers.set(name, handler);
  },
});

if (!handlers.has("tool_result") || !handlers.has("context")) {
  throw new Error("missing OMP hook handlers");
}

await handlers.get("tool_result")(
  { toolName: "Write", input: { file_path: "CLAUDE.md" } },
  { cwd: process.cwd() },
);
await handlers.get("tool_result")(
  { toolName: "Edit", input: { path: "AGENTS.md" } },
  { cwd: process.cwd() },
);
if (fs.readFileSync("CLAUDE.md", "utf8") !== "claude-updated\\n") {
  throw new Error("hook rewrote CLAUDE.md");
}
if (fs.readFileSync("AGENTS.md", "utf8") !== "agents-old\\n") {
  throw new Error("hook rewrote AGENTS.md");
}

const staleModelContext = {
  role: "custom",
  customType: "agent-flow-model-context",
  display: false,
  attribution: "agent",
  details: {
    fileName: "AGENTS.md",
    filePath: path.join(process.cwd(), "AGENTS.md"),
    source: "agent-flow-omp-model-context",
  },
  content: "<context>leaked root context</context>",
};
const materializedModelContext = {
  role: "developer",
  content: [
    {
      type: "text",
      text: '<context>\\n<file path="AGENTS.md" source="agent-flow-omp-model-context">\\nleaked root context\\n</file>\\n</context>',
    },
  ],
};
const normalMarkerMessage = {
  role: "developer",
  content: 'debug log: source="agent-flow-omp-model-context"',
};
const userQuotedContext = {
  role: "user",
  content: '<context>\\n<file path="AGENTS.md" source="agent-flow-omp-model-context">\\nquoted by user\\n</file>\\n</context>',
};
const visibleMessage = { role: "user", content: "keep me" };
const scrubbedContext = await handlers.get("context")(
  { messages: [visibleMessage, normalMarkerMessage, userQuotedContext, staleModelContext, materializedModelContext] },
  { cwd: process.cwd(), models: { current() { return { provider: "anthropic", id: "claude-sonnet" }; } } },
);
if (!scrubbedContext || scrubbedContext.messages.length !== 3 || scrubbedContext.messages[0] !== visibleMessage || scrubbedContext.messages[1] !== normalMarkerMessage || scrubbedContext.messages[2] !== userQuotedContext) {
  throw new Error("Stale hidden or materialized root context message should be stripped");
}

const claudeContext = await handlers.get("context")(
  { messages: [] },
  { cwd: process.cwd(), models: { current() { return { provider: "anthropic", id: "claude-sonnet" }; } } },
);
if (claudeContext !== undefined) {
  throw new Error("Context hook must not inject Claude root context");
}

const codexContext = await handlers.get("context")(
  { messages: [] },
  { cwd: process.cwd(), models: { current() { return { provider: "openai", id: "gpt-5" }; } } },
);
if (codexContext !== undefined) {
  throw new Error("Context hook must not inject Codex/OpenAI root context");
}
""",
                        encoding="utf-8",
                    )
                    exercise_result = subprocess.run(
                        (node, str(exercise)),
                        cwd=project_root,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(exercise_result.returncode, 0, exercise_result.stderr)

    def test_node_installers_link_default_host_skills_to_claude_codex_and_omp(self) -> None:
        installers = ("agent-flow-kit.mjs", "agent-flow-install.mjs")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            node = _node_executable()
            for installer_name in installers:
                with self.subTest(installer=installer_name):
                    project_root = root / f"{installer_name}-default-hosts"
                    skill_dir = project_root / "skills" / "default-host-skill"
                    skill_dir.mkdir(parents=True)
                    (skill_dir / "SKILL.md").write_text(
                        "---\n"
                        "name: default-host-skill\n"
                        "description: Use when testing default host skill links.\n"
                        "---\n"
                        "# Default Host Skill\n",
                        encoding="utf-8",
                    )

                    result = subprocess.run(
                        (
                            node,
                            str(Path(__file__).resolve().parents[1] / "bin" / installer_name),
                            "install",
                        ),
                        cwd=project_root,
                        text=True,
                        capture_output=True,
                        check=False,
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue((project_root / ".claude" / "skills" / "default-host-skill" / "SKILL.md").exists())
                    self.assertTrue((project_root / ".Codex" / "skills" / "default-host-skill" / "SKILL.md").exists())
                    self.assertTrue((project_root / ".omp" / "skills" / "default-host-skill" / "SKILL.md").exists())
                    index = json.loads(
                        (project_root / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8")
                    )
                    selected = next(skill for skill in index["skills"] if skill["name"] == "default-host-skill")
                    self.assertIn("claude", selected["hosts"])
                    self.assertIn("codex", selected["hosts"])
                    self.assertIn("omp", selected["hosts"])
                    self.assertNotIn("gemini", selected["hosts"])
                    self.assertNotIn("antigravity", selected["hosts"])

    def test_node_installers_refresh_managed_workflow_skills(self) -> None:
        installers = ("agent-flow-kit.mjs", "agent-flow-install.mjs")
        rels = (
            ".agent-flow/workflows/full-feature.yaml",
            ".agent-flow/skills/full-feature-workflow/SKILL.md",
            ".agent-flow/skills/product-brief/SKILL.md",
            ".agent-flow/skills/plan-reviewer/SKILL.md",
            ".agent-flow/skills/architecture-reviewer/SKILL.md",
            ".agent-flow/skills/push-watch/SKILL.md",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            node = _node_executable()
            installed_roots: dict[str, Path] = {}
            for installer_name in installers:
                with self.subTest(installer=installer_name):
                    project_root = root / f"{installer_name}-managed"
                    stale_workflow = project_root / ".agent-flow" / "workflows" / "full-feature.yaml"
                    stale_workflow.parent.mkdir(parents=True)
                    stale_workflow.write_text("stale: true\n", encoding="utf-8")

                    result = subprocess.run(
                        (
                            node,
                            str(Path(__file__).resolve().parents[1] / "bin" / installer_name),
                            "install",
                        ),
                        cwd=project_root,
                        text=True,
                        capture_output=True,
                        check=False,
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    # managed workflow와 source-backed workflow skill이 모두 설치되어야 한다.
                    self.assertIn("id: domain-grill", stale_workflow.read_text(encoding="utf-8"))
                    for rel in rels:
                        self.assertTrue((project_root / rel).is_file(), rel)
                    installed_roots[installer_name] = project_root

            kit_root = installed_roots["agent-flow-kit.mjs"]
            legacy_root = installed_roots["agent-flow-install.mjs"]
            for rel in rels:
                self.assertEqual(
                    (kit_root / rel).read_text(encoding="utf-8"),
                    (legacy_root / rel).read_text(encoding="utf-8"),
                    rel,
                )

    def test_node_installer_reinstall_updates_managed_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)

            workflow = project_root / ".agent-flow" / "workflows" / "full-feature.yaml"
            prompt = project_root / ".agent-flow" / "prompts" / "pr-watch.md"
            bootstrap = project_root / ".agent-flow" / "bootstrap" / "AGENTS.md"
            claude_bootstrap = project_root / ".agent-flow" / "bootstrap" / "CLAUDE.md"
            skill = project_root / ".agent-flow" / "skills" / "full-feature-workflow" / "SKILL.md"
            rules = project_root / ".agent-flow" / "rules" / "workflow-contract.md"
            runtime_lint = project_root / ".agent-flow" / "runtime" / "python" / "agent_flow" / "core" / "architecture_lint.py"
            workflow.write_text("stale workflow\n", encoding="utf-8")
            prompt.write_text("stale prompt\n", encoding="utf-8")
            bootstrap.write_text("stale bootstrap\n", encoding="utf-8")
            claude_bootstrap.write_text("stale claude bootstrap\n", encoding="utf-8")
            skill.write_text("stale skill\n", encoding="utf-8")
            rules.write_text("stale rules\n", encoding="utf-8")
            runtime_lint.write_text("stale runtime\n", encoding="utf-8")

            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)

            self.assertIn("id: full-feature", workflow.read_text(encoding="utf-8"))
            self.assertIn("Reviewers are installed Claude and Codex CLIs only", workflow.read_text(encoding="utf-8"))
            self.assertNotIn("Gemini sub-agent", workflow.read_text(encoding="utf-8"))
            self.assertIn("multi_review: true", workflow.read_text(encoding="utf-8"))
            self.assertIn("status: ci-failed", prompt.read_text(encoding="utf-8"))
            self.assertIn("def main(", runtime_lint.read_text(encoding="utf-8"))
            self.assertNotIn("stale runtime", runtime_lint.read_text(encoding="utf-8"))
            self.assertIn(
                "Reviewers are installed Claude and Codex CLIs only",
                (project_root / ".agent-flow" / "prompts" / "multi-review.md").read_text(encoding="utf-8"),
            )
            self.assertNotIn(
                "Gemini sub-agent",
                (project_root / ".agent-flow" / "prompts" / "multi-review.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "reviewer-source: sub-agent",
                (project_root / ".agent-flow" / "prompts" / "multi-review.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "Never use OMP or controller-session work",
                (project_root / ".agent-flow" / "prompts" / "multi-review.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "## Overall",
                (project_root / ".agent-flow" / "prompts" / "multi-review.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "verdict: approve",
                (project_root / ".agent-flow" / "prompts" / "multi-review.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "verdict: request-changes",
                (project_root / ".agent-flow" / "prompts" / "multi-review.md").read_text(encoding="utf-8"),
            )
            self.assertIn('agent-flow run "<task>"', bootstrap.read_text(encoding="utf-8"))
            self.assertIn(
                "requires at least two installed Claude/Codex CLI reviewer subprocesses",
                bootstrap.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "Use OMP as host/controller only, never as a reviewer provider",
                bootstrap.read_text(encoding="utf-8"),
            )
            self.assertNotIn("Claude/Gemini", bootstrap.read_text(encoding="utf-8"))
            self.assertIn("reviewer-source: sub-agent", bootstrap.read_text(encoding="utf-8"))
            # `CLAUDE.md` 사본은 루트에 실제로 심긴 것과 같아야 한다: 계약 본문이 아니라
            # 그것을 가리키는 포인터다. 여기에 본문을 담으면 루트와 사본이 갈라진다.
            self.assertIn("@AGENTS.md", claude_bootstrap.read_text(encoding="utf-8"))
            self.assertNotIn("### Workflow Contract", claude_bootstrap.read_text(encoding="utf-8"))
            # reviewer 실행 방식(별 subprocess, 병렬)은 phase 프롬프트가 쥔다. 블록은
            # 그 phase에만 쓰이는 절차를 사본으로 들지 않는다 — 두 벌이면 갈라진다.
            multi_review_prompt = (
                project_root / ".agent-flow" / "prompts" / "multi-review.md"
            ).read_text(encoding="utf-8")
            self.assertIn("confined subprocesses", multi_review_prompt)
            self.assertIn("Do not launch reviewer CLIs yourself", multi_review_prompt)
            self.assertIn("## Overall", bootstrap.read_text(encoding="utf-8"))
            self.assertIn("verdict: approve", bootstrap.read_text(encoding="utf-8"))
            self.assertIn("verdict: request-changes", bootstrap.read_text(encoding="utf-8"))
            self.assertEqual(skill.read_text(encoding="utf-8"), "stale skill\n")
            self.assertIn("Workflow Contract", rules.read_text(encoding="utf-8"))
            self.assertIn("two independent Claude/Codex reviewer subprocesses", rules.read_text(encoding="utf-8"))
            self.assertNotIn("Gemini sub-agent", rules.read_text(encoding="utf-8"))
            self.assertIn("reviewer-source: sub-agent", rules.read_text(encoding="utf-8"))
            self.assertIn("OMP and controller-session work are never reviewer providers", rules.read_text(encoding="utf-8"))
            self.assertIn("## Overall", rules.read_text(encoding="utf-8"))
            self.assertIn("verdict: approve", rules.read_text(encoding="utf-8"))
            self.assertIn("verdict: request-changes", rules.read_text(encoding="utf-8"))

    def test_node_installer_detects_node_project_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            (project_root / "package.json").write_text('{"scripts":{"test":"node test.js"}}\n', encoding="utf-8")
            node = _node_executable()
            result = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"),
                    "install",
                ),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.strip(),
                f"agent-flow installed profile=node root={project_root.resolve()}",
            )
            kit = json.loads((project_root / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
            self.assertEqual(kit["profile"], "node")

    def test_node_cli_routes_spec_confirm_to_the_python_cli(self) -> None:
        node = _node_executable()
        cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            run_dir = project_root / ".agent-flow" / "runs" / "r1"
            artifact = run_dir / "artifacts" / "design.md"
            artifact.parent.mkdir(parents=True)
            (run_dir / "active").touch()
            (run_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "task": "demo",
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "current_phase": "design",
                    }
                ),
                encoding="utf-8",
            )
            artifact.write_text(_SPEC_CONFIRM_ARTIFACT, encoding="utf-8")
            result = subprocess.run(
                (
                    node,
                    cli,
                    "spec",
                    "confirm",
                    "--run-dir",
                    ".agent-flow/runs/r1",
                ),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
                env=_node_test_env(),
            )
        combined = result.stdout + result.stderr
        self.assertNotIn("usage: agent-flow-kit", combined)
        self.assertNotIn("ModuleNotFoundError", combined)
        self.assertIn("SPEC changes confirmed:", combined)
        self.assertEqual(result.returncode, 0)

    def test_node_cli_requires_explicit_run_dir_for_spec_confirm(self) -> None:
        node = _node_executable()
        cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                (node, cli, "spec", "confirm"),
                cwd=temp_dir,
                text=True,
                capture_output=True,
                check=False,
                env=_node_test_env(),
            )

        combined = result.stdout + result.stderr
        self.assertNotIn("usage: agent-flow-kit", combined)
        self.assertIn("--run-dir", combined)
        self.assertEqual(result.returncode, 2)

    def test_node_cli_forwards_stdin_to_the_python_cli(self) -> None:
        """Python CLI 하위 명령이 stdin을 쓰는 경우 wrapper가 그대로 전달한다."""
        node = _node_executable()
        cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                (node, cli, "spec-stdin-probe"),
                cwd=temp_dir,
                text=True,
                capture_output=True,
                check=False,
                input="probe-line\n",
                env=_node_test_env(
                    PYTHON=str(Path(__file__).resolve().parent / "fixtures" / "stdin_probe.py"),
                ),
            )
        self.assertIn("stdin-received: probe-line", result.stdout + result.stderr)

    def test_node_cli_forwards_unknown_commands_instead_of_printing_usage(self) -> None:
        """`status`, `continue`도 워크플로가 안내하는 명령이다. 같은 자리에서 막히면 안 된다."""
        node = _node_executable()
        cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
        for subcommand in ("status", "continue"):
            with self.subTest(subcommand=subcommand):
                with tempfile.TemporaryDirectory() as temp_dir:
                    result = subprocess.run(
                        (node, cli, subcommand),
                        cwd=temp_dir,
                        text=True,
                        capture_output=True,
                        check=False,
                        env=_node_test_env(),
                    )
                self.assertNotIn("usage: agent-flow-kit", result.stdout + result.stderr)

    def test_node_run_forwards_a_task_to_the_python_cli(self) -> None:
        """`agent-flow run "<task>"`는 래퍼 자신의 안내문이 지시하는 형태다.

        `run` 서브트리만 화이트리스트로 남아 있으면 사용자는 도구가 시키는 대로
        치고도 usage만 보고 run을 시작조차 못 한다.
        """
        node = _node_executable()
        cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                (
                    node,
                    cli,
                    "run",
                    "add a dark mode toggle",
                    "--reuse-existing-worktree",
                ),
                cwd=temp_dir,
                text=True,
                capture_output=True,
                check=False,
                env=_node_test_env(
                    PYTHON=str(Path(__file__).resolve().parent / "fixtures" / "argv_probe.py"),
                ),
            )
        output = result.stdout + result.stderr
        self.assertNotIn("usage: agent-flow-kit run", output)
        self.assertIn(
            "argv-received: -m agent_flow.cli run add a dark mode toggle "
            "--reuse-existing-worktree",
            output,
        )

    def test_node_lifecycle_relays_worktree_argv_to_python(self) -> None:
        node = _node_executable()
        cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
        probe = str(Path(__file__).resolve().parent / "fixtures" / "argv_probe.py")
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "project"
            project.mkdir()
            installed = subprocess.run(
                (node, cli, "install"),
                cwd=project,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            commands = {
                "start": (
                    "run", "start", "--workflow", "default", "--task", "demo",
                    "--run-id", "r1", "--worktree", "feat-x",
                ),
                "status": ("run", "status", "--worktree", "feat-x"),
                "next": ("run", "next", "--worktree", "feat-x"),
                "advance": ("run", "advance", "--worktree", "feat-x"),
            }
            outputs = {}
            for name, command in commands.items():
                result = subprocess.run(
                    (node, cli, *command),
                    cwd=project,
                    text=True,
                    capture_output=True,
                    check=False,
                    env=_node_test_env(PYTHON=probe),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs[name] = result.stdout + result.stderr

        self.assertIn("-m agent_flow.cli start default", outputs["start"])
        self.assertIn("--run-id r1", outputs["start"])
        self.assertIn("--worktree feat-x", outputs["start"])
        self.assertIn("--checkout-identity worktree:feat-x", outputs["start"])
        self.assertIn("-m agent_flow.cli status", outputs["status"])
        self.assertIn("-m agent_flow.cli status", outputs["next"])
        self.assertIn("-m agent_flow.cli continue", outputs["advance"])
    def test_node_lifecycle_canonicalizes_a_branch_worktree_selector(self) -> None:
        node = _node_executable()
        cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
        probe = str(Path(__file__).resolve().parent / "fixtures" / "argv_probe.py")
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "project"
            project.mkdir()
            _init_git_repo(project)
            installed = subprocess.run(
                (node, cli, "install"),
                cwd=project,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            checkout = managed_worktrees_root(project) / "api-work"
            checkout.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ("git", "worktree", "add", "-b", "feat/api", str(checkout), "main"),
                cwd=project,
                text=True,
                capture_output=True,
                check=True,
            )

            result = subprocess.run(
                (
                    node,
                    cli,
                    "run",
                    "start",
                    "--workflow",
                    "default",
                    "--task",
                    "demo",
                    "--run-id",
                    "r1",
                    "--worktree",
                    "feat/api",
                ),
                cwd=project,
                text=True,
                capture_output=True,
                check=False,
                env=_node_test_env(PYTHON=probe),
            )
            derived_result = subprocess.run(
                (
                    node,
                    cli,
                    "run",
                    "start",
                    "--workflow",
                    "default",
                    "--task",
                    "demo",
                    "--run-id",
                    "r2",
                    "--worktree",
                    "api",
                ),
                cwd=project,
                text=True,
                capture_output=True,
                check=False,
                env=_node_test_env(PYTHON=probe),
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--worktree feat/api", result.stdout)
        self.assertIn(
            "--checkout-identity worktree:api-work",
            result.stdout,
        )
        self.assertEqual(derived_result.returncode, 0, derived_result.stderr)
        self.assertIn("--worktree api", derived_result.stdout)
        self.assertIn(
            "--checkout-identity worktree:api-work",
            derived_result.stdout,
        )



    def test_node_phase_runner_creates_managed_worktree_from_git_leader(self) -> None:
        node = _node_executable()
        cli = str(
            Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "project"
            project.mkdir()
            installed = subprocess.run(
                (node, cli, "install"),
                cwd=project,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            _init_git_repo(project)
            started = subprocess.run(
                (
                    node,
                    cli,
                    "run",
                    "start",
                    "--workflow",
                    "default",
                    "--task",
                    "demo",
                    "--run-id",
                    "r1",
                    "--allow-dirty",
                ),
                cwd=project,
                text=True,
                capture_output=True,
                check=False,
                env=_node_test_env(
                    AGENT_FLOW_ADAPTER="generic",
                    AGENT_FLOW_GENERIC_MODE="stub-success",
                ),
            )

            checkout = managed_worktrees_root(project) / "feat-demo"
            run_dir = _node_phase_run_dir(
                project,
                "r1",
                worktree="feat-demo",
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            self.assertTrue((checkout / ".git").is_file())
            self.assertTrue(run_dir.is_dir())
            meta = json.loads(
                (run_dir / "meta.json").read_text(encoding="utf-8")
            )
            self.assertEqual(meta["checkout_identity"], "worktree:feat-demo")
            self.assertFalse(
                (project / ".agent-flow" / "runs" / "r1").exists()
            )


    def test_node_start_from_leader_subdirectory_uses_python_state(self) -> None:
        node = _node_executable()
        cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "project"
            source = project / "src"
            source.mkdir(parents=True)
            installed = subprocess.run(
                (node, cli, "install"), cwd=project, text=True,
                capture_output=True, check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            _init_git_repo(project)
            started = subprocess.run(
                (
                    node, cli, "run", "start", "--workflow", "full-feature",
                    "--task", "demo", "--run-id", "r1",
                ),
                cwd=source, text=True, capture_output=True, check=False,
                env=_node_test_env(),
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            # 하위 디렉터리에서 시작해도 run은 leader가 아니라 관리형 worktree의
            # runtime state에 만들어지고, 상태 소유자는 Python 하나다.
            plan = plan_worktree(root=project, name="demo")
            run_dir = (
                worktree_runtime_root(root=project, name=plan.name)
                / ".agent-flow" / "runs" / "r1"
            )
            meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["checkout_identity"], f"worktree:{plan.name}")
            self.assertFalse(
                (project / ".agent-flow" / "state" / "current-run.json").exists()
            )

    def test_node_status_rejects_legacy_js_only_state(self) -> None:
        node = _node_executable()
        cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "project"
            project.mkdir()
            installed = subprocess.run(
                (node, cli, "install"), cwd=project, text=True,
                capture_output=True, check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            legacy = project / ".agent-flow" / "state" / "current-run.json"
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text('{"run_id":"legacy"}\n', encoding="utf-8")
            result = subprocess.run(
                (node, cli, "run", "status"), cwd=project, text=True,
                capture_output=True, check=False, env=_node_test_env(),
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("automatic fallback is disabled", result.stderr)

    def test_node_advance_blocks_empty_delivery_artifacts(self) -> None:
        node = _node_executable()
        cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
        definition = load_phase_workflow_definition(
            Path(__file__).resolve().parents[1], "full-feature"
        )
        for phase_id in ("commit", "push-pr"):
            with self.subTest(phase=phase_id), tempfile.TemporaryDirectory() as temp_dir:
                project = Path(temp_dir) / "project"
                project.mkdir()
                installed = subprocess.run(
                    (node, cli, "install"), cwd=project, text=True,
                    capture_output=True, check=False,
                )
                self.assertEqual(installed.returncode, 0, installed.stderr)
                phase_index, phase = next(
                    (index, item)
                    for index, item in enumerate(definition.phases)
                    if item.id == phase_id
                )
                run_dir = project / ".agent-flow" / "runs" / "r1"
                run_dir.mkdir(parents=True)
                (run_dir / "active").touch()
                (run_dir / "meta.json").write_text(
                    json.dumps(
                        {
                            "run_id": "r1",
                            "workflow": "full-feature",
                            "task": "delivery",
                            "started_at": "2020-01-01T00:00:00+00:00",
                            "phase_entered_at": "2020-01-01T00:00:00+00:00",
                            "phase_index": phase_index,
                            "current_phase": phase_id,
                            "checkout_identity": "leader",
                        }
                    ),
                    encoding="utf-8",
                )
                artifact = run_dir / phase.artifact
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text("", encoding="utf-8")
                result = subprocess.run(
                    (node, cli, "run", "advance"), cwd=project, text=True,
                    capture_output=True, check=False, env=_node_test_env(),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                output = result.stdout + result.stderr
                self.assertIn("missing completion markers", output.lower())
                self.assertIn("delivery evidence:", output)
                persisted = json.loads(
                    (run_dir / "meta.json").read_text(encoding="utf-8")
                )
                self.assertEqual(persisted["current_phase"], phase_id)


    def test_node_git_calls_ignore_a_poisoned_git_dir(self) -> None:
        """오염된 GIT_DIR이 우리 git을 요청한 cwd 밖으로 돌리면 안 된다.

        Python `git_safe`는 이미 discovery 환경변수를 벗긴다. JS가 안 벗기면 같은
        cwd에서 두 런타임이 서로 다른 root를 고른다.
        """
        node = _node_executable()
        shared = (
            Path(__file__).resolve().parents[1] / "lib" / "installer-shared.mjs"
        ).as_posix()
        with tempfile.TemporaryDirectory() as temp_dir:
            here = Path(temp_dir) / "here"
            elsewhere = Path(temp_dir) / "elsewhere"
            for repo in (here, elsewhere):
                repo.mkdir()
                subprocess.run(("git", "init", "-q"), cwd=repo, check=True)
            probe = Path(temp_dir) / "probe.mjs"
            # 사본을 잘라 붙이지 않고 배포되는 모듈을 그대로 부른다. 두 진입점이 이제
            # 이 하나를 쓰므로 여기만 막으면 둘 다 막힌다.
            probe.write_text(
                f"import {{ gitOutput }} from '{shared}';\n"
                + "import process from 'node:process';\n"
                + "console.log(gitOutput(process.argv[2], "
                + "['rev-parse', '--show-toplevel']));\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, str(probe), str(here)),
                text=True,
                capture_output=True,
                check=True,
                env={**os.environ, "GIT_DIR": str(elsewhere / ".git")},
            )
        resolved = Path(result.stdout.strip()).resolve()
        self.assertEqual(resolved, here.resolve())

    def test_node_installer_uses_source_workflow_yaml_for_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            result = subprocess.run(
                (node, cli, "install"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            source_workflow = (Path(__file__).resolve().parents[1] / "src" / "agent_flow" / "workflows" / "full-feature.yaml").read_text(encoding="utf-8")
            installed_workflow = (project_root / ".agent-flow" / "workflows" / "full-feature.yaml").read_text(encoding="utf-8")
            self.assertEqual(installed_workflow, source_workflow)
            prompt = (project_root / ".agent-flow" / "prompts" / "product-brief.md").read_text(encoding="utf-8")
            self.assertIn("Apply YC office-hours style pressure", prompt)
            self.assertNotIn("Validate demand, status quo", prompt)

    def test_node_installer_matches_project_profile_detection(self) -> None:
        cases = [
            ("nextjs", {"package.json": '{"dependencies":{"next":"latest"}}\n'}),
            ("react-native", {"package.json": '{"dependencies":{"react-native":"latest"}}\n'}),
            ("python", {"pyproject.toml": "[project]\nname='demo'\n"}),
            ("typescript", {"package.json": '{"scripts":{"test":"node test.js"}}\n', "tsconfig.json": "{}\n"}),
            ("generic", {"tsconfig.json": "{}\n"}),
            ("android", {"settings.gradle.kts": 'pluginManagement { repositories { google() } }\n'}),
            ("android", {"settings.gradle": "pluginManagement { repositories { google() } }\n"}),
        ]
        node = _node_executable()
        for expected, files in cases:
            with self.subTest(expected=expected):
                with tempfile.TemporaryDirectory() as temp_dir:
                    project_root = Path(temp_dir) / "project"
                    project_root.mkdir()
                    for name, content in files.items():
                        (project_root / name).write_text(content, encoding="utf-8")
                    result = subprocess.run(
                        (
                            node,
                            str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"),
                            "install",
                        ),
                        cwd=project_root,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    kit = json.loads((project_root / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
                    self.assertEqual(kit["profile"], expected)

    def test_node_installer_package_exposes_npx_bin(self) -> None:
        package = json.loads((Path(__file__).resolve().parents[1] / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["name"], "agent-flow-kit")
        self.assertEqual(package["bin"]["agent-flow-kit"], "bin/agent-flow-kit.mjs")
        # `agent-flow` 이름은 Python CLI(pyproject.toml) 단독 소유다. npm이 가로채면 status/run이 죽는다.
        self.assertNotIn("agent-flow", package["bin"])

    def test_install_records_a_kit_source_digest_and_warns_when_stale(self) -> None:
        """낡은 설치본은 조용히 옛 workflow/profile/runtime을 돌린다.

        `skills sync`는 이것을 고치지 않는다 — 그 명령은 외부 skill_sources만
        fetch한다. 알려주는 코드가 없으면 사용자는 재설치할 이유를 알 수 없다.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            install = subprocess.run(
                (node, cli, "install"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stderr)

            kit_path = project_root / ".agent-flow" / "kit.json"
            payload = json.loads(kit_path.read_text(encoding="utf-8"))
            digest = payload.get("kit_source_digest")
            self.assertIsInstance(digest, str)
            self.assertEqual(len(digest), 64)
            payload["kit_source_digest"] = kit_source_digest(
                Path(__file__).resolve().parents[1]
            )
            kit_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            fresh = subprocess.run(
                (node, cli, "run", "status"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(fresh.returncode, 0, fresh.stderr)
            self.assertNotIn("agent-flow-kit install", fresh.stderr)

            payload["kit_source_digest"] = "0" * 64
            kit_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            stale = subprocess.run(
                (node, cli, "run", "status"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(stale.returncode, 0, stale.stderr)
            self.assertIn("run: agent-flow-kit install", stale.stderr)

            # 지문을 기록하기 전에 설치된 프로젝트에는 대조 기준이 없다. 판정하지 않는다.
            del payload["kit_source_digest"]
            kit_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            legacy = subprocess.run(
                (node, cli, "run", "status"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(legacy.returncode, 0, legacy.stderr)
            self.assertNotIn("agent-flow-kit install", legacy.stderr)

    def test_python_entry_points_warn_about_a_stale_install(self) -> None:
        """문서가 안내하는 진입점은 Python CLI다.

        JS 래퍼에만 검사가 있으면 `agent-flow status`만 쓰는 사용자는 kit을 올린
        뒤에도 낡은 설치본을 끝까지 못 본다.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            (project_root / ".agent-flow").mkdir(parents=True)
            kit_path = project_root / ".agent-flow" / "kit.json"

            kit_path.write_text(
                json.dumps({"kit_source_digest": "0" * 64}), encoding="utf-8"
            )
            stale = io.StringIO()
            with contextlib.redirect_stderr(stale):
                main(["status", "--root", str(project_root)])
            self.assertIn("run: agent-flow-kit install", stale.getvalue())

            # 지문을 기록하기 전에 설치된 프로젝트에는 대조 기준이 없다.
            kit_path.write_text(json.dumps({"profile": "python"}), encoding="utf-8")
            legacy = io.StringIO()
            with contextlib.redirect_stderr(legacy):
                main(["status", "--root", str(project_root)])
            self.assertNotIn("agent-flow-kit install", legacy.getvalue())

    def test_node_run_double_dash_forwards_a_one_word_task(self) -> None:
        """`stats`처럼 서브커맨드에 가까운 한 단어도 정상적인 task일 수 있다.

        오타 가드에 escape가 없으면 그 task는 래퍼로 영영 시작할 수 없다.
        """
        node = _node_executable()
        cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
        with tempfile.TemporaryDirectory() as temp_dir:
            guarded = subprocess.run(
                (node, cli, "run", "stats"),
                cwd=temp_dir,
                text=True,
                capture_output=True,
                check=False,
                env=_node_test_env(),
            )
            self.assertEqual(guarded.returncode, 1)
            self.assertIn("run -- stats", guarded.stdout + guarded.stderr)

            escaped = subprocess.run(
                (node, cli, "run", "--", "stats"),
                cwd=temp_dir,
                text=True,
                capture_output=True,
                check=False,
                env=_node_test_env(
                    PYTHON=str(Path(__file__).resolve().parent / "fixtures" / "argv_probe.py"),
                ),
            )
        output = escaped.stdout + escaped.stderr
        self.assertIn("argv-received: -m agent_flow.cli run stats", output)

    def test_node_workflow_run_blocks_phase_skip_until_artifact_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            install = subprocess.run(
                (node, cli, "install"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            _init_git_repo(project_root)
            # run은 leader가 아니라 managed worktree 안에서 돈다. 이름/경로는
            # 프로덕션 planner에서 유도한다 — 문자열로 박으면 slug 규칙이 바뀔 때 깨진다.
            plan = plan_worktree(root=project_root, name="demo feature")

            start = subprocess.run(
                (node, cli, "run", "start", "--task", "demo feature", "--run-id", "r1"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(start.returncode, 0, start.stderr)
            self.assertIn("current_phase: domain-grill", start.stdout)

            status = subprocess.run(
                (node, cli, "run", "status"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("status: awaiting_host", status.stdout)
            self.assertIn("reason: missing_phase_artifact", status.stdout)
            self.assertIn("next_command: agent-flow continue --root", status.stdout)
            self.assertIn("status_json:", status.stdout)

            blocked = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(blocked.returncode, 0, blocked.stderr)
            self.assertIn("reason: missing_phase_artifact", blocked.stdout)

            artifact = _node_phase_run_dir(project_root, worktree=plan.name) / "artifacts" / "domain-grill.md"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("domain-grill\n", encoding="utf-8")

            blocked_status = subprocess.run(
                (node, cli, "run", "status"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(blocked_status.returncode, 0, blocked_status.stderr)
            blocked_json = next(
                line
                for line in blocked_status.stdout.splitlines()
                if line.startswith("status_json: ")
            )
            blocked_payload = json.loads(
                blocked_json.removeprefix("status_json: ")
            )
            self.assertIn(
                "domain-grill: complete",
                blocked_payload["missing_completion_markers"],
            )

            missing_markers = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(missing_markers.returncode, 0, missing_markers.stderr)
            self.assertIn("missing completion markers", missing_markers.stdout)

            artifact.write_text(
                "TODO: add domain-grill: complete before handoff\n"
                "shared_understanding: reached\n",
                encoding="utf-8",
            )
            false_positive = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(false_positive.returncode, 0, false_positive.stderr)
            self.assertIn("domain-grill: complete", false_positive.stdout)

            artifact.write_text(
                "domain-grill: complete\n"
                "shared_understanding: reached\n",
                encoding="utf-8",
            )
            outside_gate = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(outside_gate.returncode, 0, outside_gate.stderr)
            self.assertIn("missing completion markers", outside_gate.stdout)

            artifact.write_text(
                "## Completion Gate\n"
                "```\n"
                "domain-grill: complete\n"
                "shared_understanding: reached\n"
                "```\n",
                encoding="utf-8",
            )
            fenced_example = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(fenced_example.returncode, 0, fenced_example.stderr)
            self.assertIn("missing completion markers", fenced_example.stdout)

            artifact.write_text(
                "```\n"
                "## Completion Gate\n"
                "domain-grill: complete\n"
                "shared_understanding: reached\n"
                "```\n",
                encoding="utf-8",
            )
            fenced_heading = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(fenced_heading.returncode, 0, fenced_heading.stderr)
            self.assertIn("missing completion markers", fenced_heading.stdout)

            artifact.write_text(
                "notes\n"
                "    ## Completion Gate\n"
                "    domain-grill: complete\n"
                "    shared_understanding: reached\n",
                encoding="utf-8",
            )
            indented_heading = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(indented_heading.returncode, 0, indented_heading.stderr)
            self.assertIn("missing completion markers", indented_heading.stdout)

            artifact.write_text(
                "## Completion Gate\n"
                "domain-grill: complete\n"
                "shared_understanding: reached\n"
                "context_docs_checked: true\n"
                "context_docs_updated: maybe\n",
                encoding="utf-8",
            )
            bad_value = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(bad_value.returncode, 0, bad_value.stderr)
            self.assertIn("context_docs_updated: true|not_needed", bad_value.stdout)

            artifact.write_text(
                "## Completion Gate\n"
                "- [x] domain-grill: complete\n"
                "* shared_understanding: reached\n"
                "+ context_docs_checked: true\n"
                "- context_docs_updated: not_needed\n",
                encoding="utf-8",
            )
            status = subprocess.run(
                (node, cli, "run", "status"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("status: blocked", status.stdout)
            self.assertIn("reason: phase_artifact_written_continue_required", status.stdout)
            self.assertIn("status_json:", status.stdout)

            advanced = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(advanced.returncode, 0, advanced.stderr)
            self.assertIn("current_phase: product-brief", advanced.stdout)
            state = _read_node_phase(_node_phase_run_dir(project_root, worktree=plan.name))
            self.assertEqual(state["current_phase"], "product-brief")

    def test_node_heading_required_markers_ignore_fenced_examples(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            _init_git_repo(project_root)
            # run은 leader가 아니라 managed worktree 안에서 돈다. 이름/경로는
            # 프로덕션 planner에서 유도한다 — 문자열로 박으면 slug 규칙이 바뀔 때 깨진다.
            plan = plan_worktree(root=project_root, name="demo")
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = _node_phase_run_dir(project_root, worktree=plan.name)
            for phase in ["domain-grill", "product-brief", "prd", "slice-plan", "plan-review"]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                content = "verdict: approve\n" if phase == "plan-review" else _node_phase_content(phase, run_dir=run_dir)
                artifact.write_text(content, encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=plan.path, check=False).returncode, 0)

            ddd_artifact = run_dir / _node_phase_artifact("ddd-design")
            ddd_artifact.write_text(
                "```\n"
                "## Clean Architecture Boundary Map\n"
                "## Dependency Rule\n"
                "## Use Case Boundaries\n"
                "## Repository Boundaries\n"
                "## Cache Boundary\n"
                "## Mapping Boundary\n"
                "## Composition Root\n"
                "## Testability Boundary\n"
                "```\n"
                "## Completion Gate\n"
                "clean-architecture: applied\n"
                "usecase-interface: n/a\n"
                "usecase-composition: none\n"
                "cache-required: no\n"
                "memory-cache: n/a\n"
                "disk-cache: n/a\n"
                "cache-invalidation-policy: n/a\n"
                "remote-dto-domain-mapper: n/a\n"
                "entity-domain-mapper: n/a\n"
                "domain-ui-mapper: n/a\n"
                "solid-srp-change-reason: n/a\n"
                "solid-ocp-extension-points: n/a\n"
                "solid-lsp-contracts: n/a\n"
                "solid-isp-consumer-ports: n/a\n"
                "solid-dip-dependency-direction: inward\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("## Clean Architecture Boundary Map", result.stdout)

    def test_node_run_enforces_project_local_code_review_skill_markers_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            _write_local_skill_files(project_root)
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            _init_git_repo(project_root)
            # run은 leader가 아니라 managed worktree 안에서 돈다. 이름/경로는
            # 프로덕션 planner에서 유도한다 — 문자열로 박으면 slug 규칙이 바뀔 때 깨진다.
            plan = plan_worktree(root=project_root, name="demo")
            baked_prompt = (project_root / ".agent-flow" / "prompts" / "green.md").read_text(
                encoding="utf-8"
            )
            # install 시점 스냅샷에는 skill 목록을 굽지 않는다. 새 skill을 설치하면 바로 stale해지기 때문이다.
            self.assertNotIn("samantha-architecture-guide/SKILL.md", baked_prompt)
            self.assertIn("computed live", baked_prompt)
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--workflow", "default", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = _node_phase_run_dir(project_root, worktree=plan.name)
            artifact = _set_node_phase(run_dir, "implement", workflow="default")
            phase_prompt = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(phase_prompt.returncode, 0, phase_prompt.stderr)
            self.assertIn("api-contract-guide/SKILL.md", phase_prompt.stdout)
            self.assertIn("project-local-skill-docs: applied", phase_prompt.stdout)
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(
                _node_implement_gate(local_skill=False, run_dir=run_dir),
                encoding="utf-8",
            )

            missing_local = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(missing_local.returncode, 0)
            self.assertIn("project-local-skills: checked", missing_local.stdout)
            self.assertIn("project-local-skill-docs: applied", missing_local.stdout)

            artifact.write_text(
                _node_implement_gate(local_skill=True, run_dir=run_dir).replace("api, ", ""),
                encoding="utf-8",
            )
            partial_local = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertIn("project-local-skills-used:", partial_local.stdout)

            artifact.write_text(
                _node_implement_gate(local_skill=True, run_dir=run_dir),
                encoding="utf-8",
            )
            advanced = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(advanced.returncode, 0, advanced.stderr)
            self.assertIn("current_phase: comment-authoring", advanced.stdout)

    def test_node_run_enforces_project_local_skills_for_bugfix_code_phases(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            _write_local_skill_files(project_root)
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            _init_git_repo(project_root)
            # run은 leader가 아니라 managed worktree 안에서 돈다. 이름/경로는
            # 프로덕션 planner에서 유도한다 — 문자열로 박으면 slug 규칙이 바뀔 때 깨진다.
            plan = plan_worktree(root=project_root, name="demo")
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--workflow", "bugfix", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = _node_phase_run_dir(project_root, worktree=plan.name)
            artifact = _set_node_phase(run_dir, "implement-fix", workflow="bugfix")
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(
                "## Completion Gate\nproject-local-skills: n/a\nproject-local-skills-used: n/a\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("project-local-skills: checked", result.stdout)

    def test_node_workflow_run_requires_installed_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            subprocess.run(
                ("git", "init"),
                cwd=project_root,
                check=True,
                capture_output=True,
            )
            node = _node_executable()
            result = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"),
                    "run",
                    "start",
                    "--task",
                    "demo",
                ),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("agent-flow is not installed", result.stderr)

    def test_node_workflow_run_accepts_filtered_profile_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            install = subprocess.run(
                (node, cli, "install", "--profile", "android"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            _init_git_repo(project_root)
            # run은 leader가 아니라 managed worktree 안에서 돈다. 이름/경로는
            # 프로덕션 planner에서 유도한다 — 문자열로 박으면 slug 규칙이 바뀔 때 깨진다.
            plan = plan_worktree(root=project_root, name="demo")
            self.assertFalse((project_root / ".agent-flow" / "skills" / "ios-clean-architecture").exists())
            self.assertFalse((project_root / ".agent-flow" / "skills" / "react-native-clean-architecture").exists())

            result = subprocess.run(
                (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("current_phase: domain-grill", result.stdout)

    def test_node_workflow_run_accepts_project_skill_index_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            skill_dir = project_root / "skills" / "my-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: my-skill\ndescription: Use when testing project skills.\n---\n\nBody\n",
                encoding="utf-8",
            )
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            install = subprocess.run(
                (node, cli, "install", "--profile", "android"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            _init_git_repo(project_root)
            # run은 leader가 아니라 managed worktree 안에서 돈다. 이름/경로는
            # 프로덕션 planner에서 유도한다 — 문자열로 박으면 slug 규칙이 바뀔 때 깨진다.
            plan = plan_worktree(root=project_root, name="demo")

            result = subprocess.run(
                (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("current_phase: domain-grill", result.stdout)

    def test_node_workflow_run_rejects_pre_upgrade_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            agent_flow = project_root / ".agent-flow"
            for path_name in [
                agent_flow / "kit.json",
                agent_flow / "workflows" / "full-feature.yaml",
                agent_flow / "skills" / "full-feature-workflow" / "SKILL.md",
                agent_flow / "bootstrap" / "AGENTS.md",
                agent_flow / "bootstrap" / "CLAUDE.md",
            ]:
                path_name.parent.mkdir(parents=True, exist_ok=True)
                path_name.write_text("old install\n", encoding="utf-8")

            node = _node_executable()
            result = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"),
                    "run",
                    "start",
                    "--task",
                    "demo",
                ),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("agent-flow is not installed", result.stderr)

    @mock.patch.dict(
        os.environ,
        {"AGENT_FLOW_ADAPTER": "generic", "AGENT_FLOW_GENERIC_MODE": "emit"},
        clear=False,
    )
    def test_node_workflow_run_advances_every_phase_up_to_the_delivery_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(
                subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode,
                0,
            )
            _init_git_repo(project_root)
            # run은 leader가 아니라 managed worktree 안에서 돈다. 이름/경로는
            # 프로덕션 planner에서 유도한다 — 문자열로 박으면 slug 규칙이 바뀔 때 깨진다.
            plan = plan_worktree(root=project_root, name="demo")
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            expected_phases = [
                "domain-grill",
                "product-brief",
                "prd",
                "slice-plan",
                "plan-review",
                "ddd-design",
                "worktree",
                "run-start",
                "red",
                "green",
                "refactor",
                "comment-authoring",
                "multi-review",
                "architecture-review",
                # `gates`는 여기 없다. runner가 직접 돌리고 결과 파일도 직접 쓰므로
                # host가 서는 phase가 아니다 — architecture-review를 떠난 advance가
                # gates를 통과해 commit에 선다.
                "commit",
                "push-pr",
            ]
            run_dir = _node_phase_run_dir(project_root, worktree=plan.name)
            for index, phase in enumerate(expected_phases):
                state = _read_node_phase(run_dir)
                self.assertEqual(state["current_phase"], phase)
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                if phase == "pr-watch":
                    content = "status: green\n"
                elif phase in {"plan-review", "merge-approval"}:
                    content = "verdict: approve\n"
                elif phase == "commit":
                    # delivery gate는 기록된 OID/subject를 실제 HEAD와 대조한다.
                    # 문자열만 적으면 통과하지 않는다 — 진짜 커밋에서 유도한다.
                    subprocess.run(
                        ("git", "commit", "--allow-empty", "-m", "feat: demo"),
                        cwd=plan.path,
                        check=True,
                        capture_output=True,
                    )
                    head = subprocess.run(
                        ("git", "rev-parse", "HEAD"),
                        cwd=plan.path,
                        text=True,
                        capture_output=True,
                        check=True,
                    ).stdout.strip()
                    content = (
                        "commit\n"
                        "## Completion Gate\n"
                        f"commit-oid: {head}\n"
                        "commit-subject: feat: demo\n"
                    )
                else:
                    content = _node_phase_content(phase, run_dir=run_dir)
                artifact.write_text(content, encoding="utf-8")
                advance = subprocess.run(
                    (node, cli, "run", "advance"),
                    cwd=plan.path,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(advance.returncode, 0, advance.stderr)
                if index + 1 < len(expected_phases):
                    self.assertIn(
                        f"current_phase: {expected_phases[index + 1]}",
                        advance.stdout,
                    )

            # push-pr는 원격과 PR을 실제로 증명해야 넘어간다. 오프라인 fixture로는
            # 증명할 수 없고, 증명 없이 넘어가면 그게 배달 게이트의 구멍이다.
            self.assertIn("status: blocked", advance.stdout)
            self.assertIn("current_phase: push-pr", advance.stdout)
            self.assertIn("delivery evidence", advance.stdout)

    def test_node_workflow_next_relays_python_status_without_mutating_meta(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            _init_git_repo(project_root)
            # run은 leader가 아니라 managed worktree 안에서 돈다. 이름/경로는
            # 프로덕션 planner에서 유도한다 — 문자열로 박으면 slug 규칙이 바뀔 때 깨진다.
            plan = plan_worktree(root=project_root, name="demo")
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = _node_phase_run_dir(project_root, worktree=plan.name)
            meta_path = run_dir / "meta.json"
            state = _read_node_phase(run_dir)
            state["current_phase"] = "red"
            state["phase_index"] = 0
            meta_path.write_text(f"{json.dumps(state, indent=2)}\n", encoding="utf-8")

            next_result = subprocess.run(
                (node, cli, "run", "next"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(next_result.returncode, 0, next_result.stderr)
            self.assertIn("current_phase: red", next_result.stdout)
            unchanged = _read_node_phase(run_dir)
            self.assertEqual(unchanged["current_phase"], "red")
            self.assertEqual(unchanged["phase_index"], 0)

    @mock.patch.dict(
        os.environ,
        {"AGENT_FLOW_ADAPTER": "generic", "AGENT_FLOW_GENERIC_MODE": "emit"},
        clear=False,
    )
    def test_node_pr_watch_blocks_pending_and_routes_fix_loops_back_to_watch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            _init_git_repo(project_root)
            # run은 leader가 아니라 managed worktree 안에서 돈다. 이름/경로는
            # 프로덕션 planner에서 유도한다 — 문자열로 박으면 slug 규칙이 바뀔 때 깨진다.
            plan = plan_worktree(root=project_root, name="demo")
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = _node_phase_run_dir(project_root, worktree="feat-demo")
            project_root = (
                managed_worktrees_root(project_root) / "feat-demo"
            )
            _set_node_phase(run_dir, "pr-watch")

            watch = run_dir / _node_phase_artifact("pr-watch")
            watch.parent.mkdir(parents=True, exist_ok=True)
            watch.write_text("status: pending\n", encoding="utf-8")
            pending = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(pending.returncode, 0)
            self.assertIn("[block] pr-watch status=pending", pending.stdout)
            pending_status = subprocess.run(
                (node, cli, "run", "status"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(pending_status.returncode, 0, pending_status.stderr)
            self.assertIn("reason: route_blocked", pending_status.stdout)
            self.assertIn("next_command: agent-flow continue --root", pending_status.stdout)

            watch.write_text("status: comments\n", encoding="utf-8")
            comments = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(comments.returncode, 0, comments.stderr)
            self.assertIn("current_phase: pr-comment-fix", comments.stdout)
            comment_fix = run_dir / _node_phase_artifact("pr-comment-fix")
            comment_fix.write_text("old comment fix\n", encoding="utf-8")
            os.utime(comment_fix, (1, 1))
            stale_comment_fix = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(stale_comment_fix.returncode, 0)
            self.assertIn("reason: stale_artifact", stale_comment_fix.stdout)
            stale_comment_status = subprocess.run(
                (node, cli, "run", "status"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(stale_comment_status.returncode, 0, stale_comment_status.stderr)
            self.assertIn("reason: stale_artifact", stale_comment_status.stdout)
            self.assertIn("next_command: agent-flow continue --root", stale_comment_status.stdout)
            comment_fix.write_text(_node_phase_content("pr-comment-fix", "pushed comment fixes "), encoding="utf-8")
            same_ms = _read_node_phase(run_dir)
            entered_ts = _node_epoch_seconds(same_ms["phase_entered_at"])
            os.utime(comment_fix, (entered_ts, entered_ts))
            back_to_watch = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(back_to_watch.returncode, 0, back_to_watch.stderr)
            self.assertIn("current_phase: pr-watch", back_to_watch.stdout)

            watch.write_text("status: comments\n", encoding="utf-8")
            comments_again = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(comments_again.returncode, 0, comments_again.stderr)
            self.assertIn("current_phase: pr-comment-fix", comments_again.stdout)
            reused_comment_fix = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(reused_comment_fix.returncode, 0)
            self.assertIn("reason: missing_phase_artifact", reused_comment_fix.stdout)
            comment_fix.write_text(_node_phase_content("pr-comment-fix", "pushed second comment fixes "), encoding="utf-8")
            self.assertEqual(
                subprocess.run((node, cli, "run", "advance"), cwd=plan.path, check=False).returncode,
                0,
            )

            watch.write_text("status: ci-failed\n", encoding="utf-8")
            ci_failed = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(ci_failed.returncode, 0, ci_failed.stderr)
            self.assertIn("current_phase: pr-ci-fix", ci_failed.stdout)
            ci_fix = run_dir / _node_phase_artifact("pr-ci-fix")
            ci_fix.write_text(_node_phase_content("pr-ci-fix", "old ci fixes "), encoding="utf-8")
            os.utime(ci_fix, (1, 1))
            stale_ci_fix = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(stale_ci_fix.returncode, 0)
            self.assertIn("reason: stale_artifact", stale_ci_fix.stdout)
            ci_fix.write_text(_node_phase_content("pr-ci-fix", "pushed ci fixes "), encoding="utf-8")
            back_to_watch_again = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(back_to_watch_again.returncode, 0, back_to_watch_again.stderr)
            self.assertIn("current_phase: pr-watch", back_to_watch_again.stdout)

            watch.write_text("status: green\n", encoding="utf-8")
            ready = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(ready.returncode, 0, ready.stderr)
            self.assertIn("current_phase: merge-approval", ready.stdout)

    @mock.patch.dict(
        os.environ,
        {"AGENT_FLOW_ADAPTER": "generic", "AGENT_FLOW_GENERIC_MODE": "emit"},
        clear=False,
    )
    def test_node_plan_review_and_architecture_review_route_request_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            _init_git_repo(project_root)
            # run은 leader가 아니라 managed worktree 안에서 돈다. 이름/경로는
            # 프로덕션 planner에서 유도한다 — 문자열로 박으면 slug 규칙이 바뀔 때 깨진다.
            plan = plan_worktree(root=project_root, name="demo")
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = _node_phase_run_dir(project_root, worktree=plan.name)
            for phase in ["domain-grill", "product-brief", "prd", "slice-plan"]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text(_node_phase_content(phase, run_dir=run_dir), encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=plan.path, check=False).returncode, 0)

            plan_review = run_dir / _node_phase_artifact("plan-review")
            plan_review.write_text("verdict: REQUEST-CHANGES\n", encoding="utf-8")
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("current_phase: slice-plan", result.stdout)

            slice_plan = run_dir / _node_phase_artifact("slice-plan")
            missing_slice_plan = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(missing_slice_plan.returncode, 0)
            self.assertIn("reason: missing_phase_artifact", missing_slice_plan.stdout)

            slice_plan.write_text("updated slice-plan\n", encoding="utf-8")
            self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=plan.path, check=False).returncode, 0)
            plan_review.write_text("verdict: APPROVE\n", encoding="utf-8")
            self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=plan.path, check=False).returncode, 0)
            ddd = run_dir / _node_phase_artifact("ddd-design")
            ddd.write_text(_node_phase_content("ddd-design"), encoding="utf-8")
            self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=plan.path, check=False).returncode, 0)
            for phase in [
                "worktree",
                "run-start",
                "red",
                "green",
                "refactor",
                "comment-authoring",
                "multi-review",
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text(_node_phase_content(phase, run_dir=run_dir), encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=plan.path, check=False).returncode, 0)

            architecture_review = run_dir / _node_phase_artifact("architecture-review")
            architecture_review.write_text(
                _node_phase_content("architecture-review").replace("verdict: approve", "verdict: request-changes"),
                encoding="utf-8",
            )
            _write_node_review_results(
                run_dir,
                "architecture-review",
                ("approve", "request-changes"),
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("current_phase: refactor", result.stdout)
            refactor = run_dir / _node_phase_artifact("refactor")
            missing_refactor = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(missing_refactor.returncode, 0)
            self.assertIn("reason: missing_phase_artifact", missing_refactor.stdout)

            refactor.write_text(_node_phase_content("refactor", "updated "), encoding="utf-8")
            self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=plan.path, check=False).returncode, 0)
            for phase, next_phase in [
                ("comment-authoring", "multi-review"),
                ("multi-review", "architecture-review"),
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                missing_artifact = subprocess.run(
                    (node, cli, "run", "advance"),
                    cwd=plan.path,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(missing_artifact.returncode, 0)
                expected_reason = (
                    "generic_stub_artifact"
                    if phase == "multi-review"
                    else "missing_phase_artifact"
                )
                self.assertIn(f"reason: {expected_reason}", missing_artifact.stdout)

                artifact.write_text(_node_phase_content(phase, prefix="updated "), encoding="utf-8")
                if phase == "multi-review":
                    _write_node_review_results(run_dir, phase)
                advanced = subprocess.run(
                    (node, cli, "run", "advance"),
                    cwd=plan.path,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(advanced.returncode, 0, advanced.stderr)
                self.assertIn(f"current_phase: {next_phase}", advanced.stdout)

            missing_architecture_review = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(missing_architecture_review.returncode, 0)
            self.assertIn("reason: generic_stub_artifact", missing_architecture_review.stdout)

            architecture_review.write_text(_node_phase_content("architecture-review"), encoding="utf-8")
            _write_node_review_results(run_dir, "architecture-review")
            # architecture-review approve → gates. runner가 gates를 직접 돌리므로 이
            # advance 하나가 gates까지 통과해 commit에 선다. 예전에는 여기서 멈추고
            # host가 gate 결과 파일을 써 줄 때까지 기다렸다.
            approved = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)
            self.assertIn("current_phase: commit", approved.stdout)

    @mock.patch.dict(
        os.environ,
        {"AGENT_FLOW_ADAPTER": "generic", "AGENT_FLOW_GENERIC_MODE": "emit"},
        clear=False,
    )
    def test_node_gates_fail_routes_to_fix_loop_and_back(self) -> None:
        """gates fail → fix-loop → review → gates 순환 테스트."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            _init_git_repo(project_root)
            _declare_conditional_gate(project_root)
            # run은 leader가 아니라 managed worktree 안에서 돈다. 이름/경로는
            # 프로덕션 planner에서 유도한다 — 문자열로 박으면 slug 규칙이 바뀔 때 깨진다.
            plan = plan_worktree(root=project_root, name="demo")
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = _node_phase_run_dir(project_root, worktree=plan.name)
            # 선언된 gate를 실패로 돌린다. `run start`가 worktree를 이미 만들었고
            # gate는 그 worktree를 cwd로 돌기 때문에 이 파일 하나가 판정을 뒤집는다.
            (plan.path / "gate-must-fail").write_text("", encoding="utf-8")
            for phase in [
                "domain-grill", "product-brief", "prd",
                "slice-plan", "plan-review", "ddd-design", "worktree",
                "run-start", "red", "green", "refactor",
                "comment-authoring", "multi-review", "architecture-review",
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                content = "verdict: approve\n" if phase == "plan-review" else _node_phase_content(phase, run_dir=run_dir)
                artifact.write_text(content, encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=plan.path, check=False).returncode, 0)

            # runner가 gates를 직접 돌린다. marker 파일 때문에 선언된 gate가 실제로
            # 실패했고, 그래서 이 advance는 gates에 서지 않고 fix-loop로 갔다.
            state = _read_node_phase(run_dir)
            self.assertEqual(state["current_phase"], "fix-loop")

            fix_loop_artifact = run_dir / _node_phase_artifact("fix-loop")
            fix_loop_artifact.write_text(
                _node_phase_content("fix-loop", run_dir=run_dir),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("current_phase: comment-authoring", result.stdout)

            comment_artifact = run_dir / _node_phase_artifact("comment-authoring")
            comment_artifact.write_text(_node_phase_content("comment-authoring"), encoding="utf-8")
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("current_phase: multi-review", result.stdout)

            multi_review = run_dir / _node_phase_artifact("multi-review")
            multi_review.write_text(
                _node_phase_content("multi-review", run_dir=run_dir),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("current_phase: architecture-review", result.stdout)

            architecture_review = run_dir / _node_phase_artifact("architecture-review")
            architecture_review.write_text(
                _node_phase_content("architecture-review", run_dir=run_dir),
                encoding="utf-8",
            )
            # 같은 gate를 통과로 돌린다. 결과 파일을 손으로 고치는 것이 아니라 gate가
            # 보는 상태를 고친다 — runner가 돌리는 판정을 밖에서 바꿀 방법은 그것뿐이다.
            (plan.path / "gate-must-fail").unlink()
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("current_phase: commit", result.stdout)

    @mock.patch.dict(
        os.environ,
        {"AGENT_FLOW_ADAPTER": "generic", "AGENT_FLOW_GENERIC_MODE": "emit"},
        clear=False,
    )
    def test_node_multi_review_request_changes_routes_to_fix_loop(self) -> None:
        """multi-review request-changes → fix-loop → review 순환 테스트."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            _init_git_repo(project_root)
            # run은 leader가 아니라 managed worktree 안에서 돈다. 이름/경로는
            # 프로덕션 planner에서 유도한다 — 문자열로 박으면 slug 규칙이 바뀔 때 깨진다.
            plan = plan_worktree(root=project_root, name="demo")
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = _node_phase_run_dir(project_root, worktree=plan.name)
            for phase in [
                "domain-grill", "product-brief", "prd",
                "slice-plan", "plan-review", "ddd-design", "worktree",
                "run-start", "red", "green", "refactor", "comment-authoring",
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                content = "verdict: approve\n" if phase == "plan-review" else _node_phase_content(phase, run_dir=run_dir)
                artifact.write_text(content, encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=plan.path, check=False).returncode, 0)
            _record_node_test_evidence(run_dir, exit_code=0)

            state = _read_node_phase(run_dir)
            self.assertEqual(state["current_phase"], "multi-review")

            mr_artifact = run_dir / _node_phase_artifact("multi-review")
            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: request-changes\n\n"
                "## Overall\n"
                "verdict: request-changes\n",
            ),
                encoding="utf-8",
            )
            _write_node_review_results(
                run_dir,
                "multi-review",
                ("approve", "request-changes"),
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("current_phase: fix-loop", result.stdout)

            fix_loop_artifact = run_dir / _node_phase_artifact("fix-loop")
            fix_loop_artifact.write_text(
                _node_phase_content("fix-loop", run_dir=run_dir),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("current_phase: comment-authoring", result.stdout)

            comment_artifact = run_dir / _node_phase_artifact("comment-authoring")
            comment_artifact.write_text(_node_phase_content("comment-authoring"), encoding="utf-8")
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("current_phase: multi-review", result.stdout)

            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n"
                "## Overall\n"
                "verdict: approve\n",
            ) + "dependency-rule: fail\n",
                encoding="utf-8",
            )
            _write_node_review_results(run_dir, "multi-review")
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("current_phase: fix-loop", result.stdout)

            fix_loop_artifact.write_text(
                _node_phase_content("fix-loop", run_dir=run_dir),
                encoding="utf-8",
            )
            self.assertEqual(
                subprocess.run((node, cli, "run", "advance"), cwd=plan.path, check=False).returncode,
                0,
            )
            comment_artifact.write_text(_node_phase_content("comment-authoring"), encoding="utf-8")
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("current_phase: multi-review", result.stdout)

            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n"
                "## Overall\n"
                "verdict: approve\n",
            ),
                encoding="utf-8",
            )
            _write_node_review_results(run_dir, "multi-review")
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("current_phase: architecture-review", result.stdout)

    def test_node_default_final_review_uses_runner_owned_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            _init_git_repo(project_root)
            # run은 leader가 아니라 managed worktree 안에서 돈다. 이름/경로는
            # 프로덕션 planner에서 유도한다 — 문자열로 박으면 slug 규칙이 바뀔 때 깨진다.
            plan = plan_worktree(root=project_root, name="demo")
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--workflow", "default", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = _node_phase_run_dir(project_root, worktree=plan.name)
            _capture_node_spec_source(run_dir, _node_spec_gate(run_dir))
            final_artifact = _set_node_phase(
                run_dir, "final-review", workflow="default"
            )
            _record_node_test_evidence(run_dir, exit_code=0)
            final_artifact.parent.mkdir(parents=True, exist_ok=True)
            final_artifact.write_text(_with_final_review_gate("verdict: approve\n"), encoding="utf-8")
            _write_node_review_results(
                run_dir,
                "final-review",
                ("approve", "request-changes"),
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("current_phase: fix-loop", result.stdout)

    @mock.patch.dict(
        os.environ,
        {"AGENT_FLOW_ADAPTER": "generic", "AGENT_FLOW_GENERIC_MODE": "emit"},
        clear=False,
    )
    def test_node_fix_loop_round_cap_blocks_after_max(self) -> None:
        """fix-loop 3회 초과 시 에러로 차단."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            _init_git_repo(project_root)
            _declare_conditional_gate(project_root)
            # run은 leader가 아니라 managed worktree 안에서 돈다. 이름/경로는
            # 프로덕션 planner에서 유도한다 — 문자열로 박으면 slug 규칙이 바뀔 때 깨진다.
            plan = plan_worktree(root=project_root, name="demo")
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = _node_phase_run_dir(project_root, worktree=plan.name)
            # gate를 계속 실패로 둔다. 라운드마다 architecture-review를 떠날 때
            # runner가 gates를 돌리고 실패해서 fix-loop로 되돌아온다.
            (plan.path / "gate-must-fail").write_text("", encoding="utf-8")
            for phase in [
                "domain-grill", "product-brief", "prd",
                "slice-plan", "plan-review", "ddd-design", "worktree",
                "run-start", "red", "green", "refactor",
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                content = "verdict: approve\n" if phase == "plan-review" else _node_phase_content(phase, run_dir=run_dir)
                artifact.write_text(content, encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=plan.path, check=False).returncode, 0)

            for round_num in range(3):
                for phase in ("comment-authoring", "multi-review", "architecture-review"):
                    artifact = run_dir / _node_phase_artifact(phase)
                    artifact.parent.mkdir(parents=True, exist_ok=True)
                    artifact.write_text(_node_phase_content(phase, run_dir=run_dir), encoding="utf-8")
                    self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=plan.path, check=False).returncode, 0)
                fix_artifact = run_dir / _node_phase_artifact("fix-loop")
                fix_artifact.write_text(
                    _node_phase_content("fix-loop", run_dir=run_dir),
                    encoding="utf-8",
                )
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=plan.path, check=False).returncode, 0)

            # A fourth request-changes verdict (gates -> fix-loop) must halt for the
            # user; the pr-watch event loop is deliberately never capped.
            for phase in ("comment-authoring", "multi-review"):
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.write_text(_node_phase_content(phase, run_dir=run_dir), encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=plan.path, check=False).returncode, 0)
            arch_artifact = run_dir / _node_phase_artifact("architecture-review")
            arch_artifact.write_text(
                _node_phase_content("architecture-review", run_dir=run_dir), encoding="utf-8"
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("fix-loop exceeded", result.stdout)
            current_state = _read_node_phase(run_dir)
            self.assertEqual(current_state["fix_loop_rounds"]["fix-loop"], 3)

    @mock.patch.dict(
        os.environ,
        {"AGENT_FLOW_ADAPTER": "generic", "AGENT_FLOW_GENERIC_MODE": "emit"},
        clear=False,
    )
    def test_node_architecture_review_request_changes_routes_to_refactor(self) -> None:
        """architecture-review request-changes verdict → refactor 라우팅."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            _init_git_repo(project_root)
            # run은 leader가 아니라 managed worktree 안에서 돈다. 이름/경로는
            # 프로덕션 planner에서 유도한다 — 문자열로 박으면 slug 규칙이 바뀔 때 깨진다.
            plan = plan_worktree(root=project_root, name="demo")
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = _node_phase_run_dir(project_root, worktree=plan.name)
            for phase in [
                "domain-grill", "product-brief", "prd",
                "slice-plan", "plan-review", "ddd-design", "worktree",
                "run-start", "red", "green", "refactor", "comment-authoring", "multi-review",
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                content = "verdict: approve\n" if phase == "plan-review" else _node_phase_content(phase, run_dir=run_dir)
                artifact.write_text(content, encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=plan.path, check=False).returncode, 0)

            arch_artifact = run_dir / _node_phase_artifact("architecture-review")
            arch_artifact.write_text(
                _node_phase_content("architecture-review").replace("verdict: approve", "verdict: request-changes"),
                encoding="utf-8",
            )
            _write_node_review_results(
                run_dir,
                "architecture-review",
                ("approve", "request-changes"),
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("current_phase: refactor", result.stdout)

    @mock.patch.dict(
        os.environ,
        {"AGENT_FLOW_ADAPTER": "generic", "AGENT_FLOW_GENERIC_MODE": "emit"},
        clear=False,
    )
    def test_node_multi_review_single_request_changes_routes_to_fix_loop(self) -> None:
        """sub-agent reviewer 1명의 request-changes도 fix-loop로 라우팅한다."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            _init_git_repo(project_root)
            # run은 leader가 아니라 managed worktree 안에서 돈다. 이름/경로는
            # 프로덕션 planner에서 유도한다 — 문자열로 박으면 slug 규칙이 바뀔 때 깨진다.
            plan = plan_worktree(root=project_root, name="demo")
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = _node_phase_run_dir(project_root, worktree=plan.name)
            for phase in [
                "domain-grill", "product-brief", "prd",
                "slice-plan", "plan-review", "ddd-design", "worktree",
                "run-start", "red", "green", "refactor", "comment-authoring",
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                content = "verdict: approve\n" if phase == "plan-review" else _node_phase_content(phase, run_dir=run_dir)
                artifact.write_text(content, encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=plan.path, check=False).returncode, 0)

            mr_artifact = run_dir / _node_phase_artifact("multi-review")
            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: request-changes\n\n"
                "## Overall\nverdict: request-changes\n",
            ),
                encoding="utf-8",
            )
            _write_node_review_results(
                run_dir,
                "multi-review",
                ("request-changes",),
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("current_phase: fix-loop", result.stdout)

    def test_node_push_watch_blocks_protected_branches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            subprocess.run(("git", "init", "-q"), cwd=project_root, check=True)
            subprocess.run(("git", "checkout", "-q", "-b", "main"), cwd=project_root, check=True)

            result = subprocess.run(
                (node, cli, "run", "push-watch"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("blocked: protected branch main", result.stderr)

    def test_node_push_watch_blocks_detached_head(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            _init_git_repo(project_root)
            subprocess.run(("git", "checkout", "-q", "--detach", "HEAD"), cwd=project_root, check=True)

            result = subprocess.run(
                (node, cli, "run", "push-watch"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("blocked: push-watch requires a named git branch", result.stderr)

    def test_node_push_watch_tick_records_failed_ci_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            run_dir, checkout = _node_start_full_feature_at_pr_watch(project_root, node, cli)
            bin_dir = Path(temp_dir) / "bin"
            bin_dir.mkdir()
            gh = bin_dir / "gh"
            gh.write_text(
                "\n".join(
                    [
                        "#!/bin/sh",
                        "cat <<'JSON'",
                        '{"url":"https://github.com/acme/demo/pull/7","reviewDecision":"REVIEW_REQUIRED","statusCheckRollup":[{"name":"test","status":"COMPLETED","conclusion":"FAILURE"}]}',
                        "JSON",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            gh.chmod(0o755)

            result = subprocess.run(
                (node, cli, "run", "push-watch-tick"),
                cwd=checkout,
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            watch = run_dir / _node_phase_artifact("pr-watch")
            watch_text = watch.read_text(encoding="utf-8")
            self.assertIn("status: ci-failed", watch_text)
            self.assertIn("https://github.com/acme/demo/pull/7", watch_text)

    def test_push_watch_tick_replays_incomplete_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(
                subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode,
                0,
            )
            run_dir, checkout = _node_start_full_feature_at_pr_watch(project_root, node, cli)
            bin_dir = Path(temp_dir) / "bin"
            bin_dir.mkdir()
            gh = bin_dir / "gh"
            gh.write_text(
                "#!/bin/sh\n"
                "cat <<'JSON'\n"
                '{"url":"https://github.com/acme/demo/pull/7",'
                '"reviewDecision":"REVIEW_REQUIRED",'
                '"statusCheckRollup":[{"name":"test","status":"COMPLETED",'
                '"conclusion":"FAILURE"}]}\n'
                "JSON\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

            first = subprocess.run(
                (node, cli, "run", "push-watch-tick"),
                cwd=checkout,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            state_path = project_root / ".agent-flow" / "state" / "push-watch.json"
            artifact = run_dir / _node_phase_artifact("pr-watch")
            captured_state = json.loads(state_path.read_text(encoding="utf-8"))
            captured_artifact = artifact.read_text(encoding="utf-8")
            self.assertIn("observation_id", captured_state)
            self.assertEqual(captured_state["iterations"], 1)

            intent = project_root / ".agent-flow" / "state" / "push-watch-intent.json"
            intent.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "run_id": captured_state["run_id"],
                        "run_dir": captured_state["run_dir"],
                        "observation_id": captured_state["observation_id"],
                        "artifact": captured_artifact,
                        "state": captured_state,
                    }
                ),
                encoding="utf-8",
            )
            state_path.write_text(
                json.dumps({"status": "watching", "iterations": 0}),
                encoding="utf-8",
            )
            artifact.write_text("status: stale\n", encoding="utf-8")

            replay = subprocess.run(
                (node, cli, "run", "push-watch-tick"),
                cwd=checkout,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(replay.returncode, 0, replay.stderr)
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8")),
                captured_state,
            )
            self.assertEqual(artifact.read_text(encoding="utf-8"), captured_artifact)
            self.assertFalse(intent.exists())

    def test_push_watch_tick_rejects_intent_from_another_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(
                subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode,
                0,
            )
            run_dir, checkout = _node_start_full_feature_at_pr_watch(project_root, node, cli)
            bin_dir = Path(temp_dir) / "bin"
            bin_dir.mkdir()
            gh = bin_dir / "gh"
            gh.write_text(
                "#!/bin/sh\n"
                "cat <<'JSON'\n"
                '{"url":"https://github.com/acme/demo/pull/7",'
                '"reviewDecision":"REVIEW_REQUIRED","statusCheckRollup":[]}\n'
                "JSON\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            artifact = run_dir / _node_phase_artifact("pr-watch")
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("status: current\n", encoding="utf-8")
            former_run_dir = Path(temp_dir) / "foreign" / "former-run"
            former_run_dir.mkdir(parents=True)
            (former_run_dir / "active").touch()
            intent = project_root / ".agent-flow" / "state" / "push-watch-intent.json"
            intent.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "run_id": "former-run",
                        "run_dir": str(former_run_dir),
                        "observation_id": "former-observation",
                        "artifact": "status: former\n",
                        "state": {
                            "run_id": "former-run",
                            "run_dir": str(former_run_dir),
                            "observation_id": "former-observation",
                        },
                    }
                ),
                encoding="utf-8",
            )

            replay = subprocess.run(
                (node, cli, "run", "push-watch-tick"),
                cwd=checkout,
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
            )

            self.assertEqual(replay.returncode, 1)
            self.assertIn("push-watch intent belongs to another active run", replay.stderr)
            self.assertEqual(artifact.read_text(encoding="utf-8"), "status: current\n")
            self.assertTrue(intent.is_file())

    def test_push_watch_tick_retires_intent_from_finished_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(
                subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode,
                0,
            )
            run_dir, checkout = _node_start_full_feature_at_pr_watch(project_root, node, cli)
            bin_dir = Path(temp_dir) / "bin"
            bin_dir.mkdir()
            gh = bin_dir / "gh"
            gh.write_text(
                "#!/bin/sh\n"
                "cat <<'JSON'\n"
                '{"url":"https://github.com/acme/demo/pull/7",'
                '"reviewDecision":"REVIEW_REQUIRED","statusCheckRollup":[]}\n'
                "JSON\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            finished_run_dir = run_dir.parent / "finished-run"
            finished_run_dir.mkdir()
            intent = project_root / ".agent-flow" / "state" / "push-watch-intent.json"
            intent.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "run_id": "finished-run",
                        "run_dir": str(finished_run_dir),
                        "observation_id": "finished-observation",
                        "artifact": "status: former\n",
                        "state": {
                            "run_id": "finished-run",
                            "run_dir": str(finished_run_dir),
                            "observation_id": "finished-observation",
                        },
                    }
                ),
                encoding="utf-8",
            )

            replay = subprocess.run(
                (node, cli, "run", "push-watch-tick"),
                cwd=checkout,
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
            )

            self.assertEqual(replay.returncode, 0, replay.stderr)
            self.assertFalse(intent.exists())
            self.assertNotEqual(
                (run_dir / _node_phase_artifact("pr-watch")).read_text(encoding="utf-8"),
                "status: former\n",
            )

    def test_push_watch_tick_excludes_concurrent_replayer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(
                subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode,
                0,
            )
            run_dir, checkout = _node_start_full_feature_at_pr_watch(project_root, node, cli)
            bin_dir = Path(temp_dir) / "bin"
            bin_dir.mkdir()
            ready = Path(temp_dir) / "gh-ready"
            gh = bin_dir / "gh"
            gh.write_text(
                "#!/bin/sh\n"
                'touch \"$PUSH_WATCH_TEST_READY\"\n'
                "sleep 1\n"
                "cat <<'JSON'\n"
                '{"url":"https://github.com/acme/demo/pull/7",'
                '"reviewDecision":"REVIEW_REQUIRED",'
                '"statusCheckRollup":[{"name":"test","status":"COMPLETED",'
                '"conclusion":"FAILURE"}]}\n'
                "JSON\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "PUSH_WATCH_TEST_READY": str(ready),
            }
            first = subprocess.Popen(
                (node, cli, "run", "push-watch-tick"),
                cwd=checkout,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists())

            second = subprocess.run(
                (node, cli, "run", "push-watch-tick"),
                cwd=checkout,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            first_stdout, first_stderr = first.communicate(timeout=10)

            self.assertEqual(first.returncode, 0, first_stdout + first_stderr)
            self.assertEqual(second.returncode, 1)
            self.assertIn("push-watch tick is already active", second.stderr)
            state = json.loads(
                (
                    project_root / ".agent-flow" / "state" / "push-watch.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(state["iterations"], 1)
            self.assertTrue((run_dir / _node_phase_artifact("pr-watch")).is_file())

    def test_push_watch_stale_lock_reclaim_preserves_generation_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(
                subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode,
                0,
            )
            _, checkout = _node_start_full_feature_at_pr_watch(project_root, node, cli)
            bin_dir = Path(temp_dir) / "bin"
            bin_dir.mkdir()
            gh = bin_dir / "gh"
            gh.write_text(
                "#!/bin/sh\n"
                "cat <<'JSON'\n"
                '{"url":"https://github.com/acme/demo/pull/7",'
                '"reviewDecision":"REVIEW_REQUIRED","statusCheckRollup":[]}\n'
                "JSON\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            token = "a" * 32
            lock = project_root / ".agent-flow" / "state" / "push-watch.lock"
            lock.mkdir(parents=True)
            (lock / "owner.json").write_text(
                json.dumps({"pid": 999_999_999, "token": token}),
                encoding="utf-8",
            )

            result = subprocess.run(
                (node, cli, "run", "push-watch-tick"),
                cwd=checkout,
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            retired = lock.with_name(f"{lock.name}.retired-{token}")
            self.assertTrue((retired / "owner.json").is_file())

    def test_node_push_watch_tick_blocks_before_pr_watch_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            _init_git_repo(project_root)
            checkout = plan_worktree(root=project_root, name="demo").path
            subprocess.run(
                (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                cwd=project_root,
                check=True,
            )

            result = subprocess.run(
                (node, cli, "run", "push-watch-tick"),
                cwd=checkout,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("blocked: push-watch-tick requires current phase pr-watch", result.stderr)

    def test_node_push_watch_tick_treats_legacy_failed_context_as_ci_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            run_dir, checkout = _node_start_full_feature_at_pr_watch(project_root, node, cli)
            bin_dir = Path(temp_dir) / "bin"
            bin_dir.mkdir()
            gh = bin_dir / "gh"
            gh.write_text(
                "\n".join(
                    [
                        "#!/bin/sh",
                        "cat <<'JSON'",
                        '{"url":"https://github.com/acme/demo/pull/8","reviewDecision":"APPROVED","statusCheckRollup":[{"context":"lint","state":"FAILURE"}]}',
                        "JSON",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            gh.chmod(0o755)

            result = subprocess.run(
                (node, cli, "run", "push-watch-tick"),
                cwd=checkout,
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("status: ci-failed", (run_dir / _node_phase_artifact("pr-watch")).read_text(encoding="utf-8"))

    def test_node_push_watch_tick_requires_review_approval_before_green(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            run_dir, checkout = _node_start_full_feature_at_pr_watch(project_root, node, cli)
            bin_dir = Path(temp_dir) / "bin"
            bin_dir.mkdir()
            gh = bin_dir / "gh"
            gh.write_text(
                "\n".join(
                    [
                        "#!/bin/sh",
                        "cat <<'JSON'",
                        '{"url":"https://github.com/acme/demo/pull/9","statusCheckRollup":[{"name":"test","status":"COMPLETED","conclusion":"SUCCESS"}]}',
                        "JSON",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            gh.chmod(0o755)

            result = subprocess.run(
                (node, cli, "run", "push-watch-tick"),
                cwd=checkout,
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("status: pending", (run_dir / _node_phase_artifact("pr-watch")).read_text(encoding="utf-8"))

    def test_node_push_watch_tick_treats_legacy_success_context_as_green_when_approved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            run_dir, checkout = _node_start_full_feature_at_pr_watch(project_root, node, cli)
            bin_dir = Path(temp_dir) / "bin"
            bin_dir.mkdir()
            gh = bin_dir / "gh"
            gh.write_text(
                "\n".join(
                    [
                        "#!/bin/sh",
                        "cat <<'JSON'",
                        '{"url":"https://github.com/acme/demo/pull/10","reviewDecision":"APPROVED","statusCheckRollup":[{"context":"lint","state":"SUCCESS"}]}',
                        "JSON",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            gh.chmod(0o755)

            result = subprocess.run(
                (node, cli, "run", "push-watch-tick"),
                cwd=checkout,
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("status: green", (run_dir / _node_phase_artifact("pr-watch")).read_text(encoding="utf-8"))

    def test_start_can_create_and_record_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "start",
                            "development",
                            "--root",
                            str(root),
                            "--task",
                            "demo",
                            "--run-id",
                            "r1",
                            "--worktree",
                            "Slice A",
                        ]
                    ),
                    0,
                )
            worktree = managed_worktrees_root(root) / "feat-slice-a"
            run_dir = (
                worktree_runtime_root(root=root, name="feat-slice-a")
                / ".agent-flow"
                / "runs"
                / "r1"
            )
            meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["checkout_identity"], "worktree:feat-slice-a")
            status = get_worktree_status(root=root, name="feat-slice-a")
            self.assertEqual(status.branch, "feat/slice-a")
            self.assertEqual(status.path.resolve(), worktree.resolve())
            self.assertTrue(worktree.is_dir())

    def test_start_worktree_rejects_dirty_leader_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            (root / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            self.assertEqual(
                main(
                    [
                        "start",
                        "development",
                        "--root",
                        str(root),
                        "--task",
                        "demo",
                        "--worktree",
                        "slice-a",
                    ]
                ),
                2,
            )
            self.assertFalse((root / ".agent-flow" / "runs").exists())

    def test_start_worktree_run_id_is_scoped_to_worktree_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            self.assertEqual(
                main(
                    [
                        "start",
                        "development",
                        "--root",
                        str(root),
                        "--task",
                        "demo",
                        "--run-id",
                        "r1",
                    ]
                ),
                0,
            )

            self.assertEqual(
                main(
                    [
                        "start",
                        "development",
                        "--root",
                        str(root),
                        "--task",
                        "demo",
                        "--run-id",
                        "r1",
                        "--worktree",
                        "slice-a",
                    ]
                ),
                0,
            )
            self.assertTrue(
                (
                    worktree_runtime_root(root=root, name="feat-demo")
                    / ".agent-flow"
                    / "runs"
                    / "r1"
                ).exists()
            )
            self.assertTrue(
                (
                    worktree_runtime_root(root=root, name="feat-slice-a")
                    / ".agent-flow"
                    / "runs"
                    / "r1"
                ).exists()
            )

    def test_start_worktree_write_failure_cleans_run_and_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)

            with mock.patch(
                "agent_flow.artifact.write_meta",
                side_effect=OSError("meta failed"),
            ):
                self.assertEqual(
                    main(
                        [
                            "start",
                            "development",
                            "--root",
                            str(root),
                            "--task",
                            "demo",
                            "--run-id",
                            "r1",
                            "--worktree",
                            "slice-a",
                        ]
                    ),
                    2,
                )
            self.assertFalse(
                (
                    worktree_runtime_root(root=root, name="feat-slice-a")
                    / ".agent-flow"
                    / "runs"
                    / "r1"
                ).exists()
            )
            self.assertFalse((managed_worktrees_root(root) / "feat-slice-a").exists())

    def test_worktree_create_manifest_write_failure_cleans_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)

            with mock.patch("agent_flow.core.worktrees.write_worktree_manifest", side_effect=OSError("manifest failed")):
                self.assertEqual(
                    main(["worktree", "create", "--root", str(root), "--name", "slice-a"]),
                    2,
                )
            self.assertFalse((managed_worktrees_root(root) / "feat-slice-a").exists())

    def test_start_reuses_existing_worktree_manifest_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            self.assertEqual(
                main(
                    [
                        "worktree",
                        "create",
                        "--root",
                        str(root),
                        "--name",
                        "slice-a",
                        "--branch",
                        "feat/slice-a",
                    ]
                ),
                0,
            )

            self.assertEqual(
                main(
                    [
                        "start",
                        "development",
                        "--root",
                        str(root),
                        "--task",
                        "demo",
                        "--run-id",
                        "r1",
                        "--worktree",
                        "slice-a",
                    ]
                ),
                0,
            )
            self.assertTrue(
                (
                    worktree_runtime_root(root=root, name="feat-slice-a")
                    / ".agent-flow"
                    / "runs"
                    / "r1"
                    / "meta.json"
                ).is_file()
            )
            self.assertEqual(
                get_worktree_status(root=root, name="feat-slice-a").branch,
                "feat/slice-a",
            )

    def test_start_worktree_can_use_existing_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            subprocess.run(("git", "branch", "feat/slice-a"), cwd=root, check=True)

            self.assertEqual(
                main(
                    [
                        "start",
                        "development",
                        "--root",
                        str(root),
                        "--task",
                        "demo",
                        "--run-id",
                        "r1",
                        "--worktree",
                        "slice-a",
                        "--worktree-branch",
                        "feat/slice-a",
                    ]
                ),
                0,
            )
            self.assertTrue(
                (
                    worktree_runtime_root(root=root, name="feat-slice-a")
                    / ".agent-flow"
                    / "runs"
                    / "r1"
                    / "meta.json"
                ).is_file()
            )
            self.assertEqual(
                get_worktree_status(root=root, name="feat-slice-a").branch,
                "feat/slice-a",
            )
            self.assertTrue((managed_worktrees_root(root) / "feat-slice-a").is_dir())

    def test_detect_profile_defaults_to_generic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["detect-profile", "--root", temp_dir]), 0)
            self.assertEqual(output.getvalue().strip(), "generic")

    def test_detect_profile_reports_android_for_gradle_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "settings.gradle.kts").write_text("", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["detect-profile", "--root", temp_dir]), 0)
            self.assertEqual(output.getvalue().strip(), "android")

    def test_detect_profile_reports_android_for_groovy_gradle_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "settings.gradle").write_text("", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["detect-profile", "--root", temp_dir]), 0)
            self.assertEqual(output.getvalue().strip(), "android")

    def test_detect_profile_prefers_react_native_over_gradle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "package.json").write_text('{"dependencies":{"react-native":"latest"}}\n', encoding="utf-8")
            (root / "settings.gradle.kts").write_text("", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["detect-profile", "--root", temp_dir]), 0)
            self.assertEqual(output.getvalue().strip(), "react-native")

    def test_detect_profile_reports_flutter_for_sdk_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "pubspec.yaml").write_text(
                "name: app\ndependencies:\n  flutter:\n    sdk: flutter\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["detect-profile", "--root", temp_dir]), 0)
            self.assertEqual(output.getvalue().strip(), "flutter")

    def test_detect_profile_reads_the_sdk_value_not_its_bytes(self) -> None:
        """인용과 인라인 주석은 저자의 선택이다. 바이트로 비교하면 실제 Flutter
        저장소가 조용히 profile을 잃는다."""
        for dependency in (
            '    sdk: "flutter"',
            "    sdk: 'flutter'  # Flutter SDK",
            "    sdk: flutter # Flutter SDK",
            "    sdk:  flutter",
        ):
            with self.subTest(dependency=dependency):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    (root / "pubspec.yaml").write_text(
                        f"name: app\ndependencies:\n  flutter:\n{dependency}\n",
                        encoding="utf-8",
                    )
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(main(["detect-profile", "--root", temp_dir]), 0)
                    self.assertEqual(output.getvalue().strip(), "flutter")

    def test_detect_profile_keeps_a_pure_dart_package_off_the_flutter_profile(self) -> None:
        """`pubspec.yaml`은 Dart 패키지 manifest다. SDK 의존이 없는 저장소를
        flutter로 잡으면 `flutter analyze`·`flutter test`가 상시 실패한다."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "pubspec.yaml").write_text(
                "name: cli\ndescription: helper for flutter devs\ndependencies:\n  args: ^2.5.0\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["detect-profile", "--root", temp_dir]), 0)
            self.assertEqual(output.getvalue().strip(), "generic")

    def test_detect_profile_prefers_flutter_over_a_root_gradle_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "pubspec.yaml").write_text(
                "name: app\ndependencies:\n  flutter:\n    sdk: flutter\n",
                encoding="utf-8",
            )
            (root / "settings.gradle.kts").write_text("", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["detect-profile", "--root", temp_dir]), 0)
            self.assertEqual(output.getvalue().strip(), "flutter")

    def test_provider_list_reports_host_provider_availability(self) -> None:
        output = io.StringIO()
        with mock.patch("agent_flow.providers.host.shutil.which") as which:
            which.side_effect = lambda name: f"/usr/local/bin/{name}" if name == "codex" else None
            with mock.patch.dict("agent_flow.providers.host.os.environ", {}, clear=True):
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["provider", "list"]), 0)
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "manual available command=manual",
                "codex-session available command=/usr/local/bin/codex",
                "claude-session unavailable command=claude",
                "omp-session unavailable command=omp",
            ],
        )

    def test_host_env_hint_wins_before_omp_path_fallback(self) -> None:
        from agent_flow.cli_detect import detect_host_cli

        with mock.patch("agent_flow.cli_detect.shutil.which") as which:
            which.side_effect = lambda name: f"/usr/local/bin/{name}" if name == "omp" else None
            with mock.patch.dict("agent_flow.cli_detect.os.environ", {"CODEX_HOME": "/tmp/codex"}, clear=True):
                self.assertEqual(detect_host_cli(), "codex")

        with mock.patch("agent_flow.cli_detect.shutil.which") as which:
            which.side_effect = lambda name: f"/usr/local/bin/{name}" if name == "omp" else None
            with mock.patch.dict("agent_flow.cli_detect.os.environ", {"CLAUDECODE": "1"}, clear=True):
                self.assertEqual(detect_host_cli(), "claude")

    def test_codex_path_wins_before_omp_path_fallback(self) -> None:
        from agent_flow.cli_detect import detect_host_cli

        def both_codex_and_omp(name: str) -> str | None:
            return f"/usr/local/bin/{name}" if name in {"codex", "omp"} else None

        with mock.patch("agent_flow.cli_detect.shutil.which", side_effect=both_codex_and_omp):
            with mock.patch.dict("agent_flow.cli_detect.os.environ", {}, clear=True):
                self.assertEqual(detect_host_cli(), "codex")

    def test_storage_dir_env_does_not_select_omp_host(self) -> None:
        from agent_flow.cli_detect import detect_host_cli

        with mock.patch("agent_flow.cli_detect.shutil.which", return_value=None):
            with mock.patch.dict(
                "agent_flow.cli_detect.os.environ",
                {"PI_CODING_AGENT_DIR": "/tmp/omp"},
                clear=True,
            ):
                self.assertIsNone(detect_host_cli())
            with mock.patch.dict(
                "agent_flow.cli_detect.os.environ",
                {"PI_CODING_AGENT_DIR": "/tmp/omp", "CODEX_HOME": "/tmp/codex"},
                clear=True,
            ):
                self.assertEqual(detect_host_cli(), "codex")

    def test_provider_list_treats_host_environment_as_available(self) -> None:
        output = io.StringIO()
        with mock.patch("agent_flow.providers.host.shutil.which", return_value=None):
            with mock.patch.dict("agent_flow.providers.host.os.environ", {"CLAUDECODE": "1"}, clear=True):
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["provider", "list"]), 0)
        self.assertIn("claude-session available command=claude", output.getvalue())
        output = io.StringIO()
        with mock.patch("agent_flow.providers.host.shutil.which", return_value=None):
            with mock.patch.dict("agent_flow.providers.host.os.environ", {"OMP_PROFILE": "default"}, clear=True):
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["provider", "list"]), 0)
        self.assertIn("omp-session available command=omp", output.getvalue())
        output = io.StringIO()
        with mock.patch("agent_flow.providers.host.shutil.which", return_value=None):
            with mock.patch.dict("agent_flow.providers.host.os.environ", {"PI_CODING_AGENT_DIR": "/tmp/omp"}, clear=True):
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["provider", "list"]), 0)
        self.assertIn("omp-session unavailable command=omp", output.getvalue())

    def test_review_retry_reports_structured_blocker(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["review", "retry", "--reviewer", "claude"]), 0)
        lines = output.getvalue().strip().splitlines()
        self.assertIn("status: awaiting_retry", lines)
        self.assertIn("reason: reviewer_retry_ready", lines)
        self.assertIn("reviewer: claude", lines)
        self.assertIn("required_action: rerun_review_now", lines)
        self.assertIn("next_command: none", lines)

    def test_review_retry_blocks_until_retry_after(self) -> None:
        retry_after = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(["review", "retry", "--reviewer", "claude", "--retry-after", retry_after]),
                0,
            )
        lines = output.getvalue().strip().splitlines()
        self.assertIn("status: blocked", lines)
        self.assertIn("reason: reviewer_rate_limited", lines)
        self.assertIn(f"retry_after: {retry_after}", lines)
        self.assertIn("required_action: wait_until_retry_after", lines)
        self.assertIn(
            f"next_command: agent-flow review retry --reviewer claude --retry-after {retry_after}",
            lines,
        )

    def test_review_retry_rejects_malformed_retry_after(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(
                main(["review", "retry", "--reviewer", "claude", "--retry-after", "not-a-date"]),
                2,
            )
        self.assertIn("--retry-after must be ISO-8601", stderr.getvalue())

    def test_node_status_escapes_task_newlines_and_emits_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            task = "demo\nstatus: complete\nreason: injected"
            _init_git_repo(project_root)
            # run은 leader가 아니라 managed worktree 안에서 돈다. 이름/경로는
            # 프로덕션 planner에서 유도한다 — 문자열로 박으면 slug 규칙이 바뀔 때 깨진다.
            plan = plan_worktree(root=project_root, name=task)
            start = subprocess.run(
                (node, cli, "run", "start", "--task", task, "--run-id", "r1"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(start.returncode, 0, start.stderr)
            status = subprocess.run(
                (node, cli, "run", "status"),
                cwd=plan.path,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            lines = status.stdout.strip().splitlines()
            self.assertIn(r"task: demo\nstatus: complete\nreason: injected", lines)
            self.assertNotIn("reason: injected", lines)
            status_json = next(line for line in lines if line.startswith("status_json: "))
            payload = json.loads(status_json.removeprefix("status_json: "))
            self.assertEqual(payload["task"], task)

    def test_status_reports_missing_completion_markers_for_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            plan = plan_worktree(root=root, name="demo task")
            state_root = worktree_runtime_root(root=root, name=plan.name)
            with mock.patch.dict(
                os.environ,
                {"AGENT_FLOW_ADAPTER": "generic", "AGENT_FLOW_GENERIC_MODE": "emit"},
                clear=False,
            ):
                self.assertEqual(
                    main(["run", "demo task", "--root", str(root), "--workflow", "full-feature"]),
                    0,
                )
            run_dir = next(
                path
                for path in (state_root / ".agent-flow" / "runs").iterdir()
                if path.is_dir()
            )
            (run_dir / "domain-grill.md").write_text(
                "## Completion Gate\n"
                "TODO: domain-grill: complete\n"
                "shared_understanding: reached\n",
                encoding="utf-8",
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["status", "--root", str(root), "--worktree", plan.name]), 0
                )
            lines = output.getvalue().strip().splitlines()
            self.assertIn("status: blocked", lines)
            self.assertIn("reason: missing_completion_markers", lines)
            self.assertTrue(any(line.startswith("status_json: ") for line in lines))
            status_json = next(
                line for line in lines if line.startswith("status_json: ")
            )
            payload = json.loads(status_json.removeprefix("status_json: "))
            self.assertIn(
                "domain-grill: complete",
                payload["missing_completion_markers"],
            )
            self.assertTrue(
                any(line.startswith("missing_completion_markers: ") for line in lines)
            )

    def test_status_reports_only_later_spec_delta_and_agent_confirm_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / ".agent-flow" / "runs" / "r1"
            _set_node_phase(run_dir, "implement", workflow="default")
            initial = (
                "## Spec Items\n"
                "SPEC-1: Keep the confirmed behavior.\n"
                "verify: symbol:Behavior=confirmed\n"
                "## Design Values\n"
                "## Completion Gate\n"
                "spec-items: SPEC-1\n"
                "design-values: none\n"
            )
            (run_dir / "design.md").write_text(initial, encoding="utf-8")
            capture_design_ledger(run_dir, "design", initial)
            changed = initial.replace(
                "## Design Values",
                "SPEC-2: Add the new behavior.\n"
                "verify: symbol:NewBehavior=enabled\n"
                "## Design Values",
            )
            (run_dir / "design.md").write_text(changed, encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["status", "--root", str(root)]), 0)
            status = output.getvalue()
            delta = status.partition("### Added")[2].partition("spec_scope:")[0]
            self.assertIn("spec_changes: awaiting_confirmation", status)
            self.assertIn("SPEC-2: Add the new behavior.", delta)
            self.assertNotIn("SPEC-1", delta)
            self.assertIn(
                "spec_scope: changed items paused; unchanged confirmed items may continue",
                status,
            )
            self.assertIn(
                f"spec_confirm_command: agent-flow spec confirm --run-dir {run_dir.resolve()}",
                status,
            )

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "spec",
                            "confirm",
                            "--run-dir",
                            str(run_dir),
                            "--root",
                            str(root),
                        ]
                    ),
                    0,
                )
            confirmed_output = io.StringIO()
            with contextlib.redirect_stdout(confirmed_output):
                self.assertEqual(main(["status", "--root", str(root)]), 0)
            self.assertNotIn(
                "spec_changes: awaiting_confirmation",
                confirmed_output.getvalue(),
            )

    def test_profile_detection_for_node_and_python(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "package.json").write_text('{"scripts":{"test":"node test.js"}}', encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["detect-profile", "--root", str(root)]), 0)
            self.assertEqual(output.getvalue().strip(), "node")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["detect-profile", "--root", str(root)]), 0)
            self.assertEqual(output.getvalue().strip(), "python")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "package.json").write_text('{"scripts":{"test":"node test.js"}}', encoding="utf-8")
            (root / "tsconfig.json").write_text("{}\n", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["detect-profile", "--root", str(root)]), 0)
            self.assertEqual(output.getvalue().strip(), "typescript")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "tsconfig.json").write_text("{}\n", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["detect-profile", "--root", str(root)]), 0)
            # package.json 없는 tsconfig 단독 프로젝트는 npm gate를 강제하지 않는다.
            self.assertEqual(output.getvalue().strip(), "generic")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "Package.swift").write_text("// swift-tools-version: 5.9\n", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["detect-profile", "--root", str(root)]), 0)
            self.assertEqual(output.getvalue().strip(), "ios")

    def test_load_profile_reads_packaged_gates(self) -> None:
        profile = load_profile("node")
        self.assertEqual(profile.profile_id, "node")
        self.assertEqual(profile.gates[0].gate_id, "architecture-lint")
        self.assertEqual(
            profile.gates[0].command,
            ("agent-flow", "architecture-lint", "--profile", "node"),
        )
        # npm 기반 TypeScript profile은 subprocess argv list로 검증 명령을 보관한다.
        typescript = load_profile("typescript")
        self.assertEqual(typescript.gates[0].gate_id, "architecture-lint")
        self.assertEqual(
            typescript.gates[0].command,
            ("agent-flow", "architecture-lint", "--profile", "typescript"),
        )
        self.assertEqual(typescript.gates[1].gate_id, "typecheck")
        self.assertEqual(typescript.gates[1].command, ("npx", "tsc", "--noEmit"))
        nextjs_gates = {gate.gate_id: gate.command for gate in load_profile("nextjs").gates}
        self.assertEqual(nextjs_gates["architecture-lint"], ("agent-flow", "architecture-lint", "--profile", "nextjs"))
        self.assertEqual(nextjs_gates["build"], ("npm", "run", "build"))
        python_gates = {gate.gate_id: gate for gate in load_profile("python").gates}
        self.assertFalse(python_gates["type"].required)
        self.assertFalse(python_gates["lint"].required)
        self.assertTrue(python_gates["test"].required)
        android = load_profile("android")
        self.assertEqual(android.profile_id, "android")
        android_required = android.skills["required_review"]
        self.assertEqual(android_required[0]["group"], "profile")
        self.assertIn("android-code-review", android_required[0]["skills"])
        # baseline과 architecture는 서로 다른 group이다. 한 group이면 Kotlin 한 줄
        # 변경에도 계층 계약 문서가 required가 된다.
        self.assertEqual(
            [group["group"] for group in android_required], ["profile", "architecture"]
        )
        self.assertNotIn("android_skills", android.skills)
        rn_required = load_profile("react-native").skills["required_review"]
        # `typescript` group은 `typescript-development-guide`의 범위를 스택 glob에서
        # 분리한 자리이고, `architecture`는 계층 계약 문서를 경계 경로로 분리한 자리다.
        # 옛 escalation group이 되살아나면 이 목록이 늘어난다.
        self.assertEqual([group["group"] for group in rn_required], ["profile", "architecture", "typescript"])

    def test_runner_prefers_repository_kit_root(self) -> None:
        from agent_flow.runner import _find_kit_root

        self.assertEqual(_find_kit_root(), Path(__file__).resolve().parents[1])

    def test_pr_watch_cli_prints_snapshot_json_once(self) -> None:
        from agent_flow.pr_watch import PRSnapshot

        output = io.StringIO()
        snapshot = PRSnapshot(number=4, title="demo", state="OPEN", status="green")
        with mock.patch("agent_flow.cli.fetch_pr", return_value=snapshot):
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["pr-watch", "4", "--once", "--allow-unbound"]),
                    0,
                )
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["number"], 4)
        self.assertEqual(payload["status"], "green")

    def test_pr_watch_cli_fails_closed_without_run_context(self) -> None:
        error = io.StringIO()
        with (
            mock.patch(
                "agent_flow.cli._implicit_pr_watch_run_dir",
                return_value=None,
            ),
            mock.patch("agent_flow.cli.fetch_pr") as fetch,
            contextlib.redirect_stderr(error),
        ):
            exit_code = main(["pr-watch", "4", "--once"])

        self.assertEqual(exit_code, 1)
        self.assertIn("pass --run-dir or --allow-unbound", error.getvalue())
        fetch.assert_not_called()

    def test_pr_watch_cli_fails_closed_when_run_resolution_errors(self) -> None:
        error = io.StringIO()
        with (
            mock.patch(
                "agent_flow.cli._implicit_pr_watch_run_dir",
                side_effect=RuntimeError("ambiguous worktree"),
            ),
            mock.patch("agent_flow.cli.fetch_pr") as fetch,
            contextlib.redirect_stderr(error),
        ):
            exit_code = main(["pr-watch", "4", "--once"])

        self.assertEqual(exit_code, 1)
        self.assertIn("cannot resolve workflow run", error.getvalue())
        fetch.assert_not_called()

    def test_pr_watch_cli_explains_missing_gate_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            error = io.StringIO()
            with (
                mock.patch(
                    "agent_flow.cli._implicit_pr_watch_run_dir",
                    side_effect=RuntimeError(
                        "active run has no canonical gate ledger; "
                        "complete the gates phase first"
                    ),
                ),
                mock.patch("agent_flow.cli.fetch_pr") as fetch,
                contextlib.redirect_stderr(error),
            ):
                exit_code = main(["pr-watch", "4", "--once"])

        self.assertEqual(exit_code, 1)
        self.assertIn("complete the gates phase first", error.getvalue())
        fetch.assert_not_called()

    def test_pr_watch_cli_requires_declared_deferred_ci_checks(self) -> None:
        from agent_flow.pr_watch import PRSnapshot

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            gate_results = run_dir / "artifacts" / "gate-results.json"
            gate_results.parent.mkdir(parents=True)
            gate_results.write_text(
                json.dumps(
                    {
                        "deferred_ci_checks": ["pytest"],
                        "produced_by": {
                            "gate_phase": "all",
                            "gate_execution": "local",
                        },
                    }
                ),
                encoding="utf-8",
            )
            snapshot = PRSnapshot(
                number=4,
                title="demo",
                state="OPEN",
                status="pending",
            )
            with (
                mock.patch(
                    "agent_flow.cli._resolve_run_dir",
                    return_value=run_dir,
                ),
                mock.patch(
                    "agent_flow.cli.fetch_pr",
                    return_value=snapshot,
                ) as fetch,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    main(
                        [
                            "pr-watch",
                            "4",
                            "--once",
                            "--run-dir",
                            str(run_dir),
                        ]
                    ),
                    0,
                )

        fetch.assert_called_once_with(
            4,
            repo=None,
            required_checks=("pytest",),
        )

    def test_pr_watch_cli_fails_closed_on_unreadable_gate_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            gate_results = run_dir / "artifacts" / "gate-results.json"
            gate_results.parent.mkdir(parents=True)
            gate_results.write_text("{", encoding="utf-8")
            error = io.StringIO()
            with (
                mock.patch(
                    "agent_flow.cli._resolve_run_dir",
                    return_value=run_dir,
                ),
                mock.patch("agent_flow.cli.fetch_pr") as fetch,
                contextlib.redirect_stderr(error),
            ):
                exit_code = main(
                    [
                        "pr-watch",
                        "4",
                        "--once",
                        "--run-dir",
                        str(run_dir),
                    ]
                )

        self.assertEqual(exit_code, 1)
        self.assertIn("cannot verify deferred CI gates", error.getvalue())
        fetch.assert_not_called()


    def test_run_gate_reports_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_gate(
                GateCommand("ok", (sys.executable, "-c", "print('ok')")),
                cwd=Path(temp_dir),
            )
            self.assertTrue(result.passed)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout.strip(), "ok")


    def test_gates_cli_writes_results_for_run_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / ".agent-flow" / "runs" / "manual"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "gates",
                            "--root",
                            str(root),
                            "--profile",
                            "generic",
                            "--run-dir",
                            str(run_dir),
                        ]
                    ),
                    0,
                )
            self.assertEqual(output.getvalue().strip(), "generic: 1/1 gates passed")
            gate_payload = json.loads(
                (
                    run_dir
                    / "artifacts"
                    / "gate-results-local-pre-commit.json"
                ).read_text(encoding="utf-8")
            )
            self.assertTrue(gate_payload["passed"])
            self.assertIsInstance(gate_payload["results"], list)
            results_by_command = {result["command"]: result for result in gate_payload["results"]}
            self.assertIn("agent_flow.core.architecture_lint", " ".join(results_by_command))
            self.assertFalse((run_dir / "gate-results.json").exists())

    def test_gate_results_allow_optional_failures(self) -> None:
        from agent_flow.core.artifacts import write_gate_results
        from agent_flow.core.gates import GateResult

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            write_gate_results(
                run_dir=run_dir,
                results=[
                    GateResult("build", ("npm", "run", "build"), True, 0, "ok", ""),
                    GateResult("lint", ("ruff", "check", "."), False, None, "", "missing", required=False),
                ],
                phase="all",
            )
            payload = json.loads((run_dir / "artifacts" / "gate-results.json").read_text(encoding="utf-8"))
            self.assertTrue(payload["passed"])
            self.assertEqual(payload["status"], "green")
            self.assertFalse(payload["results"][1]["required"])

    def test_gate_result_reports_deferred_ci_gates_without_claiming_pass(
        self,
    ) -> None:
        from agent_flow.core.artifacts import write_gate_results
        from agent_flow.core.gates import GateResult

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            write_gate_results(
                run_dir=run_dir,
                results=[
                    GateResult(
                        "architecture-lint",
                        ("agent-flow", "architecture-lint"),
                        True,
                        0,
                        "ok",
                        "",
                    )
                ],
                phase="all",
                execution="local",
                deferred_ci_checks=("pytest",),
            )

            payload = json.loads(
                (run_dir / "artifacts" / "gate-results.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(payload["passed"])
            self.assertEqual(payload["execution"], "local")
            self.assertEqual(payload["deferred_ci_checks"], ["pytest"])
            self.assertNotIn(
                "pytest",
                {result["gate_id"] for result in payload["results"]},
            )

    def test_gates_cli_uses_installed_profile_union_when_auto(self) -> None:
        from agent_flow.core.gates import GateResult

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            kit = root / ".agent-flow"
            kit.mkdir()
            (kit / "kit.json").write_text(
                json.dumps({"profile": "android", "profiles": ["android", "react-native"]}),
                encoding="utf-8",
            )
            captured: list[GateCommand] = []

            def fake_run_gates(
                commands: list[GateCommand],
                *,
                cwd: Path,
                timeout_s: int = 600,
                on_start=None,
            ) -> list[GateResult]:
                captured.extend(commands)
                return [
                    GateResult(command.gate_id, command.command, True, 0, "", "")
                    for command in commands
                ]

            output = io.StringIO()
            with mock.patch("agent_flow.cli.run_gates", side_effect=fake_run_gates):
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["gates", "--root", str(root), "--phase", "all"]), 0)

            commands = [command.command for command in captured]
            architecture_command = (
                sys.executable,
                "-m",
                "agent_flow.core.architecture_lint",
                "--profile",
                "android,react-native",
                "--profile-root",
                str(root.resolve()),
            )
            self.assertIn(architecture_command, commands)
            self.assertNotIn((sys.executable, "-m", "agent_flow.core.architecture_lint", "--profile", "android"), commands)
            self.assertNotIn((sys.executable, "-m", "agent_flow.core.architecture_lint", "--profile", "react-native"), commands)
            gate_ids = [command.gate_id for command in captured]
            self.assertLess(gate_ids.index("android:build"), gate_ids.index("architecture-lint"))
            self.assertLess(gate_ids.index("react-native:android-build"), gate_ids.index("react-native:lint"))
            self.assertLess(gate_ids.index("react-native:android-build"), gate_ids.index("architecture-lint"))
            self.assertEqual(output.getvalue().strip(), "android,react-native: 7/7 gates passed")

    def test_profile_gate_commands_enforce_build_typecheck_lint_order(self) -> None:
        from agent_flow.core.gate_plan import profile_gate_commands as _profile_gate_commands

        # BUILD -> TYPECHECK -> LINT 순서 계약은 게이트 전체 집합에 대한 것이다.
        # build 게이트는 pre-push라 기본 phase 필터에서는 보이지 않는다.
        typescript_ids = [command.gate_id for command in _profile_gate_commands(["typescript"], phase="all")]
        self.assertIn("architecture-lint", typescript_ids)
        self.assertLess(typescript_ids.index("build"), typescript_ids.index("typecheck"))
        self.assertLess(typescript_ids.index("typecheck"), typescript_ids.index("lint"))

        react_native_ids = [command.gate_id for command in _profile_gate_commands(["react-native"], phase="all")]
        self.assertLess(react_native_ids.index("android-build"), react_native_ids.index("lint"))
        self.assertLess(react_native_ids.index("ios-build"), react_native_ids.index("lint"))

    def test_gates_cli_reports_unknown_profile_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(main(["gates", "--root", str(root), "--profile", "does-not-exist"]), 1)

            self.assertIn("unknown profile: does-not-exist", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())

    def test_architecture_lint_cli_reports_unknown_profile_without_traceback(self) -> None:
        from agent_flow.core.architecture_lint import main as architecture_lint_main

        with tempfile.TemporaryDirectory() as temp_dir:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(architecture_lint_main(["--root", temp_dir, "--profile", "does-not-exist"]), 1)

            self.assertIn("unknown profile: does-not-exist", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())

    def test_gates_and_architecture_lint_use_literal_worktree_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            kit = root / ".agent-flow"
            kit.mkdir(parents=True)
            (kit / "kit.json").write_text(
                json.dumps({"profile": "android", "profiles": ["android", "react-native"]}),
                encoding="utf-8",
            )
            worktree = legacy_managed_root(root) / "semantic-architecture-parity"
            worktree.mkdir(parents=True)
            (worktree / ".git").write_text("gitdir: ../../.git/worktrees/semantic-architecture-parity\n", encoding="utf-8")

            output = io.StringIO()
            captured: list[GateCommand] = []

            def fake_run_gates(commands: list[GateCommand], *, cwd: Path, timeout_s: int = 600, on_start=None):
                captured.extend(commands)
                from agent_flow.core.gates import GateResult

                self.assertEqual(cwd.resolve(), worktree.resolve())
                return [
                    GateResult(command.gate_id, command.command, True, 0, "", "")
                    for command in commands
                ]

            with mock.patch("agent_flow.cli.run_gates", side_effect=fake_run_gates):
                with contextlib.redirect_stdout(output):
                    self.assertEqual(
                        main(
                            [
                                "gates",
                                "--root",
                                str(root),
                                "--worktree",
                                "semantic-architecture-parity",
                                "--phase",
                                "all",
                            ]
                        ),
                        0,
                    )
            self.assertEqual(output.getvalue().strip(), "android,react-native: 7/7 gates passed")
            self.assertIn(
                (
                    sys.executable,
                    "-m",
                    "agent_flow.core.architecture_lint",
                    "--profile",
                    "android,react-native",
                    "--profile-root",
                    str(root.resolve()),
                ),
                [command.command for command in captured],
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "gates",
                            "--root",
                            str(root),
                            "--worktree",
                            "semantic-architecture-parity",
                            "--profile",
                            "generic",
                        ]
                    ),
                    0,
                )
            self.assertEqual(output.getvalue().strip(), "generic: 1/1 gates passed")

            output = io.StringIO()
            captured_lint_args: list[str] = []

            def fake_architecture_lint_main(argv: list[str] | None = None) -> int:
                self.assertIsNotNone(argv)
                captured_lint_args.extend(argv or [])
                print("android,react-native: architecture lint passed")
                return 0

            with mock.patch(
                "agent_flow.cli.architecture_lint_main",
                side_effect=fake_architecture_lint_main,
            ):
                with contextlib.redirect_stdout(output):
                    self.assertEqual(
                        main(
                            [
                                "architecture-lint",
                                "--root",
                                str(root),
                                "--worktree",
                                "semantic-architecture-parity",
                            ]
                        ),
                        0,
                    )
            root_index = captured_lint_args.index("--root") + 1
            profile_index = captured_lint_args.index("--profile") + 1
            self.assertEqual(Path(captured_lint_args[root_index]).resolve(), worktree.resolve())
            self.assertEqual(captured_lint_args[profile_index], "android,react-native")
            self.assertIn("android,react-native: architecture lint passed", output.getvalue())

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "architecture-lint",
                            "--root",
                            str(root),
                            "--worktree",
                            "semantic-architecture-parity",
                            "--profile",
                            "generic",
                        ]
                    ),
                    0,
                )
            self.assertIn(
                "generic: architecture lint n/a (architecture contract absent)",
                output.getvalue(),
            )

    def test_node_architecture_lint_accepts_worktree_argument(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            worktree = legacy_managed_root(root) / "semantic-architecture-parity"
            worktree.mkdir(parents=True)
            (worktree / ".git").write_text("gitdir: ../../.git/worktrees/semantic-architecture-parity\n", encoding="utf-8")
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            result = subprocess.run(
                (
                    node,
                    cli,
                    "architecture-lint",
                    "--root",
                    str(root),
                    "--worktree",
                    "semantic-architecture-parity",
                    "--profile",
                    "generic",
                ),
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "generic: architecture lint n/a (architecture contract absent)",
                result.stdout,
            )

    def test_node_gates_accepts_worktree_argument(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            worktree = legacy_managed_root(root) / "semantic-architecture-parity"
            worktree.mkdir(parents=True)
            (worktree / ".git").write_text("gitdir: ../../.git/worktrees/semantic-architecture-parity\n", encoding="utf-8")
            run_dir = root / ".agent-flow" / "runs" / "worktree-runtime"
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            result = subprocess.run(
                (
                    node,
                    cli,
                    "gates",
                    "--root",
                    str(root),
                    "--worktree",
                    "semantic-architecture-parity",
                    "--profile",
                    "generic",
                    "--run-dir",
                    str(run_dir),
                ),
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("generic: 1/1 gates passed", result.stdout)
            gate_payload_text = (
                run_dir / "artifacts" / "gate-results-local-pre-commit.json"
            ).read_text(encoding="utf-8")
            self.assertNotIn(str(root), gate_payload_text)

    def test_gates_cli_resolves_relative_run_dir_against_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            cwd = Path(temp_dir) / "caller"
            root.mkdir()
            cwd.mkdir()
            old_cwd = Path.cwd()
            try:
                import os

                os.chdir(cwd)
                self.assertEqual(
                    main(
                        [
                            "gates",
                            "--root",
                            str(root),
                            "--profile",
                            "generic",
                            "--run-dir",
                            ".agent-flow/runs/manual",
                        ]
                    ),
                    0,
                )
            finally:
                os.chdir(old_cwd)
            focused = "gate-results-local-pre-commit.json"
            self.assertTrue(
                (
                    root
                    / ".agent-flow"
                    / "runs"
                    / "manual"
                    / "artifacts"
                    / focused
                ).is_file()
            )
            self.assertFalse(
                (
                    root
                    / ".agent-flow"
                    / "runs"
                    / "manual"
                    / "gate-results.json"
                ).exists()
            )
            self.assertFalse(
                (
                    cwd
                    / ".agent-flow"
                    / "runs"
                    / "manual"
                    / "artifacts"
                    / focused
                ).exists()
            )

    def test_handoff_writes_run_and_project_index_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertEqual(
                main(
                    [
                        "handoff",
                        "--root",
                        str(root),
                        "--run-dir",
                        ".agent-flow/runs/development/r1",
                        "--from-stage",
                        "explore",
                        "--to-stage",
                        "implement",
                        "--decided",
                        "Use profile gates.",
                        "--remaining",
                        "Implement CLI.",
                    ]
                ),
                0,
            )
            run_handoff = (
                root
                / ".agent-flow"
                / "runs"
                / "development"
                / "r1"
                / "handoffs"
                / "explore-to-implement.md"
            )
            project_handoff = root / ".agent-flow" / "handoffs" / "explore-to-implement.md"
            self.assertTrue(run_handoff.is_file())
            self.assertTrue(project_handoff.is_file())
            self.assertIn("Use profile gates.", run_handoff.read_text(encoding="utf-8"))

    def test_review_summary_lgtm_writes_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / ".agent-flow" / "runs" / "development" / "r1"
            review = run_dir / "artifacts" / "review-1.md"
            review.parent.mkdir(parents=True)
            review.write_text("## Findings\n- None\n\n## Verdict\nLGTM\n", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "review-summary",
                            "--root",
                            str(root),
                            "--run-dir",
                            ".agent-flow/runs/development/r1",
                            "--reviews",
                            ".agent-flow/runs/development/r1/artifacts/review-1.md",
                        ]
                    ),
                    0,
                )
            self.assertEqual(output.getvalue().strip(), "LGTM: 0 findings")
            self.assertTrue((run_dir / "review-summary.json").is_file())
            self.assertFalse((run_dir / "recovery.md").exists())

    def test_review_parser_accepts_agent_verdict_contract(self) -> None:
        self.assertEqual(_parse_verdict("verdict: approve\n"), "LGTM")
        self.assertEqual(_parse_verdict("verdict: request-changes\n"), "NEEDS_CHANGES")

    def test_default_final_review_routes_by_verdict(self) -> None:
        from agent_flow.runner import Phase, Runner

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            phase = Phase(
                id="final-review",
                description="",
                multi_review=True,
                routes={"approve": "commit", "request-changes": "fix-loop"},
            )
            runner = Runner.__new__(Runner)
            runner.run_dir = run_dir
            runner.phases = [phase, Phase(id="fix-loop", description=""), Phase(id="commit", description="")]

            (run_dir / "final-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: request-changes\n\n"
                "## Overall\n"
                "verdict: request-changes\n",
                encoding="utf-8",
            )
            _write_node_review_results(
                run_dir,
                "final-review",
                ("request-changes",),
            )
            self.assertEqual(runner._next_index(0, phase)[:2], (1, False))

            (run_dir / "final-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: request-changes\n\n"
                "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n"
                "## Overall\n"
                "verdict: request-changes\n",
                encoding="utf-8",
            )
            _write_node_review_results(
                run_dir,
                "final-review",
                ("request-changes", "approve"),
            )
            self.assertEqual(runner._next_index(0, phase)[:2], (1, False))

            (run_dir / "final-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n"
                "## Overall\n"
                "verdict: approve\n",
                encoding="utf-8",
            )
            _write_node_review_results(
                run_dir,
                "final-review",
                ("approve", "approve"),
            )
            self.assertEqual(runner._next_index(0, phase)[:2], (2, False))

    def test_default_final_review_blocks_one_subagent_reviewer_approve(self) -> None:
        from agent_flow.runner import Phase, Runner

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            phase = Phase(
                id="final-review",
                description="",
                multi_review=True,
                routes={"approve": "commit", "request-changes": "fix-loop"},
            )
            runner = Runner.__new__(Runner)
            runner.run_dir = run_dir
            runner.phases = [phase, Phase(id="fix-loop", description=""), Phase(id="commit", description="")]
            (run_dir / "final-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n## Overall\nverdict: approve\n",
                encoding="utf-8",
            )
            _write_node_review_results(
                run_dir,
                "final-review",
                ("approve",),
            )

            self.assertEqual(runner._next_index(0, phase)[:2], (0, True))

    def test_provider_rate_limits_render_retry_status(self) -> None:
        from agent_flow.multi_review import (
            ResolvedLaunch,
            _render_angle_result,
            reviewer_result_error,
        )
        from agent_flow.subprocess_pool import SubprocessResult

        cases = [
            ("codex-generalist", "429 too many requests; rate limit resets in 5 minutes", "codex"),
        ]
        for job_id, stderr, reviewer in cases:
            artifact = _render_angle_result(
                SubprocessResult(
                    job_id=job_id,
                    stderr=stderr,
                    returncode=1,
                ),
                launch=ResolvedLaunch(
                    reviewer,
                    None,
                    None,
                    (),
                    "test",
                ),
            )
            self.assertIn("reason: reviewer_rate_limited", artifact)
            self.assertIn(f"reviewer: {reviewer}", artifact)
            self.assertIn(f"next_command: agent-flow review retry --reviewer {reviewer}", artifact)

        legitimate = SubprocessResult(
            job_id="claude-generalist",
            returncode=0,
            stdout=(
                "reviewer-source: sub-agent\n"
                "The API rate limit is enforced correctly. No findings.\n"
                "verdict: approve"
            ),
        )
        self.assertIsNone(reviewer_result_error(legitimate))

    def test_review_summary_needs_changes_writes_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / ".agent-flow" / "runs" / "development" / "r1"
            review = run_dir / "artifacts" / "review-1.md"
            review.parent.mkdir(parents=True)
            review.write_text(
                "## Findings\n- Missing regression test @ tests/test_cli.py\n\n## Verdict\nNEEDS_CHANGES\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "review-summary",
                            "--root",
                            str(root),
                            "--run-dir",
                            ".agent-flow/runs/development/r1",
                            "--reviews",
                            ".agent-flow/runs/development/r1/artifacts/review-1.md",
                        ]
                    ),
                    1,
                )
            self.assertEqual(output.getvalue().strip(), "NEEDS_CHANGES: 1 findings")
            self.assertTrue((run_dir / "review-summary.json").is_file())
            recovery = run_dir / "recovery.md"
            self.assertTrue(recovery.is_file())
            self.assertIn("Review needs changes", recovery.read_text(encoding="utf-8"))

    def test_review_summary_unknown_verdict_needs_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / ".agent-flow" / "runs" / "development" / "r1"
            review = run_dir / "artifacts" / "review-1.md"
            review.parent.mkdir(parents=True)
            review.write_text("Looks mostly fine, but add tests.\n", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "review-summary",
                            "--root",
                            str(root),
                            "--run-dir",
                            ".agent-flow/runs/development/r1",
                            "--reviews",
                            ".agent-flow/runs/development/r1/artifacts/review-1.md",
                        ]
                    ),
                    1,
                )
            self.assertEqual(output.getvalue().strip(), "NEEDS_CHANGES: 1 findings")
            self.assertTrue((run_dir / "recovery.md").is_file())

    def test_plan_worktree_sanitizes_name_and_defaults_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = plan_worktree(root=root, name="Implement Login")
            self.assertEqual(plan.name, "feat-implement-login")
            self.assertEqual(plan.branch, "feat/implement-login")
            self.assertEqual(plan.path, managed_worktrees_root(root) / "feat-implement-login")

            korean_plan = plan_worktree(root=root, name="버그 수정")
            # 한글 task도 deterministic fallback slug로 worktree를 만들 수 있어야 한다.
            self.assertRegex(korean_plan.name, r"^feat-task-[a-f0-9]{8}$")
            self.assertEqual(korean_plan.branch, korean_plan.name.replace("feat-", "feat/", 1))
            self.assertEqual(korean_plan.path, managed_worktrees_root(root) / korean_plan.name)
            with mock.patch("agent_flow.core.commands.subprocess.Popen", side_effect=OSError("no git")):
                fallback_plan = plan_worktree(root=root, name="No Git")
            # git 확인이 불가능한 환경에서는 기존 HEAD fallback으로 plan 생성만 유지한다.
            self.assertEqual(fallback_plan.base_ref, "HEAD")
            with self.assertRaises(ValueError):
                plan_worktree(root=root, name="Mainline", branch="main")

    def test_worktree_status_reports_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["worktree", "status", "--root", temp_dir, "--name", "missing"]),
                    0,
                )
            self.assertIn("feat-missing feat/missing", output.getvalue())
            self.assertTrue(output.getvalue().strip().endswith("missing"))

    def test_worktree_create_creates_git_worktree_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "worktree",
                            "create",
                            "--root",
                            str(root),
                            "--name",
                            "slice-a",
                        ]
                    ),
                    0,
            )
            worktree = managed_worktrees_root(root) / "feat-slice-a"
            self.assertTrue(worktree.is_dir())
            self.assertTrue((worktree_runtime_root(root=root, name="feat-slice-a") / "manifest.json").is_file())
            self.assertFalse((worktree / "manifest.json").exists())
            self.assertIn("feat-slice-a feat/slice-a", output.getvalue())

    def test_worktree_create_uses_main_base_without_switching_leader(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            subprocess.run(("git", "branch", "-M", "main"), cwd=root, check=True)
            main_sha = subprocess.run(
                ("git", "rev-parse", "main"),
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            subprocess.run(("git", "checkout", "-q", "-b", "codex/current"), cwd=root, check=True)
            (root / "feature.txt").write_text("feature\n", encoding="utf-8")
            subprocess.run(("git", "add", "feature.txt"), cwd=root, check=True)
            subprocess.run(("git", "commit", "-q", "-m", "feature"), cwd=root, check=True)

            self.assertEqual(main(["worktree", "create", "--root", str(root), "--name", "slice-a"]), 0)

            leader_branch = subprocess.run(
                ("git", "branch", "--show-current"),
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            worktree_head = subprocess.run(
                ("git", "rev-parse", "HEAD"),
                cwd=managed_worktrees_root(root) / "feat-slice-a",
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            # leader worktree는 feature branch에 남고, 새 worktree만 main commit에서 시작한다.
            self.assertEqual(leader_branch, "codex/current")
            self.assertEqual(worktree_head, main_sha)

    def test_worktree_create_rejects_dirty_leader_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            (root / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            self.assertEqual(main(["worktree", "create", "--root", str(root), "--name", "dirty"]), 2)

    def test_worktree_create_allows_untracked_agent_flow_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            self.assertEqual(main(["init", "--root", str(root)]), 0)
            self.assertEqual(
                main(["worktree", "create", "--root", str(root), "--name", "after-init"]),
                0,
            )
            self.assertTrue((managed_worktrees_root(root) / "feat-after-init").is_dir())

    def test_worktree_remove_accepts_listed_name_of_direct_worktree(self) -> None:
        # 손으로 예전 자리에 만든 worktree다. manifest가 없고 디스크 이름이
        # agent-flow 정규화 결과와 어긋난다.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            subprocess.run(
                (
                    "git",
                    "worktree",
                    "add",
                    "-q",
                    "-b",
                    "feat/issue#110",
                    ".agent-flow/worktrees/feat-issue#110/",
                    "main",
                ),
                cwd=root,
                check=True,
            )
            listing = io.StringIO()
            with contextlib.redirect_stdout(listing):
                self.assertEqual(main(["worktree", "list", "--root", str(root)]), 0)
            listed_name = listing.getvalue().split()[0]
            self.assertEqual(listed_name, "feat-issue#110")

            removed = io.StringIO()
            with contextlib.redirect_stdout(removed):
                self.assertEqual(
                    main(["worktree", "remove", "--root", str(root), "--name", listed_name]),
                    0,
                )
            self.assertFalse((managed_worktrees_root(root) / "feat-issue#110").exists())
            # agent-flow가 그 브랜치를 만들었다는 증거가 없으므로 브랜치는 남는다.
            self.assertIn("kept branch feat/issue#110", removed.getvalue())
            branches = subprocess.run(
                ("git", "branch", "--format=%(refname:short)"),
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.split()
            self.assertIn("feat/issue#110", branches)

    def test_worktree_status_resolves_slug_dash_and_slash_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            _init_git_repo(root)
            self.assertEqual(main(["worktree", "create", "--root", str(root), "--name", "slice-a"]), 0)
            expected = managed_worktrees_root(root) / "feat-slice-a"
            for name in ("slice-a", "feat-slice-a", "feat/slice-a"):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(
                        main(["worktree", "status", "--root", str(root), "--name", name]),
                        0,
                    )
                self.assertEqual(
                    output.getvalue().strip(),
                    f"feat-slice-a feat/slice-a {expected.resolve()} exists",
                    name,
                )

    def test_worktree_remove_refuses_checkout_owned_by_another_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "leader"
            root.mkdir()
            _init_git_repo(root)
            foreign = Path(temp_dir) / "foreign"
            foreign.mkdir()
            _init_git_repo(foreign)
            planted = managed_worktrees_root(root) / "feat-alien"
            planted.parent.mkdir(parents=True)
            subprocess.run(
                ("git", "worktree", "add", "-q", "-b", "feat/alien", str(planted), "main"),
                cwd=foreign,
                check=True,
            )

            # --allow-unmerged로 병합 증명을 건너뛰어도 소유 증명은 남아 있어야 한다.
            for argv in (
                ["worktree", "remove", "--root", str(root), "--name", "feat-alien"],
                ["worktree", "remove", "--root", str(root), "--name", "feat-alien", "--allow-unmerged"],
            ):
                error = io.StringIO()
                with contextlib.redirect_stderr(error):
                    self.assertEqual(main(argv), 2)
                self.assertIn("refusing to remove", error.getvalue())
                self.assertIn("feat-alien", error.getvalue())
            self.assertTrue((planted / ".git").is_file())
            foreign_branches = subprocess.run(
                ("git", "branch", "--format=%(refname:short)"),
                cwd=foreign,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.split()
            self.assertIn("feat/alien", foreign_branches)

    def test_worktree_remove_clears_registration_when_directory_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            worktree = legacy_managed_root(root) / "feat-issue#110"
            subprocess.run(
                (
                    "git",
                    "worktree",
                    "add",
                    "-q",
                    "-b",
                    "feat/issue#110",
                    ".agent-flow/worktrees/feat-issue#110/",
                    "main",
                ),
                cwd=root,
                check=True,
            )
            # 디렉터리만 사라지고 git 등록은 남은 상태. 디스크 스캔으로는 안 보인다.
            shutil.rmtree(worktree)

            listing = io.StringIO()
            with contextlib.redirect_stdout(listing):
                self.assertEqual(main(["worktree", "list", "--root", str(root)]), 0)
            self.assertIn("feat-issue#110", listing.getvalue())
            self.assertIn("stale", listing.getvalue())

            self.assertEqual(
                main(["worktree", "remove", "--root", str(root), "--name", "feat-issue#110"]),
                0,
            )
            registered = subprocess.run(
                ("git", "worktree", "list", "--porcelain"),
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            self.assertNotIn("feat-issue#110", registered)

    def test_worktree_remove_protects_uncommitted_work_in_direct_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            worktree = legacy_managed_root(root) / "feat-issue#110"
            subprocess.run(
                (
                    "git",
                    "worktree",
                    "add",
                    "-q",
                    "-b",
                    "feat/issue#110",
                    ".agent-flow/worktrees/feat-issue#110/",
                    "main",
                ),
                cwd=root,
                check=True,
            )
            (worktree / "wip.txt").write_text("wip\n", encoding="utf-8")

            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                self.assertEqual(
                    main(["worktree", "remove", "--root", str(root), "--name", "feat-issue#110"]),
                    2,
                )
            self.assertIn("uncommitted", error.getvalue())
            self.assertTrue(worktree.is_dir())

            self.assertEqual(
                main(
                    [
                        "worktree",
                        "remove",
                        "--root",
                        str(root),
                        "--name",
                        "feat-issue#110",
                        "--allow-unmerged",
                    ]
                ),
                0,
            )
            self.assertFalse(worktree.exists())

    def test_guard_protected_branch_blocks_chained_commit(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "guard-protected-branch.sh"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            feature_worktree = root / "feature-worktree"
            subprocess.run(
                ("git", "worktree", "add", "-q", "-b", "feat/test", str(feature_worktree), "main"),
                cwd=root,
                check=True,
            )
            for command in (
                "cd . && git commit -m test",
                "git -C . commit -m test",
                "command git commit -m test",
                "env TEST=1 git push origin main",
            ):
                result = subprocess.run(
                    ("bash", str(script)),
                    cwd=root,
                    input=json.dumps({"tool_input": {"cmd": command}}),
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2, command)
                self.assertIn("보호 브랜치", result.stderr)

            allowed = subprocess.run(
                ("bash", str(script)),
                cwd=root,
                input=json.dumps({"tool_input": {"cmd": f"git -C {feature_worktree} commit -m test"}}),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(allowed.returncode, 0)

            allowed_cd = subprocess.run(
                ("bash", str(script)),
                cwd=root,
                input=json.dumps({"tool_input": {"cmd": f"cd {feature_worktree} && git commit -m test"}}),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(allowed_cd.returncode, 0)

            for command in (
                f"(cd {feature_worktree} && true); git commit -m test",
                f"(cd {feature_worktree} && git status); git push origin main",
                "cd missing-dir || git commit -m test",
                "cd /no/such/path; git push origin main",
            ):
                blocked_after_subshell = subprocess.run(
                    ("bash", str(script)),
                    cwd=root,
                    input=json.dumps({"tool_input": {"cmd": command}}),
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(blocked_after_subshell.returncode, 2, command)
                self.assertIn("보호 브랜치", blocked_after_subshell.stderr)

    def test_guard_protected_branch_reads_the_declared_tool_cwd(self) -> None:
        """세션 프로세스의 cwd가 아니라 도구 호출이 선언한 자리로 판정해야 한다.

        host는 세션을 leader에서 연다. 그 자리를 브랜치 판정에 쓰면 worktree를
        tool cwd로 정확히 넘긴 커밋이 leader의 main으로 읽혀 막히고, 반대로
        leader를 향한 커밋이 세션이 서 있는 worktree 이름으로 통과한다.
        `host_write_boundary._session_cwd`는 이미 선언된 cwd를 쓴다.
        """
        script = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "guard-protected-branch.sh"

        def guard(
            payload: dict, cwd: Path, env: dict | None = None
        ) -> subprocess.CompletedProcess:
            return subprocess.run(
                ("bash", str(script)),
                cwd=cwd,
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, **env} if env else None,
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            feature_worktree = root / "feature-worktree"
            subprocess.run(
                ("git", "worktree", "add", "-q", "-b", "feat/test", str(feature_worktree), "main"),
                cwd=root,
                check=True,
            )

            for key in ("cwd", "workdir", "working_directory"):
                allowed = guard(
                    {
                        "tool_name": "Bash",
                        "tool_input": {"command": "git commit -m test", key: str(feature_worktree)},
                    },
                    root,
                )
                self.assertEqual(allowed.returncode, 0, f"{key}: {allowed.stderr}")

            allowed_payload_cwd = guard(
                {
                    "tool_name": "Bash",
                    "cwd": str(feature_worktree),
                    "tool_input": {"command": "git push origin main"},
                },
                root,
            )
            self.assertEqual(allowed_payload_cwd.returncode, 0, allowed_payload_cwd.stderr)

            # payload 층은 `cwd`만 읽는다. `_declared_command_cwd`가 그렇고, 넓히면
            # 검증하지 않은 분기가 통과 방향으로 남는다.
            for payload_key in ("workdir", "working_directory"):
                blocked_payload_alias = guard(
                    {
                        "tool_name": "Bash",
                        payload_key: str(feature_worktree),
                        "tool_input": {"command": "git commit -m test"},
                    },
                    root,
                )
                self.assertEqual(
                    blocked_payload_alias.returncode, 2, f"{payload_key}: {blocked_payload_alias.stderr}"
                )

            # tool input 컨테이너 이름은 host마다 다르다. `command_from`은 재귀로
            # `input.command`를 찾아 검사하는데 cwd만 `tool_input`에서 찾으면,
            # 세션이 worktree에 선 상태에서 leader를 향한 보호 브랜치 커밋이
            # feature branch로 읽혀 통과한다. `_tool_input`과 같은 키를 쓴다.
            for container in ("input", "parameters"):
                blocked_leader_target = guard(
                    {
                        "tool_name": "Bash",
                        container: {"command": "git commit -m test", "cwd": str(root)},
                    },
                    feature_worktree,
                )
                self.assertEqual(
                    blocked_leader_target.returncode, 2, f"{container}: {blocked_leader_target.stderr}"
                )

                allowed_worktree_target = guard(
                    {
                        "tool_name": "Bash",
                        container: {
                            "command": "git commit -m test",
                            "cwd": str(feature_worktree),
                        },
                    },
                    root,
                )
                self.assertEqual(
                    allowed_worktree_target.returncode, 0, f"{container}: {allowed_worktree_target.stderr}"
                )

            # 첫 번째로 **존재하는** 컨테이너가 이긴다(`_tool_input`과 동일).
            # "첫 번째 dict"로 완화하면 앞선 컨테이너가 비-dict일 때 뒤쪽 선언이
            # 자리를 만들어 낸다 — 통과 방향으로 실패한다.
            blocked_shadowed_container = guard(
                {
                    "tool_name": "Bash",
                    "tool_input": "not-a-dict",
                    "input": {"command": "git commit -m test", "cwd": str(feature_worktree)},
                },
                root,
            )
            self.assertEqual(
                blocked_shadowed_container.returncode, 2, blocked_shadowed_container.stderr
            )

            allowed_relative = guard(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "git commit -m test", "cwd": "feature-worktree"},
                },
                root,
            )
            self.assertEqual(allowed_relative.returncode, 0, allowed_relative.stderr)

            # 반대 방향. 선언을 읽지 않으면 세션이 선 worktree(feat/test) 때문에
            # leader의 main을 향한 커밋이 통과한다.
            blocked_by_declaration = guard(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "git commit -m test", "cwd": str(root)},
                },
                feature_worktree,
            )
            self.assertEqual(blocked_by_declaration.returncode, 2, blocked_by_declaration.stderr)
            self.assertIn("보호 브랜치", blocked_by_declaration.stderr)

            # 실재하지 않는 선언은 권한을 만들지 않는다. 세션 cwd로 접는다.
            blocked_missing_declaration = guard(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "git commit -m test", "cwd": "/no/such/path"},
                },
                root,
            )
            self.assertEqual(blocked_missing_declaration.returncode, 2, blocked_missing_declaration.stderr)

            # 실행 자리를 옮기지 않는 필드는 판정도 옮기지 못한다. env는 환경변수
            # map이라 모델이 자유롭게 채울 수 있고, cwd 자리에 담긴 컨테이너도
            # 선언이 아니다. payload를 재귀로 뒤지면 이 둘이 자리로 채택된다.
            for decoy in (
                {"command": "git commit -m test", "env": {"cwd": str(feature_worktree)}},
                {"command": "git commit -m test", "cwd": [{"cwd": str(feature_worktree)}]},
            ):
                blocked_decoy = guard({"tool_name": "Bash", "tool_input": decoy}, root)
                self.assertEqual(blocked_decoy.returncode, 2, blocked_decoy.stderr)

            # payload cwd만 엉터리여도 세션 cwd로 접혀 차단이 남는다.
            blocked_bogus_payload_cwd = guard(
                {
                    "tool_name": "Bash",
                    "cwd": "/no/such/path",
                    "tool_input": {"command": "git commit -m test"},
                },
                root,
            )
            self.assertEqual(
                blocked_bogus_payload_cwd.returncode, 2, blocked_bogus_payload_cwd.stderr
            )

            # 디렉터리가 아닌 선언은 자리가 될 수 없다. 파일을 cwd로 주면 git
            # subprocess가 OSError를 내고, 미판정은 차단이 아니라 통과다.
            blocked_file_declaration = guard(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "git commit -m test", "cwd": "/dev/null"},
                },
                root,
            )
            self.assertEqual(
                blocked_file_declaration.returncode, 2, blocked_file_declaration.stderr
            )

            allowed_home_relative = guard(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "git commit -m test", "cwd": "~/feature-worktree"},
                },
                root,
                {"HOME": str(root)},
            )
            self.assertEqual(allowed_home_relative.returncode, 0, allowed_home_relative.stderr)

            # tool cwd가 payload cwd를 이긴다. host는 payload에 세션 자리를 싣고
            # 개별 호출에서만 worktree를 지정할 수 있다.
            allowed_tool_cwd_wins = guard(
                {
                    "tool_name": "Bash",
                    "cwd": str(root),
                    "tool_input": {"command": "git commit -m test", "cwd": str(feature_worktree)},
                },
                root,
            )
            self.assertEqual(allowed_tool_cwd_wins.returncode, 0, allowed_tool_cwd_wins.stderr)

            # 상대 tool cwd는 세션이 아니라 payload cwd를 기준으로 푼다. 같은
            # 이름을 양쪽에 두어야 기준을 바꾼 구현이 leader의 main으로 떨어진다.
            (feature_worktree / "nested").mkdir()
            (root / "nested").mkdir()
            allowed_relative_to_payload = guard(
                {
                    "tool_name": "Bash",
                    "cwd": str(feature_worktree),
                    "tool_input": {"command": "git commit -m test", "cwd": "nested"},
                },
                root,
            )
            self.assertEqual(
                allowed_relative_to_payload.returncode, 0, allowed_relative_to_payload.stderr
            )

            # 명령 앞머리의 cd는 세션이 아니라 선언한 자리 위에 얹힌다. 세션은
            # feat/test에 서 있고 선언은 leader이므로, 기준을 세션으로 되돌린
            # 구현에서는 이 상대 cd가 feat/test로 떨어져 차단이 사라진다.
            blocked_relative_cd = guard(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "cd nested && git commit -m test", "cwd": str(root)},
                },
                feature_worktree,
            )
            self.assertEqual(blocked_relative_cd.returncode, 2, blocked_relative_cd.stderr)


    def test_cli_imports_without_fcntl(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run(
            (
                sys.executable,
                "-c",
                (
                    "import sys, tempfile; from pathlib import Path; "
                    "sys.modules['fcntl'] = None; import agent_flow.cli; "
                    "from agent_flow.core.worktree_isolation import "
                    "exclusive_file_lease, FileLeaseUnavailable; "
                    "blocked = False; "
                    "lock = Path(tempfile.mkdtemp()) / 'lock'; "
                    "\ntry:\n"
                    "    with exclusive_file_lease(lock): pass\n"
                    "except FileLeaseUnavailable:\n"
                    "    blocked = True\n"
                    "print('ok' if blocked else 'unsafe')"
                ),
            ),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_python_recognizes_project_omp_worktrees_but_not_home_omp(self) -> None:
        from agent_flow.cli import _managed_worktree_context

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            project = root / "project"
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                self.assertEqual(
                    _managed_worktree_context(
                        project / ".omp" / "worktrees" / "feat-task" / "src"
                    ),
                    (project.resolve(), "feat-task"),
                )
                self.assertIsNone(
                    _managed_worktree_context(
                        home / ".omp" / "worktrees" / "global-task" / "src"
                    )
                )

    def test_python_does_not_treat_user_central_worktrees_as_project_markers(self) -> None:
        from agent_flow.cli import _managed_worktree_context

        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            checkout = (
                home
                / ".agent-flow"
                / "worktrees"
                / "project-a1b2c3d4e5f6"
                / "feat-task"
                / "src"
            )
            with mock.patch.dict(
                os.environ,
                {"HOME": str(home), "XDG_STATE_HOME": ""},
            ):
                self.assertIsNone(_managed_worktree_context(checkout))



    def test_team_state_init_task_worker_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertEqual(
                main(["team", "init", "--root", str(root), "--name", "Feature Team", "--description", "login"]),
                0,
            )
            self.assertEqual(
                main(
                    [
                        "team",
                        "task",
                        "--root",
                        str(root),
                        "--team",
                        "feature-team",
                        "--id",
                        "task-1",
                        "--subject",
                        "Implement login",
                    ]
                ),
                0,
            )
            self.assertEqual(
                main(
                    [
                        "team",
                        "worker",
                        "--root",
                        str(root),
                        "--team",
                        "feature-team",
                        "--name",
                        "worker-1",
                        "--role",
                        "implementer",
                    ]
                ),
                0,
            )
            team_root = root / ".agent-flow" / "state" / "team" / "feature-team"
            self.assertTrue((team_root / "config.json").is_file())
            self.assertTrue((team_root / "tasks" / "task-1.json").is_file())
            self.assertTrue((team_root / "workers" / "worker-1" / "identity.json").is_file())
            self.assertTrue((team_root / "workers" / "worker-1" / "heartbeat.json").is_file())
            self.assertTrue((team_root / "shutdown").is_dir())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["team", "status", "--root", str(root), "--team", "feature-team"]),
                    0,
                )
            status_lines = output.getvalue().strip().splitlines()
            self.assertEqual(status_lines[0], "feature-team tasks=1 workers=1 exists=True")
            self.assertIn("worker-1 idle alive", status_lines[1])

    @unittest.skipUnless(
        sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file(),
        "macOS sandbox-exec confinement is required",
    )
    def test_team_run_next_completes_pending_task_with_host_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            _create_team_with_task_and_worker(root)
            _approve_worker_for_task(root)

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "team",
                            "run-next",
                            "--root",
                            str(root),
                            "--team",
                            "feature-team",
                            "--worker",
                            "worker-1",
                            "--command",
                            sys.executable,
                            "-c",
                            "print('runtime ok')",
                        ]
                    ),
                    0,
                )
            self.assertIn("task-1 completed worktree=", output.getvalue())

            status_output = io.StringIO()
            with contextlib.redirect_stdout(status_output):
                self.assertEqual(
                    main(["team", "status", "--root", str(root), "--team", "feature-team", "--detail"]),
                    0,
                )
            self.assertIn("task task-1 completed owner=worker-1 subject=Implement login", status_output.getvalue())


    @unittest.skipUnless(
        sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file(),
        "macOS sandbox-exec confinement is required",
    )
    def test_team_run_next_sweeps_the_scope_the_profile_declared(self) -> None:
        """반증: 이 경로의 sweep 범위 배선이 빠져도 team 테스트는 전부 green이었다.

        `tracked-only`를 선언한 프로젝트의 team claim만 leader의 gitignored
        산출물에 걸려 워커 결과가 failed로 되돌아간다. 파라미터 짝으로 양방향을
        고정한다 — `tracked-only`에서 전수 sweep이 나가면 배선이 빠진 것이고,
        미선언에서 좁은 sweep이 나가면 탐지가 통째로 사라진 것이다.
        """
        from agent_flow import cli as cli_module
        from agent_flow.core.worktree_isolation import leader_sweep_scope

        for declared, include_ignored in ((None, True), ("tracked-only", False)):
            with self.subTest(declared=declared):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    _init_git_repo(root)
                    if declared is not None:
                        declaration = root / ".agent-flow" / "profiles"
                        declaration.mkdir(parents=True, exist_ok=True)
                        path = declaration / "generic.local.yaml"
                        path.write_text(
                            f"branching:\n  leader_tripwire: {declared}\n",
                            encoding="utf-8",
                        )
                        subprocess.run(
                            ["git", "add", "-f", str(path.relative_to(root))],
                            cwd=root,
                            check=True,
                            capture_output=True,
                        )
                        subprocess.run(
                            ["git", "commit", "-m", "declare leader_tripwire"],
                            cwd=root,
                            check=True,
                            capture_output=True,
                        )
                    _create_team_with_task_and_worker(root)
                    _approve_worker_for_task(root)

                    captured: list[bool] = []
                    asserted: list[bool] = []
                    real_capture = cli_module.capture_leader_snapshot

                    def record_capture(leader_root, *, include_ignored=True):
                        captured.append(include_ignored)
                        return real_capture(leader_root, include_ignored=include_ignored)

                    def record_assert(leader_root, before, **kwargs):
                        asserted.append(kwargs.get("include_ignored"))

                    with mock.patch.object(
                        cli_module, "capture_leader_snapshot", record_capture
                    ), mock.patch.object(
                        cli_module, "assert_leader_unchanged", record_assert
                    ):
                        with contextlib.redirect_stdout(io.StringIO()):
                            self.assertEqual(
                                main(
                                    [
                                        "team",
                                        "run-next",
                                        "--root",
                                        str(root),
                                        "--team",
                                        "feature-team",
                                        "--worker",
                                        "worker-1",
                                        "--command",
                                        sys.executable,
                                        "-c",
                                        "print('runtime ok')",
                                    ]
                                ),
                                0,
                            )

                    self.assertEqual(captured, [include_ignored])
                    # 대조는 기록된 범위를 되읽는다. 다시 해석하면 워커가 도는 동안
                    # 선언이 바뀌었을 때 baseline과 관측의 범위가 갈린다.
                    self.assertEqual(
                        asserted,
                        [leader_sweep_scope(include_ignored) == "all"],
                    )
    @unittest.skipUnless(
        sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file(),
        "macOS sandbox-exec confinement is required",
    )
    def test_team_run_next_fails_task_when_host_command_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            _create_team_with_task_and_worker(root)
            _approve_worker_for_task(root)

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "team",
                            "run-next",
                            "--root",
                            str(root),
                            "--team",
                            "feature-team",
                            "--worker",
                            "worker-1",
                            "--command",
                            sys.executable,
                            "-c",
                            "import sys; print('runtime failed'); sys.exit(2)",
                        ]
                    ),
                    1,
                )
            self.assertIn("task-1 failed worktree=", output.getvalue())

            status_output = io.StringIO()
            with contextlib.redirect_stdout(status_output):
                self.assertEqual(
                    main(["team", "status", "--root", str(root), "--team", "feature-team", "--detail"]),
                    0,
                )
            self.assertIn("task task-1 failed owner=worker-1 subject=Implement login", status_output.getvalue())

    def test_team_heartbeat_updates_worker_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "team",
                            "heartbeat",
                            "--root",
                            str(root),
                            "--team",
                            "feature-team",
                            "--worker",
                            "worker-1",
                            "--status",
                            "reviewing",
                        ]
                    ),
                    0,
                )
            self.assertIn("worker-1 reviewing alive", output.getvalue())
            heartbeat = _read_heartbeat_json(root)
            self.assertEqual(heartbeat["worker"], "worker-1")
            self.assertEqual(heartbeat["status"], "reviewing")
            self.assertTrue(heartbeat["alive"])

    def test_team_status_detail_lists_tasks_workers_and_shutdowns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            main(
                [
                    "team",
                    "message",
                    "--root",
                    str(root),
                    "--team",
                    "feature-team",
                    "--from-actor",
                    "lead",
                    "--to-worker",
                    "worker-1",
                    "--body",
                    "status check",
                ]
            )
            main(
                [
                    "team",
                    "shutdown",
                    "--root",
                    str(root),
                    "--team",
                    "feature-team",
                    "--worker",
                    "worker-1",
                    "--reason",
                    "done",
                ]
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["team", "status", "--root", str(root), "--team", "feature-team", "--detail"]),
                    0,
                )
            lines = output.getvalue().strip().splitlines()
            self.assertIn("task task-1 pending owner=- subject=Implement login", lines)
            self.assertIn("worker worker-1 role=implementer status=idle unread=1", lines)
            self.assertTrue(any(line.startswith("shutdown ") and "worker=worker-1 pending reason=done" in line for line in lines))

    def test_team_status_summary_ignores_detail_state_problems(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            team_root = root / ".agent-flow" / "state" / "team" / "feature-team"
            mailbox_dir = team_root / "mailbox"
            (mailbox_dir / "worker-1.json").unlink()
            mailbox_dir.rmdir()
            shutdown_dir = team_root / "shutdown" / "worker-1"
            shutdown_dir.mkdir(parents=True)
            (shutdown_dir / f'{"a" * 32}.json').write_text("{", encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["team", "status", "--root", str(root), "--team", "feature-team"]),
                    0,
                )
            self.assertEqual(output.getvalue().strip().splitlines()[0], "feature-team tasks=1 workers=1 exists=True")
            self.assertFalse(mailbox_dir.exists())

    def test_team_export_outputs_state_snapshot_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            main(
                [
                    "team",
                    "message",
                    "--root",
                    str(root),
                    "--team",
                    "feature-team",
                    "--from-actor",
                    "lead",
                    "--to-worker",
                    "worker-1",
                    "--body",
                    "export me",
                ]
            )
            main(
                [
                    "team",
                    "shutdown",
                    "--root",
                    str(root),
                    "--team",
                    "feature-team",
                    "--worker",
                    "worker-1",
                    "--reason",
                    "export",
                ]
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["team", "export", "--root", str(root), "--team", "feature-team"]),
                    0,
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["team"]["name"], "feature-team")
            self.assertEqual(payload["tasks"][0]["task_id"], "task-1")
            self.assertEqual(payload["workers"][0]["name"], "worker-1")
            self.assertEqual(payload["mailboxes"]["worker-1"][0]["body"], "export me")
            self.assertEqual(payload["shutdowns"][0]["reason"], "export")

    def test_team_export_preserves_extra_fields_and_missing_mailbox(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            team_root = root / ".agent-flow" / "state" / "team" / "feature-team"
            task_path = team_root / "tasks" / "task-1.json"
            task = json.loads(task_path.read_text(encoding="utf-8"))
            task["future_field"] = "kept"
            task_path.write_text(json.dumps(task), encoding="utf-8")
            mailbox_dir = team_root / "mailbox"
            (mailbox_dir / "worker-1.json").unlink()
            mailbox_dir.rmdir()

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["team", "export", "--root", str(root), "--team", "feature-team"]),
                    0,
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["tasks"][0]["future_field"], "kept")
            self.assertEqual(payload["mailboxes"]["worker-1"], [])
            self.assertFalse(mailbox_dir.exists())

    def test_team_list_reports_existing_teams(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            self.assertEqual(
                main(
                    [
                        "team",
                        "init",
                        "--root",
                        str(root),
                        "--name",
                        "Second Team",
                        "--description",
                        "another",
                    ]
                ),
                0,
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["team", "list", "--root", str(root)]), 0)
            lines = output.getvalue().strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("feature-team tasks=1 workers=1", lines[0])
            self.assertIn("second-team tasks=0 workers=0", lines[1])

    def test_team_list_is_empty_without_team_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["team", "list", "--root", temp_dir]), 0)
            self.assertEqual(output.getvalue(), "")

    def test_team_archive_moves_team_state_to_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "team",
                            "archive",
                            "--root",
                            str(root),
                            "--team",
                            "feature-team",
                            "--reason",
                            "done",
                        ]
                    ),
                    0,
                )
            self.assertIn("feature-team archived", output.getvalue())
            resolved_root = root.resolve()
            team_root = resolved_root / ".agent-flow" / "state" / "team" / "feature-team"
            self.assertFalse(team_root.exists())
            archives = list((resolved_root / ".agent-flow" / "archive" / "team").glob("feature-team-*"))
            self.assertEqual(len(archives), 1)
            self.assertTrue((archives[0] / "tasks" / "task-1.json").is_file())
            manifest = json.loads((archives[0] / "archive.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["name"], "feature-team")
            self.assertEqual(manifest["reason"], "done")
            self.assertEqual(manifest["source_path"], str(team_root))
            self.assertEqual(manifest["archive_path"], str(archives[0]))
            self.assertTrue(manifest["archived_at"])

    def test_team_archive_removes_team_from_active_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            self.assertEqual(
                main(["team", "archive", "--root", str(root), "--team", "feature-team", "--reason", "done"]),
                0,
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["team", "list", "--root", str(root)]), 0)
            self.assertEqual(output.getvalue(), "")

    def test_team_archive_refuses_existing_archive_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            archive_root = root / ".agent-flow" / "archive" / "team"
            archive_root.mkdir(parents=True)
            archive_dir = archive_root / "feature-team-20260505T062604000000Z"
            archive_dir.mkdir()

            with mock.patch("agent_flow.core.team._now", return_value="2026-05-05T06:26:04+00:00"):
                with self.assertRaises(FileExistsError):
                    main(["team", "archive", "--root", str(root), "--team", "feature-team"])
            self.assertTrue((root / ".agent-flow" / "state" / "team" / "feature-team").is_dir())
            self.assertEqual(list(archive_dir.iterdir()), [])

    def test_team_archive_manifest_write_failure_keeps_active_team(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)

            from agent_flow.core import team as team_core

            original_write_json = team_core._write_json

            def fail_archive_manifest(path: Path, payload: object) -> None:
                if path.name == "archive.json":
                    raise OSError("manifest failed")
                original_write_json(path, payload)

            with mock.patch("agent_flow.core.team._write_json", side_effect=fail_archive_manifest):
                with self.assertRaises(OSError):
                    main(["team", "archive", "--root", str(root), "--team", "feature-team"])
            team_root = root.resolve() / ".agent-flow" / "state" / "team" / "feature-team"
            self.assertTrue(team_root.is_dir())
            self.assertEqual(list((root.resolve() / ".agent-flow" / "archive" / "team").glob("feature-team-*")), [])

    def test_team_archive_rename_failure_removes_active_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            team_root = root.resolve() / ".agent-flow" / "state" / "team" / "feature-team"
            original_rename = Path.rename

            def fail_archive_rename(self: Path, target: Path) -> Path:
                if self == team_root and target.name.startswith("feature-team-"):
                    raise OSError("rename failed")
                return original_rename(self, target)

            with mock.patch("pathlib.Path.rename", fail_archive_rename):
                with self.assertRaises(OSError):
                    main(["team", "archive", "--root", str(root), "--team", "feature-team"])
            self.assertTrue(team_root.is_dir())
            self.assertFalse((team_root / "archive.json").exists())
            self.assertEqual(list((root.resolve() / ".agent-flow" / "archive" / "team").glob("feature-team-*")), [])

    def test_team_archive_list_reports_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            with mock.patch("agent_flow.core.team._now", return_value="2026-05-05T06:26:04+00:00"):
                self.assertEqual(
                    main(
                        [
                            "team",
                            "archive",
                            "--root",
                            str(root),
                            "--team",
                            "feature-team",
                            "--reason",
                            "done",
                        ]
                    ),
                    0,
                )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["team", "archive-list", "--root", str(root)]), 0)
            line = output.getvalue().strip()
            self.assertIn("feature-team archived_at=2026-05-05T06:26:04+00:00 reason=done", line)
            self.assertIn("/.agent-flow/archive/team/feature-team-20260505T062604000000Z", line)

    def test_team_archive_list_is_empty_without_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["team", "archive-list", "--root", temp_dir]), 0)
            self.assertEqual(output.getvalue(), "")

    def test_team_archive_export_matches_active_export_before_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            active_output = io.StringIO()
            with contextlib.redirect_stdout(active_output):
                self.assertEqual(
                    main(["team", "export", "--root", str(root), "--team", "feature-team"]),
                    0,
                )
            self.assertEqual(
                main(["team", "archive", "--root", str(root), "--team", "feature-team", "--reason", "done"]),
                0,
            )
            archive_path = next((root.resolve() / ".agent-flow" / "archive" / "team").glob("feature-team-*"))

            archive_output = io.StringIO()
            with contextlib.redirect_stdout(archive_output):
                self.assertEqual(main(["team", "archive-export", "--archive-path", str(archive_path)]), 0)
            self.assertEqual(json.loads(archive_output.getvalue()), json.loads(active_output.getvalue()))

    def test_team_archive_export_requires_archive_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / ".agent-flow" / "archive" / "team" / "missing-manifest"
            archive_path.mkdir(parents=True)
            with self.assertRaises(FileNotFoundError):
                main(["team", "archive-export", "--archive-path", str(archive_path)])

    def test_team_archive_restore_moves_archive_back_to_active_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            active_output = io.StringIO()
            with contextlib.redirect_stdout(active_output):
                self.assertEqual(
                    main(["team", "export", "--root", str(root), "--team", "feature-team"]),
                    0,
                )
            self.assertEqual(
                main(["team", "archive", "--root", str(root), "--team", "feature-team", "--reason", "done"]),
                0,
            )
            archive_path = next((root.resolve() / ".agent-flow" / "archive" / "team").glob("feature-team-*"))

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["team", "archive-restore", "--root", str(root), "--archive-path", str(archive_path)]),
                    0,
                )
            self.assertIn("feature-team restored", output.getvalue())
            self.assertFalse(archive_path.exists())
            self.assertFalse((root.resolve() / ".agent-flow" / "state" / "team" / "feature-team" / "archive.json").exists())
            restored_output = io.StringIO()
            with contextlib.redirect_stdout(restored_output):
                self.assertEqual(
                    main(["team", "export", "--root", str(root), "--team", "feature-team"]),
                    0,
                )
            self.assertEqual(json.loads(restored_output.getvalue()), json.loads(active_output.getvalue()))

    def test_team_archive_restore_refuses_existing_active_team(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            self.assertEqual(
                main(["team", "archive", "--root", str(root), "--team", "feature-team", "--reason", "done"]),
                0,
            )
            archive_path = next((root.resolve() / ".agent-flow" / "archive" / "team").glob("feature-team-*"))
            _create_team_with_task_and_worker(root)

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["team", "archive-restore", "--root", str(root), "--archive-path", str(archive_path)]),
                    1,
                )
            self.assertIn("cannot restore team archive: team already exists: feature-team", output.getvalue())
            self.assertTrue(archive_path.exists())

    def test_team_archive_restore_writes_success_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            self.assertEqual(
                main(["team", "archive", "--root", str(root), "--team", "feature-team", "--reason", "done"]),
                0,
            )
            archive_path = next((root.resolve() / ".agent-flow" / "archive" / "team").glob("feature-team-*"))
            report_path = root / "reports" / "restore.json"

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "team",
                            "archive-restore",
                            "--root",
                            str(root),
                            "--archive-path",
                            str(archive_path),
                            "--report",
                            str(report_path),
                        ]
                    ),
                    0,
                )
            self.assertIn("feature-team restored", output.getvalue())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(report["valid"])
            self.assertEqual(report["team"], "feature-team")
            self.assertEqual(report["reason"], "done")

    def test_team_archive_restore_writes_failure_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / ".agent-flow" / "archive" / "team" / "missing"
            report_path = root / "reports" / "restore.json"

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "team",
                            "archive-restore",
                            "--root",
                            str(root),
                            "--archive-path",
                            str(archive_path),
                            "--report",
                            str(report_path),
                        ]
                    ),
                    1,
                )
            self.assertIn("cannot restore team archive:", output.getvalue())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(report["valid"])
            self.assertIn("cannot restore team archive:", report["errors"][0])

    def test_team_archive_restore_recovers_manifest_when_rename_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            self.assertEqual(
                main(["team", "archive", "--root", str(root), "--team", "feature-team", "--reason", "done"]),
                0,
            )
            archive_path = next((root.resolve() / ".agent-flow" / "archive" / "team").glob("feature-team-*"))
            original_manifest = json.loads((archive_path / "archive.json").read_text(encoding="utf-8"))

            original_rename = Path.rename

            def fail_archive_restore_rename(self: Path, target: Path) -> Path:
                if self == archive_path and target.name == "feature-team":
                    raise OSError("rename failed")
                return original_rename(self, target)

            with mock.patch("pathlib.Path.rename", fail_archive_restore_rename):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(
                        main(["team", "archive-restore", "--root", str(root), "--archive-path", str(archive_path)]),
                        1,
                    )
            self.assertIn("cannot restore team archive: rename failed", output.getvalue())

            self.assertTrue(archive_path.is_dir())
            restored_manifest = json.loads((archive_path / "archive.json").read_text(encoding="utf-8"))
            self.assertEqual(restored_manifest, original_manifest)
            self.assertFalse((root.resolve() / ".agent-flow" / "state" / "team" / "feature-team").exists())

    def test_team_import_validate_accepts_export_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            export_output = io.StringIO()
            with contextlib.redirect_stdout(export_output):
                self.assertEqual(
                    main(["team", "export", "--root", str(root), "--team", "feature-team"]),
                    0,
                )
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(export_output.getvalue(), encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["team", "import-validate", "--file", str(snapshot_path)]), 0)
            self.assertEqual(output.getvalue().strip(), "OK")

    def test_team_import_apply_rejects_noncanonical_team_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "team": {"name": "Feature Team", "description": "", "created_at": "now"},
                        "tasks": [],
                        "workers": [],
                        "heartbeats": [],
                        "mailboxes": {},
                        "shutdowns": [],
                    }
                ),
                encoding="utf-8",
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["team", "import-apply", "--root", str(root), "--file", str(snapshot_path)]),
                    1,
                )
            self.assertIn("team.name must be canonical: feature-team", output.getvalue())
            self.assertFalse((root / ".agent-flow" / "state" / "team" / "feature-team").exists())

    def test_team_import_validate_rejects_bad_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "team": {"name": "feature-team", "description": "", "created_at": "now"},
                        "tasks": [
                            {
                                "task_id": "task-1",
                                "subject": "s",
                                "description": "d",
                                "status": "unknown",
                                "owner": "missing-owner",
                            },
                            {"task_id": "task-2", "subject": "s", "description": "d", "status": ["bad"]},
                        ],
                        "workers": [{"name": "worker-1", "role": "implementer", "status": "idle"}],
                        "heartbeats": [
                            {"worker": "missing", "status": "idle", "alive": True, "updated_at": "now"}
                        ],
                        "mailboxes": {
                            "worker-1": [
                                {
                                    "message_id": "m1",
                                    "from_actor": "lead",
                                    "to_worker": "other-worker",
                                    "body": "b",
                                    "created_at": "now",
                                    "read": False,
                                }
                            ]
                        },
                        "shutdowns": [
                            {
                                "signal_id": "../bad",
                                "worker": "worker-1",
                                "reason": "r",
                                "requested_at": "now",
                                "acknowledged": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["team", "import-validate", "--file", str(snapshot_path)]), 1)
            errors = output.getvalue()
            self.assertIn("invalid task status: unknown", errors)
            self.assertIn("tasks[1].status must be a string", errors)
            self.assertIn("task owner references unknown worker: missing-owner", errors)
            self.assertIn("heartbeat references unknown worker: missing", errors)
            self.assertIn("mailbox message worker mismatch: worker-1 != other-worker", errors)
            self.assertIn("shutdowns[0].signal_id is unsafe: ../bad", errors)

    def test_team_import_validate_rejects_bad_schema_types(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "team": {"name": "feature-team", "description": 1, "created_at": None},
                        "tasks": [{"task_id": "task-1", "subject": 1, "description": None}],
                        "workers": [{"name": "worker-1", "role": None, "status": 2}],
                        "heartbeats": [{"worker": "worker-1", "status": None, "alive": "yes", "updated_at": 3}],
                        "mailboxes": {
                            "worker-1": [
                                {
                                    "message_id": 1,
                                    "from_actor": None,
                                    "to_worker": "worker-1",
                                    "body": 3,
                                    "created_at": None,
                                    "read": "no",
                                }
                            ]
                        },
                        "shutdowns": [
                            {
                                "signal_id": "a" * 32,
                                "worker": "worker-1",
                                "reason": 1,
                                "requested_at": None,
                                "acknowledged": "no",
                                "acknowledged_at": 5,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["team", "import-validate", "--file", str(snapshot_path)]), 1)
            errors = output.getvalue()
            self.assertIn("team.description must be a string", errors)
            self.assertIn("tasks[0].subject must be a string", errors)
            self.assertIn("workers[0].role must be a string", errors)
            self.assertIn("heartbeats[0].alive must be a boolean", errors)
            self.assertIn("mailboxes.worker-1[0].read must be a boolean", errors)
            self.assertIn("shutdowns[0].acknowledged_at must be a string or null", errors)

    def test_team_import_validate_rejects_incomplete_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(json.dumps({"team": {"name": "feature-team"}}), encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["team", "import-validate", "--file", str(snapshot_path)]), 1)
            errors = output.getvalue()
            self.assertIn("tasks is required", errors)
            self.assertIn("workers is required", errors)
            self.assertIn("mailboxes is required", errors)

    def test_team_import_validate_reports_bad_input_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            malformed_path = root / "bad.json"
            malformed_path.write_text("{", encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["team", "import-validate", "--file", str(malformed_path)]), 1)
            self.assertIn("invalid JSON:", output.getvalue())

            missing_output = io.StringIO()
            with contextlib.redirect_stdout(missing_output):
                self.assertEqual(main(["team", "import-validate", "--file", str(root / "missing.json")]), 1)
            self.assertIn("cannot read import file:", missing_output.getvalue())

    def test_team_import_dry_run_summarizes_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            main(
                [
                    "team",
                    "message",
                    "--root",
                    str(root),
                    "--team",
                    "feature-team",
                    "--from-actor",
                    "lead",
                    "--to-worker",
                    "worker-1",
                    "--body",
                    "dry run",
                ]
            )
            main(
                [
                    "team",
                    "shutdown",
                    "--root",
                    str(root),
                    "--team",
                    "feature-team",
                    "--worker",
                    "worker-1",
                    "--reason",
                    "dry run",
                ]
            )
            export_output = io.StringIO()
            with contextlib.redirect_stdout(export_output):
                self.assertEqual(
                    main(["team", "export", "--root", str(root), "--team", "feature-team"]),
                    0,
                )
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(export_output.getvalue(), encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["team", "import-dry-run", "--file", str(snapshot_path)]), 0)
            self.assertEqual(
                output.getvalue().strip(),
                "feature-team tasks=1 workers=1 heartbeats=1 mailboxes=1 messages=1 shutdowns=1",
            )

    def test_team_import_dry_run_writes_success_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            export_output = io.StringIO()
            with contextlib.redirect_stdout(export_output):
                self.assertEqual(
                    main(["team", "export", "--root", str(root), "--team", "feature-team"]),
                    0,
                )
            snapshot_path = root / "snapshot.json"
            report_path = root / "reports" / "import.json"
            snapshot_path.write_text(export_output.getvalue(), encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "team",
                            "import-dry-run",
                            "--file",
                            str(snapshot_path),
                            "--report",
                            str(report_path),
                        ]
                    ),
                    0,
                )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(report["valid"])
            self.assertEqual(report["team"], "feature-team")
            self.assertEqual(report["task_count"], 1)

    def test_team_import_dry_run_reports_validation_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            snapshot_path = root / "snapshot.json"
            report_path = root / "import-report.json"
            snapshot_path.write_text(json.dumps({"team": {"name": "feature-team"}}), encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "team",
                            "import-dry-run",
                            "--file",
                            str(snapshot_path),
                            "--report",
                            str(report_path),
                        ]
                    ),
                    1,
                )
            self.assertIn("tasks is required", output.getvalue())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(report["valid"])
            self.assertIn("tasks is required", report["errors"])

    def test_team_import_dry_run_writes_bad_file_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_path = root / "import-report.json"

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "team",
                            "import-dry-run",
                            "--file",
                            str(root / "missing.json"),
                            "--report",
                            str(report_path),
                        ]
                    ),
                    1,
                )
            self.assertIn("cannot read import file:", output.getvalue())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(report["valid"])
            self.assertIn("cannot read import file:", report["errors"][0])

    def test_team_import_apply_creates_new_team_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            _create_team_with_task_and_worker(source_root)
            main(
                [
                    "team",
                    "message",
                    "--root",
                    str(source_root),
                    "--team",
                    "feature-team",
                    "--from-actor",
                    "lead",
                    "--to-worker",
                    "worker-1",
                    "--body",
                    "import me",
                ]
            )
            export_output = io.StringIO()
            with contextlib.redirect_stdout(export_output):
                self.assertEqual(
                    main(["team", "export", "--root", str(source_root), "--team", "feature-team"]),
                    0,
                )
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(export_output.getvalue(), encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["team", "import-apply", "--root", str(target_root), "--file", str(snapshot_path)]),
                    0,
                )
            self.assertEqual(output.getvalue().strip(), "feature-team imported")

            imported_output = io.StringIO()
            with contextlib.redirect_stdout(imported_output):
                self.assertEqual(
                    main(["team", "export", "--root", str(target_root), "--team", "feature-team"]),
                    0,
                )
            self.assertEqual(json.loads(imported_output.getvalue()), json.loads(export_output.getvalue()))

    def test_team_import_apply_refuses_existing_team(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            export_output = io.StringIO()
            with contextlib.redirect_stdout(export_output):
                self.assertEqual(
                    main(["team", "export", "--root", str(root), "--team", "feature-team"]),
                    0,
                )
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(export_output.getvalue(), encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["team", "import-apply", "--root", str(root), "--file", str(snapshot_path)]),
                    1,
                )
            self.assertIn("team already exists: feature-team", output.getvalue())

    def test_team_import_apply_writes_conflict_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            export_output = io.StringIO()
            with contextlib.redirect_stdout(export_output):
                self.assertEqual(
                    main(["team", "export", "--root", str(root), "--team", "feature-team"]),
                    0,
                )
            snapshot_path = root / "snapshot.json"
            report_path = root / "reports" / "apply.json"
            snapshot_path.write_text(export_output.getvalue(), encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "team",
                            "import-apply",
                            "--root",
                            str(root),
                            "--file",
                            str(snapshot_path),
                            "--report",
                            str(report_path),
                        ]
                    ),
                    1,
                )
            self.assertIn("team already exists: feature-team", output.getvalue())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(report["valid"])
            self.assertIn("team already exists: feature-team", report["errors"])

    def test_team_import_apply_writes_success_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            _create_team_with_task_and_worker(source_root)
            export_output = io.StringIO()
            with contextlib.redirect_stdout(export_output):
                self.assertEqual(
                    main(["team", "export", "--root", str(source_root), "--team", "feature-team"]),
                    0,
                )
            snapshot_path = root / "snapshot.json"
            report_path = root / "reports" / "apply.json"
            snapshot_path.write_text(export_output.getvalue(), encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "team",
                            "import-apply",
                            "--root",
                            str(target_root),
                            "--file",
                            str(snapshot_path),
                            "--report",
                            str(report_path),
                        ]
                    ),
                    0,
                )
            self.assertEqual(output.getvalue().strip(), "feature-team imported")
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(report["valid"])
            self.assertEqual(report["team"], "feature-team")
            self.assertEqual(report["task_count"], 1)

    def test_team_import_apply_rejects_invalid_snapshot_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(json.dumps({"team": {"name": "feature-team"}}), encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["team", "import-apply", "--root", str(root), "--file", str(snapshot_path)]),
                    1,
                )
            self.assertIn("tasks is required", output.getvalue())
            self.assertFalse((root / ".agent-flow" / "state" / "team" / "feature-team").exists())

    def test_team_import_apply_rejects_duplicate_file_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            _create_team_with_task_and_worker(source_root)
            signal_output = io.StringIO()
            with contextlib.redirect_stdout(signal_output):
                self.assertEqual(
                    main(
                        [
                            "team",
                            "shutdown",
                            "--root",
                            str(source_root),
                            "--team",
                            "feature-team",
                            "--worker",
                            "worker-1",
                            "--reason",
                            "done",
                        ]
                    ),
                    0,
                )
            export_output = io.StringIO()
            with contextlib.redirect_stdout(export_output):
                self.assertEqual(
                    main(["team", "export", "--root", str(source_root), "--team", "feature-team"]),
                    0,
                )
            snapshot = json.loads(export_output.getvalue())
            snapshot["heartbeats"].append(dict(snapshot["heartbeats"][0]))
            snapshot["shutdowns"].append(dict(snapshot["shutdowns"][0]))
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["team", "import-apply", "--root", str(target_root), "--file", str(snapshot_path)]),
                    1,
                )
            self.assertIn("duplicate heartbeat worker: worker-1", output.getvalue())
            self.assertIn("duplicate shutdown signal: worker-1/", output.getvalue())
            self.assertFalse((target_root / ".agent-flow" / "state" / "team" / "feature-team").exists())

    def test_team_import_apply_rejects_duplicate_normalized_mailboxes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            _create_team_with_task_and_worker(source_root)
            export_output = io.StringIO()
            with contextlib.redirect_stdout(export_output):
                self.assertEqual(
                    main(["team", "export", "--root", str(source_root), "--team", "feature-team"]),
                    0,
                )
            snapshot = json.loads(export_output.getvalue())
            snapshot["mailboxes"][" worker-1 "] = []
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["team", "import-apply", "--root", str(target_root), "--file", str(snapshot_path)]),
                    1,
                )
            self.assertIn("duplicate mailbox worker: worker-1", output.getvalue())
            self.assertFalse((target_root / ".agent-flow" / "state" / "team" / "feature-team").exists())

    def test_team_import_apply_reports_internal_write_failure_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            _create_team_with_task_and_worker(source_root)
            export_output = io.StringIO()
            with contextlib.redirect_stdout(export_output):
                self.assertEqual(
                    main(["team", "export", "--root", str(source_root), "--team", "feature-team"]),
                    0,
                )
            snapshot_path = root / "snapshot.json"
            report_path = root / "reports" / "apply.json"
            snapshot_path.write_text(export_output.getvalue(), encoding="utf-8")

            from agent_flow.core import team as team_core

            original_write_json = team_core._write_json

            def fail_worker_write(path: Path, payload: object) -> None:
                if path.name == "identity.json":
                    raise OSError("disk full")
                original_write_json(path, payload)

            output = io.StringIO()
            with mock.patch("agent_flow.core.team._write_json", side_effect=fail_worker_write):
                with contextlib.redirect_stdout(output):
                    self.assertEqual(
                        main(
                            [
                                "team",
                                "import-apply",
                                "--root",
                                str(target_root),
                                "--file",
                                str(snapshot_path),
                                "--report",
                                str(report_path),
                            ]
                        ),
                        1,
                    )
            self.assertIn("cannot apply team import: disk full", output.getvalue())
            self.assertFalse((target_root / ".agent-flow" / "state" / "team" / "feature-team").exists())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(report["valid"])
            self.assertIn("cannot apply team import: disk full", report["errors"])

    def test_team_import_apply_reports_cleanup_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            target_root = root / "target"
            _create_team_with_task_and_worker(source_root)
            export_output = io.StringIO()
            with contextlib.redirect_stdout(export_output):
                self.assertEqual(
                    main(["team", "export", "--root", str(source_root), "--team", "feature-team"]),
                    0,
                )
            snapshot_path = root / "snapshot.json"
            report_path = root / "reports" / "apply.json"
            snapshot_path.write_text(export_output.getvalue(), encoding="utf-8")

            from agent_flow.core import team as team_core

            original_write_json = team_core._write_json

            def fail_worker_write(path: Path, payload: object) -> None:
                if path.name == "identity.json":
                    raise OSError("disk full")
                original_write_json(path, payload)

            output = io.StringIO()
            with mock.patch("agent_flow.core.team._write_json", side_effect=fail_worker_write):
                with mock.patch("agent_flow.core.team.shutil.rmtree", side_effect=OSError("permission denied")):
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(
                            main(
                                [
                                    "team",
                                    "import-apply",
                                    "--root",
                                    str(target_root),
                                    "--file",
                                    str(snapshot_path),
                                    "--report",
                                    str(report_path),
                                ]
                            ),
                            1,
                        )
            self.assertIn("cannot apply team import: disk full", output.getvalue())
            self.assertIn("cannot clean failed team import: permission denied", output.getvalue())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(report["valid"])
            self.assertIn("cannot apply team import: disk full", report["errors"])
            self.assertIn("cannot clean failed team import: permission denied", report["errors"])

    def test_team_import_dry_run_reports_report_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            export_output = io.StringIO()
            with contextlib.redirect_stdout(export_output):
                self.assertEqual(
                    main(["team", "export", "--root", str(root), "--team", "feature-team"]),
                    0,
                )
            snapshot_path = root / "snapshot.json"
            report_path = root / "report-dir"
            snapshot_path.write_text(export_output.getvalue(), encoding="utf-8")
            report_path.mkdir()

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "team",
                            "import-dry-run",
                            "--file",
                            str(snapshot_path),
                            "--report",
                            str(report_path),
                        ]
                    ),
                    1,
                )
            self.assertIn("cannot write import report:", output.getvalue())

    def test_team_heartbeat_can_mark_worker_dead(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            self.assertEqual(
                main(
                    [
                        "team",
                        "heartbeat",
                        "--root",
                        str(root),
                        "--team",
                        "feature-team",
                        "--worker",
                        "worker-1",
                        "--status",
                        "stopped",
                        "--dead",
                    ]
                ),
                0,
            )
            heartbeat = _read_heartbeat_json(root)
            self.assertEqual(heartbeat["status"], "stopped")
            self.assertFalse(heartbeat["alive"])

    def test_team_heartbeat_requires_registered_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            main(["team", "init", "--root", str(root), "--name", "feature-team"])
            with self.assertRaises(FileNotFoundError):
                main(
                    [
                        "team",
                        "heartbeat",
                        "--root",
                        str(root),
                        "--team",
                        "feature-team",
                        "--worker",
                        "missing",
                        "--status",
                        "running",
                    ]
                )

    def test_team_shutdown_request_and_acknowledge(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "team",
                            "shutdown",
                            "--root",
                            str(root),
                            "--team",
                            "feature-team",
                            "--worker",
                            "worker-1",
                            "--reason",
                            "slice complete",
                        ]
                    ),
                    0,
                )
            signal_id = output.getvalue().strip().split()[0]
            signal = _read_shutdown_json(root, signal_id)
            self.assertEqual(signal["signal_id"], signal_id)
            self.assertEqual(signal["reason"], "slice complete")
            self.assertFalse(signal["acknowledged"])

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "team",
                            "ack-shutdown",
                            "--root",
                            str(root),
                            "--team",
                            "feature-team",
                            "--worker",
                            "worker-1",
                            "--signal",
                            signal_id,
                        ]
                    ),
                    0,
                )
            self.assertIn("acknowledged", output.getvalue())
            signal = _read_shutdown_json(root, signal_id)
            self.assertTrue(signal["acknowledged"])
            self.assertIsNotNone(signal["acknowledged_at"])

    def test_team_shutdown_keeps_multiple_signal_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            signal_ids = []
            for reason in ["first", "second"]:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(
                        main(
                            [
                                "team",
                                "shutdown",
                                "--root",
                                str(root),
                                "--team",
                                "feature-team",
                                "--worker",
                                "worker-1",
                                "--reason",
                                reason,
                            ]
                        ),
                        0,
                    )
                signal_ids.append(output.getvalue().strip().split()[0])

            self.assertNotEqual(signal_ids[0], signal_ids[1])
            self.assertEqual(_read_shutdown_json(root, signal_ids[0])["reason"], "first")
            self.assertEqual(_read_shutdown_json(root, signal_ids[1])["reason"], "second")
            self.assertEqual(
                main(
                    [
                        "team",
                        "ack-shutdown",
                        "--root",
                        str(root),
                        "--team",
                        "feature-team",
                        "--worker",
                        "worker-1",
                        "--signal",
                        signal_ids[0],
                    ]
                ),
                0,
            )
            self.assertTrue(_read_shutdown_json(root, signal_ids[0])["acknowledged"])
            self.assertFalse(_read_shutdown_json(root, signal_ids[1])["acknowledged"])

    def test_team_ack_shutdown_rejects_unsafe_signal_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            with self.assertRaises(ValueError):
                main(
                    [
                        "team",
                        "ack-shutdown",
                        "--root",
                        str(root),
                        "--team",
                        "feature-team",
                        "--worker",
                        "worker-1",
                        "--signal",
                        "../worker-2/bad",
                    ]
                )

    def test_team_ack_shutdown_rejects_worker_mismatch_in_signal_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            signal_id = "a" * 32
            signal_path = (
                root
                / ".agent-flow"
                / "state"
                / "team"
                / "feature-team"
                / "shutdown"
                / "worker-1"
                / f"{signal_id}.json"
            )
            signal_path.parent.mkdir(parents=True, exist_ok=True)
            signal_path.write_text(
                json.dumps(
                    asdict(
                        ShutdownSignal(
                            signal_id=signal_id,
                            worker="../worker-2",
                            reason="bad",
                            requested_at="2026-05-05T00:00:00+00:00",
                        )
                    )
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                main(
                    [
                        "team",
                        "ack-shutdown",
                        "--root",
                        str(root),
                        "--team",
                        "feature-team",
                        "--worker",
                        "worker-1",
                        "--signal",
                        signal_id,
                    ]
                )

    def test_team_shutdown_requires_registered_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            main(["team", "init", "--root", str(root), "--name", "feature-team"])
            with self.assertRaises(FileNotFoundError):
                main(
                    [
                        "team",
                        "shutdown",
                        "--root",
                        str(root),
                        "--team",
                        "feature-team",
                        "--worker",
                        "missing",
                        "--reason",
                        "stop",
                    ]
                )

    def test_team_rejects_unsafe_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertEqual(main(["team", "init", "--root", str(root), "--name", "t"]), 0)
            with self.assertRaises(ValueError):
                main(
                    [
                        "team",
                        "task",
                        "--root",
                        str(root),
                        "--team",
                        "t",
                        "--id",
                        "../bad",
                        "--subject",
                        "bad",
                    ]
                )

    def test_team_task_requires_initialized_team(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(FileNotFoundError):
                main(
                    [
                        "team",
                        "task",
                        "--root",
                        temp_dir,
                        "--team",
                        "missing",
                        "--id",
                        "task-1",
                        "--subject",
                        "Missing team",
                    ]
                )

    def test_team_worker_requires_initialized_team(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(FileNotFoundError):
                main(
                    [
                        "team",
                        "worker",
                        "--root",
                        temp_dir,
                        "--team",
                        "missing",
                        "--name",
                        "worker-1",
                        "--role",
                        "implementer",
                    ]
                )

    def test_team_direct_claim_requires_provider_lifetime_usecase(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                exit_code = main(
                    [
                        "team",
                        "claim",
                        "--root",
                        str(root),
                        "--team",
                        "feature-team",
                        "--task",
                        "task-1",
                        "--worker",
                        "worker-1",
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("team run-next", error.getvalue())
            task = _read_task_json(root)
            self.assertEqual(task["status"], "pending")
            self.assertIsNone(task["claim_token"])

    def test_team_task_rejects_duplicate_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            with self.assertRaises(FileExistsError):
                main(
                    [
                        "team",
                        "task",
                        "--root",
                        str(root),
                        "--team",
                        "feature-team",
                        "--id",
                        "task-1",
                        "--subject",
                        "Overwrite",
                    ]
                )

    def test_team_task_duplicate_create_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            main(["team", "init", "--root", str(root), "--name", "feature-team"])

            def create(subject: str) -> bool:
                try:
                    main(
                        [
                            "team",
                            "task",
                            "--root",
                            str(root),
                            "--team",
                            "feature-team",
                            "--id",
                            "task-1",
                            "--subject",
                            subject,
                        ]
                    )
                    return True
                except FileExistsError:
                    return False

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(create, ["first", "second"]))
            self.assertEqual(results.count(True), 1)
            task = _read_task_json(root)
            self.assertIn(task["subject"], {"first", "second"})

    def test_team_direct_claim_does_not_bypass_worker_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            main(["team", "init", "--root", str(root), "--name", "feature-team"])
            main(
                [
                    "team",
                    "task",
                    "--root",
                    str(root),
                    "--team",
                    "feature-team",
                    "--id",
                    "task-1",
                    "--subject",
                    "Implement login",
                ]
            )
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                exit_code = main(
                    [
                        "team",
                        "claim",
                        "--root",
                        str(root),
                        "--team",
                        "feature-team",
                        "--task",
                        "task-1",
                        "--worker",
                        "missing-worker",
                    ]
                )
            self.assertEqual(exit_code, 2)
            self.assertIn("live provider lease", error.getvalue())

    def test_team_complete_requires_matching_claim_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            task_path = (
                root
                / ".agent-flow"
                / "state"
                / "team"
                / "feature-team"
                / "tasks"
                / "task-1.json"
            )
            task = json.loads(task_path.read_text(encoding="utf-8"))
            task.update(
                {
                    "status": "in_progress",
                    "owner": "worker-1",
                    "claim_token": "expected",
                }
            )
            task_path.write_text(json.dumps(task), encoding="utf-8")
            with self.assertRaises(PermissionError):
                main(
                    [
                        "team",
                        "complete",
                        "--root",
                        str(root),
                        "--team",
                        "feature-team",
                        "--task",
                        "task-1",
                        "--claim-token",
                        "wrong",
                    ]
                )

    def test_team_fail_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            task_path = (
                root
                / ".agent-flow"
                / "state"
                / "team"
                / "feature-team"
                / "tasks"
                / "task-1.json"
            )
            task = json.loads(task_path.read_text(encoding="utf-8"))
            task.update(
                {
                    "status": "in_progress",
                    "owner": "worker-1",
                    "claim_token": "claim",
                }
            )
            task_path.write_text(json.dumps(task), encoding="utf-8")
            self.assertEqual(
                main(
                    [
                        "team",
                        "fail",
                        "--root",
                        str(root),
                        "--team",
                        "feature-team",
                        "--task",
                        "task-1",
                        "--claim-token",
                        "claim",
                        "--result",
                        "blocked",
                    ]
                ),
                0,
            )
            task = _read_task_json(root)
            self.assertEqual(task["status"], "failed")
            self.assertEqual(task["result"], "blocked")

    def test_team_message_list_and_mark_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "team",
                            "message",
                            "--root",
                            str(root),
                            "--team",
                            "feature-team",
                            "--from-actor",
                            "lead",
                            "--to-worker",
                            "worker-1",
                            "--body",
                            "Please check auth tests",
                        ]
                    ),
                    0,
                )
            message_id = output.getvalue().strip().split()[0]
            mailbox = _read_mailbox_json(root)
            self.assertEqual(mailbox[0]["message_id"], message_id)
            self.assertFalse(mailbox[0]["read"])

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "team",
                            "messages",
                            "--root",
                            str(root),
                            "--team",
                            "feature-team",
                            "--worker",
                            "worker-1",
                            "--unread-only",
                        ]
                    ),
                    0,
                )
            self.assertIn("Please check auth tests", output.getvalue())

            self.assertEqual(
                main(
                    [
                        "team",
                        "mark-read",
                        "--root",
                        str(root),
                        "--team",
                        "feature-team",
                        "--worker",
                        "worker-1",
                        "--message",
                        message_id,
                    ]
                ),
                0,
            )
            self.assertTrue(_read_mailbox_json(root)[0]["read"])

    def test_team_message_requires_registered_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            main(["team", "init", "--root", str(root), "--name", "feature-team"])
            with self.assertRaises(FileNotFoundError):
                main(
                    [
                        "team",
                        "message",
                        "--root",
                        str(root),
                        "--team",
                        "feature-team",
                        "--from-actor",
                        "lead",
                        "--to-worker",
                        "missing",
                        "--body",
                        "hello",
                    ]
                )

    def test_team_worker_reregister_preserves_mailbox(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            main(
                [
                    "team",
                    "message",
                    "--root",
                    str(root),
                    "--team",
                    "feature-team",
                    "--from-actor",
                    "lead",
                    "--to-worker",
                    "worker-1",
                    "--body",
                    "keep me",
                ]
            )
            main(
                [
                    "team",
                    "worker",
                    "--root",
                    str(root),
                    "--team",
                    "feature-team",
                    "--name",
                    "worker-1",
                    "--role",
                    "reviewer",
                ]
            )
            self.assertEqual(_read_mailbox_json(root)[0]["body"], "keep me")

    def test_team_message_creates_missing_legacy_mailbox_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            mailbox_path = (
                root
                / ".agent-flow"
                / "state"
                / "team"
                / "feature-team"
                / "mailbox"
                / "worker-1.json"
            )
            mailbox_path.unlink()
            mailbox_path.parent.rmdir()

            self.assertEqual(
                main(
                    [
                        "team",
                        "message",
                        "--root",
                        str(root),
                        "--team",
                        "feature-team",
                        "--from-actor",
                        "lead",
                        "--to-worker",
                        "worker-1",
                        "--body",
                        "legacy mailbox",
                    ]
                ),
                0,
            )
            self.assertEqual(_read_mailbox_json(root)[0]["body"], "legacy mailbox")

    def test_team_message_list_during_writes_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)

            def send(index: int) -> bool:
                main(
                    [
                        "team",
                        "message",
                        "--root",
                        str(root),
                        "--team",
                        "feature-team",
                        "--from-actor",
                        "lead",
                        "--to-worker",
                        "worker-1",
                        "--body",
                        f"msg {index}",
                    ]
                )
                return True

            def list_for_worker(_: int) -> bool:
                main(
                    [
                        "team",
                        "messages",
                        "--root",
                        str(root),
                        "--team",
                        "feature-team",
                        "--worker",
                        "worker-1",
                    ]
                )
                return True

            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(lambda i: send(i) if i % 2 == 0 else list_for_worker(i), range(10)))
            self.assertTrue(all(results))
            self.assertEqual(len(_read_mailbox_json(root)), 5)

    def test_team_direct_claim_never_creates_unleased_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            main(
                [
                    "team",
                    "worker",
                    "--root",
                    str(root),
                    "--team",
                    "feature-team",
                    "--name",
                    "worker-2",
                    "--role",
                    "implementer",
                ]
            )

            def claim(worker: str) -> int:
                with contextlib.redirect_stderr(io.StringIO()):
                    return main(
                        [
                            "team",
                            "claim",
                            "--root",
                            str(root),
                            "--team",
                            "feature-team",
                            "--task",
                            "task-1",
                            "--worker",
                            worker,
                        ]
                    )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(claim, ["worker-1", "worker-2"]))
            self.assertEqual(results, [2, 2])
            task = _read_task_json(root)
            self.assertEqual(task["status"], "pending")
            self.assertIsNone(task["owner"])

    def test_gates_exit_code_fails_when_an_optional_gate_times_out(self) -> None:
        """반증: exit code가 required만 보면 timeout된 검증이 CI에서 성공으로 읽힌다."""
        from agent_flow.core.gates import GateResult

        results = [
            GateResult(
                gate_id="required-ok",
                command=("true",),
                passed=True,
                exit_code=0,
                stdout="ok",
                stderr="",
            ),
            GateResult(
                gate_id="optional-slow",
                command=("pytest", "-q"),
                passed=False,
                exit_code=None,
                stdout="",
                stderr="gate timed out after 600s",
                required=False,
                timed_out=True,
            ),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            (root / ".agent-flow").mkdir(parents=True)
            with mock.patch("agent_flow.cli.run_gates", return_value=results):
                exit_code = main(["gates", "--root", str(root), "--profile", "generic"])
        self.assertEqual(exit_code, 1)

    def test_declared_gate_timeouts_fit_inside_the_relay_budget(self) -> None:
        """불변: 배포 profile이 선언한 gate 상한의 합이 wrapper 예산 안에 들어온다.

        반증: wrapper 예산은 `gateTimeoutSeconds(args) * MAX_TOTAL_GATES`로, gate가
        전부 기본값(600s)이라고 가정한다. `gates[].timeout_s`는 그 가정을 깰 수 있고,
        합이 예산을 넘으면 `safeSpawnSync`가 `write_gate_results` 전에 runner를
        SIGKILL한다 — gate는 다 돌았는데 결과 파일이 없어 cursor가 gates에 남는다.
        JS가 profile YAML을 읽어 예산을 계산하게 만들 수는 없다(parity가 profile
        로직의 JS 재구현을 금지한다). 그래서 여백을 여기서 고정한다.
        """
        from agent_flow.core.gate_plan import profile_gate_commands
        from agent_flow.core.gates import DEFAULT_GATE_TIMEOUT_S

        widest = ["android", "ios", "typescript", "react-native", "node", "flutter"]
        commands = profile_gate_commands(widest, phase="all")
        declared_sum = sum(
            command.timeout_s or DEFAULT_GATE_TIMEOUT_S for command in commands
        )
        # wrapper의 GATE_PLAN_FLOOR_S와 같은 값. 바뀌면 이 테스트가 먼저 깨진다.
        budget = 18_000 + 120
        self.assertLessEqual(len(commands), 24)
        self.assertLessEqual(declared_sum, budget)

    def test_gates_relay_budget_exceeds_the_default_wrapper_timeout(self) -> None:
        """반증: gates에 relay용 30초 상한을 걸면 프로파일 게이트가 끝나기 전에 죽는다.

        이 저장소의 가장 비싼 게이트는 `pytest -q`로 실측 5분대다. 상한이
        살아 있으면 `agent-flow gates`는 어떤 프로젝트에서도 정상 종료할 수 없다.
        실시간으로 기다리는 대신 wrapper가 계산하는 예산을 고정한다.
        """
        node = _node_executable()
        kit = (
            Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"
        ).read_text(encoding="utf-8")
        start = kit.index("const DEFAULT_RELAY_TIMEOUT_MS")
        end = kit.index("\n}\n", kit.index("function relayTimeoutForSubcommand")) + 3
        with tempfile.TemporaryDirectory() as temp_dir:
            probe = Path(temp_dir) / "probe.mjs"
            probe.write_text(
                kit[start:end]
                + "\nconsole.log(JSON.stringify({\n"
                + '  default: DEFAULT_RELAY_TIMEOUT_MS,\n'
                + '  gates: relayTimeoutForSubcommand("gates", []),\n'
                + '  gatesCustom: relayTimeoutForSubcommand("gates", ["--timeout", "900"]),\n'
                + '  gatesEquals: relayTimeoutForSubcommand("gates", ["--timeout=900"]),\n'
                + '  gatesAbbrev: relayTimeoutForSubcommand("gates", ["--time", "900"]),\n'
                + '  gatesJunk: relayTimeoutForSubcommand("gates", ["--timeout", "nope"]),\n'
                + '  gatesZero: relayTimeoutForSubcommand("gates", ["--timeout", "0"]),\n'
                + '  gatesRepeated: relayTimeoutForSubcommand(\n'
                + '    "gates", ["--timeout", "100", "--timeout", "900"]\n'
                + '  ),\n'
                + '  lint: relayTimeoutForSubcommand("architecture-lint", []),\n'
                + '  lintIgnoresTimeout: relayTimeoutForSubcommand(\n'
                + '    "architecture-lint", ["--timeout", "900"]\n'
                + '  ),\n'
                + '  continueCmd: relayTimeoutForSubcommand("continue", []),\n'
                + '  other: relayTimeoutForSubcommand("skills", []),\n'
                + "}));\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, str(probe)),
                text=True,
                capture_output=True,
                timeout=60,
                check=True,
            )
        budgets = json.loads(result.stdout)
        self.assertEqual(budgets["default"], 30_000)
        self.assertEqual(budgets["other"], 30_000)
        # `continue`는 gates phase에서 runner가 프로파일 게이트를 **이 프로세스
        # 안에서** 돌리는 명령이다. 30초로 두면 gradle/xcodebuild 게이트가 첫
        # 하나도 끝나기 전에 SIGKILL되고, 그 시점에는 gate-results.json이 없어
        # cursor가 gates에 남아 다음 advance도 같은 자리에서 죽는다.
        self.assertEqual(budgets["continueCmd"], budgets["gates"])
        # architecture-lint는 profile gate로 돌 때 gate 예산 안에 있다. node로 직접
        # 부를 때만 30초로 잘리면 같은 명령이 호출 경로에 따라 다르게 죽는다.
        # 게이트 하나치 예산이지 gates 전체 예산은 아니다.
        self.assertGreater(budgets["lint"], 30_000)
        self.assertLess(budgets["lint"], budgets["gates"])
        # Python 파서에 architecture-lint용 `--timeout`이 없다. 인자를 훑어 예산을
        # 키우면 CLI가 거부하는 입력에 맞춘 죽은 코드가 된다.
        self.assertEqual(budgets["lintIgnoresTimeout"], budgets["lint"])
        # 가장 비싼 게이트 하나가 5분대다. 전체 예산은 그보다 훨씬 커야 한다.
        self.assertGreater(budgets["gates"], 30 * 60_000)
        # argparse가 받는 세 형태 모두 같은 예산이어야 한다. 하나라도 놓치면
        # 사용자가 올린 상한이 wrapper에 반영되지 않아 #119가 되돌아온다.
        self.assertGreater(budgets["gatesCustom"], budgets["gates"])
        self.assertEqual(budgets["gatesEquals"], budgets["gatesCustom"])
        self.assertEqual(budgets["gatesAbbrev"], budgets["gatesCustom"])
        # 해석 불가/0은 기본 상한으로 떨어진다.
        self.assertEqual(budgets["gatesJunk"], budgets["gates"])
        self.assertEqual(budgets["gatesZero"], budgets["gates"])
        # argparse는 반복 플래그의 마지막 값을 쓴다.
        self.assertEqual(budgets["gatesRepeated"], budgets["gatesCustom"])

    def test_relay_timeout_reports_which_command_exceeded_it(self) -> None:
        """짧은 상한이 걸린 경로는 무엇이 얼마나 걸려 끊겼는지 말해야 한다."""
        node = _node_executable()
        kit = (
            Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"
        ).read_text(encoding="utf-8")
        start = kit.index("const DEFAULT_RELAY_TIMEOUT_MS")
        end = kit.index(
            "\n}\n",
            kit.index("function safeSpawnSync(commandName, args, options = {})"),
        ) + 3
        with tempfile.TemporaryDirectory() as temp_dir:
            probe = Path(temp_dir) / "probe.mjs"
            probe.write_text(
                'import { spawnSync } from "node:child_process";\n'
                + kit[start:end]
                + '\nconst result = safeSpawnSync("sleep", ["5"], { timeout: 200 });\n'
                + "console.log(result.error?.message ?? 'no error');\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, str(probe)),
                text=True,
                capture_output=True,
                timeout=60,
                check=True,
            )
            self.assertIn("sleep", result.stdout)
            self.assertIn("200ms", result.stdout)

    def test_relay_timeout_cannot_be_ignored_by_the_child(self) -> None:
        node = _node_executable()
        kit = (
            Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"
        ).read_text(encoding="utf-8")
        start = kit.index("const DEFAULT_RELAY_TIMEOUT_MS")
        end = kit.index(
            "\n}\n",
            kit.index("function safeSpawnSync(commandName, args, options = {})"),
        ) + 3
        with tempfile.TemporaryDirectory() as temp_dir:
            probe = Path(temp_dir) / "probe.mjs"
            probe.write_text(
                'import { spawnSync } from "node:child_process";\n'
                + kit[start:end]
                + "\nconst started = Date.now();\n"
                + "const result = safeSpawnSync(process.execPath, [\n"
                + '  "-e", "process.on(\\"SIGTERM\\", () => {}); '
                + 'setTimeout(() => {}, 5000)",\n'
                + "], { timeout: 200 });\n"
                + "console.log(JSON.stringify({\n"
                + "  elapsed: Date.now() - started,\n"
                + "  code: result.error?.code ?? null,\n"
                + "}));\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, str(probe)),
                text=True,
                capture_output=True,
                timeout=3,
                check=True,
            )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["code"], "ETIMEDOUT")
        self.assertLess(payload["elapsed"], 2_000)


    def test_gates_run_dir_lands_where_the_runner_reads_it(self) -> None:
        """반증: run-dir을 checkout 기준으로 풀면 runner가 읽지 않는 자리에 결과가 남는다.

        managed worktree run을 소유하는 것은 leader checkout도 worktree checkout도
        아니라 worktree runtime root다. 손으로 만든 run 디렉터리로 검증하면 구현과
        같은 가정을 반복하게 되므로 실제 `run`으로 만든다.
        """
        from agent_flow.core.gates import GateResult
        from agent_flow.core.worktrees import worktree_runtime_root

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            root.mkdir()
            _init_git_repo(root)
            self.assertEqual(main(["run", "slice", "--root", str(root)]), 0)
            checkout = managed_worktrees_root(root) / "feat-slice"
            self.assertTrue((checkout / ".git").exists())
            state_root = worktree_runtime_root(root=root, name="feat-slice")
            # runs/ 에는 run 디렉터리 말고 `active` 마커와 `active.lock` lease 파일도
            # 산다. `iterdir()` 순서는 파일시스템이 정하므로 첫 항목을 집으면
            # APFS에서는 통과하고 CI(ext4)에서는 lease 파일을 run으로 오인한다.
            run_dir = next(
                path
                for path in sorted((state_root / ".agent-flow" / "runs").iterdir())
                if path.is_dir()
            )
            relative = run_dir.relative_to(state_root)

            results = [
                GateResult(
                    gate_id="lint",
                    command=("true",),
                    passed=True,
                    exit_code=0,
                    stdout="ok",
                    stderr="",
                )
            ]
            with mock.patch("agent_flow.cli.run_gates", return_value=results):
                main(
                    [
                        "gates",
                        "--root",
                        str(root),
                        "--profile",
                        "generic",
                        "--worktree",
                        "feat-slice",
                        "--run-dir",
                        str(relative),
                    ]
                )

            self.assertTrue(
                (
                    run_dir
                    / "artifacts"
                    / "gate-results-local-pre-commit.json"
                ).exists()
            )
            self.assertFalse((checkout / ".agent-flow" / "runs").exists())
            self.assertFalse((root / ".agent-flow" / "runs").exists())


def _declare_conditional_gate(root: Path) -> None:
    """실패를 worktree 파일 하나로 켜고 끄는 gate를 leader profile에 선언한다.

    runner가 gate를 직접 돌리므로 손으로 쓴 `gate-results.json`은 덮어써진다. gates
    실패를 시험하려면 **진짜 실패하는 gate**가 있어야 한다. 선언은 leader에 커밋해
    둔다 — run 중 leader를 고치면 phase 경계 tripwire가 drift로 잡는다. 켜고 끄는
    것은 worktree의 `gate-must-fail` 파일이고, gate는 worktree를 cwd로 돌기 때문에
    그 파일 하나가 판정을 뒤집는다.
    """
    declaration = root / ".agent-flow" / "profiles"
    declaration.mkdir(parents=True, exist_ok=True)
    path = declaration / "generic.local.yaml"
    path.write_text(
        "gates:\n"
        "  - id: lint\n"
        '    command: ["sh", "-c", "test ! -f gate-must-fail"]\n'
        "    required: true\n"
        "    phase: pre-commit\n",
        encoding="utf-8",
    )
    subprocess.run(
        ("git", "add", "-f", str(path.relative_to(root))),
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "commit", "-q", "-m", "declare conditional gate"),
        cwd=root,
        check=True,
        capture_output=True,
    )


def _init_git_repo(root: Path) -> None:
    # `git init`의 기본 브랜치는 `init.defaultBranch`에 좌우된다. 이름을 고정하지
    # 않으면 테스트가 실행 머신의 git 설정에 의존한다 — CI(ubuntu, master 기본)에서
    # `worktree add ... main`이 exit 128로 죽었다.
    subprocess.run(("git", "init", "-q", "-b", "main"), cwd=root, check=True)
    subprocess.run(("git", "config", "user.email", "test@example.com"), cwd=root, check=True)
    subprocess.run(("git", "config", "user.name", "Test User"), cwd=root, check=True)
    # run 시작 게이트는 managed hook 등록을 요구한다. 픽스처를 파일마다 손으로
    # 세우면 계약과 갈라지므로, 계약(`MANAGED_HOOK_PLACEMENT`)에서 등록을 짓는
    # 공유 helper 하나만 쓴다. 다만 실제 installer(`agent-flow-kit.mjs install`)를
    # 먼저 돌린 프로젝트에는 진짜 hook과 그 digest가 이미 kit.json에 박혀 있다.
    # 거기에 stub을 덧대면 기록과 내용이 갈라져 게이트가 정상 설치본을 위반으로
    # 읽는다. 설치본이 있으면 그 등록을 그대로 쓴다.
    if not (root / ".agent-flow" / "kit.json").is_file():
        _install_managed_hooks(root)
    # installer가 자기 `.gitignore`를 이미 써 뒀으면 그대로 둔다. 덮으면
    # installer가 무시하도록 적어 둔 `AGENTS.md`/`CLAUDE.md`가 추적 밖으로
    # 떨어져 leader가 dirty로 읽히고, worktree 생성 게이트가 막는다.
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "\n".join((".agent-flow/", ".claude/", ".Codex/", ".codex/", ".omp/")) + "\n",
            encoding="utf-8",
        )
    (root / "README.md").write_text("# test\n", encoding="utf-8")
    # 남는 untracked 파일 하나가 leader를 dirty로 만든다. 초기 커밋은 무시 대상을
    # 뺀 전부를 담는다.
    subprocess.run(("git", "add", "-A"), cwd=root, check=True)
    subprocess.run(("git", "commit", "-q", "-m", "init"), cwd=root, check=True)


def _python_for_package_install() -> str:
    candidates = [
        os.environ.get("AGENT_FLOW_TEST_PYTHON"),
        str(Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "python" / "bin" / "python3"),
        sys.executable,
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            result = subprocess.run(
                (
                    candidate,
                    "-c",
                    "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)",
                ),
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError:
            continue
        if result.returncode == 0:
            return candidate
    raise RuntimeError("Python >= 3.11 is required for package install smoke test")


def _node_executable() -> str:
    candidates = [
        os.environ.get("AGENT_FLOW_TEST_NODE"),
        str(Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node" / "bin" / "node"),
        shutil.which("node"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            result = subprocess.run(
                (candidate, "--version"),
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError:
            continue
        if result.returncode == 0:
            return candidate
    raise RuntimeError("Node.js is required for installer smoke test")


def _create_team_with_task_and_worker(root: Path) -> None:
    main(["team", "init", "--root", str(root), "--name", "feature-team"])
    main(
        [
            "team",
            "task",
            "--root",
            str(root),
            "--team",
            "feature-team",
            "--id",
            "task-1",
            "--subject",
            "Implement login",
        ]
    )
    main(
        [
            "team",
            "worker",
            "--root",
            str(root),
            "--team",
            "feature-team",
            "--name",
            "worker-1",
            "--role",
            "implementer",
        ]
    )


def _approve_worker_for_task(root: Path) -> None:
    main(
        [
            "team",
            "brief",
            "--root",
            str(root),
            "--team",
            "feature-team",
            "--task",
            "task-1",
            "--worker",
            "worker-1",
            "--brief",
            "Use the worker-brief contract.",
            "--write-scope",
            "tasks-only",
        ]
    )
    main(
        [
            "team",
            "approve-worker",
            "--root",
            str(root),
            "--team",
            "feature-team",
            "--task",
            "task-1",
            "--worker",
            "worker-1",
            "--reviewer",
            "lead",
            "--write-scope",
            "tasks-only",
        ]
    )


def _read_task_json(root: Path) -> dict[str, object]:
    return json.loads(
        (
            root
            / ".agent-flow"
            / "state"
            / "team"
            / "feature-team"
            / "tasks"
            / "task-1.json"
        ).read_text(encoding="utf-8")
    )


def _read_mailbox_json(root: Path) -> list[dict[str, object]]:
    return json.loads(
        (
            root
            / ".agent-flow"
            / "state"
            / "team"
            / "feature-team"
            / "mailbox"
            / "worker-1.json"
        ).read_text(encoding="utf-8")
    )


def _read_heartbeat_json(root: Path) -> dict[str, object]:
    return json.loads(
        (
            root
            / ".agent-flow"
            / "state"
            / "team"
            / "feature-team"
            / "workers"
            / "worker-1"
            / "heartbeat.json"
        ).read_text(encoding="utf-8")
    )


def _read_shutdown_json(root: Path, signal_id: str) -> dict[str, object]:
    return json.loads(
        (
            root
            / ".agent-flow"
            / "state"
            / "team"
            / "feature-team"
            / "shutdown"
            / "worker-1"
            / f"{signal_id}.json"
        ).read_text(encoding="utf-8")
    )


def _write_local_skill_index(root: Path, *, include_code_skill: bool = True) -> None:
    skills = [
        {
            "name": "figma-screen-spec",
            "path": ".agent-flow/local-skills/figma-screen-spec/SKILL.md",
            "source": "local",
            "description": "Use when a Figma design link is provided for screen work.",
        },
        {
            "name": "release-first-branch-pr",
            "path": ".agent-flow/local-skills/release-first-branch-pr/SKILL.md",
            "source": "local",
            "description": "Use for git commit, push, PR, branch, worktrees, and cleanup.",
        },
        {
            "name": "pr-review-flow",
            "path": ".agent-flow/local-skills/pr-review-flow/SKILL.md",
            "source": "local",
            "description": "Use during PR code review.",
        },
        {
            "name": "merge-review-flow",
            "path": ".agent-flow/local-skills/merge-review-flow/SKILL.md",
            "source": "local",
            "description": "Use during merge review.",
        },
        {
            "name": "release-branch-review",
            "path": ".agent-flow/local-skills/release-branch-review/SKILL.md",
            "source": "local",
            "description": "Use during release branch review.",
        },
    ]
    if include_code_skill:
        skills.append(
            {
                "name": "samantha-architecture-guide",
                "path": ".agent-flow/local-skills/samantha-architecture-guide/SKILL.md",
                "source": "local",
                "description": "Use before Samantha Android code development or review involving modules.",
            }
        )
    index_path = root / ".agent-flow" / "skills" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps({"skills": skills}), encoding="utf-8")

def _write_local_skill_files(root: Path) -> None:
    skills = {
        "figma-screen-spec": "Use when a Figma design link is provided for screen work.",
        "release-first-branch-pr": "Use for git commit, push, PR, branch, worktrees, and cleanup.",
        "pr-review-flow": "Use during PR code review.",
        "merge-review-flow": "Use during merge review.",
        "release-branch-review": "Use during release branch review.",
        "api": "Use for code review of APIs.",
        "api-contract-guide": "Use for code review of API contracts.",
        "samantha-architecture-guide": "Use before Samantha Android code development or review involving modules. Do not install this skill globally.",
    }
    for name, description in skills.items():
        skill_path = root / ".agent-flow" / "local-skills" / name / "SKILL.md"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
            encoding="utf-8",
        )




@functools.lru_cache(maxsize=None)
def _node_phase_artifact(
    phase_id: str, workflow: str = "full-feature"
) -> Path:
    definition = load_phase_workflow_definition(
        Path(__file__).resolve().parents[1],
        workflow,
    )
    return Path(
        next(phase.artifact for phase in definition.phases if phase.id == phase_id)
    )


def _node_presentation_gate() -> str:
    # n/a 허용 marker는 optional alias도 통과해야 한다.
    return (
        "presentation-skill: optional\n"
        "presentation-state-based-development: optional\n"
        "presentation-state-review: optional\n"
        "ui-state-modeling: optional\n"
        "presentation-mapping-boundary: optional\n"
        "di-boundary: optional\n"
        "shared-presentation-contract-placement: n/a\n"
    )


@functools.lru_cache(maxsize=None)
def _node_declared_skills(phase: str) -> tuple[str, ...]:
    """workflow YAML 선언 + frontmatter가 선언한 의존까지. fixture가 진실 소스를 직접 읽는다.

    선언만 읽으면 skill이 `requires:`로 끌어오는 정본(예: alias → core)이 빠져
    marker가 런타임 요구와 어긋난다.
    """
    from agent_flow.core.phase_workflow import find_kit_root, load_phase_workflow_definition
    from agent_flow.core.skill_resolver import (
        SkillRoot,
        discover_skill_catalog,
        expand_dependencies,
    )

    kit_root = find_kit_root()
    for workflow in ("full-feature", "default", "bugfix", "review", "development"):
        for item in load_phase_workflow_definition(kit_root, workflow).phases:
            if item.id == phase and item.skills:
                root = SkillRoot(
                    source="project",
                    template=str(kit_root / "skills" / "{skill}" / "SKILL.md"),
                )
                catalog = discover_skill_catalog(kit_root, (root,))
                return tuple(expand_dependencies(list(item.skills.required), catalog))
    return ()


def _node_project_local_gate(phase: str = "") -> str:
    required = _node_declared_skills(phase) if phase else ()
    if not required:
        return (
            "skill-availability: n/a\n"
            "skill-use-evidence: unavailable\n"
            "project-local-skills: n/a\n"
            "project-local-skills-used: n/a\n"
        )
    return (
        "skill-availability: pass\n"
        "skill-use-evidence: unavailable\n"
        "project-local-skills: checked\n"
        f"project-local-skills-used: {', '.join(required)}\n"
        "project-local-skill-docs: applied\n"
    )

def _node_project_local_applied_gate() -> str:
    # implement phase가 workflow에서 선언한 skill + local-skills drop-box 전부가 대상이다.
    return (
        "skill-availability: pass\n"
        "skill-use-evidence: unavailable\n"
        "project-local-skills: checked\n"
        "project-local-skills-used: "
        + ", ".join(_node_declared_skills("implement"))
        + ", api, api-contract-guide, figma-screen-spec, merge-review-flow, pr-review-flow,"
        " release-branch-review, release-first-branch-pr, samantha-architecture-guide\n"
        "project-local-skill-docs: applied\n"
    )


def _node_implement_gate(*, local_skill: bool, run_dir=None) -> str:
    if run_dir is not None:
        _record_node_test_evidence(Path(run_dir), exit_code=1)
        _record_node_test_evidence(Path(run_dir), exit_code=0)
    return (
        "## Completion Gate\n"
        "skills_checked: true\n"
        + _node_profile_skill_gate()
        + "clean-architecture: applied\n"
        + (_node_project_local_applied_gate() if local_skill else _node_project_local_gate())
        + _node_presentation_gate()
        + "regression-test: tests/test_x.py::test_bug\n"
        + "red-observed: 1\n"
        + "test-run-evidence: verified\n"
    )



def _node_profile_skill_gate() -> str:
    return (
        "profile-skill-selection: applied\n"
        "active-profiles: generic\n"
        "changed-file-skill-resolution: applied\n"
        "required-profile-skills: checked\n"
        "missing-required-profile-skills: none\n"
    )


def _node_review_parity_gate() -> str:
    return (
        "architecture-contract-check: n/a\n"
        "codex-claude-parity-check: pass\n"
        "hook-parity-check: pass\n"
    )


def _capture_node_spec_source(run_dir, artifact: str) -> None:
    source_path = Path(run_dir) / "artifacts" / "prd.md"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(artifact, encoding="utf-8")
    capture_design_ledger(Path(run_dir), "prd", artifact)


def _record_node_test_evidence(run_dir: Path, *, exit_code: int) -> None:
    # 로그 파일은 leader 설치본 아래 하나로 쌓이지만, runner는 "이 run의 체크아웃
    # 안에서 관측된 명령"만 증거로 받는다. run이 managed worktree로 옮겨가면 두
    # 경로가 갈라지므로, 상대 위치를 세지 말고 설치 규칙(`find_install_root`)과
    # 등록된 worktree 경로(`get_worktree_status`)를 프로덕션에서 그대로 가져온다.
    state_root = run_dir.parents[2]
    project_root = find_install_root(run_dir) or state_root
    checkout = (
        project_root
        if state_root == project_root
        else get_worktree_status(root=project_root, name=state_root.name).path
    )
    evidence_path = project_root / ".agent-flow" / "commands-run.jsonl"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    with evidence_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "command": (
                        "pytest -q "
                        "tests/test_agent_flow.py::test_agent_flow_spec_contract"
                    ),
                    "exit_code": exit_code,
                    "at": datetime.now(timezone.utc).timestamp(),
                    "cwd": str(checkout),
                }
            )
            + "\n"
        )


def _node_spec_gate(run_dir) -> str:
    test_name = "test_agent_flow_spec_contract"
    if run_dir is not None:
        _record_node_test_evidence(Path(run_dir), exit_code=0)
    spec_block = (
        "## Spec Items\n\n"
        "SPEC-1: Complete the requested test workflow.\n"
        f"verify: test:{test_name}\n\n"
    )
    return spec_block


def _write_node_review_results(
    run_dir: Path,
    phase_id: str,
    verdicts: tuple[str, ...] = ("approve", "approve"),
) -> None:
    outcomes = []
    for index, verdict in enumerate(verdicts, start=1):
        provider = "claude" if index % 2 else "codex"
        artifact = run_dir / f"{phase_id}-fixture-{index}.md"
        artifact.write_text(
            "## Reviewer\n"
            "reviewer-source: sub-agent\n"
            f"verdict: {verdict}\n",
            encoding="utf-8",
        )
        outcomes.append(
            {
                "job_id": f"{provider}-fixture-{index}",
                "provider": provider,
                "model": "test-model",
                "effort": "xhigh",
                "status": "ok",
                "verdict": verdict,
                "required": True,
                "artifact": artifact.name,
                "artifact_sha256": hashlib.sha256(
                    artifact.read_bytes()
                ).hexdigest(),
                "prompt_digest": "a" * 16,
                "argv_digest": "b" * 16,
            }
        )
    meta_path = run_dir / "meta.json"
    meta = (
        json.loads(meta_path.read_text(encoding="utf-8"))
        if meta_path.is_file()
        else {"run_id": run_dir.name}
    )
    nonce = meta.get("review_nonce")
    if not isinstance(nonce, str) or len(nonce) != 32:
        nonce = "c" * 32
        meta["review_nonce"] = nonce
    phase_entered_at = meta.get("phase_entered_at")
    if not isinstance(phase_entered_at, str) or not phase_entered_at:
        phase_entered_at = "2026-08-21T00:00:00+00:00"
        meta["phase_entered_at"] = phase_entered_at
    payload = {
        "schema_version": 1,
        "phase_id": phase_id,
        "produced_by": {
            "run_id": str(meta.get("run_id") or run_dir.name),
            "nonce": nonce,
            "phase_entered_at": phase_entered_at,
        },
        "outcomes": outcomes,
    }
    serialized = json.dumps(payload)
    results_path = run_dir / f"{phase_id}-review-results.json"
    results_path.write_text(serialized, encoding="utf-8")
    evidence = meta.get("review_evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    evidence[phase_id] = {
        "schema_version": 1,
        "nonce": nonce,
        "phase_entered_at": phase_entered_at,
        "results_sha256": hashlib.sha256(
            serialized.encode("utf-8")
        ).hexdigest(),
        "observed_job_ids": [outcome["job_id"] for outcome in outcomes],
        "blocking_job_ids": [
            outcome["job_id"] for outcome in outcomes if outcome["required"]
        ],
        "accept_any_provider": False,
        "expected_job_ids_by_provider": {
            outcome["provider"]: [outcome["job_id"]]
            for outcome in outcomes
        },
        "complete_providers": [
            outcome["provider"] for outcome in outcomes
        ],
    }
    meta["review_evidence"] = evidence
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def _node_phase_content(phase: str, prefix: str = "", run_dir=None) -> str:
    content = f"{prefix}{phase}\n"
    if run_dir is not None and phase in {"implement", "fix-loop"}:
        _record_node_test_evidence(Path(run_dir), exit_code=1)
        _record_node_test_evidence(Path(run_dir), exit_code=0)
    if run_dir is not None and phase in {
        "final-review",
        "multi-review",
        "architecture-review",
    }:
        _record_node_test_evidence(Path(run_dir), exit_code=0)
        _write_node_review_results(Path(run_dir), phase)
    skills_gate = (
        "## Completion Gate\n"
        "skills_checked: true\n"
        + _node_profile_skill_gate()
        + _node_project_local_gate(phase)
        + _node_presentation_gate()
    )
    clean_design_gate = (
        "## Clean Architecture Boundary Map\n"
        "## Dependency Rule\n"
        "## Use Case Boundaries\n"
        "## Repository Boundaries\n"
        "## Cache Boundary\n"
        "## Mapping Boundary\n"
        "## Composition Root\n"
        "## Testability Boundary\n"
        "## Completion Gate\n"
        "clean-architecture: applied\n"
        "usecase-interface: n/a\n"
        "usecase-composition: none\n"
        "cache-required: no\n"
        "memory-cache: n/a\n"
        "disk-cache: n/a\n"
        "cache-invalidation-policy: n/a\n"
        "remote-dto-domain-mapper: n/a\n"
        "entity-domain-mapper: n/a\n"
        "domain-ui-mapper: n/a\n"
        "solid-srp-change-reason: n/a\n"
        "solid-ocp-extension-points: n/a\n"
        "solid-lsp-contracts: n/a\n"
        "solid-isp-consumer-ports: n/a\n"
        "solid-dip-dependency-direction: inward\n"
    )
    clean_review_gate = (
        "clean-architecture: applied\n"
        "must-avoid-check: pass\n"
        "dependency-rule: pass\n"
        "usecase-boundary: n/a\n"
        "usecase-calls-usecase: pass\n"
        "repository-boundary: pass\n"
        "cache-boundary: n/a\n"
        "memory-disk-cache-separated: n/a\n"
        "mapping-boundary: n/a\n"
        "dto-entity-domain-ui-separated: pass\n"
        "solid-boundary-check: pass\n"
    )
    clean_code_review_gate = (
        "clean-architecture-review: applied\n"
        "must-avoid-check: pass\n"
        "usecase-interface-check: applied\n"
        "usecase-composition-check: applied\n"
        "cache-boundary-check: applied\n"
        "mapping-boundary-check: applied\n"
        "solid-clean-architecture-check: applied\n"
    )
    if phase == "push-pr":
        return content + (
            "## Completion Gate\n"
            "remote: origin\n"
            "branch: feat/demo\n"
            "remote-oid: 0123456789abcdef0123456789abcdef01234567\n"
            "pr-url: https://example.invalid/pr/1\n"
            "pr-base: main\n"
        )
    if phase == "domain-grill":
        return (
            content
            + "## Completion Gate\n"
            + "domain-grill: complete\n"
            + "shared_understanding: reached\n"
            + "context_docs_checked: true\n"
            + "context_docs_updated: not_needed\n"
        )
    if phase == "prd":
        return (
            content
            + _node_spec_gate(run_dir)
            + "## Design Values\n\n"
            + "## Completion Gate\n"
            + "spec-items: SPEC-1\n"
            + "design-values: none\n"
            + "design-values-confirmed: n/a\n"
        )
    if phase == "comment-authoring":
        return (
            content
            + "## Completion Gate\n"
            + "comment-authoring: applied\n"
            + "comment-checker: checked\n"
            + "comment-scope: final-pass-only\n"
            + "refactor-scope: none\n"
            + "performance-optimization: none\n"
            + "module-split: none\n"
        )
    if phase == "design":
        return (
            content
            + _node_spec_gate(run_dir)
            + "## Design Values\n\n"
            + clean_design_gate
            + "spec-items: SPEC-1\n"
            + "design-values: none\n"
        )
    if phase == "ddd-design":
        return content + "## Design Values\n\n" + clean_design_gate + "design-values: none\n"
    if phase == "multi-review":
        return (
            "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
            "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n"
            "## Overall\n"
            "verdict: approve\n"
            "\n## Completion Gate\n"
            "skills_checked: true\n"
            + _node_profile_skill_gate()
            + _node_review_parity_gate()
            + clean_code_review_gate
            + _node_project_local_gate(phase)
            + _node_presentation_gate()
        )
    if phase == "architecture-review":
        return (
            "## Reviewer A\nreviewer-source: sub-agent\nverdict: approve\n\n"
            "## Reviewer B\nreviewer-source: sub-agent\nverdict: approve\n\n"
            "## Overall\nverdict: approve\n\n"
            + skills_gate
            + _node_review_parity_gate()
            + clean_review_gate
        )
    if phase == "implement":
        return (
            content
            + "## Completion Gate\n"
            + "skills_checked: true\n"
            + _node_profile_skill_gate()
            + "clean-architecture: applied\n"
            + _node_project_local_gate(phase)
            + _node_presentation_gate()
            + "regression-test: tests/test_x.py::test_bug\n"
            + "red-observed: 1\n"
            + "test-run-evidence: verified\n"
        )
    if phase == "fix-loop":
        return (
            content
            + skills_gate
            + "clean-architecture: applied\n"
            + "regression-test: tests/test_x.py::test_bug\n"
            + "red-observed: 1\n"
            + "test-run-evidence: verified\n"
        )
    if phase in {"green", "refactor", "pr-comment-fix", "pr-ci-fix"}:
        return content + skills_gate + "clean-architecture: applied\n"
    if phase == "red":
        if run_dir is not None:
            _record_node_test_evidence(Path(run_dir), exit_code=1)
        return (
            content
            + skills_gate
            + "regression-test: tests/test_x.py::test_bug\n"
            + "red-observed: 1\n"
            + "test-run-evidence: unavailable\n"
        )
    return content


def _with_skills_gate(content: str, phase: str = "multi-review") -> str:
    return (
        f"{content.rstrip()}\n\n## Completion Gate\n"
        "skills_checked: true\n"
        + _node_profile_skill_gate()
        + _node_review_parity_gate()
        + "clean-architecture-review: applied\n"
        + "must-avoid-check: pass\n"
        + _node_project_local_gate(phase)
        + "usecase-interface-check: applied\n"
        "usecase-composition-check: applied\n"
        "cache-boundary-check: applied\n"
        "mapping-boundary-check: applied\n"
        "solid-clean-architecture-check: applied\n"
        + _node_presentation_gate()
    )


def _with_final_review_gate(content: str, dependency_rule: str = "pass", phase: str = "final-review") -> str:
    return (
        f"{content.rstrip()}\n\n## Completion Gate\n"
        "skills_checked: true\n"
        + _node_profile_skill_gate()
        + _node_review_parity_gate()
        + "clean-architecture: applied\n"
        + "must-avoid-check: pass\n"
        + _node_project_local_gate(phase)
        + f"dependency-rule: {dependency_rule}\n"
        "usecase-boundary: n/a\n"
        "usecase-calls-usecase: pass\n"
        "repository-boundary: pass\n"
        "cache-boundary: n/a\n"
        "memory-disk-cache-separated: n/a\n"
        "mapping-boundary: n/a\n"
        "dto-entity-domain-ui-separated: pass\n"
        "solid-boundary-check: pass\n"
        "clean-architecture-review: applied\n"
        "usecase-interface-check: applied\n"
        "usecase-composition-check: applied\n"
        "cache-boundary-check: applied\n"
        "mapping-boundary-check: applied\n"
        "solid-clean-architecture-check: applied\n"
        + _node_presentation_gate()
    )


def _node_phase_run_dir(
    project_root: Path,
    run_id: str = "r1",
    *,
    worktree: str | None = None,
) -> Path:
    state_root = (
        worktree_runtime_root(root=project_root, name=worktree)
        if worktree is not None
        else project_root
    )
    return state_root / ".agent-flow" / "runs" / run_id


def _set_node_phase(
    run_dir: Path,
    phase_id: str,
    *,
    workflow: str = "full-feature",
    **updates: object,
) -> Path:
    definition = load_phase_workflow_definition(
        Path(__file__).resolve().parents[1],
        workflow,
    )
    phase_index, phase = next(
        (index, phase)
        for index, phase in enumerate(definition.phases)
        if phase.id == phase_id
    )
    meta_path = run_dir / "meta.json"
    meta = (
        json.loads(meta_path.read_text(encoding="utf-8"))
        if meta_path.is_file()
        else {
            "run_id": run_dir.name,
            "task": "demo",
            "started_at": "2020-01-01T00:00:00+00:00",
            "checkout_identity": "leader",
        }
    )
    meta.pop("host_phase_leader_baseline", None)
    meta.update(
        {
            "workflow": workflow,
            "phase_index": phase_index,
            "current_phase": phase_id,
            "phase_entered_at": "2020-01-01T00:00:00+00:00",
            **updates,
        }
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    (run_dir / "active").touch()
    if phase.multi_review:
        _write_node_review_results(run_dir, phase_id)
    return run_dir / phase.artifact


def _read_node_phase(run_dir: Path) -> dict[str, object]:
    return json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))


def _node_start_full_feature_at_pr_watch(
    project_root: Path, node: str, cli: str
) -> tuple[Path, Path]:
    # run은 leader가 아니라 managed worktree 안에서 돈다. 뒤따르는 lifecycle
    # 명령을 leader에서 부르면 run을 못 찾으므로 checkout 경로도 함께 돌려준다.
    # 이름은 프로덕션 planner에서 유도한다 — 문자열로 박으면 slug 규칙이 바뀔 때
    # 같은 회귀가 다시 난다.
    _init_git_repo(project_root)
    plan = plan_worktree(root=project_root, name="demo")
    subprocess.run(
        (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
        cwd=project_root,
        check=True,
    )
    run_dir = _node_phase_run_dir(project_root, worktree=plan.name)
    _set_node_phase(run_dir, "pr-watch")
    return run_dir, plan.path


def _node_epoch_seconds(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _write_resolver_skills(temp_dir: str) -> Path:
    """resolver 테스트용 최소 프로젝트. alpha/beta는 선언 없음, catalog는 자기선언 skill이다."""
    root = Path(temp_dir)
    (root / ".agent-flow").mkdir(parents=True, exist_ok=True)
    for name in ("alpha-guide", "beta-guide"):
        skill_dir = root / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Example guide.\n---\n\n# {name}\n",
            encoding="utf-8",
        )
    catalog = root / "skills" / "catalog-guide"
    catalog.mkdir(parents=True, exist_ok=True)
    (catalog / "SKILL.md").write_text(
        "---\n"
        "name: catalog-guide\n"
        "description: Example catalog guide.\n"
        "workflowPhases: [green, refactor]\n"
        "taskTerms: [widget catalog]\n"
        'pathGlobs: ["**/catalog/**"]\n'
        "dependencies: [alpha-guide]\n"
        "---\n\n# catalog-guide\n",
        encoding="utf-8",
    )
    return root


if __name__ == "__main__":
    unittest.main()
