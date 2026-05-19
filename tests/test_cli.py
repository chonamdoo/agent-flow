from __future__ import annotations

import contextlib
import io
import os
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

os.environ.setdefault("AGENT_FLOW_GRAPHIFY_DRY_RUN", "1")

from agent_flow.cli import main
from agent_flow.adapters.templates import PromptContext, render_stage_prompt
from agent_flow.core.gates import GateCommand, run_gate
from agent_flow.core.profiles import load_profile
from agent_flow.core.review import _parse_verdict
from agent_flow.core.team import ShutdownSignal
from agent_flow.core.workflow import _stage_from_payload
from agent_flow.core.worktrees import plan_worktree, worktree_runtime_root


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
        self.assertEqual(phases["gates"]["routes"]["green"], "multi-review")
        self.assertEqual(phases["multi-review"]["routes"]["request-changes"], "fix-loop")
        self.assertTrue(phases["multi-review"]["multi_review"])
        self.assertIn("Default reviewers are active-host sub-agents", phases["multi-review"]["prompt"])
        self.assertIn("close that sub-agent session", phases["multi-review"]["prompt"])
        self.assertIn("reviewer-source: sub-agent", phases["multi-review"]["prompt"])
        self.assertIn("## Overall", phases["multi-review"]["prompt"])
        self.assertIn("verdict: approve", phases["multi-review"]["prompt"])
        self.assertIn("verdict: request-changes", phases["multi-review"]["prompt"])
        self.assertEqual(phases["fix-loop"]["routes"]["default"], "gates")
        self.assertEqual(phases["architecture-review"]["routes"]["blocked"], "refactor")
        self.assertEqual(phases["pr-watch"]["routes"]["comments"], "pr-comment-fix")
        self.assertEqual(phases["pr-watch"]["routes"]["ci-failed"], "pr-ci-fix")
        self.assertEqual(phases["pr-comment-fix"]["routes"]["default"], "pr-watch")
        self.assertEqual(phases["pr-ci-fix"]["routes"]["default"], "pr-watch")
        self.assertEqual(phases["merge-approval"]["routes"]["default"], "block")
        self.assertIn("Output: red.md.", phases["red"]["prompt"])
        self.assertIn("Output: green.md.", phases["green"]["prompt"])
        self.assertIn("Output: gates.md.", phases["gates"]["prompt"])

        default_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "agent_flow"
            / "workflows"
            / "default.yaml"
        )
        default_payload = yaml.safe_load(default_path.read_text(encoding="utf-8"))
        default_phases = {phase["id"]: phase for phase in default_payload["phases"]}
        self.assertEqual(default_phases["implement"]["required_markers"], ["gates: all_passed"])
        self.assertEqual(default_phases["final-review"]["routes"]["request-changes"], "fix-loop")
        self.assertEqual(default_phases["final-review"]["routes"]["approve"], "commit")
        self.assertIn("at least two active-host reviewer sub-agents", default_phases["final-review"]["prompt"])
        self.assertIn("reviewer-source: sub-agent", default_phases["final-review"]["prompt"])
        self.assertIn("close that sub-agent session", default_phases["final-review"]["prompt"])
        self.assertIn("## Overall", default_phases["final-review"]["prompt"])
        self.assertIn("verdict: approve", default_phases["final-review"]["prompt"])
        self.assertIn("verdict: request-changes", default_phases["final-review"]["prompt"])

    def test_python_runner_route_key_understands_gate_results(self) -> None:
        from agent_flow.runner import _gates_route_key, _route_key

        # gates 통과 JSON은 실제 command 결과가 있을 때만 green으로 정규화된다.
        self.assertEqual(_gates_route_key('{"passed": true}'), "default")
        self.assertEqual(_gates_route_key('{"passed": true, "results": []}'), "default")
        self.assertEqual(
            _gates_route_key('{"passed": true, "results": [{"command": "npm test", "passed": true, "output": "ok"}]}'),
            "green",
        )
        self.assertEqual(_gates_route_key('{"passed": false, "results": []}'), "request-changes")
        self.assertEqual(
            _gates_route_key('{"passed": false, "results": [{"id": "lint", "passed": true}]}'),
            "request-changes",
        )
        self.assertEqual(_route_key("status: failed"), "request-changes")
        self.assertEqual(_route_key("status: pass"), "green")
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
        with mock.patch.dict(os.environ, {"AGENT_FLOW_REVIEWERS": "claude,gemini"}, clear=True):
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
            self.assertIn("requires ## Overall with exactly one verdict", output.getvalue())

            (run_dir / "multi-review.md").write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: request-changes\n",
                encoding="utf-8",
            )
            self.assertEqual(runner._next_index(0, phase), (0, True))

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
            self.assertEqual(runner._next_index(0, phase), (0, True))

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
                    "--without-graphify",
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
            self.assertTrue((project_root / ".agent-flow" / "runs").is_dir())
            self.assertTrue((project_root / ".agent-flow" / "workflows" / "full-feature.yaml").is_file())
            self.assertTrue((project_root / ".agent-flow" / "bootstrap" / "AGENTS.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "bootstrap" / "CLAUDE.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "bootstrap" / "GEMINI.md").is_file())
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
            self.assertTrue((project_root / ".agent-flow" / "skills" / "domain-grill" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "grill-with-docs" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "product-brief" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "plan-reviewer" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "ddd-clean-architecture" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "architecture-reviewer" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "android-mvi-feature" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "android-module-creator" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "android-code-review" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "android-debugging" / "SKILL.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "skills" / "graphify" / "SKILL.md").is_file())
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
            self.assertTrue((project_root / ".Codex" / "agents" / "code-reviewer.md").is_file())
            self.assertTrue((project_root / ".Codex" / "context" / "tree.jsonl").is_file())
            self.assertIn(
                "verdict: approve | request-changes",
                (project_root / ".Codex" / "agents" / "code-reviewer.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "verdict: approve",
                (project_root / ".agent-flow" / "prompts" / "plan-review.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "data / domain / presentation",
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
                (project_root / "GEMINI.md").read_text(encoding="utf-8"),
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
            gitignore = (project_root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("graphify-out/manifest.json", gitignore)
            self.assertIn("graphify-out/cost.json", gitignore)

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
                    "--without-graphify",
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
                (node, installer, "install", "--without-graphify"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(initial.returncode, 0, initial.stderr)

            result = subprocess.run(
                (node, installer, "install", "--without-graphify"),
                cwd=worktree_root,
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
                (node, kit_installer, "install", "--without-graphify"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(initial.returncode, 0, initial.stderr)

            result = subprocess.run(
                (node, legacy_installer, "install", "--without-graphify"),
                cwd=worktree_root,
                env={**os.environ, "AGENT_FLOW_GRAPHIFY_DRY_RUN": "1"},
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
                    "--without-graphify",
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
                    "--without-graphify",
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
                    "--without-graphify",
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
                            "--without-graphify",
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
            env = {**os.environ, "HOME": str(home)}

            result = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"),
                    "install",
                    "--without-graphify",
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
            env = {**os.environ, "HOME": str(home), "AGENT_FLOW_GRAPHIFY_DRY_RUN": "1"}

            result = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-install.mjs"),
                    "install",
                    "--without-graphify",
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
            install = subprocess.run((node, cli, "install", "--without-graphify"), cwd=project_root, text=True, capture_output=True, check=False)
            self.assertEqual(install.returncode, 0, install.stderr)
            home = Path(temp_dir) / "home"
            worktree = home / ".codex" / "worktrees" / "slice" / "project"
            worktree.parent.mkdir(parents=True)
            subprocess.run(("git", "worktree", "add", "-q", "--detach", str(worktree), "HEAD"), cwd=project_root, check=True)
            env = {**os.environ, "HOME": str(home)}

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

    def test_node_installer_installs_graphify_by_default(self) -> None:
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
                env={**os.environ, "AGENT_FLOW_GRAPHIFY_DRY_RUN": "1"},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("graphify installed status=dry-run", result.stdout)
            kit = json.loads((project_root / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
            self.assertEqual(kit["graphify"]["package"], "graphifyy")
            self.assertEqual(kit["graphify"]["command"], "graphify")
            self.assertEqual(kit["graphify"]["status"], "dry-run")
            self.assertEqual(kit["graphify"]["platforms"], ["claude", "codex", "gemini"])
            self.assertEqual(kit["graphify"]["graph"]["status"], "dry-run")
            self.assertEqual(kit["graphify"]["graph"]["command"], "graphify .")

    def test_node_installer_can_skip_graphify(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            result = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"),
                    "install",
                    "--without-graphify",
                ),
                cwd=project_root,
                env={**os.environ, "AGENT_FLOW_GRAPHIFY_DRY_RUN": "1"},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            kit = json.loads((project_root / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
            self.assertNotIn("graphify", kit)

    def test_node_installer_reuses_existing_graphify_and_removes_duplicate_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_root = root / "project"
            fake_home = root / "home"
            fake_bin = root / "bin"
            project_root.mkdir()
            fake_home.mkdir()
            fake_bin.mkdir()
            graphify = fake_bin / "graphify"
            graphify.write_text(
                "#!/usr/bin/env sh\n"
                "set -eu\n"
                "if [ \"${1:-}\" = \"--help\" ]; then exit 0; fi\n"
                "if [ \"${1:-}\" = \"install\" ] && [ \"${2:-}\" = \"--help\" ]; then exit 0; fi\n"
                "if [ \"${1:-}\" = \"install\" ]; then\n"
                "  mkdir -p \"$HOME/.agents/skills/graphify\" \"$HOME/.codex/skills/graphify\" \"$HOME/.gemini/skills/graphify\" \"$HOME/.claude/skills/graphify\"\n"
                "  printf '%s\\n' '---' 'name: graphify' '---' > \"$HOME/.agents/skills/graphify/SKILL.md\"\n"
                "  printf '%s\\n' '---' 'name: graphify' '---' > \"$HOME/.codex/skills/graphify/SKILL.md\"\n"
                "  printf '%s\\n' '---' 'name: graphify' '---' > \"$HOME/.gemini/skills/graphify/SKILL.md\"\n"
                "  printf '%s\\n' '---' 'name: graphify' '---' > \"$HOME/.claude/skills/graphify/SKILL.md\"\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"${1:-}\" = \".\" ]; then\n"
                "  mkdir -p graphify-out\n"
                "  printf '{}\\n' > graphify-out/graph.json\n"
                "  exit 0\n"
                "fi\n"
                "exit 1\n",
                encoding="utf-8",
            )
            graphify.chmod(0o755)
            uv = fake_bin / "uv"
            uv_marker = fake_home / "uv-invoked"
            uv.write_text(
                "#!/usr/bin/env sh\n"
                "set -eu\n"
                ": > \"$AGENT_FLOW_TEST_UV_MARKER\"\n"
                "exit 0\n",
                encoding="utf-8",
            )
            uv.chmod(0o755)

            env = {
                **os.environ,
                "HOME": str(fake_home),
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "AGENT_FLOW_TEST_UV_MARKER": str(uv_marker),
            }
            env.pop("AGENT_FLOW_GRAPHIFY_DRY_RUN", None)
            node = _node_executable()
            result = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"),
                    "install",
                ),
                cwd=project_root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            kit = json.loads((project_root / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
            self.assertEqual(kit["graphify"]["installer"], "existing")
            self.assertEqual(kit["graphify"]["skill_location"], "~/.agents/skills/graphify")
            self.assertCountEqual(
                kit["graphify"]["removed_duplicate_skills"],
                ["~/.codex/skills/graphify", "~/.gemini/skills/graphify", "~/.claude/skills/graphify"],
            )
            self.assertEqual(kit["graphify"]["graph"]["status"], "generated")
            self.assertTrue((fake_home / ".agents" / "skills" / "graphify" / "SKILL.md").is_file())
            self.assertFalse((fake_home / ".codex" / "skills" / "graphify").exists())
            self.assertFalse((fake_home / ".gemini" / "skills" / "graphify").exists())
            self.assertFalse((fake_home / ".claude" / "skills" / "graphify").exists())
            self.assertTrue((project_root / "graphify-out" / "graph.json").is_file())

    def test_node_installer_does_not_reinstall_graphify_when_kit_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_root = root / "project"
            fake_home = root / "home"
            fake_bin = root / "bin"
            counter = root / "graphify-count"
            uv_marker = root / "uv-invoked"
            project_root.mkdir()
            fake_home.mkdir()
            fake_bin.mkdir()
            graphify = fake_bin / "graphify"
            graphify.write_text(
                "#!/usr/bin/env sh\n"
                "set -eu\n"
                "if [ \"${1:-}\" = \"--help\" ]; then exit 0; fi\n"
                "if [ \"${1:-}\" = \"install\" ] && [ \"${2:-}\" = \"--help\" ]; then exit 0; fi\n"
                "if [ \"${1:-}\" = \"install\" ]; then\n"
                "  printf 'install\\n' >> \"$AGENT_FLOW_TEST_GRAPHIFY_COUNTER\"\n"
                "  mkdir -p \"$HOME/.agents/skills/graphify\"\n"
                "  printf '%s\\n' '---' 'name: graphify' '---' > \"$HOME/.agents/skills/graphify/SKILL.md\"\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"${1:-}\" = \".\" ]; then\n"
                "  printf 'graph\\n' >> \"$AGENT_FLOW_TEST_GRAPHIFY_COUNTER\"\n"
                "  mkdir -p graphify-out\n"
                "  printf '{}\\n' > graphify-out/graph.json\n"
                "  exit 0\n"
                "fi\n"
                "exit 1\n",
                encoding="utf-8",
            )
            graphify.chmod(0o755)
            env = {
                **os.environ,
                "HOME": str(fake_home),
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "AGENT_FLOW_TEST_GRAPHIFY_COUNTER": str(counter),
                "AGENT_FLOW_TEST_UV_MARKER": str(uv_marker),
            }
            env.pop("AGENT_FLOW_GRAPHIFY_DRY_RUN", None)
            node = _node_executable()

            for _ in range(2):
                result = subprocess.run(
                    (
                        node,
                        str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"),
                        "install",
                    ),
                    cwd=project_root,
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            self.assertEqual(counter.read_text(encoding="utf-8").splitlines(), ["install", "install", "install", "graph"])
            kit = json.loads((project_root / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
            self.assertEqual(kit["graphify"]["status"], "reused")
            self.assertFalse(uv_marker.exists())

            legacy_project_root = root / "legacy-project"
            legacy_project_root.mkdir()
            (fake_home / ".codex" / "skills" / "graphify").mkdir(parents=True)
            (fake_home / ".gemini" / "skills" / "graphify").mkdir(parents=True)
            (fake_home / ".claude" / "skills" / "graphify").mkdir(parents=True)
            (fake_home / ".codex" / "skills" / "graphify" / "SKILL.md").write_text(
                "---\nname: graphify\n---\n",
                encoding="utf-8",
            )
            (fake_home / ".gemini" / "skills" / "graphify" / "SKILL.md").write_text(
                "---\nname: graphify\n---\n",
                encoding="utf-8",
            )
            (fake_home / ".claude" / "skills" / "graphify" / "SKILL.md").write_text(
                "---\nname: graphify\n---\n",
                encoding="utf-8",
            )
            legacy_result = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-install.mjs"),
                    "install",
                ),
                cwd=legacy_project_root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(legacy_result.returncode, 0, legacy_result.stderr)
            legacy_kit = json.loads((legacy_project_root / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
            self.assertEqual(legacy_kit["graphify"]["installer"], "existing")
            self.assertEqual(legacy_kit["graphify"]["skill_location"], "~/.agents/skills/graphify")
            self.assertCountEqual(
                legacy_kit["graphify"]["removed_duplicate_skills"],
                ["~/.codex/skills/graphify", "~/.gemini/skills/graphify", "~/.claude/skills/graphify"],
            )
            self.assertEqual(legacy_kit["graphify"]["graph"]["status"], "generated")
            self.assertFalse((fake_home / ".codex" / "skills" / "graphify").exists())
            self.assertFalse((fake_home / ".gemini" / "skills" / "graphify").exists())
            self.assertFalse((fake_home / ".claude" / "skills" / "graphify").exists())
            self.assertTrue((legacy_project_root / "graphify-out" / "graph.json").is_file())
            self.assertTrue((legacy_project_root / ".Codex" / "agents" / "code-reviewer.md").is_file())
            self.assertFalse(uv_marker.exists())

    def test_legacy_node_installer_installs_graphify_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "project"
            project_root.mkdir()
            node = _node_executable()
            result = subprocess.run(
                (
                    node,
                    str(Path(__file__).resolve().parents[1] / "bin" / "agent-flow-install.mjs"),
                    "install",
                ),
                cwd=project_root,
                env={**os.environ, "AGENT_FLOW_GRAPHIFY_DRY_RUN": "1"},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("graphify: dry-run", result.stdout)
            kit = json.loads((project_root / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
            self.assertEqual(kit["graphify"]["package"], "graphifyy")
            self.assertEqual(kit["graphify"]["command"], "graphify")
            self.assertEqual(kit["graphify"]["status"], "dry-run")
            self.assertEqual(kit["graphify"]["platforms"], ["claude", "codex", "gemini"])
            self.assertTrue((project_root / ".Codex" / "agents" / "code-reviewer.md").is_file())
            self.assertEqual(kit["graphify"]["graph"]["status"], "dry-run")
            self.assertEqual(kit["graphify"]["graph"]["command"], "graphify .")
            gitignore = (project_root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("graphify-out/manifest.json", gitignore)
            self.assertIn("graphify-out/cost.json", gitignore)

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
            gemini_bootstrap = project_root / ".agent-flow" / "bootstrap" / "GEMINI.md"
            skill = project_root / ".agent-flow" / "skills" / "full-feature-workflow" / "SKILL.md"
            rules = project_root / ".agent-flow" / "rules" / "workflow-contract.md"
            workflow.write_text("stale workflow\n", encoding="utf-8")
            prompt.write_text("stale prompt\n", encoding="utf-8")
            bootstrap.write_text("stale bootstrap\n", encoding="utf-8")
            claude_bootstrap.write_text("stale claude bootstrap\n", encoding="utf-8")
            gemini_bootstrap.write_text("stale gemini bootstrap\n", encoding="utf-8")
            skill.write_text("stale skill\n", encoding="utf-8")
            rules.write_text("stale rules\n", encoding="utf-8")

            self.assertEqual(subprocess.run((node, cli, "install"), cwd=project_root, check=False).returncode, 0)

            self.assertIn("id: full-feature", workflow.read_text(encoding="utf-8"))
            self.assertIn("Default reviewers are active-host sub-agents", workflow.read_text(encoding="utf-8"))
            self.assertIn("Gemini sub-agent in Gemini", workflow.read_text(encoding="utf-8"))
            self.assertIn("multi_review: true", workflow.read_text(encoding="utf-8"))
            self.assertIn("status: ci-failed", prompt.read_text(encoding="utf-8"))
            self.assertIn(
                "Default reviewers are active-host sub-agents",
                (project_root / ".agent-flow" / "prompts" / "multi-review.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "Gemini sub-agent in Gemini",
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
            self.assertIn('agent-flow run "<task>"', gemini_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("현재 사용 중인 CLI(활성 host)의 sub-agent 2개가 필수", bootstrap.read_text(encoding="utf-8"))
            self.assertIn("현재 사용 중인 CLI(활성 host)의 sub-agent 2개가 필수", claude_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("현재 사용 중인 CLI(활성 host)의 sub-agent 2개가 필수", gemini_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("활성 host가 아닌 추가 provider는 optional", bootstrap.read_text(encoding="utf-8"))
            self.assertIn("활성 host가 아닌 추가 provider는 optional", claude_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("활성 host가 아닌 추가 provider는 optional", gemini_bootstrap.read_text(encoding="utf-8"))
            self.assertNotIn("예: Claude/Gemini", bootstrap.read_text(encoding="utf-8"))
            self.assertNotIn("예: Claude/Gemini", claude_bootstrap.read_text(encoding="utf-8"))
            self.assertNotIn("예: Claude/Gemini", gemini_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("reviewer-source: sub-agent", bootstrap.read_text(encoding="utf-8"))
            self.assertIn("reviewer-source: sub-agent", claude_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("reviewer-source: sub-agent", gemini_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("sub-agent를 닫는다", bootstrap.read_text(encoding="utf-8"))
            self.assertIn("sub-agent를 닫는다", claude_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("sub-agent를 닫는다", gemini_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("## Overall", bootstrap.read_text(encoding="utf-8"))
            self.assertIn("## Overall", claude_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("## Overall", gemini_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("verdict: approve", bootstrap.read_text(encoding="utf-8"))
            self.assertIn("verdict: approve", claude_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("verdict: approve", gemini_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("verdict: request-changes", bootstrap.read_text(encoding="utf-8"))
            self.assertIn("verdict: request-changes", claude_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("verdict: request-changes", gemini_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("Full Feature Workflow", skill.read_text(encoding="utf-8"))
            self.assertIn("Workflow Contract", rules.read_text(encoding="utf-8"))
            self.assertIn("two active-host sub-agents", rules.read_text(encoding="utf-8"))
            self.assertIn("Gemini sub-agent in Gemini", rules.read_text(encoding="utf-8"))
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
                    "--without-graphify",
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
                "TODO: add grill-with-docs: complete before handoff\n"
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
            self.assertIn("grill-with-docs: complete", false_positive.stderr)

            artifact.write_text(
                "grill-with-docs: complete\n"
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
                "grill-with-docs: complete\n"
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
                "grill-with-docs: complete\n"
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
                "    grill-with-docs: complete\n"
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
                "grill-with-docs: complete\n"
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

            artifact.write_text(_node_phase_content("domain-grill"), encoding="utf-8")
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
                agent_flow / "bootstrap" / "GEMINI.md",
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
                "gates",
                "multi-review",
                "architecture-review",
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
                elif phase in {"plan-review", "architecture-review", "merge-approval"}:
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
                "gates",
                "multi-review",
                "architecture-review",
                "commit",
                "push-pr",
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                content = "verdict: approve\n" if phase in {"plan-review", "architecture-review"} else _node_phase_content(phase)
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
            self.assertIn("blocked: stale artifact", reused_comment_fix.stderr)
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
            os.utime(slice_plan, (1, 1))
            stale_slice_plan = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(stale_slice_plan.returncode, 1)
            self.assertIn("blocked: stale artifact", stale_slice_plan.stderr)

            slice_plan.write_text("updated slice-plan\n", encoding="utf-8")
            self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)
            plan_review.write_text("verdict: APPROVE\n", encoding="utf-8")
            self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)
            ddd = run_dir / _node_phase_artifact("ddd-design")
            ddd.write_text("ddd-design\n", encoding="utf-8")
            self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)
            for phase in [
                "worktree",
                "run-start",
                "red",
                "green",
                "refactor",
                "gates",
                "multi-review",
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text(_node_phase_content(phase), encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)

            architecture_review = run_dir / _node_phase_artifact("architecture-review")
            architecture_review.write_text("verdict: request-changes\n", encoding="utf-8")
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
            os.utime(refactor, (1, 1))
            stale_refactor = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(stale_refactor.returncode, 1)
            self.assertIn("blocked: stale artifact", stale_refactor.stderr)

            refactor.write_text("updated refactor\n", encoding="utf-8")
            self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)
            for phase, next_phase in [
                ("gates", "multi-review"),
                ("multi-review", "architecture-review"),
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                os.utime(artifact, (1, 1))
                stale_artifact = subprocess.run(
                    (node, cli, "run", "advance"),
                    cwd=project_root,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(stale_artifact.returncode, 1)
                self.assertIn("blocked: stale artifact", stale_artifact.stderr)

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

            os.utime(architecture_review, (1, 1))
            stale_architecture_review = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(stale_architecture_review.returncode, 1)
            self.assertIn("blocked: stale artifact", stale_architecture_review.stderr)

            architecture_review.write_text("verdict: approve\n", encoding="utf-8")
            approved = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)
            self.assertIn("Current phase: commit", approved.stdout)

    def test_node_gates_fail_routes_to_fix_loop_and_back(self) -> None:
        """gates fail → fix-loop → gates → multi-review 순환 테스트."""
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
            fix_loop_artifact.write_text("fix-loop\n", encoding="utf-8")
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
            self.assertIn("Current phase: multi-review", result.stdout)

    def test_node_multi_review_request_changes_routes_to_fix_loop(self) -> None:
        """multi-review request-changes → fix-loop → gates 순환 테스트."""
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
                "run-start", "red", "green", "refactor", "gates",
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                content = "verdict: approve\n" if phase == "plan-review" else _node_phase_content(phase)
                artifact.write_text(content, encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)

            state = json.loads((project_root / ".agent-flow" / "state" / "current-run.json").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "multi-review")

            mr_artifact = run_dir / _node_phase_artifact("multi-review")
            mr_artifact.write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: request-changes\n\n"
                "## Overall\n"
                "verdict: request-changes\n",
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
            fix_loop_artifact.write_text("fix-loop\n", encoding="utf-8")
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current phase: gates", result.stdout)

            gates_artifact = run_dir / _node_phase_artifact("gates")
            gates_artifact.write_text(_node_phase_content("gates"), encoding="utf-8")
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current phase: multi-review", result.stdout)

            mr_artifact.write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n"
                "## Overall\n"
                "verdict: approve\n",
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
                gates_artifact = run_dir / _node_phase_artifact("gates")
                gates_artifact.parent.mkdir(parents=True, exist_ok=True)
                gates_artifact.write_text('{"passed": false}\n', encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)
                fix_artifact = run_dir / _node_phase_artifact("fix-loop")
                fix_artifact.write_text(f"fix round {round_num + 1}\n", encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)

            gates_artifact = run_dir / _node_phase_artifact("gates")
            gates_artifact.write_text('{"passed": false}\n', encoding="utf-8")
            self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)
            fix_artifact = run_dir / _node_phase_artifact("fix-loop")
            fix_artifact.write_text("fix round 4\n", encoding="utf-8")
            result = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("fix-loop exceeded", result.stderr)

    def test_node_architecture_review_blocked_routes_to_refactor(self) -> None:
        """architecture-review blocked verdict → refactor 라우팅."""
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
                "run-start", "red", "green", "refactor", "gates", "multi-review",
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                content = "verdict: approve\n" if phase == "plan-review" else _node_phase_content(phase)
                artifact.write_text(content, encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)

            arch_artifact = run_dir / _node_phase_artifact("architecture-review")
            arch_artifact.write_text("verdict: blocked\n", encoding="utf-8")
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
                "run-start", "red", "green", "refactor", "gates",
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                content = "verdict: approve\n" if phase == "plan-review" else _node_phase_content(phase)
                artifact.write_text(content, encoding="utf-8")
                self.assertEqual(subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=False).returncode, 0)

            mr_artifact = run_dir / _node_phase_artifact("multi-review")
            mr_artifact.write_text(
                "reviewer verdict: approve\n## Reviewer\nverdict: approve\nverdict: approve\n",
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

            mr_artifact.write_text(
                "## Reviewer Notes\nverdict: approve\n\nverdict: approve\n",
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

            mr_artifact.write_text(
                "## Reviewer 1\nverdict: lgtm\n\n"
                "verdict: approve\n",
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

            mr_artifact.write_text(
                "Reviewer verdict: approve\nReviewer verdict: approve\n"
                "verdict: approve\n",
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
                mr_artifact.write_text(legacy_status, encoding="utf-8")
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
                mr_artifact.write_text(bad_source, encoding="utf-8")
                result = subprocess.run(
                    (node, cli, "run", "advance"),
                    cwd=project_root,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("at least 1 independent sub-agent reviewer verdict", result.stderr)

            mr_artifact.write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n",
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
            self.assertIn("matching reviewer verdicts and overall verdict", result.stderr)

            mr_artifact.write_text(
                "## Reviewer 1\n\nreviewer-source: sub-agent\nverdict: approve\n",
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
            self.assertIn("matching reviewer verdicts and overall verdict", result.stderr)

            mr_artifact.write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\n### Findings\nverdict: approve\n",
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
            self.assertIn("matching reviewer verdicts and overall verdict", result.stderr)

            mr_artifact.write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\n# Code Review\nverdict: approve\n",
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
            self.assertIn("matching reviewer verdicts and overall verdict", result.stderr)

            mr_artifact.write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Overall\nstatus: passed\n",
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
            self.assertIn("matching reviewer verdicts and overall verdict", result.stderr)

            mr_artifact.write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Overall\nverdict: approve\nverdict: request-changes\n",
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

            mr_artifact.write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Overall\nverdict: request-changes\n\n## Overall\nverdict: approve\n",
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

            mr_artifact.write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Final\nverdict: approve\n",
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
            self.assertIn("matching reviewer verdicts and overall verdict", result.stderr)

            mr_artifact.write_text(
                "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
                "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n"
                "## Overall\nverdict: approve\n",
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

    def test_is_git_repo_treats_missing_git_as_non_git(self) -> None:
        from agent_flow.cli import _is_git_repo

        with tempfile.TemporaryDirectory() as temp_dir:
            # git 실행 파일이 없는 환경에서도 run/start가 non-git fallback으로 이어져야 한다.
            with mock.patch("agent_flow.cli.subprocess.run", side_effect=FileNotFoundError):
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
                "gemini-cli unavailable command=gemini",
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
                "TODO: grill-with-docs: complete\n"
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
        # npm 기반 TypeScript profile은 subprocess argv list로 검증 명령을 보관한다.
        typescript = load_profile("typescript")
        self.assertEqual(typescript.gates[1].gate_id, "typecheck")
        self.assertEqual(typescript.gates[1].command, ("npx", "tsc", "--noEmit"))
        self.assertEqual(load_profile("nextjs").gates[1].command, ("npm", "run", "build"))
        self.assertEqual(load_profile("android").profile_id, "android")

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
            scripts = root / "scripts"
            scripts.mkdir()
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
            self.assertEqual(output.getvalue().strip(), "generic: 1/1 gates passed")
            self.assertTrue((run_dir / "gate-results.json").is_file())

    def test_gates_cli_resolves_relative_run_dir_against_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            cwd = Path(temp_dir) / "caller"
            root.mkdir()
            cwd.mkdir()
            scripts = root / "scripts"
            scripts.mkdir()
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
            self.assertTrue((root / ".agent-flow" / "runs" / "manual" / "gate-results.json").is_file())
            self.assertFalse((cwd / ".agent-flow" / "runs" / "manual" / "gate-results.json").exists())

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
            ("gemini-generalist", "resource exhausted: quota exceeded; retry later", "gemini"),
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
            with mock.patch("agent_flow.core.worktrees.subprocess.run", side_effect=OSError("no git")):
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
        self.assertIn("기준 worktree", result.stdout)

        allowed = subprocess.run(
            ("bash", str(script)),
            input=json.dumps({"tool_input": {"command": "git checkout -- README.md"}}),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(allowed.returncode, 0)

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
        "slice-plan": Path("artifacts/slice-plan.md"),
        "plan-review": Path("artifacts/plan-review.md"),
        "ddd-design": Path("artifacts/ddd-design.md"),
        "worktree": Path("artifacts/worktree.md"),
        "run-start": Path("artifacts/run-start.md"),
        "red": Path("artifacts/red.log"),
        "green": Path("artifacts/green.log"),
        "refactor": Path("artifacts/refactor.md"),
        "gates": Path("artifacts/gate-results.json"),
        "multi-review": Path("artifacts/multi-review.md"),
        "fix-loop": Path("artifacts/fix-loop.md"),
        "architecture-review": Path("artifacts/architecture-review.md"),
        "commit": Path("artifacts/commit.md"),
        "push-pr": Path("artifacts/push-pr.md"),
        "pr-watch": Path("artifacts/pr-watch.md"),
        "pr-comment-fix": Path("artifacts/pr-comment-fix.md"),
        "pr-ci-fix": Path("artifacts/pr-ci-fix.md"),
        "merge-approval": Path("artifacts/merge-approval.md"),
        "merge": Path("artifacts/merge.md"),
        "handoff": Path("artifacts/handoff.md"),
    }
    return artifacts[phase]


def _node_phase_content(phase: str, prefix: str = "") -> str:
    content = f"{prefix}{phase}\n"
    if phase == "domain-grill":
        return (
            content
            + "## Completion Gate\n"
            + "grill-with-docs: complete\n"
            + "shared_understanding: reached\n"
            + "context_docs_checked: true\n"
            + "context_docs_updated: not_needed\n"
        )
    if phase == "gates":
        return '{"passed": true, "results": [{"id": "test", "command": "npm test", "passed": true, "output": "ok"}]}\n'
    if phase == "multi-review":
        return (
            "## Reviewer 1\nreviewer-source: sub-agent\nreviewer-1 verdict: approve\n\n"
            "## Reviewer 2\nreviewer-source: sub-agent\nreviewer-2 verdict: approve\n\n"
            "## Overall\n"
            "verdict: approve\n"
        )
    return content


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
        "gates",
        "multi-review",
        "architecture-review",
        "commit",
        "push-pr",
    ]:
        artifact = run_dir / _node_phase_artifact(phase)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        content = "verdict: approve\n" if phase in {"plan-review", "architecture-review"} else _node_phase_content(phase)
        artifact.write_text(content, encoding="utf-8")
        subprocess.run((node, cli, "run", "advance"), cwd=project_root, check=True)
    return run_dir


def _node_epoch_seconds(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


if __name__ == "__main__":
    unittest.main()
