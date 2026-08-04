"""탐지 경로의 fail-open 반증 테스트 (#111, #112, #121~#126).

생성 경로가 아니라 **탐지 경로**를 공격한다. 탐지기가 조용히 빈 값을 돌려주면
그 위에 쌓은 격리 설계 전체가 무의미해지고, 그 실패는 오염이 일어난 뒤에야
드러난다. 그래서 모든 테스트가 falsification pair를 갖는다 — 고친 코드를 원래
형태로 되돌리면 반드시 FAIL하는 케이스가 붙어 있다.
"""
from __future__ import annotations

import os
import sqlite3
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
    probe_provider_leases,
    provider_lease,
)
from agent_flow.providers import subprocess as PROVIDER_PROCESS
from agent_flow.providers.subprocess import ProviderCommand, run_provider
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


@pytest.mark.parametrize("marker", (".agent-flow", ".codex", ".Codex", ".omp"))
def test_provider_accepts_only_recognized_managed_worktree_roots(tmp_path, marker):
    _init_repo(tmp_path)
    linked = tmp_path / marker / "worktrees" / "feat-w"
    linked.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-q", "-b", "feat/w", str(linked), "HEAD", cwd=tmp_path)

    assert PROVIDER_PROCESS._verified_provider_worktree(linked) == linked.resolve()


def test_provider_rejects_registered_worktree_outside_managed_roots(tmp_path):
    _init_repo(tmp_path)
    linked = tmp_path.parent / f"{tmp_path.name}-outside"
    _git("worktree", "add", "-q", "-b", "feat/outside", str(linked), "HEAD", cwd=tmp_path)

    with pytest.raises(
        WorktreeIsolationError,
        match="not a direct child of a managed root",
    ):
        PROVIDER_PROCESS._verified_provider_worktree(linked)



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


def test_invalid_provider_capacity_fails_before_registry_mutation(
    tmp_path, monkeypatch
):
    _init_repo(tmp_path)
    monkeypatch.setenv("AGENT_FLOW_MAX_WORKERS", "unknown")

    with pytest.raises(WorktreeIsolationError):
        with provider_lease(tmp_path):
            pytest.fail("invalid capacity must never acquire")

    assert not (
        tmp_path / ".git" / "agent-flow" / "provider-leases"
    ).exists()


def test_malformed_provider_registry_probes_unknown(tmp_path):
    _init_repo(tmp_path)
    with provider_lease(tmp_path, capacity=1):
        assert probe_provider_leases(tmp_path) == "active"
    registry = (
        tmp_path
        / ".git"
        / "agent-flow"
        / "provider-leases"
        / "registry.json"
    )
    registry.write_text("{not-json", encoding="utf-8")

    assert probe_provider_leases(tmp_path) == "unknown"


def _capture_provider_launches(
    monkeypatch, provider_argv: tuple[str, ...]
) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
    real_popen = subprocess.Popen
    provider_launches: list[tuple[str, ...]] = []
    verifier_launches: list[tuple[str, ...]] = []

    def capture(*args, **kwargs):
        command = args[0] if args else kwargs["args"]
        normalized = (
            (command,)
            if isinstance(command, str)
            else tuple(str(item) for item in command)
        )
        if normalized == provider_argv or normalized[-len(provider_argv) :] == provider_argv:
            provider_launches.append(normalized)
            raise AssertionError("provider command was spawned")
        verifier_launches.append(normalized)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(PROVIDER_PROCESS.subprocess, "Popen", capture)
    return provider_launches, verifier_launches


@pytest.mark.parametrize(
    ("mode", "uid", "expected"),
    (
        (stat.S_IFREG | 0o755, 0, True),
        (stat.S_IFREG | 0o755, 501, False),
        (stat.S_IFREG | 0o775, 0, False),
        (stat.S_IFREG | 0o757, 0, False),
        (stat.S_IFDIR | 0o755, 0, False),
    ),
)
def test_sandbox_backend_requires_trusted_system_executable_identity(
    mode, uid, expected, monkeypatch
):
    class Identity:
        pass

    identity = Identity()
    identity.st_mode = mode
    identity.st_uid = uid

    class Candidate:
        def stat(self):
            return identity

    monkeypatch.setattr(PROVIDER_PROCESS.os, "access", lambda path, flag: True)

    assert PROVIDER_PROCESS._is_trusted_system_executable(Candidate()) is expected


@pytest.mark.parametrize(
    "argv",
    (
        ("codex", "exec", "--cd", "../leader"),
        ("codex", "exec", "-C", "../leader"),
        ("codex-aarch64-apple-darwin", "exec", "--cd", "../leader"),
        ("omp", "-p", "review", "--cwd=../leader"),
    ),
)
def test_provider_cli_rejects_cwd_outside_verified_worktree(tmp_path, argv):
    verified = tmp_path / "worker"
    verified.mkdir()

    with pytest.raises(
        W_ISO.WorktreeIsolationError,
        match="does not match the verified worktree",
    ):
        PROVIDER_PROCESS._assert_provider_cli_cwd(argv, verified.resolve())


@pytest.mark.parametrize(
    "argv",
    (
        ("codex", "exec", "--cd", "."),
        ("codex", "exec", "-C", "."),
        ("codex-aarch64-apple-darwin", "exec", "--cd", "."),
        ("omp", "-p", "review", "--cwd=."),
    ),
)
def test_provider_cli_accepts_verified_worktree_cwd(tmp_path, argv):
    verified = tmp_path / "worker"
    verified.mkdir()

    PROVIDER_PROCESS._assert_provider_cli_cwd(argv, verified.resolve())


@pytest.mark.parametrize(
    ("argv", "provider_kind", "expected_option"),
    (
        (("codex", "exec"), "codex", "--cd"),
        (("omp", "-p", "review"), "omp", "--cwd"),
    ),
)
def test_provider_cli_injects_exact_verified_worktree_cwd(
    tmp_path,
    argv,
    provider_kind,
    expected_option,
):
    verified = (tmp_path / "worker").resolve()
    verified.mkdir()

    bound = PROVIDER_PROCESS._bind_provider_cli_cwd(
        argv,
        verified,
        provider_kind=provider_kind,
    )

    assert bound[:3] == (argv[0], expected_option, str(verified))
    assert bound[3:] == argv[1:]


def test_provider_cli_preserves_verified_explicit_cwd(tmp_path):
    verified = (tmp_path / "worker").resolve()
    verified.mkdir()
    argv = ("codex", "--cd", str(verified), "exec")

    assert PROVIDER_PROCESS._bind_provider_cli_cwd(
        argv,
        verified,
        provider_kind="codex",
    ) == argv


def test_provider_sandbox_writes_only_to_the_verified_worktree(tmp_path):
    linked = _repo_with_linked(tmp_path)
    profile = PROVIDER_PROCESS._macos_sandbox_profile(linked)
    leader = linked.parents[2]

    assert (
        f'(allow file-write* (subpath "{linked.resolve()}"))'
        in profile
    )
    assert (
        f'(allow file-write* (subpath "{leader.resolve()}"))'
        not in profile
    )
    assert (
        f'(deny file-write* (literal "{linked.resolve() / ".git"}"))'
        in profile
    )
    scratch = tmp_path / "provider-scratch"
    scratch.mkdir()
    read_only_profile = PROVIDER_PROCESS._macos_sandbox_profile(
        linked,
        allow_workspace_writes=False,
        scratch=scratch,
    )
    assert (
        f'(allow file-write* (subpath "{linked.resolve()}"))'
        not in read_only_profile
    )
    assert (
        f'(allow file-write* (subpath "{scratch.resolve()}"))'
        in read_only_profile
    )
    # 반증: 이 규칙이 read-only 실행에도 붙어 있으면, workspace가 잠긴 reviewer가
    # 공유 gitdir의 ref를 직접 덮어써 protected branch를 옮길 수 있다. hook 기반
    # commit/push 보호는 파일을 직접 쓰는 이 경로를 보지 못한다.
    #
    # `gitdir`도 같은 분기로 닫힌다. 그쪽은 보안이 아니라 기능 축이다 — index와
    # HEAD가 거기 있어서, 열려 있으면 read-only 계약이 이름만 남는다.
    common = (leader / ".git").resolve()
    worker_gitdir = W_ISO.git_dir(linked)
    assert worker_gitdir is not None
    shared_subpaths = (
        worker_gitdir,
        common / "refs",
        common / "logs",
        common / "objects",
    )
    for shared in shared_subpaths:
        assert (
            f'(allow file-write* (subpath "{shared}"))' not in read_only_profile
        ), f"read-only 실행에 공유 git 쓰기가 열려 있다: {shared}"
    for locked in (common / "packed-refs", common / "packed-refs.lock"):
        assert (
            f'(allow file-write* (literal "{locked}"))' not in read_only_profile
        ), f"read-only 실행에 공유 git 쓰기가 열려 있다: {locked}"
    # 커밋해야 하는 실행에는 그대로 열려 있어야 한다. 이 양성 단언이 없으면 위
    # `not in` 검사가 규칙 문자열 포맷 변경만으로 조용히 공허해진다.
    for shared in shared_subpaths:
        assert f'(allow file-write* (subpath "{shared}"))' in profile, shared
    for locked in (common / "packed-refs", common / "packed-refs.lock"):
        assert f'(allow file-write* (literal "{locked}"))' in profile, locked
    for parent in (Path.home().resolve(), leader.resolve(), linked.resolve()):
        for name in (".claude", ".Codex", ".codex", ".omp", ".agents"):
            assert (
                f'(deny file-write* (subpath "{parent / name}"))'
                in profile
            )
    worker_allow = f'(allow file-write* (subpath "{linked.resolve()}"))'
    worker_state_deny = (
        f'(deny file-write* (subpath "{linked.resolve() / ".claude"}"))'
    )
    assert profile.index(worker_allow) < profile.index(worker_state_deny)
    custom_home = tmp_path / "custom-home"
    custom_profile = PROVIDER_PROCESS._macos_sandbox_profile(
        linked,
        host_home=custom_home,
    )
    assert (
        f'(deny file-write* (subpath "{custom_home.resolve() / ".codex"}"))'
        in custom_profile
    )


def test_provider_state_isolated_in_private_scratch(tmp_path):
    user_home = tmp_path / "user"
    codex_home = user_home / ".codex"
    codex_home.mkdir(parents=True)
    auth = codex_home / "auth.json"
    auth.write_text('{"token":"secret"}\n', encoding="utf-8")
    auth.chmod(stat.S_IRUSR | stat.S_IWUSR)
    scratch = tmp_path / "codex-scratch"
    scratch.mkdir()
    env = {"HOME": str(user_home)}

    PROVIDER_PROCESS._prepare_provider_state(
        argv=("codex", "exec"),
        env=env,
        scratch=scratch,
    )

    isolated_auth = Path(env["CODEX_HOME"]) / "auth.json"
    assert isolated_auth.read_text(encoding="utf-8") == auth.read_text(
        encoding="utf-8"
    )
    assert stat.S_IMODE(isolated_auth.stat().st_mode) == 0o600
    assert isolated_auth.resolve().is_relative_to(scratch.resolve())


def test_provider_state_copy_reads_the_attested_file_descriptor(
    tmp_path, monkeypatch
):
    source = tmp_path / "state"
    source.write_text("attested", encoding="utf-8")
    source.chmod(0o600)
    replacement = tmp_path / "replacement"
    replacement.write_text("replacement", encoding="utf-8")
    replacement.chmod(0o600)
    target = tmp_path / "copy"
    real_open = os.open

    def open_then_replace(path, flags):
        fd = real_open(path, flags)
        if Path(path) == source:
            source.unlink()
            replacement.replace(source)
        return fd

    monkeypatch.setattr(PROVIDER_PROCESS.os, "open", open_then_replace)

    PROVIDER_PROCESS._copy_owned_regular_file(
        source,
        target,
        max_size=1024,
        require_private=True,
    )

    assert target.read_text(encoding="utf-8") == "attested"
    assert source.read_text(encoding="utf-8") == "replacement"


def test_provider_state_copy_stops_when_source_grows_past_limit(
    tmp_path, monkeypatch
):
    source = tmp_path / "state"
    source.write_bytes(b"123456789")
    source.chmod(0o600)
    target = tmp_path / "copy"
    real_fstat = os.fstat

    def stale_fstat(fd):
        identity = list(real_fstat(fd))
        identity[6] = 8
        return os.stat_result(identity)

    monkeypatch.setattr(PROVIDER_PROCESS.os, "fstat", stale_fstat)

    with pytest.raises(WorktreeIsolationError, match="copy limit"):
        PROVIDER_PROCESS._copy_owned_regular_file(
            source,
            target,
            max_size=8,
            require_private=True,
        )

    assert not target.exists()


def test_omp_provider_uses_consistent_private_state_snapshot(tmp_path):
    source = tmp_path / "omp-agent"
    source.mkdir()
    config = source / "config.yml"
    config.write_text("model: test\n", encoding="utf-8")
    config.chmod(stat.S_IRUSR | stat.S_IWUSR)
    for index, name in enumerate(("models.db", "agent.db")):
        connection = sqlite3.connect(source / name)
        connection.execute("CREATE TABLE seed (value INTEGER)")
        connection.execute("INSERT INTO seed VALUES (?)", (index,))
        connection.commit()
        connection.close()
    (source / "models.db").chmod(0o644)
    (source / "agent.db").chmod(0o600)
    scratch = tmp_path / "omp-scratch"
    scratch.mkdir()
    env = {"HOME": str(tmp_path), "PI_CODING_AGENT_DIR": str(source)}

    PROVIDER_PROCESS._prepare_provider_state(
        argv=("omp", "-p"),
        env=env,
        scratch=scratch,
    )

    isolated = Path(env["PI_CODING_AGENT_DIR"])
    assert isolated.resolve().is_relative_to(scratch.resolve())
    assert Path(env["HOME"]).resolve().is_relative_to(scratch.resolve())
    assert (isolated / "config.yml").read_text(encoding="utf-8") == "model: test\n"
    for index, name in enumerate(("models.db", "agent.db")):
        connection = sqlite3.connect(isolated / name)
        try:
            assert connection.execute("SELECT value FROM seed").fetchone() == (index,)
        finally:
            connection.close()
        assert stat.S_IMODE((isolated / name).stat().st_mode) == 0o600


def test_unverified_sandbox_backend_blocks_before_provider_spawn(
    tmp_path, monkeypatch
):
    linked = _repo_with_linked(tmp_path)
    marker = linked / "spawned"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_backend = fake_bin / "sandbox-exec"
    fake_backend.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_backend.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    monkeypatch.setattr("agent_flow.providers.subprocess.sys.platform", "darwin")
    monkeypatch.setattr(
        "agent_flow.providers.subprocess._MACOS_SANDBOX_EXEC",
        fake_backend,
    )
    monkeypatch.setenv(
        "PATH",
        str(fake_bin),
        prepend=PROVIDER_PROCESS.os.pathsep,
    )
    provider_argv = (
        sys.executable,
        "-c",
        f"open({str(marker)!r}, 'w').close()",
    )
    provider_launches, verifier_launches = _capture_provider_launches(
        monkeypatch, provider_argv
    )

    result = run_provider(
        ProviderCommand(name="blocked", argv=provider_argv),
        prompt="",
        cwd=linked,
    )

    assert result.failed is True
    assert result.exit_code is None
    assert "verified macOS sandbox-exec backend is unavailable" in result.stderr
    assert provider_launches == []
    assert verifier_launches == []
    assert not marker.exists()


def test_unsupported_sandbox_backend_blocks_before_provider_spawn(
    tmp_path, monkeypatch
):
    linked = _repo_with_linked(tmp_path)
    monkeypatch.setattr("agent_flow.providers.subprocess.sys.platform", "linux")
    provider_argv = (sys.executable, "-c", "pass")
    provider_launches, verifier_launches = _capture_provider_launches(
        monkeypatch, provider_argv
    )

    result = run_provider(
        ProviderCommand(name="blocked", argv=provider_argv),
        prompt="",
        cwd=linked,
    )

    assert result.failed is True
    assert result.exit_code is None
    assert "no verified provider write-sandbox backend" in result.stderr
    assert provider_launches == []
    assert verifier_launches == []


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="macOS sandbox-exec confinement is required",
)
def test_provider_executable_cannot_resolve_from_the_repository(
    tmp_path, monkeypatch
):
    linked = _repo_with_linked(tmp_path)
    fake = linked / "codex"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    provider_argv = (
        str(fake.resolve()),
        "exec",
        "--cd",
        str(linked.resolve()),
    )
    provider_launches, _ = _capture_provider_launches(
        monkeypatch, provider_argv
    )

    result = run_provider(
        ProviderCommand(
            name="untrusted-provider",
            argv=("codex", "exec", "--cd", str(linked.resolve())),
        ),
        prompt="",
        cwd=linked,
        env={"HOME": str(tmp_path), "PATH": str(linked)},
    )

    assert result.failed is True
    assert "provider executable is not a trusted external regular file" in result.stderr
    assert provider_launches == []
