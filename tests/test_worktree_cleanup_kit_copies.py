from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[1]
SRC = str(KIT_ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core import worktrees as W


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(("git", *args), cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result


def _init_repo(root: Path, *, ignored: tuple[str, ...] = ("CLAUDE.md",)) -> None:
    root.mkdir()
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "cleanup@example.com", cwd=root)
    _git("config", "user.name", "Cleanup Test", cwd=root)
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    # gitignore는 커밋되어야 새 checkout에도 같은 판정이 붙는다.
    (root / ".gitignore").write_text("".join(f"{name}\n" for name in ignored), encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "init", cwd=root)


def _checkout_with_kit_copies(root: Path, *, names: tuple[str, ...]) -> Path:
    """kit이 checkout을 만들 때 하는 일을 그대로 한다: leader 파일을 복사해 심는다."""
    status = W.create_worktree(root=root, plan=W.plan_worktree(root=root, name="kit-copies"))
    copied = W.copy_declared_worktree_files(leader=root, checkout=status.path, names=names)
    assert copied == names
    return status.path


def test_identical_kit_copy_does_not_block_cleanup(tmp_path: Path) -> None:
    """반증: 하네스가 스스로 심은 `CLAUDE.md` 때문에 모든 worktree가 정리 불가였다.

    `_apply_worktree_setup`이 leader의 루트 컨텍스트 파일을 checkout에 복사하는데,
    이 저장소에서 `CLAUDE.md`는 gitignored라 `--ignored=matching` status에 `!!`로 뜬다.
    kit이 심은 파일이 kit의 정리를 영구히 막는 상태였다.
    """
    root = tmp_path / "repo"
    _init_repo(root)
    (root / "CLAUDE.md").write_text("leader contract\n", encoding="utf-8")
    checkout = _checkout_with_kit_copies(root, names=("CLAUDE.md",))

    W._assert_cleanup_checkout_clean(root=root, path=checkout)


def test_edited_kit_copy_still_blocks_cleanup(tmp_path: Path) -> None:
    """불변: 이름이 아니라 내용으로 판정한다.

    예외를 받은 경로는 checkout과 함께 지워진다. 이름만 보고 통과시키면 사용자가
    worktree에서 고친 설정이 조용히 사라진다.
    """
    root = tmp_path / "repo"
    _init_repo(root)
    (root / "CLAUDE.md").write_text("leader contract\n", encoding="utf-8")
    checkout = _checkout_with_kit_copies(root, names=("CLAUDE.md",))
    (checkout / "CLAUDE.md").write_text("leader contract\nedited by the user\n", encoding="utf-8")

    with pytest.raises(W.CleanupBlockedError) as excinfo:
        W._assert_cleanup_checkout_clean(root=root, path=checkout)
    assert "CLAUDE.md" in str(excinfo.value)


def test_unrelated_ignored_file_still_blocks_cleanup(tmp_path: Path) -> None:
    """불변: 후보 목록에 없는 파일은 leader에 같은 내용이 있어도 막는다."""
    root = tmp_path / "repo"
    _init_repo(root, ignored=("CLAUDE.md", "scratch.txt"))
    (root / "CLAUDE.md").write_text("leader contract\n", encoding="utf-8")
    (root / "scratch.txt").write_text("same bytes\n", encoding="utf-8")
    checkout = _checkout_with_kit_copies(root, names=("CLAUDE.md",))
    (checkout / "scratch.txt").write_text("same bytes\n", encoding="utf-8")

    with pytest.raises(W.CleanupBlockedError) as excinfo:
        W._assert_cleanup_checkout_clean(root=root, path=checkout)
    assert "scratch.txt" in str(excinfo.value)


def test_profile_declared_copy_is_exempt_when_identical(tmp_path: Path) -> None:
    """반증: 후보를 루트 컨텍스트 파일로 좁히면 profile이 선언한 복사본이 정리를 막는다.

    `branching.worktree_setup.copy`는 `local.properties`처럼 gitignored인 머신 설정을
    고르라고 있는 선언이다. 그 파일이 정리 차단 사유가 되면 android 프로젝트의 worktree는
    만들자마자 지울 수 없다.
    """
    root = tmp_path / "repo"
    _init_repo(root, ignored=("CLAUDE.md", "local.properties"))
    (root / "CLAUDE.md").write_text("leader contract\n", encoding="utf-8")
    (root / "local.properties").write_text("sdk.dir=/opt/android\n", encoding="utf-8")
    profiles_dir = root / ".agent-flow" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "android.yaml").write_text(
        "id: android\nbranching:\n  worktree_setup:\n    copy:\n      - local.properties\n",
        encoding="utf-8",
    )
    (root / ".agent-flow" / "kit.json").write_text(json.dumps({"profile": "android"}), encoding="utf-8")
    checkout = _checkout_with_kit_copies(root, names=("CLAUDE.md", "local.properties"))

    W._assert_cleanup_checkout_clean(root=root, path=checkout)


def test_tracked_context_file_modified_in_the_checkout_still_blocks(tmp_path: Path) -> None:
    """불변: 추적 중인 파일의 커밋되지 않은 수정은 leader 워킹트리와 같아도 막는다.

    후보 이름은 추적 여부를 가리지 않는다. 내용 동일성만 보면 leader에서 하던 수정과
    우연히 같은 checkout의 미커밋 작업이 정리 대상이 된다.
    """
    root = tmp_path / "repo"
    _init_repo(root, ignored=())
    (root / "CLAUDE.md").write_text("v1\n", encoding="utf-8")
    _git("add", "CLAUDE.md", cwd=root)
    _git("commit", "-m", "context", cwd=root)
    status = W.create_worktree(root=root, plan=W.plan_worktree(root=root, name="tracked-context"))
    (root / "CLAUDE.md").write_text("v2\n", encoding="utf-8")
    (status.path / "CLAUDE.md").write_text("v2\n", encoding="utf-8")

    with pytest.raises(W.CleanupBlockedError) as excinfo:
        W._assert_cleanup_checkout_clean(root=root, path=status.path)
    assert "CLAUDE.md" in str(excinfo.value)
