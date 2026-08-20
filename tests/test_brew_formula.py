"""Homebrew formula와 릴리스 stamping.

formula가 틀렸다는 사실은 사용자의 `brew install`에서만 드러난다 — 그때는 이미
저장소 밖이고, 고치는 데 새 커밋이 필요하다. 그래서 두 가지를 여기서 반증한다.

  - kit root 판정에 필요한 자산이 formula의 설치 목록에 있는가. 하나라도 빠지면
    CLI는 링크되지만 workflow·skill 정본을 찾지 못한다.
  - 태그 tarball의 sha256을 사람이 아니라 스크립트가 박는가. 그 값은 태그가 생긴
    뒤에만 알 수 있고, 한 글자 틀리면 설치가 통째로 실패한다.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.kit_digest import KIT_SOURCE_DIGEST_ROOTS

FORMULA = REPO / "Formula" / "agent-flow.rb"
STAMPER = Path("scripts") / "stamp-brew-formula.mjs"
DIGEST = "0" * 64


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    return node


def _kit_copy(tmp_path: Path) -> Path:
    """stamper가 보는 파일만 옮긴 사본. 스크립트는 자기 위치로 root를 정한다."""
    root = tmp_path / "kit"
    (root / "scripts").mkdir(parents=True)
    (root / "Formula").mkdir()
    shutil.copy2(REPO / STAMPER, root / STAMPER)
    shutil.copy2(FORMULA, root / "Formula" / "agent-flow.rb")
    shutil.copy2(REPO / "pyproject.toml", root / "pyproject.toml")
    return root


def _declared_version(root: Path) -> str:
    match = re.search(
        r'^version = "(.+)"$', (root / "pyproject.toml").read_text(encoding="utf-8"), re.M
    )
    assert match is not None, "pyproject.toml declares no version"
    return match.group(1)


def _stamp(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (_node(), str(root / STAMPER), *args),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_formula_installs_every_asset_root_the_kit_resolves(tmp_path: Path) -> None:
    """반증: 자산 하나가 빠지면 brew 설치본은 CLI만 있고 정본이 없다."""
    text = FORMULA.read_text(encoding="utf-8")
    install = re.search(r"libexec\.install ([^\n]+(?:\n\s+\"[^\n]+)*)", text)
    assert install is not None, "formula does not install an asset tree"
    listed = set(re.findall(r'"([^"]+)"', install.group(1)))
    # 기대 목록을 손으로 적지 않는다. 지문이 보는 자리가 곧 런타임이 읽는 자리다.
    required = {root.split("/")[0] for root in KIT_SOURCE_DIGEST_ROOTS}
    # kit root 판정 자체가 이 파일을 본다(core/phase_workflow.py의 find_kit_root).
    required.add("pyproject.toml")
    # 런타임이 읽는 자산만으로는 부족하다. pyproject가 선언한 빌드 입력이 빠지면
    # `pip install`이 metadata 생성에서 죽고, 그 실패는 사용자의 `brew install`에서만
    # 드러난다 — 실제로 `readme`가 그렇게 빠져 있었다.
    readme = re.search(
        r'^readme = "(.+)"$', (REPO / "pyproject.toml").read_text(encoding="utf-8"), re.M
    )
    if readme is not None:
        required.add(readme.group(1))
    assert required <= listed, f"formula does not install {sorted(required - listed)}"


def test_the_formula_keeps_the_venv_below_the_asset_tree(tmp_path: Path) -> None:
    """반증: venv가 libexec 자체면 설치된 패키지의 조상에 kit 서명이 없다."""
    text = FORMULA.read_text(encoding="utf-8")
    assert 'virtualenv_create(libexec/"venv"' in text
    # 래퍼가 없으면 PATH가 정리된 자리에서 node를 찾지 못해 프로젝트 설치가 죽는다.
    assert "write_env_script" in text
    assert 'formula_opt_bin("node")' in text
    # 빈 PATH를 그대로 이어 붙이면 빈 항목이 남고, 빈 항목은 cwd로 해석된다.
    assert "${PATH:-" in text


def test_the_stamper_points_the_formula_at_the_declared_version(tmp_path: Path) -> None:
    """반증: stable 태그와 digest를 손으로 적으면 그 오타는 설치에서만 드러난다."""
    root = _kit_copy(tmp_path)
    version = _declared_version(root)
    result = _stamp(root, "--version", version, "--sha256", DIGEST)
    assert result.returncode == 0, result.stdout + result.stderr
    stamped = (root / "Formula" / "agent-flow.rb").read_text(encoding="utf-8")
    assert f'url "https://github.com/chonamdoo/agent-flow/archive/refs/tags/v{version}.tar.gz"' in stamped
    assert f'sha256 "{DIGEST}"' in stamped
    # Homebrew의 ComponentsOrder cop은 어떤 tap에서도 순서를 강제한다:
    # homepage → url → sha256 → license → head. resource 블록의 url·sha256과
    # 섞이지 않게 최상위 들여쓰기로 고정해서 찾는다.
    def _at(stanza: str) -> int:
        match = re.search(rf'^ {{2}}{stanza} ', stamped, re.M)
        assert match is not None, f"{stanza} is missing from the formula"
        return match.start()

    order = [_at(name) for name in ("homepage", "url", "sha256", "license", "head")]
    assert order == sorted(order), stamped

    # 두 번 돌려도 stanza는 한 벌이어야 한다. 릴리스 workflow는 재실행될 수 있다.
    again = _stamp(root, "--version", version, "--sha256", DIGEST)
    assert again.returncode == 0, again.stdout + again.stderr
    assert (root / "Formula" / "agent-flow.rb").read_text(encoding="utf-8") == stamped


def test_the_stamper_refuses_a_version_the_tree_does_not_declare(tmp_path: Path) -> None:
    """반증: 태그와 소스 버전이 갈라진 formula는 다른 kit을 설치한다."""
    root = _kit_copy(tmp_path)
    before = (root / "Formula" / "agent-flow.rb").read_text(encoding="utf-8")
    other = f"{_declared_version(root)}9"
    result = _stamp(root, "--version", other, "--sha256", DIGEST)
    assert result.returncode != 0
    assert "pyproject.toml declares version" in result.stderr
    # 거절은 파일을 건드리지 않는다. "url이 없다"로 보면 릴리스가 한 번 stamp된 뒤에는
    # 이미 있는 stable stanza 때문에 이 반증이 통째로 무너진다.
    assert (root / "Formula" / "agent-flow.rb").read_text(encoding="utf-8") == before


def test_the_stamper_refuses_a_digest_that_is_not_one(tmp_path: Path) -> None:
    """반증: 길이만 맞는 문자열을 통과시키면 그 formula는 설치 시점에 죽는다."""
    root = _kit_copy(tmp_path)
    result = _stamp(
        root, "--version", _declared_version(root), "--sha256", "not-a-digest"
    )
    assert result.returncode != 0
    assert "not a sha256 digest" in result.stderr
