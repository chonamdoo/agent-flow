from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD_HOOK = REPO_ROOT / "scripts" / "hooks" / "guard-worktree-write.py"
STOP_HOOK = REPO_ROOT / "scripts" / "hooks" / "show-phase-status.sh"


def _init_git_repo(root: Path) -> None:
    subprocess.run(("git", "init", "-q", "-b", "main"), cwd=root, check=True)
    subprocess.run(("git", "config", "user.email", "test@example.com"), cwd=root, check=True)
    subprocess.run(("git", "config", "user.name", "Test User"), cwd=root, check=True)
    (root / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(("git", "add", "README.md"), cwd=root, check=True)
    subprocess.run(("git", "commit", "-q", "-m", "init"), cwd=root, check=True)


def _write_manifest(root: Path, workspace: Path, run_id: str) -> Path:
    relative_run_dir = Path(".agent-flow") / "runs" / "default" / run_id
    manifest = root / relative_run_dir / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "workflow": "default",
                "run_dir": relative_run_dir.as_posix(),
                "status": "running",
                "phase": "implement",
                "workspace_root": str(workspace),
            }
        ),
        encoding="utf-8",
    )
    return manifest


def _write_python_manifest(root: Path, run_id: str) -> Path:
    relative_run_dir = Path(".agent-flow") / "runs" / "default" / run_id
    manifest = root / relative_run_dir / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "workflow_id": "default",
                "run_dir": relative_run_dir.as_posix(),
                "status": "running",
                "task": "structured Python run",
                "created_at": "2026-01-01T00:00:00+00:00",
                "worktree_mode": "disabled",
            }
        ),
        encoding="utf-8",
    )
    return manifest


def _guard(root: Path, command: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (str(GUARD_HOOK),),
        cwd=cwd or root,
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        text=True,
        capture_output=True,
        check=False,
    )


def _stop(root: Path) -> subprocess.CompletedProcess[str]:
    fake_bin = root.parent / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    fake_cli = fake_bin / "agent-flow"
    fake_cli.write_text("#!/bin/sh\nprintf 'status: running\\n'\n", encoding="utf-8")
    fake_cli.chmod(0o755)
    _install_pinned_status_runtime(root, "#!/bin/sh\nprintf 'status: running\\n'\n")
    return subprocess.run(
        ("/bin/bash", str(STOP_HOOK)),
        cwd=root,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        check=False,
    )


def _install_pinned_status_runtime(root: Path, launcher_source: str) -> Path:
    launcher = root / ".agent-flow" / "bin" / "agent-flow"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(launcher_source, encoding="utf-8")
    launcher.chmod(0o755)
    runtime_root = root / ".agent-flow" / "runtime" / "node"
    entrypoint = runtime_root / "bin" / "agent-flow-kit.mjs"
    entrypoint.parent.mkdir(parents=True, exist_ok=True)
    entrypoint.write_text("// test runtime\n", encoding="utf-8")
    python_runtime_root = root / ".agent-flow" / "runtime" / "python"
    python_package = python_runtime_root / "agent_flow"
    python_package.mkdir(parents=True, exist_ok=True)
    (python_package / "__init__.py").write_text("", encoding="utf-8")
    runtime_hash = hashlib.sha256()
    for file in sorted(candidate for candidate in runtime_root.rglob("*") if candidate.is_file()):
        runtime_hash.update(file.relative_to(runtime_root).as_posix().encode("utf-8"))
        runtime_hash.update(b"\0")
        runtime_hash.update(file.read_bytes())
        runtime_hash.update(b"\0")
    python_runtime_hash = hashlib.sha256()
    for file in sorted(candidate for candidate in python_runtime_root.rglob("*") if candidate.is_file()):
        python_runtime_hash.update(file.relative_to(python_runtime_root).as_posix().encode("utf-8"))
        python_runtime_hash.update(b"\0")
        python_runtime_hash.update(file.read_bytes())
        python_runtime_hash.update(b"\0")
    contract = {
        "version": 2,
        "launcher": {
            "path": ".agent-flow/bin/agent-flow",
            "sha256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
        },
        "node_runtime": {
            "root": ".agent-flow/runtime/node",
            "entrypoint": ".agent-flow/runtime/node/bin/agent-flow-kit.mjs",
            "tree_hash": runtime_hash.hexdigest(),
        },
        "python_runtime": {
            "root": ".agent-flow/runtime/python",
            "tree_hash": python_runtime_hash.hexdigest(),
        },
    }
    commitment = hashlib.sha256(
        json.dumps({"version": 2, "contract": contract}, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    (root / ".agent-flow" / "kit.json").write_text(
        json.dumps(
            {
                "node_runtime": {
                    "path": ".agent-flow/runtime/node/bin/agent-flow-kit.mjs",
                    "tree_hash": contract["node_runtime"]["tree_hash"],
                },
                "python_runtime": {
                    "path": ".agent-flow/runtime/python",
                    "tree_hash": contract["python_runtime"]["tree_hash"],
                },
                "project_runtime_contract": contract,
                "project_runtime_contract_commitment_version": 2,
                "project_runtime_contract_commitment": commitment,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return launcher


class HookActiveManifestRecoveryTest(unittest.TestCase):
    def test_leader_structured_python_manifest_blocks_a_second_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            _init_git_repo(root)
            _write_python_manifest(root, "python-active")

            result = _guard(root, "agent-flow run another-task")

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("outside the disabled-mode leader workspace", result.stderr)

    def test_git_private_relative_node_run_dir_is_resolved_from_its_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            _init_git_repo(root)
            workspace = Path(temp_dir) / "feature"
            subprocess.run(
                ("git", "worktree", "add", "-q", "-b", "feat/relative", str(workspace), "main"),
                cwd=root,
                check=True,
            )
            state_root = root / ".git/agent-flow/worktrees/feat-relative"
            state_root.mkdir(parents=True)
            (state_root / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "feat-relative",
                        "branch": "feat/relative",
                        "path": str(workspace),
                        "leader_root": str(root),
                    }
                ),
                encoding="utf-8",
            )
            manifest = _write_manifest(state_root, workspace, "relative")
            pointer = state_root / ".agent-flow/state/current-run.json"
            pointer.parent.mkdir(parents=True)
            pointer.write_bytes(manifest.read_bytes())

            result = _guard(root, "git status --short", cwd=workspace)

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_pointer_recovers_manifest_for_guard_and_stop_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            _init_git_repo(root)
            workspace = Path(temp_dir) / "feature"
            subprocess.run(
                ("git", "worktree", "add", "-q", "-b", "feat/recovery", str(workspace), "main"),
                cwd=root,
                check=True,
            )
            _write_manifest(root, workspace, "active")

            leader_write = _guard(root, "touch leader.txt")
            self.assertEqual(leader_write.returncode, 2, leader_write.stderr)
            self.assertIn("active worktree", leader_write.stderr)
            outside_write = _guard(root, f"touch {Path(temp_dir) / 'outside.txt'}", cwd=workspace)
            self.assertEqual(outside_write.returncode, 2, outside_write.stderr)
            self.assertIn("outside the active worktree", outside_write.stderr)
            second_start = _guard(root, "agent-flow run another-task")
            self.assertEqual(second_start.returncode, 2, second_start.stderr)

            fake_bin = Path(temp_dir) / "bin"
            fake_bin.mkdir()
            fake_cli = fake_bin / "agent-flow"
            fake_cli.write_text(
                "#!/bin/sh\nprintf 'cwd: %s\\nstatus: running\\n' \"$PWD\"\n",
                encoding="utf-8",
            )
            fake_cli.chmod(0o755)
            _install_pinned_status_runtime(
                root,
                "#!/bin/sh\nprintf 'cwd: %s\\nstatus: running\\n' \"$PWD\"\n",
            )
            stop = subprocess.run(
                ("/bin/bash", str(STOP_HOOK)),
                cwd=root,
                env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(stop.returncode, 0, stop.stderr)
            payload = json.loads(stop.stdout)
            self.assertIn(f"cwd: {workspace.resolve()}", payload["systemMessage"])
            self.assertIn("status: running", payload["systemMessage"])

    def test_corrupt_pointer_recovers_the_single_active_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            _init_git_repo(root)
            workspace = Path(temp_dir) / "feature"
            subprocess.run(
                ("git", "worktree", "add", "-q", "-b", "feat/corrupt-pointer", str(workspace), "main"),
                cwd=root,
                check=True,
            )
            _write_manifest(root, workspace, "active")
            pointer = root / ".agent-flow" / "state" / "current-run.json"
            pointer.parent.mkdir(parents=True)
            pointer.write_text("{broken", encoding="utf-8")

            result = _guard(root, "agent-flow run another-task")

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("active run state is unreadable", result.stderr)

    def test_terminal_pointer_recovers_the_single_active_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            _init_git_repo(root)
            workspace = Path(temp_dir) / "feature"
            subprocess.run(
                ("git", "worktree", "add", "-q", "-b", "feat/stale-pointer", str(workspace), "main"),
                cwd=root,
                check=True,
            )
            _write_manifest(root, workspace, "active")
            pointer = root / ".agent-flow" / "state" / "current-run.json"
            pointer.parent.mkdir(parents=True)
            pointer.write_text(
                json.dumps(
                    {
                        "run_id": "old",
                        "workflow": "default",
                        "run_dir": ".agent-flow/runs/default/old",
                        "status": "complete",
                        "phase": "complete",
                        "workspace_root": str(root),
                    }
                ),
                encoding="utf-8",
            )

            result = _guard(root, "agent-flow run another-task")

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("active worktree", result.stderr)
            self.assertNotIn("does not match", result.stderr)

    def test_multiple_or_malformed_manifests_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            _init_git_repo(root)
            workspace = Path(temp_dir) / "feature"
            subprocess.run(
                ("git", "worktree", "add", "-q", "-b", "feat/ambiguous", str(workspace), "main"),
                cwd=root,
                check=True,
            )
            _write_manifest(root, workspace, "one")
            second_manifest = _write_manifest(root, workspace, "two")

            multiple = _guard(root, "git status --short")
            self.assertEqual(multiple.returncode, 2, multiple.stderr)
            self.assertIn("multiple Node run manifests", multiple.stderr)
            multiple_stop = _stop(root)
            self.assertEqual(multiple_stop.returncode, 0, multiple_stop.stderr)
            self.assertIn(
                "blocked: multiple Node run manifests",
                json.loads(multiple_stop.stdout)["systemMessage"],
            )

            second_manifest.write_text("{broken", encoding="utf-8")
            malformed = _guard(root, "git status --short")
            self.assertEqual(malformed.returncode, 2, malformed.stderr)
            self.assertIn("run manifest is unreadable", malformed.stderr)
            malformed_stop = _stop(root)
            self.assertEqual(malformed_stop.returncode, 0, malformed_stop.stderr)
            self.assertIn(
                "blocked: Node run manifest is unreadable",
                json.loads(malformed_stop.stdout)["systemMessage"],
            )

    def test_active_pointer_manifest_identity_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            _init_git_repo(root)
            workspace = Path(temp_dir) / "feature"
            subprocess.run(
                ("git", "worktree", "add", "-q", "-b", "feat/mismatch", str(workspace), "main"),
                cwd=root,
                check=True,
            )
            _write_manifest(root, workspace, "manifest-run")
            pointer = root / ".agent-flow" / "state" / "current-run.json"
            pointer.parent.mkdir(parents=True)
            pointer.write_text(
                json.dumps(
                    {
                        "run_id": "pointer-run",
                        "workflow": "default",
                        "run_dir": ".agent-flow/runs/default/pointer-run",
                        "status": "running",
                        "phase": "implement",
                        "workspace_root": str(workspace),
                    }
                ),
                encoding="utf-8",
            )

            result = _guard(root, "git status --short")

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("pointer does not match", result.stderr)


if __name__ == "__main__":
    unittest.main()
