from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from agent_flow.core.host_bridge import ensure_worktree_host_bridge
from agent_flow.core.codex_trust import (
    acquire_codex_config_transaction_lock,
    release_codex_config_transaction_lock,
)
from agent_flow.core.skill_plan import MANAGED_HOOK_VERIFIER


os.environ.setdefault("AGENT_FLOW_SKIP_CODEX_TRUST", "1")


CONTEXT_PATHS = ("AGENTS.md", "CLAUDE.md")
REVIEWER_PATHS = (
    ".Codex/agents/code-reviewer.md",
    ".claude/agents/code-reviewer.md",
    ".omp/agents/code-reviewer.md",
)
REGULAR_HOST_PATHS = (
    ".Codex/hooks.json",
    ".codex/hooks.json",
    ".claude/settings.json",
)
SYMLINK_PATHS = (
    ".agent-flow/bin",
    ".agent-flow/skills",
    ".agents/skills",
    ".claude/skills",
    ".omp/extensions/agent-flow-hooks.ts",
    ".omp/skills",
)
BRIDGE_PATHS = CONTEXT_PATHS + REVIEWER_PATHS + REGULAR_HOST_PATHS + SYMLINK_PATHS


def _configure_fake_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str = "ok"
) -> Path:
    binary = tmp_path / "fake-codex"
    shutil.copyfile(
        Path(__file__).parent / "fixtures/fake_codex_app_server.py",
        binary,
    )
    binary.chmod(0o755)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.delenv("AGENT_FLOW_SKIP_CODEX_TRUST", raising=False)
    monkeypatch.setenv("CODEX_CLI_PATH", str(binary))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("FAKE_CODEX_SCRIPT_MUTATION", raising=False)
    monkeypatch.delenv("FAKE_CODEX_SCRIPT_MUTATION_QUERY", raising=False)
    if mode == "ok":
        monkeypatch.delenv("FAKE_CODEX_MODE", raising=False)
    else:
        monkeypatch.setenv("FAKE_CODEX_MODE", mode)
    return codex_home


def _flow_block(version: str) -> str:
    return "\n".join(
        (
            "<!-- agent-flow:start -->",
            "## Agent Flow",
            f"contract: {version}",
            "hosts: Claude/Codex/OMP",
            "<!-- agent-flow:end -->",
        )
    )


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(("git", *args), cwd=cwd, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


def _user_claude_settings() -> dict[str, object]:
    return {
        "theme": "dark",
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "user-hook"}],
                }
            ]
        },
    }


def _installed_hook_settings(
    leader: Path, version: str, host: str
) -> dict[str, object]:
    config = _user_claude_settings() if host == "claude" else {"host": host}
    hooks = config.setdefault("hooks", {})
    pre_tool = hooks.setdefault("PreToolUse", [])
    write_matcher = (
        "^(apply_patch|Write|Edit|MultiEdit|NotebookEdit|Eval|Python|Notebook|write|"
        "edit|multi_edit|multiedit|notebook_edit|notebookedit|eval|python|notebook)$"
    )

    def command_hook(script_name: str) -> dict[str, str]:
        script_path = (
            leader.resolve(strict=True)
            / ".agent-flow"
            / "scripts"
            / "hooks"
            / script_name
        )
        digest = hashlib.sha256(script_path.read_bytes()).hexdigest()

        def shell_quote(value: object) -> str:
            return "'" + str(value).replace("'", "'\\''") + "'"

        command = " ".join(
            (
                shell_quote("/usr/bin/python3"),
                "-I",
                "-c",
                shell_quote(MANAGED_HOOK_VERIFIER),
                shell_quote(
                    base64.b64encode(script_path.as_posix().encode("utf-8")).decode(
                        "ascii"
                    )
                ),
                shell_quote(digest),
            )
        )
        return {
            "type": "command",
            "command": command,
            "bridgeVersion": version,
        }

    bash = next((entry for entry in pre_tool if entry.get("matcher") == "Bash"), None)
    if bash is None:
        bash = {"matcher": "Bash", "hooks": []}
        pre_tool.append(bash)
    bash["hooks"].extend(
        (
            command_hook("guard-worktree.sh"),
            command_hook("guard-protected-branch.sh"),
            command_hook("guard-worktree-write.py"),
        )
    )
    pre_tool.append(
        {
            "matcher": write_matcher,
            "hooks": [command_hook("guard-worktree-write.py")],
        }
    )
    hooks["PostToolUse"] = [
        {
            "matcher": write_matcher,
            "hooks": [command_hook("comment-checker.py")],
        }
    ]
    hooks["Stop"] = [
        {
            "hooks": [command_hook("show-phase-status.sh")]
        }
    ]
    return config


def _seed_installed_leader(leader: Path, version: str) -> None:
    for script_name in (
        "guard-worktree.sh",
        "guard-worktree-write.py",
        "guard-protected-branch.sh",
        "show-phase-status.sh",
        "comment-checker.py",
    ):
        script = leader / ".agent-flow/scripts/hooks" / script_name
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(f"# {script_name}\n", encoding="utf-8")
        script.chmod(0o755)
    files = {
        "AGENTS.md": f"# Leader AGENTS\nleader-only\n{_flow_block(version)}\nleader-tail\n",
        "CLAUDE.md": f"# Leader CLAUDE\nleader-only\n{_flow_block(version)}\nleader-tail\n",
        ".Codex/agents/code-reviewer.md": f"# Codex Reviewer\nreview: {version}\n",
        ".claude/agents/code-reviewer.md": (
            "---\nname: code-reviewer\ndescription: Review code\n---\n\n"
            f"# Claude Reviewer\nreview: {version}\n"
        ),
        ".Codex/hooks.json": json.dumps(
            _installed_hook_settings(leader, version, "codex"), indent=2
        )
        + "\n",
        ".codex/hooks.json": json.dumps(
            _installed_hook_settings(leader, version, "codex-lower"), indent=2
        )
        + "\n",
        ".claude/settings.json": json.dumps(
            _installed_hook_settings(leader, version, "claude"), indent=2
        )
        + "\n",
        ".omp/extensions/agent-flow-hooks.ts": f'export const version = "{version}";\n',
        ".agent-flow/bin/agent-flow": "#!/usr/bin/env node\n",
        ".agent-flow/skills/index.json": json.dumps(
            {
                "version": 2,
                "skills": [
                    {"name": "demo", "path": ".agent-flow/skills/demo/SKILL.md"}
                ],
            },
            indent=2,
        )
        + "\n",
        ".agent-flow/skills/demo/SKILL.md": f"# canonical {version}\n",
        ".agents/skills/demo/SKILL.md": f"# codex {version}\n",
        ".claude/skills/demo/SKILL.md": f"# claude {version}\n",
        ".omp/skills/demo/SKILL.md": f"# omp {version}\n",
    }
    for relative, content in files.items():
        destination = leader / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    _seed_managed_hook_kit(leader)


def _seed_managed_hook_kit(leader: Path) -> None:
    matcher = (
        "^(apply_patch|Write|Edit|MultiEdit|NotebookEdit|Eval|Python|Notebook|write|"
        "edit|multi_edit|multiedit|notebook_edit|notebookedit|eval|python|notebook)$"
    )
    projection = sorted(
        (
            ["PostToolUse", matcher, "command", "comment-checker.py"],
            ["PreToolUse", "Bash", "command", "guard-protected-branch.sh"],
            ["PreToolUse", "Bash", "command", "guard-worktree-write.py"],
            ["PreToolUse", "Bash", "command", "guard-worktree.sh"],
            ["PreToolUse", matcher, "command", "guard-worktree-write.py"],
            ["Stop", "", "command", "show-phase-status.sh"],
        )
    )
    projection_hash = hashlib.sha256(
        json.dumps(projection, separators=(",", ":")).encode()
    ).hexdigest()
    configs = {
        relative: {"sha256": projection_hash}
        for relative in (
            ".Codex/hooks.json",
            ".claude/settings.json",
            ".codex/hooks.json",
        )
    }
    scripts = {}
    for name in (
        "comment-checker.py",
        "guard-protected-branch.sh",
        "guard-worktree-write.py",
        "guard-worktree.sh",
        "show-phase-status.sh",
    ):
        relative = f".agent-flow/scripts/hooks/{name}"
        scripts[relative] = {
            "sha256": hashlib.sha256((leader / relative).read_bytes()).hexdigest(),
            "mode": "executable",
        }
    contract = {"version": 2, "configs": configs, "scripts": scripts}
    skill_plan_hash = "0" * 64
    commitment_payload = {
        "version": 2,
        "skill_plan_hash": skill_plan_hash,
        "configs": [
            [relative, entry["sha256"]]
            for relative, entry in sorted(configs.items())
        ],
        "scripts": [
            [relative, entry["sha256"], "executable"]
            for relative, entry in sorted(scripts.items())
        ],
    }
    commitment = hashlib.sha256(
        json.dumps(commitment_payload, separators=(",", ":")).encode()
    ).hexdigest()
    (leader / ".agent-flow/kit.json").write_text(
        json.dumps(
            {
                "skill_plan_hash": skill_plan_hash,
                "managed_hook_contract": contract,
                "managed_hook_contract_commitment_version": 2,
                "managed_hook_contract_commitment": commitment,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _setup_repository(
    tmp_path: Path,
    *,
    tracked_context: bool = True,
    tracked_context_bytes: dict[str, bytes] | None = None,
    tracked_reviewer_conflict: bool = False,
    tracked_host_settings: bool = True,
    seed_installed: bool = True,
) -> Path:
    leader = tmp_path / "leader"
    leader.mkdir()
    _git(leader, "init", "-b", "main")
    _git(leader, "config", "user.email", "test@example.com")
    _git(leader, "config", "user.name", "Test User")
    (leader / "README.md").write_text("fixture\n", encoding="utf-8")
    (leader / ".gitignore").write_text("kept-entry\n", encoding="utf-8")
    tracked = ["README.md", ".gitignore"]
    if tracked_context:
        context = tracked_context_bytes or {
            "AGENTS.md": b"# User AGENTS\nkeep-agents\n",
            "CLAUDE.md": (
                f"# User CLAUDE\nkeep-before\n{_flow_block('base')}\nkeep-after\n"
            ).encode("utf-8"),
        }
        (leader / "AGENTS.md").write_bytes(context["AGENTS.md"])
        (leader / "CLAUDE.md").write_bytes(context["CLAUDE.md"])
        tracked.extend(("AGENTS.md", "CLAUDE.md"))
    if tracked_reviewer_conflict:
        for relative in (
            ".Codex/agents/code-reviewer.md",
            ".claude/agents/code-reviewer.md",
        ):
            destination = leader / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(f"user-owned {relative}\n", encoding="utf-8")
            tracked.append(relative)
    if tracked_host_settings:
        settings = leader / ".claude/settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps(_user_claude_settings(), indent=2) + "\n", encoding="utf-8"
        )
        tracked.append(".claude/settings.json")
    _git(leader, "add", *tracked)
    _git(leader, "commit", "-m", "base")
    if seed_installed:
        _seed_installed_leader(leader, "v1")
    return leader


def _add_worktree(leader: Path, destination: Path, branch: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _git(leader, "worktree", "add", "-b", branch, str(destination), "main")


def _seed_worktree_codex_hooks(leader: Path, worktree: Path) -> None:
    for relative in (".Codex/hooks.json", ".codex/hooks.json"):
        destination = worktree / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(leader / relative, destination)


def _bridge_manifest(leader: Path, worktree: Path) -> tuple[Path, bytes, dict[str, object]]:
    directory = leader / ".git/agent-flow/worktree-host-bridges"
    for manifest_path in directory.glob("*.json"):
        content = manifest_path.read_bytes()
        value = json.loads(content)
        if value["workspace_root"] == str(worktree.resolve()):
            return manifest_path, content, value
    raise AssertionError(f"manifest not found for {worktree}")


def _outside_flow_block(text: str) -> str:
    start = text.index("<!-- agent-flow:start -->")
    end = text.index("<!-- agent-flow:end -->") + len("<!-- agent-flow:end -->")
    return text[:start] + "<FLOW-BLOCK>" + text[end:]


def _assert_bridge(leader: Path, worktree: Path, version: str | None = "v1") -> None:
    for relative in CONTEXT_PATHS + REVIEWER_PATHS + REGULAR_HOST_PATHS:
        destination = worktree / relative
        assert destination.is_file() and not destination.is_symlink(), relative
    for relative in SYMLINK_PATHS:
        destination = worktree / relative
        assert destination.is_symlink(), relative
        assert os.path.samefile(destination, leader / relative), relative
    if version:
        for relative in CONTEXT_PATHS:
            assert f"contract: {version}" in (worktree / relative).read_text(encoding="utf-8")
    assert (worktree / REVIEWER_PATHS[0]).read_bytes() == (
        leader / ".Codex/agents/code-reviewer.md"
    ).read_bytes()
    for relative in REVIEWER_PATHS[1:]:
        assert (worktree / relative).read_bytes() == (
            leader / ".claude/agents/code-reviewer.md"
        ).read_bytes()


def test_python_bridge_preserves_tracked_user_context_and_settings(tmp_path: Path) -> None:
    leader = _setup_repository(tmp_path)
    internal = leader / ".agent-flow/worktrees/feat-internal"
    external = tmp_path / "external"
    _add_worktree(leader, internal, "feat/internal")
    _add_worktree(leader, external, "feat/external")

    ensure_worktree_host_bridge(leader_root=leader, worktree_root=internal)
    ensure_worktree_host_bridge(leader_root=leader, worktree_root=external)
    manifest_before = _bridge_manifest(leader, external)[1]
    exclude_before = (leader / ".git/info/exclude").read_bytes()
    ensure_worktree_host_bridge(leader_root=leader, worktree_root=external)

    _assert_bridge(leader, internal)
    _assert_bridge(leader, external)
    assert _outside_flow_block((external / "AGENTS.md").read_text(encoding="utf-8")) == (
        "# User AGENTS\nkeep-agents\n\n<FLOW-BLOCK>\n"
    )
    assert _outside_flow_block((external / "CLAUDE.md").read_text(encoding="utf-8")) == (
        "# User CLAUDE\nkeep-before\n<FLOW-BLOCK>\nkeep-after\n"
    )
    settings = json.loads((external / ".claude/settings.json").read_text(encoding="utf-8"))
    assert settings["theme"] == "dark"
    bash_hooks = settings["hooks"]["PreToolUse"][0]["hooks"]
    assert bash_hooks[0] == {"type": "command", "command": "user-hook"}
    encoded_path = re.search(
        r" '([A-Za-z0-9+/=]+)' '[0-9a-f]{64}'$",
        bash_hooks[1]["command"],
    )
    assert encoded_path is not None
    assert base64.b64decode(encoded_path.group(1), validate=True).decode(
        "utf-8"
    ).endswith("/.agent-flow/scripts/hooks/guard-worktree.sh")
    assert _bridge_manifest(leader, external)[1] == manifest_before
    assert (leader / ".git/info/exclude").read_bytes() == exclude_before
    assert _git(external, "status", "--short", "--untracked-files=all").splitlines() == [
        " M .claude/settings.json",
        " M AGENTS.md",
        " M CLAUDE.md",
    ]
    assert sorted(path.name for path in (external / ".agent-flow").iterdir()) == [
        "bin",
        "skills",
    ]


def test_python_refreshes_managed_content_and_rejects_user_json_or_block_tamper(
    tmp_path: Path,
) -> None:
    leader = _setup_repository(tmp_path)
    worktree = tmp_path / "refresh"
    _add_worktree(leader, worktree, "feat/refresh")
    ensure_worktree_host_bridge(leader_root=leader, worktree_root=worktree)
    agents = worktree / "AGENTS.md"
    agents.write_text(agents.read_text(encoding="utf-8") + "user-tail\n", encoding="utf-8")
    outside_before = _outside_flow_block(agents.read_text(encoding="utf-8"))
    _seed_installed_leader(leader, "v2")

    ensure_worktree_host_bridge(leader_root=leader, worktree_root=worktree)
    _assert_bridge(leader, worktree, "v2")
    assert _outside_flow_block(agents.read_text(encoding="utf-8")) == outside_before
    settings_path = worktree / ".claude/settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["hooks"]["PreToolUse"][0]["hooks"][1]["bridgeVersion"] == "v2"

    settings["theme"] = "light"
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="user hook settings differ"):
        ensure_worktree_host_bridge(leader_root=leader, worktree_root=worktree)
    assert json.loads(settings_path.read_text(encoding="utf-8"))["theme"] == "light"
    settings["theme"] = "dark"
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")

    agents.write_text(
        agents.read_text(encoding="utf-8").replace("contract: v2", "contract: tampered"),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="managed worktree context block was modified"):
        ensure_worktree_host_bridge(leader_root=leader, worktree_root=worktree)


def test_python_rejects_reviewer_conflicts_and_source_or_destination_symlinks(
    tmp_path: Path,
) -> None:
    conflict_root = tmp_path / "conflict-root"
    conflict_root.mkdir()
    conflict = _setup_repository(conflict_root, tracked_reviewer_conflict=True)
    conflicting_worktree = conflict_root / "worktree"
    _add_worktree(conflict, conflicting_worktree, "feat/conflict")
    agents_before = (conflicting_worktree / "AGENTS.md").read_bytes()
    with pytest.raises(RuntimeError, match="unmanaged host bridge path already exists"):
        ensure_worktree_host_bridge(
            leader_root=conflict, worktree_root=conflicting_worktree
        )
    assert (conflicting_worktree / "AGENTS.md").read_bytes() == agents_before
    assert not (conflicting_worktree / ".omp").exists()

    unsafe_root = tmp_path / "unsafe-root"
    unsafe_root.mkdir()
    unsafe = _setup_repository(unsafe_root)
    unsafe_worktree = unsafe_root / "worktree"
    _add_worktree(unsafe, unsafe_worktree, "feat/unsafe")
    outside = unsafe_root / "outside"
    outside.mkdir()
    (unsafe_worktree / ".omp").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="unsafe host bridge parent path"):
        ensure_worktree_host_bridge(leader_root=unsafe, worktree_root=unsafe_worktree)
    assert os.path.samefile(unsafe_worktree / ".omp", outside)

    source_root = tmp_path / "source-root"
    source_root.mkdir()
    source_leader = _setup_repository(source_root)
    source_worktree = source_root / "worktree"
    _add_worktree(source_leader, source_worktree, "feat/source")
    real_skills = source_root / "real-skills"
    (source_leader / ".claude/skills").rename(real_skills)
    (source_leader / ".claude/skills").symlink_to(real_skills, target_is_directory=True)
    with pytest.raises(RuntimeError, match="unsafe leader host bridge source symlink"):
        ensure_worktree_host_bridge(
            leader_root=source_leader, worktree_root=source_worktree
        )


@pytest.mark.parametrize(
    ("relative", "wrong_type"),
    (
        (".agents/skills", "file"),
        (".claude/skills", "file"),
        (".omp/skills", "file"),
        (".omp/extensions/agent-flow-hooks.ts", "directory"),
        (".Codex/agents/code-reviewer.md", "directory"),
        (".claude/agents/code-reviewer.md", "directory"),
    ),
)
def test_python_rejects_bridge_sources_with_the_wrong_spec_type(
    tmp_path: Path, relative: str, wrong_type: str
) -> None:
    leader = _setup_repository(tmp_path)
    worktree = tmp_path / "wrong-type"
    _add_worktree(leader, worktree, "feat/wrong-type")
    source = leader / relative
    if source.is_dir():
        shutil.rmtree(source)
    else:
        source.unlink()
    if wrong_type == "file":
        source.write_text("wrong source type\n", encoding="utf-8")
    else:
        source.mkdir()
    agents_before = (worktree / "AGENTS.md").read_bytes()

    with pytest.raises(RuntimeError, match="invalid leader host bridge source type"):
        ensure_worktree_host_bridge(leader_root=leader, worktree_root=worktree)

    assert (worktree / "AGENTS.md").read_bytes() == agents_before
    assert not (worktree / ".omp").exists()


def test_python_preserves_tracked_context_bytes_when_appending_first_flow_block(
    tmp_path: Path,
) -> None:
    tracked_context_bytes = {
        "AGENTS.md": b"# User AGENTS\r\nkeep-agents  \t",
        "CLAUDE.md": b"# User CLAUDE\r\nkeep-claude \t\r\n",
    }
    leader = _setup_repository(
        tmp_path, tracked_context_bytes=tracked_context_bytes
    )
    worktree = tmp_path / "crlf"
    _add_worktree(leader, worktree, "feat/crlf")
    for relative in CONTEXT_PATHS:
        assert (worktree / relative).read_bytes() == tracked_context_bytes[relative]

    ensure_worktree_host_bridge(leader_root=leader, worktree_root=worktree)

    expected_suffixes = {
        "AGENTS.md": f"\r\n\r\n{_flow_block('v1')}\n".encode("utf-8"),
        "CLAUDE.md": f"\r\n{_flow_block('v1')}\n".encode("utf-8"),
    }
    before_refresh: dict[str, bytes] = {}
    for relative in CONTEXT_PATHS:
        expected = tracked_context_bytes[relative] + expected_suffixes[relative]
        actual = (worktree / relative).read_bytes()
        assert actual == expected
        before_refresh[relative] = actual

    ensure_worktree_host_bridge(leader_root=leader, worktree_root=worktree)
    for relative in CONTEXT_PATHS:
        assert (worktree / relative).read_bytes() == before_refresh[relative]


def test_python_registers_actual_codex_worktree_hook_keys_and_preserves_user_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leader = _setup_repository(tmp_path)
    worktree = tmp_path / "codex-trusted"
    _add_worktree(leader, worktree, "feat/codex-trusted")
    codex_home = _configure_fake_codex(tmp_path, monkeypatch)
    config_path = codex_home / "config.toml"
    user_prefix = '# user-owned bytes  \n[custom]\nvalue = "keep"\n'
    config_path.write_text(user_prefix, encoding="utf-8")
    config_path.chmod(0o640)

    ensure_worktree_host_bridge(leader_root=leader, worktree_root=worktree)

    after_first = config_path.read_bytes()
    config = after_first.decode("utf-8")
    assert config.startswith(user_prefix)
    assert f"[projects.{json.dumps(str(worktree.resolve()))}]" in config
    assert len(re.findall(r'trusted_hash = "sha256:[0-9a-f]{64}"', config)) == 6
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640
    _assert_bridge(leader, worktree)

    ensure_worktree_host_bridge(leader_root=leader, worktree_root=worktree)
    assert config_path.read_bytes() == after_first


@pytest.mark.skipif(os.name == "nt", reason="hard-kill recovery requires SIGKILL")
def test_node_recovers_python_worktree_codex_trust_after_hard_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leader = _setup_repository(tmp_path)
    worktree = tmp_path / "codex-python-crash"
    _add_worktree(leader, worktree, "feat/codex-python-crash")
    _seed_worktree_codex_hooks(leader, worktree)
    codex_home = _configure_fake_codex(tmp_path, monkeypatch)
    config_path = codex_home / "config.toml"
    original = b'# durable user bytes  \n[custom]\nvalue = "keep"\n'
    config_path.write_bytes(original)
    config_path.chmod(0o640)
    crash_script = "; ".join(
        (
            "from pathlib import Path",
            "from agent_flow.core.codex_trust import ensure_codex_worktree_hook_trust",
            "import sys",
            "ensure_codex_worktree_hook_trust(leader_root=Path(sys.argv[1]), worktree_root=Path(sys.argv[2]))",
        )
    )
    crashed = subprocess.run(
        ("python3", "-c", crash_script, str(leader), str(worktree)),
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "AGENT_FLOW_TEST_HARD_KILL_AFTER_WORKTREE_CODEX_TRUST_WRITE": "1",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr or crashed.stdout
    assert config_path.read_bytes() != original
    transaction_path = leader / ".git/agent-flow/codex-worktree-trust.json"
    assert transaction_path.is_file()

    module_uri = (
        Path(__file__).resolve().parents[1] / "lib/codex-hook-trust.mjs"
    ).as_uri()
    recovery_script = "\n".join(
        (
            f"import {{ ensureCodexWorktreeHookTrust }} from {json.dumps(module_uri)};",
            "ensureCodexWorktreeHookTrust({ leaderRoot: process.argv[1], worktreeRoot: process.argv[2] });",
        )
    )
    recovered = subprocess.run(
        (
            "node",
            "--input-type=module",
            "--eval",
            recovery_script,
            str(leader),
            str(worktree),
        ),
        env={
            **os.environ,
            "AGENT_FLOW_SKIP_CODEX_TRUST": "1",
            "CODEX_HOME": str(tmp_path / "different-codex-home"),
            "AGENT_FLOW_TEST_HARD_KILL_AFTER_WORKTREE_CODEX_TRUST_WRITE": "0",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert recovered.returncode == 0, recovered.stderr or recovered.stdout
    assert config_path.read_bytes() == original
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640
    assert not transaction_path.exists()


def test_node_observes_python_config_global_lock_across_repositories(
    tmp_path: Path,
) -> None:
    leader = _setup_repository(tmp_path)
    codex_home = tmp_path / "shared-lock-codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    original = b'shared = "config"\n'
    config_path.write_bytes(original)
    config_path.chmod(0o640)
    state_root = leader / ".git/agent-flow"
    state_root.mkdir()
    transaction = acquire_codex_config_transaction_lock(
        leader_root=leader,
        config_path=config_path,
        journal_path=state_root / "codex-worktree-trust.json",
        original={"exists": True, "content": original, "mode": 0o640},
    )
    module_uri = (
        Path(__file__).resolve().parents[1] / "lib/codex-hook-trust.mjs"
    ).as_uri()
    script = "\n".join(
        (
            f"import {{ recoverCodexConfigTransactionLock }} from {json.dumps(module_uri)};",
            "recoverCodexConfigTransactionLock(process.argv[1]);",
        )
    )
    blocked = subprocess.run(
        (
            "node",
            "--input-type=module",
            "--eval",
            script,
            str(config_path),
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert blocked.returncode != 0
    assert "transaction is active" in blocked.stderr
    release_codex_config_transaction_lock(transaction)
    assert config_path.read_bytes() == original
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640
    assert not (codex_home / ".config.toml.agent-flow.lock.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="hard-kill recovery requires SIGKILL")
def test_node_recovery_preserves_external_bytes_in_python_displacement_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leader = _setup_repository(tmp_path)
    worktree = tmp_path / "python-displacement-window"
    _add_worktree(leader, worktree, "feat/python-displacement-window")
    _seed_worktree_codex_hooks(leader, worktree)
    codex_home = _configure_fake_codex(tmp_path, monkeypatch)
    config_path = codex_home / "config.toml"
    config_path.write_bytes(b'original = "config"\n')
    config_path.chmod(0o640)
    crash_script = "; ".join(
        (
            "from pathlib import Path",
            "from agent_flow.core.codex_trust import ensure_codex_worktree_hook_trust",
            "import sys",
            "ensure_codex_worktree_hook_trust(leader_root=Path(sys.argv[1]), worktree_root=Path(sys.argv[2]))",
        )
    )
    crashed = subprocess.run(
        ("python3", "-c", crash_script, str(leader), str(worktree)),
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "AGENT_FLOW_TEST_HARD_KILL_AFTER_WORKTREE_CODEX_TRUST_DISPLACE": "1",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr or crashed.stdout
    receipt_path = codex_home / ".config.toml.agent-flow.lock.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    displaced_path = Path(receipt["rounds"][0]["displaced_path"])
    external = b'external = "preserved"\n'
    displaced_path.write_bytes(external)
    displaced_path.chmod(0o600)
    module_uri = (
        Path(__file__).resolve().parents[1] / "lib/codex-hook-trust.mjs"
    ).as_uri()
    recovery_script = "\n".join(
        (
            f"import {{ ensureCodexWorktreeHookTrust }} from {json.dumps(module_uri)};",
            "ensureCodexWorktreeHookTrust({ leaderRoot: process.argv[1], worktreeRoot: process.argv[2] });",
        )
    )
    recovered = subprocess.run(
        (
            "node",
            "--input-type=module",
            "--eval",
            recovery_script,
            str(leader),
            str(worktree),
        ),
        env={**os.environ, "AGENT_FLOW_SKIP_CODEX_TRUST": "1"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert recovered.returncode == 0, recovered.stderr or recovered.stdout
    assert config_path.read_bytes() == external
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert not receipt_path.exists()
    assert not (leader / ".git/agent-flow/codex-worktree-trust.json").exists()


def test_python_codex_worktree_trust_fails_closed_on_semantic_toml_table_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leader = _setup_repository(tmp_path)
    worktree = tmp_path / "codex-semantic-table"
    _add_worktree(leader, worktree, "feat/codex-semantic-table")
    codex_home = _configure_fake_codex(tmp_path, monkeypatch)
    config_path = codex_home / "config.toml"
    original = (
        f"[ projects . {json.dumps(str(worktree.resolve()))} ]\n"
        'trust_level = "untrusted"\n'
    ).encode("utf-8")
    config_path.write_bytes(original)
    config_path.chmod(0o640)

    with pytest.raises(
        RuntimeError,
        match="equivalent Codex config TOML target cannot be edited losslessly",
    ):
        ensure_worktree_host_bridge(leader_root=leader, worktree_root=worktree)

    assert config_path.read_bytes() == original
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640


def test_python_rolls_bridge_and_user_codex_config_back_when_discovery_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leader = _setup_repository(tmp_path)
    worktree = tmp_path / "codex-failure"
    _add_worktree(leader, worktree, "feat/codex-failure")
    codex_home = _configure_fake_codex(tmp_path, monkeypatch, "fail-query")
    config_path = codex_home / "config.toml"
    config_before = b'# keep exactly  \n[custom]\nvalue = "user"\n'
    config_path.write_bytes(config_before)
    config_path.chmod(0o600)
    exclude_path = leader / ".git/info/exclude"
    exclude_before = exclude_path.read_bytes()
    agents_before = (worktree / "AGENTS.md").read_bytes()

    with pytest.raises(RuntimeError, match="Codex worktree hook discovery failed"):
        ensure_worktree_host_bridge(leader_root=leader, worktree_root=worktree)

    assert config_path.read_bytes() == config_before
    assert exclude_path.read_bytes() == exclude_before
    assert (worktree / "AGENTS.md").read_bytes() == agents_before
    assert not (worktree / ".omp").exists()
    assert not (leader / ".git/agent-flow/worktree-host-bridges").exists()


@pytest.mark.parametrize(
    "mode",
    (
        "subset-managed",
        "extra-managed",
        "duplicate-managed",
        "verify-subset-managed",
        "verify-extra-managed",
        "verify-duplicate-managed",
    ),
)
def test_python_rejects_managed_hook_subset_extra_and_duplicate_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    leader = _setup_repository(tmp_path)
    worktree = tmp_path / f"codex-{mode}"
    _add_worktree(leader, worktree, f"feat/codex-{mode}")
    codex_home = _configure_fake_codex(tmp_path, monkeypatch, mode)
    config_path = codex_home / "config.toml"
    config_before = f'# {mode}\n[custom]\nvalue = "user"\n'.encode()
    config_path.write_bytes(config_before)
    config_path.chmod(0o640)

    with pytest.raises(
        RuntimeError,
        match="incomplete, extra, or duplicate managed hook set",
    ):
        ensure_worktree_host_bridge(leader_root=leader, worktree_root=worktree)

    assert config_path.read_bytes() == config_before
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640
    assert not (worktree / ".omp").exists()


@pytest.mark.parametrize(
    ("mutation", "query"),
    (("content", "1"), ("symlink", "1"), ("mode", "2")),
)
def test_python_rejects_managed_hook_script_races(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    query: str,
) -> None:
    leader = _setup_repository(tmp_path)
    worktree = tmp_path / f"codex-script-{mutation}"
    _add_worktree(leader, worktree, f"feat/codex-script-{mutation}")
    codex_home = _configure_fake_codex(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_CODEX_SCRIPT_MUTATION", mutation)
    monkeypatch.setenv("FAKE_CODEX_SCRIPT_MUTATION_QUERY", query)
    config_path = codex_home / "config.toml"
    config_before = f'# {mutation}\n[custom]\nvalue = "user"\n'.encode()
    config_path.write_bytes(config_before)
    config_path.chmod(0o640)

    with pytest.raises(
        RuntimeError,
        match=r"managed hook script .*guard-worktree\.sh",
    ):
        ensure_worktree_host_bridge(leader_root=leader, worktree_root=worktree)

    assert config_path.read_bytes() == config_before
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640
    assert not (worktree / ".omp").exists()


def test_python_rolls_back_context_settings_exclude_and_links_on_manifest_failure(
    tmp_path: Path,
) -> None:
    leader = _setup_repository(tmp_path)
    worktree = tmp_path / "rollback"
    _add_worktree(leader, worktree, "feat/rollback")
    before = {
        relative: (worktree / relative).read_bytes()
        for relative in BRIDGE_PATHS
        if (worktree / relative).is_file() and not (worktree / relative).is_symlink()
    }
    exclude_path = leader / ".git/info/exclude"
    exclude_before = exclude_path.read_bytes()
    real_link = os.link

    def fail_manifest(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        if "worktree-host-bridges" in str(destination):
            raise OSError("injected manifest failure")
        real_link(source, destination)

    with mock.patch("agent_flow.core.host_bridge.os.link", side_effect=fail_manifest):
        with pytest.raises(OSError, match="injected manifest failure"):
            ensure_worktree_host_bridge(leader_root=leader, worktree_root=worktree)

    for relative in BRIDGE_PATHS:
        destination = worktree / relative
        if relative in before:
            assert destination.read_bytes() == before[relative], relative
        else:
            assert not destination.exists() and not destination.is_symlink(), relative
    assert exclude_path.read_bytes() == exclude_before
    assert not (leader / ".git/agent-flow/worktree-host-bridges").exists()


def test_python_rollback_preserves_paths_whose_inode_or_link_ownership_changed(
    tmp_path: Path,
) -> None:
    leader = _setup_repository(tmp_path)
    worktree = tmp_path / "rollback-ownership"
    _add_worktree(leader, worktree, "feat/rollback-ownership")
    replaced_file = worktree / ".Codex/agents/code-reviewer.md"
    linked_file = worktree / ".Codex/hooks.json"
    link_alias = tmp_path / "hook-settings-alias.json"
    replaced_symlink = worktree / ".agents/skills"
    real_link = os.link

    def fail_after_ownership_change(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        if "worktree-host-bridges" in str(destination):
            replacement = replaced_file.with_name(f"{replaced_file.name}.replacement")
            replacement.write_bytes(replaced_file.read_bytes())
            replacement.chmod(stat.S_IMODE(replaced_file.stat().st_mode))
            os.replace(replacement, replaced_file)
            real_link(linked_file, link_alias)
            target = os.readlink(replaced_symlink)
            replaced_symlink.unlink()
            replaced_symlink.symlink_to(target, target_is_directory=True)
            raise OSError("injected manifest failure after ownership change")
        real_link(source, destination)

    with mock.patch(
        "agent_flow.core.host_bridge.os.link",
        side_effect=fail_after_ownership_change,
    ):
        with pytest.raises(
            OSError, match="injected manifest failure after ownership change"
        ):
            ensure_worktree_host_bridge(
                leader_root=leader, worktree_root=worktree
            )

    assert replaced_file.exists()
    assert linked_file.exists()
    assert linked_file.stat().st_nlink == 2
    assert replaced_symlink.is_symlink()


def test_python_rollback_aggregates_runtime_errors_with_the_original_failure(
    tmp_path: Path,
) -> None:
    leader = _setup_repository(tmp_path)
    worktree = tmp_path / "rollback-runtime-error"
    _add_worktree(leader, worktree, "feat/rollback-runtime-error")
    real_link = os.link
    from agent_flow.core import host_bridge

    rollback_started = False
    real_replace = host_bridge._replace_file_no_clobber

    def fail_manifest(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        nonlocal rollback_started
        if "worktree-host-bridges" in str(destination):
            rollback_started = True
            raise OSError("injected manifest failure")
        real_link(source, destination)

    def fail_runtime_rollback(action: dict[str, object]) -> None:
        if rollback_started:
            raise RuntimeError("injected rollback runtime error")
        real_replace(action)

    with mock.patch(
        "agent_flow.core.host_bridge.os.link", side_effect=fail_manifest
    ), mock.patch(
        "agent_flow.core.host_bridge._replace_file_no_clobber",
        side_effect=fail_runtime_rollback,
    ):
        with pytest.raises(
            RuntimeError,
            match=(
                "injected manifest failure; rollback failed: .*"
                "injected rollback runtime error"
            ),
        ):
            ensure_worktree_host_bridge(
                leader_root=leader, worktree_root=worktree
            )


def test_python_run_bridges_installed_leader_without_worktree_install(tmp_path: Path) -> None:
    leader = _setup_repository(
        tmp_path,
        tracked_context=False,
        tracked_host_settings=False,
        seed_installed=False,
    )
    kit_root = Path(__file__).resolve().parents[1]
    install = subprocess.run(
        ("node", str(kit_root / "bin/agent-flow-kit.mjs"), "install"),
        cwd=leader,
        env={**os.environ, "AGENT_FLOW_SKIP_CODEX_TRUST": "1"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert install.returncode == 0, install.stderr
    index_path = leader / ".agent-flow/skills/index.json"
    index_before = index_path.read_bytes()
    index_mtime_before = index_path.stat().st_mtime_ns
    runtime = leader / ".agent-flow/runtime/python"
    assert (runtime / "agent_flow/core/codex_trust.py").is_file()
    external = tmp_path / "external"
    _git(leader, "worktree", "add", "--detach", str(external), "main")

    run = subprocess.run(
        (
            "python3",
            "-m",
            "agent_flow.cli",
            "run",
            "external bridge",
            "--workflow",
            "default",
        ),
        cwd=external,
        env={
            **os.environ,
            "PYTHONPATH": str(runtime),
            "AGENT_FLOW_ADAPTER": "generic",
            "AGENT_FLOW_GENERIC_MODE": "stub-success",
            "AGENT_FLOW_SKIP_CODEX_TRUST": "1",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr or run.stdout

    _assert_bridge(leader, external, None)
    assert sorted(path.name for path in (external / ".agent-flow").iterdir()) == [
        "bin",
        "skills",
    ]
    assert index_path.read_bytes() == index_before
    assert index_path.stat().st_mtime_ns == index_mtime_before
