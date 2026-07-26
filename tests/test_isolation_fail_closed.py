"""탐지 경로의 fail-open 반증 테스트 (#111, #112, #121~#126).

생성 경로가 아니라 **탐지 경로**를 공격한다. 탐지기가 조용히 빈 값을 돌려주면
그 위에 쌓은 격리 설계 전체가 무의미해지고, 그 실패는 오염이 일어난 뒤에야
드러난다. 그래서 모든 테스트가 falsification pair를 갖는다 — 고친 코드를 원래
형태로 되돌리면 반드시 FAIL하는 케이스가 붙어 있다.
"""
from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core import worktree_isolation as W_ISO
from agent_flow.core.commands import SafeCommandResult
from agent_flow.core.worktree_isolation import (
    WorktreeIsolationError,
    assert_leader_unchanged,
    capture_leader_snapshot,
    git_repo_state,
    list_registered_worktrees,
)
from agent_flow.core import worktrees as W

_CONTENTION = "fatal: Unable to create '/r/.git/index.lock': File exists."


def _git(*args, cwd):
    return subprocess.run(("git", *args), cwd=str(cwd), capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "f.txt").write_text("base\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "init", cwd=root)


def _failed(stderr: str, returncode: int = 128) -> SafeCommandResult:
    return SafeCommandResult(
        args=("git",), returncode=returncode, stdout="", stderr=stderr
    )


def _fail_git_for(monkeypatch, *, prefix: tuple, result: SafeCommandResult):
    # 전부 실패시키면 어느 관측이 무장 해제됐는지 구분할 수 없다. 지목한 호출만
    # 실패시키고 나머지는 진짜 git에 넘긴다.
    real = W_ISO.git_safe

    def fake(*args, **kwargs):
        if tuple(str(a) for a in args[: len(prefix)]) == prefix:
            return result
        return real(*args, **kwargs)

    monkeypatch.setattr(W_ISO, "git_safe", fake)


def test_fatal_that_is_not_a_missing_repo_stays_unknown(tmp_path):
    """불변: git이 fatal로 죽어도 `.git`이 있으면 non-repo로 접지 않는다.

    exit 128은 "저장소가 아니다"만이 아니라 dubious ownership, 손상된 config,
    권한 오류에도 나온다. 그걸 non-repo로 접으면 스냅샷이 disarmed로 돌아가
    오염 판정자 자체가 사라진다. 여기서는 실제 fatal을 주입한다.
    """
    _init_repo(tmp_path)
    config = tmp_path / ".git" / "config"
    original = config.read_text(encoding="utf-8")
    config.write_text("[core\nbroken", encoding="utf-8")

    assert git_repo_state(tmp_path) == "unknown"
    with pytest.raises(WorktreeIsolationError):
        capture_leader_snapshot(tmp_path)

    config.write_text(original, encoding="utf-8")
    assert git_repo_state(tmp_path) == "repo"


def test_missing_repo_is_proven_by_the_filesystem_not_the_exit_code(tmp_path):
    """반증: 진짜 non-git은 계속 disarmed다. unknown으로 전부 막으면 도구가 죽는다."""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_repo_state(plain) == "non-repo"
    assert capture_leader_snapshot(plain).armed is False


def test_repo_ancestor_blocks_the_non_repo_downgrade(tmp_path):
    """불변: 상위에 `.git`이 있으면 그 아래 어떤 경로도 non-repo로 증명되지 않는다."""
    _init_repo(tmp_path)
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert W_ISO._no_git_dir_above(nested) is False
    assert W_ISO._no_git_dir_above(tmp_path.parent) is True


def test_detached_head_is_stable_and_not_an_error(tmp_path):
    """반증: detached HEAD를 실패로 보면 정상 상태에서 런이 통째로 막힌다.

    `_git_fact`의 non-zero 판정이 빈 stdout까지 실패로 접으면 여기서 걸린다.
    """
    _init_repo(tmp_path)
    _git("checkout", "-q", "--detach", cwd=tmp_path)
    before = capture_leader_snapshot(tmp_path)
    assert before.armed and before.branch == "HEAD"
    assert_leader_unchanged(tmp_path, before)


def test_git_output_locale_is_pinned_and_does_not_leak_to_workers(monkeypatch):
    """불변: 우리가 읽는 git 출력은 사용자 로케일과 무관하게 결정적이다.

    `is_git_lock_contention`이 영문 부분문자열로 판정하므로, 로케일을 고정하지
    않으면 한국어 환경에서 lock 재시도가 아예 발동하지 않는다.
    """
    seen: dict = {}

    def capture(command, *, cwd, env=None, **kwargs):
        seen["env"] = env
        return SafeCommandResult(args=tuple(command), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(W_ISO, "run_safe_command", capture)
    monkeypatch.setenv("LANG", "ko_KR.UTF-8")
    monkeypatch.setenv("LC_ALL", "ko_KR.UTF-8")

    W_ISO.git_safe("status", cwd=".")
    assert seen["env"]["LC_ALL"] == "C"
    assert seen["env"]["LANG"] == "C"
    assert seen["env"]["LANGUAGE"] == ""

    assert W_ISO.sanitized_worker_env()["LC_ALL"] == "ko_KR.UTF-8"


def test_observation_disables_optional_locks_and_mutation_does_not(monkeypatch):
    """불변: 관측만 optional lock을 끈다.

    `GIT_OPTIONAL_LOCKS=0`을 쓰기 명령에 붙이면 git이 실제로 필요한 index 갱신을
    건너뛴다. 기본값은 켜진 상태여야 하고 관측만 명시적으로 끈다.
    """
    seen: list = []

    def capture(command, *, cwd, env=None, **kwargs):
        seen.append(env.get("GIT_OPTIONAL_LOCKS"))
        return SafeCommandResult(args=tuple(command), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(W_ISO, "run_safe_command", capture)
    W_ISO.git_safe("status", cwd=".", optional_locks=False)
    W_ISO.git_safe("worktree", "add", "x", cwd=".")
    assert seen == ["0", None]


def test_tripwire_observation_never_takes_the_index_lock(tmp_path, monkeypatch):
    """불변: 스냅샷이 쓰는 git 호출은 전부 optional lock을 끈 상태로 나간다.

    한 자리라도 빠지면 tripwire가 phase마다 index.lock을 잡아, 같은 common dir에서
    도는 `worktree add`가 tripwire 때문에 실패한다.
    """
    _init_repo(tmp_path)
    flags: list = []
    real = W_ISO.run_safe_command

    def capture(command, *, cwd, env=None, **kwargs):
        flags.append(env.get("GIT_OPTIONAL_LOCKS"))
        return real(command, cwd=cwd, env=env, **kwargs)

    monkeypatch.setattr(W_ISO, "run_safe_command", capture)
    capture_leader_snapshot(tmp_path)
    assert flags and all(flag == "0" for flag in flags)


def test_lock_refuses_to_guess_its_path_in_a_git_repo(tmp_path, monkeypatch):
    """불변: common dir을 못 읽으면 다른 경로로 접지 않고 멈춘다.

    접으면 프로세스마다 다른 lock 파일을 잡아 상호배제가 조용히 사라진다.
    git이 대답 못 할 확률은 경합이 심할 때 가장 높으므로, 가드가 필요한 바로
    그 순간에 꺼진다.
    """
    _init_repo(tmp_path)
    monkeypatch.setattr(W_ISO, "_git_common_dir", lambda path: None)
    with pytest.raises(WorktreeIsolationError):
        with W_ISO.worker_claim_lock(tmp_path):
            pass


def test_leader_and_linked_worktree_share_one_lock_file(tmp_path):
    """불변: 같은 저장소면 어디서 잠그든 같은 파일이다."""
    _init_repo(tmp_path)
    linked = tmp_path / ".agent-flow" / "worktrees" / "feat-x"
    linked.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-q", "-b", "feat/x", str(linked), "HEAD", cwd=tmp_path)

    with W_ISO.worker_claim_lock(linked):
        pass
    assert (tmp_path / ".git" / "agent-flow" / "worker-claim.lock").exists()
    assert not (linked / ".git" / "agent-flow").exists()


def test_unreadable_file_is_distinguishable_from_a_non_target(tmp_path):
    """불변: "읽지 못했다"와 "볼 대상이 아니다"가 다른 값을 남긴다.

    둘 다 ""면 권한 변화가 경로 소실로 보고되고, 정규 파일이 비정규 파일로
    바뀐 것과 관측 실패가 구분되지 않는다.
    """
    secret = tmp_path / "secret.txt"
    secret.write_text("classified\n", encoding="utf-8")
    (tmp_path / "folder").mkdir()

    readable = W_ISO._path_content_stamp(tmp_path, "secret.txt")
    secret.chmod(0o000)
    try:
        unreadable = W_ISO._path_content_stamp(tmp_path, "secret.txt")
    finally:
        secret.chmod(stat.S_IRUSR | stat.S_IWUSR)
    directory = W_ISO._path_content_stamp(tmp_path, "folder")

    assert directory == ""
    assert unreadable not in ("", directory, readable)



def test_registered_worktree_listing_never_degrades_to_empty(tmp_path, monkeypatch):
    """불변: 조회 실패는 "등록된 worktree가 없다"로 강등되지 않는다.

    빈 목록으로 접으면 제거 경로는 지울 게 없다고 판단하고, 소유권 증명은
    통과할 근거 없이 통과한다.
    """
    _init_repo(tmp_path)
    assert [entry.path for entry in list_registered_worktrees(tmp_path)] == [
        W_ISO.real_path(tmp_path)
    ]

    _fail_git_for(
        monkeypatch, prefix=("worktree", "list"), result=_failed("fatal: injected")
    )
    with pytest.raises(WorktreeIsolationError):
        list_registered_worktrees(tmp_path)


def test_worktree_list_parsing_keeps_branch_and_lock_state(tmp_path):
    """불변: 제거 판정이 쓰는 필드(branch/locked)를 파서가 잃지 않는다."""
    _init_repo(tmp_path)
    linked = tmp_path / ".agent-flow" / "worktrees" / "feat-y"
    linked.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-q", "-b", "feat/y", str(linked), "HEAD", cwd=tmp_path)
    _git("worktree", "lock", str(linked), cwd=tmp_path)

    entries = {entry.path: entry for entry in list_registered_worktrees(tmp_path)}
    target = entries[W_ISO.real_path(linked)]
    assert target.branch == "feat/y"
    assert target.locked is True
    assert entries[W_ISO.real_path(tmp_path)].branch == "main"


def test_worktree_path_key_absorbs_symlink_drift(tmp_path):
    """불변: 경로 비교는 symlink 경유 표기를 흡수한다."""
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    assert W_ISO.same_worktree_path(link, target)
    assert not W_ISO.same_worktree_path(target, tmp_path / "other")


def test_removal_refuses_when_the_leader_cannot_be_identified(tmp_path, monkeypatch):
    """불변: leader를 지목하지 못하면 파괴적 제거를 진행하지 않는다.

    "leader가 아님"은 제거의 전제다. 그 전제를 증명하지 못한 채 통과시키면
    가드가 있으나 마나다 — leader 판정 실패가 곧 leader 보호 해제가 된다.
    """
    _init_repo(tmp_path)
    linked = tmp_path / ".agent-flow" / "worktrees" / "feat-z"
    linked.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-q", "-b", "feat/z", str(linked), "HEAD", cwd=tmp_path)
    status = W.get_worktree_status(root=tmp_path, name="z")

    monkeypatch.setattr(W, "leader_worktree_path", lambda root: None)
    with pytest.raises(WorktreeIsolationError):
        W.remove_worktree(root=tmp_path, status=status, allow_unmerged=True)
    assert linked.exists()


def _repo_with_linked(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    linked = tmp_path / ".agent-flow" / "worktrees" / "feat-w"
    linked.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-q", "-b", "feat/w", str(linked), "HEAD", cwd=tmp_path)
    return linked


def test_candidate_list_refuses_when_the_leader_cannot_be_resolved(tmp_path, monkeypatch):
    """불변: leader를 지목하지 못하면 후보 목록 자체를 만들지 않는다.

    leader 제외의 근거는 common dir 하나뿐이다. 그 조회가 실패했을 때 None으로
    강등하면 leader가 그대로 제거 후보로 새어 나간다 — 근거가 하나인 설계에서는
    그 하나가 fail-closed여야 한다.
    """
    _repo_with_linked(tmp_path)
    _fail_git_for(
        monkeypatch,
        prefix=("rev-parse", "--git-common-dir"),
        result=_failed("fatal: injected"),
    )
    monkeypatch.setattr(W, "git_safe", W_ISO.git_safe)
    with pytest.raises(WorktreeIsolationError):
        W.removable_worktrees(root=tmp_path)


def test_leader_exclusion_does_not_depend_on_porcelain_ordering(tmp_path, monkeypatch):
    """불변: main이 첫 항목이 아니어도 판정이 흔들리지 않는다.

    "porcelain은 main을 먼저 낸다"는 git의 관행이지 검증한 사실이 아니다. 그걸
    판정 근거로 쓰면 순서가 흔들릴 때 leader를 놓치는 게 아니라 **엉뚱한
    worktree를 leader로 오인해 제거 불가로 만든다.**
    """
    linked = _repo_with_linked(tmp_path)
    real = W.list_registered_worktrees
    monkeypatch.setattr(
        W, "list_registered_worktrees", lambda root: list(reversed(real(root)))
    )
    paths = {entry.path for entry in W.removable_worktrees(root=tmp_path)}
    assert paths == {W_ISO.real_path(linked)}
