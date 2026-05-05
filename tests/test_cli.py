from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
import subprocess

from agent_flow.cli import main
from agent_flow.adapters.templates import PromptContext, render_stage_prompt
from agent_flow.core.gates import GateCommand, run_gate
from agent_flow.core.profiles import load_profile
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

    def test_detect_profile_defaults_to_generic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["detect-profile", "--root", temp_dir]), 0)
            self.assertEqual(output.getvalue().strip(), "generic")

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


def _init_git_repo(root: Path) -> None:
    subprocess.run(("git", "init", "-q"), cwd=root, check=True)
    subprocess.run(("git", "config", "user.email", "test@example.com"), cwd=root, check=True)
    subprocess.run(("git", "config", "user.name", "Test User"), cwd=root, check=True)
    (root / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(("git", "add", "README.md"), cwd=root, check=True)
    subprocess.run(("git", "commit", "-q", "-m", "init"), cwd=root, check=True)


if __name__ == "__main__":
    unittest.main()
