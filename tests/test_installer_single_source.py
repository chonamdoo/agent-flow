"""install 로직이 저장소에 한 벌만 있는지 본다.

`bin/agent-flow-install.mjs`와 `bin/agent-flow-kit.mjs`는 함수명 86개를 공유했고,
`ompHooksExtensionSource()`는 321줄이 바이트 동일하게 두 벌 박혀 있었다. 두 벌이면
한쪽만 고쳐도 절반만 반영되므로, 둘이 갈라지지 않았는지 보는 검사가 따로 필요해진다.
그 검사를 지우려면 사본부터 없어야 한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[1]
BIN = KIT_ROOT / "bin"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node를 찾을 수 없다")
    return node


def _trusted_system_python() -> Path:
    for candidate in (Path("/usr/bin/python3"), Path("/usr/local/bin/python3")):
        try:
            probe = subprocess.run(
                (
                    str(candidate),
                    "-I",
                    "-c",
                    "import os, sys; print(os.path.realpath(sys.executable))",
                ),
                text=True,
                capture_output=True,
                check=True,
            )
            identity = Path(probe.stdout.strip()).stat()
        except (OSError, subprocess.SubprocessError):
            continue
        mode = stat.S_IMODE(identity.st_mode)
        if identity.st_uid == 0 and mode & 0o111 and not mode & 0o022:
            return candidate
    raise RuntimeError("tests require a root-owned system Python")


def _js_sources() -> dict[str, str]:
    """진입점과 공유 모듈 전체. 어느 쪽에 사본이 생겨도 잡힌다."""
    paths = sorted(BIN.glob("*.mjs")) + sorted((KIT_ROOT / "lib").glob("*.mjs"))
    return {
        str(path.relative_to(KIT_ROOT)): path.read_text(encoding="utf-8")
        for path in paths
    }


def test_omp_hooks_extension_source_defined_once():
    """반증: 321줄 TypeScript가 두 벌이면 한쪽만 고쳐도 조용히 갈라진다."""
    definers = [
        name
        for name, text in _js_sources().items()
        if "function ompHooksExtensionSource()" in text
    ]
    assert definers == ["lib/omp-hooks-extension.mjs"], (
        f"ompHooksExtensionSource()를 정의하는 파일이 하나가 아니다: {definers}"
    )


def _extension_source(agent_flow_home: Path) -> str:
    """확장 소스는 실제로 생성해서 본다. 모듈 텍스트만 읽으면 `String.raw` 템플릿의
    보간이 끼어들어 심기는 바이트와 다른 것을 검사하게 된다.
    """
    return subprocess.run(
        (
            _node(),
            "--input-type=module",
            "-e",
            "import { ompHooksExtensionSource } from "
            f"{json.dumps(str(KIT_ROOT / 'lib' / 'omp-hooks-extension.mjs'))};"
            "process.stdout.write(ompHooksExtensionSource());",
        ),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env={**os.environ, "AGENT_FLOW_HOME": str(agent_flow_home)},
    ).stdout


def _install_extension(root: Path, source: str, extra: str = "") -> Path:
    target = root / ".omp" / "extensions" / "agent-flow-hooks.mjs"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source + extra, encoding="utf-8")
    return target


def _install_fake_shared_launcher(agent_flow_home: Path) -> None:
    launcher = agent_flow_home / "bin" / "agent-flow-hook"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        "#!/usr/bin/env python3\n"
        "import hashlib, json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "event = args[args.index('--event') + 1]\n"
        "payload_bytes = sys.stdin.buffer.read()\n"
        "payload = json.loads(payload_bytes)\n"
        "home = Path(os.environ['AGENT_FLOW_HOME'])\n"
        "with (home / 'fake-launcher-dispatches.jsonl').open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps({'args': args, 'event': event, 'payload': payload}) + '\\n')\n"
        "registry = json.loads((home / 'managed-projects.json').read_text())\n"
        "candidate = Path(payload['cwd']).resolve()\n"
        "roots = sorted(\n"
        "    (Path(value).resolve() for value in registry['projects'] if candidate == Path(value).resolve() or Path(value).resolve() in candidate.parents),\n"
        "    key=lambda value: len(value.parts), reverse=True,\n"
        ")\n"
        "if not roots:\n"
        "    raise SystemExit(0)\n"
        "root = roots[0]\n"
        "record = registry['projects'][str(root)]\n"
        "manifest_path = root / '.agent-flow' / 'kit.json'\n"
        "try:\n"
        "    manifest = manifest_path.read_bytes()\n"
        "except OSError as error:\n"
        "    print(f'registered project manifest is invalid: {error}', file=sys.stderr)\n"
        "    raise SystemExit(70)\n"
        "accepted = [record['kit_digest'], *record.get('accepted_kit_digests', [])]\n"
        "if hashlib.sha256(manifest).hexdigest() not in accepted:\n"
        "    print('project manifest digest does not match the private registry', file=sys.stderr)\n"
        "    raise SystemExit(70)\n"
        "tool_name = str(payload.get('tool_name') or payload.get('tool') or '').lower()\n"
        "command_tools = {'bash', 'shell', 'run_terminal_cmd', 'execute_command', 'local_shell', 'terminal'}\n"
        "write_tools = {'apply_patch', 'write', 'edit', 'multiedit', 'multi_edit', 'write_file', 'edit_file'}\n"
        "read_tools = {'read', 'read_file', 'view', 'cat', 'skill'}\n"
        "if event == 'Stop':\n"
        "    tool_class = 'stop'\n"
        "elif tool_name in command_tools:\n"
        "    tool_class = 'command'\n"
        "elif tool_name in write_tools:\n"
        "    tool_class = 'write'\n"
        "elif tool_name in read_tools:\n"
        "    tool_class = 'read'\n"
        "elif event == 'PreToolUse':\n"
        "    print('PreToolUse payload has no supported tool identity', file=sys.stderr)\n"
        "    raise SystemExit(70)\n"
        "else:\n"
        "    raise SystemExit(0)\n"
        "policies = {\n"
        "    'PreToolUse': {\n"
        "        'command': ('guard-protected-branch.sh', 'guard-host-worktree.sh'),\n"
        "        'write': ('guard-host-worktree.sh',),\n"
        "    },\n"
        "    'PostToolUse': {\n"
        "        'command': ('record-command-run.py', 'bind-host-worktree.py', 'guard-host-worktree.sh', 'worktree-tripwire.py'),\n"
        "        'write': (),\n"
        "        'read': (),\n"
        "    },\n"
        "    'Stop': {'stop': ('show-phase-status.sh',)},\n"
        "}\n"
        "for hook in policies.get(event, {}).get(tool_class, ()):\n"
        "    target = root / '.agent-flow' / 'scripts' / 'hooks' / hook\n"
        "    if not target.is_file():\n"
        "        print(f'agent-flow managed hook is missing: {target}', file=sys.stderr)\n"
        "        raise SystemExit(78)\n"
        "    command = [sys.executable, '-I', str(target)] if target.suffix == '.py' else ['/bin/bash', str(target)]\n"
        "    result = subprocess.run(command, input=payload_bytes)\n"
        "    if result.returncode:\n"
        "        raise SystemExit(result.returncode)\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    state = agent_flow_home / "hook-runtime.json"
    state.write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "launcher_digest": hashlib.sha256(launcher.read_bytes()).hexdigest(),
                "launcher_digests": [
                    hashlib.sha256(launcher.read_bytes()).hexdigest()
                ],
            }
        ),
        encoding="utf-8",
    )
    state.chmod(0o600)


def _omp_env(root: Path, agent_flow_home: Path | None = None) -> dict[str, str]:
    return {
        **os.environ,
        "AGENT_FLOW_HOME": str(agent_flow_home or root / ".agent-flow"),
    }


def _run_bash_tool_call(
    root: Path,
    source: str,
    *,
    agent_flow_home: Path | None = None,
    install_runtime: bool = True,
    tool_name: str = "Bash",
    tool_input: dict[str, object] | None = None,
) -> tuple[str, str]:
    target = _install_extension(root, source)
    shared_home = agent_flow_home or root / ".agent-flow"
    if install_runtime and (root / ".agent-flow" / "kit.json").is_file():
        _install_fake_shared_launcher(shared_home)
    event = {
        "toolName": tool_name,
        "type": "PreToolUse",
        "input": tool_input or {"command": "echo hi"},
        "session_id": "parity-session",
    }
    driver = (
        f"import ext from {json.dumps(str(target))};\n"
        "const handlers = {};\n"
        "const pi = { setLabel() {}, on(name, fn) { (handlers[name] = handlers[name] || []).push(fn); } };\n"
        "ext(pi);\n"
        f"handlers.tool_call[0]({json.dumps(event)}, "
        f"{{ cwd: {json.dumps(str(root))} }}).then((out) => {{\n"
        "  process.stdout.write(JSON.stringify(out ?? null));\n"
        "}).catch((error) => {\n"
        "  process.stderr.write('driver failed: ' + (error?.stack || String(error)) + '\\n');\n"
        "  process.exitCode = 1;\n"
        "});\n"
    )
    result = subprocess.run(
        (_node(), "--input-type=module", "-e", driver),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env=_omp_env(root, shared_home),
    )
    assert result.returncode == 0, f"driver exited {result.returncode}: {result.stderr}"
    return result.stdout, result.stderr




def _run_command_result_handler(
    root: Path,
    source: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    hooks = _seed_install(root)
    shutil.copy2(
        KIT_ROOT / "scripts" / "hooks" / "record-command-run.py",
        hooks / "record-command-run.py",
    )
    binding_log = root / ".agent-flow" / "binding-events.jsonl"
    binding_recorder = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "payload = json.load(sys.stdin)\n"
        f"with Path({str(binding_log)!r}).open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(payload) + '\\n')\n"
    )
    (hooks / "bind-host-worktree.py").write_text(binding_recorder, encoding="utf-8")
    for script_name in (
        "record-skill-read.py",
        "worktree-tripwire.py",
    ):
        (hooks / script_name).write_text("pass\n", encoding="utf-8")
    (hooks / "guard-host-worktree.sh").write_text("exit 0\n", encoding="utf-8")
    _install_fake_shared_launcher(root / ".agent-flow")

    target = _install_extension(root, source)
    events = [
        {
            "type": "tool_result",
            "toolName": "bash",
            "input": {"command": "python3 -m pytest -q tests/test_ok.py"},
            "content": [{"type": "text", "text": "1 passed"}],
            "details": {"timeoutSeconds": 60, "wallTimeMs": 12},
            "isError": False,
        },
        {
            "type": "tool_result",
            "toolName": "bash",
            "input": {"command": "python3 -c 'raise SystemExit(7)'"},
            "content": [{"type": "text", "text": "Command exited with code 7"}],
            "details": {"timeoutSeconds": 60, "wallTimeMs": 12, "exitCode": 7},
            "isError": True,
        },
        {
            "type": "tool_result",
            "toolName": "bash",
            "input": {"command": "python3 -c 'raise SystemExit(9)'"},
            "content": [{"type": "text", "text": "Command exited with code 9"}],
            "details": {"timeoutSeconds": 60, "wallTimeMs": 12, "exitCode": 9},
            "isError": False,
        },
        {
            "type": "tool_result",
            "toolName": "bash",
            "input": {"command": "python3 -m pytest -q tests/test_async.py"},
            "content": [{"type": "text", "text": "Process running in background"}],
            "details": {
                "timeoutSeconds": 60,
                "wallTimeMs": 12,
                "async": {"state": "running"},
            },
            "isError": False,
        },
        {
            "type": "tool_result",
            "toolName": "bash",
            "input": {"command": "python3 -m pytest -q tests/test_timeout.py"},
            "content": [{"type": "text", "text": "Deadline exceeded"}],
            "details": {"timeoutSeconds": 60, "wallTimeMs": 60_000, "timedOut": True},
            "isError": False,
        },
    ]
    driver = (
        f"import ext from {json.dumps(str(target))};\n"
        "const handlers = {};\n"
        "const pi = { setLabel() {}, on(name, fn) { (handlers[name] = handlers[name] || []).push(fn); } };\n"
        "ext(pi);\n"
        f"const events = {json.dumps(events)};\n"
        f"const ctx = {{ cwd: {json.dumps(str(root))}, sessionManager: {{ getSessionId() {{ return 'session-1'; }} }} }};\n"
        "async function run() {\n"
        "  for (const event of events) {\n"
        "    await handlers.tool_result[0](event, ctx);\n"
        "  }\n"
        "}\n"
        "run().catch((error) => {\n"
        "  process.stderr.write('driver failed: ' + (error?.stack || String(error)) + '\\n');\n"
        "  process.exitCode = 1;\n"
        "});\n"
    )
    result = subprocess.run(
        (_node(), "--input-type=module", "-e", driver),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env=_omp_env(root),
    )
    assert result.returncode == 0, f"driver exited {result.returncode}: {result.stderr}"
    command_log = root / ".agent-flow" / "commands-run.jsonl"
    command_events = [
        json.loads(line)
        for line in command_log.read_text(encoding="utf-8").splitlines()
    ]
    binding_events = [
        json.loads(line)
        for line in binding_log.read_text(encoding="utf-8").splitlines()
    ]
    return command_events, binding_events


def _seed_install(
    root: Path,
    *,
    agent_flow_home: Path | None = None,
    registered: bool = True,
) -> Path:
    hooks = root / ".agent-flow" / "scripts" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    manifest = root / ".agent-flow" / "kit.json"
    manifest.write_text(
        json.dumps(
            {
                "hook_runtime": {
                    "python": str(_trusted_system_python()),
                },
                "hooks": True,
            }
        ),
        encoding="utf-8",
    )
    if registered:
        registry_home = agent_flow_home or root / ".agent-flow"
        registry_home.mkdir(parents=True, exist_ok=True)
        registry = registry_home / "managed-projects.json"
        registry.write_text(
            json.dumps(
                {
                    "protocol_version": 1,
                    "projects": {
                        str(root.resolve()): {
                            "root": str(root.resolve()),
                            "kit_digest": hashlib.sha256(
                                manifest.read_bytes()
                            ).hexdigest(),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        registry.chmod(0o600)
    return hooks


def _run_fake_launcher(
    shared_home: Path,
    payload: dict[str, object],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            str(_trusted_system_python()),
            "-I",
            str(shared_home / "bin" / "agent-flow-hook"),
            "--event",
            "PreToolUse",
        ),
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
        env={**os.environ, "AGENT_FLOW_HOME": str(shared_home)},
    )


@pytest.mark.parametrize("blocked", [False, True])
def test_claude_codex_omp_share_the_launcher_decision(
    tmp_path: Path,
    blocked: bool,
) -> None:
    shared_home = tmp_path / ".agent-flow"
    project = tmp_path / "project"
    project.mkdir()
    hooks = _seed_install(project, agent_flow_home=shared_home)
    guard = hooks / "guard-protected-branch.sh"
    guard.write_text(
        'echo "canonical launcher denied" >&2\nexit 2\n'
        if blocked
        else "exit 0\n",
        encoding="utf-8",
    )
    guard.chmod(0o755)
    host_guard = hooks / "guard-host-worktree.sh"
    host_guard.write_text("exit 0\n", encoding="utf-8")
    host_guard.chmod(0o755)
    _install_fake_shared_launcher(shared_home)
    source = _extension_source(shared_home)
    payload = {
        "cwd": str(project),
        "hook_event_name": "PreToolUse",
        "session_id": "parity-session",
        "tool_name": "Bash",
        "tool": "Bash",
        "tool_input": {"command": "echo hi"},
        "input": {"command": "echo hi"},
        "parameters": {"command": "echo hi"},
    }

    claude = _run_fake_launcher(shared_home, payload)
    codex = _run_fake_launcher(shared_home, payload)
    stdout, stderr = _run_bash_tool_call(
        project,
        source,
        agent_flow_home=shared_home,
        install_runtime=False,
    )
    omp = json.loads(stdout)

    assert (claude.returncode != 0) is blocked
    assert (codex.returncode != 0) is blocked
    assert (omp is not None and omp["block"] is True) is blocked
    if blocked:
        assert "canonical launcher denied" in omp["reason"]
    assert stderr == ""
    omp_dispatch = json.loads(
        (shared_home / "fake-launcher-dispatches.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    assert omp_dispatch["payload"] == payload


def test_omp_unregistered_project_noop_is_decided_by_launcher(tmp_path: Path) -> None:
    shared_home = tmp_path / ".agent-flow"
    shared_home.mkdir()
    registry = shared_home / "managed-projects.json"
    registry.write_text(
        json.dumps({"protocol_version": 1, "projects": {}}),
        encoding="utf-8",
    )
    registry.chmod(0o600)
    _install_fake_shared_launcher(shared_home)
    project = tmp_path / "unregistered"
    project.mkdir()

    stdout, stderr = _run_bash_tool_call(
        project,
        _extension_source(shared_home),
        agent_flow_home=shared_home,
        install_runtime=False,
    )

    assert json.loads(stdout) is None
    assert stderr == ""
    dispatch = json.loads(
        (shared_home / "fake-launcher-dispatches.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    assert dispatch["args"] == ["--event", "PreToolUse"]
    assert dispatch["payload"]["cwd"] == str(project)


@pytest.mark.parametrize(
    ("manifest_state", "expected_reason"),
    [
        ("missing", "registered project manifest is invalid"),
        ("tampered", "project manifest digest does not match"),
    ],
)
def test_omp_registered_corrupt_manifest_block_is_decided_by_launcher(
    tmp_path: Path,
    manifest_state: str,
    expected_reason: str,
) -> None:
    shared_home = tmp_path / ".agent-flow"
    project = tmp_path / manifest_state
    project.mkdir()
    _seed_install(project, agent_flow_home=shared_home)
    _install_fake_shared_launcher(shared_home)
    manifest = project / ".agent-flow" / "kit.json"
    if manifest_state == "missing":
        manifest.unlink()
    else:
        manifest.write_text('{"hooks": false}\n', encoding="utf-8")

    stdout, stderr = _run_bash_tool_call(
        project,
        _extension_source(shared_home),
        agent_flow_home=shared_home,
        install_runtime=False,
    )

    decision = json.loads(stdout)
    assert decision["block"] is True
    assert expected_reason in decision["reason"]
    assert stderr == ""


def test_omp_applies_canonical_pretooluse_matcher_before_launcher(tmp_path: Path) -> None:
    shared_home = tmp_path / ".agent-flow"
    project = tmp_path / "project"
    project.mkdir()
    _seed_install(project, agent_flow_home=shared_home)
    _install_fake_shared_launcher(shared_home)

    stdout, stderr = _run_bash_tool_call(
        project,
        _extension_source(shared_home),
        agent_flow_home=shared_home,
        install_runtime=False,
        tool_name="CanonicalMatcherOnlyTool",
        tool_input={"value": 1},
    )

    assert json.loads(stdout) is None
    assert stderr == ""
    assert not (shared_home / "fake-launcher-dispatches.jsonl").exists()


def test_omp_extension_normalizes_v17_bash_result_exit_codes(tmp_path: Path):
    """반증: OMP v17.2.1은 완료된 foreground 성공에서 exitCode를 생략하므로
    명시적 성공만 0으로 정규화하고 running/timeout 결과는 성공으로 만들지 않아야 한다.
    """
    source = _extension_source(tmp_path / ".agent-flow")
    command_events, binding_events = _run_command_result_handler(
        tmp_path,
        source,
    )
    assert [event["exit_code"] for event in command_events] == [0, 7, 9, None, None]
    assert [event["output"] for event in binding_events] == [
        "1 passed",
        "Command exited with code 7",
        "Command exited with code 9",
        "Process running in background",
        "Deadline exceeded",
    ]


def test_legacy_managed_hook_names_declared_once() -> None:
    node_definers = [
        name
        for name, text in _js_sources().items()
        if "const MANAGED_HOOK_SCRIPTS = [" in text
    ]
    assert node_definers == ["lib/managed-hooks.mjs"]


def test_python_dispatcher_consumes_manifest_policy_sequence() -> None:
    text = (KIT_ROOT / "scripts" / "hook-runtime" / "agent-flow-hook.py").read_text(
        encoding="utf-8"
    )
    assert "event_policy = policy.get(event)" in text
    assert "policy[args.event].get(tool_class, [])" in text


def test_atomic_text_write_keeps_previous_file_when_rename_fails(
    tmp_path: Path,
) -> None:
    target = tmp_path / "settings.json"
    target.write_text("before\n", encoding="utf-8")
    module = (KIT_ROOT / "lib" / "installer-shared.mjs").as_uri()
    script = f"""
import fs from "node:fs";
import path from "node:path";
import {{ writeAtomicTextFile }} from {json.dumps(module)};
const target = {json.dumps(str(target))};
fs.renameSync = () => {{ throw new Error("injected rename failure"); }};
let failed = false;
try {{
  writeAtomicTextFile(target, "after\\n");
}} catch {{
  failed = true;
}}
const stagingPrefix = `${{path.basename(target)}}.`;
const staged = fs.readdirSync(path.dirname(target))
  .filter((name) => name.startsWith(stagingPrefix) && name.endsWith(".tmp"));
console.log(JSON.stringify({{
  failed,
  content: fs.readFileSync(target, "utf8"),
  staged,
}}));
"""

    result = subprocess.run(
        (_node(), "--input-type=module", "-e", script),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "failed": True,
        "content": "before\n",
        "staged": [],
    }


@pytest.mark.skipif(os.name == "nt", reason="hardlink invariant is POSIX-only")
def test_kit_asset_record_publish_preserves_a_hardlinked_external_inode(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    managed = root / ".agent-flow"
    external = tmp_path / "external.json"
    target = managed / "kit-assets.json"
    managed.mkdir(parents=True)
    external.write_text("external content\n", encoding="utf-8")
    os.link(external, target)
    external_inode = external.stat().st_ino
    module = (KIT_ROOT / "lib" / "installer-shared.mjs").as_uri()
    script = f"""
import {{ writeKitAssetRecord }} from {json.dumps(module)};
writeKitAssetRecord(
  {json.dumps(str(root))},
  new Map([["templates/review.md", "abc123"]]),
);
"""

    result = subprocess.run(
        (_node(), "--input-type=module", "-e", script),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert external.stat().st_ino == external_inode
    assert external.read_text(encoding="utf-8") == "external content\n"
    assert target.stat().st_ino != external_inode
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "version": 1,
        "files": {"templates/review.md": "abc123"},
    }


def test_agent_flow_install_entry_point_still_installs(tmp_path: Path):
    """불변: `agent-flow-install`은 npm `bin`으로 공개된 이름이라 사라지면 안 된다.

    구현을 합치는 것과 진입점을 없애는 것은 다르다. 소비자가 쓰는 표면은 그대로 둔다.
    """
    entry = BIN / "agent-flow-install.mjs"
    assert entry.is_file(), "공개된 진입점이 사라졌다"

    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        (_node(), str(entry), "install"),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "HOME": str(home),
            "AGENT_FLOW_HOME": str(home / ".agent-flow"),
            "PYTHON": sys.executable,
            "AGENT_FLOW_SKIP_CODEX_TRUST": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    assert (project / ".agent-flow" / "workflows" / "default.yaml").is_file()


@pytest.mark.parametrize("entry", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_installer_never_launders_managed_hook_approval(entry: str):
    """불변: install이 현재 등록된 hook 해시를 trusted로 되받아 적으면 안 된다.

    그렇게 하면 변조된 등록이 다음 install에서 승인 상태로 세탁된다. 등록 무결성은
    런 시작 시 `hook_integrity`가 `kit.json`과 대조해서 판정하는 것이지, install이
    현장에서 재승인할 일이 아니다.

    보안상 중요한 등록 본문은 공유 모듈에만 있고 두 진입점은 그 함수를 호출한다.
    진입점과 공유 본문을 함께 봐야 승인 세탁 코드가 어느 쪽에서도 되살아나지 않는다.
    """
    sources = {
        f"bin/{entry}": (BIN / entry).read_text(encoding="utf-8"),
        "lib/installer-shared.mjs": (
            KIT_ROOT / "lib" / "installer-shared.mjs"
        ).read_text(encoding="utf-8"),
    }
    for name, source in sources.items():
        for forbidden in ("[hooks.state.", "trusted_hash"):
            assert forbidden not in source, (
                f"{name}가 {forbidden!r}를 다시 들였다 — hook 승인 세탁 경로"
            )


def test_installer_removes_broad_codex_trust_but_never_adds_it():
    """불변: install은 넓은 trust를 걷어내는 쪽이지 심는 쪽이 아니다."""
    sources = _js_sources()
    definers = [
        name
        for name, text in sources.items()
        if "function removeCodexBroadTrustState(root)" in text
    ]
    assert definers == ["lib/installer-shared.mjs"], (
        f"removeCodexBroadTrustState()를 정의하는 파일이 하나가 아니다: {definers}"
    )
    for name, text in sources.items():
        assert "function installCodexTrustState(root)" not in text, name


@pytest.mark.parametrize("entry", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_both_entry_points_call_the_shared_trust_removal(entry: str):
    """반증: 한쪽이 호출을 빼면 그 진입점에서만 넓은 trust가 살아남는다."""
    source = (BIN / entry).read_text(encoding="utf-8")
    assert "removeCodexBroadTrustState(" in source
    assert "removeCodexBroadTrustState," in source, (
        f"{entry}가 공유 모듈에서 removeCodexBroadTrustState를 가져오지 않는다"
    )
    assert "installGlobalHookRegistrations(" in source
    assert "installGlobalHookRegistrations," in source, (
        f"{entry}가 공유 모듈에서 installGlobalHookRegistrations를 가져오지 않는다"
    )


# 두 진입점에 본문이 한 벌씩 있던 것들. 사본이 다시 생기면 여기서 걸린다.
_SHARED_ONLY = (
    "architectureReviewerSkillMarkdown",
    "fullFeatureSkillMarkdown",
    "productBriefSkillMarkdown",
    "pushWatchSkillMarkdown",
    "isPruneBackupName",
    "writePruneBackup",
    "managedHookScriptName",
    "codexConfigPath",
    "ompExtensionIsKitOwned",
    "removeOmpHooksExtension",
    "safeSkillName",
    "skillRequires",
    "readJsonIfExists",
    "retiredHookScripts",
    "isRetiredHookCommand",
    "pruneRetiredHooks",
    "pruneRetiredHookScripts",
    "mergeHookSettings",
    "mergeHookConfig",
    "claudeHooksSettings",
    "codexHooksSettings",
    "pathHasSymlink",
    "assertProjectHookPathsSafe",
    "installCodexHooks",
    "installClaudeHooks",
    "installOmpHooks",
    "installGlobalHookRegistrations",
    "skillIndexBlock",
    "upsertSkillIndexBlock",
    "extractCliOption",
    "cliOptionValue",
    "requestedInstallRootOption",
    "withoutInstallRootOption",
    "assertInstallRootIsFinal",
    "upgradeBundledSkills",
    "preserveKitSkillHashes",
    "syncKitAssets",
    "readKitAssetRecord",
    "writeKitAssetRecord",
    "isBundledSkillManifest",
    "reportSkippedUserEdit",
    # `samePath`/`gitEnv`/`resolveInstallRoot`는 뺐다. `lib/omp-hooks-extension.mjs`가
    # 생성물 안에 같은 이름을 들고 있어 이 검사로는 셀 수 없다.
    "canonicalPath",
    "gitOutput",
    "resolveManagedWorktreeContext",
    "resolveManagedWorktreeRoot",
    "resolveGitCommonWorktreeRoot",
    "resolveLinkedWorktreeLeader",
)


@pytest.mark.parametrize("name", _SHARED_ONLY)
def test_previously_duplicated_helper_is_defined_once(name: str):
    """불변: 사본이 하나라도 돌아오면 둘이 갈라졌는지 보는 검사가 다시 필요해진다."""
    definers = [
        source
        for source, text in _js_sources().items()
        if re.search(rf"^(?:export )?function {re.escape(name)}\(", text, re.M)
    ]
    assert definers == ["lib/installer-shared.mjs"], (
        f"{name}()를 정의하는 파일이 하나가 아니다: {definers}"
    )


@pytest.mark.parametrize("xdg_state_home", ["", "~/.agent-flow", r"~\.agent-flow"])
def test_js_does_not_treat_user_central_worktrees_as_project_markers(
    tmp_path: Path, xdg_state_home: str
):
    home = tmp_path / "home"
    project = tmp_path / "project"
    source = KIT_ROOT / "lib" / "installer-shared.mjs"
    script = (
        "import { resolveManagedWorktreeContext as resolve } from "
        f"{json.dumps(str(source))};"
        "process.stdout.write(JSON.stringify(["
        f"resolve({json.dumps(str(home / '.agent-flow' / 'worktrees' / 'project-a1b2c3d4e5f6' / 'feat-task' / 'src'))}),"
        f"resolve({json.dumps(str(project / '.agent-flow' / 'worktrees' / 'feat-task' / 'src'))})"
        "]));"
    )
    result = subprocess.run(
        (_node(), "--input-type=module", "-e", script),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "HOME": str(home), "XDG_STATE_HOME": xdg_state_home},
        timeout=60,
    )

    central, local = json.loads(result.stdout)
    assert central is None
    assert local == {"root": str(project), "name": "feat-task"}


def test_hooks_disabled_is_passed_in_not_read_from_the_entry_point():
    """불변: 공유 본문이 진입점 전역을 읽으면 그 진입점에서만 도는 코드가 된다.

    기본값을 두면 인자를 빠뜨린 호출이 hook을 켜 둔 것으로 조용히 처리된다.
    """
    shared = (KIT_ROOT / "lib" / "installer-shared.mjs").read_text(encoding="utf-8")
    for name in (
        "retiredHookScripts",
        "pruneRetiredHooks",
        "pruneRetiredHookScripts",
        "mergeHookSettings",
        "mergeHookConfig",
        "isRetiredHookCommand",
    ):
        signature = re.search(rf"^export function {name}\(([^)]*)\)", shared, re.M)
        assert signature is not None, name
        assert "hooksDisabled" in signature.group(1), name
        assert "hooksDisabled =" not in signature.group(1), name


def test_managed_hook_parser_accepts_only_exact_generated_argv(tmp_path: Path):
    home = tmp_path / "home"
    root = tmp_path / "project"
    other = tmp_path / "other"
    for directory in (home, root, other):
        directory.mkdir()
    module = (KIT_ROOT / "lib" / "installer-shared.mjs").as_uri()
    script = (
        "import { hookEventCommand, managedHookScriptName, isRetiredHookCommand } "
        f"from {json.dumps(module)};"
        f"const root = {json.dumps(str(root))};"
        f"const other = {json.dumps(str(other))};"
        "const exact = hookEventCommand(root, 'PreToolUse');"
        "const rootScoped = exact.replace(' --event', ` --root ${JSON.stringify(root)} --event`);"
        "const wrongRoot = exact.replace(' --event', ` --root ${JSON.stringify(other)} --event`);"
        "const legacy = `'${root}/.agent-flow/scripts/hooks/comment-checker.py'`;"
        "const legacyPython = `/usr/bin/python3 -I '${root}/.agent-flow/scripts/hooks/comment-checker.py'`;"
        "const legacyShell = `/bin/bash '${root}/.agent-flow/scripts/hooks/guard-protected-branch.sh'`;"
        "const retired = `'${root}/.agent-flow/scripts/hooks/guard-worktree.sh'`;"
        "process.stdout.write(JSON.stringify({"
        "exact: managedHookScriptName(exact, root),"
        "rootScoped: managedHookScriptName(rootScoped, root),"
        "wrapper: managedHookScriptName(`bash -c ${JSON.stringify(exact)}`, root),"
        "echo: managedHookScriptName(`echo ${exact}`, root),"
        "extra: managedHookScriptName(`${exact} extra`, root),"
        "wrongRoot: managedHookScriptName(wrongRoot, root),"
        "otherLauncher: managedHookScriptName(exact.replace('agent-flow-hook', 'other-hook'), root),"
        "wrongEvent: managedHookScriptName(exact.replace('PreToolUse', 'Unknown'), root),"
        "legacy: managedHookScriptName(legacy, root),"
        "legacyPython: managedHookScriptName(legacyPython, root),"
        "legacyShell: managedHookScriptName(legacyShell, root),"
        "legacyWrongFlag: managedHookScriptName(legacyPython.replace(' -I ', ' -E '), root),"
        "legacyWrongShell: managedHookScriptName(legacyShell.replace('/bin/bash', '/bin/sh'), root),"
        "legacyWrapper: managedHookScriptName(`env ${legacy}`, root),"
        "retired: isRetiredHookCommand(retired, false, root),"
        "retiredWrapper: isRetiredHookCommand(`echo ${retired}`, false, root)"
        "}));"
    )

    result = subprocess.run(
        (_node(), "--input-type=module", "-e", script),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "HOME": str(home), "AGENT_FLOW_HOME": str(home / "af")},
        timeout=60,
    )

    assert json.loads(result.stdout) == {
        "exact": "@PreToolUse",
        "rootScoped": "@PreToolUse",
        "wrapper": None,
        "echo": None,
        "extra": None,
        "wrongRoot": None,
        "otherLauncher": None,
        "wrongEvent": None,
        "legacy": "comment-checker.py",
        "legacyPython": "comment-checker.py",
        "legacyShell": "guard-protected-branch.sh",
        "legacyWrongFlag": None,
        "legacyWrongShell": None,
        "legacyWrapper": None,
        "retired": True,
        "retiredWrapper": False,
    }


def test_installer_omp_ownership_requires_exact_marker_or_legacy_digest(
    tmp_path: Path,
):
    module = (KIT_ROOT / "lib" / "installer-shared.mjs").as_uri()
    managed = tmp_path / "managed.ts"
    signature_only = tmp_path / "signature-only.ts"
    embedded_marker = tmp_path / "embedded-marker.ts"
    managed.write_text(
        "// agent-flow: managed omp extension\nexport default function hooks() {}\n",
        encoding="utf-8",
    )
    signature_only.write_text(
        "export default function agentFlowHooks() {}\n", encoding="utf-8"
    )
    embedded_marker.write_text(
        "export const mine = true;\n// agent-flow: managed omp extension\n",
        encoding="utf-8",
    )
    script = (
        f"import {{ ompExtensionIsKitOwned as owned }} from {json.dumps(module)};"
        "process.stdout.write(JSON.stringify(["
        f"owned({json.dumps(str(managed))}),"
        f"owned({json.dumps(str(signature_only))}),"
        f"owned({json.dumps(str(embedded_marker))})"
        "]));"
    )

    result = subprocess.run(
        (_node(), "--input-type=module", "-e", script),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert json.loads(result.stdout) == [True, False, False]


@pytest.mark.skipif(os.name != "posix", reason="dir-fd cleanup requires POSIX")
def test_remove_omp_hooks_extension_preserves_backup_and_user_file(
    tmp_path: Path,
):
    module = (KIT_ROOT / "lib" / "installer-shared.mjs").as_uri()

    def remove(root: Path) -> subprocess.CompletedProcess[str]:
        script = (
            "import { removeOmpHooksExtension } "
            f"from {json.dumps(module)};"
            f"removeOmpHooksExtension({json.dumps(str(root))});"
        )
        return subprocess.run(
            (_node(), "--input-type=module", "-e", script),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHON": sys.executable},
            check=False,
            timeout=60,
        )

    managed_root = tmp_path / "managed"
    managed = managed_root / ".omp" / "extensions" / "agent-flow-hooks.ts"
    managed.parent.mkdir(parents=True)
    managed_content = (
        "// agent-flow: managed omp extension\nexport default function hooks() {}\n"
    )
    managed.write_text(managed_content, encoding="utf-8")

    removed = remove(managed_root)

    assert removed.returncode == 0, removed.stderr
    assert not managed.exists()
    assert (
        managed.with_name("agent-flow-hooks.ts.removed").read_text(encoding="utf-8")
        == managed_content
    )

    user_root = tmp_path / "user"
    user = user_root / ".omp" / "extensions" / "agent-flow-hooks.ts"
    user.parent.mkdir(parents=True)
    user_content = "export const mine = true;\n"
    user.write_text(user_content, encoding="utf-8")

    preserved = remove(user_root)

    assert preserved.returncode == 0, preserved.stderr
    assert "not kit-managed" in preserved.stderr
    assert user.read_text(encoding="utf-8") == user_content
    assert not user.with_name("agent-flow-hooks.ts.removed").exists()


@pytest.mark.skipif(os.name != "posix", reason="dir-fd cleanup requires POSIX")
def test_remove_omp_hooks_extension_rejects_symlink_target_and_parent_component(
    tmp_path: Path,
):
    module = (KIT_ROOT / "lib" / "installer-shared.mjs").as_uri()

    def remove(root: Path) -> subprocess.CompletedProcess[str]:
        script = (
            "import { removeOmpHooksExtension } "
            f"from {json.dumps(module)};"
            f"removeOmpHooksExtension({json.dumps(str(root))});"
        )
        return subprocess.run(
            (_node(), "--input-type=module", "-e", script),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHON": sys.executable},
            check=False,
            timeout=60,
        )

    managed_content = (
        "// agent-flow: managed omp extension\nexport default function hooks() {}\n"
    )
    target_root = tmp_path / "target-symlink"
    target = target_root / ".omp" / "extensions" / "agent-flow-hooks.ts"
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside.ts"
    outside.write_text(managed_content, encoding="utf-8")
    target.symlink_to(outside)

    target_result = remove(target_root)

    assert target_result.returncode != 0
    assert target.is_symlink()
    assert outside.read_text(encoding="utf-8") == managed_content
    assert not target.with_name("agent-flow-hooks.ts.removed").exists()

    component_root = tmp_path / "component-symlink"
    component_root.mkdir()
    outside_omp = tmp_path / "outside-omp"
    outside_target = outside_omp / "extensions" / "agent-flow-hooks.ts"
    outside_target.parent.mkdir(parents=True)
    outside_target.write_text(managed_content, encoding="utf-8")
    (component_root / ".omp").symlink_to(outside_omp, target_is_directory=True)

    component_result = remove(component_root)

    assert component_result.returncode != 0
    assert outside_target.read_text(encoding="utf-8") == managed_content
    assert not outside_target.with_name("agent-flow-hooks.ts.removed").exists()
