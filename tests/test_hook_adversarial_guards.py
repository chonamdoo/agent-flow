from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WRITE_HOOK = ROOT / "scripts" / "hooks" / "guard-worktree-write.py"
PROTECTED_HOOK = ROOT / "scripts" / "hooks" / "guard-protected-branch.sh"
WORKTREE_HOOK = ROOT / "scripts" / "hooks" / "guard-worktree.sh"
COMMENT_HOOK = ROOT / "scripts" / "hooks" / "comment-checker.py"


class HookAdversarialGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "repo"
        self.root.mkdir()
        _init_git_repo(self.root)
        self.worktree = self.root / ".agent-flow" / "worktrees" / "feat-hook-review"
        subprocess.run(
            (
                "git",
                "worktree",
                "add",
                "-q",
                "-b",
                "feat/hook-review",
                str(self.worktree),
                "main",
            ),
            cwd=self.root,
            check=True,
        )
        _write_active_run(self.root, self.worktree)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_sed_multiple_expression_write_commands_fail_closed(self) -> None:
        for command in (
            "sed -n -e p -e 'w AGENTS.md' README.md",
            "sed --quiet --expression=p --expression='W AGENTS.md' README.md",
            "sed -n -f commands.sed README.md",
        ):
            with self.subTest(command=command):
                result = _run_write_hook(self.root, command)
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_sed_compact_write_and_execute_commands_fail_closed(self) -> None:
        for command in (
            "sed -n '1wAGENTS.md' README.md",
            "sed -n '1WAGENTS.md' README.md",
            "sed -n 's/test/changed/wAGENTS.md' README.md",
            "sed -n '1eprintf bypass' README.md",
            "sed -n '1Eprintf bypass' README.md",
        ):
            for payload in _host_payloads(command):
                with self.subTest(command=command, payload=payload):
                    result = _run_write_hook_payload(self.root, payload)
                    self.assertEqual(result.returncode, 2, result.stderr)

    def test_protected_push_repo_forms_scan_every_refspec(self) -> None:
        for command in (
            "git push --repo origin HEAD:refs/heads/main",
            "git push --repo=origin HEAD:refs/heads/feature HEAD:refs/heads/master",
            "git push --repo=origin HEAD:develop",
            "git push --signed --repo=origin HEAD:refs/heads/main",
            "git push --force-with-lease --repo origin HEAD:refs/heads/develop",
        ):
            with self.subTest(command=command):
                result = _run_protected_hook(self.worktree, command)
                self.assertEqual(result.returncode, 2, (command, result.stderr))
                self.assertIn("보호 브랜치", result.stderr)

        allowed = _run_protected_hook(
            self.worktree,
            "git push --repo origin HEAD:refs/heads/feature",
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_ln_target_directory_forms_cannot_write_outside_the_active_worktree(self) -> None:
        outside = self.root
        blocked_commands = (
            f"ln -t{outside} local-source",
            f"ln -t {outside} local-source",
            f"ln --target-directory={outside} local-source",
            f"ln --target-directory {outside} local-source",
            f"ln -st{outside} local-source",
        )
        for command in blocked_commands:
            for payload in _host_payloads(command):
                with self.subTest(command=command, payload=payload):
                    result = _run_write_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("outside the active worktree", result.stderr)

        allowed_commands = (
            "ln -tlinks local-source",
            "ln -t links local-source",
            "ln --target-directory=links local-source",
            "ln --target-directory links local-source",
            "ln -stlinks local-source",
        )
        for command in allowed_commands:
            for payload in _host_payloads(command):
                with self.subTest(command=command, payload=payload):
                    result = _run_write_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_low_level_and_fetch_ref_updates_protect_shared_git_state_across_hosts(self) -> None:
        (self.worktree / "stream.fi").write_text(
            "commit refs/heads/main\nmark :1\ncommitter Test <test@example.com> 0 +0000\n"
            "data 4\ntest\n\n",
            encoding="utf-8",
        )
        blocked_commands = (
            "git fast-import --quiet < stream.fi",
            "git receive-pack .",
            "FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f --tree-filter 'touch injected' -- main",
            "FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f --tree-filter 'true' -- --all",
            "git send-pack origin HEAD:refs/heads/main",
            "git send-pack origin HEAD:master",
            "git send-pack origin 'refs/heads/ma*:refs/heads/ma*'",
            "git send-pack --stdin origin",
            "git fetch --update-head-ok origin feature:refs/heads/main",
            "git fetch origin feature:master",
            "git fetch origin 'refs/heads/feature:refs/heads/ma*'",
            "git pull origin feature:refs/heads/develop",
            "git pull --gpg-sign origin feature:refs/heads/main",
            "git push origin 'refs/heads/ma*:refs/heads/ma*'",
            "git remote rename origin backup",
            "git remote set-url origin https://example.com/repo.git",
            "git config user.name changed",
            "git submodule update --init",
            "git worktree prune",
            "/usr/lib/git-core/git-receive-pack .",
            "/usr/lib/git-core/git-update-ref refs/heads/main HEAD",
            "/usr/lib/git-core/git-send-pack origin HEAD:refs/heads/main",
            "command /usr/lib/git-core/git-update-ref refs/heads/feature HEAD",
        )
        for command in blocked_commands:
            for payload in _host_payloads(command):
                with self.subTest(hook="worktree-write", command=command, payload=payload):
                    result = _run_write_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 2, result.stderr)
                with self.subTest(hook="protected-branch", command=command, payload=payload):
                    result = _run_protected_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 2, result.stderr)

        safe_commands = (
            "git fast-export main",
            "git send-pack origin HEAD:refs/heads/feat/hook-review",
            "git send-pack origin 'refs/heads/feature/*:refs/heads/feature/*'",
            "git fetch --update-head-ok origin feature:refs/heads/feat/hook-review",
            "git fetch origin 'refs/heads/feature/*:refs/heads/feature/*'",
            "git pull origin feature:refs/heads/feat/hook-review",
            "git push origin 'refs/heads/feature/*:refs/heads/feature/*'",
            "git fetch origin main",
            "git fetch origin master",
            "git fetch origin develop",
            "git pull origin main",
            "git pull origin master",
            "git pull origin develop",
            "git remote -v",
            "git remote show origin",
            "git remote get-url origin",
            "git config --get remote.origin.url",
            "git config --list",
            "git config get remote.origin.url",
            "git config list",
            "git submodule status",
            "git submodule summary",
            "git worktree list --porcelain",
        )
        for command in safe_commands:
            for payload in _host_payloads(command):
                with self.subTest(safe_hook="worktree-write", command=command, payload=payload):
                    result = _run_write_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 0, result.stderr)
                with self.subTest(safe_hook="protected-branch", command=command, payload=payload):
                    result = _run_protected_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_tracking_push_default_and_protected_branch_rewrites_block_across_hosts(self) -> None:
        subprocess.run(
            ("git", "config", "branch.feat/hook-review.merge", "refs/heads/main"),
            cwd=self.worktree,
            check=True,
        )
        subprocess.run(
            ("git", "config", "push.default", "tracking"),
            cwd=self.worktree,
            check=True,
        )
        commands = (
            "git push origin",
            "git -c push.default=tracking -c branch.feat/hook-review.merge=refs/heads/main push origin",
            "git branch -f main HEAD",
            "git branch --force refs/heads/master HEAD",
            "git branch -D main develop",
            "git branch --delete master",
            "/usr/lib/git-core/git-branch -f main HEAD",
            "/usr/lib/git-core/git-branch -D develop",
        )
        for command in commands:
            for payload in _host_payloads(command):
                with self.subTest(hook="write", command=command, payload=payload):
                    self.assertEqual(
                        _run_write_hook_payload(self.worktree, payload).returncode,
                        2,
                    )
                with self.subTest(hook="protected", command=command, payload=payload):
                    self.assertEqual(
                        _run_protected_hook_payload(self.worktree, payload).returncode,
                        2,
                    )

    def test_worktree_wrappers_cannot_hide_branch_changes_across_hosts(self) -> None:
        commands = (
            "exec git checkout main",
            "exec -a git git switch main",
            "sudo git checkout main",
            "xargs git checkout main",
            "printf '%s\\n' main | xargs git checkout",
            "command env SAFE=1 exec /usr/bin/git checkout main",
            "/usr/lib/git-core/git-checkout main",
            "/usr/lib/git-core/git-switch main",
        )
        for command in commands:
            for payload in _host_payloads(command):
                with self.subTest(command=command, payload=payload):
                    result = _run_worktree_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 2, result.stderr)

    def test_fake_path_python_cannot_disable_managed_hooks_across_hosts(self) -> None:
        fake_bin = Path(self.temp_dir.name) / "fake-bin"
        fake_bin.mkdir()
        fake_python = fake_bin / "python3"
        fake_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_python.chmod(0o755)
        env = {**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}

        for payload in _host_payloads(f"touch {self.root / 'path-bypass.txt'}"):
            with self.subTest(hook="write", payload=payload):
                result = _run_write_hook_payload(self.worktree, payload, env=env)
                self.assertEqual(result.returncode, 2, result.stderr)
        for payload in _host_payloads("git push origin HEAD:main"):
            with self.subTest(hook="protected", payload=payload):
                result = _run_protected_hook_payload(self.worktree, payload, env=env)
                self.assertEqual(result.returncode, 2, result.stderr)
        for payload in _host_payloads("git checkout main"):
            with self.subTest(hook="worktree", payload=payload):
                result = _run_worktree_hook_payload(self.worktree, payload, env=env)
                self.assertEqual(result.returncode, 2, result.stderr)

        comment_payloads = (
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "sample.py", "content": "# Initialize value\nvalue = 1\n"},
            },
            {
                "tool": "write",
                "input": {"path": "sample.py", "content": "# Initialize value\nvalue = 1\n"},
            },
            {
                "tool": "write",
                "parameters": {"path": "sample.py", "content": "# Initialize value\nvalue = 1\n"},
            },
        )
        for payload in comment_payloads:
            with self.subTest(hook="comment", payload=payload):
                result = subprocess.run(
                    (str(COMMENT_HOOK),),
                    cwd=self.worktree,
                    input=json.dumps(payload),
                    text=True,
                    capture_output=True,
                    check=False,
                    env=env,
                )
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_persisted_fetch_refspec_cannot_update_a_protected_local_branch(self) -> None:
        subprocess.run(
            (
                "git",
                "config",
                "remote.origin.fetch",
                "+refs/heads/feature:refs/heads/main",
            ),
            cwd=self.worktree,
            check=True,
        )
        try:
            for command in ("git fetch origin", "git pull origin"):
                for payload in _host_payloads(command):
                    with self.subTest(hook="worktree-write", command=command, payload=payload):
                        result = _run_write_hook_payload(self.worktree, payload)
                        self.assertEqual(result.returncode, 2, result.stderr)
                    with self.subTest(hook="protected-branch", command=command, payload=payload):
                        result = _run_protected_hook_payload(self.worktree, payload)
                        self.assertEqual(result.returncode, 2, result.stderr)
        finally:
            subprocess.run(
                ("git", "config", "--unset-all", "remote.origin.fetch"),
                cwd=self.worktree,
                check=False,
            )

    def test_persisted_wildcard_refspecs_cannot_match_protected_branches(self) -> None:
        for key in ("remote.origin.push", "remote.origin.fetch"):
            subprocess.run(
                (
                    "git",
                    "config",
                    key,
                    "+refs/heads/ma*:refs/heads/ma*",
                ),
                cwd=self.worktree,
                check=True,
            )
        try:
            for command in ("git push origin", "git fetch origin", "git pull origin"):
                for payload in _host_payloads(command):
                    with self.subTest(hook="worktree-write", command=command, payload=payload):
                        result = _run_write_hook_payload(self.worktree, payload)
                        self.assertEqual(result.returncode, 2, result.stderr)
                    with self.subTest(hook="protected-branch", command=command, payload=payload):
                        result = _run_protected_hook_payload(self.worktree, payload)
                        self.assertEqual(result.returncode, 2, result.stderr)
        finally:
            for key in ("remote.origin.push", "remote.origin.fetch"):
                subprocess.run(
                    ("git", "config", "--unset-all", key),
                    cwd=self.worktree,
                    check=False,
                )

    def test_dynamic_and_nested_wrappers_block_protected_push_across_hosts(self) -> None:
        (self.worktree / "protected-push.sh").write_text(
            "git push origin HEAD:refs/heads/main\n",
            encoding="utf-8",
        )
        blocked_commands = (
            "eval 'git push origin HEAD:main'",
            "command eval 'git push origin HEAD:refs/heads/master'",
            "builtin eval 'git push origin HEAD:develop'",
            "exec git push origin +HEAD:refs/heads/main",
            "exec -a git git push origin HEAD:master",
            "command env SAFE=1 /usr/bin/git push origin 'HEAD:refs/heads/develop'",
            "FOO=1 exec sh -c 'git push --repo origin HEAD:refs/heads/develop'",
            'env SAFE=1 sh -lc "exec git push --repo=origin +HEAD:refs/heads/main"',
            'bash -c "eval \'git push origin HEAD:refs/heads/main\'"',
            "env -S 'git push origin HEAD:main'",
            "printf 'git push origin HEAD:main\\n' | sh",
            "source protected-push.sh",
            ". ./protected-push.sh",
        )
        for command in blocked_commands:
            for payload in _host_payloads(command):
                write_result = _run_write_hook_payload(self.worktree, payload)
                with self.subTest(hook="worktree-write", command=command, payload=payload):
                    self.assertEqual(write_result.returncode, 2, (command, write_result.stderr))
                with self.subTest(hook="protected-branch", command=command, payload=payload):
                    result = _run_protected_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 2, (command, result.stderr))
                    self.assertIn("BLOCKED", result.stderr)
                with self.subTest(hook="combined", command=command, payload=payload):
                    self.assertIn(
                        2,
                        (write_result.returncode, result.returncode),
                        (command, write_result.stderr, result.stderr),
                    )

        config_vectors = (
            "git -c alias.p='push origin HEAD:main' p",
            "ALIAS_PUSH='push origin HEAD:refs/heads/main' "
            "git --config-env=alias.p=ALIAS_PUSH p",
            "ALIAS_PUSH='push origin HEAD:refs/heads/develop' "
            "git --config-env alias.p=ALIAS_PUSH p",
        )
        for command in config_vectors:
            for payload in _host_payloads(command):
                with self.subTest(hook="worktree-write-config", command=command, payload=payload):
                    write_result = _run_write_hook_payload(self.worktree, payload)
                    self.assertEqual(write_result.returncode, 2, (command, write_result.stderr))
                with self.subTest(hook="combined-config", command=command, payload=payload):
                    protected_result = _run_protected_hook_payload(self.worktree, payload)
                    self.assertIn(
                        2,
                        (write_result.returncode, protected_result.returncode),
                        (command, write_result.stderr, protected_result.stderr),
                    )

    def test_process_wrappers_and_shell_controls_cannot_hide_protected_git_actions(self) -> None:
        blocked_commands = (
            "nohup git push origin HEAD:main",
            "nice git push origin HEAD:main",
            "timeout 5 git push origin HEAD:main",
            "setsid git push origin HEAD:main",
            "/usr/bin/time git push origin HEAD:main",
            "stdbuf -oL git push origin HEAD:main",
            "ionice -c 3 git push origin HEAD:main",
            "sudo git push origin HEAD:main",
            "doas git push origin HEAD:main",
            "parallel git push origin HEAD:main ::: one",
            "printf '%s\\n' 'push origin HEAD:main' | xargs git",
            "find . -maxdepth 0 -exec git push origin HEAD:main ';'",
            "if true; then git push origin HEAD:main; fi",
            "while true; do git push origin HEAD:main; done",
            "{ git push origin HEAD:main; }",
            "! git push origin HEAD:main",
            "time git push origin HEAD:main",
        )
        for command in blocked_commands:
            for payload in _host_payloads(command):
                with self.subTest(hook="worktree-write", command=command, payload=payload):
                    result = _run_write_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 2, result.stderr)
                with self.subTest(hook="protected-branch", command=command, payload=payload):
                    result = _run_protected_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 2, result.stderr)

        outside = self.root / "time-output-bypass.txt"
        for command in (
            f"time -o {outside} git status --short",
            f"time --output={outside} git status --short",
        ):
            for payload in _host_payloads(command):
                with self.subTest(time_output=command, payload=payload):
                    result = _run_write_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 2, result.stderr)

        safe_commands = (
            "nohup git status --short",
            "nice -n 5 git status --short",
            "timeout --signal TERM 5 git status --short",
            "setsid --wait git status --short",
            "/usr/bin/time git status --short",
            "stdbuf -oL git status --short",
            "ionice -c 3 git status --short",
            "find . -maxdepth 0 -print",
            "echo time",
        )
        for command in safe_commands:
            for payload in _host_payloads(command):
                with self.subTest(safe_hook="worktree-write", command=command, payload=payload):
                    result = _run_write_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 0, result.stderr)
                with self.subTest(safe_hook="protected-branch", command=command, payload=payload):
                    result = _run_protected_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_git_config_source_overrides_and_persisted_aliases_fail_closed(self) -> None:
        evil_config = self.worktree / "evil.gitconfig"
        evil_config.write_text(
            "[alias]\n\tdeploy = push origin HEAD:main\n",
            encoding="utf-8",
        )
        blocked_commands = (
            "GIT_CONFIG_GLOBAL=evil.gitconfig GIT_CONFIG_NOSYSTEM=1 git deploy",
            "GIT_CONFIG_SYSTEM=evil.gitconfig git deploy",
            "GIT_CONFIG_PARAMETERS=\"'alias.deploy=push origin HEAD:main'\" git deploy",
            "HOME=evil-home git deploy",
            "XDG_CONFIG_HOME=evil-xdg git deploy",
            "PATH=evil-bin:$PATH git push origin HEAD:main",
            "GIT_EXEC_PATH=evil-bin git deploy",
            "git --exec-path=evil-bin deploy",
        )
        for command in blocked_commands:
            for payload in _host_payloads(command):
                with self.subTest(hook="worktree-write", command=command, payload=payload):
                    result = _run_write_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 2, result.stderr)
                with self.subTest(hook="protected-branch", command=command, payload=payload):
                    result = _run_protected_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 2, result.stderr)

        subprocess.run(
            ("git", "config", "alias.deploy", "push origin HEAD:main"),
            cwd=self.worktree,
            check=True,
        )
        try:
            for payload in _host_payloads("git deploy"):
                with self.subTest(hook="worktree-write", payload=payload):
                    result = _run_write_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 2, result.stderr)
                with self.subTest(hook="protected-branch", payload=payload):
                    result = _run_protected_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 2, result.stderr)
        finally:
            subprocess.run(
                ("git", "config", "--unset-all", "alias.deploy"),
                cwd=self.worktree,
                check=False,
            )

    def test_static_wrappers_preserve_normal_commands_across_hosts(self) -> None:
        allowed_commands = (
            "git status --short",
            "exec git status --short",
            "command -- git status --short",
            "command -p git status --short",
            "command env SAFE=1 git status --short",
            "env SAFE=1 sh -c 'git status --short'",
            "exec sh -lc 'git status --short'",
            "git config --get remote.origin.push",
            "git config --list",
            "exec git push origin HEAD:refs/heads/feature",
            "command npm test",
            "exec npm test",
            "env SAFE=1 sh -c 'npm test'",
        )
        for command in allowed_commands:
            for payload in _host_payloads(command):
                with self.subTest(hook="worktree-write", command=command, payload=payload):
                    result = _run_write_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 0, (command, result.stderr))
                with self.subTest(hook="protected-branch", command=command, payload=payload):
                    result = _run_protected_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 0, (command, result.stderr))

    def test_git_executable_environment_and_config_fail_closed(self) -> None:
        outside = str(self.root / "git-vector.txt")
        blocked = (
            f"PAGER='sh -c \"touch {outside}\"' git log -1",
            f"GIT_PAGER='sh -c \"touch {outside}\"' git log -1",
            f"GIT_EXTERNAL_DIFF='touch {outside}' git diff",
            f"GIT_DIFF_OPTS='--unified=3; touch {outside}' git diff",
            f"LESS='-FRX; touch {outside}' PAGER=less git log -1",
            f"TERM='xterm; touch {outside}' git log -1",
            f"GIT_EDITOR='touch {outside}' git status",
            f"GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.pager GIT_CONFIG_VALUE_0='touch {outside}' git log -1",
            f"git -c core.pager='sh -c \"touch {outside}\"' log -1",
            f"git -c core.editor='touch {outside}' status",
            f"git -c diff.external='touch {outside}' diff",
            f"git -c diff.demo.textconv='touch {outside}' diff",
            f"git -c filter.demo.clean='touch {outside}' status",
            f"git -c alias.review='!touch {outside}' review",
            f"EVIL='sh -c \"touch {outside}\"' git --config-env=core.pager=EVIL log -1",
            f"EVIL='touch {outside}' git --config-env=diff.demo.textconv=EVIL diff",
        )
        for cwd in (self.root, self.worktree):
            for command in blocked:
                with self.subTest(cwd=cwd.name, command=command):
                    self.assertEqual(_run_write_hook(cwd, command).returncode, 2)

        allowed = (
            "git status --short",
            "PAGER=cat git log -1",
            "GIT_PAGER=/usr/bin/cat git log -1",
            "LESS=-FRX TERM=xterm-256color git log -1",
            "GIT_DIFF_OPTS=-u3 git diff --stat",
            "git -c core.pager=cat log -1",
            "git -c core.editor=true status",
            "EDITOR=vim git status",
            "SAFE_PAGER=cat git --config-env=core.pager=SAFE_PAGER log -1",
        )
        for cwd in (self.root, self.worktree):
            for command in allowed:
                with self.subTest(cwd=cwd.name, command=command):
                    result = _run_write_hook(cwd, command)
                    self.assertEqual(result.returncode, 0, (command, result.stderr))

    def test_awk_environment_redirect_cannot_escape_the_pinned_worktree(self) -> None:
        outside = self.root / "awk-environ-bypass.txt"
        commands = (
            (
                f"OUT={outside} awk "
                "'BEGIN { print \"x\" > ENVIRON[\"OUT\"] }'"
            ),
            (
                f"OUT={outside} awk "
                "'BEGIN { (\"tee \" ENVIRON[\"OUT\"]) | getline }'"
            ),
        )
        for command in commands:
            for payload in _host_payloads(command):
                with self.subTest(command=command, payload=payload):
                    result = _run_write_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 2, result.stderr)

        for payload in _host_payloads("awk '{ print $1 }' README.md"):
            with self.subTest(safe_payload=payload):
                result = _run_write_hook_payload(self.worktree, payload)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_persisted_remote_push_refspec_cannot_target_a_protected_branch(self) -> None:
        mutation = "git config remote.origin.push HEAD:main"
        for payload in _host_payloads(mutation):
            with self.subTest(mutation_payload=payload):
                result = _run_write_hook_payload(self.worktree, payload)
                self.assertEqual(result.returncode, 2, result.stderr)

        subprocess.run(
            ("git", "config", "remote.origin.push", "HEAD:main"),
            cwd=self.worktree,
            check=True,
        )
        try:
            for payload in _host_payloads("git push origin"):
                with self.subTest(hook="worktree-write", payload=payload):
                    result = _run_write_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 2, result.stderr)
                with self.subTest(hook="protected-branch", payload=payload):
                    result = _run_protected_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 2, result.stderr)
        finally:
            subprocess.run(
                ("git", "config", "--unset-all", "remote.origin.push"),
                cwd=self.worktree,
                check=False,
            )

        subprocess.run(
            (
                "git",
                "config",
                "remote.origin.push",
                "HEAD:refs/heads/feat/hook-review",
            ),
            cwd=self.worktree,
            check=True,
        )
        try:
            for payload in _host_payloads("git push origin"):
                with self.subTest(safe_hook="worktree-write", payload=payload):
                    result = _run_write_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 0, result.stderr)
                with self.subTest(safe_hook="protected-branch", payload=payload):
                    result = _run_protected_hook_payload(self.worktree, payload)
                    self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            subprocess.run(
                ("git", "config", "--unset-all", "remote.origin.push"),
                cwd=self.worktree,
                check=False,
            )

    def test_package_managers_and_make_recursively_inspect_commands(self) -> None:
        outside = str(self.root / "runner-vector.txt")
        build = self.worktree / "build"
        build.mkdir()
        (self.worktree / "package.json").write_text(
            json.dumps(
                {
                    "scripts": {
                        "preprecase": f"touch {outside}",
                        "precase": "touch build/pre.txt",
                        "maincase": f"touch {outside}",
                        "postcase": "touch build/post.txt",
                        "postpostcase": f"touch {outside}",
                        "local": "touch build/local.txt",
                        "pnpm-cycle": "pnpm run pnpm-cycle",
                        "yarn-cycle": "yarn run yarn-cycle",
                        "bun-cycle": "bun run bun-cycle",
                    }
                }
            ),
            encoding="utf-8",
        )
        for manager in ("pnpm", "yarn", "bun"):
            for script in ("precase", "maincase", "postcase"):
                command = f"{manager} run {script}"
                with self.subTest(command=command):
                    self.assertEqual(_run_write_hook(self.worktree, command).returncode, 2)
        for command in (
            "pnpm run pnpm-cycle",
            "yarn run yarn-cycle",
            "bun run bun-cycle",
        ):
            with self.subTest(command=command):
                self.assertEqual(_run_write_hook(self.worktree, command).returncode, 2)

        for command in (
            "npm --prefix . run local",
            "pnpm --dir . run local",
            "pnpm local",
            "yarn --cwd . run local",
            "yarn local",
            "bun --cwd . run local",
        ):
            with self.subTest(command=command):
                result = _run_write_hook(self.worktree, command)
                self.assertEqual(result.returncode, 0, (command, result.stderr))

        (self.worktree / "Makefile").write_text(
            (
                "safe:\n\tmkdir -p build\n\ttouch build/make.txt\n"
                f"escape:\n\ttouch {outside}\n"
                "chained: escape\n"
                "cycle-a: cycle-b\n"
                "cycle-b: cycle-a\n"
                f"inline: ; touch {outside}\n"
            ),
            encoding="utf-8",
        )
        for command in ("make escape", "make chained", "make cycle-a", "make inline"):
            with self.subTest(command=command):
                self.assertEqual(_run_write_hook(self.worktree, command).returncode, 2)
        for command in ("make safe", "make -C . safe", "make -f Makefile safe"):
            with self.subTest(command=command):
                result = _run_write_hook(self.worktree, command)
                self.assertEqual(result.returncode, 0, (command, result.stderr))

        for wrapper_name in ("gradlew", "mvnw"):
            wrapper = self.worktree / wrapper_name
            wrapper.write_text(
                "#!/bin/sh\nAPP_HOME=$(cd \"${0%/*}\" && pwd)\nexec java -jar \"$APP_HOME/wrapper.jar\" \"$@\"\n",
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            result = _run_write_hook(self.worktree, f"./{wrapper_name} test")
            self.assertEqual(result.returncode, 0, (wrapper_name, result.stderr))

    def test_redirect_variants_cannot_target_leader_or_outside(self) -> None:
        outside = str(self.root / "redirect-vector.txt")
        for redirect in (">|", ">>|", ">&", "&>", "&>>", "2>", "2>>", "2>|"):
            command = f"echo x {redirect} {outside}"
            with self.subTest(command=command):
                self.assertEqual(_run_write_hook(self.worktree, command).returncode, 2)
        for redirect in (">|", ">>|", ">&", "&>", "&>>", "2>", "2>>", "2>|"):
            command = f"git status {redirect} {outside}"
            with self.subTest(leader_command=command):
                self.assertEqual(_run_write_hook(self.root, command).returncode, 2)

        for command in (
            "echo x >| local.txt",
            "echo x >>| local.txt",
            "echo x >& local.txt",
            "echo x &> local.txt",
            "echo x &>> local.txt",
            "echo x 2> local.txt",
            "echo x 2>> local.txt",
            "echo x 2>| local.txt",
            "echo x 2>&1",
        ):
            with self.subTest(command=command):
                result = _run_write_hook(self.worktree, command)
                self.assertEqual(result.returncode, 0, (command, result.stderr))

    def test_registered_detached_worktree_allows_only_initial_start_across_hosts(self) -> None:
        for state_path in (
            self.root / ".agent-flow" / "state" / "current-run.json",
            self.root / ".agent-flow" / "runs" / "default" / "active" / "manifest.json",
        ):
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["status"] = "complete"
            state_path.write_text(json.dumps(state), encoding="utf-8")
        detached = Path(self.temp_dir.name) / "external-detached"
        subprocess.run(
            ("git", "worktree", "add", "-q", "--detach", str(detached), "main"),
            cwd=self.root,
            check=True,
        )
        payloads = (
            {"tool_name": "Bash", "tool_input": {"command": 'agent-flow run "detached task"'}},
            {"tool": "Bash", "input": {"command": 'agent-flow-kit run "detached task"'}},
            {
                "tool": "bash",
                "parameters": {"command": 'agent-flow-python run development --task "detached task"'},
            },
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                result = _run_write_hook_payload(detached, payload)
                self.assertEqual(result.returncode, 0, result.stderr)

        for command in (
            "agent-flow install",
            "agent-flow run advance",
            "git status --short",
            "touch local.txt",
            'agent-flow run "task" && touch local.txt',
        ):
            with self.subTest(command=command):
                result = _run_write_hook(detached, command)
                self.assertEqual(result.returncode, 2, (command, result.stderr))

    def test_direct_eval_and_python_tools_fail_closed_in_every_checkout(self) -> None:
        other = self.root / ".agent-flow" / "worktrees" / "feat-other"
        subprocess.run(
            (
                "git",
                "worktree",
                "add",
                "-q",
                "-b",
                "feat/other",
                str(other),
                "main",
            ),
            cwd=self.root,
            check=True,
        )
        payloads = (
            {"tool_name": "python", "tool_input": {"code": "print(1 + 1)"}},
            {"tool": "python", "input": {"source": "print(1 + 1)"}},
            {"tool": "eval", "parameters": {"language": "py", "code": "print(1 + 1)"}},
            {"tool": "eval", "input": {"language": "js", "code": "console.log(2)"}},
            {"tool_name": "python", "tool_input": {}},
            {"tool_name": "python", "tool_input": {"code": ["print(2)"]}},
        )
        for cwd in (self.root, self.worktree, other):
            for payload in payloads:
                with self.subTest(cwd=cwd.name, payload=payload):
                    result = _run_write_hook_payload(cwd, payload)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("embedded execution tool", result.stderr)

    def test_non_git_disabled_run_enforces_direct_tool_boundaries_across_hosts(self) -> None:
        non_git = Path(self.temp_dir.name) / "non-git-project"
        non_git.mkdir()
        _write_active_run(non_git, non_git)
        for state_path in (
            non_git / ".agent-flow" / "state" / "current-run.json",
            non_git / ".agent-flow" / "runs" / "default" / "active" / "manifest.json",
        ):
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["worktree_mode"] = "disabled"
            state_path.write_text(json.dumps(state), encoding="utf-8")
        (non_git / "package.json").write_text(
            json.dumps({"scripts": {"test": "echo ok"}}),
            encoding="utf-8",
        )

        blocked = (
            {"tool_name": "python", "tool_input": {"code": "print(1)"}},
            {"tool": "eval", "input": {"language": "js", "code": "1 + 1"}},
            {"tool": "eval", "parameters": {"language": "py", "code": "print(1)"}},
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(self.root / "outside.txt")},
            },
            {
                "tool": "apply_patch",
                "input": (
                    "*** Begin Patch\n"
                    f"*** Add File: {self.root / 'outside.txt'}\n"
                    "+x\n"
                    "*** End Patch"
                ),
            },
            {"tool": "write", "parameters": {"path": str(self.root / "outside.txt")}},
        )
        for payload in blocked:
            with self.subTest(blocked_payload=payload):
                result = _run_write_hook_payload(non_git, payload)
                self.assertEqual(result.returncode, 2, result.stderr)

        allowed = (
            {"tool_name": "Write", "tool_input": {"file_path": "claude-local.txt"}},
            {"tool": "write", "input": {"path": "codex-local.txt"}},
            {"tool": "write", "parameters": {"path": "omp-local.txt"}},
        )
        for payload in allowed:
            with self.subTest(allowed_payload=payload):
                result = _run_write_hook_payload(non_git, payload)
                self.assertEqual(result.returncode, 0, result.stderr)

        for command in ("pwd", "git status --short", "npm test", "touch local.txt"):
            for payload in _host_payloads(command):
                with self.subTest(safe_command=command, payload=payload):
                    result = _run_write_hook_payload(non_git, payload)
                    self.assertEqual(result.returncode, 0, result.stderr)

        outside_command = f"touch {self.root / 'non-git-outside.txt'}"
        for payload in _host_payloads(outside_command):
            with self.subTest(outside_command=payload):
                result = _run_write_hook_payload(non_git, payload)
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_non_git_python_disabled_run_uses_the_same_host_policy(self) -> None:
        non_git = Path(self.temp_dir.name) / "non-git-python-project"
        run_dir = non_git / ".agent-flow" / "runs" / "python-disabled"
        run_dir.mkdir(parents=True)
        (run_dir / "meta.json").write_text(
            json.dumps(
                {
                    "run_id": "python-disabled",
                    "workflow": "default",
                    "task": "python non-git task",
                    "worktree_mode": "disabled",
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "active").write_text("", encoding="utf-8")

        for payload in _host_payloads("touch local.txt"):
            with self.subTest(allowed_payload=payload):
                result = _run_write_hook_payload(non_git, payload)
                self.assertEqual(result.returncode, 0, result.stderr)

        outside = self.root / "python-non-git-outside.txt"
        for payload in _host_payloads(f"touch {outside}"):
            with self.subTest(blocked_payload=payload):
                result = _run_write_hook_payload(non_git, payload)
                self.assertEqual(result.returncode, 2, result.stderr)

        for payload in (
            {"tool_name": "python", "tool_input": {"code": "print(1)"}},
            {"tool": "eval", "input": {"language": "js", "code": "1 + 1"}},
            {"tool": "eval", "parameters": {"language": "py", "code": "print(1)"}},
        ):
            with self.subTest(embedded_payload=payload):
                result = _run_write_hook_payload(non_git, payload)
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_notebook_tools_require_an_explicit_path_inside_the_pinned_worktree(self) -> None:
        safe_notebook = self.worktree / "notebooks" / "safe.ipynb"
        safe_payloads = (
            {
                "tool_name": "NotebookEdit",
                "tool_input": {"notebook_path": str(safe_notebook), "new_source": "print(1)"},
            },
            {
                "tool": "notebook",
                "input": {"path": str(safe_notebook), "code": "print(1)"},
            },
            {
                "tool": "notebook",
                "parameters": {"notebookPath": str(safe_notebook), "cells": [{"code": "print(1)"}]},
            },
        )
        for payload in safe_payloads:
            with self.subTest(location="pinned", payload=payload):
                result = _run_write_hook_payload(self.worktree, payload)
                self.assertEqual(result.returncode, 0, result.stderr)

        outside = self.root / "leader.ipynb"
        blocked_payloads = (
            {
                "tool_name": "NotebookEdit",
                "tool_input": {"notebook_path": str(outside), "new_source": "print(1)"},
            },
            {
                "tool": "notebook",
                "input": {"path": str(outside), "code": "print(1)"},
            },
            {"tool": "notebook", "parameters": {"code": "print(1)"}},
            {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": [str(safe_notebook)]}},
        )
        for payload in blocked_payloads:
            with self.subTest(location="outside-or-missing", payload=payload):
                result = _run_write_hook_payload(self.worktree, payload)
                self.assertEqual(result.returncode, 2, result.stderr)

        relative_leader = _run_write_hook_payload(
            self.root,
            {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "leader.ipynb"}},
        )
        self.assertEqual(relative_leader.returncode, 2, relative_leader.stderr)

        other = self.root / ".agent-flow" / "worktrees" / "feat-notebook-other"
        subprocess.run(
            (
                "git",
                "worktree",
                "add",
                "-q",
                "-b",
                "feat/notebook-other",
                str(other),
                "main",
            ),
            cwd=self.root,
            check=True,
        )
        wrong_worktree = _run_write_hook_payload(
            other,
            {"tool": "notebook", "input": {"path": str(other / "wrong.ipynb")}},
        )
        self.assertEqual(wrong_worktree.returncode, 2, wrong_worktree.stderr)

        for state_path in (
            self.root / ".agent-flow" / "state" / "current-run.json",
            self.root / ".agent-flow" / "runs" / "default" / "active" / "manifest.json",
        ):
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["status"] = "complete"
            state_path.write_text(json.dumps(state), encoding="utf-8")
        no_active_run = _run_write_hook_payload(
            self.root,
            {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "leader.ipynb"}},
        )
        self.assertEqual(no_active_run.returncode, 2, no_active_run.stderr)

    def test_disabled_worktree_mode_allows_only_leader_internal_writes_across_hosts(self) -> None:
        state_paths = (
            self.root / ".agent-flow" / "state" / "current-run.json",
            self.root / ".agent-flow" / "runs" / "default" / "active" / "manifest.json",
        )
        for state_path in state_paths:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["workspace_root"] = str(self.root)
            state["worktree_mode"] = "disabled"
            state_path.write_text(json.dumps(state), encoding="utf-8")

        payloads = (
            {"tool_name": "Bash", "tool_input": {"command": "touch claude-local.txt"}},
            {"tool": "bash", "input": {"command": "mkdir -p codex-local"}},
            {"tool": "bash", "parameters": {"command": "echo omp > omp-local.txt"}},
            {"tool_name": "Write", "tool_input": {"file_path": "claude-write.txt"}},
            {
                "tool": "apply_patch",
                "input": "*** Begin Patch\n*** Add File: codex-write.txt\n+x\n*** End Patch",
            },
            {"tool": "write", "parameters": {"path": "omp-write.txt"}},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                result = _run_write_hook_payload(self.root, payload)
                self.assertEqual(result.returncode, 0, result.stderr)

        outside = self.root.parent / "outside.txt"
        blocked_payloads = (
            {"tool_name": "Bash", "tool_input": {"command": f"touch {outside}"}},
            {"tool": "write", "input": {"path": str(outside)}},
            {"tool_name": "Bash", "tool_input": {"command": "git commit -am blocked"}},
            {"tool": "bash", "parameters": {"command": "git push origin HEAD:main"}},
        )
        for payload in blocked_payloads:
            with self.subTest(blocked=payload):
                result = _run_write_hook_payload(self.root, payload)
                self.assertEqual(result.returncode, 2, result.stderr)

        for state_path in state_paths:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["worktree_mode"] = "required"
            state_path.write_text(json.dumps(state), encoding="utf-8")
        required = _run_write_hook(self.root, "touch still-blocked.txt")
        self.assertEqual(required.returncode, 2, required.stderr)

    def test_disabled_python_leader_run_uses_the_same_write_policy(self) -> None:
        for state_path in (
            self.root / ".agent-flow" / "state" / "current-run.json",
            self.root / ".agent-flow" / "runs" / "default" / "active" / "manifest.json",
        ):
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["status"] = "complete"
            state_path.write_text(json.dumps(state), encoding="utf-8")
        run_dir = self.root / ".agent-flow" / "runs" / "python-disabled"
        run_dir.mkdir(parents=True)
        (run_dir / "meta.json").write_text(
            json.dumps(
                {
                    "run_id": "python-disabled",
                    "workflow": "default",
                    "task": "leader task",
                    "worktree_mode": "disabled",
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "active").write_text("", encoding="utf-8")

        allowed = _run_write_hook(self.root, "touch python-local.txt")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        outside = self.root.parent / "python-outside.txt"
        blocked = _run_write_hook(self.root, f"touch {outside}")
        self.assertEqual(blocked.returncode, 2, blocked.stderr)


def _run_write_hook(cwd: Path, command: str) -> subprocess.CompletedProcess[str]:
    return _run_write_hook_payload(
        cwd,
        {"tool_name": "Bash", "tool_input": {"command": command}},
    )


def _run_write_hook_payload(
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


def _run_protected_hook(cwd: Path, command: str) -> subprocess.CompletedProcess[str]:
    return _run_protected_hook_payload(
        cwd,
        {"tool_name": "Bash", "tool_input": {"command": command}},
    )


def _run_protected_hook_payload(
    cwd: Path,
    payload: dict[str, object],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("/bin/bash", str(PROTECTED_HOOK)),
        cwd=cwd,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def _run_worktree_hook_payload(
    cwd: Path,
    payload: dict[str, object],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("/bin/bash", str(WORKTREE_HOOK)),
        cwd=cwd,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=env,
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
