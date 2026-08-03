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

import hashlib
import json
import os
import shutil
import shlex
import subprocess
import stat
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

    assert copied == ("local.properties",), (
        "선언했지만 leader에 없는 것까지 세면 안 된다"
    )
    assert (checkout / "local.properties").read_text(
        encoding="utf-8"
    ) == "sdk.dir=/opt/android\n"
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
    assert (checkout / "local.properties").read_text(
        encoding="utf-8"
    ) == "sdk.dir=/opt/mine\n"


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
    return (
        f"'{Path(sys.executable).resolve()}' -I "
        f"'{Path.home() / '.agent-flow' / 'bin' / 'agent-flow-hook'}' "
        f"--root '{leader}' --hook '{script}'"
    )


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
    (root / ".agent-flow" / "kit.json").write_text(
        json.dumps(
            {
                "hook_runtime": {
                    "python": str(Path(sys.executable).resolve()),
                    "launcher_path": str(
                        Path.home() / ".agent-flow" / "bin" / "agent-flow-hook"
                    ),
                }
            }
        ),
        encoding="utf-8",
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


def test_managed_checkout_does_not_get_project_local_host_registrations(
    tmp_path: Path,
):
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    checkout = _managed_checkout(leader, "feat-hooks")
    before = _leader_registration_bytes(leader)

    changed = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert changed == ()
    for rel in (*HOST_HOOK_REGISTRATION_FILES, ".omp/extensions/agent-flow-hooks.ts"):
        assert not (checkout / rel).exists()
    assert _leader_registration_bytes(leader) == before


def test_registered_managed_checkout_remains_free_of_project_local_hooks(
    tmp_path: Path,
):
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    checkout = _managed_checkout(leader, "feat-existing")

    synced = provision_registered_worktree_host_hooks(root=leader)

    assert synced == ((checkout.resolve(), ()),)
    for rel in (*HOST_HOOK_REGISTRATION_FILES, ".omp/extensions/agent-flow-hooks.ts"):
        assert not (checkout / rel).exists()


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


def test_tracked_registration_file_is_never_removed(tmp_path: Path):
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    tracked = ".claude/settings.json"
    (leader / tracked).write_text("COMMITTED\n", encoding="utf-8")
    _git("add", tracked, cwd=leader)
    _git("commit", "-m", "track claude settings", cwd=leader)
    checkout = _managed_checkout(leader, "feat-tracked")

    changed = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert changed == ()
    assert (checkout / tracked).read_text(encoding="utf-8") == "COMMITTED\n"


def test_legacy_registration_cleanup_is_idempotent(tmp_path: Path):
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    checkout = _managed_checkout(leader, "feat-idempotent")
    json_rel = ".claude/settings.json"
    omp_rel = ".omp/extensions/agent-flow-hooks.ts"
    _write_registration(
        checkout,
        json_rel,
        _kit_settings_json(leader, "comment-checker.py"),
    )
    _write_registration(checkout, omp_rel, _kit_omp_extension("confirm"))

    changed = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert set(changed) == {json_rel, omp_rel}
    assert not (checkout / json_rel).exists()
    assert not (checkout / omp_rel).exists()
    assert provision_host_hook_registrations(leader=leader, checkout=checkout) == ()


def _kit_settings_json(leader: Path, script: str) -> str:
    """installer가 실제로 쓰는 모양. command는 관리 네임스페이스만 가리킨다."""
    return json.dumps(
        {
            "hooks": {
                "PostToolUse": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": _hook_command(leader, script),
                            }
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


def _write_registration(root: Path, rel: str, content: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


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
                payload=_kit_settings_json(leader, "bind-host-worktree.py").encode(),
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


def test_cleanup_retires_only_kit_owned_checkout_files(tmp_path: Path):
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    managed = {
        ".claude/settings.json": _kit_settings_json(leader, "comment-checker.py"),
        ".omp/extensions/agent-flow-hooks.ts": _kit_omp_extension("confirm"),
    }
    checkout = _managed_checkout(leader, "feat-disable-hooks")
    for rel, content in managed.items():
        _write_registration(checkout, rel, content)

    changed = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert set(changed) == set(managed)
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
    target.write_text(
        '{"hooks":{"PostToolUse":[{"command":"./mine"}]}}', encoding="utf-8"
    )
    before = target.read_bytes()

    changed = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert rel not in changed
    assert target.read_bytes() == before


def test_a_user_written_registration_in_the_checkout_is_preserved(
    tmp_path: Path,
):
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    checkout = _managed_checkout(leader, "feat-user-owned")

    mine_json = checkout / ".claude" / "settings.json"
    mine_json.parent.mkdir()
    mine_json.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "hooks": [
                                {"type": "command", "command": "/usr/bin/env my-hook"}
                            ]
                        }
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

    changed = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert changed == ()
    assert {path: path.read_bytes() for path in before} == before


def test_user_keys_and_hooks_next_to_managed_hooks_are_preserved(tmp_path: Path):
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
    hook_dir = leader / ".agent-flow" / "scripts" / "hooks"
    document["hooks"]["PostToolUse"][0]["hooks"] = [
        {
            "type": "command",
            "command": (
                "/usr/bin/python3 -I "
                f"{shlex.quote(str(hook_dir / 'comment-checker.py'))}"
            ),
        },
        {
            "type": "command",
            "command": (
                f"/bin/bash {shlex.quote(str(hook_dir / 'guard-protected-branch.sh'))}"
            ),
        },
        {
            "type": "command",
            "command": (
                f"cd '{leader.resolve()}' && '{hook_dir / 'prepare-spec-user-prompt.py'}'"
            ),
        },
        {"type": "command", "command": "./mine"},
    ]
    mine.write_text(json.dumps(document), encoding="utf-8")
    mine.chmod(0o600)

    written = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert json_rel in written
    assert json.loads(mine.read_text(encoding="utf-8")) == {
        "hooks": {
            "PostToolUse": [
                {
                    "hooks": [{"type": "command", "command": "./mine"}],
                }
            ]
        },
        "permissions": {"allow": ["Bash(ls:*)"]},
    }
    assert stat.S_IMODE(mine.stat().st_mode) == 0o600


def test_signature_only_user_omp_extension_is_preserved(tmp_path: Path):
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    json_rel, omp_rel = ".claude/settings.json", ".omp/extensions/agent-flow-hooks.ts"
    (leader / json_rel).write_text(
        _kit_settings_json(leader, "comment-checker.py"), encoding="utf-8"
    )
    checkout = _managed_checkout(leader, "feat-upgrade")

    stale_json = checkout / json_rel
    stale_json.parent.mkdir()
    stale_json.write_text(
        _kit_settings_json(leader, "record-skill-read.py"), encoding="utf-8"
    )
    user_omp = checkout / omp_rel
    user_omp.parent.mkdir(parents=True)
    user_payload = (
        "export default function agentFlowHooks(ctx) { return 'user-owned'; }\n"
    )
    user_omp.write_text(user_payload, encoding="utf-8")

    written = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert json_rel in written
    assert omp_rel not in written
    assert not stale_json.exists()
    assert user_omp.read_text(encoding="utf-8") == user_payload


def test_a_symlinked_registration_target_is_preserved(tmp_path: Path):
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    checkout = _managed_checkout(leader, "feat-symlink")
    outside = tmp_path / "elsewhere.json"
    outside.write_text("{}\n", encoding="utf-8")
    target = checkout / ".claude" / "settings.json"
    target.parent.mkdir()
    target.symlink_to(outside)

    changed = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert changed == ()
    assert target.is_symlink()
    assert outside.read_text(encoding="utf-8") == "{}\n"


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
    settings = json.loads(generated["settings"])
    first_command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    tokens = shlex.split(first_command)
    (leader / ".agent-flow" / "kit.json").write_text(
        json.dumps(
            {
                "hook_runtime": {
                    "python": tokens[0],
                    "launcher_path": str(
                        Path.home() / ".agent-flow" / "bin" / "agent-flow-hook"
                    ),
                    "bootstrap_digest": hashlib.sha256(tokens[3].encode()).hexdigest(),
                }
            }
        ),
        encoding="utf-8",
    )
    return generated["settings"], generated["extension"]


def test_cleanup_ownership_matches_what_installers_actually_generated(
    tmp_path: Path,
):
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    json_rel, omp_rel = ".claude/settings.json", ".omp/extensions/agent-flow-hooks.ts"
    settings, extension = _kit_generated_registrations(leader)
    checkout = _managed_checkout(leader, "feat-real-bytes")
    for rel, generated in ((json_rel, settings), (omp_rel, extension)):
        stale = checkout / rel
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(generated, encoding="utf-8")

    changed = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert set(changed) == {json_rel, omp_rel}
    assert not (checkout / json_rel).exists()
    assert not (checkout / omp_rel).exists()
    backups = tuple(
        (leader / ".agent-flow" / "backups" / "legacy-omp").glob(
            "agent-flow-hooks.ts.removed.*"
        )
    )
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == extension


def test_omp_marker_must_be_the_exact_first_line(tmp_path: Path):
    leader = tmp_path / "leader"
    leader.mkdir()
    rel = ".omp/extensions/agent-flow-hooks.ts"
    user_payload = (
        "export const mine = true;\n"
        "// agent-flow: managed omp extension\n"
        "export default function agentFlowHooks() {}\n"
    ).encode()

    assert not W._host_hook_registration_is_kit_owned(
        leader=leader, rel=rel, payload=user_payload
    )


def test_known_legacy_omp_digest_is_owned(tmp_path: Path, monkeypatch):
    leader = tmp_path / "leader"
    leader.mkdir()

    class LegacyDigest:
        def hexdigest(self) -> str:
            return "7e70b38f3e1c4dff4c4f1a332b5722c51650950b1ce3cfe2349cdf89fd057fab"

    monkeypatch.setattr(W.hashlib, "sha256", lambda _payload: LegacyDigest())

    assert W._host_hook_registration_is_kit_owned(
        leader=leader,
        rel=".omp/extensions/agent-flow-hooks.ts",
        payload=b"historical generated payload",
    )


def _settings_json_with_command(command: str) -> str:
    return json.dumps(
        {
            "hooks": {
                "PostToolUse": [{"hooks": [{"type": "command", "command": command}]}]
            }
        }
    )


def test_a_user_wrapper_around_a_managed_hook_is_preserved(tmp_path: Path):
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    rel = ".claude/settings.json"
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

    changed = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert changed == ()
    assert mine.read_bytes() == before


def test_a_registration_pointing_at_another_installation_is_preserved(
    tmp_path: Path,
):
    leader = tmp_path / "leader"
    _leader_with_host_hooks(leader)
    rel = ".claude/settings.json"
    other = tmp_path / "other-install"
    (other / ".agent-flow" / "scripts" / "hooks").mkdir(parents=True)
    checkout = _managed_checkout(leader, "feat-foreign")
    mine = checkout / rel
    mine.parent.mkdir()
    mine.write_text(_kit_settings_json(other, "comment-checker.py"), encoding="utf-8")
    before = mine.read_bytes()

    changed = provision_host_hook_registrations(leader=leader, checkout=checkout)

    assert changed == ()
    assert mine.read_bytes() == before
