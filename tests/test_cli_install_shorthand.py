"""`agent-flow <path>`가 프로젝트 설치로 읽히는 경로.

brew로 설치한 사용자에게는 `npx <kit> install`이 없다. kit이 Cellar 안에 있어서
경로를 손으로 찾아야 하고, 그 자리를 아는 것은 CLI 자신뿐이다. 그래서 반증할 것은
"경로 하나로 설치가 끝난다"와, 그 지름길이 **오타를 설치로 바꾸지 않는다**는 쪽이다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.cli import main


@pytest.fixture(autouse=True)
def isolated_host_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """설치는 host 설정 파일을 건드린다. 개발자의 실제 홈에 쓰지 않게 격리한다."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("AGENT_FLOW_SKIP_CODEX_TRUST", "1")


def test_a_bare_directory_installs_the_project(tmp_path: Path) -> None:
    """반증: 경로만 준 호출이 설치를 하지 않으면 brew 사용자에게 진입점이 없다."""
    project = tmp_path / "project"
    project.mkdir()
    assert main([str(project)]) == 0
    kit = json.loads((project / ".agent-flow" / "kit.json").read_text(encoding="utf-8"))
    assert kit["profile"]
    # 설치의 정본은 JS installer다. 그 산출물이 함께 와야 지름길이 반쪽이 아니다.
    assert (project / "AGENTS.md").is_file()
    assert (project / ".agent-flow" / "skills").is_dir()


def test_installer_flags_are_validated_by_the_installer(tmp_path: Path) -> None:
    """반증: 지름길이 플래그를 자체 검증하면 목록이 두 곳으로 갈라진다."""
    project = tmp_path / "project"
    project.mkdir()
    assert main([str(project), "--bogus-flag"]) != 0
    assert not (project / ".agent-flow" / "kit.json").exists()


def test_a_path_that_does_not_exist_is_not_read_as_an_install(tmp_path: Path) -> None:
    """반증: 존재하지 않는 첫 토큰을 설치로 읽으면 오타가 파일을 만든다."""
    with pytest.raises(SystemExit) as raised:
        main(["stauts"])
    assert raised.value.code == 2


def test_the_explicit_command_reports_a_missing_target(tmp_path: Path) -> None:
    """반증: 없는 대상을 조용히 만들면 사용자가 의도하지 않은 자리에 설치된다."""
    missing = tmp_path / "missing"
    assert main(["install", str(missing)]) == 2
    assert not missing.exists()


def test_a_known_subcommand_is_never_shadowed_by_a_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """반증: 같은 이름의 디렉터리가 있으면 `status`가 설치로 바뀐다."""
    (tmp_path / "status").mkdir()
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        main(["status"])
    captured = capsys.readouterr()
    assert "agent-flow installed" not in captured.out
    assert not (tmp_path / "status" / ".agent-flow").exists()


def test_the_home_directory_is_refused_as_a_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """반증: 홈에 설치하면 프로젝트용 host 설정이 사용자의 전역 설정을 덮어쓴다.

    경로 하나로 끝나는 지름길이 있으니, 그 오타의 대가가 가장 큰 자리는 거절해야 한다.
    """
    home = Path.home()
    assert main([str(home)]) == 2
    assert "home directory" in capsys.readouterr().err
    assert not (home / ".agent-flow" / "kit.json").exists()


def test_a_node_inside_the_install_target_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """반증: 설치 대상 안의 `node`를 실행하면 agent가 쓴 파일이 installer가 된다."""
    project = tmp_path / "project"
    project.mkdir()
    planted = project / "node"
    planted.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    planted.chmod(0o755)
    monkeypatch.setenv("PATH", str(project))
    assert main([str(project)]) == 2
    assert "inside the install target" in capsys.readouterr().err
    assert not (project / ".agent-flow").exists()
