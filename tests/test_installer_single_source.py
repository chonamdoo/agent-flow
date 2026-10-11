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
import runpy
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


def _resolved_hook_dir(root: Path, source: str, *, home: Path | None = None) -> Path:
    target = _install_extension(root, source, "\nexport const __HOOK_DIR = HOOK_DIR;\n")
    env = None if home is None else {**os.environ, "HOME": str(home)}
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
            env=env,
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
    events: list[dict[str, object]] | None = None,
    context_cwd: Path | None = None,
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
        "payload['_hook'] = sys.argv[1] if len(sys.argv) > 1 else Path(__file__).name\n"
        "payload['_spawn_cwd'] = str(Path.cwd())\n"
        f"with Path({str(binding_log)!r}).open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(payload) + '\\n')\n"
    )
    (hooks / "bind-host-worktree.py").write_text(binding_recorder, encoding="utf-8")
    for script_name in (
        "record-skill-read.py",
        "worktree-tripwire.py",
    ):
        (hooks / script_name).write_text(
            binding_recorder if script_name == "worktree-tripwire.py" else "pass\n",
            encoding="utf-8",
        )

    target = _install_extension(root, source)
    default_events = [
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
    if events is None:
        events = default_events
    driver = (
        f"import ext from {json.dumps(str(target))};\n"
        "const handlers = {};\n"
        "const pi = { setLabel() {}, on(name, fn) { (handlers[name] = handlers[name] || []).push(fn); } };\n"
        "ext(pi);\n"
        f"const events = {json.dumps(events)};\n"
        f"const ctx = {{ cwd: {json.dumps(str(context_cwd or root))}, sessionManager: {{ getSessionId() {{ return 'session-1'; }} }} }};\n"
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
        cwd=root.parent,
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
    return command_events, [
        event for event in binding_events if event["_hook"] == "bind-host-worktree.py"
    ]



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


@pytest.mark.parametrize("layout", ("managed", "legacy", "manual"))
@pytest.mark.parametrize("plant", ("checkout", "subdirectory"))
def test_omp_extension_prefers_the_leader_install_over_a_worktree_copy(
    tmp_path: Path, layout: str, plant: str
):
    """반증: linked worktree에 `.agent-flow/kit.json` 사본이 있으면 OMP가 그 사본의
    hook을 돌린다. 위조본이면 leader 쓰기 guard와 tripwire가 사본 스크립트로 바뀌고,
    Python run 시작 검사는 leader 설치만 보므로 이것을 못 본다.

    `find_install_root`와 같은 답이어야 두 구현이 같은 설치본의 hook을 실행한다.
    """
    from agent_flow.core.hook_integrity import find_install_root

    source = _extension_source()
    root = tmp_path.resolve()
    home = root / "home"
    home.mkdir()
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
    checkout = {
        "managed": home / ".agent-flow" / "worktrees" / "repo-id" / "w1",
        "legacy": leader / ".agent-flow" / "worktrees" / "w1",
        "manual": root / "manual-wt",
    }[layout]
    _git(leader, "worktree", "add", "-q", "-b", "w1", str(checkout))

    if plant == "checkout":
        session_root = checkout
        _seed_install(checkout)
    else:
        session_root = checkout / "pkg" / "app"
        session_root.mkdir(parents=True)
        _seed_install(checkout / "pkg")

    assert _resolved_hook_dir(session_root, source, home=home) == leader_hooks
    assert find_install_root(session_root) == leader


def _omp_install_root(
    start: Path, source: str, *, home: Path, preload: Path | None = None
) -> Path | None:
    """실제로 선택된 설치본. 진단 메시지용 HOOK_DIR fallback과 구분한다."""
    target = _install_extension(
        start,
        source,
        "\nexport const __INSTALL_ROOT = INSTALL_ROOT_REASON ? null : INSTALL_ROOT;\n",
    )
    node_args = (_node(),) if preload is None else (_node(), "--require", str(preload))
    result = subprocess.run(
        (
            *node_args,
            "--input-type=module",
            "-e",
            f"import({json.dumps(str(target))})"
            ".then((m) => process.stdout.write(JSON.stringify(m.__INSTALL_ROOT)));",
        ),
        cwd=start,
        env={**os.environ, "HOME": str(home)},
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    chosen = json.loads(result.stdout)
    return Path(chosen) if chosen is not None else None


def _observation_hook_roots(start: Path) -> dict[str, Path | None]:
    return {
        name: runpy.run_path(str(KIT_ROOT / "scripts" / "hooks" / name))[
            "find_project_root"
        ](start)
        for name in ("record-command-run.py", "record-skill-read.py")
    }


def _seed_cli_runtime(root: Path) -> None:
    """CLI가 선택한 runtime package가 자기 프로젝트 루트를 출력하게 한다."""
    package = root / ".agent-flow" / "runtime" / "python" / "agent_flow"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(
        "from pathlib import Path\nprint(Path(__file__).resolve().parents[4])\n",
        encoding="utf-8",
    )


def _node_project_root(
    start: Path, *, home: Path, preload: Path | None = None
) -> Path:
    node_args = (_node(),) if preload is None else (_node(), "--require", str(preload))
    result = subprocess.run(
        (*node_args, str(BIN / "agent-flow-kit.mjs"), "discovery-probe"),
        cwd=start,
        env={**os.environ, "HOME": str(home), "PYTHON": sys.executable},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return Path(result.stdout.strip())


def test_node_project_root_discovery_stops_at_home_or_start(tmp_path: Path):
    """public CLI는 git common root를 유지하고 git 밖 설치 탐색에는 경계를 둔다."""
    root = tmp_path.resolve()
    home = root / "home"
    _seed_install(home)
    loose = home / "loose"
    loose.mkdir()
    nearby = home / "projects"
    _seed_install(nearby)
    nearby_start = nearby / "app"
    nearby_start.mkdir()
    foreign = root / "foreign"
    _seed_install(foreign)
    foreign_start = foreign / "child"
    foreign_start.mkdir()
    marker_start = foreign / ".agent-flow" / "scratch"
    marker_start.mkdir()
    home_marker_start = home / ".agent-flow" / "scratch"
    home_marker_start.mkdir()

    repository = root / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(
        repository,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    nested_install = repository / "pkg"
    _seed_install(nested_install)
    repository_start = nested_install / "app"
    repository_start.mkdir()
    linked = root / "manual-wt"
    _git(repository, "worktree", "add", "-q", "-b", "w1", str(linked))
    _seed_install(linked)

    cases = (
        ("HOME 설치는 하위 프로젝트의 runtime이 아니다", loose, loose),
        ("HOME 안 가까운 설치", nearby_start, nearby),
        ("HOME 밖 시작점 fallback", foreign_start, foreign_start),
        ("HOME 밖 marker에도 같은 경계", marker_start, marker_start),
        ("HOME marker도 HOME 설치를 선택하지 않는다", home_marker_start, home_marker_start),
        ("HOME에서 직접 시작", home, home),
        ("git 안은 가까운 kit보다 common root", repository_start, repository),
        ("linked는 kit가 없는 common root도 유지", linked, repository),
    )
    for candidate in {home, nearby, foreign, nested_install, linked, repository}:
        _seed_cli_runtime(candidate)
    for _, start, _ in cases:
        _seed_cli_runtime(start)
    for label, start, expected in cases:
        assert _node_project_root(start, home=home) == expected, label


@pytest.mark.parametrize("layout", ("managed", "legacy", "manual", "home-leader"))
def test_linked_worktree_without_leader_install_ignores_all_local_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
):
    """leader가 설치되지 않은 linked checkout은 worker/container 사본을 실행하지 않는다.

    HOME인 leader도 설치 후보가 아니다. 정상 HOME 시작점 예외가 linked checkout의
    worker 사본까지 허용하는 것으로 확대되면 안 된다.
    """
    from agent_flow.core.hook_integrity import find_install_root

    root = tmp_path.resolve()
    home = root / "home"
    home.mkdir()
    leader = home if layout == "home-leader" else root / "leader"
    leader.mkdir(exist_ok=True)
    _git(leader, "init", "-q")
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
        "--allow-empty",
        "-m",
        "init",
    )
    checkout = {
        "managed": home / ".agent-flow" / "worktrees" / "repo-id" / "w1",
        "legacy": leader / ".agent-flow" / "worktrees" / "w1",
        "manual": root / "manual-wt",
        "home-leader": root / "home-worktrees" / "w1",
    }[layout]
    _git(leader, "worktree", "add", "-q", "-b", "w1", str(checkout))
    for planted in (checkout.parent, checkout, checkout / "pkg"):
        hooks = _seed_install(planted)
        for name in ("guard-protected-branch.sh", "guard-host-worktree.sh"):
            (hooks / name).write_text('echo "worker copy ran" >&2\nexit 1\n', encoding="utf-8")
            (hooks / name).chmod(0o755)
    if layout == "home-leader":
        _seed_install(home)
    nested = checkout / "pkg" / "app"
    nested.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source = _extension_source()

    for start in (checkout, nested):
        assert find_install_root(start) is None, f"{layout}: Python chose a worker copy"
        assert _omp_install_root(start, source, home=home) is None, (
            f"{layout}: OMP chose a worker copy"
        )
        for hook, chosen in _observation_hook_roots(start).items():
            assert chosen is None, f"{layout}: {hook} chose a worker copy"
        stdout, _ = _run_bash_tool_call(start, source)
        assert json.loads(stdout) is None, f"{layout}: OMP ran a worker hook"


def test_install_resolvers_compare_directory_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """casing을 보존하는 filesystem에서 stat 동일성으로 HOME/git 경계를 지킨다.

    Linux에서는 filesystem seam만 모사한다. git은 실제 저장소를 조회하고 common-dir의
    표기만 바꿔 반환한다. 이 결과를 실제 macOS filesystem 실행으로 보고하지 않는다.
    """
    from agent_flow.core.hook_integrity import find_install_root

    root = tmp_path.resolve()
    home = root / "home"
    _seed_install(home)
    projects = home / "projects"
    _seed_install(projects)
    app = projects / "app"
    app.mkdir()
    for candidate in (home, projects, app):
        _seed_cli_runtime(candidate)

    def repository(name: str) -> Path:
        candidate = root / name
        candidate.mkdir()
        _git(candidate, "init", "-q")
        _git(
            candidate,
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        )
        _seed_install(candidate)
        return candidate

    repo = repository("repo")
    _seed_install(repo / "pkg")
    repo_app = repo / "pkg" / "app"
    repo_app.mkdir()
    repo_home = repository("repo-home")
    repo_home_src = repo_home / "src"
    repo_home_src.mkdir()
    home_checkout = root / "home-worktree"
    _git(repo_home, "worktree", "add", "-q", "-b", "w1", str(home_checkout))
    _seed_install(home_checkout)
    source = _extension_source()
    aliases = {
        str(root / "HOME"): str(home),
        str(root / "REPO"): str(repo),
        str(root / "REPO-HOME"): str(repo_home),
    }

    def disk_path(value):
        if isinstance(value, int):
            return value
        raw = os.fspath(value)
        for alias, disk in aliases.items():
            if raw == alias or raw.startswith(alias + os.sep):
                return disk + raw[len(alias):]
        return value

    real_stat = os.stat
    real_realpath = os.path.realpath

    def alias_stat(value, *args, **kwargs):
        return real_stat(disk_path(value), *args, **kwargs)

    def alias_realpath(value, *args, **kwargs):
        raw = os.fspath(value)
        resolved = real_realpath(disk_path(value), *args, **kwargs)
        for alias, disk in aliases.items():
            if raw == alias or raw.startswith(alias + os.sep):
                return alias + resolved[len(disk):]
        return resolved

    preload = root / "case-preserving-filesystem.cjs"
    preload.write_text(
        'const fs = require("node:fs");\n'
        f"const aliases = {json.dumps(aliases)};\n"
        "function diskPath(value) {\n"
        "  if (typeof value !== 'string') return value;\n"
        "  for (const [alias, disk] of Object.entries(aliases)) {\n"
        "    if (value === alias || value.startsWith(alias + '/')) return disk + value.slice(alias.length);\n"
        "  }\n"
        "  return value;\n"
        "}\n"
        "const stat = fs.statSync, exists = fs.existsSync, realpath = fs.realpathSync;\n"
        "fs.statSync = (value, ...args) => stat(diskPath(value), ...args);\n"
        "fs.existsSync = (value) => exists(diskPath(value));\n"
        "fs.realpathSync = (value, ...args) => {\n"
        "  const resolved = realpath(diskPath(value), ...args);\n"
        "  for (const [alias, disk] of Object.entries(aliases)) {\n"
        "    if (value === alias || value.startsWith(alias + '/')) return alias + resolved.slice(disk.length);\n"
        "  }\n"
        "  return resolved;\n"
        "};\n"
        "fs.realpathSync.native = fs.realpathSync;\n",
        encoding="utf-8",
    )
    git = shutil.which("git")
    assert git is not None
    tools_dir = root / "tools"
    tools_dir.mkdir()
    git_wrapper = tools_dir / "git"
    git_wrapper.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\nfrom pathlib import Path\n"
        f"aliases = {aliases!r}\n"
        f"result = subprocess.run([{git!r}, *sys.argv[1:]], capture_output=True)\n"
        "output = result.stdout\n"
        "if result.returncode == 0 and '--git-common-dir' in sys.argv:\n"
        "    lines = output.decode().splitlines()\n"
        "    if lines:\n"
        "        common = (Path.cwd() / lines[0]).resolve()\n"
        "        for alias, disk in aliases.items():\n"
        "            if common == Path(disk) / '.git':\n"
        "                lines[0] = str(Path(alias) / '.git')\n"
        "        output = ('\\n'.join(lines) + '\\n').encode()\n"
        "sys.stdout.buffer.write(output)\nsys.stderr.buffer.write(result.stderr)\n"
        "sys.exit(result.returncode)\n",
        encoding="utf-8",
    )
    git_wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(os, "stat", alias_stat)
    monkeypatch.setattr(os.path, "realpath", alias_realpath)

    cases = (
        ("HOME 별칭 아래 가까운 설치", app, root / "HOME", projects),
        ("정상 checkout을 linked로 오인하지 않는다", repo_app, home, repo / "pkg"),
        ("git common-root 별칭도 HOME 제외", repo_home_src, repo_home, None),
        ("linked leader의 HOME 별칭도 제외", home_checkout, repo_home, None),
    )
    for label, start, case_home, expected in cases:
        monkeypatch.setenv("HOME", str(case_home))
        choices = {
            "Python": find_install_root(start),
            "OMP": _omp_install_root(start, source, home=case_home, preload=preload),
            **_observation_hook_roots(start),
        }
        for resolver, chosen in choices.items():
            if expected is None:
                assert chosen is None, f"{label}: {resolver}"
            else:
                assert chosen is not None and os.path.samefile(chosen, expected), (
                    f"{label}: {resolver} chose {chosen}, expected {expected}"
                )
    assert _node_project_root(app, home=root / "HOME", preload=preload) == projects


def test_python_install_root_matches_the_omp_resolver_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """반증: Python과 OMP가 다른 설치본을 고르면 run 시작 검사가 증명한 hook과 OMP가
    실행하는 hook이 갈린다. 조상 탐색이 경계 없이 올라가면 `~/.agent-flow/kit.json`
    하나가 HOME 아래 모든 프로젝트의 설치본이 된다.

    기대값을 사례마다 적는다. 두 구현이 같은 오답을 내면 동일성만으로는 드러나지 않는다.
    """
    from agent_flow.core.hook_integrity import find_install_root

    source = _extension_source()
    root = tmp_path.resolve()

    def repository(path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        _git(path, "init", "-q")
        _git(
            path,
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        )
        return path

    home = root / "home"
    _seed_install(home)
    loose = home / "projects" / "loose"
    loose.mkdir(parents=True)

    foreign = root / "foreign"
    _seed_install(foreign)
    foreign_src = repository(foreign / "repo") / "src"
    foreign_src.mkdir()

    mono = repository(root / "mono")
    _seed_install(mono / "pkg")
    nested = mono / "pkg" / "app"
    nested.mkdir()

    outside = root / "outside"
    _seed_install(outside)
    outside_child = outside / "child"
    outside_child.mkdir()

    leader = repository(root / "leader")
    _seed_install(leader)
    manual = root / "manual-wt"
    _git(leader, "worktree", "add", "-q", "-b", "w1", str(manual))

    repo_home = repository(root / "repo-home")
    _seed_install(repo_home)
    repo_home_src = repo_home / "src"
    repo_home_src.mkdir()

    superproject = repository(home / "projects" / "super")
    _seed_install(superproject)
    library = repository(root / "library")
    _git(superproject, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(library), "sub")
    submodule = superproject / "sub"

    cases = (
        ("git 밖, HOME 아래: HOME 설치는 후보가 아니다", loose, home, None),
        ("HOME에서 바로 시작", home, home, home),
        ("저장소 밖 조상 설치", foreign_src, home, None),
        ("저장소 안 가까운 설치", nested, home, mono / "pkg"),
        ("git 밖, HOME 밖: 시작점만 본다", outside_child, home, None),
        ("leader 밖 수동 worktree", manual, home, leader),
        ("저장소가 HOME: 하위 폴더", repo_home_src, repo_home, None),
        ("저장소가 HOME: HOME에서 바로 시작", repo_home, repo_home, repo_home),
        ("HOME 아래 submodule은 상위 저장소 설치", submodule, home, superproject),
    )
    for label, start, case_home, expected in cases:
        monkeypatch.setenv("HOME", str(case_home))
        assert _omp_install_root(start, source, home=case_home) == expected, label
        assert find_install_root(start) == expected, label
        for hook, chosen in _observation_hook_roots(start).items():
            assert chosen == expected, f"{label}: {hook}"


def test_python_and_omp_agree_when_home_is_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """반증: HOME을 symlink 이름 그대로 실경로 시작점과 비교하면 HOME 아래가 HOME 밖으로
    보인다. 그러면 HOME 경계와 "HOME은 후보가 아니다" 규칙이 구현마다 다르게 걸려,
    run 시작 검사가 증명한 설치본과 OMP가 실행하는 hook의 설치본이 갈린다.
    """
    from agent_flow.core.hook_integrity import find_install_root

    source = _extension_source()
    root = tmp_path.resolve()

    real_home = root / "real-home"
    _seed_install(real_home / "projects")
    app = real_home / "projects" / "app"
    app.mkdir()
    home_link = root / "home-link"
    home_link.symlink_to(real_home, target_is_directory=True)

    real_repo_home = root / "real-repo-home"
    real_repo_home.mkdir()
    _git(real_repo_home, "init", "-q")
    _seed_install(real_repo_home)
    repo_src = real_repo_home / "src"
    repo_src.mkdir()
    repo_home_link = root / "repo-home-link"
    repo_home_link.symlink_to(real_repo_home, target_is_directory=True)

    cases = (
        ("git 밖, HOME 아래 설치", app, home_link, real_home / "projects"),
        ("저장소가 HOME: HOME 설치는 후보가 아니다", repo_src, repo_home_link, None),
    )
    for label, start, case_home, expected in cases:
        monkeypatch.setenv("HOME", str(case_home))
        assert _omp_install_root(start, source, home=case_home) == expected, label
        assert find_install_root(start) == expected, label
        for hook, chosen in _observation_hook_roots(start).items():
            assert chosen == expected, f"{label}: {hook}"


def test_observation_hooks_ignore_poisoned_git_discovery_env(tmp_path: Path):
    """ambient GIT_*가 관측 증거를 decoy 저장소에 쓰게 만들지 못한다."""
    root = tmp_path.resolve()
    home = root / "home"
    home.mkdir()
    actual = root / "actual"
    decoy = root / "decoy"
    for repository in (actual, decoy):
        repository.mkdir()
        _git(repository, "init", "-q")
        _seed_install(repository)
    start = actual / "src"
    start.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "GIT_DIR": str(decoy / ".git"),
        "GIT_WORK_TREE": str(decoy),
        "GIT_COMMON_DIR": str(decoy / ".git"),
    }
    cases = (
        (
            "record-command-run.py",
            "commands-run.jsonl",
            {"tool_name": "Bash", "tool_input": {"command": "echo boundary"}, "exit_code": 0},
            "command",
            "echo boundary",
        ),
        (
            "record-skill-read.py",
            "skills-read.jsonl",
            {"tool_name": "Skill", "tool_input": {"skill": "tdd"}},
            "skill",
            "tdd",
        ),
    )
    for hook, log, payload, key, expected in cases:
        result = subprocess.run(
            (sys.executable, str(KIT_ROOT / "scripts" / "hooks" / hook)),
            cwd=start,
            env=env,
            input=json.dumps({**payload, "cwd": str(start)}),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        records = (actual / ".agent-flow" / log).read_text(encoding="utf-8").splitlines()
        assert [json.loads(record)[key] for record in records] == [expected]
        assert not (decoy / ".agent-flow" / log).exists()


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


def test_omp_recorder_cwd_does_not_change_guard_context(tmp_path: Path):
    root = tmp_path
    session = root / "session"
    session.mkdir()
    bound = session / "bound directory"
    bound.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (session / "link").symlink_to(outside, target_is_directory=True)
    inputs = [str(bound), "link/../bound directory", "../outside"]
    events = []
    starts = []
    for cwd in inputs:
        resolved = cwd if os.path.isabs(cwd) else os.path.abspath(session / cwd)
        completed = subprocess.run(
            ("pwd", "-P"), cwd=resolved, capture_output=True, text=True, check=True
        )
        starts.append(completed.stdout.strip())
        events.append({
            "type": "tool_result",
            "toolName": "bash",
            "input": {"command": "pwd -P", "cwd": cwd},
            "content": [{"type": "text", "text": completed.stdout}],
            "details": {"exitCode": completed.returncode},
            "isError": False,
        })
    commands, _ = _run_command_result_handler(
        root, _extension_source(), events, context_cwd=session
    )
    assert [entry["cwd"] for entry in commands] == starts
    assert [entry["exit_code"] for entry in commands] == [0, 0, 0]
    guards = [
        json.loads(line)
        for line in (root / ".agent-flow" / "binding-events.jsonl").read_text().splitlines()
    ]
    for index, event in enumerate(events):
        received = guards[index * 2:index * 2 + 2]
        assert [entry.pop("_hook") for entry in received] == [
            "bind-host-worktree.py", "worktree-tripwire.py"
        ]
        assert [entry.pop("_spawn_cwd") for entry in received] == [str(session)] * 2
        assert received[0] == received[1]
        assert received[0]["cwd"] == str(session)
        assert received[0]["tool_input"] == event["input"]
        assert event["input"]["cwd"] == inputs[index]


def test_omp_recorder_keeps_unresolved_cwd_compatibility(tmp_path: Path):
    inputs = [None, "", "~/elsewhere", "file:///elsewhere", "local:/elsewhere",
              "@../elsewhere", ":/elsewhere", "/", "\u00a0elsewhere", "C:\\elsewhere"]
    events = []
    for cwd in inputs:
        tool_input = {"command": "cd elsewhere && pwd"}
        if cwd is not None:
            tool_input["cwd"] = cwd
        events.append({
            "type": "tool_result", "toolName": "bash", "input": tool_input,
            "content": [{"type": "text", "text": "unverified cwd"}],
            "details": {"exitCode": 7}, "isError": True,
        })
    commands, bindings = _run_command_result_handler(tmp_path, _extension_source(), events)
    assert [entry["cwd"] for entry in commands] == [str(tmp_path)] * len(inputs)
    assert [entry["exit_code"] for entry in commands] == [7] * len(inputs)
    assert [entry["tool_input"] for entry in bindings] == [event["input"] for event in events]


def _drive_extension(
    root: Path, target: Path, calls: list[tuple[str, dict[str, object]]]
) -> list[object]:
    driver = (
        f"import ext from {json.dumps(str(target))};\n"
        "const handlers = {};\n"
        "const pi = { setLabel() {}, on(name, fn) { (handlers[name] = handlers[name] || []).push(fn); } };\n"
        "ext(pi);\n"
        f"const calls = {json.dumps(calls)};\n"
        f"const ctx = {{ cwd: {json.dumps(str(root))}, sessionManager: {{ getSessionId() {{ return 'session-1'; }} }} }};\n"
        "const outputs = [];\n"
        "for (const [name, event] of calls) {\n"
        "  outputs.push((await handlers[name][0](event, ctx)) ?? null);\n"
        "}\n"
        "process.stdout.write(JSON.stringify(outputs));\n"
    )
    result = subprocess.run(
        (_node(), "--input-type=module", "-e", driver),
        cwd=root,
        env={**os.environ, "AGENT_FLOW_HOOK_PYTHON": sys.executable},
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, f"driver exited {result.returncode}: {result.stderr}"
    return json.loads(result.stdout)


def test_omp_post_tool_keeps_successful_run_output_and_pre_tool_still_blocks(
    tmp_path: Path,
):
    """반증: 성공한 run 직후 경계를 다시 판정하면, 그 run이 만든 active run과 binding이
    방금 끝난 명령을 위반으로 만든다. 도구 출력이 그 오류로 바뀌면 next_command가
    사라지고, 실패로 읽은 사용자가 run을 중복으로 만든다.
    """
    from agent_flow.artifact import create_run
    from agent_flow.core.host_write_boundary import bound_worktree_for_session
    from agent_flow.core.worktrees import create_worktree, plan_worktree, worktree_runtime_root
    from tests.test_host_write_boundary import _install_boundary_hooks

    root = tmp_path.resolve() / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    (root / ".gitignore").write_text(".agent-flow/\n.omp/\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false",
         "commit", "-q", "-m", "init")
    hooks = _install_boundary_hooks(root)
    (root / ".agent-flow" / "kit.json").write_text("{}", encoding="utf-8")
    for name in ("record-skill-read.py", "record-command-run.py", "worktree-tripwire.py"):
        (hooks / name).write_text("pass\n", encoding="utf-8")
    (hooks / "guard-protected-branch.sh").write_text("exit 0\n", encoding="utf-8")
    launcher = root / ".agent-flow" / "bin" / "agent-flow-hook"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'  *.py) exec {sys.executable} "$@" ;;\n'
        '  *) exec /bin/sh "$@" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    target = _install_extension(root, _extension_source())

    def bash(kind: str, command: str, output: str = "") -> tuple[str, dict[str, object]]:
        event: dict[str, object] = {"type": kind, "toolName": "bash", "input": {"command": command}}
        if kind == "tool_result":
            event.update(
                content=[{"type": "text", "text": output}],
                details={"wallTimeMs": 12},
                isError=False,
            )
        return kind, event

    # leader 경로가 리터럴로 들어간 run. 시작 전에는 active run이 없어 통과한다.
    chained = f"cd {root} && agent-flow run task --worktree first"
    assert _drive_extension(root, target, [bash("tool_call", chained)]) == [None]

    status = create_worktree(root=root, plan=plan_worktree(root=root, name="first"))
    run_dir = create_run(
        worktree_runtime_root(root=root, name=status.name),
        "default",
        "task-first",
        checkout_identity=f"worktree:{status.name}",
        checkout_registration_identity=status.registration_identity,
    )
    run_output = "status_json: " + json.dumps({
        "status": "awaiting_host",
        "run": f"default/{run_dir.name}",
        "next_command": f"agent-flow continue --root {root} --worktree {status.name}",
    })
    direct = f"agent-flow run task --root {root} --worktree {status.name}"
    outputs = _drive_extension(root, target, [
        bash("tool_result", chained, run_output),
        bash("tool_result", direct, run_output),
        bash("tool_call", direct),
        bash("tool_call", f"touch {root}/leaked.py"),
    ])

    assert outputs[:2] == [None, None], "성공한 run의 출력은 그대로 에이전트에게 가야 한다"
    binding = bound_worktree_for_session("session-1", root)
    assert binding is not None and binding.checkout.checkout == status.path.resolve()
    assert outputs[2]["block"] is True
    assert "refusing to start a new run" in outputs[2]["reason"]
    assert outputs[3]["block"] is True





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
    assert (project / ".agent-flow" / "kit.json").is_file()


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
