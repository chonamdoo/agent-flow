"""새 checkout이 프로젝트 밖의 사용자 전용 중앙 경로에 태어나는지 본다.

leader 안에 두면 IDE가 worktree 작업에 반응해 leader 쪽 캐시를 건드린다. tripwire는
그 변경을 정당하게 오염으로 보고하고 남은 phase를 막으므로, 외부 격리는 유지하면서
저장소별 checkout을 ``~/.agent-flow/worktrees/<repo-id>`` 아래 모은다.

layout을 옮기면 신뢰의 근거도 함께 옮겨야 한다. 외부 경로는 모양만으로 관리형임을
증명할 수 없으므로 생성 자신이 채택 기록을 남긴다.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest

from agent_flow.core.worktrees import (
    _ensure_creation_root,
    _is_managed_child,
    _load_worktree_manifest,
    adopt_worktree,
    attach_worktree,
    create_worktree,
    get_worktree_status,
    known_worktree_names,
    legacy_managed_root,
    managed_worktrees_root,
    sibling_managed_root,
    plan_worktree,
    remove_worktree,
    remove_worktree_metadata,
)
from agent_flow.cli import _resolve_cli_root_context, _verified_checkout_identity
from agent_flow.core.state import _safe_relative_path
from agent_flow.core.worktree_isolation import (
    WorktreeIsolationError,
    adopted_worktree_parent,
    managed_worktree_root,
    same_worktree_path,
    trusted_checkout_paths,
    verify_linked_worktree,
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True, text=True)


def _leader_at(root: Path) -> Path:
    root.mkdir(parents=True)
    _git("init", "-b", "main", ".", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "f.txt").write_text("base\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "init", cwd=root)
    return root


def _leader(tmp_path: Path) -> Path:
    return _leader_at(tmp_path / "myapp")



def test_default_root_uses_the_agent_flow_directory_in_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _leader(tmp_path)
    home = tmp_path / "home"
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    location = managed_worktrees_root(root)

    assert location.parent == home / ".agent-flow" / "worktrees"


def test_created_checkout_lands_in_the_user_state_root(tmp_path: Path):
    root = _leader(tmp_path)

    status = create_worktree(root=root, plan=plan_worktree(root=root, name="slice"))

    expected_parent = Path(os.environ["XDG_STATE_HOME"]) / "worktrees"
    assert status.path.parent == managed_worktrees_root(root)
    assert status.path.parent.parent == expected_parent
    assert status.path.parent.name.startswith("myapp-")
    assert root.resolve() not in status.path.resolve().parents


def test_repository_ids_separate_clones_with_the_same_directory_name(tmp_path: Path):
    first = _leader_at(tmp_path / "first" / "myapp")
    second = _leader_at(tmp_path / "second" / "myapp")

    first_root = managed_worktrees_root(first)
    second_root = managed_worktrees_root(second)

    assert first_root != second_root
    assert first_root.name.startswith("myapp-")
    assert second_root.name.startswith("myapp-")


def test_creation_records_its_own_adoption(tmp_path: Path):
    """외부 중앙 폴더는 모양으로 증명되지 않는다. 생성이 기록을 남겨야 경계가 신뢰한다."""
    root = _leader(tmp_path)

    status = create_worktree(root=root, plan=plan_worktree(root=root, name="slice"))

    assert adopted_worktree_parent(root=root, path=status.path) is not None
    assert managed_worktree_root(root=root, path=status.path) == status.path.parent.resolve()
    assert status.path.resolve() in trusted_checkout_paths(root=root, name=status.name)
    # 기록이 없으면 containment 판정 자체가 실패해야 한다 — 모양은 근거가 아니다.
    assert verify_linked_worktree(root=root, path=status.path) == status.path.resolve()


def test_external_layout_alone_does_not_prove_management(tmp_path: Path):
    """외부 경로는 누구나 만들 수 있다. 모양이 아니라 채택 기록이 근거다.

    모양으로 인정하면 raw `git worktree add`가 미채택 차단을 우회하고, 그 checkout은
    나중에 host write boundary가 소유를 증명하지 못해 run이 막힌다.
    """
    root = _leader(tmp_path)
    created = create_worktree(root=root, plan=plan_worktree(root=root, name="slice"))
    raw = managed_worktrees_root(root) / "feat-raw"
    _git("worktree", "add", "-q", "-b", "feat/raw", str(raw), "main", cwd=root)
    legacy = legacy_managed_root(root) / "feat-old"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-q", "-b", "feat/old", str(legacy), "main", cwd=root)

    # marker 자리는 모양만으로 관리형이다 — 예전 생성 규약이 쓰던 자리다.
    assert _is_managed_child(root=root, path=legacy)
    # 외부 자리는 생성이 남긴 기록으로만 관리형이 된다.
    assert not _is_managed_child(root=root, path=created.path)
    assert not _is_managed_child(root=root, path=raw)
    assert adopted_worktree_parent(root=root, path=created.path) is not None
    assert adopted_worktree_parent(root=root, path=raw) is None


def test_raw_central_checkout_must_be_adopted_before_attach(tmp_path: Path):
    root = _leader(tmp_path)
    raw = managed_worktrees_root(root) / "feat-raw"
    raw.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-q", "-b", "feat/raw", str(raw), "main", cwd=root)

    with pytest.raises(ValueError, match="is not adopted"):
        attach_worktree(root=root, selector="feat-raw")

    adopt_worktree(root=root, path=raw)
    attached = attach_worktree(root=root, selector="feat-raw")
    assert attached is not None and same_worktree_path(attached.path, raw)


def test_names_are_enumerated_from_all_layouts(tmp_path: Path):
    """정리 명령은 현재 중앙 자리와 이전 외부·내부 자리의 대상을 모두 찾는다."""
    root = _leader(tmp_path)
    create_worktree(root=root, plan=plan_worktree(root=root, name="slice"))
    previous = sibling_managed_root(root) / "feat-previous"
    previous.mkdir(parents=True)
    legacy = legacy_managed_root(root) / "feat-legacy"
    legacy.mkdir(parents=True)

    names = known_worktree_names(root=root)

    assert {"feat-slice", "feat-previous", "feat-legacy"} <= set(names)


def test_status_resolves_the_created_checkout(tmp_path: Path):
    root = _leader(tmp_path)
    created = create_worktree(root=root, plan=plan_worktree(root=root, name="slice"))

    status = get_worktree_status(root=root, name="slice")

    assert same_worktree_path(status.path, created.path)
    assert status.branch == "feat/slice"
    assert status.branch_created_by_agent_flow is True


def test_central_root_is_stable_from_a_linked_checkout(tmp_path: Path):
    root = _leader(tmp_path)
    created = create_worktree(root=root, plan=plan_worktree(root=root, name="slice"))

    assert managed_worktrees_root(created.path) == managed_worktrees_root(root)


def test_status_resolves_a_previous_sibling_checkout(tmp_path: Path):
    root = _leader(tmp_path)
    previous = sibling_managed_root(root) / "feat-previous"
    previous.parent.mkdir(parents=True)
    _git("worktree", "add", "-q", "-b", "feat/previous", str(previous), "main", cwd=root)

    status = get_worktree_status(root=root, name="feat-previous")

    assert same_worktree_path(status.path, previous)
    assert status.branch == "feat/previous"


def test_cli_routes_a_home_central_checkout_back_to_its_leader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _leader(tmp_path)
    home = tmp_path / "home"
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(home))
    created = create_worktree(root=root, plan=plan_worktree(root=root, name="slice"))

    config_root, worktree, unadopted = _resolve_cli_root_context(created.path, None)

    assert same_worktree_path(config_root, root)
    assert worktree == created.name
    assert unadopted is None


def test_central_layout_does_not_require_a_writable_leader_parent(tmp_path: Path):
    parent = tmp_path / "ro"
    parent.mkdir()
    root = _leader_at(parent / "myapp")
    parent.chmod(0o500)
    try:
        created = create_worktree(root=root, plan=plan_worktree(root=root, name="slice"))
    finally:
        parent.chmod(0o700)

    assert created.path.parent == managed_worktrees_root(root)
    assert created.path.parent != sibling_managed_root(root)


def test_previous_sibling_root_is_not_selected_or_scanned(tmp_path: Path):
    root = _leader(tmp_path)
    attacker = tmp_path / "attacker"
    decoy = attacker / "feat-decoy"
    decoy.mkdir(parents=True)
    sibling_managed_root(root).symlink_to(attacker)

    created = create_worktree(root=root, plan=plan_worktree(root=root, name="slice"))

    assert created.path.parent == managed_worktrees_root(root)
    assert created.path.parent != sibling_managed_root(root)
    assert "feat-decoy" not in known_worktree_names(root=root)
    assert decoy.is_dir()
    stale = get_worktree_status(root=root, name="feat-decoy")
    assert not same_worktree_path(stale.path, decoy)
    remove_worktree(root=root, status=stale, require_merged=False)
    assert decoy.is_dir()


def test_unsafe_duplicate_layout_does_not_hide_the_safe_legacy_root(tmp_path: Path):
    root = _leader(tmp_path)
    legacy = legacy_managed_root(root)
    (legacy / "feat-legacy").mkdir(parents=True)
    sibling_managed_root(root).symlink_to(legacy)

    assert "feat-legacy" in known_worktree_names(root=root)


def test_creation_refuses_a_symlink_planted_after_path_selection(tmp_path: Path):
    """선택과 생성 사이에 끼워 넣어도 mkdir이 따라가지 않는다."""
    root = _leader(tmp_path)
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    planted = managed_worktrees_root(root)
    planted.symlink_to(attacker)

    with pytest.raises(WorktreeIsolationError, match="unsafe parent"):
        _ensure_creation_root(planted)


def test_creation_refuses_a_symlinked_central_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _leader(tmp_path)
    state_home = tmp_path / "state"
    state_home.mkdir()
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (state_home / "worktrees").symlink_to(attacker)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    with pytest.raises(WorktreeIsolationError, match="unsafe parent"):
        create_worktree(root=root, plan=plan_worktree(root=root, name="slice"))

    assert list(attacker.iterdir()) == []


def test_creation_refuses_a_shared_write_state_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _leader(tmp_path)
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    monkeypatch.setenv("XDG_STATE_HOME", str(shared / "state"))
    try:
        with pytest.raises(WorktreeIsolationError, match="unsafe parent"):
            create_worktree(root=root, plan=plan_worktree(root=root, name="slice"))
    finally:
        shared.chmod(0o700)


def test_legacy_in_checkout_manifest_is_read_and_removed(tmp_path: Path):
    """업그레이드 전 checkout은 예전 자리에 manifest를 들고 있다.

    현재 자리만 보면 그 checkout의 branch ownership과 base를 잃고, 정리 뒤에도
    manifest가 남는다.
    """
    root = _leader(tmp_path)
    legacy = legacy_managed_root(root) / "feat-old"
    legacy.mkdir(parents=True)
    manifest = legacy / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "name": "feat-old",
                "branch": "feat/old",
                "path": str(legacy),
                "branch_created_by_agent_flow": True,
                "base_ref": "main",
            }
        ),
        encoding="utf-8",
    )

    payload = _load_worktree_manifest(root=root, name="feat-old")
    assert payload is not None and payload["branch"] == "feat/old"

    status = get_worktree_status(root=root, name="feat-old")
    assert status.branch_created_by_agent_flow is True

    remove_worktree_metadata(root=root, name="feat-old", path=legacy)
    assert not manifest.exists()


def test_reusing_an_existing_checkout_records_its_adoption(tmp_path: Path):
    """create가 성공했는데 run이 미채택으로 막히면 사용자에게는 이유 없는 차단이다."""
    root = _leader(tmp_path)
    raw = managed_worktrees_root(root) / "feat-x"
    raw.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-q", "-b", "feat/x", str(raw), "main", cwd=root)
    assert adopted_worktree_parent(root=root, path=raw) is None

    reused = create_worktree(root=root, plan=plan_worktree(root=root, name="x"))

    assert same_worktree_path(reused.path, raw)
    assert adopted_worktree_parent(root=root, path=raw) is not None
    assert _verified_checkout_identity(root=root, path=raw) == "worktree:feat-x"


def test_stale_legacy_leftover_is_reported_and_removed_at_its_real_path(tmp_path: Path):
    """예전 자리 잔재를 현재 자리로 보고하면 정리가 헛돌고 목록에 계속 남는다."""
    root = _leader(tmp_path)
    stale = legacy_managed_root(root) / "feat-ghost"
    stale.mkdir(parents=True)

    status = get_worktree_status(root=root, name="feat-ghost")
    assert status.path == stale

    remove_worktree(root=root, status=status, require_merged=False)

    assert not stale.exists()
    assert "feat-ghost" not in known_worktree_names(root=root)


def test_identity_command_refuses_a_recreated_checkout(tmp_path: Path):
    """채택 기록의 지문이 낡으면 identity를 주지 않는다. JS 상태 루트의 authority다."""
    root = _leader(tmp_path)
    created = create_worktree(root=root, plan=plan_worktree(root=root, name="slice"))

    assert _verified_checkout_identity(root=root, path=created.path) == "worktree:feat-slice"
    assert _verified_checkout_identity(root=root, path=root) == "leader"

    _git("worktree", "remove", "--force", str(created.path), cwd=root)
    _git("worktree", "add", "-q", str(created.path), "feat/slice", cwd=root)

    assert _verified_checkout_identity(root=root, path=created.path) is None


def test_recorded_checkout_path_survives_an_agent_flow_component_in_the_project_path(
    tmp_path: Path,
):
    """`.agent-flow`를 포함한 경로에 프로젝트가 있어도 기록이 그 checkout을 가리킨다.

    원본이 아니라 잘라낸 경로로 판정하면 leader 아래의 없는 자리가 기록된다.
    """
    root = tmp_path / ".agent-flow" / "repos" / "app"
    root.mkdir(parents=True)
    checkout = tmp_path / ".agent-flow" / "repos" / "app.worktrees" / "feat-x"
    checkout.mkdir(parents=True)

    recorded = _safe_relative_path(str(checkout), root=root)

    assert not Path(recorded).is_absolute()
    assert (root / recorded).resolve() == checkout.resolve()


def test_recorded_checkout_path_keeps_the_legacy_shape(tmp_path: Path):
    """예전 자리는 예전처럼 `.agent-flow/worktrees/<name>`로 기록된다."""
    root = _leader(tmp_path)
    legacy = legacy_managed_root(root) / "feat-old"
    legacy.mkdir(parents=True)

    assert _safe_relative_path(str(legacy), root=root) == ".agent-flow/worktrees/feat-old"


def test_creation_root_is_owner_only(tmp_path: Path):
    """기본 umask면 0755로 생긴다. 같은 호스트의 다른 사용자가 소스를 읽는다."""
    root = _leader(tmp_path)

    create_worktree(root=root, plan=plan_worktree(root=root, name="slice"))

    creation_root = managed_worktrees_root(root)
    for private_root in (creation_root.parent.parent, creation_root.parent, creation_root):
        assert not private_root.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)


def test_existing_creation_root_is_hardened(tmp_path: Path):
    """예전 버전이 umask 그대로 만든 자리도 다음 생성에서 좁혀진다."""
    root = _leader(tmp_path)
    created = create_worktree(root=root, plan=plan_worktree(root=root, name="slice"))
    creation_root = managed_worktrees_root(root)
    os.chmod(creation_root, 0o755)

    _ensure_creation_root(creation_root)

    assert not creation_root.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    assert created.path.exists()


def test_shared_leader_parent_does_not_change_the_central_layout(tmp_path: Path):
    parent = tmp_path / "shared"
    parent.mkdir()
    root = _leader_at(parent / "myapp")
    os.chmod(parent, 0o777)
    try:
        created = create_worktree(root=root, plan=plan_worktree(root=root, name="slice"))
    finally:
        os.chmod(parent, 0o700)

    assert created.path.parent == managed_worktrees_root(root)
    assert created.path.parent != sibling_managed_root(root)
