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

import json
import os
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.worktrees import (
    bootstrap_host_hook_surfaces,
    copy_declared_worktree_files,
)


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


def test_symlinked_parent_directory_is_refused(tmp_path: Path):
    """반증: 마지막 구성요소만 보면 중간 디렉터리 symlink로 밖을 읽는다.

    leader에 `config`가 저장소 밖을 가리키는 symlink로 커밋돼 있고 profile이
    `config/passwd`를 선언하면, lexical 봉쇄는 통과하고 leaf는 symlink가 아니며
    `is_file()`은 따라간 곳을 보고 참을 낸다. 선언 한 줄로 저장소 밖 파일이 복사된다.
    git은 symlink를 커밋할 수 있으므로 실제로 닿는 경로다.
    """
    leader, checkout = tmp_path / "leader", tmp_path / "wt"
    _repo(leader)
    checkout.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "passwd").write_text("root:x:0:0\n", encoding="utf-8")
    (leader / "config").symlink_to(outside, target_is_directory=True)

    copied = copy_declared_worktree_files(
        leader=leader, checkout=checkout, names=["config/passwd"]
    )

    assert copied == ()
    assert not (checkout / "config").exists()


def test_symlinked_parent_in_the_checkout_is_refused(tmp_path: Path):
    """불변: 쓰는 쪽도 같다. checkout의 중간 디렉터리가 symlink면 밖에 쓴다."""
    leader, checkout = tmp_path / "leader", tmp_path / "wt"
    _repo(leader)
    checkout.mkdir()
    nested = leader / "config" / "local.properties"
    nested.parent.mkdir(parents=True)
    nested.write_text("sdk.dir=/opt/android\n", encoding="utf-8")

    outside = tmp_path / "sink"
    outside.mkdir()
    (checkout / "config").symlink_to(outside, target_is_directory=True)

    copied = copy_declared_worktree_files(
        leader=leader, checkout=checkout, names=["config/local.properties"]
    )

    assert copied == ()
    assert not (outside / "local.properties").exists()


def test_multi_profile_union_still_declares_its_copies():
    """반증: 합성 profile에는 최상위 `branching`이 없어 선언이 조용히 사라진다.

    `_load_profile_union`은 개별 profile을 `profiles` 아래에 넣고 최상위에는
    `review_angles`/`gates`/`skills`/`architecture`만 합친다. 최상위 `branching`만
    보면 android+react-native 프로젝트에서 `local.properties`가 영영 복사되지 않는다.
    """
    from agent_flow.cli import _declared_worktree_copies

    android = {"branching": {"worktree_setup": {"copy": ["local.properties"]}}}
    react_native = {"branching": {"worktree_setup": {"copy": [".env"]}}}
    union = {"id": "multi-profile", "profiles": [android, react_native]}

    assert _declared_worktree_copies(union) == ["local.properties", ".env"]


def test_single_profile_declaration_still_works():
    """불변: 합성본을 지원하느라 단일 profile 경로를 잃으면 안 된다."""
    from agent_flow.cli import _declared_worktree_copies

    single = {"branching": {"worktree_setup": {"copy": ["local.properties"]}}}
    assert _declared_worktree_copies(single) == ["local.properties"]


def test_duplicate_declarations_are_collapsed():
    """불변: 두 profile이 같은 파일을 선언해도 한 번만 다룬다."""
    from agent_flow.cli import _declared_worktree_copies

    same = {"branching": {"worktree_setup": {"copy": ["local.properties"]}}}
    union = {"profiles": [same, dict(same)]}
    assert _declared_worktree_copies(union) == ["local.properties"]


def _hook_registration(leader: Path) -> None:
    command = f"/bin/bash {leader / '.agent-flow' / 'scripts' / 'hooks' / 'guard.sh'}"
    for relative, body in (
        (Path(".claude") / "settings.json", json.dumps({"hooks": {"PreToolUse": [command]}})),
        (Path(".Codex") / "hooks.json", json.dumps({"hooks": {"PreToolUse": [command]}})),
        (Path(".omp") / "extensions" / "agent-flow-hooks.ts", 'runHook("guard.sh", payload);\n'),
    ):
        target = leader / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")


def test_host_hook_registration_files_are_copied_into_the_checkout(tmp_path: Path):
    """반증: host는 cwd의 등록 파일만 읽는다. 없는 checkout에서는 강제 hook이 한 개도
    안 뜨는데 무결성 게이트는 leader를 보므로 초록불이 켜진다."""
    leader, checkout = tmp_path / "leader", tmp_path / "wt"
    _repo(leader)
    checkout.mkdir()
    _hook_registration(leader)

    installed = bootstrap_host_hook_surfaces(leader=leader, checkout=checkout)

    assert set(installed) == {
        str(Path(".claude") / "settings.json"),
        str(Path(".Codex") / "hooks.json"),
        str(Path(".omp") / "extensions" / "agent-flow-hooks.ts"),
    }
    for relative in installed:
        assert (checkout / relative).read_text(encoding="utf-8") == (
            leader / relative
        ).read_text(encoding="utf-8")


def test_host_hook_bootstrap_is_idempotent_when_the_checkout_copy_matches(tmp_path: Path):
    """불변: 내용이 같으면 다시 쓰지 않는다. 이 부트스트랩은 create/attach/adopt마다
    도는데 매번 덮으면 등록 파일의 mtime이 계속 흔들려, 어떤 설치가 실제로 등록을
    바꿨는지 구분할 수 없다."""
    leader, checkout = tmp_path / "leader", tmp_path / "wt"
    _repo(leader)
    checkout.mkdir()
    _hook_registration(leader)
    first = bootstrap_host_hook_surfaces(leader=leader, checkout=checkout)
    assert first
    # 다시 쓰면 copy2가 leader의 mtime을 실어 오므로 이 표식이 지워진다.
    for relative in first:
        os.utime(checkout / relative, ns=(0, 0))

    again = bootstrap_host_hook_surfaces(leader=leader, checkout=checkout)

    assert again == ()
    for relative in first:
        assert (checkout / relative).stat().st_mtime_ns == 0


def test_host_hook_bootstrap_refreshes_a_stale_checkout_copy(tmp_path: Path):
    """반증: kit 업그레이드로 leader 등록이 바뀌었는데 묵은 checkout 사본을 그대로 두면
    그 checkout에서만 새 hook이 안 뜬다. 무결성 게이트는 leader 설치본만 보므로 초록불이
    켜진 채 그 자리만 무력화된다."""
    leader, checkout = tmp_path / "leader", tmp_path / "wt"
    _repo(leader)
    checkout.mkdir()
    _hook_registration(leader)
    assert bootstrap_host_hook_surfaces(leader=leader, checkout=checkout)
    settings = Path(".claude") / "settings.json"
    upgraded = json.dumps({"hooks": {"PreToolUse": ["/bin/bash guard-v2.sh"]}})
    (leader / settings).write_text(upgraded, encoding="utf-8")

    installed = bootstrap_host_hook_surfaces(leader=leader, checkout=checkout)

    assert str(settings) in installed
    assert (checkout / settings).read_text(encoding="utf-8") == upgraded


def test_host_hook_bootstrap_is_a_no_op_in_the_leader(tmp_path: Path):
    leader = tmp_path / "leader"
    _repo(leader)
    _hook_registration(leader)

    assert bootstrap_host_hook_surfaces(leader=leader, checkout=leader) == ()


def test_host_hook_bootstrap_skips_a_target_that_is_not_a_regular_file(tmp_path: Path):
    """반증: 대상이 디렉터리면 `copy2`는 실패하지 않고 그 안에 같은 이름으로 복사한다.
    host가 읽는 자리는 여전히 비어 있는데 설치됐다고 보고하면 그 checkout의 강제 hook이
    없는 것을 아무도 모른다."""
    leader, checkout = tmp_path / "leader", tmp_path / "wt"
    _repo(leader)
    checkout.mkdir()
    _hook_registration(leader)
    settings = Path(".claude") / "settings.json"
    (checkout / settings).mkdir(parents=True)

    installed = bootstrap_host_hook_surfaces(leader=leader, checkout=checkout)

    assert str(settings) not in installed
    assert (checkout / settings).is_dir()
    assert list((checkout / settings).iterdir()) == []


def test_host_hook_bootstrap_keeps_every_backup_slot(tmp_path: Path):
    """반증: 슬롯이 하나면 두 번째 갱신이 사용자 원본 백업을 기계 사본으로 덮는다.
    installer(`nextFreeBackupPath`)와 같은 순번 규약이어야 한다."""
    leader, checkout = tmp_path / "leader", tmp_path / "wt"
    _repo(leader)
    checkout.mkdir()
    _hook_registration(leader)
    settings = Path(".claude") / "settings.json"
    assert bootstrap_host_hook_surfaces(leader=leader, checkout=checkout)
    (checkout / settings).write_text("USER EDIT", encoding="utf-8")

    (leader / settings).write_text("LEADER V2", encoding="utf-8")
    assert bootstrap_host_hook_surfaces(leader=leader, checkout=checkout)
    (leader / settings).write_text("LEADER V3", encoding="utf-8")
    assert bootstrap_host_hook_surfaces(leader=leader, checkout=checkout)

    backups = sorted(p.name for p in (checkout / ".claude").iterdir() if ".bak" in p.name)
    assert backups == ["settings.json.bak", "settings.json.bak.1"]
    assert (checkout / ".claude" / "settings.json.bak").read_text(
        encoding="utf-8"
    ) == "USER EDIT"
