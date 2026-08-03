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
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core import worktrees as W
from agent_flow.core.worktrees import (
    HOST_HOOK_REGISTRATION_FILES,
    copy_declared_worktree_files,
    provision_host_hook_registrations,
    provision_registered_worktree_host_hooks,
)
from agent_flow.core.worktree_isolation import WorktreeIsolationError


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


def _hook_command(leader: Path, script: str) -> str:
    return f"/usr/bin/python3 -I '{leader}/.agent-flow/scripts/hooks/{script}'"


def _leader_with_host_hooks(root: Path) -> None:
    """installer가 leader에만 심는 상태를 그대로 만든다."""
    _repo(root)
    (root / ".agent-flow" / "scripts" / "hooks").mkdir(parents=True)
    for rel in HOST_HOOK_REGISTRATION_FILES:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            _hook_command(root, "comment-checker.py") + "\n", encoding="utf-8"
        )


def _managed_checkout(leader: Path, name: str) -> Path:
    checkout = leader / ".agent-flow" / "worktrees" / name
    _git("worktree", "add", "-b", f"feat/{name}", str(checkout), "HEAD", cwd=leader)
    return checkout


def _leader_registration_bytes(leader: Path) -> dict[str, bytes]:
    return {
        rel: (leader / rel).read_bytes()
        for rel in HOST_HOOK_REGISTRATION_FILES
        if (leader / rel).is_file()
    }


def test_managed_checkout_gets_the_host_hook_registrations(tmp_path: Path):
    """leader의 host 경계 hook 등록이 managed checkout에도 provision된다."""
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    checkout = _managed_checkout(leader, "feat-hooks")
    before = _leader_registration_bytes(leader)

    written = provision_host_hook_registrations(leader=leader, checkout=checkout)

    # `.codex`와 `.Codex`는 대소문자를 구분하지 않는 파일시스템에서 한 자리다.
    # 그런 곳에서는 둘 중 하나만 실제 쓰기로 잡히므로 목록 대신 결과를 본다.
    assert set(written) <= set(HOST_HOOK_REGISTRATION_FILES)
    for rel in HOST_HOOK_REGISTRATION_FILES:
        text = (checkout / rel).read_text(encoding="utf-8")
        assert _hook_command(leader, "comment-checker.py") in text, (
            f"{rel}의 command가 leader 절대경로를 가리키지 않는다: {text!r}"
        )
    assert _leader_registration_bytes(leader) == before, "leader 등록 파일은 읽기 전용이다"


def test_registered_managed_checkout_gets_host_hooks_on_reinstall(tmp_path: Path):
    """재설치는 이미 존재하는 managed checkout도 다시 provision한다."""
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    checkout = _managed_checkout(leader, "feat-existing")

    synced = provision_registered_worktree_host_hooks(root=leader)

    assert len(synced) == 1
    assert synced[0][0] == checkout.resolve()
    assert set(synced[0][1]) <= set(HOST_HOOK_REGISTRATION_FILES)
    assert set(synced[0][1])
    for rel, payload in _leader_registration_bytes(leader).items():
        assert (checkout / rel).read_bytes() == payload


@pytest.mark.parametrize(
    ("registration_identity", "checkout_identity", "message"),
    [
        ("wrong", None, "both registration and checkout identities"),
        ("wrong", (-1, -1), "path changed before hook sync"),
    ],
)
def test_hook_sync_fails_closed_when_checkout_binding_does_not_match(
    tmp_path: Path,
    registration_identity: str,
    checkout_identity: tuple[int, int] | None,
    message: str,
):
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    checkout = _managed_checkout(leader, "feat-binding")

    with pytest.raises(WorktreeIsolationError, match=message):
        provision_host_hook_registrations(
            leader=leader,
            checkout=checkout,
            expected_registration_identity=registration_identity,
            expected_checkout_identity=checkout_identity,
        )

    for rel in HOST_HOOK_REGISTRATION_FILES:
        assert not (checkout / rel).exists()

def test_reinstall_without_worktrees_does_not_require_dir_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    monkeypatch.setattr(W, "_DIR_FD_SUPPORTED", False)

    assert provision_registered_worktree_host_hooks(root=leader) == ()



def test_reinstall_does_not_trust_an_unadopted_sibling_worktree(tmp_path: Path):
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    checkout = leader.parent / f"{leader.name}.worktrees" / "raw"
    checkout.parent.mkdir()
    _git("worktree", "add", "-b", "feat/raw", str(checkout), "HEAD", cwd=leader)

    synced = provision_registered_worktree_host_hooks(root=leader)

    assert synced == ()
    for rel in HOST_HOOK_REGISTRATION_FILES:
        assert not (checkout / rel).exists()


def test_tracked_registration_file_is_never_overwritten(tmp_path: Path, capsys):
    """불변: 프로젝트가 `.claude/settings.json`을 추적하면 그 파일은 사용자 소유다.

    덮으면 사용자 설정이 사라지고 worktree가 dirty가 되어 정리 게이트까지 막힌다.

    반증: 조용히 건너뛰면 그 checkout은 hook 미등록으로 남고 사용자는 격리
    가드가 빠진 이유를 어디에서도 못 본다.
    """
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    tracked = ".claude/settings.json"
    (leader / tracked).write_text("COMMITTED\n", encoding="utf-8")
    _git("add", tracked, cwd=leader)
    _git("commit", "-m", "track claude settings", cwd=leader)
    checkout = _managed_checkout(leader, "feat-tracked")
    # leader의 작업본만 installer가 덮은 상태. 내용이 달라 동일성 skip에 걸리지 않는다.
    (leader / tracked).write_text(
        _hook_command(leader, "comment-checker.py") + "\n", encoding="utf-8"
    )

    written = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert tracked not in written
    assert (checkout / tracked).read_text(encoding="utf-8") == "COMMITTED\n"
    assert ".omp/extensions/agent-flow-hooks.ts" in written, (
        "추적 하나 때문에 나머지 등록까지 멈추면 안 된다"
    )
    reported = capsys.readouterr().err
    assert tracked in reported and "tracked by git" in reported, (
        f"tracked skip이 사유를 말하지 않는다: {reported!r}"
    )
    assert "untrack" in reported, "사유만 말하고 해결 방법을 말하지 않으면 사용자가 못 고친다"


def test_reprovisioning_writes_nothing(tmp_path: Path):
    """불변: run 해석 지점에서 매번 불리므로 두 번째부터는 쓰기가 없어야 한다."""
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    checkout = _managed_checkout(leader, "feat-idempotent")
    provision_host_hook_registrations(leader=leader, checkout=checkout)
    stamps = {
        rel: (checkout / rel).stat().st_mtime_ns for rel in HOST_HOOK_REGISTRATION_FILES
    }

    assert provision_host_hook_registrations(leader=leader, checkout=checkout) == ()
    assert {
        rel: (checkout / rel).stat().st_mtime_ns for rel in HOST_HOOK_REGISTRATION_FILES
    } == stamps


def _kit_settings_json(leader: Path, script: str) -> str:
    """installer가 실제로 쓰는 모양. command는 관리 네임스페이스만 가리킨다."""
    return json.dumps(
        {
            "hooks": {
                "PostToolUse": [
                    {
                        "hooks": [
                            {"type": "command", "command": _hook_command(leader, script)}
                        ]
                    }
                ]
            }
        }
    )


def _kit_omp_extension(script: str) -> str:
    """kit이 통째로 생성하는 소스. 표지가 소유 판정의 근거다."""
    return (
        "// agent-flow: managed omp extension\n"
        "export default function agentFlowHooks(ctx) {\n"
        f"  return {script!r};\n"
        "}\n"
    )


@pytest.mark.parametrize("operation", ["replace", "retire"])
def test_fd_transaction_preserves_a_registration_replaced_after_inspection(
    tmp_path: Path, operation: str
):
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    checkout = _managed_checkout(leader, f"feat-race-{operation}")
    target = checkout / ".claude" / "settings.json"
    target.parent.mkdir()
    target.write_text(
        _kit_settings_json(leader, "comment-checker.py"),
        encoding="utf-8",
    )
    inspected = target.stat()
    expected = (inspected.st_dev, inspected.st_ino)
    target.unlink()
    mine = b'{"hooks":{"PostToolUse":[{"command":"./mine"}]}}'
    target.write_bytes(mine)
    parent = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if operation == "replace":
            changed = W._replace_host_hook_registration_at(
                parent=parent,
                name=target.name,
                payload=_kit_settings_json(
                    leader, "bind-host-worktree.py"
                ).encode(),
                leader=leader,
                rel=".claude/settings.json",
                expected=expected,
            )
        else:
            changed = W._retire_host_hook_registration_at(
                parent=parent,
                name=target.name,
                leader=leader,
                rel=".claude/settings.json",
                expected=expected,
            )
    finally:
        os.close(parent)

    assert not changed
    assert target.read_bytes() == mine


def test_pinned_git_admin_ignores_pointer_swap_and_blocks_index_writers(
    tmp_path: Path,
):
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    rel = ".claude/settings.json"
    _git("add", rel, cwd=leader)
    _git("commit", "-m", "track registration", cwd=leader)
    checkout = _managed_checkout(leader, "feat-pinned-index")
    checkout_fd = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with W._locked_verified_worktree_index(
            leader=leader,
            checkout=checkout,
            checkout_fd=checkout_fd,
        ) as gitdir_fd:
            pointer = checkout / ".git"
            original = pointer.read_bytes()
            pointer.write_text("gitdir: /tmp/untrusted-admin\n", encoding="utf-8")
            try:
                state = W._HostHookProvisionState(
                    path=None,
                    skipped={},
                    tracked={},
                    index_identity="",
                )
                assert W._host_hook_path_is_tracked_at(
                    gitdir_fd=gitdir_fd,
                    rel=rel,
                    state=state,
                )
            finally:
                pointer.write_bytes(original)

            (checkout / "race.txt").write_text("race\n", encoding="utf-8")
            blocked = subprocess.run(
                ("git", "add", "race.txt"),
                cwd=checkout,
                capture_output=True,
                text=True,
                check=False,
            )
            assert blocked.returncode != 0
            assert "index.lock" in blocked.stderr
    finally:
        os.close(checkout_fd)


def test_missing_leader_registration_retires_only_kit_owned_checkout_files(
    tmp_path: Path,
):
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    managed = {
        ".claude/settings.json": _kit_settings_json(
            leader, "comment-checker.py"
        ),
        ".omp/extensions/agent-flow-hooks.ts": _kit_omp_extension("confirm"),
    }
    for rel, content in managed.items():
        (leader / rel).write_text(content, encoding="utf-8")
    checkout = _managed_checkout(leader, "feat-disable-hooks")
    provision_host_hook_registrations(leader=leader, checkout=checkout)
    for rel in managed:
        (leader / rel).unlink()

    changed = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert set(managed) <= set(changed)
    for rel in managed:
        assert not (checkout / rel).exists()


def test_missing_leader_registration_keeps_user_owned_checkout_file(
    tmp_path: Path,
):
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    rel = ".claude/settings.json"
    (leader / rel).unlink()
    checkout = _managed_checkout(leader, "feat-user-hooks")
    target = checkout / rel
    target.parent.mkdir()
    target.write_text('{"hooks":{"PostToolUse":[{"command":"./mine"}]}}', encoding="utf-8")
    before = target.read_bytes()

    changed = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert rel not in changed
    assert target.read_bytes() == before


def test_a_user_written_registration_in_the_checkout_is_not_overwritten(
    tmp_path: Path, capsys
):
    """반증: 미추적 등록 파일을 그냥 덮으면 사용자가 그 checkout에 직접 둔 host 설정이
    백업도 경고도 없이 사라진다. `status`/`continue`가 매번 이 경로를 타므로 사용자가
    다시 써 넣어도 다음 명령에 또 짓밟힌다.
    """
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    (leader / ".claude/settings.json").write_text(
        _kit_settings_json(leader, "comment-checker.py"), encoding="utf-8"
    )
    (leader / ".omp/extensions/agent-flow-hooks.ts").write_text(
        _kit_omp_extension("confirm"), encoding="utf-8"
    )
    checkout = _managed_checkout(leader, "feat-user-owned")

    mine_json = checkout / ".claude" / "settings.json"
    mine_json.parent.mkdir()
    mine_json.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {"hooks": [{"type": "command", "command": "/usr/bin/env my-hook"}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    mine_ts = checkout / ".omp" / "extensions" / "agent-flow-hooks.ts"
    mine_ts.parent.mkdir(parents=True)
    mine_ts.write_text("export const mine = 1;\n", encoding="utf-8")
    before = {mine_json: mine_json.read_bytes(), mine_ts: mine_ts.read_bytes()}

    written = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert ".claude/settings.json" not in written
    assert ".omp/extensions/agent-flow-hooks.ts" not in written
    assert {path: path.read_bytes() for path in before} == before
    reported = capsys.readouterr().err
    for rel in (".claude/settings.json", ".omp/extensions/agent-flow-hooks.ts"):
        assert rel in reported, f"{rel}을 건너뛴 사유가 없다: {reported!r}"
    assert "this kit did not write" in reported


def test_user_keys_next_to_managed_hooks_are_not_overwritten(tmp_path: Path, capsys):
    """반증: hook command만 보고 kit 소유로 판정하면, 같은 파일에 사용자가 둔
    `permissions`/`env`/MCP 설정이 leader 파일로 통째 교체돼 조용히 사라진다.
    installer의 `mergeHookConfig`가 그 공존을 보존하므로 흔한 구성이다.
    """
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    json_rel = ".claude/settings.json"
    (leader / json_rel).write_text(
        _kit_settings_json(leader, "comment-checker.py"), encoding="utf-8"
    )
    checkout = _managed_checkout(leader, "feat-user-keys")

    mine = checkout / json_rel
    mine.parent.mkdir()
    document = json.loads(_kit_settings_json(leader, "comment-checker.py"))
    document["permissions"] = {"allow": ["Bash(ls:*)"]}
    mine.write_text(json.dumps(document), encoding="utf-8")
    before = mine.read_bytes()

    written = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert json_rel not in written
    assert mine.read_bytes() == before
    assert json_rel in capsys.readouterr().err


def test_a_registration_this_kit_wrote_is_upgraded_in_place(tmp_path: Path):
    """반증: 소유 판정이 "이미 있으면 손대지 않는다"로 굳으면 등록 갱신이 checkout까지
    번지지 않는다. 그 checkout은 낡은 command를 계속 부르게 된다.
    """
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    json_rel, omp_rel = ".claude/settings.json", ".omp/extensions/agent-flow-hooks.ts"
    (leader / json_rel).write_text(
        _kit_settings_json(leader, "comment-checker.py"), encoding="utf-8"
    )
    (leader / omp_rel).write_text(_kit_omp_extension("confirm"), encoding="utf-8")
    checkout = _managed_checkout(leader, "feat-upgrade")

    stale_json = checkout / json_rel
    stale_json.parent.mkdir()
    stale_json.write_text(
        _kit_settings_json(leader, "record-skill-read.py"), encoding="utf-8"
    )
    stale_omp = checkout / omp_rel
    stale_omp.parent.mkdir(parents=True)
    # 표지가 붙기 전 설치본. 생성 서명만으로도 kit 소유로 인정해야 업그레이드가 닿는다.
    stale_omp.write_text(
        "export default function agentFlowHooks(ctx) { return 'stale'; }\n", encoding="utf-8"
    )

    written = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert json_rel in written and omp_rel in written
    assert stale_json.read_bytes() == (leader / json_rel).read_bytes()
    assert stale_omp.read_bytes() == (leader / omp_rel).read_bytes()


def test_a_symlinked_registration_target_is_skipped_with_a_reason(
    tmp_path: Path, capsys
):
    """반증: symlink 거부가 조용하면 그 checkout은 hook 미등록으로 남는다.
    따라간 곳이 checkout 밖일 수 있어 쓸 수는 없다."""
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    checkout = _managed_checkout(leader, "feat-symlink")
    outside = tmp_path / "elsewhere.json"
    outside.write_text("{}\n", encoding="utf-8")
    target = checkout / ".claude" / "settings.json"
    target.parent.mkdir()
    target.symlink_to(outside)

    written = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert ".claude/settings.json" not in written
    assert outside.read_text(encoding="utf-8") == "{}\n", "symlink를 따라 밖에 썼다"
    reported = capsys.readouterr().err
    assert ".claude/settings.json" in reported and "symlink" in reported


def _kit_generated_registrations(leader: Path) -> tuple[str, str]:
    """installer가 실제로 생산하는 바이트. 소유 판정 기준(`agent-flow: managed omp
    extension` 표지, 관리 hook 디렉터리)은 JS가 단일 소스이고 Python에는 사본이 있다.
    사본을 문자열로 대조하면 생산물이 바뀌었을 때 잡히지 않으므로 실제로 생성해 본다.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node를 찾을 수 없다")
    kit_root = Path(__file__).resolve().parents[1]
    payload = subprocess.run(
        (
            node,
            "--input-type=module",
            "-e",
            "import { claudeHooksSettings } from "
            f"{json.dumps(str(kit_root / 'lib' / 'installer-shared.mjs'))};"
            "import { ompHooksExtensionSource } from "
            f"{json.dumps(str(kit_root / 'lib' / 'omp-hooks-extension.mjs'))};"
            "process.stdout.write(JSON.stringify({"
            f"  settings: JSON.stringify(claudeHooksSettings({json.dumps(str(leader))}), null, 2),"
            "  extension: ompHooksExtensionSource(),"
            "}));",
        ),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    generated = json.loads(payload)
    return generated["settings"], generated["extension"]


def test_kit_ownership_matches_what_the_installers_actually_generate(tmp_path: Path):
    """반증: 소유 판정 기준이 실제 생산물과 갈라지면 두 방향 모두 사고다 — kit이 깐
    등록을 사용자 것으로 오판하면 갱신이 checkout에 영영 닿지 않고, 반대로 오판하면
    사용자 파일을 덮는다. 여기서는 installer가 생성한 바이트가 kit 소유로 읽히는지 본다.
    """
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    json_rel, omp_rel = ".claude/settings.json", ".omp/extensions/agent-flow-hooks.ts"
    settings, extension = _kit_generated_registrations(leader)
    checkout = _managed_checkout(leader, "feat-real-bytes")

    # checkout에는 installer가 깐 그대로, leader에는 그 뒤 갱신된 등록이 있는 상태.
    for rel, generated in ((json_rel, settings), (omp_rel, extension)):
        stale = checkout / rel
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(generated, encoding="utf-8")
        (leader / rel).write_text(generated + "\n", encoding="utf-8")

    written = provision_host_hook_registrations(leader=leader, checkout=checkout)

    for rel in (json_rel, omp_rel):
        assert rel in written, (
            f"installer가 생성한 {rel}을 kit 소유로 읽지 못했다 — 갱신이 checkout에 닿지 않는다"
        )
        assert (checkout / rel).read_bytes() == (leader / rel).read_bytes()


def _settings_json_with_command(command: str) -> str:
    return json.dumps(
        {
            "hooks": {
                "PostToolUse": [
                    {"hooks": [{"type": "command", "command": command}]}
                ]
            }
        }
    )


def test_a_user_wrapper_around_a_managed_hook_is_not_overwritten(
    tmp_path: Path, capsys
):
    """반증: 관리 hook 디렉터리를 **부분문자열**로 찾으면 사용자가 그 hook을 감싸서 넣은
    항목까지 kit 소유로 읽힌다. 그 래퍼(로깅·알림)는 백업도 경고도 없이 사라지고,
    `status`/`continue`가 매번 이 경로를 타므로 다시 써 넣어도 또 짓밟힌다.
    """
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    rel = ".claude/settings.json"
    (leader / rel).write_text(
        _kit_settings_json(leader, "comment-checker.py"), encoding="utf-8"
    )
    checkout = _managed_checkout(leader, "feat-wrapper")
    mine = checkout / rel
    mine.parent.mkdir()
    mine.write_text(
        _settings_json_with_command(
            "/bin/bash -c 'mylog; /usr/bin/python3 -I "
            f"{leader}/.agent-flow/scripts/hooks/comment-checker.py'"
        ),
        encoding="utf-8",
    )
    before = mine.read_bytes()

    written = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert rel not in written
    assert mine.read_bytes() == before, "관리 hook을 감싼 사용자 래퍼를 덮었다"
    reported = capsys.readouterr().err
    assert rel in reported and "this kit did not write" in reported, (
        f"덮지 않은 사유가 없다: {reported!r}"
    )


def test_a_registration_pointing_at_another_installation_is_not_overwritten(
    tmp_path: Path, capsys
):
    """반증: 부분문자열 판정은 **다른 설치본**의 hook 디렉터리를 가리키는 command도 kit
    소유로 읽는다. 이 leader가 생성할 수 없는 command라면 사용자가 넣은 것이다.
    """
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    rel = ".claude/settings.json"
    (leader / rel).write_text(
        _kit_settings_json(leader, "comment-checker.py"), encoding="utf-8"
    )
    other = tmp_path / "other-install"
    (other / ".agent-flow" / "scripts" / "hooks").mkdir(parents=True)
    checkout = _managed_checkout(leader, "feat-foreign")
    mine = checkout / rel
    mine.parent.mkdir()
    mine.write_text(
        _kit_settings_json(other, "comment-checker.py"), encoding="utf-8"
    )
    before = mine.read_bytes()

    written = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert rel not in written
    assert mine.read_bytes() == before, "다른 설치본을 가리키는 등록을 이 leader 것으로 덮었다"
    reported = capsys.readouterr().err
    assert rel in reported and "this kit did not write" in reported, (
        f"덮지 않은 사유가 없다: {reported!r}"
    )


def test_an_unfixable_skip_is_reported_once_and_stops_spawning_git(
    tmp_path: Path, capsys, monkeypatch
):
    """반증: `status`는 host 세션이 매 턴 돌리는 명령이다. 사유를 기억하지 않으면 고칠
    수 없는 구성에서 같은 경고가 무한히 쌓여 아무도 읽지 않고, tracked 판정도 호출마다
    `git ls-files`를 새로 띄운다 — 그 케이스는 구조적으로 내용 동일성 skip을 통과할 수
    없으므로 그 spawn이 영구 비용이 된다.
    """
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    tracked = ".claude/settings.json"
    (leader / tracked).write_text("COMMITTED\n", encoding="utf-8")
    _git("add", tracked, cwd=leader)
    _git("commit", "-m", "track claude settings", cwd=leader)
    checkout = _managed_checkout(leader, "feat-quiet")
    (leader / tracked).write_text(
        _kit_settings_json(leader, "comment-checker.py"), encoding="utf-8"
    )

    provision_host_hook_registrations(leader=leader, checkout=checkout)
    assert "tracked by git" in capsys.readouterr().err, (
        "첫 호출에서 사유를 말하지 않으면 사용자는 고칠 기회를 못 얻는다"
    )

    spawned: list[tuple[str, ...]] = []
    real_git_safe = W.git_safe

    def _counting_git_safe(*args, **kwargs):
        spawned.append(args)
        return real_git_safe(*args, **kwargs)

    monkeypatch.setattr(W, "git_safe", _counting_git_safe)
    provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert capsys.readouterr().err == "", "같은 사유를 매 호출마다 반복해서 낸다"
    assert [args for args in spawned if args[:1] == ("ls-files",)] == [], (
        f"tracked 판정이 호출마다 git을 띄운다: {spawned!r}"
    )


def test_a_changed_skip_reason_is_reported_again(tmp_path: Path, capsys):
    """불변: 기억은 같은 사유의 반복만 없앤다. 사유가 바뀌면 그건 새 사건이다."""
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    rel = ".claude/settings.json"
    (leader / rel).write_text(
        _kit_settings_json(leader, "comment-checker.py"), encoding="utf-8"
    )
    checkout = _managed_checkout(leader, "feat-changed-reason")
    mine = checkout / rel
    mine.parent.mkdir()
    mine.write_text(_settings_json_with_command("/usr/bin/env my-hook"), encoding="utf-8")

    provision_host_hook_registrations(leader=leader, checkout=checkout)
    assert "this kit did not write" in capsys.readouterr().err

    mine.unlink()
    mine.parent.rmdir()
    (checkout / ".claude").symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    provision_host_hook_registrations(leader=leader, checkout=checkout)

    reported = capsys.readouterr().err
    assert "symlink" in reported, f"사유가 바뀌었는데 침묵한다: {reported!r}"
