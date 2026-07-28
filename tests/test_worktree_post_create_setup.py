"""worktree가 gitignored 머신 설정을 이어받는지 본다.

`git worktree add`는 추적 파일만 가져온다. Android의 `local.properties`(`sdk.dir`),
`.env` 같은 머신 고정 설정은 gitignored라 새 worktree에 없고, 그래서 빌드가 leader
에서는 되고 worktree에서는 안 된다. 사용자가 매번 손으로 복사하게 되는 자리다.

복사와 symlink는 갈라서 본다. 여기서 다루는 것은 **복사**다 — 작고 머신 고정인 설정은
worktree 안에서 고쳐도 leader로 새면 안 된다. `node_modules` 같은 큰 디렉터리 공유는
symlink가 맞지만, 그건 host write boundary가 symlink 대상을 해석해 worktree 밖 쓰기로
판정하는 문제와 얽히므로 별도로 다룬다.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.worktrees import copy_declared_worktree_files


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


def test_declared_machine_config_is_copied_into_the_worktree(tmp_path: Path):
    """반증: 안 가져오면 worktree에서 Gradle이 SDK를 못 찾는다."""
    leader, checkout = tmp_path / "leader", tmp_path / "wt"
    _repo(leader)
    checkout.mkdir()
    (leader / "local.properties").write_text("sdk.dir=/opt/android\n", encoding="utf-8")

    copied = copy_declared_worktree_files(
        leader=leader, checkout=checkout, names=["local.properties", ".env"]
    )

    assert copied == ("local.properties",), "선언했지만 leader에 없는 것까지 세면 안 된다"
    assert (checkout / "local.properties").read_text(encoding="utf-8") == "sdk.dir=/opt/android\n"
    assert not (checkout / ".env").exists()


def test_copy_does_not_overwrite_what_the_worktree_already_has(tmp_path: Path):
    """불변: 이미 손댄 설정을 덮으면 사용자의 수정이 조용히 사라진다."""
    leader, checkout = tmp_path / "leader", tmp_path / "wt"
    _repo(leader)
    checkout.mkdir()
    (leader / "local.properties").write_text("sdk.dir=/opt/leader\n", encoding="utf-8")
    (checkout / "local.properties").write_text("sdk.dir=/opt/mine\n", encoding="utf-8")

    copied = copy_declared_worktree_files(
        leader=leader, checkout=checkout, names=["local.properties"]
    )

    assert copied == ()
    assert (checkout / "local.properties").read_text(encoding="utf-8") == "sdk.dir=/opt/mine\n"


def test_copy_refuses_to_escape_the_worktree(tmp_path: Path):
    """불변: 선언은 설정 파일 이름이지 임의 경로가 아니다.

    `../../.ssh/id_rsa` 같은 값을 그대로 쓰면 profile 한 줄로 worktree 밖을 읽고 쓴다.
    """
    leader, checkout = tmp_path / "leader", tmp_path / "wt"
    _repo(leader)
    checkout.mkdir()

    for bad in ("../escape", "/etc/passwd", "nested/../../escape"):
        try:
            copy_declared_worktree_files(leader=leader, checkout=checkout, names=[bad])
        except ValueError:
            continue
        raise AssertionError(f"worktree 밖 경로를 거부하지 않았다: {bad}")


def test_nested_declared_path_is_allowed_and_creates_parents(tmp_path: Path):
    """불변: `config/local.properties`처럼 하위 경로도 실제로 쓰인다."""
    leader, checkout = tmp_path / "leader", tmp_path / "wt"
    _repo(leader)
    checkout.mkdir()
    nested = leader / "config" / "local.properties"
    nested.parent.mkdir(parents=True)
    nested.write_text("sdk.dir=/opt/android\n", encoding="utf-8")

    copied = copy_declared_worktree_files(
        leader=leader, checkout=checkout, names=["config/local.properties"]
    )

    assert copied == ("config/local.properties",)
    assert (checkout / "config" / "local.properties").is_file()


def test_symlinked_declaration_is_refused(tmp_path: Path):
    """불변: leader 쪽이 symlink면 따라간 곳이 저장소 밖일 수 있다."""
    leader, checkout = tmp_path / "leader", tmp_path / "wt"
    _repo(leader)
    checkout.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (leader / "local.properties").symlink_to(outside)

    copied = copy_declared_worktree_files(
        leader=leader, checkout=checkout, names=["local.properties"]
    )

    assert copied == ()
    assert not (checkout / "local.properties").exists()
