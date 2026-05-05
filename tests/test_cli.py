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
from concurrent.futures import ThreadPoolExecutor
from importlib import resources

from agent_flow.cli import main
from agent_flow.adapters.templates import PromptContext, render_stage_prompt
from agent_flow.core.gates import GateCommand, run_gate
from agent_flow.core.profiles import load_profile
from agent_flow.core.team import ShutdownSignal
from agent_flow.core.workflow import _stage_from_payload
from agent_flow.core.worktrees import plan_worktree


class CliTest(unittest.TestCase):
    def test_init_creates_agent_flow_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertEqual(main(["init", "--root", str(root)]), 0)
            self.assertTrue((root / ".agent-flow" / "runs").is_dir())
            self.assertTrue((root / ".agent-flow" / "handoffs").is_dir())

    def test_start_creates_manifest_and_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
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
            self.assertTrue((project_root / ".agent-flow" / "prompts" / "pr-watch.md").is_file())
            self.assertTrue((project_root / ".agent-flow" / "prompts" / "merge.md").is_file())
            self.assertIn(
                "status: ci-failed",
                (project_root / ".agent-flow" / "prompts" / "pr-watch.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "npx github:chonamdoo/agent-flow run next",
                (project_root / "AGENTS.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "npx github:chonamdoo/agent-flow run next",
                (project_root / "CLAUDE.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "npx github:chonamdoo/agent-flow run next",
                (project_root / "GEMINI.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "npx github:chonamdoo/agent-flow run next",
                (project_root / ".agent-flow" / "bootstrap" / "AGENTS.md").read_text(encoding="utf-8"),
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
            self.assertIn("status: ci-failed", prompt.read_text(encoding="utf-8"))
            self.assertIn("npx github:chonamdoo/agent-flow run next", bootstrap.read_text(encoding="utf-8"))
            self.assertIn("npx github:chonamdoo/agent-flow run next", claude_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("npx github:chonamdoo/agent-flow run next", gemini_bootstrap.read_text(encoding="utf-8"))
            self.assertIn("Full Feature Workflow", skill.read_text(encoding="utf-8"))
            self.assertIn("Workflow Contract", rules.read_text(encoding="utf-8"))

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

    def test_node_installer_matches_project_profile_detection(self) -> None:
        cases = [
            ("nextjs", {"package.json": '{"dependencies":{"next":"latest"}}\n'}),
            ("react-native", {"package.json": '{"dependencies":{"react-native":"latest"}}\n'}),
            ("python", {"pyproject.toml": "[project]\nname='demo'\n"}),
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
            self.assertIn("Current phase: prd", start.stdout)

            blocked = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(blocked.returncode, 1)
            self.assertIn("blocked: missing artifact", blocked.stderr)

            artifact = project_root / ".agent-flow" / "runs" / "full-feature" / "r1" / "artifacts" / "prd.md"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("PRD\n", encoding="utf-8")

            advanced = subprocess.run(
                (node, cli, "run", "advance"),
                cwd=project_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(advanced.returncode, 0, advanced.stderr)
            self.assertIn("Current phase: slice-plan", advanced.stdout)
            state = json.loads((project_root / ".agent-flow" / "state" / "current-run.json").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "slice-plan")

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
                "prd",
                "slice-plan",
                "worktree",
                "run-start",
                "red",
                "green",
                "refactor",
                "gates",
                "multi-review",
                "fix-loop",
                "commit",
                "push-pr",
                "pr-watch",
                "merge",
                "handoff",
            ]
            run_dir = project_root / ".agent-flow" / "runs" / "full-feature" / "r1"
            for index, phase in enumerate(expected_phases):
                state = json.loads((project_root / ".agent-flow" / "state" / "current-run.json").read_text(encoding="utf-8"))
                self.assertEqual(state["phase"], phase)
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                content = "status: green\n" if phase == "pr-watch" else f"{phase}\n"
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
                "prd",
                "slice-plan",
                "worktree",
                "run-start",
                "red",
                "green",
                "refactor",
                "gates",
                "multi-review",
                "fix-loop",
                "commit",
                "push-pr",
            ]:
                artifact = run_dir / _node_phase_artifact(phase)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text(f"{phase}\n", encoding="utf-8")
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
            run_dir = root / ".agent-flow" / "runs" / "development" / "r1"
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["worktree"],
                {
                    "name": "slice-a",
                    "branch": "agent-flow/slice-a",
                    "path": str(root.resolve() / ".agent-flow" / "worktrees" / "slice-a"),
                },
            )
            self.assertTrue((root / ".agent-flow" / "worktrees" / "slice-a").is_dir())

    def test_start_worktree_rejects_dirty_leader_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            (root / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
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
                )
            self.assertFalse((root / ".agent-flow" / "runs" / "development").exists())

    def test_start_worktree_failure_does_not_leave_orphan_worktree(self) -> None:
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

            with self.assertRaises(FileExistsError):
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
                )
            self.assertFalse((root / ".agent-flow" / "worktrees" / "slice-a").exists())

    def test_start_worktree_write_failure_cleans_run_and_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)

            with mock.patch("agent_flow.core.state._write_json", side_effect=OSError("manifest failed")):
                with self.assertRaises(OSError):
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
                    )
            self.assertFalse((root / ".agent-flow" / "runs" / "development" / "r1").exists())
            self.assertFalse((root / ".agent-flow" / "worktrees" / "slice-a").exists())

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
                        "custom/slice-a",
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
                (root / ".agent-flow" / "runs" / "development" / "r1" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["worktree"]["branch"], "custom/slice-a")

    def test_start_worktree_can_use_existing_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            subprocess.run(("git", "branch", "custom/slice-a"), cwd=root, check=True)

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
                        "custom/slice-a",
                    ]
                ),
                0,
            )
            manifest = json.loads(
                (root / ".agent-flow" / "runs" / "development" / "r1" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["worktree"]["branch"], "custom/slice-a")
            self.assertTrue((root / ".agent-flow" / "worktrees" / "slice-a").is_dir())

    def test_detect_profile_defaults_to_generic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["detect-profile", "--root", temp_dir]), 0)
            self.assertEqual(output.getvalue().strip(), "generic")

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
            root = Path(temp_dir)
            self.assertEqual(
                main(["start", "review", "--root", str(root), "--task", "review demo", "--run-id", "r1"]),
                0,
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["status", "--root", str(root)]), 0)
            self.assertEqual(output.getvalue().strip(), "review r1 running")

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
        self.assertEqual(profile.gates[0].gate_id, "test")
        self.assertEqual(profile.gates[0].command, ("npm", "test"))

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
            self.assertEqual(output.getvalue().strip(), "generic: 0/0 gates passed")
            self.assertTrue((run_dir / "gate-results.json").is_file())

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
            self.assertEqual(plan.name, "implement-login")
            self.assertEqual(plan.branch, "agent-flow/implement-login")
            self.assertEqual(plan.path, root / ".agent-flow" / "worktrees" / "implement-login")

    def test_worktree_status_reports_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["worktree", "status", "--root", temp_dir, "--name", "missing"]),
                    0,
                )
            self.assertIn("missing agent-flow/missing", output.getvalue())
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
            worktree = root / ".agent-flow" / "worktrees" / "slice-a"
            self.assertTrue(worktree.is_dir())
            self.assertTrue((worktree / "manifest.json").is_file())
            self.assertIn("slice-a agent-flow/slice-a", output.getvalue())

    def test_worktree_create_rejects_dirty_leader_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            (root / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                main(["worktree", "create", "--root", str(root), "--name", "dirty"])

    def test_worktree_create_allows_untracked_agent_flow_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_git_repo(root)
            self.assertEqual(main(["init", "--root", str(root)]), 0)
            self.assertEqual(
                main(["worktree", "create", "--root", str(root), "--name", "after-init"]),
                0,
            )
            self.assertTrue((root / ".agent-flow" / "worktrees" / "after-init").is_dir())

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
        "prd": Path("artifacts/prd.md"),
        "slice-plan": Path("artifacts/slice-plan.md"),
        "worktree": Path("artifacts/worktree.md"),
        "run-start": Path("artifacts/run-start.md"),
        "red": Path("artifacts/red.log"),
        "green": Path("artifacts/green.log"),
        "refactor": Path("artifacts/refactor.md"),
        "gates": Path("gate-results.json"),
        "multi-review": Path("artifacts/multi-review.md"),
        "fix-loop": Path("artifacts/fix-loop.md"),
        "commit": Path("artifacts/commit.md"),
        "push-pr": Path("artifacts/push-pr.md"),
        "pr-watch": Path("artifacts/pr-watch.md"),
        "pr-comment-fix": Path("artifacts/pr-comment-fix.md"),
        "pr-ci-fix": Path("artifacts/pr-ci-fix.md"),
        "merge": Path("artifacts/merge.md"),
        "handoff": Path("artifacts/handoff.md"),
    }
    return artifacts[phase]


def _node_epoch_seconds(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


if __name__ == "__main__":
    unittest.main()
