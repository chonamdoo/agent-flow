from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WRITE_HOOK = ROOT / "scripts" / "hooks" / "guard-worktree-write.py"
STOP_HOOK = ROOT / "scripts" / "hooks" / "show-phase-status.sh"


class PinnedLauncherGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "repo"
        self.root.mkdir()
        _init_git_repo(self.root)
        self.worktree = self.root / ".agent-flow" / "worktrees" / "feat-launcher"
        subprocess.run(
            (
                "git",
                "worktree",
                "add",
                "-q",
                "-b",
                "feat/launcher",
                str(self.worktree),
                "main",
            ),
            cwd=self.root,
            check=True,
        )
        _write_node_run(self.root, self.worktree, status="running")
        self.launcher = _install_launcher_bridge(self.root, self.worktree)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_inactive_leader_accepts_pinned_start_path_forms_across_hosts(self) -> None:
        _write_node_run(self.root, self.worktree, status="complete")
        for launcher in _leader_launcher_forms(self.root):
            for suffix in ('run "new task"', 'run start --task "new task"'):
                command = f"{launcher} {suffix}"
                for payload in _host_payloads(command):
                    with self.subTest(command=command, payload=payload):
                        self.assertEqual(
                            _run_write_hook(self.root, payload).returncode,
                            0,
                        )

    def test_active_leader_accepts_status_and_advance_but_rejects_start(self) -> None:
        for launcher in _leader_launcher_forms(self.root):
            for suffix in ("status", "run status", "run advance"):
                command = f"{launcher} {suffix}"
                for payload in _host_payloads(command):
                    with self.subTest(command=command, payload=payload):
                        self.assertEqual(
                            _run_write_hook(self.root, payload).returncode,
                            0,
                        )
            for suffix in ('run "another task"', 'run start --task "another task"'):
                command = f"{launcher} {suffix}"
                for payload in _host_payloads(command):
                    with self.subTest(command=command, payload=payload):
                        self.assertEqual(
                            _run_write_hook(self.root, payload).returncode,
                            2,
                        )

    def test_active_worktree_accepts_bridge_status_and_advance_but_rejects_start(self) -> None:
        for launcher in _worktree_launcher_forms(self.root, self.worktree):
            for suffix in ("status", "run status", "run advance"):
                command = f"{launcher} {suffix}"
                for payload in _host_payloads(command):
                    with self.subTest(command=command, payload=payload):
                        self.assertEqual(
                            _run_write_hook(self.worktree, payload).returncode,
                            0,
                        )
            for suffix in ('run "another task"', 'run start --task "another task"'):
                command = f"{launcher} {suffix}"
                for payload in _host_payloads(command):
                    with self.subTest(command=command, payload=payload):
                        self.assertEqual(
                            _run_write_hook(self.worktree, payload).returncode,
                            2,
                        )

    def test_path_and_multi_command_bypasses_remain_blocked(self) -> None:
        leader_launcher = _leader_launcher_forms(self.root)[0]
        worktree_launcher = _worktree_launcher_forms(self.root, self.worktree)[0]
        commands = (
            (self.root, f"{leader_launcher} status && touch leader-bypass.txt"),
            (self.root, f"{leader_launcher} run advance; {leader_launcher} run another-task"),
            (self.root, f"PATH=/tmp {leader_launcher} status"),
            (
                self.worktree,
                f"{worktree_launcher} status && touch {shlex.quote(str(self.root / 'leader-bypass.txt'))}",
            ),
            (
                self.worktree,
                f"bash -lc {shlex.quote(f'{worktree_launcher} status; {worktree_launcher} run another-task')}",
            ),
            (
                self.worktree,
                f"NODE_OPTIONS=--require=/tmp/evil nohup {worktree_launcher} status",
            ),
            (
                self.worktree,
                f"PATH=/tmp bash -lc {shlex.quote(f'{worktree_launcher} status')}",
            ),
            (self.worktree, "agent-flow run another-task"),
        )
        for cwd, command in commands:
            for payload in _host_payloads(command):
                with self.subTest(cwd=cwd, command=command, payload=payload):
                    self.assertEqual(_run_write_hook(cwd, payload).returncode, 2)

    def test_installed_launcher_blocks_bare_stale_path_commands_across_hosts(self) -> None:
        tool_bin = Path(self.temp_dir.name) / "stale-guard-bin"
        _seed_hook_path(tool_bin)
        for command_name in ("agent-flow", "agent-flow-kit"):
            _write_executable(tool_bin / command_name, "#!/bin/sh\nexit 0\n")
        env = {**os.environ, "PATH": str(tool_bin)}
        for cwd in (self.root, self.worktree):
            for command in (
                "agent-flow status",
                "agent-flow run advance",
                "agent-flow-kit status",
                "agent-flow-kit run advance",
                "agent-flow-python status",
            ):
                for payload in _host_payloads(command):
                    with self.subTest(cwd=cwd, command=command, payload=payload):
                        self.assertEqual(
                            _run_write_hook(cwd, payload, env=env).returncode,
                            2,
                        )
            for launcher in (
                "./.agent-flow/bin/agent-flow",
                shlex.quote(str(self.root / ".agent-flow" / "bin" / "agent-flow")),
            ):
                for suffix in ("status", "run advance"):
                    command = f"{launcher} {suffix}"
                    for payload in _host_payloads(command):
                        with self.subTest(cwd=cwd, command=command, payload=payload):
                            self.assertEqual(
                                _run_write_hook(cwd, payload, env=env).returncode,
                                0,
                            )

    def test_installed_missing_launcher_blocks_bare_commands(self) -> None:
        self.launcher.unlink()
        for cwd in (self.root, self.worktree):
            for command in (
                "agent-flow status",
                "agent-flow run advance",
                "agent-flow-kit status",
                "agent-flow-kit run advance",
                "agent-flow-python status",
            ):
                for payload in _host_payloads(command):
                    with self.subTest(cwd=cwd, command=command, payload=payload):
                        self.assertEqual(_run_write_hook(cwd, payload).returncode, 2)

    def test_bare_commands_remain_compatible_only_without_kit_metadata(self) -> None:
        self.launcher.unlink()
        (self.root / ".agent-flow" / "kit.json").unlink()
        for cwd in (self.root, self.worktree):
            for command in (
                "agent-flow status",
                "agent-flow run advance",
                "agent-flow-kit status",
                "agent-flow-kit run advance",
            ):
                for payload in _host_payloads(command):
                    with self.subTest(cwd=cwd, command=command, payload=payload):
                        self.assertEqual(_run_write_hook(cwd, payload).returncode, 0)

    def test_only_the_exact_managed_launcher_target_is_authenticated(self) -> None:
        external_dir = Path(self.temp_dir.name) / "external"
        external_dir.mkdir()
        external = external_dir / "agent-flow"
        _write_executable(external, "#!/bin/sh\nexit 0\n")
        alias = self.worktree / "external-launcher"
        alias.symlink_to(external)
        for command in (
            f"{shlex.quote(str(external))} status",
            "./external-launcher status",
        ):
            for payload in _host_payloads(command):
                with self.subTest(command=command, payload=payload):
                    self.assertEqual(
                        _run_write_hook(self.worktree, payload).returncode,
                        2,
                    )

        bridge = self.worktree / ".agent-flow" / "bin"
        bridge.unlink()
        bridge.symlink_to(external_dir, target_is_directory=True)
        for payload in _host_payloads("./.agent-flow/bin/agent-flow status"):
            self.assertEqual(_run_write_hook(self.worktree, payload).returncode, 2)

    def test_symlinked_leader_launcher_is_not_authenticated(self) -> None:
        external = Path(self.temp_dir.name) / "external-agent-flow"
        _write_executable(external, "#!/bin/sh\nexit 0\n")
        self.launcher.unlink()
        self.launcher.symlink_to(external)
        for payload in _host_payloads("./.agent-flow/bin/agent-flow status"):
            self.assertEqual(_run_write_hook(self.root, payload).returncode, 2)

    def test_launcher_and_runtime_tamper_are_rejected_across_hosts(self) -> None:
        cases = (
            self.launcher,
            self.root / ".agent-flow" / "runtime" / "node" / "bin" / "agent-flow-kit.mjs",
        )
        for target in cases:
            original = target.read_bytes()
            target.write_bytes(original + b"tamper\n")
            if target == self.launcher:
                target.chmod(0o755)
            try:
                for payload in _host_payloads("./.agent-flow/bin/agent-flow status"):
                    with self.subTest(target=target, payload=payload):
                        self.assertEqual(_run_write_hook(self.root, payload).returncode, 2)
            finally:
                target.write_bytes(original)
                if target == self.launcher:
                    target.chmod(0o755)


class PinnedLauncherStopHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "repo"
        self.root.mkdir()
        _init_git_repo(self.root)
        self.worktree = Path(self.temp_dir.name) / "feature"
        subprocess.run(
            (
                "git",
                "worktree",
                "add",
                "-q",
                "-b",
                "feat/status-hook",
                str(self.worktree),
                "main",
            ),
            cwd=self.root,
            check=True,
        )
        _write_node_run(self.root, self.worktree, status="running")
        self.launcher = self.root / ".agent-flow" / "bin" / "agent-flow"
        _write_executable(
            self.launcher,
            "#!/bin/sh\nprintf 'runner: pinned\\ncwd: %s\\nargs: %s\\n' \"$PWD\" \"$*\"\n",
        )
        _write_runtime_contract(self.root, self.launcher)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_stop_hook_prefers_pinned_launcher_over_stale_global(self) -> None:
        tool_bin = Path(self.temp_dir.name) / "stale-bin"
        _seed_hook_path(tool_bin)
        stale_marker = Path(self.temp_dir.name) / "stale-called"
        _write_executable(
            tool_bin / "agent-flow",
            f"#!/bin/sh\n: > {shlex.quote(str(stale_marker))}\nprintf 'runner: stale\\n'\n",
        )
        payload = _run_stop_hook(self.root, tool_bin)
        self.assertIn("runner: pinned", payload["systemMessage"])
        self.assertIn(f"cwd: {self.worktree.resolve()}", payload["systemMessage"])
        self.assertIn("args: status --format hook", payload["systemMessage"])
        self.assertFalse(stale_marker.exists())

    def test_stop_hook_uses_pinned_launcher_without_a_global_command(self) -> None:
        tool_bin = Path(self.temp_dir.name) / "clean-bin"
        _seed_hook_path(tool_bin)
        payload = _run_stop_hook(self.root, tool_bin)
        self.assertIn("runner: pinned", payload["systemMessage"])
        self.assertIn(f"cwd: {self.worktree.resolve()}", payload["systemMessage"])

    def test_stop_hook_ignores_a_fake_path_python_interpreter(self) -> None:
        tool_bin = Path(self.temp_dir.name) / "fake-python-bin"
        _seed_hook_path(tool_bin)
        marker = Path(self.temp_dir.name) / "fake-python-called"
        fake_python = tool_bin / "python3"
        fake_python.unlink()
        _write_executable(
            fake_python,
            f"#!/bin/sh\n: > {shlex.quote(str(marker))}\nprintf '{{}}\\n'\n",
        )
        payload = _run_stop_hook(self.root, tool_bin)
        self.assertIn("runner: pinned", payload["systemMessage"])
        self.assertFalse(marker.exists())

    def test_shared_stop_hook_rejects_symlinked_launcher_without_global_fallback(self) -> None:
        external = Path(self.temp_dir.name) / "external-launcher"
        unsafe_marker = Path(self.temp_dir.name) / "unsafe-launcher-called"
        _write_executable(
            external,
            f"#!/bin/sh\n: > {shlex.quote(str(unsafe_marker))}\n",
        )
        self.launcher.unlink()
        self.launcher.symlink_to(external)
        self._assert_unsafe_pinned_launcher(unsafe_marker)

    def test_shared_stop_hook_rejects_symlinked_bin_without_global_fallback(self) -> None:
        external_bin = Path(self.temp_dir.name) / "external-bin"
        unsafe_marker = Path(self.temp_dir.name) / "unsafe-bin-called"
        _write_executable(
            external_bin / "agent-flow",
            f"#!/bin/sh\n: > {shlex.quote(str(unsafe_marker))}\n",
        )
        shutil.rmtree(self.launcher.parent)
        self.launcher.parent.symlink_to(external_bin, target_is_directory=True)
        self._assert_unsafe_pinned_launcher(unsafe_marker)

    def test_shared_stop_hook_rejects_symlinked_agent_flow_root_without_global_fallback(self) -> None:
        external_root = Path(self.temp_dir.name) / "external-agent-flow-root"
        unsafe_marker = Path(self.temp_dir.name) / "unsafe-root-called"
        agent_flow_root = self.root / ".agent-flow"
        agent_flow_root.rename(external_root)
        agent_flow_root.symlink_to(external_root, target_is_directory=True)
        _write_executable(
            external_root / "bin" / "agent-flow",
            f"#!/bin/sh\n: > {shlex.quote(str(unsafe_marker))}\n",
        )
        self._assert_unsafe_pinned_launcher(unsafe_marker)

    def test_shared_stop_hook_rejects_launcher_byte_tamper_without_global_fallback(self) -> None:
        self.launcher.write_text("#!/bin/sh\nprintf 'tampered\\n'\n", encoding="utf-8")
        self.launcher.chmod(0o755)
        self._assert_unsafe_pinned_launcher(Path(self.temp_dir.name) / "never-created")

    def test_shared_stop_hook_rejects_runtime_byte_tamper_without_global_fallback(self) -> None:
        runtime = self.root / ".agent-flow" / "runtime" / "node" / "bin" / "agent-flow-kit.mjs"
        runtime.write_text("tampered runtime\n", encoding="utf-8")
        self._assert_unsafe_pinned_launcher(Path(self.temp_dir.name) / "never-created")

    def _assert_unsafe_pinned_launcher(self, unsafe_marker: Path) -> None:
        tool_bin = Path(self.temp_dir.name) / "unsafe-stale-bin"
        _seed_hook_path(tool_bin)
        stale_marker = Path(self.temp_dir.name) / "unsafe-stale-called"
        _write_executable(
            tool_bin / "agent-flow",
            f"#!/bin/sh\n: > {shlex.quote(str(stale_marker))}\n",
        )
        payload = _run_stop_hook(self.root, tool_bin)
        self.assertIn("blocked:", payload["systemMessage"])
        self.assertFalse(stale_marker.exists())
        self.assertFalse(unsafe_marker.exists())

    def test_user_facing_recovery_commands_use_the_pinned_launcher(self) -> None:
        cli = (ROOT / "src" / "agent_flow" / "cli.py").read_text(encoding="utf-8")
        self.assertEqual(
            cli.count('print("continue: ./.agent-flow/bin/agent-flow status")'),
            2,
        )
        for relative in (
            "scripts/hooks/guard-worktree.sh",
            "scripts/hooks/guard-protected-branch.sh",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn('./.agent-flow/bin/agent-flow run \\"<task>\\"', source)
            self.assertNotIn('echo "agent-flow run \\"<task>\\"', source)


class PinnedPythonStopHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "repo"
        self.root.mkdir()
        _init_git_repo(self.root)
        self.worktree = Path(self.temp_dir.name) / "python-feature"
        subprocess.run(
            (
                "git",
                "worktree",
                "add",
                "-q",
                "-b",
                "feat/python-status",
                str(self.worktree),
                "main",
            ),
            cwd=self.root,
            check=True,
        )
        (self.root / ".agent-flow").mkdir()
        launcher = self.root / ".agent-flow" / "bin" / "agent-flow"
        _write_executable(launcher, "#!/bin/sh\nexit 0\n")
        _write_test_python_runtime(self.root / ".agent-flow" / "runtime" / "python")
        _write_runtime_contract(self.root, launcher)
        _write_python_run(self.root, self.worktree)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_bundled_python_runtime_precedes_stale_global(self) -> None:
        tool_bin = Path(self.temp_dir.name) / "bundled-python-bin"
        _seed_hook_path(tool_bin)
        stale_marker = Path(self.temp_dir.name) / "stale-python-called"
        _write_executable(
            tool_bin / "agent-flow-python",
            f"#!/bin/sh\n: > {shlex.quote(str(stale_marker))}\nprintf 'runner: stale-python\\n'\n",
        )
        payload = _run_stop_hook(self.root, tool_bin)
        self.assertIn("runner: bundled-python", payload["systemMessage"])
        self.assertIn(f"cwd: {self.worktree.resolve()}", payload["systemMessage"])
        self.assertIn("--worktree feat-python-status", payload["systemMessage"])
        self.assertFalse(stale_marker.exists())

    def test_installed_missing_python_runtime_never_falls_back_to_global(self) -> None:
        shutil.rmtree(self.root / ".agent-flow" / "runtime" / "python")
        tool_bin = Path(self.temp_dir.name) / "legacy-python-bin"
        _seed_hook_path(tool_bin)
        legacy_marker = Path(self.temp_dir.name) / "legacy-python-called"
        _write_executable(
            tool_bin / "agent-flow-python",
            (
                f"#!/bin/sh\n: > {shlex.quote(str(legacy_marker))}\n"
                "printf 'runner: legacy-python\\ncwd: %s\\nargs: %s\\n' \"$PWD\" \"$*\"\n"
            ),
        )
        payload = _run_stop_hook(self.root, tool_bin)
        self.assertIn("blocked: pinned Python runtime root is unavailable", payload["systemMessage"])
        self.assertFalse(legacy_marker.exists())

    def test_unsafe_python_runtime_does_not_fallback_to_global(self) -> None:
        external_runtime = Path(self.temp_dir.name) / "external-python-runtime"
        _write_test_python_runtime(external_runtime)
        runtime_root = self.root / ".agent-flow" / "runtime"
        runtime_root.mkdir(exist_ok=True)
        shutil.rmtree(runtime_root / "python")
        (runtime_root / "python").symlink_to(external_runtime, target_is_directory=True)
        tool_bin = Path(self.temp_dir.name) / "unsafe-python-bin"
        _seed_hook_path(tool_bin)
        stale_marker = Path(self.temp_dir.name) / "unsafe-stale-python-called"
        _write_executable(
            tool_bin / "agent-flow-python",
            f"#!/bin/sh\n: > {shlex.quote(str(stale_marker))}\n",
        )
        payload = _run_stop_hook(self.root, tool_bin)
        self.assertIn("blocked: pinned Python runtime root path may not use symlinks", payload["systemMessage"])
        self.assertFalse(stale_marker.exists())


def _leader_launcher_forms(root: Path) -> tuple[str, ...]:
    return (
        "./.agent-flow/bin/agent-flow",
        shlex.quote(str(root / ".agent-flow" / "bin" / "agent-flow")),
        shlex.quote(str(root / ".agent-flow" / "bin" / ".." / "bin" / "agent-flow")),
    )


def _worktree_launcher_forms(root: Path, worktree: Path) -> tuple[str, ...]:
    return (
        "./.agent-flow/bin/agent-flow",
        shlex.quote(str(worktree / ".agent-flow" / "bin" / "agent-flow")),
        shlex.quote(str(root / ".agent-flow" / "bin" / "agent-flow")),
        "./.agent-flow/bin/../bin/agent-flow",
    )


def _host_payloads(command: str) -> tuple[dict[str, object], ...]:
    return (
        {"tool_name": "Bash", "tool_input": {"command": command}},
        {"tool": "bash", "input": {"command": command}},
        {"tool": "bash", "parameters": {"command": command}},
    )


def _run_write_hook(
    cwd: Path,
    payload: dict[str, object],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (str(WRITE_HOOK),),
        cwd=cwd,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def _run_stop_hook(root: Path, tool_bin: Path) -> dict[str, str]:
    result = subprocess.run(
        ("/bin/bash", str(STOP_HOOK)),
        cwd=root,
        env={**os.environ, "PATH": str(tool_bin)},
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def _seed_hook_path(directory: Path) -> None:
    directory.mkdir()
    for command in ("dirname", "git", "python3"):
        executable = shutil.which(command)
        if executable is None:
            raise AssertionError(f"missing test executable: {command}")
        (directory / command).symlink_to(executable)


def _install_launcher_bridge(root: Path, worktree: Path) -> Path:
    launcher = root / ".agent-flow" / "bin" / "agent-flow"
    _write_executable(launcher, "#!/bin/sh\nexit 0\n")
    _write_runtime_contract(root, launcher)
    bridge_root = worktree / ".agent-flow"
    bridge_root.mkdir()
    (bridge_root / "bin").symlink_to(launcher.parent, target_is_directory=True)
    return launcher


def _write_runtime_contract(root: Path, launcher: Path) -> None:
    runtime_root = root / ".agent-flow" / "runtime" / "node"
    entrypoint = runtime_root / "bin" / "agent-flow-kit.mjs"
    entrypoint.parent.mkdir(parents=True, exist_ok=True)
    if not entrypoint.exists():
        entrypoint.write_text("// test runtime\n", encoding="utf-8")
    python_runtime_root = root / ".agent-flow" / "runtime" / "python"
    python_package = python_runtime_root / "agent_flow"
    python_package.mkdir(parents=True, exist_ok=True)
    init_file = python_package / "__init__.py"
    if not init_file.exists():
        init_file.write_text("", encoding="utf-8")
    contract = {
        "version": 2,
        "launcher": {
            "path": ".agent-flow/bin/agent-flow",
            "sha256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
        },
        "node_runtime": {
            "root": ".agent-flow/runtime/node",
            "entrypoint": ".agent-flow/runtime/node/bin/agent-flow-kit.mjs",
            "tree_hash": _hash_test_runtime_tree(runtime_root),
        },
        "python_runtime": {
            "root": ".agent-flow/runtime/python",
            "tree_hash": _hash_test_runtime_tree(python_runtime_root),
        },
    }
    commitment = hashlib.sha256(
        json.dumps(
            {"version": 2, "contract": contract},
            separators=(",", ":"),
        ).encode("utf-8")
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


def _hash_test_runtime_tree(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(candidate for candidate in root.rglob("*") if candidate.is_file())
    for file in files:
        digest.update(file.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(file.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _write_executable(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _write_node_run(root: Path, workspace: Path, *, status: str) -> None:
    run_dir = Path(".agent-flow") / "runs" / "default" / "active"
    state = {
        "run_id": "active",
        "workflow": "default",
        "run_dir": run_dir.as_posix(),
        "status": status,
        "phase": "implement" if status == "running" else "complete",
        "workspace_root": str(workspace),
    }
    manifest = root / run_dir / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(state), encoding="utf-8")
    pointer = root / ".agent-flow" / "state" / "current-run.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps(state), encoding="utf-8")


def _write_python_run(root: Path, workspace: Path) -> None:
    runtime = root / ".git" / "agent-flow" / "worktrees" / "feat-python-status"
    run_dir = runtime / ".agent-flow" / "runs" / "python-run"
    run_dir.mkdir(parents=True)
    (run_dir / "active").touch()
    (run_dir / "meta.json").write_text(
        json.dumps({"run_id": "python-run", "workflow": "default", "task": "python"}),
        encoding="utf-8",
    )
    (runtime / "manifest.json").write_text(
        json.dumps(
            {
                "name": "feat-python-status",
                "branch": "feat/python-status",
                "path": str(workspace),
            }
        ),
        encoding="utf-8",
    )


def _write_test_python_runtime(runtime: Path) -> None:
    package = runtime / "agent_flow"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(
        (
            "import os, sys\n"
            "print('runner: bundled-python')\n"
            "print(f'cwd: {os.getcwd()}')\n"
            "print('args: ' + ' '.join(sys.argv[1:]))\n"
        ),
        encoding="utf-8",
    )


def _init_git_repo(root: Path) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test User",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test User",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(("git", "init", "-q", "-b", "main"), cwd=root, check=True)
    (root / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(("git", "add", "README.md"), cwd=root, check=True)
    subprocess.run(("git", "commit", "-q", "-m", "init"), cwd=root, env=env, check=True)


if __name__ == "__main__":
    unittest.main()
