"""Global managed hook registration and runtime integrity tests."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.hook_integrity import (
    MANAGED_JSON_EVENT_PLACEMENT,
    MANAGED_HOOK_EVENTS,
    SHARED_RUNTIME_CLI_ENTRYPOINT,
    HookIntegrityError,
    assert_managed_hooks_registered,
    find_install_root,
    verify_managed_hooks,
)

CLAUDE_SETTINGS = Path(".claude") / "settings.json"
CODEX_SETTINGS = Path(".Codex") / "hooks.json"
OMP_EXTENSION = Path(".omp") / "agent" / "extensions" / "agent-flow-hooks.ts"
LEGACY_HOOK_DIR = Path(".agent-flow") / "scripts" / "hooks"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENT_FLOW_HOME", str(home / ".agent-flow"))
    monkeypatch.setenv(
        "AGENT_FLOW_OMP_EXTENSION",
        str(home / OMP_EXTENSION),
    )


def _shared_home() -> Path:
    configured = os.environ.get("AGENT_FLOW_HOME")
    home = Path(configured) if configured else Path.home() / ".agent-flow"
    return home.resolve()


def _global_registration(relative: Path) -> Path:
    return Path.home() / relative


def _omp_extension() -> Path:
    configured = os.environ.get("AGENT_FLOW_OMP_EXTENSION")
    extension = Path(configured) if configured else Path.home() / OMP_EXTENSION
    return extension.resolve()


def _trusted_system_python() -> Path:
    trusted_uids = {0, os.getuid()}
    for candidate in (
        Path(sys.executable),
        Path("/usr/bin/python3"),
        Path("/usr/local/bin/python3"),
    ):
        try:
            resolved = candidate.resolve(strict=True)
            identity = resolved.stat()
            ancestors = tuple(parent.stat() for parent in resolved.parents)
        except OSError:
            continue
        if (
            resolved.is_absolute()
            and resolved.is_file()
            and identity.st_uid in trusted_uids
            and stat.S_IMODE(identity.st_mode) & 0o111
            and not stat.S_IMODE(identity.st_mode) & 0o022
            and all(
                stat.S_ISDIR(ancestor.st_mode)
                and ancestor.st_uid in trusted_uids
                and not stat.S_IMODE(ancestor.st_mode) & 0o022
                for ancestor in ancestors
            )
        ):
            return resolved
    raise RuntimeError("tests require a trusted stable Python")


def _hook_command(_root: Path, event: str) -> str:
    def quote(value: object) -> str:
        return "'" + str(value).replace("'", "'\\''") + "'"

    python = _trusted_system_python()
    launcher = _shared_home() / "bin" / "agent-flow-hook"
    return f"{quote(python)} -I {quote(launcher)} --event {quote(event)}"


def _host_settings(root: Path) -> dict:
    hooks: dict[str, list[dict]] = {}
    for marker in MANAGED_HOOK_EVENTS:
        event = marker[1:]
        registered_event, matcher = MANAGED_JSON_EVENT_PLACEMENT[event]
        block: dict = {
            "hooks": [{"type": "command", "command": _hook_command(root, event)}]
        }
        if matcher:
            block["matcher"] = matcher
        hooks.setdefault(registered_event, []).append(block)
    return {"hooks": hooks}


def _install(root: Path, *, hooks: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".agent-flow").mkdir(exist_ok=True)

    runtime_content = b"#!/bin/sh\nexit 0\n"
    runtime_files = [
        {
            "path": "agent-flow-hook.py",
            "sha256": hashlib.sha256(runtime_content).hexdigest(),
            "mode": 0o444,
        },
        {
            "path": SHARED_RUNTIME_CLI_ENTRYPOINT,
            "sha256": hashlib.sha256(runtime_content).hexdigest(),
            "mode": 0o444,
        },
    ]
    policy_sequence = {
        marker[1:]: {
            "matcher": MANAGED_JSON_EVENT_PLACEMENT[marker[1:]][1],
            "command": [],
        }
        for marker in MANAGED_HOOK_EVENTS
    }
    canonical = {
        "protocol_version": 1,
        "entrypoint": "agent-flow-hook.py",
        "cli_entrypoint": SHARED_RUNTIME_CLI_ENTRYPOINT,
        "policy_sequence": policy_sequence,
        "files": runtime_files,
    }
    runtime_digest = hashlib.sha256(
        json.dumps(canonical, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    runtime_dir = _shared_home() / "runtimes" / runtime_digest
    shared_runtime = runtime_dir / "agent-flow-hook.py"
    runtime_manifest = {
        **canonical,
        "runtime_digest": runtime_digest,
    }
    manifest_path = runtime_dir / "runtime-manifest.json"
    if not runtime_dir.exists():
        runtime_dir.mkdir(parents=True)
        shared_runtime.write_bytes(runtime_content)
        shared_runtime.chmod(0o444)
        shared_cli = runtime_dir / SHARED_RUNTIME_CLI_ENTRYPOINT
        shared_cli.parent.mkdir(parents=True, exist_ok=True)
        shared_cli.write_bytes(runtime_content)
        shared_cli.chmod(0o444)
        manifest_path.write_text(
            json.dumps(runtime_manifest, indent=2),
            encoding="utf-8",
        )
        manifest_path.chmod(0o444)
    else:
        assert shared_runtime.read_bytes() == runtime_content
        assert (runtime_dir / SHARED_RUNTIME_CLI_ENTRYPOINT).read_bytes() == runtime_content
        assert json.loads(manifest_path.read_text(encoding="utf-8")) == runtime_manifest
    runtime_dir.chmod(0o555)

    shared_launcher = _shared_home() / "bin" / "agent-flow-hook"
    shared_launcher.parent.mkdir(parents=True, exist_ok=True)
    shared_launcher.write_bytes(runtime_content)
    shared_launcher.chmod(0o755)
    shared_digest = hashlib.sha256(shared_content := runtime_content).hexdigest()
    omp = _omp_extension()
    omp.parent.mkdir(parents=True, exist_ok=True)
    omp.write_text(
        "// agent-flow: managed omp extension\nrunHook(event, payload);\n",
        encoding="utf-8",
    )
    omp.chmod(0o644)
    state_path = _shared_home() / "hook-runtime.json"
    state_path.write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "active_runtime_digest": runtime_digest,
                "launcher_digest": shared_digest,
                "launcher_digests": [shared_digest],
                "python": str(_trusted_system_python().resolve()),
                "omp_adapter": {
                    "path": str(omp),
                    "digest": hashlib.sha256(omp.read_bytes()).hexdigest(),
                },
            }
        ),
        encoding="utf-8",
    )
    state_path.chmod(0o600)
    (root / ".agent-flow" / "kit.json").write_text(
        json.dumps(
            {
                "profile": "generic",
                "hooks": hooks,
                "hook_runtime": {
                    "protocol_version": 1,
                    "digest": runtime_digest,
                    "path": str(shared_runtime),
                    "manifest_path": str(manifest_path),
                    "launcher_path": str(shared_launcher),
                    "python": str(_trusted_system_python().resolve()),
                },
            }
        ),
        encoding="utf-8",
    )
    kit_path = root / ".agent-flow" / "kit.json"
    registry_path = _shared_home() / "managed-projects.json"
    registry_path.write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "projects": {
                    str(root.resolve()): {
                        "root": str(root.resolve()),
                        "kit_digest": hashlib.sha256(kit_path.read_bytes()).hexdigest(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    registry_path.chmod(0o600)
    settings = _host_settings(root)
    for relative in (CLAUDE_SETTINGS, CODEX_SETTINGS, Path(".codex") / "hooks.json"):
        target = _global_registration(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return root


def _violations(root: Path) -> tuple[str, ...]:
    reports = verify_managed_hooks(root)
    assert len(reports) == 1
    return reports[0].violations


def _rewrite_claude(root: Path, mutate) -> None:
    path = _global_registration(CLAUDE_SETTINGS)
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _rewrite_recorded_python(root: Path, interpreter: Path) -> None:
    kit_path = root / ".agent-flow" / "kit.json"
    kit = json.loads(kit_path.read_text(encoding="utf-8"))
    kit["hook_runtime"]["python"] = str(interpreter)
    kit_path.write_text(json.dumps(kit), encoding="utf-8")
    registry_path = _shared_home() / "managed-projects.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["projects"][str(root.resolve())]["kit_digest"] = hashlib.sha256(
        kit_path.read_bytes()
    ).hexdigest()
    registry_path.write_text(json.dumps(registry), encoding="utf-8")


def test_clean_install_has_no_violations(tmp_path):
    _install(tmp_path)
    assert _violations(tmp_path) == ()

def test_integrity_uses_shared_home_selected_by_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_home = tmp_path / "custom-shared-home"
    monkeypatch.setenv("AGENT_FLOW_HOME", str(shared_home))
    _install(tmp_path)
    monkeypatch.delenv("AGENT_FLOW_HOME")
    monkeypatch.setenv("AGENT_FLOW_SHARED_HOME", str(shared_home))

    assert _violations(tmp_path) == ()



def test_historical_project_python_need_not_match_current_owned_state(tmp_path):
    _install(tmp_path)
    historical_python = Path("/bin/sh").resolve(strict=True)
    assert historical_python != _trusted_system_python()
    _rewrite_recorded_python(tmp_path, historical_python)

    assert _violations(tmp_path) == ()


def test_historical_project_python_must_remain_trusted(tmp_path):
    _install(tmp_path)
    historical_python = _shared_home() / "historical-python"
    historical_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    historical_python.chmod(0o722)
    _rewrite_recorded_python(tmp_path, historical_python)

    assert any(
        "historical shared hook Python interpreter is group or world writable" in value
        for value in _violations(tmp_path)
    )


def test_project_cannot_select_an_alternate_shared_hook_home(tmp_path):
    _install(tmp_path)
    kit_path = tmp_path / ".agent-flow" / "kit.json"
    payload = json.loads(kit_path.read_text(encoding="utf-8"))
    alternate = tmp_path / "alternate-global"
    digest = payload["hook_runtime"]["digest"]
    payload["hook_runtime"]["launcher_path"] = str(
        alternate / "bin" / "agent-flow-hook"
    )
    payload["hook_runtime"]["path"] = str(
        alternate / "runtimes" / digest / "agent-flow-hook.py"
    )
    kit_path.write_text(json.dumps(payload), encoding="utf-8")

    assert any(
        "outside the configured global store" in value
        for value in _violations(tmp_path)
    )


def test_selected_shared_runtime_tampering_is_detected(tmp_path):
    _install(tmp_path)
    kit = json.loads(
        (tmp_path / ".agent-flow" / "kit.json").read_text(encoding="utf-8")
    )
    runtime = Path(kit["hook_runtime"]["path"])
    runtime.chmod(0o644)
    runtime.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    runtime.chmod(0o444)

    assert any(
        "selected shared hook runtime file digest does not match" in value
        for value in _violations(tmp_path)
    )


def test_unrecorded_shared_runtime_file_is_detected(tmp_path):
    _install(tmp_path)
    kit = json.loads(
        (tmp_path / ".agent-flow" / "kit.json").read_text(encoding="utf-8")
    )
    runtime_dir = Path(kit["hook_runtime"]["path"]).parent
    runtime_dir.chmod(0o755)
    extra = runtime_dir / "unrecorded.py"
    extra.write_text("raise RuntimeError('must not load')\n", encoding="utf-8")
    extra.chmod(0o444)
    runtime_dir.chmod(0o555)

    assert any("contains unrecorded files" in value for value in _violations(tmp_path))


def test_shared_launcher_tampering_is_detected(tmp_path):
    _install(tmp_path)
    launcher = _shared_home() / "bin" / "agent-flow-hook"
    launcher.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

    assert any(
        "shared hook launcher content digest" in value
        for value in _violations(tmp_path)
    )


def test_global_omp_adapter_tampering_is_detected_even_when_names_remain(
    tmp_path: Path,
) -> None:
    _install(tmp_path)
    adapter = _omp_extension()
    adapter.write_text(
        adapter.read_text(encoding="utf-8") + "\nexport default () => {};\n",
        encoding="utf-8",
    )

    assert any(
        "global OMP adapter content digest" in value for value in _violations(tmp_path)
    )


def test_old_runtime_state_without_omp_adapter_digest_is_detected(
    tmp_path: Path,
) -> None:
    _install(tmp_path)
    state_path = _shared_home() / "hook-runtime.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    del state["omp_adapter"]
    state_path.write_text(json.dumps(state), encoding="utf-8")

    assert any(
        "does not record the global OMP adapter" in value
        for value in _violations(tmp_path)
    )


def test_global_omp_adapter_unsafe_mode_is_detected(tmp_path: Path) -> None:
    _install(tmp_path)
    _omp_extension().chmod(0o666)

    assert any(
        "global OMP adapter is missing, unsafe, or unreadable" in value
        for value in _violations(tmp_path)
    )


def test_missing_shared_runtime_record_requires_reinstall(tmp_path):
    _install(tmp_path)
    kit_path = tmp_path / ".agent-flow" / "kit.json"
    kit = json.loads(kit_path.read_text(encoding="utf-8"))
    del kit["hook_runtime"]
    kit_path.write_text(json.dumps(kit), encoding="utf-8")

    assert any(
        "does not select a shared hook runtime" in value
        for value in _violations(tmp_path)
    )


def test_install_without_hooks_record_is_not_verified(tmp_path):
    """`hooks` 키가 없는 설치본은 대조할 기록이 없다. 없으면 위반이 아니다."""
    _install(tmp_path)
    (tmp_path / ".agent-flow" / "kit.json").write_text(
        json.dumps({"profile": "generic"}), encoding="utf-8"
    )
    _global_registration(CLAUDE_SETTINGS).unlink()
    report = verify_managed_hooks(tmp_path)[0]
    assert report.recorded is False
    assert report.violations == ()


def test_deleting_one_event_registration_is_detected(tmp_path):
    _install(tmp_path)

    def drop_post_tool_use(payload):
        payload["hooks"]["PostToolUse"] = []

    _rewrite_claude(tmp_path, drop_post_tool_use)
    assert any(
        ".claude/settings.json does not delegate PostToolUse" in violation
        for violation in _violations(tmp_path)
    )


def test_deleting_the_whole_registration_file_is_detected(tmp_path):
    _install(tmp_path)
    _global_registration(CLAUDE_SETTINGS).unlink()
    assert any(
        str(CLAUDE_SETTINGS) in violation and "is missing" in violation
        for violation in _violations(tmp_path)
    )


def test_corrupt_registration_file_is_detected(tmp_path):
    _install(tmp_path)
    _global_registration(CLAUDE_SETTINGS).write_text("{ not json", encoding="utf-8")
    assert any(
        "cannot read the hook registration file" in violation
        for violation in _violations(tmp_path)
    )


def test_project_local_managed_hook_code_is_detected(tmp_path):
    _install(tmp_path)
    legacy = tmp_path / LEGACY_HOOK_DIR / "guard-protected-branch.sh"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    assert any(
        "project-local managed hook code must be removed" in violation
        and "guard-protected-branch.sh" in violation
        for violation in _violations(tmp_path)
    )


def test_unapproved_dispatcher_event_is_detected(tmp_path):
    _install(tmp_path)

    def add_unknown(payload):
        payload["hooks"]["PreToolUse"].append(
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": _hook_command(tmp_path, "Unknown"),
                    }
                ],
            }
        )

    _rewrite_claude(tmp_path, add_unknown)
    assert any(
        "delegates an unapproved dispatcher event: Unknown" in violation
        for violation in _violations(tmp_path)
    )


def test_user_owned_hook_is_not_a_violation(tmp_path):
    _install(tmp_path)
    custom = tmp_path / ".claude" / "hooks" / "mine.sh"
    custom.parent.mkdir(parents=True)
    custom.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    def add_user_hook(payload):
        payload["hooks"]["PostToolUse"].append(
            {"matcher": "Bash", "hooks": [{"type": "command", "command": str(custom)}]}
        )

    _rewrite_claude(tmp_path, add_user_hook)
    assert _violations(tmp_path) == ()


def test_hooks_disabled_ignores_global_registration(tmp_path):
    _install(tmp_path, hooks=False)
    assert _violations(tmp_path) == ()


def test_hooks_disabled_does_not_require_global_registration(tmp_path):
    _install(tmp_path, hooks=False)
    _global_registration(CLAUDE_SETTINGS).unlink()
    assert _violations(tmp_path) == ()


def test_event_dispatcher_moved_off_its_event_is_detected(tmp_path):
    _install(tmp_path)

    def move(payload):
        moved = payload["hooks"]["PreToolUse"].pop()
        payload["hooks"]["PostToolUse"].append(moved)

    _rewrite_claude(tmp_path, move)
    assert any(
        "registers PreToolUse dispatcher at PostToolUse" in violation
        for violation in _violations(tmp_path)
    )


def test_event_dispatcher_with_a_dead_matcher_is_detected(tmp_path):
    _install(tmp_path)

    def neuter(payload):
        payload["hooks"]["PreToolUse"][0]["matcher"] = "^$"

    _rewrite_claude(tmp_path, neuter)
    assert any(
        "expected PreToolUse/" in violation for violation in _violations(tmp_path)
    )


def test_trailing_shell_syntax_on_dispatcher_command_is_detected(tmp_path):
    _install(tmp_path)

    def swallow(payload):
        hook = payload["hooks"]["PreToolUse"][0]["hooks"][0]
        hook["command"] = f"{hook['command']} || true"

    _rewrite_claude(tmp_path, swallow)
    violations = _violations(tmp_path)
    assert any("extra shell syntax" in violation for violation in violations)
    assert any("does not delegate PreToolUse" in violation for violation in violations)


def test_shell_alias_for_dispatcher_is_not_exact_registration(tmp_path):
    _install(tmp_path)

    def alias(payload):
        hook = payload["hooks"]["PreToolUse"][0]["hooks"][0]
        hook["command"] = f"exec {hook['command']}"

    _rewrite_claude(tmp_path, alias)
    assert any(
        "does not delegate PreToolUse" in violation
        for violation in _violations(tmp_path)
    )


def test_untrusted_shared_launcher_command_is_detected(tmp_path):
    _install(tmp_path)

    def replace_launcher(payload):
        expected = str(_shared_home() / "bin" / "agent-flow-hook")
        for block in payload["hooks"]["PostToolUse"]:
            for hook in block["hooks"]:
                hook["command"] = hook["command"].replace(
                    expected,
                    "/tmp/untrusted-agent-flow-hook",
                )

    _rewrite_claude(tmp_path, replace_launcher)
    assert any(
        "does not delegate PostToolUse" in violation
        for violation in _violations(tmp_path)
    )



def test_missing_cli_entrypoint_in_shared_runtime_is_detected(tmp_path):
    _install(tmp_path)
    kit = json.loads(
        (tmp_path / ".agent-flow" / "kit.json").read_text(encoding="utf-8")
    )
    cli = Path(kit["hook_runtime"]["path"]).parent / SHARED_RUNTIME_CLI_ENTRYPOINT
    cli.parent.chmod(0o755)
    cli.unlink()
    cli.parent.chmod(0o555)
    assert any(
        "shared hook runtime file is unsafe or unreadable" in value
        for value in _violations(tmp_path)
    )


def test_assert_raises_and_changes_nothing(tmp_path):
    _install(tmp_path)
    _global_registration(CLAUDE_SETTINGS).unlink()
    runtime_dir = Path(
        json.loads((tmp_path / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))[
            "hook_runtime"
        ]["path"]
    ).parent
    before = sorted(path.name for path in runtime_dir.iterdir())
    with pytest.raises(HookIntegrityError) as caught:
        assert_managed_hooks_registered(tmp_path)
    assert "kit.json" in str(caught.value)
    # 자동 복구는 곧 승인 세탁이다. 아무것도 되돌리지 않는다.
    assert not _global_registration(CLAUDE_SETTINGS).exists()
    assert sorted(path.name for path in runtime_dir.iterdir()) == before




def test_assert_passes_for_a_clean_install(tmp_path):
    _install(tmp_path)
    reports = assert_managed_hooks_registered(tmp_path)
    assert [report.ok for report in reports] == [True]


def test_run_gate_rejects_project_without_kit_json(tmp_path):
    assert verify_managed_hooks(tmp_path) == ()
    with pytest.raises(HookIntegrityError, match="installation could not be found"):
        assert_managed_hooks_registered(tmp_path)


def test_run_gate_rejects_hooks_disabled_install(tmp_path):
    _install(tmp_path, hooks=False)
    assert verify_managed_hooks(tmp_path)[0].violations == ()
    with pytest.raises(HookIntegrityError, match="not enabled"):
        assert_managed_hooks_registered(tmp_path)


def test_install_root_resolves_from_a_nested_worktree(tmp_path):
    _install(tmp_path)
    nested = tmp_path / ".agent-flow" / "worktrees" / "feat-x" / "src"
    nested.mkdir(parents=True)
    assert find_install_root(nested) == tmp_path


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True, text=True)


def _repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "f.txt").write_text("base\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "init", cwd=root)


def test_install_root_resolves_the_leader_from_an_external_linked_worktree(tmp_path):
    """불변: 관리 루트 밖 linked worktree도 자기를 지탱하는 leader 설치본을 찾는다.

    조상 탐색만 하면 `<tmp>/outside/feat-x`에는 조상 중 설치본이 없어 `None`이 되고,
    무결성 게이트는 "설치본을 못 찾음"으로 런을 거부한다.
    """
    leader = tmp_path / "leader"
    _repo(leader)
    _install(leader)
    external = tmp_path / "outside" / "feat-x"
    _git("worktree", "add", "-b", "feat/x", str(external), cwd=leader)

    assert find_install_root(external) == leader.resolve()


def test_worktree_local_kit_json_does_not_shadow_the_leader_install(tmp_path):
    """반증: checkout 안의 `kit.json`이 이겨 버리면 워커가 자기 무결성 기준선을 쓴다."""
    leader = tmp_path / "leader"
    _repo(leader)
    _install(leader)
    external = tmp_path / "outside" / "feat-x"
    _git("worktree", "add", "-b", "feat/x", str(external), cwd=leader)
    planted = external / ".agent-flow" / "kit.json"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(
        json.dumps({"profile": "generic", "hooks": False}), encoding="utf-8"
    )

    assert find_install_root(external) == leader.resolve()


def test_duplicate_roots_are_reported_once(tmp_path):
    _install(tmp_path)
    nested = tmp_path / ".agent-flow" / "worktrees" / "feat-x"
    nested.mkdir(parents=True)
    assert len(verify_managed_hooks(nested, tmp_path)) == 1


def _generic_runner(monkeypatch, project: Path):
    for key in tuple(os.environ):
        if key.startswith("AGENT_FLOW_") or key in {
            "CLAUDECODE",
            "CLAUDE_CLI",
            "CODEX_CLI",
        }:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_FLOW_ADAPTER", "generic")
    monkeypatch.setenv("AGENT_FLOW_GENERIC_MODE", "stub-success")
    from agent_flow.runner import Runner

    return Runner(project_root=project)


def test_runner_refuses_to_start_before_creating_a_run(tmp_path, monkeypatch):
    """게이트가 phase 루프 앞에 있다는 관측 가능한 증거. run 디렉터리가 없어야 한다."""
    from agent_flow.runner import ResumeMode

    project = tmp_path / "proj"
    project.mkdir()
    _install(project)
    runner = _generic_runner(monkeypatch, project)
    _global_registration(CLAUDE_SETTINGS).unlink()
    with pytest.raises(HookIntegrityError):
        runner.run(mode=ResumeMode.START, task="x")
    assert not (project / ".agent-flow" / "runs").exists()


def test_runner_gate_precedes_every_leader_snapshot(tmp_path, monkeypatch):
    """#100 P0의 순서 제약. 뒤에서 돌면 오염된 상태가 tripwire 기준선이 된다."""
    from agent_flow import runner as runner_module
    from agent_flow.core.worktree_isolation import LeaderSnapshot

    project = tmp_path / "proj"
    project.mkdir()
    _install(project)
    runner = _generic_runner(monkeypatch, project)

    order: list[str] = []
    real_gate = runner_module.assert_managed_hooks_registered

    def recording_gate(*roots):
        order.append("hooks")
        return real_gate(*roots)

    monkeypatch.setattr(
        runner_module, "assert_managed_hooks_registered", recording_gate
    )
    monkeypatch.setattr(runner_module, "leader_root_for", lambda root: project)
    monkeypatch.setattr(
        runner_module,
        "capture_leader_snapshot",
        lambda root: (
            order.append("snapshot") or LeaderSnapshot(head="h", branch="b", status="")
        ),
    )
    monkeypatch.setattr(runner_module, "assert_leader_unchanged", lambda *a, **k: None)

    runner.run(mode=runner_module.ResumeMode.START, task="x")
    assert "snapshot" in order, (
        "the snapshot path never ran; the ordering claim is untested"
    )
    assert order[0] == "hooks"
