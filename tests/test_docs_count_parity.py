"""문서에 적힌 숫자가 정본과 어긋나면 parity가 실패하는지 확인한다.

`parity:check`가 README의 워크플로 phase 수·프로파일·스킬 수를 대조하기
시작했지만, "틀리면 실패한다"를 확인한 기록은 일회성 수동 실행뿐이었다. 그
확인이 사라지면 대조가 조용히 통과하도록 퇴화해도 아무도 모른다 — 그때
문서는 다시 낡고, 낡은 숫자가 정본처럼 읽힌다.

기대 숫자를 이 파일에 박지 않는다. README에서 지금 값을 읽어 일부러 틀린
값으로 바꾼다. 상수로 박으면 정본이 바뀔 때마다 이 테스트가 먼저 깨지고,
그 실패는 문서 오류와 구별되지 않는다.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[1]
KIT_COPY_IGNORE = shutil.ignore_patterns(".git", "node_modules", "__pycache__", ".agent-flow")
PARITY_SCRIPT = Path("scripts") / "check-agent-flow-parity.mjs"
INSTALL_BIN = Path("bin") / "agent-flow-kit.mjs"
DOC_COUNT_SOURCE = "README.md"
# 스크립트의 DOC_WORKFLOW_ROW·DOC_SKILL_LINE과 같은 형태를 읽는다. 두 곳이
# 어긋나면 이 테스트가 아무것도 바꾸지 못한 채 통과하므로, 아래에서 치환
# 결과를 확인한다.
WORKFLOW_ROW = re.compile(r"^\|\s*`(bugfix)`\s*\|([^|]*)\|\s*(\d+)\s*\|", re.MULTILINE)
SKILL_LINE = re.compile(r"^스킬\s+(\d+)종", re.MULTILINE)


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return node


def _parity(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (_node(), str(root / PARITY_SCRIPT)),
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=900,
    )


@pytest.fixture(scope="module")
def installed_kit(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """install까지 끝난 kit 사본. 설치본 대조까지 켜진 상태로 판정한다."""
    root = tmp_path_factory.mktemp("kit-parity") / "kit"
    shutil.copytree(KIT_ROOT, root, ignore=KIT_COPY_IGNORE, symlinks=True)
    install = subprocess.run(
        (_node(), str(root / INSTALL_BIN), "install"),
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=900,
    )
    assert install.returncode == 0, install.stderr
    return root


def test_parity_check_passes_on_the_installed_kit(installed_kit: Path) -> None:
    """반증: 문서 숫자·LICENSE·USAGE 대조가 지금 저장소에서 통과하지 않으면,
    아래 반대 방향 테스트의 실패가 '문서가 틀렸다'를 뜻하지 못한다."""
    result = _parity(installed_kit)
    assert result.returncode == 0, result.stdout + result.stderr


def test_parity_check_fails_on_wrong_doc_counts(
    installed_kit: Path, tmp_path: Path
) -> None:
    """반증: 숫자를 틀려도 통과하면 이 대조는 있으나 마나다."""
    root = tmp_path / "kit"
    shutil.copytree(installed_kit, root, symlinks=True)
    readme = root / DOC_COUNT_SOURCE
    text = readme.read_text(encoding="utf-8")

    row = WORKFLOW_ROW.search(text)
    skills = SKILL_LINE.search(text)
    assert row is not None, f"{DOC_COUNT_SOURCE}에 bugfix 워크플로 행이 없다"
    assert skills is not None, f"{DOC_COUNT_SOURCE}에 스킬 수 줄이 없다"
    wrong_phases = int(row.group(3)) + 2
    wrong_skills = int(skills.group(1)) - 3

    text = text.replace(row.group(0), f"| `bugfix` |{row.group(2)}| {wrong_phases} |", 1)
    text = text.replace(skills.group(0), f"스킬 {wrong_skills}종", 1)
    readme.write_text(text, encoding="utf-8")

    result = _parity(root)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert f"has {wrong_phases} phases" in output, output
    assert f"says {wrong_skills} skills" in output, output
