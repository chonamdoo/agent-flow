from __future__ import annotations

import hashlib
import io
import json
import os
import runpy
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_flow.core.hook_integrity import verify_managed_hooks


KIT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def trusted_python_root() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(
        prefix=".agent-flow-trusted-python-",
        dir=KIT_ROOT.parent,
    ) as directory:
        root = Path(directory)
        root.chmod(0o700)
        yield root


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    return node


def _install(
    project: Path,
    home: Path,
    *args: str,
    agent_flow_home: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    shared_home = agent_flow_home or home / ".agent-flow"
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "AGENT_FLOW_HOME": str(shared_home),
            "PYTHON": sys.executable,
            "AGENT_FLOW_SKIP_CODEX_TRUST": "1",
        }
    )
    env.update(extra_env or {})
    return subprocess.run(
        (
            _node(),
            str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"),
            "install",
            *args,
        ),
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )


def _installed_runtime(project: Path) -> tuple[dict, Path]:
    kit = json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
    runtime_record = kit.get("hook_runtime")
    assert isinstance(runtime_record, dict), kit
    runtime = Path(runtime_record["path"])
    return kit, runtime


def _unsafe_legacy_python(root: Path) -> tuple[Path, Path]:
    parent = root / "unsafe-python-parent"
    parent.mkdir(mode=0o700)
    executable = parent / "python"
    marker = root / "unsafe-python-executed"
    executable.write_text(
        f"#!/bin/sh\nprintf ran > {shlex.quote(str(marker))}\nexit 99\n",
        encoding="utf-8",
    )
    executable.chmod(0o555)
    parent.chmod(0o775)
    return executable, marker


def _dispatch(
    project: Path,
    home: Path,
    hook: str,
    payload: dict,
    *,
    requested_root: Path | None = None,
    agent_flow_home: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    kit, _ = _installed_runtime(project)
    runtime = kit["hook_runtime"]
    shared_home = agent_flow_home or home / ".agent-flow"
    env = {
        **os.environ,
        "HOME": str(home),
        "AGENT_FLOW_HOME": str(shared_home),
        **(extra_env or {}),
    }
    event = {
        "guard-protected-branch.sh": "PreToolUse",
        "record-skill-read.py": "PostToolUse",
        "show-phase-status.sh": "Stop",
    }[hook]
    command = [runtime["python"], "-I", runtime["launcher_path"]]
    if requested_root is not None:
        command.extend(("--root", str(requested_root)))
    command.extend(("--event", event))
    return subprocess.run(
        tuple(command),
        cwd=home,
        env=env,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
def _write_runtime_binding(shared_home: Path, run_path: Path, digest: str) -> None:
    canonical = str(run_path.resolve())
    key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    binding_root = shared_home / "run-bindings"
    binding_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    binding_root.chmod(0o700)
    target = binding_root / f"{key}.json"
    target.write_text(
        f"{json.dumps({'protocol_version': 1, 'run_path': canonical, 'runtime_digest': digest}, sort_keys=True)}\n",
        encoding="utf-8",
    )
    target.chmod(0o600)




@pytest.mark.parametrize(
    "relative_run",
    (
        Path(".agent-flow/runs/current-run"),
        Path(
            ".git/agent-flow/worktrees/feature/"
            ".agent-flow/runs/current-run"
        ),
    ),
    ids=("project", "git-private"),
)
def test_stop_dispatcher_reports_current_active_run_layout(
    tmp_path: Path,
    relative_run: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    result = _install(project, home)
    assert result.returncode == 0, result.stderr
    kit, _ = _installed_runtime(project)

    current_run = project / relative_run
    current_run.mkdir(parents=True)
    (current_run / "meta.json").write_text(
        json.dumps(
            {
                "run_id": "current-run",
                "workflow": "full-feature",
                "current_phase": "implement",
                "task": "fix stop scanner",
            }
        ),
        encoding="utf-8",
    )
    (current_run / "active").write_text("", encoding="utf-8")
    _write_runtime_binding(
        home / ".agent-flow",
        current_run,
        kit["hook_runtime"]["digest"],
    )

    legacy_run = project / ".agent-flow" / "runs" / "legacy" / "legacy-run"
    legacy_run.mkdir(parents=True)
    (legacy_run / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": "legacy-run",
                "workflow_id": "legacy",
                "status": "complete",
                "current_phase": "done",
                "task": "old task",
            }
        ),
        encoding="utf-8",
    )

    dispatch = _dispatch(
        project,
        home,
        "show-phase-status.sh",
        {"cwd": str(project), "hook_event_name": "Stop"},
        requested_root=project,
    )

    assert dispatch.returncode == 0, dispatch.stderr
    assert json.loads(dispatch.stdout) == {
        "systemMessage": (
            "[agent-flow] Run id     : current-run\n"
            "Workflow   : full-feature\n"
            "Status     : running\n"
            "Phase      : implement\n"
            "Task       : fix stop scanner"
        )
    }


def test_stop_dispatcher_keeps_no_runs_message_for_project_without_runs(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    result = _install(project, home)
    assert result.returncode == 0, result.stderr

    dispatch = _dispatch(
        project,
        home,
        "show-phase-status.sh",
        {"cwd": str(project), "hook_event_name": "Stop"},
        requested_root=project,
    )

    assert dispatch.returncode == 0, dispatch.stderr
    assert json.loads(dispatch.stdout) == {
        "systemMessage": "[agent-flow] no runs"
    }


def test_shared_hook_runtime_is_digest_addressed_and_immutable(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()

    result = _install(project, home)

    assert result.returncode == 0, result.stderr
    kit, runtime = _installed_runtime(project)
    record = kit["hook_runtime"]
    manifest = json.loads(
        (runtime.parent / "runtime-manifest.json").read_text(encoding="utf-8")
    )
    digest = record["digest"]
    entrypoint = next(
        item for item in manifest["files"] if item["path"] == "agent-flow-hook.py"
    )
    assert record["protocol_version"] == 1
    assert manifest["runtime_digest"] == digest
    assert entrypoint["sha256"] == hashlib.sha256(runtime.read_bytes()).hexdigest()
    assert runtime == home / ".agent-flow" / "runtimes" / digest / "agent-flow-hook.py"
    assert record["launcher_path"] == str(
        home / ".agent-flow" / "bin" / "agent-flow-hook"
    )
    assert Path(record["python"]).is_absolute()
    assert stat.S_IMODE(runtime.stat().st_mode) & 0o222 == 0
    assert stat.S_IMODE(runtime.parent.stat().st_mode) & 0o022 == 0
    state = json.loads(
        (home / ".agent-flow" / "hook-runtime.json").read_text(encoding="utf-8")
    )
    assert (
        stat.S_IMODE((home / ".agent-flow" / "hook-runtime.json").stat().st_mode)
        == 0o600
    )
    assert (
        hashlib.sha256(Path(record["launcher_path"]).read_bytes()).hexdigest()
        == state["launcher_digest"]
    )
    adapter = home / ".omp" / "agent" / "extensions" / "agent-flow-hooks.ts"
    assert state["omp_adapter"] == {
        "path": str(adapter),
        "digest": hashlib.sha256(adapter.read_bytes()).hexdigest(),
    }
    assert stat.S_IMODE(adapter.stat().st_mode) == 0o644
    registry_path = home / ".agent-flow" / "managed-projects.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    canonical_project = str(project.resolve())
    assert registry["protocol_version"] == 1
    assert registry["projects"][canonical_project] == {
        "root": canonical_project,
        "kit_digest": hashlib.sha256(
            (project / ".agent-flow" / "kit.json").read_bytes()
        ).hexdigest(),
    }
    assert stat.S_IMODE(registry_path.stat().st_mode) == 0o600


@pytest.mark.parametrize("relative", ["runtimes", "bin"])
def test_shared_runtime_refuses_symlinked_publish_directories(
    tmp_path: Path,
    relative: str,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    shared_home = home / ".agent-flow"
    outside = tmp_path / f"outside-{relative}"
    project.mkdir()
    shared_home.mkdir(parents=True)
    outside.mkdir()
    (shared_home / relative).symlink_to(outside, target_is_directory=True)

    result = _install(project, home)

    assert result.returncode != 0
    assert "not a regular owned directory" in result.stderr
    assert not any(outside.iterdir())


def test_selected_hook_runtime_digest_is_required(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    result = _install(project, home)
    assert result.returncode == 0, result.stderr
    kit, runtime = _installed_runtime(project)
    selected_digest = kit["hook_runtime"]["digest"]
    missing_runtime = runtime.parent.with_name(f"{selected_digest}.missing")
    runtime.parent.rename(missing_runtime)

    dispatch = _dispatch(
        project,
        home,
        "record-skill-read.py",
        {"cwd": str(project), "tool_name": "Skill"},
    )

    assert dispatch.returncode != 0
    assert selected_digest in dispatch.stderr
    assert "unavailable" in dispatch.stderr.lower()
    assert missing_runtime.is_dir()


def test_shared_launcher_rejects_unrecorded_runtime_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    result = _install(project, home)
    assert result.returncode == 0, result.stderr
    _, runtime = _installed_runtime(project)
    runtime_dir = runtime.parent
    runtime_dir.chmod(0o755)
    extra = runtime_dir / "unrecorded.py"
    extra.write_text("raise RuntimeError('must not load')\n", encoding="utf-8")
    extra.chmod(0o444)
    runtime_dir.chmod(0o555)

    dispatch = _dispatch(
        project,
        home,
        "record-skill-read.py",
        {"cwd": str(project), "tool_name": "Skill"},
    )

    assert dispatch.returncode != 0
    assert "unrecorded files" in dispatch.stderr


def test_shared_hook_runtime_update_keeps_previous_digest(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    alternate_kit = tmp_path / "alternate-kit"
    project.mkdir()
    home.mkdir()
    result = _install(project, home)
    assert result.returncode == 0, result.stderr
    kit, previous_runtime = _installed_runtime(project)
    previous_digest = kit["hook_runtime"]["digest"]
    active_run = project / ".agent-flow" / "runs" / "default" / "run-1"
    active_run.mkdir(parents=True)
    (active_run / "manifest.json").write_text(
        json.dumps(
            {
                "workflow_id": "default",
                "run_id": "run-1",
                "status": "running",
            }
        ),
        encoding="utf-8",
    )
    _write_runtime_binding(home / ".agent-flow", active_run, previous_digest)

    shutil.copytree(KIT_ROOT / "scripts", alternate_kit / "scripts")
    shutil.copytree(
        KIT_ROOT / "src" / "agent_flow", alternate_kit / "src" / "agent_flow"
    )
    shutil.copytree(KIT_ROOT / "templates", alternate_kit / "templates")
    alternate_source = alternate_kit / "scripts" / "hook-runtime" / "agent-flow-hook.py"
    alternate_source.write_bytes(previous_runtime.read_bytes() + b"\n# next runtime\n")
    module = KIT_ROOT / "lib" / "shared-hook-runtime.mjs"
    update = subprocess.run(
        (
            _node(),
            "--input-type=module",
            "-e",
            "import { installSharedHookRuntime } from "
            f"{json.dumps(module.as_uri())};"
            "installSharedHookRuntime({"
            f"kitRoot:{json.dumps(str(alternate_kit))},"
            f"homeDir:{json.dumps(str(home / '.agent-flow'))}"
            "});",
        ),
        env={**os.environ, "PYTHON": sys.executable},
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert update.returncode == 0, update.stderr

    dispatch = _dispatch(
        project,
        home,
        "record-skill-read.py",
        {"cwd": str(project), "tool_name": "Skill", "tool_input": {}},
    )

    assert dispatch.returncode == 0, dispatch.stderr
    assert previous_runtime.is_file()
    state = json.loads(
        (home / ".agent-flow" / "hook-runtime.json").read_text(encoding="utf-8")
    )
    assert (
        hashlib.sha256(
            (home / ".agent-flow" / "bin" / "agent-flow-hook").read_bytes()
        ).hexdigest()
        == state["launcher_digest"]
    )
    assert state["active_runtime_digest"] != previous_digest
    updated_kit = json.loads(json.dumps(kit))
    updated_kit["hook_runtime"]["digest"] = state["active_runtime_digest"]
    select = runpy.run_path(kit["hook_runtime"]["launcher_path"])[
        "_selected_runtime_digest"
    ]
    assert select(project, updated_kit, {"cwd": str(project)}) == previous_digest


def test_selected_runtime_survives_run_state_changes_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    alternate_kit = tmp_path / "alternate-kit"
    project.mkdir()
    home.mkdir()
    installed = _install(project, home)
    assert installed.returncode == 0, installed.stderr
    kit, project_runtime = _installed_runtime(project)
    project_digest = kit["hook_runtime"]["digest"]

    shutil.copytree(KIT_ROOT / "scripts", alternate_kit / "scripts")
    shutil.copytree(
        KIT_ROOT / "src" / "agent_flow",
        alternate_kit / "src" / "agent_flow",
    )
    shutil.copytree(KIT_ROOT / "templates", alternate_kit / "templates")
    alternate_source = (
        alternate_kit / "scripts" / "hook-runtime" / "agent-flow-hook.py"
    )
    alternate_source.write_bytes(project_runtime.read_bytes() + b"\n# alternate\n")
    module = KIT_ROOT / "lib" / "shared-hook-runtime.mjs"
    published = subprocess.run(
        (
            _node(),
            "--input-type=module",
            "-e",
            "import { installSharedHookRuntime } from "
            f"{json.dumps(module.as_uri())};"
            "installSharedHookRuntime({"
            f"kitRoot:{json.dumps(str(alternate_kit))},"
            f"homeDir:{json.dumps(str(home / '.agent-flow'))}"
            "});",
        ),
        env={**os.environ, "PYTHON": sys.executable},
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert published.returncode == 0, published.stderr
    state = json.loads(
        (home / ".agent-flow" / "hook-runtime.json").read_text(encoding="utf-8")
    )
    alternate_digest = state["active_runtime_digest"]
    assert alternate_digest != project_digest
    alternate_runtime = (
        home
        / ".agent-flow"
        / "runtimes"
        / alternate_digest
        / "agent-flow-hook.py"
    )
    launcher = runpy.run_path(kit["hook_runtime"]["launcher_path"])
    select = launcher["_selected_runtime_digest"]
    payload = {"cwd": str(project)}
    payload_bytes = json.dumps(payload).encode("utf-8")
    active_run = project / ".agent-flow" / "runs" / "selection-race"
    active_run.mkdir(parents=True)
    marker = active_run / "active"
    (active_run / "meta.json").write_text("{}\n", encoding="utf-8")

    def execute_selected(runtime: Path, digest: str) -> None:
        namespace = runpy.run_path(str(runtime))
        descriptor = os.open(runtime, os.O_RDONLY)
        try:
            with monkeypatch.context() as patch:
                patch.setenv("AGENT_FLOW_EXECUTED_FD", str(descriptor))
                patch.setenv("AGENT_FLOW_PROJECT_ROOT", str(project.resolve()))
                patch.setenv("AGENT_FLOW_RUNTIME_DIGEST", digest)
                patch.setenv("AGENT_FLOW_RUNTIME_ENTRYPOINT", str(runtime))
                patch.setenv("AGENT_FLOW_RUNTIME_DIR", str(runtime.parent))
                patch.setenv("AGENT_FLOW_SHARED_HOME", str(home / ".agent-flow"))
                patch.setattr(
                    sys,
                    "argv",
                    [
                        str(runtime),
                        "--root",
                        str(project),
                        "--event",
                        "PostToolUse",
                    ],
                )
                patch.setattr(
                    sys,
                    "stdin",
                    io.TextIOWrapper(io.BytesIO(payload_bytes), encoding="utf-8"),
                )
                assert namespace["main"]() == 0
        finally:
            os.close(descriptor)

    assert select(project, kit, payload) == project_digest
    _write_runtime_binding(home / ".agent-flow", active_run, alternate_digest)
    marker.write_text("", encoding="utf-8")
    execute_selected(project_runtime, project_digest)

    assert select(project, kit, payload) == alternate_digest
    marker.unlink()
    execute_selected(alternate_runtime, alternate_digest)


def test_successful_cutover_rejects_previous_launcher_digest(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    first = _install(project, home)
    assert first.returncode == 0, first.stderr
    state_path = home / ".agent-flow" / "hook-runtime.json"
    launcher_path = home / ".agent-flow" / "bin" / "agent-flow-hook"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    previous_launcher = b"raise SystemExit('previous launcher executed')\n"
    previous_digest = hashlib.sha256(previous_launcher).hexdigest()
    assert previous_digest != state["launcher_digest"]
    launcher_path.write_bytes(previous_launcher)
    state["launcher_digest"] = previous_digest
    state["launcher_digests"] = [previous_digest]
    state_path.write_text(f"{json.dumps(state, indent=2)}\n", encoding="utf-8")

    update = _install(project, home)

    assert update.returncode == 0, update.stderr
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert final_state["launcher_digests"] == [final_state["launcher_digest"]]
    assert previous_digest not in final_state["launcher_digests"]
    launcher_path.write_bytes(previous_launcher)
    command = next(
        command
        for command in _registered_commands(home / ".claude" / "settings.json")
        if shlex.split(command)[-1] == "PostToolUse"
    )
    dispatch = _host_dispatch(
        command,
        project,
        home,
        {"cwd": str(project), "tool_name": "Skill", "tool_input": {}},
    )
    assert dispatch.returncode == 70
    assert "launcher digest does not match runtime state" in dispatch.stderr


def _registered_commands(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    commands: list[str] = []
    for entries in payload["hooks"].values():
        for entry in entries:
            commands.extend(hook["command"] for hook in entry["hooks"])
    return commands


def _host_dispatch(
    command: str,
    project: Path,
    home: Path,
    payload: dict,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        tuple(shlex.split(command)),
        cwd=project,
        env={
            **os.environ,
            "HOME": str(home),
            "AGENT_FLOW_HOME": str(home / ".agent-flow"),
        },
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def test_claude_codex_omp_hook_runtime_parity(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()

    result = _install(project, home)

    assert result.returncode == 0, result.stderr
    claude = _registered_commands(home / ".claude" / "settings.json")
    codex = _registered_commands(home / ".Codex" / "hooks.json")
    launcher = str(home / ".agent-flow" / "bin" / "agent-flow-hook")
    python = str(Path(_installed_runtime(project)[0]["hook_runtime"]["python"]))
    assert claude == codex
    assert claude
    assert all(
        shlex.split(command)[:3] == [python, "-I", "-c"]
        and str(home / ".agent-flow") in shlex.split(command)[3]
        and "launcher_digest" in shlex.split(command)[3]
        and shlex.split(command)[4] == "--event"
        and len(shlex.split(command)) == 6
        and "--root" not in shlex.split(command)
        for command in claude
    )
    assert all("/.agent-flow/scripts/hooks/" not in command for command in claude)
    extension = home / ".omp" / "agent" / "extensions" / "agent-flow-hooks.ts"
    assert extension.is_file()
    extension_text = extension.read_text(encoding="utf-8")
    assert "agent-flow: managed omp extension" in extension_text
    assert "agent-flow-hook" in extension_text
    manifest = json.loads(
        (
            Path(_installed_runtime(project)[0]["hook_runtime"]["path"]).parent
            / "runtime-manifest.json"
        ).read_text(encoding="utf-8")
    )
    bundled_scripts = {
        script
        for event in manifest["policy_sequence"].values()
        for tool_class, scripts in event.items()
        if tool_class != "matcher"
        for script in scripts
    }
    assert bundled_scripts == {
        "bind-host-worktree.py",
        "guard-protected-branch.sh",
        "guard-host-worktree.sh",
        "show-phase-status.sh",
        "comment-checker.py",
        "record-skill-read.py",
        "record-command-run.py",
        "worktree-tripwire.py",
    }
    assert not (project / ".omp" / "extensions" / "agent-flow-hooks.ts").exists()


@pytest.mark.parametrize(
    "mutation",
    (
        "primary-format",
        "accepted-format",
        "accepted-member",
        "accepted-duplicate",
        "primary-membership",
    ),
)
def test_host_bootstrap_rejects_malformed_launcher_digest_state(
    tmp_path: Path,
    mutation: str,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    result = _install(project, home)
    assert result.returncode == 0, result.stderr
    state_path = home / ".agent-flow" / "hook-runtime.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if mutation == "primary-format":
        state["launcher_digest"] = "invalid"
    elif mutation == "accepted-format":
        state["launcher_digests"] = state["launcher_digest"]
    elif mutation == "accepted-member":
        state["launcher_digests"] = ["invalid"]
    elif mutation == "accepted-duplicate":
        state["launcher_digests"] = [
            state["launcher_digest"],
            state["launcher_digest"],
        ]
    else:
        state["launcher_digest"] = "0" * 64
    state_path.write_text(f"{json.dumps(state, indent=2)}\n", encoding="utf-8")
    command = next(
        command
        for command in _registered_commands(home / ".claude" / "settings.json")
        if shlex.split(command)[-1] == "PostToolUse"
    )

    dispatch = _host_dispatch(
        command,
        project,
        home,
        {"cwd": str(project), "tool_name": "Skill", "tool_input": {}},
    )

    assert dispatch.returncode == 70
    assert "launcher digest state is invalid" in dispatch.stderr


def test_host_bootstrap_rejects_payload_without_routing_cwd(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    result = _install(project, home)
    assert result.returncode == 0, result.stderr
    command = next(
        command
        for command in _registered_commands(home / ".claude" / "settings.json")
        if shlex.split(command)[-1] == "PostToolUse"
    )

    dispatch = _host_dispatch(
        command,
        project,
        home,
        {"tool_name": "Skill", "tool_input": {}},
    )

    assert dispatch.returncode == 70
    assert "payload does not provide a routing cwd" in dispatch.stderr


def test_shared_hook_runtime_preserves_managed_hook_behavior(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    subprocess.run(("git", "init", "-q", "-b", "main", str(project)), check=True)

    result = _install(project, home)

    assert result.returncode == 0, result.stderr
    kit, _ = _installed_runtime(project)
    runtime = Path(kit["hook_runtime"]["path"])
    manifest = json.loads(
        (runtime.parent / "runtime-manifest.json").read_text(encoding="utf-8")
    )
    recorded = {
        item["path"].removeprefix("hooks/"): item["sha256"]
        for item in manifest["files"]
        if item["path"].startswith("hooks/")
    }
    source_hooks = {
        source.name: source
        for source in (KIT_ROOT / "scripts" / "hooks").iterdir()
        if source.is_file()
    }
    assert set(recorded) == set(source_hooks)
    for script_name, source in source_hooks.items():
        assert hashlib.sha256(source.read_bytes()).hexdigest() == recorded[script_name]
    assert not (project / ".agent-flow" / "scripts" / "hooks").exists()
    dispatch = _dispatch(
        project,
        home,
        "guard-protected-branch.sh",
        {
            "cwd": str(project),
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m blocked"},
        },
    )

    assert dispatch.returncode == 2, dispatch.stderr
    assert "BLOCKED" in dispatch.stderr


def test_multiple_projects_share_one_omp_adapter(tmp_path: Path) -> None:
    home = tmp_path / "home"
    first = tmp_path / "first"
    second = tmp_path / "second"
    home.mkdir()
    first.mkdir()
    second.mkdir()

    first_result = _install(first, home)
    adapter = home / ".omp" / "agent" / "extensions" / "agent-flow-hooks.ts"
    first_bytes = adapter.read_bytes()
    second_result = _install(second, home)

    assert first_result.returncode == second_result.returncode == 0
    assert adapter.read_bytes() == first_bytes
    assert not (first / ".omp" / "extensions" / "agent-flow-hooks.ts").exists()
    assert not (second / ".omp" / "extensions" / "agent-flow-hooks.ts").exists()
    assert (
        _installed_runtime(first)[0]["hook_runtime"]["digest"]
        == _installed_runtime(second)[0]["hook_runtime"]["digest"]
    )


def test_identical_omp_adapter_republishes_missing_runtime_state(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    first = tmp_path / "first"
    second = tmp_path / "second"
    home.mkdir()
    first.mkdir()
    second.mkdir()
    assert _install(first, home).returncode == 0
    state_path = home / ".agent-flow" / "hook-runtime.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.pop("omp_adapter")
    state_path.write_text(json.dumps(state), encoding="utf-8")
    state_path.chmod(0o600)

    result = _install(second, home)

    assert result.returncode == 0, result.stderr
    repaired = json.loads(state_path.read_text(encoding="utf-8"))
    adapter = home / ".omp" / "agent" / "extensions" / "agent-flow-hooks.ts"
    assert repaired["omp_adapter"] == {
        "path": str(adapter),
        "digest": hashlib.sha256(adapter.read_bytes()).hexdigest(),
    }


def test_reinstall_repairs_the_managed_omp_adapter_mode(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    first = _install(project, home)
    assert first.returncode == 0, first.stderr
    adapter = home / ".omp" / "agent" / "extensions" / "agent-flow-hooks.ts"
    adapter.chmod(0o666)

    repaired = _install(project, home)

    assert repaired.returncode == 0, repaired.stderr
    assert stat.S_IMODE(adapter.stat().st_mode) == 0o644


@pytest.mark.skipif(os.name == "nt", reason="hardlink invariant is POSIX-only")
@pytest.mark.parametrize("target_kind", ["runtime", "adapter"])
def test_reinstall_rejects_hardlinked_shared_executables(
    tmp_path: Path,
    target_kind: str,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    assert _install(project, home).returncode == 0
    kit, runtime = _installed_runtime(project)
    targets = {
        "runtime": runtime,
        "launcher": Path(kit["hook_runtime"]["launcher_path"]),
        "adapter": home / ".omp" / "agent" / "extensions" / "agent-flow-hooks.ts",
    }
    os.link(targets[target_kind], tmp_path / f"{target_kind}-alias")

    result = _install(project, home)

    assert result.returncode != 0
    assert "unsafe link count" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="hardlink invariant is POSIX-only")
def test_reinstall_atomically_replaces_a_hardlinked_launcher(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    assert _install(project, home).returncode == 0
    kit, _runtime = _installed_runtime(project)
    launcher = Path(kit["hook_runtime"]["launcher_path"])
    alias = tmp_path / "launcher-alias"
    os.link(launcher, alias)

    result = _install(project, home)

    assert result.returncode == 0, result.stderr
    assert launcher.stat().st_nlink == 1
    assert alias.stat().st_nlink == 1


def test_concurrent_project_installs_preserve_every_registry_record(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    projects = [tmp_path / f"project-{index}" for index in range(6)]
    for project in projects:
        project.mkdir()
    environment = {
        **os.environ,
        "HOME": str(home),
        "AGENT_FLOW_HOME": str(home / ".agent-flow"),
        "AGENT_FLOW_SKIP_CODEX_TRUST": "1",
        "PYTHON": sys.executable,
    }
    processes = [
        subprocess.Popen(
            (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
            cwd=project,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for project in projects
    ]

    completed = [process.communicate(timeout=120) for process in processes]

    assert all(process.returncode == 0 for process in processes), completed
    registry = json.loads(
        (home / ".agent-flow" / "managed-projects.json").read_text(encoding="utf-8")
    )
    assert set(registry["projects"]) == {str(project.resolve()) for project in projects}


def test_shared_launcher_resolves_a_managed_worktree_to_its_leader(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    checkout = tmp_path / "checkout"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    subprocess.run(("git", "init", "-q", "-b", "main"), cwd=project, check=True)
    subprocess.run(
        ("git", "config", "user.email", "t@example.com"), cwd=project, check=True
    )
    subprocess.run(("git", "config", "user.name", "t"), cwd=project, check=True)
    (project / ".gitignore").write_text(
        ".agent-flow/\n.claude/\n.Codex/\n.codex/\n.omp/\n"
    )
    (project / "base.txt").write_text("base\n")
    subprocess.run(("git", "add", "."), cwd=project, check=True)
    subprocess.run(("git", "commit", "-qm", "base"), cwd=project, check=True)
    result = _install(project, home)
    assert result.returncode == 0, result.stderr
    subprocess.run(
        ("git", "worktree", "add", "-qb", "feat/runtime", str(checkout)),
        cwd=project,
        check=True,
    )

    dispatch = _dispatch(
        project,
        home,
        "record-skill-read.py",
        {"cwd": str(checkout), "tool_name": "Skill", "tool_input": {}},
    )

    assert dispatch.returncode == 0, dispatch.stderr


def test_cli_dispatch_uses_active_worktree_pinned_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    checkout = tmp_path / "checkout"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    alternate_kit = tmp_path / "alternate-kit"
    shutil.copytree(KIT_ROOT / "scripts", alternate_kit / "scripts")
    shutil.copytree(
        KIT_ROOT / "src" / "agent_flow", alternate_kit / "src" / "agent_flow"
    )
    shutil.copytree(KIT_ROOT / "templates", alternate_kit / "templates")
    alternate_cli = alternate_kit / "scripts" / "hook-runtime" / "agent-flow-cli.py"
    alternate_cli.write_bytes(
        alternate_cli.read_bytes() + b"\nRUNTIME_VARIANT = 'pinned-old'\n"
    )
    module = KIT_ROOT / "lib" / "shared-hook-runtime.mjs"
    old_publish = subprocess.run(
        (
            _node(),
            "--input-type=module",
            "-e",
            "import { installSharedHookRuntime } from "
            f"{json.dumps(module.as_uri())};"
            "installSharedHookRuntime({"
            f"kitRoot:{json.dumps(str(alternate_kit))},"
            f"homeDir:{json.dumps(str(home / '.agent-flow'))}"
            "});",
        ),
        env={**os.environ, "PYTHON": sys.executable},
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert old_publish.returncode == 0, old_publish.stderr
    pinned_digest = json.loads(
        (home / ".agent-flow" / "hook-runtime.json").read_text(encoding="utf-8")
    )["active_runtime_digest"]
    subprocess.run(("git", "init", "-q", "-b", "main"), cwd=project, check=True)
    subprocess.run(
        ("git", "config", "user.email", "test@example.com"),
        cwd=project,
        check=True,
    )
    subprocess.run(
        ("git", "config", "user.name", "Test"),
        cwd=project,
        check=True,
    )
    (project / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(("git", "add", "tracked.txt"), cwd=project, check=True)
    subprocess.run(("git", "commit", "-q", "-m", "base"), cwd=project, check=True)
    result = _install(project, home)
    assert result.returncode == 0, result.stderr
    subprocess.run(
        ("git", "worktree", "add", "--detach", str(checkout)),
        cwd=project,
        check=True,
        capture_output=True,
    )
    kit, _ = _installed_runtime(project)
    launcher_namespace = runpy.run_path(kit["hook_runtime"]["launcher_path"])
    adopted = project / ".git" / "agent-flow" / "adopted"
    adopted.mkdir(parents=True)
    record = adopted / "checkout.json"
    record.write_text(
        json.dumps(
            {
                "path": str(checkout),
                "registration_identity": launcher_namespace[
                    "_adopted_checkout_identity"
                ](checkout, project),
            }
        ),
        encoding="utf-8",
    )
    record.chmod(0o600)
    active_run = (
        project
        / ".git"
        / "agent-flow"
        / "worktrees"
        / "checkout"
        / ".agent-flow"
        / "runs"
        / "active-run"
    )
    active_run.mkdir(parents=True)
    (active_run / "active").write_text("", encoding="utf-8")
    (active_run / "meta.json").write_text("{}\n", encoding="utf-8")
    assert pinned_digest != kit["hook_runtime"]["digest"]
    _write_runtime_binding(home / ".agent-flow", active_run, pinned_digest)
    observed: dict[str, object] = {}

    class Dispatched(RuntimeError):
        pass

    def execve(
        executable: str,
        argv: list[str],
        environment: dict[str, str],
    ) -> None:
        observed["executable"] = executable
        observed["argv"] = argv
        observed["environment"] = environment
        raise Dispatched

    monkeypatch.setattr(launcher_namespace["os"], "execve", execve)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            kit["hook_runtime"]["launcher_path"],
            "--root",
            str(project),
            "--cli",
            "status",
        ],
    )
    monkeypatch.chdir(checkout)

    with pytest.raises(Dispatched):
        launcher_namespace["main"]()

    environment = observed["environment"]
    assert isinstance(environment, dict)
    assert environment["AGENT_FLOW_RUNTIME_DIGEST"] == pinned_digest
    assert Path(environment["AGENT_FLOW_RUNTIME_ENTRYPOINT"]).parent.name == pinned_digest


def test_explicit_project_root_cannot_be_hijacked_by_an_adoption_record(
    tmp_path: Path,
) -> None:
    project_b = tmp_path / "project-b"
    project_a = project_b / "nested-a"
    home = tmp_path / "home"
    project_a.mkdir(parents=True)
    home.mkdir()
    subprocess.run(("git", "init", "-q", "-b", "main"), cwd=project_b, check=True)
    disabled = _install(project_a, home, "--no-hooks")
    assert disabled.returncode == 0, disabled.stderr
    enabled = _install(project_b, home, "--hooks")
    assert enabled.returncode == 0, enabled.stderr
    adopted = project_a / ".git" / "agent-flow" / "adopted"
    adopted.mkdir(parents=True)
    record = adopted / "hijack.json"
    record.write_text(
        json.dumps({"path": str(project_b.resolve())}),
        encoding="utf-8",
    )
    record.chmod(0o600)

    dispatch = _dispatch(
        project_b,
        home,
        "guard-protected-branch.sh",
        {
            "cwd": str(project_b),
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m blocked"},
        },
        requested_root=project_b,
    )

    assert dispatch.returncode == 2, dispatch.stderr
    assert "BLOCKED" in dispatch.stderr


def test_launcher_selects_runtime_digest_for_each_routing_context(tmp_path: Path) -> None:
    project = tmp_path / "project"
    checkout = tmp_path / "checkout"
    nested_checkout = project / ".agent-flow" / "worktrees" / "nested"
    outside = tmp_path / "outside"
    home = tmp_path / "home"
    for target in (project, outside, home):
        target.mkdir()
    subprocess.run(("git", "init", "-q", "-b", "main"), cwd=project, check=True)
    subprocess.run(
        ("git", "config", "user.email", "test@example.com"),
        cwd=project,
        check=True,
    )
    subprocess.run(
        ("git", "config", "user.name", "Test"),
        cwd=project,
        check=True,
    )
    (project / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(("git", "add", "tracked.txt"), cwd=project, check=True)
    subprocess.run(("git", "commit", "-q", "-m", "base"), cwd=project, check=True)
    result = _install(project, home)
    assert result.returncode == 0, result.stderr
    for target in (checkout, nested_checkout):
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ("git", "worktree", "add", "--detach", str(target)),
            cwd=project,
            check=True,
            capture_output=True,
        )
    kit, _runtime = _installed_runtime(project)
    launcher_namespace = runpy.run_path(kit["hook_runtime"]["launcher_path"])
    launcher_select = launcher_namespace["_selected_runtime_digest"]
    project_digest = kit["hook_runtime"]["digest"]
    leader_digest = "1" * 64
    worktree_digest = "2" * 64

    nested_digest = "3" * 64
    leader_run = project / ".agent-flow" / "runs" / "leader"
    leader_run.mkdir(parents=True)
    (leader_run / "active").write_text("", encoding="utf-8")
    (leader_run / "meta.json").write_text(
        json.dumps({}),
        encoding="utf-8",
    )
    _write_runtime_binding(home / ".agent-flow", leader_run, leader_digest)
    adopted = project / ".git" / "agent-flow" / "adopted"
    adopted.mkdir(parents=True)
    identity_for = launcher_namespace["_adopted_checkout_identity"]
    (adopted / "checkout.json").write_text(
        json.dumps(
            {
                "path": str(checkout),
                "registration_identity": identity_for(checkout, project),
            }
        ),
        encoding="utf-8",
    )
    (adopted / "checkout.json").chmod(0o600)
    (adopted / "nested.json").write_text(
        json.dumps(
            {
                "path": str(nested_checkout),
                "registration_identity": identity_for(nested_checkout, project),
            }
        ),
        encoding="utf-8",
    )
    (adopted / "nested.json").chmod(0o600)
    worktree_run = (
        project
        / ".git"
        / "agent-flow"
        / "worktrees"
        / "checkout"
        / ".agent-flow"
        / "runs"
        / "active-run"
    )
    worktree_run.mkdir(parents=True)
    (worktree_run / "active").write_text("", encoding="utf-8")
    (worktree_run / "meta.json").write_text(
        json.dumps({}),
        encoding="utf-8",
    )
    _write_runtime_binding(home / ".agent-flow", worktree_run, worktree_digest)
    nested_run = (
        project
        / ".git"
        / "agent-flow"
        / "worktrees"
        / "nested"
        / ".agent-flow"
        / "runs"
        / "active-run"
    )
    nested_run.mkdir(parents=True)
    (nested_run / "active").write_text("", encoding="utf-8")
    (nested_run / "meta.json").write_text(
        json.dumps({}),
        encoding="utf-8",
    )
    _write_runtime_binding(home / ".agent-flow", nested_run, nested_digest)

    scenarios = (
        ({}, leader_digest),
        ({"cwd": str(project)}, leader_digest),
        ({"cwd": str(checkout)}, worktree_digest),
        ({"cwd": str(nested_checkout)}, nested_digest),
        ({"cwd": str(outside)}, project_digest),
        ({"tool_input": {"workdir": str(checkout)}}, worktree_digest),
    )
    for payload, expected in scenarios:
        assert launcher_select(project, kit, payload) == expected

    checkout_local_digest = "4" * 64
    checkout_local_run = checkout / ".agent-flow" / "runs" / "local-active"
    checkout_local_run.mkdir(parents=True)
    (checkout_local_run / "active").write_text("", encoding="utf-8")
    (checkout_local_run / "meta.json").write_text(
        json.dumps({}),
        encoding="utf-8",
    )
    _write_runtime_binding(
        home / ".agent-flow",
        checkout_local_run,
        checkout_local_digest,
    )
    with pytest.raises(SystemExit):
        launcher_select(project, kit, {"cwd": str(checkout)})

    (worktree_run / "active").unlink()
    assert (
        launcher_select(project, kit, {"cwd": str(checkout)})
        == checkout_local_digest
    )

    subprocess.run(
        ("git", "worktree", "remove", "--force", str(checkout)),
        cwd=project,
        check=True,
        capture_output=True,
    )
    checkout.mkdir()
    with pytest.raises(SystemExit):
        launcher_select(project, kit, {"cwd": str(checkout)})


def test_routing_directory_swap_cannot_select_an_attacker_digest(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    attacker_root = tmp_path / "attacker-routing"
    project.mkdir()
    home.mkdir()
    subprocess.run(("git", "init", "-q", "-b", "main"), cwd=project, check=True)
    result = _install(project, home)
    assert result.returncode == 0, result.stderr
    kit, runtime = _installed_runtime(project)
    routing_root = project / ".git" / "agent-flow"
    trusted_run = (
        routing_root
        / "worktrees"
        / "leader"
        / ".agent-flow"
        / "runs"
        / "trusted"
    )
    attacker_run = (
        attacker_root
        / "worktrees"
        / "leader"
        / ".agent-flow"
        / "runs"
        / "attacker"
    )
    trusted_run.mkdir(parents=True)
    attacker_run.mkdir(parents=True)
    (trusted_run / "active").write_text("", encoding="utf-8")
    (attacker_run / "active").write_text("", encoding="utf-8")
    trusted_digest = "5" * 64
    attacker_digest = "6" * 64
    _write_runtime_binding(home / ".agent-flow", trusted_run, trusted_digest)
    _write_runtime_binding(
        home / ".agent-flow",
        routing_root
        / "worktrees"
        / "leader"
        / ".agent-flow"
        / "runs"
        / "attacker",
        attacker_digest,
    )
    namespaces = (runpy.run_path(kit["hook_runtime"]["launcher_path"]),)

    for namespace in namespaces:
        selector = namespace["_selected_runtime_digest"]
        globals_ = selector.__globals__
        original_open = globals_["_open_directory_path"]
        held_root = project / ".git" / "agent-flow-held"
        swapped = False

        def racing_open(path: Path, **kwargs: object) -> int | None:
            nonlocal swapped
            fd = original_open(path, **kwargs)
            if path == routing_root and fd is not None and not swapped:
                routing_root.rename(held_root)
                routing_root.symlink_to(attacker_root, target_is_directory=True)
                swapped = True
            return fd

        globals_["_open_directory_path"] = racing_open
        try:
            selected = selector(project, kit, {"cwd": str(project)})
            assert selected == trusted_digest
            assert selected != attacker_digest
        finally:
            globals_["_open_directory_path"] = original_open
            if routing_root.is_symlink():
                routing_root.unlink()
            if held_root.exists():
                held_root.rename(routing_root)


def test_run_state_ancestor_symlink_swap_fails_closed(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    attacker_state = tmp_path / "attacker-state"
    project.mkdir()
    home.mkdir()
    attacker_state.mkdir()
    result = _install(project, home)
    assert result.returncode == 0, result.stderr
    kit, runtime = _installed_runtime(project)
    trusted_run = project / ".agent-flow" / "runs" / "trusted"
    attacker_run = attacker_state / "runs" / "attacker"
    trusted_run.mkdir(parents=True)
    attacker_run.mkdir(parents=True)
    (trusted_run / "active").write_text("", encoding="utf-8")
    (attacker_run / "active").write_text("", encoding="utf-8")
    _write_runtime_binding(home / ".agent-flow", trusted_run, "7" * 64)
    _write_runtime_binding(
        home / ".agent-flow",
        project / ".agent-flow" / "runs" / "attacker",
        "8" * 64,
    )
    namespaces = (runpy.run_path(kit["hook_runtime"]["launcher_path"]),)

    for namespace in namespaces:
        selector = namespace["_selected_runtime_digest"]
        globals_ = selector.__globals__
        original_open = globals_["_open_directory_path"]
        state_root = project / ".agent-flow"
        held_state = project / ".agent-flow-held"
        swapped = False

        def racing_open(path: Path, **kwargs: object) -> int | None:
            nonlocal swapped
            fd = original_open(path, **kwargs)
            if path == project and fd is not None and not swapped:
                state_root.rename(held_state)
                state_root.symlink_to(attacker_state, target_is_directory=True)
                swapped = True
            return fd

        globals_["_open_directory_path"] = racing_open
        try:
            with pytest.raises(SystemExit):
                selector(project, kit, {"cwd": str(project)})
        finally:
            globals_["_open_directory_path"] = original_open
            if state_root.is_symlink():
                state_root.unlink()
            if held_state.exists():
                held_state.rename(state_root)


def test_active_run_marker_race_never_falls_back_to_project_digest(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    result = _install(project, home)
    assert result.returncode == 0, result.stderr
    kit, _runtime = _installed_runtime(project)
    active_run = project / ".agent-flow" / "runs" / "active-run"
    active_run.mkdir(parents=True)
    active_marker = active_run / "active"
    _write_runtime_binding(home / ".agent-flow", active_run, "9" * 64)
    namespaces = (runpy.run_path(kit["hook_runtime"]["launcher_path"]),)

    for namespace in namespaces:
        active_marker.write_text("", encoding="utf-8")
        selector = namespace["_selected_runtime_digest"]
        globals_ = selector.__globals__
        original_open = globals_["_open_owned_at"]
        raced = False

        def racing_open(
            parent_fd: int,
            name: str,
            **kwargs: object,
        ) -> int | None:
            nonlocal raced
            if name == "active" and not raced:
                active_marker.unlink()
                raced = True
            return original_open(parent_fd, name, **kwargs)

        globals_["_open_owned_at"] = racing_open
        try:
            with pytest.raises(SystemExit):
                selector(project, kit, {"cwd": str(project)})
        finally:
            globals_["_open_owned_at"] = original_open
            active_marker.unlink(missing_ok=True)


def test_adoption_record_symlink_swap_fails_closed(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    subprocess.run(("git", "init", "-q", "-b", "main"), cwd=project, check=True)
    result = _install(project, home)
    assert result.returncode == 0, result.stderr
    kit, runtime = _installed_runtime(project)
    adopted = project / ".git" / "agent-flow" / "adopted"
    adopted.mkdir(parents=True)
    record_path = adopted / "checkout.json"
    record_path.write_text(
        json.dumps({"path": str(project), "registration_identity": "unused"}),
        encoding="utf-8",
    )
    record_path.chmod(0o600)
    alternate = tmp_path / "alternate-record.json"
    alternate.write_text(record_path.read_text(encoding="utf-8"), encoding="utf-8")
    alternate.chmod(0o600)
    namespaces = (runpy.run_path(kit["hook_runtime"]["launcher_path"]),)

    for namespace in namespaces:
        selector = namespace["_selected_runtime_digest"]
        globals_ = selector.__globals__
        original_read = globals_["_read_json_at"]
        held_record = adopted / "checkout.saved"
        swapped = False

        def racing_read(
            parent_fd: int,
            name: str,
            **kwargs: object,
        ) -> dict | None:
            nonlocal swapped
            if name == record_path.name and not swapped:
                record_path.rename(held_record)
                record_path.symlink_to(alternate)
                swapped = True
            return original_read(parent_fd, name, **kwargs)

        globals_["_read_json_at"] = racing_read
        try:
            with pytest.raises(SystemExit):
                selector(project, kit, {"cwd": str(project)})
        finally:
            globals_["_read_json_at"] = original_read
            if record_path.is_symlink():
                record_path.unlink()
            if held_record.exists():
                held_record.rename(record_path)


def test_hook_dispatch_uses_stable_runtime_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    subprocess.run(("git", "init", "-q", "-b", "main", str(project)), check=True)
    result = _install(project, home)
    assert result.returncode == 0, result.stderr

    dispatch = _dispatch(
        project,
        home,
        "guard-protected-branch.sh",
        {
            "cwd": str(project),
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m blocked"},
        },
        extra_env={"AGENT_FLOW_MANAGED_PYTHON": "/bin/true"},
    )

    assert dispatch.returncode == 2, dispatch.stderr
    assert "BLOCKED" in dispatch.stderr
    kit, runtime = _installed_runtime(project)
    runtime_dir = runtime.parent
    runtime_namespace = runpy.run_path(str(runtime))
    files, _ = runtime_namespace["_verify_runtime_bundle"](
        runtime_dir,
        kit["hook_runtime"]["digest"],
    )
    observed: dict[str, object] = {}

    def fake_run(
        command: list[str],
        **kwargs: object,
    ) -> tuple[subprocess.CompletedProcess[bytes], bool]:
        observed.update(kwargs)
        return (
            subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b""),
            False,
        )

    monkeypatch.setitem(
        runtime_namespace["_run_hook"].__globals__,
        "_run_managed_process",
        fake_run,
    )
    assert (
        runtime_namespace["_run_hook"](
            runtime_dir,
            files,
            "record-skill-read.py",
            b"{}",
            project.resolve(),
        )
        == 0
    )
    assert observed["cwd"] == runtime_dir
    assert observed["env"]["AGENT_FLOW_PROJECT_ROOT"] == str(project.resolve())


@pytest.mark.parametrize(
    ("script_name", "payload"),
    [
        (
            "guard-protected-branch.sh",
            {"tool_name": "Bash", "tool_input": {"command": "git status"}},
        ),
        (
            "guard-host-worktree.sh",
            {"tool_name": "write", "tool_input": {"path": "feature.py"}},
        ),
    ],
)
def test_enforcement_guards_fail_closed_without_the_managed_python(
    tmp_path: Path,
    script_name: str,
    payload: dict,
) -> None:
    missing = tmp_path / "missing-python"
    result = subprocess.run(
        ("/bin/bash", str(KIT_ROOT / "scripts" / "hooks" / script_name)),
        cwd=tmp_path,
        env={**os.environ, "AGENT_FLOW_MANAGED_PYTHON": str(missing)},
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "중단" in result.stderr or "BLOCKED" in result.stderr


def test_pre_tool_payload_is_fail_closed_and_shell_startup_env_is_removed(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    subprocess.run(("git", "init", "-q", "-b", "main"), cwd=project, check=True)
    result = _install(project, home)
    assert result.returncode == 0, result.stderr
    kit, _ = _installed_runtime(project)
    runtime = kit["hook_runtime"]
    command = (
        runtime["python"],
        "-I",
        runtime["launcher_path"],
        "--root",
        str(project),
        "--event",
        "PreToolUse",
    )
    environment = {
        **os.environ,
        "HOME": str(home),
        "AGENT_FLOW_HOME": str(home / ".agent-flow"),
    }
    for payload in ("", "{", "[]", "{}"):
        dispatch = subprocess.run(
            command,
            cwd=home,
            env=environment,
            input=payload,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert dispatch.returncode == 70, (payload, dispatch.stderr)

    marker = tmp_path / "shell-startup-ran"
    startup = tmp_path / "startup.sh"
    startup.write_text(f"touch {shlex.quote(str(marker))}\n", encoding="utf-8")
    dispatch = _dispatch(
        project,
        home,
        "guard-protected-branch.sh",
        {
            "cwd": str(project),
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
        },
        extra_env={"BASH_ENV": str(startup), "ENV": str(startup)},
    )

    assert dispatch.returncode == 0, dispatch.stderr
    assert not marker.exists()


def test_agent_flow_home_overrides_home_for_shared_runtime(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    shared_home = tmp_path / "shared-home"
    project.mkdir()
    home.mkdir()
    shared_home.mkdir()

    result = _install(project, home, agent_flow_home=shared_home)

    assert result.returncode == 0, result.stderr
    kit, runtime = _installed_runtime(project)
    digest = kit["hook_runtime"]["digest"]
    assert runtime == shared_home / "runtimes" / digest / "agent-flow-hook.py"
    assert kit["hook_runtime"]["launcher_path"] == str(
        shared_home / "bin" / "agent-flow-hook"
    )
    assert (shared_home / "hook-runtime.json").is_file()
    assert not (home / ".agent-flow").exists()


def test_project_state_symlink_is_rejected_before_writing_outside(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    project.mkdir()
    home.mkdir()
    outside.mkdir()
    (project / ".agent-flow").symlink_to(outside, target_is_directory=True)

    result = _install(project, home)
    assert "symlinked project managed path" in result.stderr
    assert result.returncode != 0
    assert not tuple(outside.iterdir())


def test_shared_hook_home_symlink_is_rejected_before_target_mode_changes(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    target = tmp_path / "target"
    shared_home = tmp_path / "shared-home"
    project.mkdir()
    home.mkdir()
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    shared_home.symlink_to(target, target_is_directory=True)

    result = _install(project, home, agent_flow_home=shared_home)

    assert result.returncode != 0
    assert "shared hook home" in result.stderr
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert not tuple(target.iterdir())


def test_shared_hook_home_with_a_writable_ancestor_is_rejected(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    unsafe = tmp_path / "unsafe"
    project.mkdir()
    home.mkdir()
    unsafe.mkdir()
    unsafe.chmod(0o777)

    result = _install(project, home, agent_flow_home=unsafe / "shared-home")

    assert result.returncode != 0
    assert "unsafe writable ancestor" in result.stderr
    assert not (unsafe / "shared-home").exists()


def test_install_migrates_owned_state_with_only_an_unsafe_python_ancestor(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    installed = _install(project, home)
    assert installed.returncode == 0, installed.stderr
    unsafe_python, marker = _unsafe_legacy_python(tmp_path)
    state_path = home / ".agent-flow" / "hook-runtime.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["python"] = str(unsafe_python)
    state_path.write_text(f"{json.dumps(state, indent=2)}\n", encoding="utf-8")
    state_path.chmod(0o600)

    migrated = _install(project, home)

    assert migrated.returncode == 0, migrated.stderr
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    final_kit, _ = _installed_runtime(project)
    assert final_state["python"] == final_kit["hook_runtime"]["python"]
    assert final_state["python"] != str(unsafe_python)
    assert not marker.exists()


def test_install_refuses_unsafe_python_migration_from_unowned_state(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    installed = _install(project, home)
    assert installed.returncode == 0, installed.stderr
    unsafe_python, marker = _unsafe_legacy_python(tmp_path)
    state_path = home / ".agent-flow" / "hook-runtime.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["python"] = str(unsafe_python)
    state_path.write_text(f"{json.dumps(state, indent=2)}\n", encoding="utf-8")
    state_path.chmod(0o644)

    refused = _install(project, home)

    assert refused.returncode != 0
    assert json.loads(state_path.read_text(encoding="utf-8"))["python"] == str(
        unsafe_python
    )
    assert not marker.exists()


def test_stable_launcher_ignores_path_python_and_pythonpath(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    fake_bin = tmp_path / "fake-bin"
    injected = tmp_path / "injected"
    fake_marker = tmp_path / "fake-python-ran"
    startup_marker = tmp_path / "pythonpath-ran"
    project.mkdir()
    home.mkdir()
    fake_bin.mkdir()
    injected.mkdir()
    result = _install(project, home)
    assert result.returncode == 0, result.stderr

    fake_source = f"#!/bin/sh\nprintf ran > {shlex.quote(str(fake_marker))}\nexit 99\n"
    for name in ("python", "python3"):
        candidate = fake_bin / name
        candidate.write_text(fake_source, encoding="utf-8")
        candidate.chmod(0o755)
    (injected / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(startup_marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )

    dispatch = _dispatch(
        project,
        home,
        "record-skill-read.py",
        {"cwd": str(project), "tool_name": "Skill", "tool_input": {}},
        extra_env={
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHONPATH": str(injected),
            "PYTHONSTARTUP": str(injected / "sitecustomize.py"),
        },
    )

    assert dispatch.returncode == 0, dispatch.stderr
    assert not fake_marker.exists()
    assert not startup_marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="trusted Python wrappers are POSIX-only")
def test_second_project_python_becomes_global_without_invalidating_first_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_python_root: Path,
) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    home = tmp_path / "home"
    project_a.mkdir()
    project_b.mkdir()
    home.mkdir()

    install_a = _install(project_a, home)
    assert install_a.returncode == 0, install_a.stderr
    initial_settings = json.loads(
        (home / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    initial_bootstrap_command = initial_settings["hooks"]["PreToolUse"][0]["hooks"][0][
        "command"
    ]
    initial_bootstrap_tokens = shlex.split(initial_bootstrap_command)
    assert initial_bootstrap_tokens[1:3] == ["-I", "-c"]
    prior_bootstrap = initial_bootstrap_tokens[3] + "\n# prior managed bootstrap"
    python_a = trusted_python_root / "python-a"
    python_a.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    python_a.chmod(0o555)
    module = KIT_ROOT / "lib" / "shared-hook-runtime.mjs"
    manifest_path = project_a / ".agent-flow" / "kit.json"
    republish_source = f"""
import fs from "node:fs";
import {{
  installSharedHookRuntime,
  publishManagedProject,
}} from {json.dumps(module.as_uri())};
const manifestPath = {json.dumps(str(manifest_path))};
const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
manifest.hook_runtime = installSharedHookRuntime({{
  kitRoot: {json.dumps(str(KIT_ROOT))},
  homeDir: {json.dumps(str(home / ".agent-flow"))},
  managedPython: {{
    python: {json.dumps(str(python_a))},
    realpath: {json.dumps(str(python_a))},
    flag: "-I",
  }},
}});
publishManagedProject({{
  root: {json.dumps(str(project_a))},
  manifest,
}});
"""
    republish = subprocess.run(
        (_node(), "--input-type=module", "-e", republish_source),
        env={
            **os.environ,
            "HOME": str(home),
            "AGENT_FLOW_HOME": str(home / ".agent-flow"),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert republish.returncode == 0, republish.stderr
    old_launcher_command = shlex.join(
        (
            str(python_a),
            "-I",
            str(home / ".agent-flow" / "bin" / "agent-flow-hook"),
            "--event",
            "PreToolUse",
        )
    )
    old_bootstrap_command = shlex.join(
        (
            str(python_a),
            "-I",
            "-c",
            prior_bootstrap,
            "--event",
            "PreToolUse",
        )
    )
    state_path = home / ".agent-flow" / "hook-runtime.json"
    transition_state = json.loads(state_path.read_text(encoding="utf-8"))
    current_bootstrap_digest = hashlib.sha256(
        initial_bootstrap_tokens[3].encode()
    ).hexdigest()
    transition_state["bootstrap_digest"] = current_bootstrap_digest
    transition_state["bootstrap_digests"] = [
        hashlib.sha256(prior_bootstrap.encode()).hexdigest(),
        current_bootstrap_digest,
    ]
    state_path.write_text(
        f"{json.dumps(transition_state, indent=2)}\n", encoding="utf-8"
    )
    custom_command = "./custom-global-hook.sh"
    stale_registration = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "",
                    "hooks": [
                        {"type": "command", "command": old_launcher_command},
                        {"type": "command", "command": old_bootstrap_command},
                        {"type": "command", "command": custom_command},
                    ],
                }
            ]
        }
    }
    for relative in (Path(".claude/settings.json"), Path(".Codex/hooks.json")):
        target = home / relative
        target.write_text(json.dumps(stale_registration), encoding="utf-8")


    install_b = _install(project_b, home)
    assert install_b.returncode == 0, install_b.stderr
    kit_a, _ = _installed_runtime(project_a)
    kit_b, _ = _installed_runtime(project_b)
    state = json.loads(
        (home / ".agent-flow" / "hook-runtime.json").read_text(encoding="utf-8")
    )
    python_b = Path(state["python"])
    assert kit_a["hook_runtime"]["python"] == str(python_a)
    assert kit_b["hook_runtime"]["python"] == str(python_b)
    assert python_a != python_b

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENT_FLOW_HOME", str(home / ".agent-flow"))
    monkeypatch.setenv(
        "AGENT_FLOW_OMP_EXTENSION",
        str(home / ".omp" / "agent" / "extensions" / "agent-flow-hooks.ts"),
    )
    for project in (project_a, project_b):
        reports = verify_managed_hooks(project)
        assert len(reports) == 1
        assert reports[0].violations == ()

    claude = _registered_commands(home / ".claude" / "settings.json")
    codex = _registered_commands(home / ".Codex" / "hooks.json")
    assert claude == codex
    assert old_launcher_command not in claude
    assert old_bootstrap_command not in claude
    assert custom_command in claude
    managed = [command for command in claude if command != custom_command]
    assert managed
    assert all(shlex.split(command)[0] == str(python_b) for command in managed)


def test_concurrent_runtime_install_keeps_state_and_launcher_consistent(
    tmp_path: Path,
) -> None:
    shared_home = tmp_path / "shared-home"
    source = KIT_ROOT / "scripts" / "hook-runtime" / "agent-flow-hook.py"
    module = KIT_ROOT / "lib" / "shared-hook-runtime.mjs"
    kits: list[Path] = []
    for index in range(2):
        kit = tmp_path / f"kit-{index}"
        shutil.copytree(KIT_ROOT / "scripts", kit / "scripts")
        shutil.copytree(KIT_ROOT / "src" / "agent_flow", kit / "src" / "agent_flow")
        shutil.copytree(KIT_ROOT / "templates", kit / "templates")
        runtime_source = kit / "scripts" / "hook-runtime" / "agent-flow-hook.py"
        runtime_source.write_bytes(
            source.read_bytes() + f"\nRUNTIME_VARIANT = {index}\n".encode()
        )
        kits.append(kit)

    shared_home.mkdir(parents=True)
    lock = shared_home / "managed-runtime.lock"
    lock.write_text("", encoding="utf-8")
    lock.chmod(0o600)
    environment = {
        **os.environ,
        "AGENT_FLOW_HOME": str(shared_home),
        "PYTHON": sys.executable,
    }
    processes = [
        subprocess.Popen(
            (
                _node(),
                "--input-type=module",
                "-e",
                "import { installSharedHookRuntime } from "
                f"{json.dumps(module.as_uri())};"
                "installSharedHookRuntime({"
                f"kitRoot:{json.dumps(str(kits[index % len(kits)]))}"
                "});",
            ),
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for index in range(8)
    ]
    completed = [process.communicate(timeout=30) for process in processes]

    assert all(process.returncode == 0 for process in processes), completed
    state_path = shared_home / "hook-runtime.json"
    launcher = shared_home / "bin" / "agent-flow-hook"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    launcher_digest = hashlib.sha256(launcher.read_bytes()).hexdigest()
    runtime = (
        shared_home / "runtimes" / state["active_runtime_digest"] / "agent-flow-hook.py"
    )
    assert state["launcher_digest"] == launcher_digest
    manifest = json.loads(
        (runtime.parent / "runtime-manifest.json").read_text(encoding="utf-8")
    )
    entrypoint = next(
        item for item in manifest["files"] if item["path"] == "agent-flow-hook.py"
    )
    assert manifest["runtime_digest"] == state["active_runtime_digest"]
    assert hashlib.sha256(runtime.read_bytes()).hexdigest() == entrypoint["sha256"]
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o755
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o555
    assert lock.is_file()
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


@pytest.mark.skipif(
    sys.prefix == sys.base_prefix,
    reason="test runner is not using an external virtual environment",
)
def test_install_accepts_explicit_absolute_python_from_external_venv(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    external_python = Path(sys.executable)
    assert project.resolve() not in external_python.resolve().parents

    result = _install(
        project,
        home,
        extra_env={
            "PYTHON": str(external_python),
            "PYTHON_EXECUTABLE": str(external_python),
            "VIRTUAL_ENV": sys.prefix,
        },
    )

    assert result.returncode == 0, result.stderr
    kit, runtime = _installed_runtime(project)
    interpreter = Path(kit["hook_runtime"]["python"])
    assert interpreter == external_python.resolve()
    assert interpreter.is_absolute()
    assert not interpreter.is_symlink()
    state = json.loads(
        (home / ".agent-flow" / "hook-runtime.json").read_text(encoding="utf-8")
    )
    assert Path(state["python"]) == interpreter
    for host_config in (
        home / ".claude" / "settings.json",
        home / ".Codex" / "hooks.json",
    ):
        commands = _registered_commands(host_config)
        assert commands
        assert all(shlex.split(command)[0] == str(interpreter) for command in commands)
    bundled = runtime.parent / "runtime" / "python"
    assert (bundled / "click" / "__init__.py").is_file()
    assert (bundled / "yaml" / "__init__.py").is_file()
    probe = subprocess.run(
        (
            str(interpreter),
            "-I",
            "-c",
            (
                "import json,pathlib,sys;"
                "sys.path.insert(0, sys.argv[1]);"
                "import click,yaml;"
                "print(json.dumps([click.__file__, yaml.__file__]))"
            ),
            str(bundled),
        ),
        cwd=home,
        env={},
        text=True,
        capture_output=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert all(
        bundled.resolve() in Path(origin).resolve().parents
        for origin in json.loads(probe.stdout)
    )


@pytest.mark.skipif(os.name == "nt", reason="shell poison fixture is POSIX-only")
def test_install_never_executes_project_venv_or_relative_path_python(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    venv_bin = project / ".venv" / "bin"
    marker = tmp_path / "project-python-ran"
    project.mkdir()
    home.mkdir()
    venv_bin.mkdir(parents=True)
    poison = f"#!/bin/sh\nprintf ran > {shlex.quote(str(marker))}\nexit 99\n"
    for fake_python in (
        project / "python3",
        venv_bin / "python",
        venv_bin / "python3",
    ):
        fake_python.write_text(poison, encoding="utf-8")
        fake_python.chmod(0o755)

    result = _install(
        project,
        home,
        extra_env={
            "PYTHON": str(Path(".venv") / "bin" / "python"),
            "PYTHON_EXECUTABLE": str(venv_bin / "python"),
            "VIRTUAL_ENV": str(project / ".venv"),
            "PATH": (
                f".{os.pathsep}{venv_bin}{os.pathsep}"
                f"{os.environ.get('PATH', '')}"
            ),
        },
    )

    assert result.returncode == 0, result.stderr
    interpreter = Path(_installed_runtime(project)[0]["hook_runtime"]["python"])
    assert interpreter.is_absolute()
    assert project.resolve() not in interpreter.resolve().parents
    assert not marker.exists()


def test_invalid_managed_project_registry_fails_closed_without_replacement(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    shared_home = home / ".agent-flow"
    shared_home.mkdir()
    registry = shared_home / "managed-projects.json"
    registry.write_text("{invalid", encoding="utf-8")
    registry.chmod(0o600)

    result = _install(project, home)

    assert result.returncode != 0
    assert "managed project registry is invalid" in result.stderr
    assert registry.read_text(encoding="utf-8") == "{invalid"
    assert not list(shared_home.glob("managed-projects.json.invalid.*"))


def test_unsafe_managed_project_registry_is_not_quarantined(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    shared_home = home / ".agent-flow"
    shared_home.mkdir()
    registry = shared_home / "managed-projects.json"
    original = json.dumps({"protocol_version": 1, "projects": {}})
    registry.write_text(original, encoding="utf-8")
    registry.chmod(0o644)

    result = _install(project, home)

    assert result.returncode != 0
    assert "unsafe mode" in result.stderr
    assert registry.read_text(encoding="utf-8") == original
    assert not list(shared_home.glob("managed-projects.json.invalid.*"))


def test_interrupted_runtime_publish_keeps_old_project_dispatchable(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    alternate_kit = tmp_path / "alternate-kit"
    project.mkdir()
    home.mkdir()
    result = _install(project, home)
    assert result.returncode == 0, result.stderr
    _, previous_runtime = _installed_runtime(project)
    shutil.copytree(KIT_ROOT / "scripts", alternate_kit / "scripts")
    shutil.copytree(
        KIT_ROOT / "src" / "agent_flow", alternate_kit / "src" / "agent_flow"
    )
    shutil.copytree(KIT_ROOT / "templates", alternate_kit / "templates")
    alternate_source = alternate_kit / "scripts" / "hook-runtime" / "agent-flow-hook.py"
    alternate_source.write_bytes(
        previous_runtime.read_bytes() + b"\nRUNTIME_VARIANT = 2\n"
    )
    alternate_lib = alternate_kit / "lib"
    alternate_lib.mkdir()
    shutil.copy2(KIT_ROOT / "lib" / "managed-hooks.mjs", alternate_lib)
    module = alternate_lib / "shared-hook-runtime.mjs"
    module.write_text(
        (KIT_ROOT / "lib" / "shared-hook-runtime.mjs")
        .read_text(encoding="utf-8")
        .replace(
            "cannot execute selected runtime entrypoint",
            "cannot execute chosen runtime entrypoint",
        ),
        encoding="utf-8",
    )
    command = (
        _node(),
        "--input-type=module",
        "-e",
        "import { installSharedHookRuntime } from "
        f"{json.dumps(module.as_uri())};"
        "installSharedHookRuntime({"
        f"kitRoot:{json.dumps(str(alternate_kit))}"
        "});",
    )
    environment = {
        **os.environ,
        "AGENT_FLOW_HOME": str(home / ".agent-flow"),
    }

    interrupted = subprocess.run(
        command,
        env={**environment, "AGENT_FLOW_TEST_PUBLISH_FAULT": "after-launcher-publish"},
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert interrupted.returncode != 0
    state_path = home / ".agent-flow" / "hook-runtime.json"
    transition = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(transition["launcher_digests"]) == 2
    dispatch = _dispatch(
        project,
        home,
        "record-skill-read.py",
        {"cwd": str(project), "tool_name": "Skill", "tool_input": {}},
    )
    assert dispatch.returncode == 0, dispatch.stderr
    host_payload = {
        "cwd": str(project),
        "tool_name": "Skill",
        "tool_input": {},
    }
    for host_config in (
        home / ".claude" / "settings.json",
        home / ".Codex" / "hooks.json",
    ):
        host_command = next(
            command
            for command in _registered_commands(host_config)
            if shlex.split(command)[-1] == "PostToolUse"
        )
        host_dispatch = _host_dispatch(host_command, project, home, host_payload)
        assert host_dispatch.returncode == 0, host_dispatch.stderr

    recovered = subprocess.run(
        command,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert recovered.returncode == 0, recovered.stderr
    final = json.loads(state_path.read_text(encoding="utf-8"))
    assert final["launcher_digests"] == [final["launcher_digest"]]
    assert transition["launcher_digest"] not in final["launcher_digests"]


def test_interrupted_registry_transition_never_trusts_a_tampered_manifest(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    result = _install(project, home)
    assert result.returncode == 0, result.stderr
    manifest_path = project / ".agent-flow" / "kit.json"
    trusted_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    next_manifest = {**trusted_manifest, "publication_probe": "next"}
    tampered_manifest = {**trusted_manifest, "hooks": False}
    manifest_path.write_text(
        f"{json.dumps(tampered_manifest, indent=2)}\n",
        encoding="utf-8",
    )
    tampered_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    module = KIT_ROOT / "lib" / "shared-hook-runtime.mjs"
    command = (
        _node(),
        "--input-type=module",
        "-e",
        "import { publishManagedProject } from "
        f"{json.dumps(module.as_uri())};"
        "publishManagedProject({"
        f"root:{json.dumps(str(project))},"
        f"manifest:{json.dumps(next_manifest)}"
        "});",
    )
    environment = {
        **os.environ,
        "AGENT_FLOW_HOME": str(home / ".agent-flow"),
        "AGENT_FLOW_TEST_PUBLISH_FAULT": "after-transition-registry",
    }

    interrupted = subprocess.run(
        command,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert interrupted.returncode != 0
    registry = json.loads(
        (home / ".agent-flow" / "managed-projects.json").read_text(encoding="utf-8")
    )
    accepted = registry["projects"][str(project.resolve())][
        "accepted_kit_digests"
    ]
    assert tampered_digest not in accepted
    dispatch = _dispatch(
        project,
        home,
        "record-skill-read.py",
        {"cwd": str(project), "tool_name": "Skill", "tool_input": {}},
    )
    assert dispatch.returncode == 70
    assert "project manifest digest does not match" in dispatch.stderr


def test_interrupted_manifest_publish_keeps_registry_and_manifest_compatible(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    result = _install(project, home)
    assert result.returncode == 0, result.stderr
    manifest_path = project / ".agent-flow" / "kit.json"
    previous_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["publication_probe"] = "next"
    module = KIT_ROOT / "lib" / "shared-hook-runtime.mjs"
    command = (
        _node(),
        "--input-type=module",
        "-e",
        "import { publishManagedProject } from "
        f"{json.dumps(module.as_uri())};"
        "publishManagedProject({"
        f"root:{json.dumps(str(project))},"
        f"manifest:{json.dumps(manifest)}"
        "});",
    )
    environment = {
        **os.environ,
        "AGENT_FLOW_HOME": str(home / ".agent-flow"),
    }

    interrupted = subprocess.run(
        command,
        env={**environment, "AGENT_FLOW_TEST_PUBLISH_FAULT": "after-manifest"},
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert interrupted.returncode != 0
    registry_path = home / ".agent-flow" / "managed-projects.json"
    transition = json.loads(registry_path.read_text(encoding="utf-8"))
    record = transition["projects"][str(project.resolve())]
    assert record["kit_digest"] == previous_digest
    assert record["accepted_kit_digests"] == [
        hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    ]
    dispatch = _dispatch(
        project,
        home,
        "record-skill-read.py",
        {"cwd": str(project), "tool_name": "Skill", "tool_input": {}},
    )
    assert dispatch.returncode == 0, dispatch.stderr

    recovered = subprocess.run(
        command,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert recovered.returncode == 0, recovered.stderr
    final = json.loads(registry_path.read_text(encoding="utf-8"))
    final_record = final["projects"][str(project.resolve())]
    assert "accepted_kit_digests" not in final_record
    assert (
        final_record["kit_digest"]
        == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    )
