from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


HOOK = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "hooks"
    / "guard-worktree-write.py"
)


class GuardScriptWrapperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "repo"
        self.root.mkdir()
        _init_git_repo(self.root)
        self.worktree = self.root / ".agent-flow" / "worktrees" / "feat-script-guard"
        subprocess.run(
            (
                "git",
                "worktree",
                "add",
                "-q",
                "-b",
                "feat/script-guard",
                str(self.worktree),
                "main",
            ),
            cwd=self.root,
            check=True,
        )
        _write_active_run(self.root, self.worktree)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_blocks_direct_python_and_node_scripts_with_constant_outside_writes(self) -> None:
        (self.worktree / "writer.py").write_text(
            f'from pathlib import Path\nPath({str(self.root / "python-bypass.txt")!r}).write_text("x")\n',
            encoding="utf-8",
        )
        (self.worktree / "writer.js").write_text(
            f'require("fs").writeFileSync({json.dumps(str(self.root / "node-bypass.txt"))}, "x");\n',
            encoding="utf-8",
        )
        (self.worktree / "variable-writer.js").write_text(
            (
                f'const target = {json.dumps(str(self.root / "node-variable-bypass.txt"))};\n'
                'require("fs").writeFileSync(target, "x");\n'
            ),
            encoding="utf-8",
        )

        self.assert_blocked("python3 writer.py")
        self.assert_blocked("node writer.js")
        self.assert_blocked("node variable-writer.js")
        self.assert_blocked("python3 -m writer")
        self.assert_blocked("node --require ./writer.js --check missing-local-script.js")

    def test_blocks_python_cwd_mutation_and_dynamic_execution(self) -> None:
        (self.worktree / "chdir-writer.py").write_text(
            (
                "import os\n"
                "from pathlib import Path\n"
                f"os.chdir({str(self.root)!r})\n"
                'Path("cwd-bypass.txt").write_text("x")\n'
            ),
            encoding="utf-8",
        )
        (self.worktree / "dynamic-system.py").write_text(
            "import os\ncommand = input()\nos.system(command)\n",
            encoding="utf-8",
        )
        import_command = f"touch {self.root / 'import-bypass.txt'}"
        (self.worktree / "dynamic-import.py").write_text(
            f"__import__('os').system({import_command!r})\n",
            encoding="utf-8",
        )
        eval_command = f'open({str(self.root / "eval-bypass.txt")!r}, "w").write("x")'
        (self.worktree / "dynamic-eval.py").write_text(
            f"eval({eval_command!r})\n",
            encoding="utf-8",
        )

        for command in (
            "python3 chdir-writer.py",
            "python3 dynamic-system.py",
            "python3 dynamic-import.py",
            "python3 dynamic-eval.py",
        ):
            with self.subTest(command=command):
                self.assert_blocked(command)

    def test_blocks_python_and_node_links_crossing_the_worktree_boundary_across_hosts(self) -> None:
        leader_source = self.root / "README.md"
        python_sources = {
            "os-hardlink.py": (
                "import os\n"
                f"os.link({str(leader_source)!r}, 'linked-readme')\n"
            ),
            "os-symlink.py": (
                "import os\n"
                f"os.symlink({str(leader_source)!r}, 'linked-readme')\n"
            ),
            "path-hardlink.py": (
                "from pathlib import Path\n"
                f"Path('linked-readme').hardlink_to({str(leader_source)!r})\n"
            ),
            "path-symlink.py": (
                "from pathlib import Path\n"
                f"Path('linked-readme').symlink_to({str(leader_source)!r})\n"
            ),
        }
        node_sources = {
            "node-hardlink.js": (
                f"require('fs').linkSync({json.dumps(str(leader_source))}, 'linked-readme');\n"
            ),
            "node-symlink.js": (
                f"require('fs').symlinkSync({json.dumps(str(leader_source))}, 'linked-readme');\n"
            ),
            "node-async-link.js": (
                f"require('fs').link({json.dumps(str(leader_source))}, 'linked-readme', () => {{}});\n"
            ),
        }
        for name, source in {**python_sources, **node_sources}.items():
            (self.worktree / name).write_text(source, encoding="utf-8")
            command = f"python3 {name}" if name.endswith(".py") else f"node {name}"
            for payload in _host_payloads(command):
                with self.subTest(name=name, payload=payload):
                    result = _run_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 2, result.stderr)

        (self.worktree / "local-source").write_text("local", encoding="utf-8")
        (self.worktree / "local-links.py").write_text(
            "import os\n"
            "from pathlib import Path\n"
            "os.link('local-source', 'local-hardlink')\n"
            "Path('local-symlink').symlink_to('local-source')\n",
            encoding="utf-8",
        )
        (self.worktree / "local-links.js").write_text(
            "const fs = require('fs');\n"
            "fs.linkSync('local-source', 'local-node-hardlink');\n"
            "fs.symlinkSync('local-source', 'local-node-symlink');\n",
            encoding="utf-8",
        )
        for command in ("python3 local-links.py", "node local-links.js"):
            for payload in _host_payloads(command):
                with self.subTest(local=command, payload=payload):
                    result = _run_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_blocks_sourced_and_direct_local_shell_scripts(self) -> None:
        writer = self.worktree / "writer.sh"
        writer.write_text(
            f"#!/bin/sh\ntouch {str(self.root / 'shell-bypass.txt')!r}\n",
            encoding="utf-8",
        )
        writer.chmod(0o755)

        for command in (
            "source writer.sh",
            ". writer.sh",
            "./writer.sh",
            "PATH=.:$PATH writer.sh",
            "sh writer.sh",
        ):
            with self.subTest(command=command):
                self.assert_blocked(command)

    def test_blocks_npm_pre_main_and_post_lifecycle_scripts(self) -> None:
        outside = str(self.root / "npm-bypass.txt")
        (self.worktree / "package.json").write_text(
            json.dumps(
                {
                    "scripts": {
                        "preprecase": f"touch {outside}",
                        "precase": "touch local-pre.txt",
                        "maincase": f"touch {outside}",
                        "postcase": "touch local-post.txt",
                        "postpostcase": f"touch {outside}",
                    }
                }
            ),
            encoding="utf-8",
        )

        for command in ("npm run precase", "npm run maincase", "npm run postcase"):
            with self.subTest(command=command):
                self.assert_blocked(command)

    def test_requires_resolved_scripts_to_remain_inside_the_active_worktree(self) -> None:
        outside_script = self.root / "outside-writer.py"
        outside_script.write_text("print('outside')\n", encoding="utf-8")
        (self.worktree / "linked-writer.py").symlink_to(outside_script)

        self.assert_blocked("python3 linked-writer.py")

    def test_script_inspection_limits_fail_closed(self) -> None:
        (self.worktree / "package.json").write_text(
            json.dumps({"scripts": {"cycle": "npm run cycle"}}),
            encoding="utf-8",
        )
        self.assert_blocked("npm run cycle")

        oversized = self.worktree / "oversized.py"
        oversized.write_text("#" * (256 * 1024 + 1), encoding="utf-8")
        self.assert_blocked("python3 oversized.py")

        for index in range(7):
            script = self.worktree / f"depth-{index}.sh"
            next_command = f"./depth-{index + 1}.sh" if index < 6 else "touch local.txt"
            script.write_text(f"#!/bin/sh\n{next_command}\n", encoding="utf-8")
            script.chmod(0o755)
        self.assert_blocked("./depth-0.sh")

    def test_preserves_legitimate_local_and_non_executing_commands(self) -> None:
        (self.worktree / "local-writer.py").write_text(
            'from pathlib import Path\nPath("generated.txt").write_text("x")\n',
            encoding="utf-8",
        )
        (self.worktree / "local-writer.js").write_text(
            'require("fs").writeFileSync("generated.js.txt", "x");\n',
            encoding="utf-8",
        )
        local_shell = self.worktree / "local-writer.sh"
        local_shell.write_text("#!/bin/sh\ntouch generated.sh.txt\n", encoding="utf-8")
        local_shell.chmod(0o755)

        for command in (
            "python3 local-writer.py",
            "node local-writer.js",
            "./local-writer.sh",
            "PATH=.:$PATH local-writer.sh",
            "python3 -m pytest --collect-only",
            "node --check missing-local-script.js",
            "npm test",
        ):
            with self.subTest(command=command):
                self.assert_allowed(command)

    def test_preserves_android_and_maven_wrappers_but_blocks_literal_leader_writes(self) -> None:
        gradlew = self.worktree / "gradlew"
        gradlew.write_text(
            "#!/bin/sh\nAPP_HOME=$(cd \"${0%/*}\" && pwd)\nexec java -jar \"$APP_HOME/gradle-wrapper.jar\" \"$@\"\n",
            encoding="utf-8",
        )
        gradlew.chmod(0o755)
        self.assert_allowed("./gradlew test")
        self.assert_allowed("sh -c 'cd . && ./gradlew assembleDebug'")

        gradlew.write_text(
            f"#!/bin/sh\ntouch {str(self.root / 'wrapper-bypass.txt')!r}\n",
            encoding="utf-8",
        )
        self.assert_blocked("./gradlew test")

    def test_allows_run_artifact_control_writes_but_blocks_leader_source_writes(self) -> None:
        artifact = self.root / ".agent-flow" / "runs" / "default" / "active" / "artifacts" / "result.md"
        leader_source = self.root / "src" / "wrong.py"

        for payload in (
            {"tool_name": "Write", "tool_input": {"file_path": str(artifact)}},
            {"tool_name": "Bash", "tool_input": {"command": f"touch {artifact}"}},
        ):
            with self.subTest(tool=payload["tool_name"]):
                result = _run_hook_payload(self.worktree, payload)
                self.assertEqual(result.returncode, 0, result.stderr)

        for payload in (
            {"tool_name": "Write", "tool_input": {"file_path": str(leader_source)}},
            {"tool_name": "Bash", "tool_input": {"command": f"touch {leader_source}"}},
        ):
            with self.subTest(tool=payload["tool_name"]):
                result = _run_hook_payload(self.worktree, payload)
                self.assertEqual(result.returncode, 2, result.stderr)

    def assert_blocked(self, command: str) -> None:
        result = _run_hook(self.worktree, command)
        self.assertEqual(result.returncode, 2, (command, result.stderr))
        self.assertIn("outside the active worktree", result.stderr)

    def assert_allowed(self, command: str) -> None:
        result = _run_hook(self.worktree, command)
        self.assertEqual(result.returncode, 0, (command, result.stderr))


def _run_hook(cwd: Path, command: str) -> subprocess.CompletedProcess[str]:
    return _run_hook_payload(
        cwd,
        {"tool_name": "Bash", "tool_input": {"command": command}},
    )


def _run_hook_payload(cwd: Path, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (str(HOOK),),
        cwd=cwd,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


def _host_payloads(command: str) -> tuple[dict[str, object], ...]:
    return (
        {"tool_name": "Bash", "tool_input": {"command": command}},
        {"tool": "bash", "input": {"command": command}},
        {"tool": "bash", "parameters": {"command": command}},
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


def _write_active_run(root: Path, workspace: Path) -> None:
    run_dir = Path(".agent-flow") / "runs" / "default" / "active"
    state = {
        "run_id": "active",
        "workflow": "default",
        "run_dir": run_dir.as_posix(),
        "status": "running",
        "phase": "implement",
        "workspace_root": str(workspace),
    }
    manifest = root / run_dir / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(state), encoding="utf-8")
    pointer = root / ".agent-flow" / "state" / "current-run.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps(state), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
