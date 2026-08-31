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
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

KIT_ROOT = Path(__file__).resolve().parents[1]
BIN = KIT_ROOT / "bin"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node를 찾을 수 없다")
    return node


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


def _extension_source() -> str:
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
    ).stdout


def _git(cwd: Path, *args: str) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git을 찾을 수 없다")
    subprocess.run(
        (git, *args),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


def _install_extension(root: Path, source: str, extra: str = "") -> Path:
    """host가 실제로 심는 자리에 둔다. ROOT 산정이 이 위치에 달려 있다."""
    target = root / ".omp" / "extensions" / "agent-flow-hooks.mjs"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source + extra, encoding="utf-8")
    return target


def _resolved_hook_dir(root: Path, source: str) -> Path:
    target = _install_extension(root, source, "\nexport const __HOOK_DIR = HOOK_DIR;\n")
    return Path(
        subprocess.run(
            (
                _node(),
                "--input-type=module",
                "-e",
                f"import({json.dumps(str(target))})"
                ".then((m) => process.stdout.write(m.__HOOK_DIR));",
            ),
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )


def _run_bash_tool_call(root: Path, source: str) -> tuple[str, str]:
    """확장의 tool_call 핸들러를 Bash 이벤트로 한 번 돌린다.

    top-level await 대신 `.then`을 쓴다 — `--input-type=module -e`에서의 TLA 지원은
    node 버전에 따라 갈리고, CI(node 20)에서 이 하네스만 exit 1로 죽었다.
    """
    target = _install_extension(root, source)
    driver = (
        f"import ext from {json.dumps(str(target))};\n"
        "const handlers = {};\n"
        "const pi = { setLabel() {}, on(name, fn) { (handlers[name] = handlers[name] || []).push(fn); } };\n"
        "ext(pi);\n"
        "handlers.tool_call[0](\n"
        '  { toolName: "Bash", type: "PreToolUse", input: { command: "echo hi" } },\n'
        f"  {{ cwd: {json.dumps(str(root))} }},\n"
        ").then((out) => {\n"
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
    )
    # 실패를 CalledProcessError로 흘리면 node가 남긴 사유가 리포트에서 사라진다.
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
    )
    assert result.returncode == 0, f"driver exited {result.returncode}: {result.stderr}"
    command_log = root / ".agent-flow" / "commands-run.jsonl"
    command_events = [
        json.loads(line) for line in command_log.read_text(encoding="utf-8").splitlines()
    ]
    binding_events = [
        json.loads(line) for line in binding_log.read_text(encoding="utf-8").splitlines()
    ]
    return command_events, binding_events



def _seed_install(root: Path) -> Path:
    hooks = root / ".agent-flow" / "scripts" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (root / ".agent-flow" / "kit.json").write_text("{}", encoding="utf-8")
    # managed hook launcher: hook은 이 portable launcher로만 실행된다.
    launcher = root / ".agent-flow" / "bin" / "agent-flow-hook"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        "#!/bin/sh\n"
        "set -u\n"
        "script=$1\n"
        "shift\n"
        'case "$script" in\n'
        '  *.py) exec python3 "$script" "$@" ;;\n'
        '  *) exec /bin/sh "$script" "$@" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return hooks


def test_omp_extension_resolves_its_hook_dir_from_the_install_root(tmp_path: Path):
    """반증: worktree checkout에서 연 OMP 세션은 ROOT가 worktree라 hook을 못 찾는다.

    문자열 대조가 아니라 생성본을 실제로 임포트해 HOOK_DIR 값을 잰다. 문자열만 보면
    탐색을 중화해도(한 칸 건너뛰기, 폴백 변경) 통과한다. 여기서 고른 값은 보고용이
    아니라 실제로 실행할 hook 디렉터리다.
    """
    source = _extension_source()
    root = tmp_path.resolve()

    leader = root / "leader"
    leader.mkdir()
    _git(leader, "init", "-q")
    (leader / ".gitignore").write_text(".agent-flow/\n.omp/\n", encoding="utf-8")
    _git(leader, "add", "-A")
    _git(
        leader,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        "init",
    )
    leader_hooks = _seed_install(leader)
    checkout = leader / ".agent-flow" / "worktrees" / "w1"
    _git(leader, "worktree", "add", "-q", "-b", "w1", str(checkout))

    # 조상에 있는 남의 프로젝트 설치본. 자기 설치본이 없는 checkout이 이것을 집으면
    # 남의 hook 스크립트가 이 cwd로 실행된다.
    foreign = root / "foreign"
    foreign_hooks = _seed_install(foreign)
    outsider = foreign / "child"
    outsider.mkdir()
    _git(outsider, "init", "-q")

    # 같은 저장소 안의 더 가까운 설치본. 판정 기준은 kit.json 하나뿐이라 hooks
    # 디렉터리가 지워진 설치본도 여전히 이 설치본이다 — Python 쪽 형제 함수
    # find_install_root와 같은 조건이어야 두 구현이 같은 설치본을 고른다. 여기에
    # scripts/hooks 존재를 더하면 이 checkout이 조상 것을 집는다.
    nested = leader / "pkg" / "app"
    nested.mkdir(parents=True)
    (leader / "pkg" / ".agent-flow").mkdir()
    (leader / "pkg" / ".agent-flow" / "kit.json").write_text("{}", encoding="utf-8")
    nested_hooks = leader / "pkg" / ".agent-flow" / "scripts" / "hooks"

    # leader 밖에 수동으로 만든 worktree. 조상 어디에도 설치본이 없지만 같은
    # 저장소이므로 leader 설치본은 남의 것이 아니다.
    detached = root / "manual-wt"
    _git(leader, "worktree", "add", "-q", "-b", "w2", str(detached))

    assert _resolved_hook_dir(leader, source) == leader_hooks
    assert _resolved_hook_dir(checkout, source) == leader_hooks, (
        "managed checkout이 leader 설치본에 닿지 못하면 채팅 승인이 그대로 무시된다"
    )
    assert _resolved_hook_dir(nested, source) == nested_hooks, (
        "가장 가까운 조상의 설치본이 아니면 다른 프로젝트의 hook을 돌리는 것이다"
    )
    assert _resolved_hook_dir(detached, source) == leader_hooks, (
        "leader 밖 worktree가 hook을 못 찾으면 그 세션의 채팅 승인은 무음이다"
    )
    resolved = _resolved_hook_dir(outsider, source)
    assert resolved != foreign_hooks, "조상의 남의 설치본을 집으면 안 된다"
    assert resolved == outsider / ".agent-flow" / "scripts" / "hooks"


def test_omp_extension_separates_no_install_from_a_deleted_guard(tmp_path: Path):
    """부재의 두 종류를 가른다.

    설치본을 못 찾은 것은 이 프로젝트가 agent-flow를 안 쓰는 상태다 — 도구를 막으면
    세션이 통째로 죽는다. 반대로 설치본은 있는데 관리 hook만 사라진 것은 가드 제거이고,
    거기서 통과시키면 `rm` 한 번으로 그 세션의 경계 가드가 전부 꺼진다.
    """
    source = _extension_source()
    root = tmp_path.resolve()

    # 설치본 없음: kit.json이 없으므로 해석이 실패한다.
    unmanaged = root / "unmanaged"
    (unmanaged / ".omp" / "extensions").mkdir(parents=True)
    stdout, stderr = _run_bash_tool_call(unmanaged, source)
    assert json.loads(stdout) is None, "설치본이 없는 프로젝트의 도구를 막으면 안 된다"
    assert "agent-flow hooks are not registered" in stderr, (
        "조용히 삼키면 등록 누락을 아무도 볼 수 없다 — 이 버그의 원인이 그것이었다"
    )
    assert stderr.count("agent-flow hooks are not registered") == 1, (
        "세션당 한 번만 낸다"
    )

    # 설치본은 있는데 관리 hook만 없음: fail-closed.
    stripped = root / "stripped"
    _seed_install(stripped)
    stdout, stderr = _run_bash_tool_call(stripped, source)
    assert json.loads(stdout) == {
        "block": True,
        "reason": "agent-flow managed hook is missing: "
        + str(stripped / ".agent-flow" / "scripts" / "hooks" / "guard-protected-branch.sh"),
    }, "설치본 안에서 가드가 사라진 것은 정책 위반으로 다뤄야 한다"
    assert "agent-flow hooks are not registered" in stderr

    denying = root / "denying"
    hooks = _seed_install(denying)
    for name in (
        "guard-protected-branch.sh",
        "guard-host-worktree.sh",
    ):
        script = hooks / name
        script.write_text('echo "denied by " >&2\nexit 1\n', encoding="utf-8")
        script.chmod(0o755)
    stdout, _ = _run_bash_tool_call(denying, source)
    assert json.loads(stdout) == {"block": True, "reason": "denied by"}, (
        "스크립트가 있는데 0이 아닌 종료면 가드는 그대로 막아야 한다"
    )




def test_omp_extension_normalizes_v17_bash_result_exit_codes(tmp_path: Path):
    """반증: OMP v17.2.1은 완료된 foreground 성공에서 exitCode를 생략하므로
    명시적 성공만 0으로 정규화하고 running/timeout 결과는 성공으로 만들지 않아야 한다.
    """
    command_events, binding_events = _run_command_result_handler(
        tmp_path,
        _extension_source(),
    )
    assert [event["exit_code"] for event in command_events] == [0, 7, 9, None, None]
    assert [event["output"] for event in binding_events] == [
        "1 passed",
        "Command exited with code 7",
        "Command exited with code 9",
        "Process running in background",
        "Deadline exceeded",
    ]





def test_managed_hook_scripts_declared_once_per_language():
    """불변: 같은 hook 목록이 Node 두 곳과 Python 한 곳에 있으면 3벌이다."""
    node_definers = [
        name
        for name, text in _js_sources().items()
        if "const MANAGED_HOOK_SCRIPTS = [" in text
    ]
    assert node_definers == ["lib/managed-hooks.mjs"], (
        f"MANAGED_HOOK_SCRIPTS를 선언하는 Node 파일이 하나가 아니다: {node_definers}"
    )


def test_node_and_python_managed_hook_scripts_match():
    """불변: Node가 심는 hook과 Python이 검증하는 hook이 갈라지면 무결성 게이트가 헛돈다.

    parity 스크립트가 지키던 계약이다. 남은 두 선언은 언어가 달라 합칠 수 없으므로,
    같은 값인지는 계속 확인해야 한다 — 다만 pytest가 확인한다.
    """
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.core.hook_integrity import MANAGED_HOOK_SCRIPTS as PY_SCRIPTS

    text = (KIT_ROOT / "lib" / "managed-hooks.mjs").read_text(encoding="utf-8")
    block = text.split("const MANAGED_HOOK_SCRIPTS = [", 1)[1].split("]", 1)[0]
    node_scripts = tuple(
        line.strip().strip(",").strip('"')
        for line in block.splitlines()
        if line.strip().startswith('"')
    )

    assert node_scripts == tuple(PY_SCRIPTS)


def test_agent_flow_install_entry_point_still_installs(tmp_path: Path):
    """불변: `agent-flow-install`은 npm `bin`으로 공개된 이름이라 사라지면 안 된다.

    구현을 합치는 것과 진입점을 없애는 것은 다르다. 소비자가 쓰는 표면은 그대로 둔다.
    """
    entry = BIN / "agent-flow-install.mjs"
    assert entry.is_file(), "공개된 진입점이 사라졌다"

    project = tmp_path / "project"
    project.mkdir()
    result = subprocess.run(
        (_node(), str(entry), "install"),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (project / ".agent-flow" / "workflows" / "default.yaml").is_file()


@pytest.mark.parametrize(
    "entry", ["agent-flow-kit.mjs", "agent-flow-install.mjs"]
)
def test_installer_never_launders_managed_hook_approval(entry: str):
    """불변: install이 현재 등록된 hook 해시를 trusted로 되받아 적으면 안 된다.

    그렇게 하면 변조된 등록이 다음 install에서 승인 상태로 세탁된다. 등록 무결성은
    런 시작 시 `hook_integrity`가 `kit.json`과 대조해서 판정하는 것이지, install이
    현장에서 재승인할 일이 아니다.

    두 진입점 모두 제 `installCodexHooks`/`installClaudeHooks`/`installOmpHooks`
    본문을 갖고 있으므로 둘 다 본다. 한쪽만 보면 다른 쪽에서 조용히 되살아난다.
    """
    source = (BIN / entry).read_text(encoding="utf-8")
    for forbidden in ("[hooks.state.", "trusted_hash"):
        assert forbidden not in source, (
            f"{entry}가 {forbidden!r}를 다시 들였다 — hook 승인 세탁 경로"
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


@pytest.mark.parametrize(
    "entry", ["agent-flow-kit.mjs", "agent-flow-install.mjs"]
)
def test_both_entry_points_call_the_shared_trust_removal(entry: str):
    """반증: 한쪽이 호출을 빼면 그 진입점에서만 넓은 trust가 살아남는다."""
    source = (BIN / entry).read_text(encoding="utf-8")
    assert "removeCodexBroadTrustState(" in source
    assert "removeCodexBroadTrustState," in source, (
        f"{entry}가 공유 모듈에서 removeCodexBroadTrustState를 가져오지 않는다"
    )


@pytest.mark.parametrize(
    "entry", ["agent-flow-kit.mjs", "agent-flow-install.mjs"]
)
def test_both_entry_points_sync_the_recorded_kit_assets(entry: str):
    """반증: 한쪽이 호출을 빼면 그 CLI로 깐 프로젝트만 kit 개정을 못 받는다.
    자산 목록을 진입점에 손으로 나열하는 것도 같은 갈라짐이라 막는다."""
    source = (BIN / entry).read_text(encoding="utf-8")
    assert "syncRecordedKitAssets(" in source
    assert "syncRecordedKitAssets," in source, (
        f"{entry}가 공유 모듈에서 syncRecordedKitAssets를 가져오지 않는다"
    )
    assert "syncKitAssets(" not in source, (
        f"{entry}가 자산 트리를 직접 지명한다; 목록은 공유 모듈 한 벌이다"
    )

# 두 진입점이 각자 본문을 들고 있는 세 함수. 셋 다 `backupIfDifferent`의 답을 읽고
# 그 자리에서 원본을 덮는다.
_BACKUP_CONSUMERS = ("upgradeManagedHooks", "upgradeBundledProfiles", "installOmpHooks")


def _function_body(source: str, name: str) -> str:
    start = re.search(rf"^function {re.escape(name)}\(", source, re.M)
    assert start is not None, f"{name}()를 찾지 못했다"
    end = source.index("\n}\n", start.start())
    return source[start.start():end]


@pytest.mark.parametrize("name", _BACKUP_CONSUMERS)
def test_both_entry_points_read_the_backup_verdict_the_same_way(name: str):
    """불변: `backupIfDifferent`는 "덮어도 되는가"를 답한다. 한쪽 진입점만 그 답을

    읽으면 어느 CLI로 깔았는지에 따라 사본 없는 덮어쓰기가 갈린다 - 실측: 두 진입점이
    답을 falsy 하나로 읽던 동안, 사본 자리가 고갈된 프로젝트에서 사용자가 고친 hook이
    둘 다에서 사본 없이 사라졌다. 계약을 소비하는 줄이 두 파일에서 같은지 본다."""
    guards = {}
    for entry in ("agent-flow-kit.mjs", "agent-flow-install.mjs"):
        body = _function_body((BIN / entry).read_text(encoding="utf-8"), name)
        guards[entry] = [
            line.strip()
            for line in body.splitlines()
            if "backupIfDifferent(" in line or "safeToWrite" in line
        ]
        assert guards[entry], f"{entry}의 {name}()가 사본 판정을 읽지 않는다"
        assert any("safeToWrite" in line for line in guards[entry]), (
            f"{entry}의 {name}()가 사본을 못 남긴 경우를 구분하지 않고 덮는다"
        )
    assert len(set(map(tuple, guards.values()))) == 1, (
        f"{name}()가 두 진입점에서 사본 판정을 다르게 읽는다: {guards}"
    )

# 두 JS 진입점이 공유해야 하는 helper. 사본이 다시 생기면 여기서 걸린다.
_SHARED_ONLY = (
    "hookScriptCommand",
    "isPruneBackupName", "writePruneBackup", "managedHookScriptName",
    "managedHookDigests", "codexConfigPath", "ompExtensionIsKitOwned",
    "removeOmpHooksExtension", "safeSkillName",
    "readJsonIfExists", "retiredHookScripts", "isRetiredHookCommand",
    "pruneRetiredHooks", "pruneRetiredHookScripts", "mergeHookSettings",
    "mergeHookConfig", "claudeHooksSettings", "codexHooksSettings",
    "skillIndexBlock", "upsertSkillIndexBlock",
    "docsIndexBlock", "upsertDocsIndexBlock", "upsertManagedSubBlock",
    "extractCliOption", "cliOptionValue", "requestedInstallRootOption",
    "withoutInstallRootOption", "assertInstallRootIsFinal", "upgradeBundledSkills",
    "preserveKitSkillHashes", "syncKitAssets", "syncKitAsset", "syncRecordedKitAssets",
    "readKitAssetRecord", "writeKitAssetRecord",
    "isBundledSkillManifest", "isManagedHookScript", "isRecordedKitAsset",
    "pathHasSymlink",
    "reportSkippedUserEdit",
    # `samePath`/`gitEnv`/`resolveInstallRoot`는 뺐다. `lib/omp-hooks-extension.mjs`가
    # 생성물 안에 같은 이름을 들고 있어 이 검사로는 셀 수 없다.
    "canonicalPath", "gitOutput",
    "resolveManagedWorktreeContext", "resolveManagedWorktreeRoot",
    "resolveGitCommonWorktreeRoot", "resolveLinkedWorktreeLeader",
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
    for name in ("retiredHookScripts", "pruneRetiredHooks", "pruneRetiredHookScripts",
                 "mergeHookSettings", "mergeHookConfig", "isRetiredHookCommand"):
        signature = re.search(rf"^export function {name}\(([^)]*)\)", shared, re.M)
        assert signature is not None, name
        assert "hooksDisabled" in signature.group(1), name
        assert "hooksDisabled =" not in signature.group(1), name




def _frontmatter_parse(text: str) -> dict:
    """설치 경로가 실제로 쓰는 파서로 파싱한다. 텍스트 검사가 아니라 동작 검사다."""
    module = KIT_ROOT / "lib" / "frontmatter.mjs"
    return json.loads(
        subprocess.run(
            (
                _node(),
                "--input-type=module",
                "-e",
                "import { parseSimpleYaml } from "
                f"{json.dumps(str(module))};"
                "let raw = '';"
                "process.stdin.on('data', (chunk) => { raw += chunk; });"
                "process.stdin.on('end', () => "
                "process.stdout.write(JSON.stringify(parseSimpleYaml(raw))));",
            ),
            input=text,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        ).stdout
    )


def test_frontmatter_parser_is_single_source():
    """반증: 파서가 세 벌이면 한쪽만 고쳐도 index와 선택 로직이 갈라진다."""
    definers = [
        name
        for name, text in _js_sources().items()
        if "function parseSimpleYaml(" in text
    ]
    assert definers == ["lib/frontmatter.mjs"], (
        f"parseSimpleYaml()을 정의하는 파일이 하나가 아니다: {definers}"
    )

def test_skill_metadata_parser_is_single_source():
    definers = [
        name
        for name, text in _js_sources().items()
        if "function parseSkillMetadata(" in text
    ]
    assert definers == ["lib/skill-metadata.mjs"], (
        f"parseSkillMetadata()을 정의하는 파일이 하나가 아니다: {definers}"
    )


def _skill_metadata_parse(text: str, source: str = "") -> dict:
    module = KIT_ROOT / "lib" / "skill-metadata.mjs"
    return json.loads(
        subprocess.run(
            (
                _node(),
                "--input-type=module",
                "-e",
                "import { parseSkillMetadata } from "
                f"{json.dumps(str(module))};"
                "let raw = '';"
                "process.stdin.on('data', (chunk) => { raw += chunk; });"
                "process.stdin.on('end', () => process.stdout.write(JSON.stringify("
                "parseSkillMetadata(raw, 'fallback', ['claude', 'codex', 'omp'], "
                "process.argv[1]))));",
                source,
            ),
            input=text,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        ).stdout
    )


def test_skill_governance_metadata_matches_python():
    text = (
        "---\nname: governed\ndescription: Governed skill.\nversion: 1.2.3\n"
        "owner: platform\nlifecycle: active\napproval: approved\n"
        "provenance: internal\n---\n"
    )

    parsed = _skill_metadata_parse(text)
    expected = yaml.safe_load(text.split("---\n", 2)[1])

    assert parsed["governance"] == {
        "version": expected["version"],
        "owner": expected["owner"],
        "lifecycle": expected["lifecycle"],
        "approval": expected["approval"],
        "provenance": expected["provenance"],
    }

def test_skill_governance_defaults_match_python_catalog(tmp_path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.core.skill_resolver import SkillRoot, discover_skill_catalog

    text = "---\nname: governed\ndescription: Governed skill.\n---\n"
    path = tmp_path / "governed" / "SKILL.md"
    path.parent.mkdir()
    path.write_text(text, encoding="utf-8")
    entry = discover_skill_catalog(
        tmp_path,
        (SkillRoot("project-local", str(tmp_path / "{skill}" / "SKILL.md")),),
    )[0]

    assert _skill_metadata_parse(text, "local")["governance"] == {
        "version": entry.version,
        "owner": entry.owner,
        "lifecycle": entry.lifecycle,
        "approval": entry.approval,
        "provenance": entry.provenance,
    }


@pytest.mark.parametrize("field", ["excludes", "conflicts"])
def test_skill_metadata_preserves_exclusion_aliases(field):
    text = (
        "---\nname: governed\ndescription: Governed skill.\n"
        f"{field}: [legacy-one, legacy-two]\n---\n"
    )

    assert _skill_metadata_parse(text)["excludes"] == ["legacy-one", "legacy-two"]


def test_skill_governance_scalar_coercion_matches_python_catalog(tmp_path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.core.skill_resolver import SkillRoot, discover_skill_catalog

    text = (
        "---\nname: governed\ndescription: Governed skill.\n"
        "version: 2.10\napproval: no\n---\n"
    )
    path = tmp_path / "governed" / "SKILL.md"
    path.parent.mkdir()
    path.write_text(text, encoding="utf-8")
    entry = discover_skill_catalog(
        tmp_path,
        (SkillRoot("project", str(tmp_path / "{skill}" / "SKILL.md")),),
    )[0]
    governance = _skill_metadata_parse(text, "project")["governance"]

    assert governance["version"] == entry.version == "2.10"
    assert governance["approval"] == entry.approval == "no"


@pytest.mark.parametrize(
    "scalar_lines",
    [
        "version:\nowner:\nlifecycle:\napproval:\nprovenance:\n",
        "version: ' '\nowner: ' '\nlifecycle: ' '\napproval: ' '\nprovenance: ' '\n",
    ],
    ids=["empty", "quoted-whitespace"],
)
def test_blank_skill_governance_scalars_match_python_defaults(
    tmp_path, scalar_lines
):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.core.skill_resolver import SkillRoot, discover_skill_catalog

    text = (
        "---\nname: governed\ndescription: Governed skill.\n"
        f"{scalar_lines}---\n"
    )
    path = tmp_path / "governed" / "SKILL.md"
    path.parent.mkdir()
    path.write_text(text, encoding="utf-8")
    entry = discover_skill_catalog(
        tmp_path,
        (SkillRoot("project", str(tmp_path / "{skill}" / "SKILL.md")),),
    )[0]

    assert _skill_metadata_parse(text, "project")["governance"] == {
        "version": entry.version,
        "owner": entry.owner,
        "lifecycle": entry.lifecycle,
        "approval": entry.approval,
        "provenance": entry.provenance,
    }


def test_structured_skill_governance_stays_invalid_across_parsers(tmp_path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.core.skill_resolver import (
        INVALID_GOVERNANCE_SCALAR,
        SkillRoot,
        discover_skill_catalog,
    )

    text = (
        "---\nname: governed\ndescription: Governed skill.\n"
        "version:\n  - 1.2.3\nowner: [platform]\n"
        "lifecycle:\n  status: active\napproval: [approved]\n"
        "provenance:\n  source: internal\n---\n"
    )
    path = tmp_path / "governed" / "SKILL.md"
    path.parent.mkdir()
    path.write_text(text, encoding="utf-8")
    entry = discover_skill_catalog(
        tmp_path,
        (SkillRoot("project", str(tmp_path / "{skill}" / "SKILL.md")),),
    )[0]
    governance = _skill_metadata_parse(text, "project")["governance"]

    assert set(governance.values()) == {INVALID_GOVERNANCE_SCALAR}
    assert {
        entry.version,
        entry.owner,
        entry.lifecycle,
        entry.approval,
        entry.provenance,
    } == {INVALID_GOVERNANCE_SCALAR}



@pytest.mark.parametrize(
    ("governance_yaml", "expected"),
    (
        ("approval: approved\napproval: [rejected]\n", "<invalid-structured-value>"),
        ("approval: [rejected]\napproval: approved\n", "approved"),
    ),
)
def test_duplicate_governance_keys_use_final_value_across_parsers(
    governance_yaml, expected, tmp_path
):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.core.skill_resolver import SkillRoot, discover_skill_catalog

    text = (
        "---\nname: governed\ndescription: Governed skill.\n"
        f"{governance_yaml}---\n"
    )
    root = tmp_path / "skills"
    path = root / "governed" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    entry = discover_skill_catalog(
        tmp_path,
        (SkillRoot("project", str(root / "{skill}" / "SKILL.md")),),
    )[0]

    assert _skill_metadata_parse(text, "project")["governance"]["approval"] == expected
    assert entry.approval == expected

def test_governance_comment_only_lines_match_python(tmp_path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.core.skill_resolver import (
        INVALID_GOVERNANCE_SCALAR,
        SkillRoot,
        discover_skill_catalog,
    )

    cases = (
        (
            "lifecycle:\n  # keep the default\n# still blank\napproval: approved\n",
            "active",
        ),
        (
            "lifecycle: # mapping follows\n# explanation\n  status: active\n",
            INVALID_GOVERNANCE_SCALAR,
        ),
    )
    for index, (governance_yaml, expected) in enumerate(cases):
        text = (
            "---\nname: governed\ndescription: Governed skill.\n"
            f"{governance_yaml}---\n"
        )
        root = tmp_path / str(index)
        path = root / "governed" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
        entry = discover_skill_catalog(
            root,
            (SkillRoot("project", str(root / "{skill}" / "SKILL.md")),),
        )[0]

        assert _skill_metadata_parse(text, "project")["governance"]["lifecycle"] == expected
        assert entry.lifecycle == expected


def test_skill_observed_content_digest_matches_python_and_tracks_references(tmp_path):
    sys.path.insert(0, str(KIT_ROOT / "src"))
    from agent_flow.core.skill_resolver import skill_observed_content_digest

    skill = tmp_path / "governed"
    references = skill / "references"
    references.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: governed\n---\n", encoding="utf-8")
    reference = references / "contract.md"
    reference.write_text("first\n", encoding="utf-8")
    (skill / "references.md").write_text("sibling\n", encoding="utf-8")
    module = KIT_ROOT / "lib" / "skill-metadata.mjs"

    def js_digest() -> str:
        return subprocess.run(
            (
                _node(),
                "--input-type=module",
                "-e",
                "import { skillObservedContentDigest } from "
                f"{json.dumps(str(module))};"
                "process.stdout.write(skillObservedContentDigest(process.argv[1]));",
                str(skill),
            ),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        ).stdout

    first = skill_observed_content_digest(skill)
    assert js_digest() == first

    reference.write_text("second\n", encoding="utf-8")
    second = skill_observed_content_digest(skill)

    assert second != first
    assert js_digest() == second

    external_a = tmp_path / "external-a.md"
    external_b = tmp_path / "external-b.md"
    external_a.write_text("outside\n", encoding="utf-8")
    external_b.write_text("outside\n", encoding="utf-8")
    linked = references / "linked.md"
    linked.symlink_to(external_a)
    linked_first = skill_observed_content_digest(skill)
    assert js_digest() == linked_first

    linked.unlink()
    linked.symlink_to(external_b)
    linked_second = skill_observed_content_digest(skill)

    assert linked_second != linked_first
    assert js_digest() == linked_second
    external_b.write_text("changed outside\n", encoding="utf-8")
    assert skill_observed_content_digest(skill) == linked_second
    assert js_digest() == linked_second


def test_skill_content_observation_reports_read_failure_without_aborting(tmp_path):
    module = KIT_ROOT / "lib" / "skill-metadata.mjs"
    missing = tmp_path / "missing-skill"
    observed = json.loads(
        subprocess.run(
            (
                _node(),
                "--input-type=module",
                "-e",
                "import { observeSkillContent } from "
                f"{json.dumps(str(module))};"
                "process.stdout.write(JSON.stringify("
                "observeSkillContent(process.argv[1])));",
                str(missing),
            ),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        ).stdout
    )

    assert observed["digest"] == ""
    assert observed["warning"].startswith("content digest unavailable:")



def test_frontmatter_folded_scalar_matches_python():
    """반증: `description: >`가 `\">\"` 한 글자로 기록되면 JS와 Python이 갈라진다."""
    text = "name: x\ndescription: >\n  Line one continues\n  here and here.\n  Second sentence.\n"
    assert _frontmatter_parse(text)["description"] == yaml.safe_load(text)["description"]


def test_frontmatter_literal_scalar_matches_python():
    text = "name: y\ndescription: |\n  Line one\n  Line two\n"
    assert _frontmatter_parse(text)["description"] == yaml.safe_load(text)["description"]


def test_frontmatter_plain_scalar_and_lists_unchanged():
    """반증: block scalar를 붙이며 기존 단일 행/리스트 파싱이 깨지면 설치가 조용히 바뀐다."""
    text = (
        "name: z\n"
        "description: one line\n"
        "requires:\n"
        "  - alpha\n"
        "  - beta\n"
        "tags: [a, b]\n"
    )
    parsed = _frontmatter_parse(text)
    assert parsed["name"] == "z"
    assert parsed["description"] == "one line"
    assert parsed["requires"] == ["alpha", "beta"]
    assert parsed["tags"] == ["a", "b"]


def test_installed_skill_descriptions_match_python():
    """반증: 실제 배포 skill 중 block scalar를 쓰는 것이 index에 잘못 기록된다."""
    mismatches = []
    for skill in sorted((KIT_ROOT / "skills").glob("*/SKILL.md")):
        text = skill.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            continue
        end = text.index("\n---", 4)
        frontmatter = text[4:end + 1]
        expected = yaml.safe_load(frontmatter) or {}
        parsed = _frontmatter_parse(frontmatter)
        if str(expected.get("description", "")) != str(parsed.get("description", "")):
            mismatches.append(skill.parent.name)
    assert mismatches == [], f"JS/Python description이 갈린 skill: {mismatches}"


def test_frontmatter_crlf_summary_matches_python():
    """반증: CRLF 파일에서 JS만 summary가 비면 index와 프롬프트가 다시 갈린다."""
    module = KIT_ROOT / "lib" / "frontmatter.mjs"
    text = "---\r\nname: custom\r\ndescription: First sentence. Second sentence.\r\n---\r\n\r\n# custom\r\n"
    summary = subprocess.run(
        (
            _node(),
            "--input-type=module",
            "-e",
            "import { skillSummaryFromMarkdown } from "
            f"{json.dumps(str(module))};"
            "let raw = '';"
            "process.stdin.on('data', (chunk) => { raw += chunk; });"
            "process.stdin.on('end', () => process.stdout.write(skillSummaryFromMarkdown(raw)));",
        ),
        input=text,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    ).stdout
    frontmatter = text.split("\r\n---", 1)[0][4:]
    assert summary == yaml.safe_load(frontmatter)["description"].split(". ")[0] + "."


def test_frontmatter_splitter_is_single_source():
    """반증: 분리기가 여러 벌이면 CRLF 같은 입력에서 진입점마다 다른 metadata를 본다."""
    definers = [
        name
        for name, text in _js_sources().items()
        if "function splitSkillFrontmatter(" in text or "export function splitFrontmatter(" in text
    ]
    assert definers == ["lib/frontmatter.mjs"], (
        f"frontmatter 분리기를 정의하는 파일이 하나가 아니다: {definers}"
    )


def test_crlf_skill_metadata_matches_python():
    """반증: CRLF 파일에서 name/description/requires가 비면 설치 선택과 index가 갈린다."""
    text = (
        "---\r\nname: crlf-skill\r\ndescription: First sentence. Second sentence.\r\n"
        "requires:\r\n  - other-skill\r\n---\r\n\r\n# crlf-skill\r\n"
    )
    module = KIT_ROOT / "lib" / "frontmatter.mjs"
    parsed = json.loads(
        subprocess.run(
            (
                _node(),
                "--input-type=module",
                "-e",
                "import { parseSimpleYaml, splitFrontmatter } from "
                f"{json.dumps(str(module))};"
                "let raw = '';"
                "process.stdin.on('data', (chunk) => { raw += chunk; });"
                "process.stdin.on('end', () => process.stdout.write("
                "JSON.stringify(parseSimpleYaml(splitFrontmatter(raw) ?? ''))));",
            ),
            input=text,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        ).stdout
    )
    expected = yaml.safe_load(text.split("\r\n---", 1)[0][4:])
    assert parsed["name"] == expected["name"]
    assert parsed["description"] == expected["description"]
    assert parsed["requires"] == expected["requires"]


def test_frontmatter_block_scalar_shapes_match_python():
    """반증: 접기·chomping·more-indented 처리가 PyYAML과 갈리면 같은 SKILL.md가 두 값이 된다."""
    module = KIT_ROOT / "lib" / "frontmatter.mjs"
    shapes = (
        "d: >\n  a\n\n  b\n",
        "d: >\n  a\n\n\n  b\n",
        "d: >-\n  a\n  b\n",
        "d: |+\n  a\n\n",
        "d: |\n  a\n\n  b\n",
        "d: >\n  a\n    indented\n  b\n",
        "d: >\n  a\n    i1\n    i2\n  b\n",
        "d: >\n  a\n\n    ind\n  b\n",
        "d: >\n    only\n",
        "d: |\n",
    )
    mismatches = []
    for text in shapes:
        parsed = json.loads(
            subprocess.run(
                (
                    _node(),
                    "--input-type=module",
                    "-e",
                    "import { parseSimpleYaml } from "
                    f"{json.dumps(str(module))};"
                    "let raw = '';"
                    "process.stdin.on('data', (chunk) => { raw += chunk; });"
                    "process.stdin.on('end', () => "
                    "process.stdout.write(JSON.stringify(parseSimpleYaml(raw).d)));",
                ),
                input=text,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            ).stdout
        )
        expected = yaml.safe_load(text)["d"]
        if parsed != expected:
            mismatches.append((text, parsed, expected))
    assert mismatches == []
