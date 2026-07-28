"""install 로직이 저장소에 한 벌만 있는지 본다.

`bin/agent-flow-install.mjs`와 `bin/agent-flow-kit.mjs`는 함수명 86개를 공유했고,
`ompHooksExtensionSource()`는 321줄이 바이트 동일하게 두 벌 박혀 있었다. 두 벌이면
한쪽만 고쳐도 절반만 반영되므로, 둘이 갈라지지 않았는지 보는 검사가 따로 필요해진다.
그 검사를 지우려면 사본부터 없어야 한다.
"""
from __future__ import annotations

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
