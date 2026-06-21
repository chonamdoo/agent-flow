from __future__ import annotations

import contextlib
import io
import os
import site
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
import subprocess
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from importlib import resources

from agent_flow.cli import main
from agent_flow.adapters.templates import PromptContext, render_stage_prompt
from agent_flow.core.gates import GateCommand, run_gate
from agent_flow.core.profiles import load_profile
from agent_flow.core.review import _parse_verdict
from agent_flow.core.team import ShutdownSignal
from agent_flow.core.workflow import _stage_from_payload
from agent_flow.core.worktrees import plan_worktree, worktree_runtime_root


os.environ.setdefault("AGENT_FLOW_SKIP_CODEX_TRUST", "1")


def _node_test_env(**overrides: str) -> dict[str, str]:
    env = {**os.environ, **overrides}
    python_paths = [
        env.get("PYTHONPATH"),
        site.getusersitepackages(),
    ]
    env["PYTHONPATH"] = os.pathsep.join(path for path in python_paths if path)
    return env


def _write_minimal_context_docs(root: Path) -> None:
    root.joinpath("CONTEXT.md").write_text(
        "# Context\n\n## Current Vocabulary\n\n- Project\n\n## Future Vocabulary\n\n- Worker\n",
        encoding="utf-8",
    )
    context_root = root / ".Codex" / "rules" / "context"
    context_root.mkdir(parents=True, exist_ok=True)
    required = [
        "domain-glossary-full.md",
        "research-context.md",
        "paper-runtime-context.md",
        "agent-flow-context-map.md",
        "context-maintenance.md",
    ]
    records = []
    for name in required:
        rel = f".Codex/rules/context/{name}"
        (context_root / name).write_text(f"# {name}\n\nMinimal context.\n", encoding="utf-8")
        records.append({"id": name, "path": rel, "summary": "Minimal context.", "parent": None})
    tree = root / ".Codex" / "context" / "tree.jsonl"
    tree.parent.mkdir(parents=True, exist_ok=True)
    tree.write_text("".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8")


class CliTest(unittest.TestCase):
    def test_init_creates_agent_flow_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            self.assertEqual(main(["init", "--root", str(root)]), 0)
            self.assertTrue((root / ".agent-flow" / "runs").is_dir())
            self.assertTrue((root / ".agent-flow" / "handoffs").is_dir())

    def test_start_creates_manifest_and_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            self.assertEqual(
                main(["start", "development", "--root", str(root), "--task", "demo", "--adapter", "manual"]),
                0,
            )
            manifests = list((root / ".agent-flow" / "runs").glob("development/*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            run_dir = manifests[0].parent
            self.assertTrue((run_dir / "events.jsonl").is_file())
            self.assertTrue((run_dir / "prompts" / "explore.md").is_file())
            self.assertTrue((run_dir / "prompts" / "review-1.md").is_file())
            self.assertTrue((run_dir / "prompts" / "review-2.md").is_file())
            self.assertTrue((run_dir / "prompts" / "review-3.md").is_file())
            self.assertIn(
                "Adapter:",
                (run_dir / "prompts" / "explore.md").read_text(encoding="utf-8"),
            )

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
            )
            self.assertEqual(init.returncode, 0, init.stderr)
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
                    "--adapter",
                    "manual",
                    "--run-id",
                    "r1",
                ),
                cwd=external_cwd,
                env={**os.environ, "PYTHONPATH": str(install_target)},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            run_dir = project_root / ".agent-flow" / "runs" / "development" / "r1"
            self.assertTrue((project_root / ".agent-flow").is_dir())
            self.assertTrue((run_dir / "manifest.json").is_file())
            self.assertTrue((run_dir / "prompts" / "explore.md").is_file())

    def test_workflow_kit_resources_are_packaged(self) -> None:
        package_root = resources.files("agent_flow")
        self.assertTrue(package_root.joinpath("workflows", "development.yaml").is_file())
        self.assertTrue(package_root.joinpath("profiles", "generic.yaml").is_file())
        self.assertTrue(package_root.joinpath("roles", "default.yaml").is_file())
        self.assertTrue(package_root.joinpath("templates", "generic", "stage.md").is_file())

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
        self.assertIn("Default reviewers are active-host sub-agents", phases["multi-review"]["prompt"])
        self.assertIn("close that sub-agent session", phases["multi-review"]["prompt"])
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
                "clean-architecture: applied",
                "project-local-skills: checked|n/a",
                "project-local-skills-used:",
                "presentation-skill: android|react|react-native|ios|n/a",
                "presentation-state-based-development: applied|n/a",
                "presentation-state-review: pass|n/a",
                "ui-state-modeling: explicit|n/a",
                "presentation-mapping-boundary: domain-to-uimodel|n/a",
                "di-boundary: hilt|context-provider|tsyringe|swift-environment|factory|swift-dependencies|swinject|needle|direct|existing|n/a",
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
        self.assertIn("at least two active-host reviewer sub-agents", default_phases["final-review"]["prompt"])
        self.assertIn("reviewer-source: sub-agent", default_phases["final-review"]["prompt"])
        self.assertIn("close that sub-agent session", default_phases["final-review"]["prompt"])
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

    def test_workflow_export_outputs_normalized_phase_contract(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["workflow", "export", "--workflow", "full-feature"]), 0)
        payload = json.loads(output.getvalue())
        phases = {phase["id"]: phase for phase in payload["phases"]}
        self.assertEqual(payload["id"], "full-feature")
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
        self.assertIn("clean-architecture: applied", phases["green"]["required_markers"])
        self.assertIn("clean-architecture: applied", phases["fix-loop"]["required_markers"])
        self.assertIn("clean-architecture-review: applied", phases["multi-review"]["required_markers"])
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
        self.assertIn("presentation-skill: android|react|react-native|ios|n/a", phases["green"]["required_markers"])
        self.assertNotIn("android-local-skills: checked|n/a", phases["green"]["required_markers"])
        self.assertIn("Android/Chris Banes skills are required only", phases["green"]["prompt"])
        self.assertEqual(phases["gates"]["artifact"], "artifacts/gate-results.json")
        self.assertEqual(phases["gates"]["routes"]["green"], "commit")
        self.assertEqual(phases["comment-authoring"]["routes"]["default"], "multi-review")

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
        from agent_flow.runner import _gates_route_key, _route_key

        # gates 통과 JSON은 실제 command 결과가 있을 때만 green으로 정규화된다.
        self.assertEqual(_gates_route_key('{"passed": true}'), "default")
        self.assertEqual(_gates_route_key('{"passed": true, "results": []}'), "default")
        self.assertEqual(
            _gates_route_key('{"passed": true, "results": [{"command": "npm test", "passed": true, "output": "ok"}]}'),
            "green",
        )
        self.assertEqual(
            _gates_route_key('{"passed": true, "results": [{"command": "npm test", "passed": true, "exit_code": 0}]}'),
            "green",
        )
        self.assertEqual(
            _gates_route_key(
                '{"passed": true, "results": ['
                '{"command": "npm test", "passed": true, "exit_code": 0, "required": true},'
                '{"command": "npm run lint", "passed": false, "stderr": "missing", "required": false}'
                "]}"
            ),
            "green",
        )
        self.assertEqual(
            _gates_route_key('{"passed": true, "status": "approve", "results": [{"command": "npm test", "passed": true, "output": "ok"}]}'),
            "approve",
        )
        self.assertEqual(_gates_route_key('{"passed": false, "results": []}'), "request-changes")
        self.assertEqual(
            _gates_route_key('{"passed": false, "results": [{"id": "lint", "passed": true}]}'),
            "request-changes",
        )
        self.assertEqual(
            _gates_route_key('{"passed": false, "status": "request-changes", "results": []}'),
            "request-changes",
        )
        self.assertEqual(_gates_route_key('{"passed": false, "status": "blocked", "results": []}'), "blocked")
        self.assertEqual(_gates_route_key('{"passed": false, "status": "error", "results": []}'), "error")
        self.assertEqual(_gates_route_key('{"passed": false, "status": "pending", "results": []}'), "pending")
        self.assertEqual(_route_key("status: failed"), "default")
        self.assertEqual(_route_key("status: pass"), "default")
        self.assertEqual(_route_key("- status: green"), "default")
        self.assertEqual(_route_key("note: status: green"), "default")
        self.assertEqual(_route_key("  status: green"), "default")
        self.assertEqual(_route_key("- verdict: approve"), "default")
        self.assertEqual(_route_key("status: green"), "green")
        self.assertEqual(_route_key("status: ci_failed"), "ci_failed")
        self.assertEqual(_route_key("status: has_comments"), "has_comments")
        self.assertEqual(_route_key("status: has-comments"), "default")
        self.assertEqual(_gates_route_key("status: pass"), "default")

    def test_codex_multi_review_requires_one_codex_subagent(self) -> None:
        from agent_flow.adapters.hosted import HostedAdapter, _multi_reviewer_block

        adapter = HostedAdapter("codex")
        self.assertIn("spawn at least two Codex reviewer sub-agents", adapter._hint)
        self.assertIn("reviewer-source: sub-agent", adapter._hint)
        self.assertIn("close that", adapter._hint)

        with mock.patch("agent_flow.adapters.hosted.resolve_review_clis", return_value=[]):
            block = _multi_reviewer_block()
        self.assertIn("Spawn at least two host-native reviewer sub-agents", block)
        self.assertIn("reviewer-source: sub-agent", block)
        self.assertIn("2+ independent sub-agent reviewer verdicts", block)

    def test_optional_reviewer_clis_are_opt_in(self) -> None:
        from agent_flow.multi_review import (
            ReviewerJob,
            distribute,
            residual_host_jobs,
            resolve_review_clis,
        )

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_review_clis(), [])
            distribution = distribute([
                ReviewerJob("generalist", "prompt", Path("generalist.md")),
                ReviewerJob("architecture-design", "prompt", Path("architecture-design.md")),
            ])
            self.assertTrue(distribution.fallback_to_generic)
            self.assertEqual(distribution.by_cli, {})
        with mock.patch.dict(os.environ, {"AGENT_FLOW_REVIEWERS": "codex"}, clear=True):
            self.assertEqual([cli.name for cli in resolve_review_clis()], ["codex"])
        with mock.patch.dict(os.environ, {"AGENT_FLOW_REVIEWERS": "claude,codex"}, clear=True):
            jobs = [
                ReviewerJob("generalist", "prompt", Path("generalist.md")),
                ReviewerJob("architecture-design", "prompt", Path("architecture-design.md")),
            ]
            distribution = distribute(jobs, host="codex")
            host_jobs = residual_host_jobs(distribution)
            assigned_jobs = [job for assigned in distribution.by_cli.values() for job in assigned]
            self.assertGreater(len(host_jobs), 0)
            self.assertTrue({job.angle_id for job in host_jobs}.issubset({job.angle_id for job in jobs}))
            self.assertEqual(len(assigned_jobs), len(jobs))
            self.assertEqual({id(job) for job in assigned_jobs}, {id(job) for job in jobs})
        with mock.patch.dict(os.environ, {"AGENT_FLOW_REVIEWERS": "codex,claude"}, clear=True):
            distribution = distribute([
                ReviewerJob("generalist", "prompt", Path("generalist.md")),
                ReviewerJob("architecture-design", "prompt", Path("architecture-design.md")),
            ], host="codex")
            self.assertTrue(residual_host_jobs(distribution))

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

    def test_python_multi_review_approve_requires_subagent_reviewer(self) -> None:
        from agent_flow.runner import Phase, Runner

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            phase = Phase(
                id="multi-review",
                description="",
                multi_review=True,
                routes={"approve": "architecture-review", "request-changes": "fix-loop"},
            )
            runner = Runner.__new__(Runner)
            runner.run_dir = run_dir
            runner.phases = [
                phase,
                Phase(id="fix-loop", description=""),
                Phase(id="architecture-review", description=""),
            ]

            (run_dir / "multi-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n"
                "## Overall\nverdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (2, False))

            (run_dir / "multi-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: APPROVE\n\n"
                "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: APPROVE\n\n"
                "## Overall\nverdict: APPROVE\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (0, True))

            (run_dir / "multi-review.md").write_text(
                "## Reviewer 1\nreviewer-source: codex sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Reviewer 2\nreviewer-source: claude sub-agent\nreviewer-2 verdict: approve\n\n"
                "## Overall\nverdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (0, True))

            (run_dir / "multi-review.md").write_text(
                "## Reviewer 1\nreviewer-source: non-sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Overall\nverdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (0, True))

            (run_dir / "multi-review.md").write_text(
                "reviewer-1 source: non-sub-agent\n"
                "reviewer-1 verdict: approve\n\n"
                "## Overall\nverdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (0, True))

            (run_dir / "multi-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(runner._next_index(0, phase), (0, True))
            self.assertIn("requires 2+ independent sub-agent reviewer verdicts", output.getvalue())

            (run_dir / "multi-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: request-changes\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (1, False))

            for legacy_status in ("verdict: request-changes\n", "status: failed\n", "status: fail\n"):
                (run_dir / "multi-review.md").write_text(legacy_status, encoding="utf-8")
                self.assertEqual(runner._next_index(0, phase), (0, True))

            (run_dir / "multi-review.md").write_text(
                "## Reviewer 1\n\nreviewer-source: sub-agent\nverdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (0, True))

            (run_dir / "multi-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\n### Findings\nverdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (0, True))

            (run_dir / "multi-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Overall\nstatus: passed\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (0, True))

            (run_dir / "multi-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Overall\nverdict: approve\nverdict: request-changes\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (0, True))

            (run_dir / "multi-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Overall\nverdict: request-changes\n\n## Overall\nverdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (0, True))

            (run_dir / "multi-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Final\nverdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (0, True))

            (run_dir / "multi-review.md").write_text(
                "reviewer verdict: approve\n## Reviewer\nverdict: approve\nverdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (0, True))

            (run_dir / "multi-review.md").write_text(
                "## Reviewer Verdicts\nverdict: approve\n\n"
                "reviewer-1 source: sub-agent\n"
                "reviewer-1 verdict: approve\n\n"
                "## Overall\n"
                "verdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (0, True))

            (run_dir / "multi-review.md").write_text(
                "## Reviewer Notes\nverdict: approve\n\n"
                "reviewer-1 source: sub-agent\n"
                "reviewer-1 verdict: approve\n\n"
                "## Overall\n"
                "verdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (0, True))

            (run_dir / "multi-review.md").write_text(
                "## Reviewer Feedback\nverdict: approve\n\n"
                "reviewer-1 reviewer-source: sub-agent\n"
                "reviewer-1 verdict: approve\n\n"
                "reviewer-2 reviewer-source: sub-agent\n"
                "reviewer-2 verdict: approve\n\n"
                "## Overall\n"
                "verdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (2, False))

            (run_dir / "multi-review.md").write_text(
                "## Reviewer 1\nverdict: lgtm\n\n"
                "## Reviewer 2\nverdict: lgtm\n\n"
                "Overall verdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (0, True))

            (run_dir / "multi-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Reviewer 2\nverdict: lgtm\n\n"
                "## Overall\n"
                "verdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (0, True))

            (run_dir / "multi-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n## Overall\nverdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (2, False))

    def test_python_final_review_approve_requires_subagent_reviewer(self) -> None:
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
            runner.phases = [
                phase,
                Phase(id="fix-loop", description=""),
                Phase(id="commit", description=""),
            ]

            (run_dir / "final-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n"
                "## Overall\nverdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (2, False))

            (run_dir / "final-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: request-changes\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (1, False))

            (run_dir / "final-review.md").write_text(
                "reviewer verdict: approve\n## Reviewer\nverdict: approve\nverdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (0, True))

            (run_dir / "final-review.md").write_text(
                "## Reviewer Verdicts\nverdict: approve\n\n"
                "reviewer-1 reviewer-source: sub-agent\n"
                "reviewer-1 verdict: approve\n\n"
                "reviewer-2 reviewer-source: sub-agent\n"
                "reviewer-2 verdict: approve\n\n"
                "## Overall\n"
                "verdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (2, False))

            (run_dir / "final-review.md").write_text(
                "## Reviewer Notes\nverdict: approve\n\n"
                "reviewer-1 reviewer-source: sub-agent\n"
                "reviewer-1 verdict: approve\n\n"
                "reviewer-2 reviewer-source: sub-agent\n"
                "reviewer-2 verdict: approve\n\n"
                "## Overall\n"
                "verdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (2, False))

            (run_dir / "final-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n## Overall\nverdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (2, False))

    def test_python_architecture_review_requires_two_active_host_reviewers(self) -> None:
        from agent_flow.runner import Phase, Runner

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            phase = Phase(
                id="architecture-review",
                description="",
                multi_review=True,
                routes={"approve": "gates", "request-changes": "refactor"},
            )
            runner = Runner.__new__(Runner)
            runner.run_dir = run_dir
            runner.phases = [
                phase,
                Phase(id="refactor", description=""),
                Phase(id="gates", description=""),
            ]

            (run_dir / "architecture-review.md").write_text(
                "## Reviewer A\nreviewer-source: sub-agent\nverdict: approve\n\n"
                "## Reviewer B\nreviewer-source: sub-agent\nverdict: approve\n\n"
                "## Overall\nverdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (2, False))

            (run_dir / "architecture-review.md").write_text(
                "## Reviewer A\nreviewer-source: sub-agent\nverdict: approve\n\n"
                "## Reviewer B\nreviewer-source: sub-agent\nverdict: request-changes\n\n"
                "## Overall\nverdict: request-changes\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (1, False))

            (run_dir / "architecture-review.md").write_text(
                "## Reviewer A\nreviewer-source: sub-agent\nverdict: approve\n\n"
                "## Overall\nverdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (0, True))

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

            python_wrong = root / "src" / "core" / "wrong" / "thing.py"
            python_wrong.parent.mkdir(parents=True, exist_ok=True)
            python_wrong.write_text("value = 1\n", encoding="utf-8")
            python_wrong_findings = lint_project(root, "python", files=[str(python_wrong.relative_to(root))])
            self.assertIn("path is outside profile architecture role mapping", "\n".join(f.message for f in python_wrong_findings))

            unmanaged = root / "components" / "Button.tsx"
            unmanaged.parent.mkdir(parents=True, exist_ok=True)
            unmanaged.write_text("export function Button() { return null }\n", encoding="utf-8")
            unmanaged_findings = lint_project(root, "nextjs", files=[str(unmanaged.relative_to(root))])
            self.assertIn("path is outside profile architecture role mapping", "\n".join(f.message for f in unmanaged_findings))
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
            self.assertIn(
                "path is outside profile architecture role mapping",
                "\n".join(f.message for findings in partitioned_outside.values() for f in findings),
            )

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
            subprocess.run(("git", "init", "-q"), cwd=root, check=True)
            untracked = root / "core" / "domain" / "chat" / "New.kt"
            untracked.parent.mkdir(parents=True, exist_ok=True)
            untracked.write_text("class New\n", encoding="utf-8")
            self.assertIn("core/domain/chat/New.kt", changed_files(root))

    def test_python_runner_fix_loop_round_cap_blocks_after_three_gate_failures(self) -> None:
        from agent_flow.artifact import read_meta, write_meta
        from agent_flow.runner import Phase, Runner

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            write_meta(run_dir, {})
            runner = Runner.__new__(Runner)
            runner.run_dir = run_dir
            runner.phases = [
                Phase(id="gates", description="", routes={"request-changes": "fix-loop", "green": "multi-review"}),
                Phase(id="fix-loop", description="", routes={"default": "gates"}),
                Phase(id="multi-review", description=""),
            ]
            gates = runner.phases[0]

            for expected_round in (1, 2, 3):
                (run_dir / "gates.md").write_text('{"passed": false, "results": []}', encoding="utf-8")
                self.assertEqual(runner._next_index(0, gates), (1, False))
                self.assertEqual(read_meta(run_dir)["fix_loop_rounds"], expected_round)

            (run_dir / "gates.md").write_text('{"passed": false, "results": []}', encoding="utf-8")
            # gates 실패가 3회를 넘으면 fix-loop로 더 보내지 않고 사용자가 개입하도록 막는다.
            self.assertEqual(runner._next_index(0, gates), (0, True))
            self.assertEqual(read_meta(run_dir)["fix_loop_rounds"], 3)

            (run_dir / "gates.md").write_text('{"passed": true, "results": []}', encoding="utf-8")
            self.assertEqual(runner._next_index(0, gates), (0, True))

            (run_dir / "gates.md").write_text("status: pass\n", encoding="utf-8")
            self.assertEqual(runner._next_index(0, gates), (0, True))

            (run_dir / "gates.md").write_text(
                '{"passed": true, "results": [{"command": "npm test", "passed": true, "output": "ok"}]}',
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, gates), (2, False))
            self.assertNotIn("fix_loop_rounds", read_meta(run_dir))

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

            self.assertEqual(runner._next_index(0, runner.phases[0]), (1, False))

    def test_source_profiles_use_argv_command_lists(self) -> None:
        import yaml

        profiles_root = Path(__file__).resolve().parents[1] / "profiles"
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
            self.assertEqual(result.stdout.strip(), "agent-flow installed profile=generic")
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
            _write_minimal_context_docs(project_root)
            context_result = subprocess.run(
                (node, str(project_root / ".agent-flow" / "scripts" / "check-context-docs.mjs")),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(context_result.returncode, 0, context_result.stderr)
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
            self.assertTrue((project_root / ".agent-flow" / "skills" / "ddd-clean-architecture" / "SKILL.md").is_file())
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
            self.assertFalse((project_root / ".agent-flow" / "skills" / "android-module-creator").exists())
            self.assertFalse((project_root / ".agent-flow" / "skills" / "android-debugging").exists())
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
            self.assertTrue((project_root / ".agent-flow" / "templates" / "generic" / "stage.md").is_file())
            self.assertTrue((project_root / ".Codex" / "agents" / "code-reviewer.md").is_file())
            code_reviewer = (project_root / ".Codex" / "agents" / "code-reviewer.md").read_text(encoding="utf-8")
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
                "skills/clean-architecture/SKILL.md",
                (project_root / ".agent-flow" / "skills" / "ddd-clean-architecture" / "SKILL.md").read_text(
                    encoding="utf-8"
                ),
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
            self.assertTrue(
                os.access(project_root / ".agent-flow" / "scripts" / "hooks" / "comment-checker.py", os.X_OK)
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
            self.assertIn(
                "code-generation-discipline",
                (project_root / ".agent-flow" / "bootstrap" / "AGENTS.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "현재 사용 중인 CLI(활성 host)의 sub-agent 2개가 필수",
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
            self.assertIn(
                'agent-flow run "<task>"',
                (project_root / "CLAUDE.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                'agent-flow run "<task>"',
                (project_root / ".agent-flow" / "bootstrap" / "AGENTS.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "현재 사용 중인 CLI(활성 host)의 sub-agent 2개가 필수",
                (project_root / "AGENTS.md").read_text(encoding="utf-8"),
            )
            agent_flow_skill = (project_root / ".agent-flow" / "skills" / "agent-flow" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("Treat the status command output as the only source of truth.", agent_flow_skill)
            self.assertIn("Do not run install just because a new session started.", agent_flow_skill)
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
                                        {"type": "command", "command": "scripts/hooks/guard-worktree.sh"},
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
            expected = [
                f"'{resolved_root / '.agent-flow' / 'scripts' / 'hooks' / 'guard-worktree.sh'}'",
                f"'{resolved_root / '.agent-flow' / 'scripts' / 'hooks' / 'guard-protected-branch.sh'}'",
                f"'{resolved_root / '.agent-flow' / 'scripts' / 'hooks' / 'comment-checker.py'}'",
                f"'{resolved_root / '.agent-flow' / 'scripts' / 'hooks' / 'show-phase-status.sh'}'",
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
        # exit 2일 때 Claude/Codex는 stderr만 모델에 전달한다. stdout은 무시된다.
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
                    hooks_dir / "guard-worktree.sh",
                    {"tool_name": "Bash", "tool_input": {"command": "git checkout -b feat/x"}},
                ),
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

    def test_guard_worktree_allows_non_branch_checkout(self) -> None:
        hook = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "guard-worktree.sh"
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
            subprocess.run(("git", "-C", str(repo), "tag", "v1.0.0"), check=True)
            subprocess.run(("git", "-C", str(repo), "branch", "other"), check=True)
            allowed_commands = (
                "git checkout v1.0.0",
                "git checkout HEAD~0",
                "git checkout missing-file.txt",
            )
            for command in allowed_commands:
                with self.subTest(command=command):
                    result = subprocess.run(
                        ("/bin/bash", str(hook)),
                        cwd=repo,
                        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
            blocked = subprocess.run(
                ("/bin/bash", str(hook)),
                cwd=repo,
                input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "git checkout other"}}),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(blocked.returncode, 2, blocked.stderr)
            self.assertIn("BLOCKED", blocked.stderr)

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
            self.assertEqual(result.stdout.strip(), "agent-flow installed profile=generic")
            self.assertTrue((project_root / ".agent-flow" / "kit.json").is_file())

    def test_node_installer_skips_managed_worktree_reinstall(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            worktree_root = project_root / ".agent-flow" / "worktrees" / "feat-task"
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
            worktree_root = project_root / ".agent-flow" / "worktrees" / "feat-task"
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
            worktree = project_root / ".agent-flow" / "worktrees" / "slice"
            worktree.mkdir(parents=True)

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
                ),
                cwd=worktree,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(start.returncode, 0, start.stderr)
            self.assertTrue((project_root / ".agent-flow" / "runs" / "full-feature" / "r1").is_dir())
            artifact = project_root / ".agent-flow" / "runs" / "full-feature" / "r1" / "artifacts" / "domain-grill.md"
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
            self.assertIn("reason: phase_artifact_written_advance_required", status.stdout)
            self.assertNotIn("reason: missing_phase_artifact", status.stdout)

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
            for index, marker in enumerate((".codex", ".Codex"), start=1):
                with self.subTest(marker=marker):
                    run_id = f"r{index}"
                    worktree = project_root / marker / "worktrees" / "slice"
                    worktree.mkdir(parents=True, exist_ok=True)

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
                        ),
                        cwd=worktree,
                        text=True,
                        capture_output=True,
                        check=False,
                    )

                    self.assertEqual(start.returncode, 0, start.stderr)
                    self.assertTrue((project_root / ".agent-flow" / "runs" / "full-feature" / run_id).is_dir())
                    self.assertFalse((worktree / ".agent-flow" / "runs" / "full-feature" / run_id).exists())

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
            worktree = root / ".agent-flow" / "worktrees" / "feat-slice"
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
            worktree = root / ".agent-flow" / "worktrees" / "feat-slice"

            old_cwd = Path.cwd()
            try:
                os.chdir(worktree)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["run", "other"]), 2)
            finally:
                os.chdir(old_cwd)

            self.assertIn("already active", output.getvalue())
            self.assertFalse((root / ".agent-flow" / "worktrees" / "feat-other").exists())
            self.assertFalse((worktree / ".agent-flow" / "worktrees").exists())

    def test_node_installer_from_agent_flow_worktree_without_root_install_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            worktree = project_root / ".agent-flow" / "worktrees" / "slice"
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

    def test_node_installer_from_external_codex_worktree_updates_git_common_install(self) -> None:
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

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((project_root / ".agent-flow" / "kit.json").is_file())
            self.assertFalse((worktree / ".agent-flow" / "kit.json").exists())

    def test_legacy_node_installer_from_external_codex_worktree_updates_git_common_install(self) -> None:
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
            self.assertTrue((project_root / ".agent-flow" / "kit.json").is_file())
            self.assertFalse((worktree / ".agent-flow" / "kit.json").exists())

    def test_node_runner_uses_git_common_install_from_external_codex_worktree(self) -> None:
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

            self.assertEqual(start.returncode, 0, start.stderr)
            self.assertTrue((project_root / ".agent-flow" / "runs" / "full-feature" / "r1").is_dir())
            self.assertFalse((worktree / ".agent-flow" / "runs" / "full-feature" / "r1").exists())

    def test_node_installers_ignore_profile_managed_host_only_project_skills(self) -> None:
        installers = ("agent-flow-kit.mjs", "agent-flow-install.mjs")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            node = _node_executable()
            for installer_name in installers:
                project_root = root / installer_name
                project_root.mkdir()
                skill_dir = project_root / "skills" / "android-mvi-feature"
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    "---\n"
                    "name: android-mvi-feature\n"
                    "hosts: [codex]\n"
                    "---\n"
                    "# Android MVI Feature\n",
                    encoding="utf-8",
                )
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
                self.assertNotIn("android-mvi-feature", skill_names)
                self.assertNotIn("compose-state-authoring", skill_names)
                self.assertNotIn("edge-to-edge", skill_names)
                self.assertFalse((project_root / ".codex" / "skills" / "android-mvi-feature").exists())
                self.assertFalse((project_root / ".codex" / "skills" / "compose-state-authoring").exists())
                self.assertFalse((project_root / ".codex" / "skills" / "edge-to-edge").exists())

    def test_node_installers_link_default_host_skills_to_claude_and_codex(self) -> None:
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
                    index = json.loads(
                        (project_root / ".agent-flow" / "skills" / "index.json").read_text(encoding="utf-8")
                    )
                    selected = next(skill for skill in index["skills"] if skill["name"] == "default-host-skill")
                    self.assertIn("claude", selected["hosts"])
                    self.assertIn("codex", selected["hosts"])
                    self.assertNotIn("gemini", selected["hosts"])
                    self.assertNotIn("antigravity", selected["hosts"])

    def test_node_installers_refresh_managed_workflow_skills(self) -> None:
        installers = ("agent-flow-kit.mjs", "agent-flow-install.mjs")
        rels = (
            ".agent-flow/workflows/full-feature.yaml",
            ".agent-flow/skills/full-feature-workflow/SKILL.md",
            ".agent-flow/skills/product-brief/SKILL.md",
            ".agent-flow/skills/plan-reviewer/SKILL.md",
            ".agent-flow/skills/ddd-clean-architecture/SKILL.md",
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
                    # managed workflow와 generated skill은 stale 설치본을 남기면 안 된다.
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
            self.assertIn("Default reviewers are active-host sub-agents", workflow.read_text(encoding="utf-8"))
            self.assertNotIn("Gemini sub-agent", workflow.read_text(encoding="utf-8"))
            self.assertIn("multi_review: true", workflow.read_text(encoding="utf-8"))
            self.assertIn("status: ci-failed", prompt.read_text(encoding="utf-8"))
            self.assertIn("def main(", runtime_lint.read_text(encoding="utf-8"))
            self.assertNotIn("stale runtime", runtime_lint.read_text(encoding="utf-8"))
            self.assertIn(
                "Default reviewers are active-host sub-agents",
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
                "close that sub-agent session",
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
            self.assertIn('agent-flow run "<task>"', claude_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("현재 사용 중인 CLI(활성 host)의 sub-agent 2개가 필수", bootstrap.read_text(encoding="utf-8"))
            self.assertIn("현재 사용 중인 CLI(활성 host)의 sub-agent 2개가 필수", claude_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("활성 host가 아닌 추가 provider는 optional", bootstrap.read_text(encoding="utf-8"))
            self.assertIn("활성 host가 아닌 추가 provider는 optional", claude_bootstrap.read_text(encoding="utf-8"))
            self.assertNotIn("예: Claude/Gemini", bootstrap.read_text(encoding="utf-8"))
            self.assertNotIn("예: Claude/Gemini", claude_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("reviewer-source: sub-agent", bootstrap.read_text(encoding="utf-8"))
            self.assertIn("reviewer-source: sub-agent", claude_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("sub-agent를 닫는다", bootstrap.read_text(encoding="utf-8"))
            self.assertIn("sub-agent를 닫는다", claude_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("## Overall", bootstrap.read_text(encoding="utf-8"))
            self.assertIn("## Overall", claude_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("verdict: approve", bootstrap.read_text(encoding="utf-8"))
            self.assertIn("verdict: approve", claude_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("verdict: request-changes", bootstrap.read_text(encoding="utf-8"))
            self.assertIn("verdict: request-changes", claude_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("Full Feature Workflow", skill.read_text(encoding="utf-8"))
            self.assertIn("Workflow Contract", rules.read_text(encoding="utf-8"))
            self.assertIn("two active-host sub-agents", rules.read_text(encoding="utf-8"))
            self.assertNotIn("Gemini sub-agent", rules.read_text(encoding="utf-8"))
            self.assertIn("reviewer-source: sub-agent", rules.read_text(encoding="utf-8"))
            self.assertIn("close that sub-agent session", rules.read_text(encoding="utf-8"))
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
            self.assertEqual(result.stdout.strip(), "agent-flow installed profile=node")
            kit = json.loads((project_root / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
            self.assertEqual(kit["profile"], "node")

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
            source_workflow = (Path(__file__).resolve().parents[1] / "workflows" / "full-feature.yaml").read_text(encoding="utf-8")
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
        self.assertEqual(package["bin"]["agent-flow"], "bin/agent-flow-kit.mjs")
        self.assertEqual(package["bin"]["agent-flow-kit"], "bin/agent-flow-kit.mjs")
        self.assertIn("bin", package["files"])

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

            start = subprocess.run(
                (node, cli, "run", "start", "--task", "demo feature", "--run-id", "r1"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(start.returncode, 0, start.stderr)
            self.assertIn("Current phase: domain-grill", start.stdout)

            status = subprocess.run(
                (node, cli, "run", "status"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("status: awaiting_host", status.stdout)
            self.assertIn("reason: missing_phase_artifact", status.stdout)
            self.assertIn("next_command: agent-flow run advance", status.stdout)
            self.assertIn("status_json:", status.stdout)

            blocked = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(blocked.returncode, 1)
            self.assertIn("blocked: missing artifact", blocked.stderr)

            artifact = project_root / ".agent-flow" / "runs" / "full-feature" / "r1" / "artifacts" / "domain-grill.md"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("domain-grill\n", encoding="utf-8")

            missing_markers = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(missing_markers.returncode, 1)
            self.assertIn("missing completion markers", missing_markers.stderr)

            artifact.write_text(
                "TODO: add domain-grill: complete before handoff\n"
                "shared_understanding: reached\n",
                encoding="utf-8",
            )
            false_positive = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(false_positive.returncode, 1)
            self.assertIn("domain-grill: complete", false_positive.stderr)

            artifact.write_text(
                "domain-grill: complete\n"
                "shared_understanding: reached\n",
                encoding="utf-8",
            )
            outside_gate = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(outside_gate.returncode, 1)
            self.assertIn("missing completion markers", outside_gate.stderr)

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
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(fenced_example.returncode, 1)
            self.assertIn("missing completion markers", fenced_example.stderr)

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
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(fenced_heading.returncode, 1)
            self.assertIn("missing completion markers", fenced_heading.stderr)

            artifact.write_text(
                "notes\n"
                "    ## Completion Gate\n"
                "    domain-grill: complete\n"
                "    shared_understanding: reached\n",
                encoding="utf-8",
            )
            indented_heading = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(indented_heading.returncode, 1)
            self.assertIn("missing completion markers", indented_heading.stderr)

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
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(bad_value.returncode, 1)
            self.assertIn("context_docs_updated: true|not_needed", bad_value.stderr)

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
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("status: blocked", status.stdout)
            self.assertIn("reason: phase_artifact_written_advance_required", status.stdout)
            self.assertIn("status_json:", status.stdout)

            advanced = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(advanced.returncode, 0, advanced.stderr)
            self.assertIn("Current phase: product-brief", advanced.stdout)
            state = json.loads((project_root / ".agent-flow" / "state" / "current-run.json").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "product-brief")

    def test_node_heading_required_markers_ignore_fenced_examples(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = project_root / ".agent-flow" / "runs" / "full-feature" / "r1"
            for phase in ["domain-grill", "product-brief", "prd", "slice-plan", "plan-review"]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                content = "verdict: approve\n" if phase == "plan-review" else _node_phase_content(phase)
                artifact.write_text(content, encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)

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
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("## Clean Architecture Boundary Map", result.stderr)

    def test_node_workflow_run_requires_installed_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
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
            self.assertIn("Current phase: domain-grill", result.stdout)

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

            result = subprocess.run(
                (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current phase: domain-grill", result.stdout)

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

    def test_node_workflow_run_advances_all_phases_and_handles_complete_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(
                subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode,
                0,
            )
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
                "gates",
                "commit",
                "push-pr",
                "pr-watch",
                "merge-approval",
                "merge",
                "handoff",
            ]
            run_dir = project_root / ".agent-flow" / "runs" / "full-feature" / "r1"
            for index, phase in enumerate(expected_phases):
                state = json.loads((project_root / ".agent-flow" / "state" / "current-run.json").read_text(encoding="utf-8"))
                self.assertEqual(state["phase"], phase)
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                if phase == "pr-watch":
                    content = "status: green\n"
                elif phase in {"plan-review", "merge-approval"}:
                    content = "verdict: approve\n"
                else:
                    content = _node_phase_content(phase)
                artifact.write_text(content, encoding="utf-8")
                advance = subprocess.run(
                    (node, cli, "run", "advance"),
                    cwd=project_root,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(advance.returncode, 0, advance.stderr)
                if index + 1 < len(expected_phases):
                    self.assertIn(f"Current phase: {expected_phases[index + 1]}", advance.stdout)
                else:
                    self.assertIn("workflow complete: r1", advance.stdout)

            complete = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(complete.returncode, 0, complete.stderr)
            self.assertIn("workflow already complete: r1", complete.stdout)

    def test_node_workflow_run_normalizes_persisted_phase_index_from_phase_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            current_run = project_root / ".agent-flow" / "state" / "current-run.json"
            state = json.loads(current_run.read_text(encoding="utf-8"))
            state["phase"] = "red"
            state["phase_index"] = 0
            current_run.write_text(f"{json.dumps(state, indent=2)}\n", encoding="utf-8")
            # run_dir는 project root 기준 상대 경로로 저장되므로 clean worktree에서도 명시적으로 해석한다.
            run_dir = Path(state["run_dir"])
            if not run_dir.is_absolute():
                run_dir = project_root / run_dir
            manifest = run_dir / "manifest.json"
            manifest.write_text(f"{json.dumps(state, indent=2)}\n", encoding="utf-8")

            next_result = subprocess.run(
                (node, cli, "run", "next"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(next_result.returncode, 0, next_result.stderr)
            self.assertIn("Current phase: red", next_result.stdout)
            normalized = json.loads(current_run.read_text(encoding="utf-8"))
            self.assertEqual(normalized["phase"], "red")
            self.assertNotEqual(normalized["phase_index"], 0)

    def test_node_pr_watch_blocks_pending_and_routes_fix_loops_back_to_watch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = project_root / ".agent-flow" / "runs" / "full-feature" / "r1"
            for phase in [
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
                "gates",
                "commit",
                "push-pr",
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                content = "verdict: approve\n" if phase == "plan-review" else _node_phase_content(phase)
                artifact.write_text(content, encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)

            watch = run_dir / _node_phase_artifact("pr-watch")
            watch.write_text("status: pending\n", encoding="utf-8")
            pending = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(pending.returncode, 1)
            self.assertIn("blocked: PR watch is pending", pending.stderr)
            pending_status = subprocess.run(
                (node, cli, "run", "status"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(pending_status.returncode, 0, pending_status.stderr)
            self.assertIn("reason: route_blocked", pending_status.stdout)
            self.assertIn("next_command: agent-flow run next", pending_status.stdout)

            watch.write_text("status: comments\n", encoding="utf-8")
            comments = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(comments.returncode, 0, comments.stderr)
            self.assertIn("Current phase: pr-comment-fix", comments.stdout)
            comment_fix = run_dir / _node_phase_artifact("pr-comment-fix")
            comment_fix.write_text("old comment fix\n", encoding="utf-8")
            os.utime(comment_fix, (1, 1))
            stale_comment_fix = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(stale_comment_fix.returncode, 1)
            self.assertIn("blocked: stale artifact", stale_comment_fix.stderr)
            stale_comment_status = subprocess.run(
                (node, cli, "run", "status"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(stale_comment_status.returncode, 0, stale_comment_status.stderr)
            self.assertIn("reason: stale_artifact", stale_comment_status.stdout)
            self.assertIn("next_command: agent-flow run advance", stale_comment_status.stdout)
            comment_fix.write_text("pushed comment fixes\n", encoding="utf-8")
            same_ms = json.loads((project_root / ".agent-flow" / "state" / "current-run.json").read_text(encoding="utf-8"))
            entered_ts = _node_epoch_seconds(same_ms["phase_entered_at"])
            os.utime(comment_fix, (entered_ts, entered_ts))
            back_to_watch = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(back_to_watch.returncode, 0, back_to_watch.stderr)
            self.assertIn("Current phase: pr-watch", back_to_watch.stdout)

            watch.write_text("status: comments\n", encoding="utf-8")
            comments_again = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(comments_again.returncode, 0, comments_again.stderr)
            self.assertIn("Current phase: pr-comment-fix", comments_again.stdout)
            reused_comment_fix = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(reused_comment_fix.returncode, 1)
            self.assertIn("blocked: missing artifact", reused_comment_fix.stderr)
            comment_fix.write_text("pushed second comment fixes\n", encoding="utf-8")
            self.assertEqual(
                subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode,
                0,
            )

            watch.write_text("status: ci-failed\n", encoding="utf-8")
            ci_failed = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(ci_failed.returncode, 0, ci_failed.stderr)
            self.assertIn("Current phase: pr-ci-fix", ci_failed.stdout)
            ci_fix = run_dir / _node_phase_artifact("pr-ci-fix")
            ci_fix.write_text("old ci fixes\n", encoding="utf-8")
            os.utime(ci_fix, (1, 1))
            stale_ci_fix = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(stale_ci_fix.returncode, 1)
            self.assertIn("blocked: stale artifact", stale_ci_fix.stderr)
            ci_fix.write_text("pushed ci fixes\n", encoding="utf-8")
            back_to_watch_again = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(back_to_watch_again.returncode, 0, back_to_watch_again.stderr)
            self.assertIn("Current phase: pr-watch", back_to_watch_again.stdout)

            watch.write_text("status: green\n", encoding="utf-8")
            ready = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(ready.returncode, 0, ready.stderr)
            self.assertIn("Current phase: merge", ready.stdout)

    def test_node_plan_review_and_architecture_review_route_request_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = project_root / ".agent-flow" / "runs" / "full-feature" / "r1"
            for phase in ["domain-grill", "product-brief", "prd", "slice-plan"]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text(_node_phase_content(phase), encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)

            plan_review = run_dir / _node_phase_artifact("plan-review")
            plan_review.write_text("verdict: REQUEST-CHANGES\n", encoding="utf-8")
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current phase: slice-plan", result.stdout)

            slice_plan = run_dir / _node_phase_artifact("slice-plan")
            missing_slice_plan = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(missing_slice_plan.returncode, 1)
            self.assertIn("blocked: missing artifact", missing_slice_plan.stderr)

            slice_plan.write_text("updated slice-plan\n", encoding="utf-8")
            self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)
            plan_review.write_text("verdict: APPROVE\n", encoding="utf-8")
            self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)
            ddd = run_dir / _node_phase_artifact("ddd-design")
            ddd.write_text(_node_phase_content("ddd-design"), encoding="utf-8")
            self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)
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
                artifact.write_text(_node_phase_content(phase), encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)

            architecture_review = run_dir / _node_phase_artifact("architecture-review")
            architecture_review.write_text(
                _node_phase_content("architecture-review").replace("verdict: approve", "verdict: request-changes"),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current phase: refactor", result.stdout)
            refactor = run_dir / _node_phase_artifact("refactor")
            missing_refactor = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(missing_refactor.returncode, 1)
            self.assertIn("blocked: missing artifact", missing_refactor.stderr)

            refactor.write_text(_node_phase_content("refactor", "updated "), encoding="utf-8")
            self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)
            for phase, next_phase in [
                ("comment-authoring", "multi-review"),
                ("multi-review", "architecture-review"),
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                missing_artifact = subprocess.run(
                    (node, cli, "run", "advance"),
                    cwd=project_root,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(missing_artifact.returncode, 1)
                self.assertIn("blocked: missing artifact", missing_artifact.stderr)

                artifact.write_text(_node_phase_content(phase, prefix="updated "), encoding="utf-8")
                advanced = subprocess.run(
                    (node, cli, "run", "advance"),
                    cwd=project_root,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(advanced.returncode, 0, advanced.stderr)
                self.assertIn(f"Current phase: {next_phase}", advanced.stdout)

            missing_architecture_review = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(missing_architecture_review.returncode, 1)
            self.assertIn("blocked: missing artifact", missing_architecture_review.stderr)

            architecture_review.write_text(_node_phase_content("architecture-review"), encoding="utf-8")
            approved = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)
            self.assertIn("Current phase: gates", approved.stdout)

            gates = run_dir / _node_phase_artifact("gates")
            gates.write_text(
                '{"passed": true, "results": [{"id": "lint", "command": "npm run lint", "passed": true, "exit_code": 0}]}\n',
                encoding="utf-8",
            )
            committed = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(committed.returncode, 0, committed.stderr)
            self.assertIn("Current phase: commit", committed.stdout)

    def test_node_gates_fail_routes_to_fix_loop_and_back(self) -> None:
        """gates fail → fix-loop → review → gates 순환 테스트."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = project_root / ".agent-flow" / "runs" / "full-feature" / "r1"
            for phase in [
                "domain-grill", "product-brief", "prd",
                "slice-plan", "plan-review", "ddd-design", "worktree",
                "run-start", "red", "green", "refactor",
                "comment-authoring", "multi-review", "architecture-review",
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                content = "verdict: approve\n" if phase == "plan-review" else _node_phase_content(phase)
                artifact.write_text(content, encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)

            state = json.loads((project_root / ".agent-flow" / "state" / "current-run.json").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "gates")

            gates_artifact = run_dir / _node_phase_artifact("gates")
            gates_artifact.parent.mkdir(parents=True, exist_ok=True)
            gates_artifact.write_text('{"passed": false, "results": [{"name": "lint", "passed": false}]}\n', encoding="utf-8")
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current phase: fix-loop", result.stdout)

            fix_loop_artifact = run_dir / _node_phase_artifact("fix-loop")
            fix_loop_artifact.write_text(_node_phase_content("fix-loop"), encoding="utf-8")
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current phase: comment-authoring", result.stdout)

            comment_artifact = run_dir / _node_phase_artifact("comment-authoring")
            comment_artifact.write_text(_node_phase_content("comment-authoring"), encoding="utf-8")
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current phase: multi-review", result.stdout)

            multi_review = run_dir / _node_phase_artifact("multi-review")
            multi_review.write_text(_node_phase_content("multi-review"), encoding="utf-8")
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current phase: architecture-review", result.stdout)

            architecture_review = run_dir / _node_phase_artifact("architecture-review")
            architecture_review.write_text(_node_phase_content("architecture-review"), encoding="utf-8")
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current phase: gates", result.stdout)

            gates_artifact.write_text(
                '{"passed": true, "results": [{"id": "lint", "command": "npm run lint", "passed": true, "output": "ok"}]}\n',
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current phase: commit", result.stdout)

    def test_node_multi_review_request_changes_routes_to_fix_loop(self) -> None:
        """multi-review request-changes → fix-loop → review 순환 테스트."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = project_root / ".agent-flow" / "runs" / "full-feature" / "r1"
            for phase in [
                "domain-grill", "product-brief", "prd",
                "slice-plan", "plan-review", "ddd-design", "worktree",
                "run-start", "red", "green", "refactor", "comment-authoring",
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                content = "verdict: approve\n" if phase == "plan-review" else _node_phase_content(phase)
                artifact.write_text(content, encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)

            state = json.loads((project_root / ".agent-flow" / "state" / "current-run.json").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "multi-review")

            mr_artifact = run_dir / _node_phase_artifact("multi-review")
            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: request-changes\n\n"
                "## Overall\n"
                "verdict: request-changes\n",
            ),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current phase: fix-loop", result.stdout)

            fix_loop_artifact = run_dir / _node_phase_artifact("fix-loop")
            fix_loop_artifact.write_text(_node_phase_content("fix-loop"), encoding="utf-8")
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current phase: comment-authoring", result.stdout)

            comment_artifact = run_dir / _node_phase_artifact("comment-authoring")
            comment_artifact.write_text(_node_phase_content("comment-authoring"), encoding="utf-8")
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current phase: multi-review", result.stdout)

            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n"
                "## Overall\n"
                "verdict: approve\n",
            ) + "dependency-rule: fail\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current phase: fix-loop", result.stdout)

            fix_loop_artifact.write_text(_node_phase_content("fix-loop"), encoding="utf-8")
            self.assertEqual(
                subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode,
                0,
            )
            comment_artifact.write_text(_node_phase_content("comment-authoring"), encoding="utf-8")
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current phase: multi-review", result.stdout)

            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n"
                "## Overall\n"
                "verdict: approve\n",
            ),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current phase: architecture-review", result.stdout)

    def test_node_default_final_review_uses_multi_review_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--workflow", "default", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = project_root / ".agent-flow" / "runs" / "default" / "r1"
            for phase in ["design", "slice-plan", "worktree", "implement"]:
                artifact = run_dir / f"{phase}.md"
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text(_node_phase_content(phase), encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)

            comment_artifact = run_dir / "comment-authoring.md"
            comment_artifact.write_text(_node_phase_content("comment-authoring"), encoding="utf-8")
            self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)

            final_artifact = run_dir / "final-review.md"
            final_artifact.write_text(_with_final_review_gate("verdict: approve\n"), encoding="utf-8")
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("at least 1 independent sub-agent reviewer verdict", result.stderr)

            final_artifact.write_text(
                _with_final_review_gate(
                    "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                    "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n"
                    "## Overall\nverdict: approve\n",
                    dependency_rule="fail",
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current phase: fix-loop", result.stdout)

    def test_node_fix_loop_round_cap_blocks_after_max(self) -> None:
        """fix-loop 3회 초과 시 에러로 차단."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = project_root / ".agent-flow" / "runs" / "full-feature" / "r1"
            for phase in [
                "domain-grill", "product-brief", "prd",
                "slice-plan", "plan-review", "ddd-design", "worktree",
                "run-start", "red", "green", "refactor",
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                content = "verdict: approve\n" if phase == "plan-review" else _node_phase_content(phase)
                artifact.write_text(content, encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)

            for round_num in range(3):
                for phase in ("comment-authoring", "multi-review", "architecture-review"):
                    artifact = run_dir / _node_phase_artifact(phase)
                    artifact.parent.mkdir(parents=True, exist_ok=True)
                    artifact.write_text(_node_phase_content(phase), encoding="utf-8")
                    self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)
                gates_artifact = run_dir / _node_phase_artifact("gates")
                gates_artifact.parent.mkdir(parents=True, exist_ok=True)
                gates_artifact.write_text('{"passed": false}\n', encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)
                fix_artifact = run_dir / _node_phase_artifact("fix-loop")
                fix_artifact.write_text(_node_phase_content("fix-loop"), encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)

            for phase in ("comment-authoring", "multi-review", "architecture-review"):
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.write_text(_node_phase_content(phase), encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)
            gates_artifact = run_dir / _node_phase_artifact("gates")
            gates_artifact.write_text('{"passed": false}\n', encoding="utf-8")
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("fix-loop exceeded", result.stderr)
            current_state = json.loads(
                (project_root / ".agent-flow" / "state" / "current-run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(current_state["fix_loop_rounds"], 3)

    def test_node_architecture_review_request_changes_routes_to_refactor(self) -> None:
        """architecture-review request-changes verdict → refactor 라우팅."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = project_root / ".agent-flow" / "runs" / "full-feature" / "r1"
            for phase in [
                "domain-grill", "product-brief", "prd",
                "slice-plan", "plan-review", "ddd-design", "worktree",
                "run-start", "red", "green", "refactor", "comment-authoring", "multi-review",
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                content = "verdict: approve\n" if phase == "plan-review" else _node_phase_content(phase)
                artifact.write_text(content, encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)

            arch_artifact = run_dir / _node_phase_artifact("architecture-review")
            arch_artifact.write_text(
                _node_phase_content("architecture-review").replace("verdict: approve", "verdict: request-changes"),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current phase: refactor", result.stdout)

    def test_node_multi_review_requires_subagent_reviewer(self) -> None:
        """multi-review artifact에 독립 sub-agent reviewer가 없으면 차단."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = project_root / ".agent-flow" / "runs" / "full-feature" / "r1"
            for phase in [
                "domain-grill", "product-brief", "prd",
                "slice-plan", "plan-review", "ddd-design", "worktree",
                "run-start", "red", "green", "refactor", "comment-authoring",
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                content = "verdict: approve\n" if phase == "plan-review" else _node_phase_content(phase)
                artifact.write_text(content, encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)

            mr_artifact = run_dir / _node_phase_artifact("multi-review")
            mr_artifact.write_text(_with_skills_gate(
                "reviewer verdict: approve\n## Reviewer\nverdict: approve\nverdict: approve\n",
            ),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("at least 1 independent sub-agent reviewer verdict", result.stderr)

            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer Notes\nverdict: approve\n\nverdict: approve\n",
            ),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("at least 1 independent sub-agent reviewer verdict", result.stderr)

            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer 1\nverdict: lgtm\n\n"
                "verdict: approve\n",
            ),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("at least 1 independent sub-agent reviewer verdict", result.stderr)

            mr_artifact.write_text(_with_skills_gate(
                "Reviewer verdict: approve\nReviewer verdict: approve\n"
                "verdict: approve\n",
            ),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("at least 1 independent sub-agent reviewer verdict", result.stderr)

            for legacy_status in ("verdict: request-changes\n", "status: failed\n", "status: fail\n"):
                mr_artifact.write_text(_with_skills_gate(legacy_status), encoding="utf-8")
                result = subprocess.run(
                    (node, cli, "run", "advance"),
                    cwd=project_root,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("at least 1 independent sub-agent reviewer verdict", result.stderr)

            for bad_source in (
                "## Reviewer 1\nreviewer-source: non-sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Overall\nverdict: approve\n",
                "reviewer-1 source: non-sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Overall\nverdict: approve\n",
                "reviewer-1 source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Overall\nverdict: approve\n",
                "## Reviewer 1\nsource: sub-agent\nverdict: approve\n\n"
                "## Overall\nverdict: approve\n",
            ):
                mr_artifact.write_text(_with_skills_gate(bad_source), encoding="utf-8")
                result = subprocess.run(
                    (node, cli, "run", "advance"),
                    cwd=project_root,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("at least 1 independent sub-agent reviewer verdict", result.stderr)

            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n",
            ),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("at least 2 independent sub-agent reviewer verdicts", result.stderr)

            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: APPROVE\n\n"
                "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: APPROVE\n\n"
                "## Overall\nverdict: APPROVE\n",
            ),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("overall verdict must be approve or request-changes", result.stderr)

            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer 1\n\nreviewer-source: sub-agent\nverdict: approve\n",
            ),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("at least 2 independent sub-agent reviewer verdicts", result.stderr)

            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer 1\nreviewer-source: sub-agent\n### Findings\nverdict: approve\n",
            ),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("at least 2 independent sub-agent reviewer verdicts", result.stderr)

            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer 1\nreviewer-source: sub-agent\n# Code Review\nverdict: approve\n",
            ),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("at least 2 independent sub-agent reviewer verdicts", result.stderr)

            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Overall\nstatus: passed\n",
            ),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("at least 2 independent sub-agent reviewer verdicts", result.stderr)

            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Overall\nverdict: approve\nverdict: request-changes\n",
            ),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("overall verdict must be approve or request-changes", result.stderr)

            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Overall\nverdict: request-changes\n\n## Overall\nverdict: approve\n",
            ),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("overall verdict must be approve or request-changes", result.stderr)

            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Final\nverdict: approve\n",
            ),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            # ## Final은 overall alias로 인정되므로 reviewer 수 부족이 정확한 차단 사유다.
            self.assertIn("at least 2 independent sub-agent reviewer verdicts", result.stderr)

            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer 1\nreviewer-source: codex sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Reviewer 2\nreviewer-source: claude sub-agent\nreviewer-2 verdict: approve\n\n"
                "## Overall\nverdict: approve\n",
            ),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("at least 1 independent sub-agent reviewer verdict", result.stderr)

            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer 1\nreviewer-source: active-host sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n"
                "## Overall\nverdict: approve\n",
            ),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_node_multi_review_single_request_changes_routes_to_fix_loop(self) -> None:
        """sub-agent reviewer 1명의 request-changes도 fix-loop로 라우팅한다."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            self.assertEqual(
                subprocess.run(
                    (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                    cwd=project_root,
                    check=False,
                ).returncode,
                0,
            )
            run_dir = project_root / ".agent-flow" / "runs" / "full-feature" / "r1"
            for phase in [
                "domain-grill", "product-brief", "prd",
                "slice-plan", "plan-review", "ddd-design", "worktree",
                "run-start", "red", "green", "refactor", "comment-authoring",
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                content = "verdict: approve\n" if phase == "plan-review" else _node_phase_content(phase)
                artifact.write_text(content, encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)

            mr_artifact = run_dir / _node_phase_artifact("multi-review")
            mr_artifact.write_text(_with_skills_gate(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: request-changes\n\n"
                "## Overall\nverdict: request-changes\n",
            ),
                encoding="utf-8",
            )
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current phase: fix-loop", result.stdout)

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
            run_dir = _node_start_full_feature_at_pr_watch(project_root, node, cli)
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
                cwd=project_root,
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

    def test_node_push_watch_tick_blocks_before_pr_watch_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            cli = str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs")
            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)
            subprocess.run(
                (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
                cwd=project_root,
                check=True,
            )

            result = subprocess.run(
                (node, cli, "run", "push-watch-tick"),
                cwd=project_root,
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
            run_dir = _node_start_full_feature_at_pr_watch(project_root, node, cli)
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
                cwd=project_root,
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
            run_dir = _node_start_full_feature_at_pr_watch(project_root, node, cli)
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
                cwd=project_root,
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
            run_dir = _node_start_full_feature_at_pr_watch(project_root, node, cli)
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
                cwd=project_root,
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
                            "--adapter",
                            "manual",
                            "--run-id",
                            "r1",
                            "--worktree",
                            "Slice A",
                        ]
                    ),
                    0,
                )
            worktree = root / ".agent-flow" / "worktrees" / "feat-slice-a"
            run_dir = worktree_runtime_root(root=root, name="feat-slice-a") / ".agent-flow" / "runs" / "development" / "r1"
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["worktree"],
                {
                    "name": "feat-slice-a",
                    "branch": "feat/slice-a",
                    "path": ".agent-flow/worktrees/feat-slice-a",
                },
            )
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
                        "--adapter",
                        "manual",
                        "--worktree",
                        "slice-a",
                    ]
                ),
                2,
            )
            self.assertFalse((root / ".agent-flow" / "runs" / "development").exists())

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
                        "--adapter",
                        "manual",
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
                        "--adapter",
                        "manual",
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
                    / "development"
                    / "r1"
                ).exists()
            )
            self.assertTrue(
                (
                    worktree_runtime_root(root=root, name="feat-slice-a")
                    / ".agent-flow"
                    / "runs"
                    / "development"
                    / "r1"
                ).exists()
            )

    def test_start_worktree_write_failure_cleans_run_and_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)

            with mock.patch("agent_flow.core.state._write_json", side_effect=OSError("manifest failed")):
                self.assertEqual(
                    main(
                        [
                            "start",
                            "development",
                            "--root",
                            str(root),
                            "--task",
                            "demo",
                            "--adapter",
                            "manual",
                            "--run-id",
                            "r1",
                            "--worktree",
                            "slice-a",
                        ]
                    ),
                    2,
                )
            self.assertFalse((root / ".agent-flow" / "runs" / "development" / "r1").exists())
            self.assertFalse((root / ".agent-flow" / "worktrees" / "feat-slice-a").exists())

    def test_worktree_create_manifest_write_failure_cleans_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)

            with mock.patch("agent_flow.core.worktrees.write_worktree_manifest", side_effect=OSError("manifest failed")):
                self.assertEqual(
                    main(["worktree", "create", "--root", str(root), "--name", "slice-a"]),
                    2,
                )
            self.assertFalse((root / ".agent-flow" / "worktrees" / "feat-slice-a").exists())

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
                        "--adapter",
                        "manual",
                        "--run-id",
                        "r1",
                        "--worktree",
                        "slice-a",
                    ]
                ),
                0,
            )
            manifest = json.loads(
                (
                    worktree_runtime_root(root=root, name="feat-slice-a")
                    / ".agent-flow"
                    / "runs"
                    / "development"
                    / "r1"
                    / "manifest.json"
                ).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["worktree"]["branch"], "feat/slice-a")

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
                        "--adapter",
                        "manual",
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
            manifest = json.loads(
                (
                    worktree_runtime_root(root=root, name="feat-slice-a")
                    / ".agent-flow"
                    / "runs"
                    / "development"
                    / "r1"
                    / "manifest.json"
                ).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["worktree"]["branch"], "feat/slice-a")
            self.assertTrue((root / ".agent-flow" / "worktrees" / "feat-slice-a").is_dir())

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

    def test_is_git_repo_treats_missing_git_as_non_git(self) -> None:
        from agent_flow.cli import _is_git_repo

        with tempfile.TemporaryDirectory() as temp_dir:
            # git 실행 파일이 없는 환경에서도 run/start가 non-git fallback으로 이어져야 한다.
            with mock.patch("agent_flow.core.commands.subprocess.run", side_effect=FileNotFoundError):
                self.assertFalse(_is_git_repo(Path(temp_dir)))

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
            ],
        )

    def test_provider_list_treats_host_environment_as_available(self) -> None:
        output = io.StringIO()
        with mock.patch("agent_flow.providers.host.shutil.which", return_value=None):
            with mock.patch.dict("agent_flow.providers.host.os.environ", {"CLAUDECODE": "1"}, clear=True):
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["provider", "list"]), 0)
        self.assertIn("claude-session available command=claude", output.getvalue())

    def test_status_reports_latest_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            self.assertEqual(
                main(["start", "review", "--root", str(root), "--task", "review demo", "--run-id", "r1"]),
                0,
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["status", "--root", str(root)]), 0)
            lines = output.getvalue().strip().splitlines()
            self.assertEqual(lines[0], "review r1 awaiting_host")
            self.assertIn("status: awaiting_host", lines)
            self.assertIn("run: review/r1", lines)
            self.assertIn("task: review demo", lines)
            self.assertIn("current_phase: explore", lines)
            self.assertIn("reason: missing_stage_artifact", lines)
            self.assertIn("required_action: write_stage_artifact", lines)
            self.assertIn(
                "required_artifact: .agent-flow/runs/review/r1/artifacts/explore.md",
                lines,
            )
            self.assertIn("next_command: none", lines)
            self.assertIn(
                "next_command_template: agent-flow record-stage --root "
                + str(root)
                + " --run-dir .agent-flow/runs/review/r1 --stage explore --content '<stage result>'",
                lines,
            )
            self.assertTrue(any(line.startswith("status_json: ") for line in lines))

    def test_status_summary_advances_to_next_missing_stage_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            self.assertEqual(
                main(["start", "review", "--root", str(root), "--task", "review demo", "--run-id", "r1"]),
                0,
            )
            run_dir = root / ".agent-flow" / "runs" / "review" / "r1"
            manifest = run_dir / "manifest.json"
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["current_phase"] = "explore"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(
                main(
                    [
                        "record-stage",
                        "--root",
                        str(root),
                        "--run-dir",
                        ".agent-flow/runs/review/r1",
                        "--stage",
                        "explore",
                        "--content",
                        "explored",
                    ]
                ),
                0,
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["status", "--root", str(root)]), 0)
            lines = output.getvalue().strip().splitlines()
            self.assertIn("current_phase: review-1", lines)
            self.assertIn(
                "required_artifact: .agent-flow/runs/review/r1/artifacts/review-1.md",
                lines,
            )
            self.assertIn("next_command: none", lines)
            self.assertIn("required_action: write_stage_artifact", lines)
            self.assertIn(
                "next_command_template: agent-flow record-stage --root "
                + str(root)
                + " --run-dir .agent-flow/runs/review/r1 --stage review-1 --content '<stage result>'",
                lines,
            )

    def test_status_escapes_task_newlines_and_emits_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task = "review demo\nstatus: complete\nreason: injected"
            self.assertEqual(
                main(["start", "review", "--root", str(root), "--task", task, "--run-id", "r1"]),
                0,
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["status", "--root", str(root)]), 0)
            lines = output.getvalue().strip().splitlines()
            self.assertIn(r"task: review demo\nstatus: complete\nreason: injected", lines)
            self.assertNotIn("reason: injected", lines)
            status_json = next(line for line in lines if line.startswith("status_json: "))
            payload = json.loads(status_json.removeprefix("status_json: "))
            self.assertEqual(payload["task"], task)
            self.assertEqual(payload["current_phase"], "explore")
            self.assertEqual(payload["next_command"], "none")
            self.assertEqual(payload["required_action"], "write_stage_artifact")

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
                cwd=project_root,
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
            with mock.patch.dict(
                os.environ,
                {"AGENT_FLOW_ADAPTER": "generic", "AGENT_FLOW_GENERIC_MODE": "emit"},
                clear=False,
            ):
                self.assertEqual(
                    main(["run", "demo task", "--root", str(root), "--workflow", "full-feature"]),
                    0,
                )
            run_dir = next((root / ".agent-flow" / "runs").iterdir())
            (run_dir / "domain-grill.md").write_text(
                "## Completion Gate\n"
                "TODO: domain-grill: complete\n"
                "shared_understanding: reached\n",
                encoding="utf-8",
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["status", "--root", str(root)]), 0)
            lines = output.getvalue().strip().splitlines()
            self.assertIn("status: blocked", lines)
            self.assertIn("reason: missing_completion_markers", lines)
            self.assertTrue(any(line.startswith("status_json: ") for line in lines))

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

    def test_workflow_stage_rejects_invalid_parallel_type(self) -> None:
        with self.assertRaises(ValueError):
            _stage_from_payload(
                {"id": "review", "role": "reviewer", "parallel": "false"},
                workflow_id="bad",
            )

    def test_workflow_stage_rejects_bool_replicas(self) -> None:
        with self.assertRaises(ValueError):
            _stage_from_payload(
                {"id": "review", "role": "reviewer", "replicas": True},
                workflow_id="bad",
            )

    def test_load_profile_reads_packaged_gates(self) -> None:
        profile = load_profile("node")
        self.assertEqual(profile.profile_id, "node")
        self.assertEqual(profile.gates[0].gate_id, "context-lint")
        self.assertEqual(profile.gates[0].command, ("node", "scripts/check-context-docs.mjs"))
        self.assertEqual(profile.gates[1].gate_id, "architecture-lint")
        self.assertEqual(profile.gates[1].command, ("agent-flow", "architecture-lint", "--profile", "node"))
        # npm 기반 TypeScript profile은 subprocess argv list로 검증 명령을 보관한다.
        typescript = load_profile("typescript")
        self.assertEqual(typescript.gates[1].gate_id, "architecture-lint")
        self.assertEqual(typescript.gates[1].command, ("agent-flow", "architecture-lint", "--profile", "typescript"))
        self.assertEqual(typescript.gates[2].gate_id, "typecheck")
        self.assertEqual(typescript.gates[2].command, ("npx", "tsc", "--noEmit"))
        nextjs_gates = {gate.gate_id: gate.command for gate in load_profile("nextjs").gates}
        self.assertEqual(nextjs_gates["architecture-lint"], ("agent-flow", "architecture-lint", "--profile", "nextjs"))
        self.assertEqual(nextjs_gates["build"], ("npm", "run", "build"))
        python_gates = {gate.gate_id: gate for gate in load_profile("python").gates}
        self.assertFalse(python_gates["type"].required)
        self.assertFalse(python_gates["lint"].required)
        self.assertFalse(python_gates["test"].required)
        android = load_profile("android")
        self.assertEqual(android.profile_id, "android")
        android_required = android.skills["required_review"]
        self.assertEqual(android_required[0]["group"], "profile")
        self.assertIn("android-code-review", android_required[0]["skills"])
        self.assertEqual(android_required[1]["group"], "android_skills")
        self.assertEqual(android_required[2]["group"], "chrisbanes_skills")
        rn_required = load_profile("react-native").skills["required_review"]
        self.assertEqual(rn_required[1]["group"], "android-native-escalation")

    def test_runner_prefers_repository_kit_root(self) -> None:
        from agent_flow.runner import _find_kit_root

        self.assertEqual(_find_kit_root(), Path(__file__).resolve().parents[1])

    def test_pr_watch_cli_prints_snapshot_json_once(self) -> None:
        from agent_flow.pr_watch import PRSnapshot

        output = io.StringIO()
        snapshot = PRSnapshot(number=4, title="demo", state="OPEN", status="green")
        with mock.patch("agent_flow.cli.fetch_pr", return_value=snapshot):
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["pr-watch", "4", "--once"]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["number"], 4)
        self.assertEqual(payload["status"], "green")

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
            scripts = root / ".agent-flow" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "check-context-docs.mjs").write_text("process.exit(0);\n", encoding="utf-8")
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
            self.assertEqual(output.getvalue().strip(), "generic: 2/2 gates passed")
            gate_payload = json.loads((run_dir / "artifacts" / "gate-results.json").read_text(encoding="utf-8"))
            self.assertTrue(gate_payload["passed"])
            self.assertIsInstance(gate_payload["results"], list)
            results_by_command = {result["command"]: result for result in gate_payload["results"]}
            self.assertEqual(
                results_by_command["node .agent-flow/scripts/check-context-docs.mjs"]["argv"],
                ["node", ".agent-flow/scripts/check-context-docs.mjs"],
            )
            self.assertTrue(results_by_command["node .agent-flow/scripts/check-context-docs.mjs"]["required"])
            self.assertIn("agent_flow.core.architecture_lint", " ".join(results_by_command))
            self.assertTrue((run_dir / "gate-results.json").is_file())

    def test_gate_results_allow_optional_failures(self) -> None:
        from agent_flow.core.artifacts import write_gate_results
        from agent_flow.core.gates import GateResult

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            write_gate_results(
                run_dir=run_dir,
                results=[
                    GateResult("context-lint", ("node", "scripts/check-context-docs.mjs"), True, 0, "ok", ""),
                    GateResult("lint", ("ruff", "check", "."), False, None, "", "missing", required=False),
                ],
            )
            payload = json.loads((run_dir / "artifacts" / "gate-results.json").read_text(encoding="utf-8"))
            self.assertTrue(payload["passed"])
            self.assertEqual(payload["status"], "green")
            self.assertFalse(payload["results"][1]["required"])

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

            def fake_run_gates(commands: list[GateCommand], *, cwd: Path, timeout_s: int = 600) -> list[GateResult]:
                captured.extend(commands)
                return [
                    GateResult(command.gate_id, command.command, True, 0, "", "")
                    for command in commands
                ]

            output = io.StringIO()
            with mock.patch("agent_flow.cli.run_gates", side_effect=fake_run_gates):
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["gates", "--root", str(root)]), 0)

            commands = [command.command for command in captured]
            architecture_command = (sys.executable, "-m", "agent_flow.core.architecture_lint", "--profile", "android,react-native")
            self.assertIn(architecture_command, commands)
            self.assertNotIn((sys.executable, "-m", "agent_flow.core.architecture_lint", "--profile", "android"), commands)
            self.assertNotIn((sys.executable, "-m", "agent_flow.core.architecture_lint", "--profile", "react-native"), commands)
            gate_ids = [command.gate_id for command in captured]
            self.assertLess(gate_ids.index("android:build"), gate_ids.index("architecture-lint"))
            self.assertLess(gate_ids.index("react-native:android-build"), gate_ids.index("react-native:lint"))
            self.assertLess(gate_ids.index("react-native:android-build"), gate_ids.index("android:lint"))
            self.assertEqual(output.getvalue().strip(), "android,react-native: 9/9 gates passed")

    def test_profile_gate_commands_enforce_build_typecheck_lint_order(self) -> None:
        from agent_flow.cli import _profile_gate_commands

        typescript_ids = [command.gate_id for command in _profile_gate_commands(["typescript"])]
        self.assertIn("architecture-lint", typescript_ids)
        self.assertLess(typescript_ids.index("build"), typescript_ids.index("typecheck"))
        self.assertLess(typescript_ids.index("typecheck"), typescript_ids.index("lint"))

        react_native_ids = [command.gate_id for command in _profile_gate_commands(["react-native"])]
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
            worktree = root / ".agent-flow" / "worktrees" / "semantic-architecture-parity"
            scripts = kit / "scripts"
            scripts.mkdir(parents=True)
            worktree.mkdir(parents=True)
            (worktree / ".git").write_text("gitdir: ../../.git/worktrees/semantic-architecture-parity\n", encoding="utf-8")
            (scripts / "check-context-docs.mjs").write_text("process.exit(0);\n", encoding="utf-8")

            output = io.StringIO()
            captured: list[GateCommand] = []

            def fake_run_gates(commands: list[GateCommand], *, cwd: Path, timeout_s: int = 600):
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
                            ]
                        ),
                        0,
                    )
            self.assertEqual(output.getvalue().strip(), "android,react-native: 9/9 gates passed")
            self.assertIn(
                (sys.executable, "-m", "agent_flow.core.architecture_lint", "--profile", "android,react-native"),
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
            self.assertEqual(output.getvalue().strip(), "generic: 2/2 gates passed")

            output = io.StringIO()
            captured_lint: dict[str, object] = {}

            def fake_lint_profiles(cwd: Path, profile_ids: list[str], files: list[str] | None = None):
                captured_lint["cwd"] = cwd
                captured_lint["profile_ids"] = profile_ids
                captured_lint["files"] = files
                return {profile_id: [] for profile_id in profile_ids}

            with mock.patch("agent_flow.cli.lint_profiles", side_effect=fake_lint_profiles):
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
            self.assertEqual(captured_lint["cwd"].resolve(), worktree.resolve())
            self.assertEqual(captured_lint["profile_ids"], ["android", "react-native"])
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
            self.assertIn("generic: architecture lint passed", output.getvalue())

    def test_node_architecture_lint_accepts_worktree_argument(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            worktree = root / ".agent-flow" / "worktrees" / "semantic-architecture-parity"
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
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("generic: architecture lint passed", result.stdout)

    def test_node_gates_accepts_worktree_argument(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            worktree = root / ".agent-flow" / "worktrees" / "semantic-architecture-parity"
            scripts = root / ".agent-flow" / "scripts"
            scripts.mkdir(parents=True)
            worktree.mkdir(parents=True)
            (worktree / ".git").write_text("gitdir: ../../.git/worktrees/semantic-architecture-parity\n", encoding="utf-8")
            (scripts / "check-context-docs.mjs").write_text(
                (Path(__file__).resolve().parents[1] / "scripts" / "check-context-docs.mjs").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            _write_minimal_context_docs(root)
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
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("generic: 2/2 gates passed", result.stdout)
            gate_payload_text = (run_dir / "artifacts" / "gate-results.json").read_text(encoding="utf-8")
            self.assertNotIn(str(root), gate_payload_text)
            context_result = subprocess.run(
                (node, str(scripts / "check-context-docs.mjs")),
                cwd=worktree,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(context_result.returncode, 0, context_result.stdout + context_result.stderr)

    def test_gates_cli_resolves_relative_run_dir_against_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            cwd = Path(temp_dir) / "caller"
            root.mkdir()
            cwd.mkdir()
            scripts = root / ".agent-flow" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "check-context-docs.mjs").write_text("process.exit(0);\n", encoding="utf-8")
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
            self.assertTrue((root / ".agent-flow" / "runs" / "manual" / "artifacts" / "gate-results.json").is_file())
            self.assertTrue((root / ".agent-flow" / "runs" / "manual" / "gate-results.json").is_file())
            self.assertFalse((cwd / ".agent-flow" / "runs" / "manual" / "gate-results.json").exists())
            self.assertFalse((cwd / ".agent-flow" / "runs" / "manual" / "artifacts" / "gate-results.json").exists())

    def test_record_stage_writes_stage_result_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertEqual(
                main(
                    [
                        "record-stage",
                        "--root",
                        str(root),
                        "--run-dir",
                        ".agent-flow/runs/development/r1",
                        "--stage",
                        "explore",
                        "--content",
                        "Found auth module.",
                    ]
                ),
                0,
            )
            path = root / ".agent-flow" / "runs" / "development" / "r1" / "artifacts" / "explore.md"
            self.assertTrue(path.is_file())
            self.assertIn("Found auth module.", path.read_text(encoding="utf-8"))

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

    def test_render_stage_prompt_uses_codex_template(self) -> None:
        prompt = render_stage_prompt(
            PromptContext(
                adapter="codex-session",
                stage_id="explore",
                role="explorer",
                workflow_id="development",
                run_id="r1",
                replica=1,
                replicas=1,
                task="inspect repo",
            )
        )
        self.assertIn("Codex Subagent Stage: explore", prompt)
        self.assertIn("Spawn a subagent for role `explorer`.", prompt)
        self.assertIn("Run: r1", prompt)

    def test_render_stage_prompt_uses_claude_template(self) -> None:
        prompt = render_stage_prompt(
            PromptContext(
                adapter="claude-session",
                stage_id="review",
                role="reviewer",
                workflow_id="review",
                run_id="r2",
                replica=2,
                replicas=3,
                task="review diff",
            )
        )
        self.assertIn("Claude Task Stage: review", prompt)
        self.assertIn("Replica: 2/3", prompt)

    def test_render_stage_prompt_falls_back_to_generic_template(self) -> None:
        prompt = render_stage_prompt(
            PromptContext(
                adapter="manual",
                stage_id="qa",
                role="qa",
                workflow_id="development",
                run_id="r3",
                replica=1,
                replicas=1,
                task="run gates",
            )
        )
        self.assertIn("# qa", prompt)
        self.assertIn("Adapter: manual", prompt)

    def test_render_stage_prompt_allows_task_braces(self) -> None:
        prompt = render_stage_prompt(
            PromptContext(
                adapter="manual",
                stage_id="implement",
                role="implementer",
                workflow_id="development",
                run_id="r4",
                replica=1,
                replicas=1,
                task="replace {{token}} in docs",
            )
        )
        self.assertIn("replace {{token}} in docs", prompt)

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
            self.assertEqual(runner._next_index(0, phase), (1, False))

            (run_dir / "final-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: request-changes\n\n"
                "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n"
                "## Overall\n"
                "verdict: request-changes\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (1, False))

            (run_dir / "final-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n"
                "## Overall\n"
                "verdict: approve\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (2, False))

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

            self.assertEqual(runner._next_index(0, phase), (0, True))

    def test_provider_rate_limits_render_retry_status(self) -> None:
        from agent_flow.multi_review import _render_angle_result
        from agent_flow.subprocess_pool import SubprocessResult

        cases = [
            ("codex-generalist", "429 too many requests; rate limit resets in 5 minutes", "codex"),
        ]
        for job_id, stderr, reviewer in cases:
            artifact = _render_angle_result(SubprocessResult(job_id=job_id, stderr=stderr, returncode=1))
            self.assertIn("reason: reviewer_rate_limited", artifact)
            self.assertIn(f"reviewer: {reviewer}", artifact)
            self.assertIn(f"next_command: agent-flow review retry --reviewer {reviewer}", artifact)

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
            self.assertEqual(plan.path, root / ".agent-flow" / "worktrees" / "feat-implement-login")

            korean_plan = plan_worktree(root=root, name="버그 수정")
            # 한글 task도 deterministic fallback slug로 worktree를 만들 수 있어야 한다.
            self.assertRegex(korean_plan.name, r"^feat-task-[a-f0-9]{8}$")
            self.assertEqual(korean_plan.branch, korean_plan.name.replace("feat-", "feat/", 1))
            self.assertEqual(korean_plan.path, root / ".agent-flow" / "worktrees" / korean_plan.name)
            with mock.patch("agent_flow.core.commands.subprocess.run", side_effect=OSError("no git")):
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
            worktree = root / ".agent-flow" / "worktrees" / "feat-slice-a"
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
                cwd=root / ".agent-flow" / "worktrees" / "feat-slice-a",
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
            self.assertTrue((root / ".agent-flow" / "worktrees" / "feat-after-init").is_dir())

    def test_guard_worktree_blocks_leader_branch_switch(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "guard-worktree.sh"
        payload = json.dumps({"tool_input": {"command": "git switch codex/current && npm test"}})
        result = subprocess.run(
            ("bash", str(script)),
            input=payload,
            text=True,
            capture_output=True,
            check=False,
        )
        # agent가 기준 worktree의 브랜치 표시를 바꾸는 명령을 실행하지 못하게 막는다.
        self.assertEqual(result.returncode, 2)
        self.assertIn("기준 worktree", result.stderr)

        chained = subprocess.run(
            ("bash", str(script)),
            input=json.dumps({"tool_input": {"cmd": "cd . && git checkout -b feat/test"}}),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(chained.returncode, 2)
        self.assertIn("브랜치만 만들지", chained.stderr)

        for command in (
            "git -C . checkout -b feat/test",
            "command git checkout -b feat/test",
            "env TEST=1 git checkout -B feat/test",
        ):
            blocked_create = subprocess.run(
                ("bash", str(script)),
                input=json.dumps({"tool_input": {"command": command}}),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(blocked_create.returncode, 2, command)
            self.assertIn("브랜치만 만들지", blocked_create.stderr)

        blocked_detach = subprocess.run(
            ("bash", str(script)),
            input=json.dumps({"tool_input": {"command": "git checkout --detach main"}}),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(blocked_detach.returncode, 2)
        self.assertIn("기준 worktree", blocked_detach.stderr)

        allowed = subprocess.run(
            ("bash", str(script)),
            input=json.dumps({"tool_input": {"command": "git checkout -- README.md"}}),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(allowed.returncode, 0)

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

    def test_team_run_next_completes_pending_task_with_host_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
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
            self.assertEqual(output.getvalue().strip(), "task-1 completed")

            status_output = io.StringIO()
            with contextlib.redirect_stdout(status_output):
                self.assertEqual(
                    main(["team", "status", "--root", str(root), "--team", "feature-team", "--detail"]),
                    0,
                )
            self.assertIn("task task-1 completed owner=worker-1 subject=Implement login", status_output.getvalue())

    def test_team_run_next_fails_task_when_host_command_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
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
            self.assertEqual(output.getvalue().strip(), "task-1 failed")

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

    def test_team_claim_and_complete_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
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
                    ),
                    0,
                )
            parts = output.getvalue().strip().split()
            self.assertEqual(parts[:3], ["task-1", "in_progress", "worker-1"])
            token = parts[3]
            self.assertEqual(
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
                        token,
                        "--result",
                        "done",
                    ]
                ),
                0,
            )
            task = _read_task_json(root)
            self.assertEqual(task["status"], "completed")
            self.assertIsNone(task["claim_token"])
            self.assertEqual(task["result"], "done")

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

    def test_team_claim_requires_registered_worker(self) -> None:
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
            with self.assertRaises(FileNotFoundError):
                main(
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

    def test_team_complete_requires_matching_claim_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _create_team_with_task_and_worker(root)
            main(
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
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                main(
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
            token = output.getvalue().strip().split()[3]
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
                        token,
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

    def test_team_claim_allows_only_one_worker(self) -> None:
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

            def claim(worker: str) -> bool:
                try:
                    main(
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
                    return True
                except RuntimeError:
                    return False

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(claim, ["worker-1", "worker-2"]))
            self.assertEqual(results.count(True), 1)
            task = _read_task_json(root)
            self.assertEqual(task["status"], "in_progress")
            self.assertIn(task["owner"], {"worker-1", "worker-2"})


def _init_git_repo(root: Path) -> None:
    subprocess.run(("git", "init", "-q"), cwd=root, check=True)
    subprocess.run(("git", "config", "user.email", "test@example.com"), cwd=root, check=True)
    subprocess.run(("git", "config", "user.name", "Test User"), cwd=root, check=True)
    (root / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(("git", "add", "README.md"), cwd=root, check=True)
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


def _node_phase_artifact(phase: str) -> Path:
    artifacts = {
        "domain-grill": Path("artifacts/domain-grill.md"),
        "product-brief": Path("artifacts/product-brief.md"),
        "prd": Path("artifacts/prd.md"),
        "design": Path("artifacts/design.md"),
        "slice-plan": Path("artifacts/slice-plan.md"),
        "plan-review": Path("artifacts/plan-review.md"),
        "ddd-design": Path("artifacts/ddd-design.md"),
        "worktree": Path("artifacts/worktree.md"),
        "run-start": Path("artifacts/run-start.md"),
        "red": Path("artifacts/red.log"),
        "green": Path("artifacts/green.log"),
        "refactor": Path("artifacts/refactor.md"),
        "gates": Path("artifacts/gate-results.json"),
        "comment-authoring": Path("artifacts/comment-authoring.md"),
        "multi-review": Path("artifacts/multi-review.md"),
        "implement": Path("artifacts/implement.md"),
        "final-review": Path("artifacts/final-review.md"),
        "fix-loop": Path("artifacts/fix-loop.md"),
        "architecture-review": Path("artifacts/architecture-review.md"),
        "commit": Path("artifacts/commit.md"),
        "push-pr": Path("artifacts/push-pr.md"),
        "pr-watch": Path("artifacts/pr-watch.md"),
        "pr-comment-fix": Path("artifacts/pr-comment-fix.md"),
        "pr-ci-fix": Path("artifacts/pr-ci-fix.md"),
        "merge-approval": Path("artifacts/merge-approval.md"),
        "merge": Path("artifacts/merge.md"),
        "cleanup": Path("artifacts/cleanup.md"),
        "handoff": Path("artifacts/handoff.md"),
    }
    return artifacts[phase]


def _node_presentation_gate() -> str:
    # n/a 허용 marker는 optional alias도 통과해야 한다.
    return (
        "presentation-skill: optional\n"
        "presentation-state-based-development: optional\n"
        "presentation-state-review: optional\n"
        "ui-state-modeling: optional\n"
        "presentation-mapping-boundary: optional\n"
        "di-boundary: optional\n"
    )


def _node_project_local_gate() -> str:
    # 테스트 fixture에서는 프로젝트별 로컬 skill이 적용되지 않은 경우를 명시한다.
    return "project-local-skills: n/a\nproject-local-skills-used: n/a\n"


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


def _node_phase_content(phase: str, prefix: str = "") -> str:
    content = f"{prefix}{phase}\n"
    skills_gate = (
        "## Completion Gate\n"
        "skills_checked: true\n"
        + _node_profile_skill_gate()
        + _node_project_local_gate()
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
        "usecase-interface-check: applied\n"
        "usecase-composition-check: applied\n"
        "cache-boundary-check: applied\n"
        "mapping-boundary-check: applied\n"
        "solid-clean-architecture-check: applied\n"
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
    if phase == "gates":
        return '{"passed": true, "results": [{"id": "test", "command": "npm test", "passed": true, "output": "ok"}]}\n'
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
    if phase in {"design", "ddd-design"}:
        return content + clean_design_gate
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
            + _node_project_local_gate()
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
            + _node_project_local_gate()
            + _node_presentation_gate()
        )
    if phase in {"green", "refactor", "fix-loop"}:
        return content + skills_gate + "clean-architecture: applied\n"
    if phase in {"red", "green", "refactor", "fix-loop"}:
        return content + skills_gate
    return content


def _with_skills_gate(content: str) -> str:
    return (
        f"{content.rstrip()}\n\n## Completion Gate\n"
        "skills_checked: true\n"
        + _node_profile_skill_gate()
        + _node_review_parity_gate()
        + "clean-architecture-review: applied\n"
        + _node_project_local_gate()
        + "usecase-interface-check: applied\n"
        "usecase-composition-check: applied\n"
        "cache-boundary-check: applied\n"
        "mapping-boundary-check: applied\n"
        "solid-clean-architecture-check: applied\n"
        + _node_presentation_gate()
    )


def _with_final_review_gate(content: str, dependency_rule: str = "pass") -> str:
    return (
        f"{content.rstrip()}\n\n## Completion Gate\n"
        "skills_checked: true\n"
        + _node_profile_skill_gate()
        + _node_review_parity_gate()
        + "clean-architecture: applied\n"
        + _node_project_local_gate()
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


def _node_start_full_feature_at_pr_watch(project_root: Path, node: str, cli: str) -> Path:
    subprocess.run(
        (node, cli, "run", "start", "--task", "demo", "--run-id", "r1"),
        cwd=project_root,
        check=True,
    )
    run_dir = project_root / ".agent-flow" / "runs" / "full-feature" / "r1"
    for phase in [
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
        "gates",
        "commit",
        "push-pr",
    ]:
        artifact = run_dir / _node_phase_artifact(phase)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        content = "verdict: approve\n" if phase == "plan-review" else _node_phase_content(phase)
        artifact.write_text(content, encoding="utf-8")
        subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=True)
    return run_dir


def _node_epoch_seconds(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


if __name__ == "__main__":
    unittest.main()
