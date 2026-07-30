"""install 로직이 저장소에 한 벌만 있는지 본다.

`bin/agent-flow-install.mjs`와 `bin/agent-flow-kit.mjs`는 함수명 86개를 공유했고,
`ompHooksExtensionSource()`는 321줄이 바이트 동일하게 두 벌 박혀 있었다. 두 벌이면
한쪽만 고쳐도 절반만 반영되므로, 둘이 갈라지지 않았는지 보는 검사가 따로 필요해진다.
그 검사를 지우려면 사본부터 없어야 한다.
"""
from __future__ import annotations

import json
import re
import shutil
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
    ).stdout


def _git(cwd: Path, *args: str) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git을 찾을 수 없다")
    subprocess.run((git, *args), cwd=cwd, check=True, capture_output=True, text=True)


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
    )
    # 실패를 CalledProcessError로 흘리면 node가 남긴 사유가 리포트에서 사라진다.
    assert result.returncode == 0, f"driver exited {result.returncode}: {result.stderr}"
    return result.stdout, result.stderr


def _seed_install(root: Path) -> Path:
    hooks = root / ".agent-flow" / "scripts" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (root / ".agent-flow" / "kit.json").write_text("{}", encoding="utf-8")
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
    거기서 통과시키면 `rm` 한 번으로 그 세션의 승인·경계 가드가 전부 꺼진다.
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
        "guard-spec-approval.sh",
    ):
        script = hooks / name
        script.write_text('echo "denied by " >&2\nexit 1\n', encoding="utf-8")
        script.chmod(0o755)
    stdout, _ = _run_bash_tool_call(denying, source)
    assert json.loads(stdout) == {"block": True, "reason": "denied by"}, (
        "스크립트가 있는데 0이 아닌 종료면 가드는 그대로 막아야 한다"
    )


def test_omp_extension_reads_the_session_id_from_the_session_manager():
    """반증: 이 fallback을 지우면 session_id 없이 hook이 돌아 승인이 조용히 무시된다.

    실측(omp 17.1.8): input/tool_call/tool_result 어느 이벤트에도 식별자가 없어
    direct 후보는 전부 비고, 이 getter가 유일한 출처다.
    """
    source = _extension_source()
    assert "ctx?.sessionManager?.getSessionId?.()" in source
    assert "session_id: sessionIdentity(event, ctx)" in source


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


# 두 진입점에 본문이 한 벌씩 있던 것들. 사본이 다시 생기면 여기서 걸린다.
_SHARED_ONLY = (
    "architectureReviewerSkillMarkdown", "fullFeatureSkillMarkdown",
    "productBriefSkillMarkdown", "pushWatchSkillMarkdown", "hookScriptCommand",
    "isPruneBackupName", "writePruneBackup", "managedHookScriptName",
    "managedHookDigests", "codexConfigPath", "ompExtensionIsKitOwned",
    "removeOmpHooksExtension", "safeSkillName", "skillRequires",
    "readJsonIfExists", "retiredHookScripts", "isRetiredHookCommand",
    "pruneRetiredHooks", "pruneRetiredHookScripts", "mergeHookSettings",
    "mergeHookConfig", "claudeHooksSettings", "codexHooksSettings",
    "skillIndexBlock", "upsertSkillIndexBlock",
    "extractCliOption", "cliOptionValue", "requestedInstallRootOption",
    "withoutInstallRootOption", "assertInstallRootIsFinal",
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
