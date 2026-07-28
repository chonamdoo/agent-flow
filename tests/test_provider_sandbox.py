"""Policy shape and fail-closed contract, without spawning anything real.

Every guard here has a falsification case: a mutation that would pass if the
guard were absent. A test that only asserts "an exception was raised" cannot
tell a working boundary from a broken one.
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.provider_sandbox import (  # noqa: E402
    SandboxCapability,
    SandboxPolicy,
    SandboxUnavailableError,
    UnboundedSpawn,
    derive_sandbox_policy,
    prove_sandbox,
    require_capability,
    resolve_spawn_argv,
)
from agent_flow.providers.sandbox import UnsupportedPlatformBackend, select_sandbox_backend  # noqa: E402
from agent_flow.providers.seatbelt import SeatbeltBackend, render_profile  # noqa: E402
from agent_flow.providers.subprocess import ProviderCommand, run_provider  # noqa: E402
from agent_flow.subprocess_pool import SubprocessJob  # noqa: E402


class RecordingBackend:
    """Backend that reports availability but records instead of enforcing."""

    name = "recording"

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.wrapped: list[tuple[str, ...]] = []

    def probe(self) -> SandboxCapability:
        return SandboxCapability(self.name, self.available, "" if self.available else "unavailable")

    def wrap(self, argv, *, policy) -> tuple[str, ...]:
        self.wrapped.append(tuple(argv))
        return ("recorded", *argv)


def _policy(tmp_path: Path, **overrides) -> SandboxPolicy:
    leader = tmp_path / "repo"
    worktree = leader / ".agent-flow" / "worktrees" / "feat-a"
    common = leader / ".git"
    (worktree).mkdir(parents=True)
    (common / "worktrees" / "feat-a").mkdir(parents=True)
    kwargs = dict(
        worktree=worktree,
        leader_root=leader,
        git_common_dir=common,
        worktree_git_dir=common / "worktrees" / "feat-a",
        branch="feat/a",
        run_state_dir=common / "agent-flow" / "worktrees" / "feat-a",
        sibling_roots=(),
    )
    kwargs.update(overrides)
    return derive_sandbox_policy(**kwargs)


def test_policy_paths_are_canonical(tmp_path: Path) -> None:
    """SBPL matches resolved paths; an unresolved one silently matches nothing."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    policy = _policy(real, leader_root=link)
    assert policy.protected_roots[0] == Path(os.path.realpath(str(real)))
    assert not str(policy.protected_roots[0]).startswith(str(link))


def test_policy_protects_leader_common_dir_and_siblings(tmp_path: Path) -> None:
    leader = tmp_path / "repo"
    sibling = leader / ".agent-flow" / "worktrees" / "feat-b"
    sibling.mkdir(parents=True)
    policy = _policy(tmp_path, sibling_roots=(sibling,))
    protected = {str(p) for p in policy.protected_roots}
    assert str(Path(os.path.realpath(str(leader)))) in protected
    assert str(Path(os.path.realpath(str(sibling)))) in protected


def test_policy_grants_only_the_worker_branch_ref(tmp_path: Path) -> None:
    """Opening refs/heads as a subtree would let workers overwrite each other."""
    policy = _policy(tmp_path)
    names = [p.name for p in policy.writable_literals]
    # Parents first, then leaves. The `feat` entries are the directories git
    # must create for a hierarchical ref once `pack-refs --prune` removed them;
    # they are literals, so a sibling's `feat/<other>` stays denied.
    assert names == ["feat", "feat", "a", "a.lock", "a", "a.lock"]
    assert not any(str(p).endswith("refs/heads") for p in policy.writable_literals)
    # The directories are undeletable; the leaves are not, because git replaces
    # a ref by renaming its `.lock` over it.
    undeletable = {p.name for p in policy.undeletable}
    assert "feat" in undeletable
    assert "a" not in undeletable and "a.lock" not in undeletable


def test_policy_denies_the_pointer_files_on_both_sides(tmp_path: Path) -> None:
    """`<worktree>/.git` names the admin dir; `gitdir`/`commondir` name it back.

    `objects/info/alternates` is the same kind of file for the object store:
    it names where objects are read from, so a worker that writes it decides
    what history the leader sees.
    """
    policy = _policy(tmp_path)
    assert [p.name for p in policy.protected_literals] == [
        ".git",
        "gitdir",
        "commondir",
        "alternates",
    ]


def test_policy_denies_deletion_inside_the_object_database(tmp_path: Path) -> None:
    """반증: entry만 막으면 worker가 저장소 전체 history를 지울 수 있다.

    `objects`는 worker의 커밋이 들어가야 하므로 write는 열려 있다. 열린
    subtree 안의 삭제까지 열리면 leader와 모든 sibling의 history가 함께
    사라지고, 저장소 안에서 복구할 방법이 없다.
    """
    policy = _policy(tmp_path)
    objects = Path(os.path.realpath(str(tmp_path / "repo"))) / ".git" / "objects"
    assert policy.undeletable_subtrees == (objects,)
    assert objects in policy.writable_subpaths
    # staging 이름만 되돌려 연다. 그게 없으면 loose object의 rename이 막혀
    # 커밋 자체가 죽는다.
    assert policy.transient_globs == (
        f"{objects}/tmp_*",
        f"{objects}/*/tmp_*",
        f"{objects}/maintenance.lock",
    )


def test_policy_reallows_leader_runtime_state(tmp_path: Path) -> None:
    """The tripwire calls these writes legitimate; the kernel must not refuse them."""
    leader = Path(os.path.realpath(str(tmp_path / "repo")))
    writable = {str(p) for p in _policy(tmp_path).writable_subpaths}
    assert str(leader / ".agent-flow" / "runs") in writable
    assert str(leader / ".agent-flow" / "skills-read.jsonl") in writable


def test_policy_does_not_reallow_the_worktrees_directory(tmp_path: Path) -> None:
    """Falsification: reopening it would undo every sibling deny."""
    leader = Path(os.path.realpath(str(tmp_path / "repo")))
    writable = {str(p) for p in _policy(tmp_path).writable_subpaths}
    assert str(leader / ".agent-flow" / "worktrees") not in writable


def test_policy_rejects_relative_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _policy(tmp_path, worktree=Path("relative/worktree"))


def test_profile_orders_deny_allow_deny(tmp_path: Path) -> None:
    """Later SBPL rules win, so ordering is the whole contract."""
    rendered = render_profile(_policy(tmp_path))
    assert rendered.startswith("(version 1)\n(allow default)\n")
    first_deny = rendered.index("(deny file-write*")
    allow = rendered.index("(allow file-write*")
    last_deny = rendered.rindex("(deny file-write*")
    assert first_deny < allow < last_deny


def test_profile_escapes_quotes_in_paths(tmp_path: Path) -> None:
    odd = tmp_path / 'we"ird'
    odd.mkdir()
    rendered = render_profile(_policy(tmp_path, leader_root=odd))
    assert '\\"' in rendered


def test_unsupported_host_fails_closed_before_spawn(tmp_path: Path) -> None:
    """The falsification case: a spawn double that would record a launch."""
    backend = RecordingBackend(available=False)
    with pytest.raises(SandboxUnavailableError):
        prove_sandbox(backend, _policy(tmp_path))
    assert backend.wrapped == []


def test_unsupported_platform_backend_reports_the_platform() -> None:
    capability = UnsupportedPlatformBackend("plan9").probe()
    assert capability.available is False
    assert "plan9" in capability.reason


def test_require_capability_passes_when_available() -> None:
    require_capability(SandboxCapability("x", True))


def test_both_spawn_sites_require_a_boundary(tmp_path: Path) -> None:
    """A forgotten boundary must be a construction error, not an open spawn."""
    with pytest.raises(ValueError):
        SubprocessJob(job_id="j", binary="echo", args=("hi",), cwd=tmp_path)
    with pytest.raises(TypeError):
        run_provider(ProviderCommand(name="p", argv=("echo",)), prompt="", cwd=tmp_path)


def test_spawn_sites_wrap_argv_through_the_backend(tmp_path: Path) -> None:
    backend = RecordingBackend()
    boundary = prove_sandbox(backend, _policy(tmp_path))
    job = SubprocessJob(job_id="j", binary="echo", args=("hi",), cwd=tmp_path, sandbox=boundary)
    assert resolve_spawn_argv(job.sandbox, (job.binary, *job.args)) == ("recorded", "echo", "hi")
    result = run_provider(
        ProviderCommand(name="p", argv=("echo", "hi"), prompt_via_stdin=False),
        prompt="",
        cwd=tmp_path,
        sandbox=boundary,
    )
    assert backend.wrapped == [("echo", "hi"), ("echo", "hi")]
    # "recorded" is not a real binary, so the wrapped argv is what ran.
    assert result.failed


def test_unbounded_spawn_is_explicit_and_passes_argv_through(tmp_path: Path) -> None:
    boundary = UnboundedSpawn("leader checkout")
    assert resolve_spawn_argv(boundary, ("echo", "hi")) == ("echo", "hi")


@pytest.mark.skipif(sys.platform != "darwin", reason="seatbelt is macOS only")
def test_selected_backend_on_macos_is_enforcing() -> None:
    backend = select_sandbox_backend()
    assert isinstance(backend, SeatbeltBackend)
    assert backend.probe().available is True


@pytest.mark.skipif(sys.platform != "darwin", reason="seatbelt is macOS only")
def test_seatbelt_self_test_detects_a_non_enforcing_sandbox_exec(tmp_path: Path, monkeypatch) -> None:
    """If sandbox-exec stops enforcing, probe must say so rather than trust it."""
    fake = tmp_path / "sandbox-exec"
    fake.write_text("#!/bin/sh\nshift 2\nexec \"$@\"\n")
    fake.chmod(0o755)
    backend = SeatbeltBackend()
    monkeypatch.setattr(backend, "_executable", lambda: str(fake))
    capability = backend.probe()
    assert capability.available is False
    assert capability.reason == "protected root was writable"


@pytest.mark.skipif(sys.platform != "darwin", reason="seatbelt is macOS only")
def test_seatbelt_self_test_rejects_a_sandbox_exec_that_refuses_every_profile(
    tmp_path: Path, monkeypatch
) -> None:
    """Falsification: "nothing was written" must not read as "the sandbox denied it".

    An outer sandbox this one cannot nest inside produces exactly this shape,
    so the refusal must name the exit status instead of blaming rule order.
    """
    fake = tmp_path / "sandbox-exec"
    fake.write_text("#!/bin/sh\necho 'sandbox_apply: Operation not permitted' >&2\nexit 71\n")
    fake.chmod(0o755)
    backend = SeatbeltBackend()
    monkeypatch.setattr(backend, "_executable", lambda: str(fake))
    capability = backend.probe()
    assert capability.available is False
    assert capability.reason == "sandbox-exec exited 71: sandbox_apply: Operation not permitted"


@pytest.mark.skipif(sys.platform != "darwin", reason="seatbelt is macOS only")
def test_seatbelt_self_test_detects_a_missing_unlink_group(monkeypatch) -> None:
    """반증: 이 프로브가 쓰기 뒤에 돌면 ENOTEMPTY로 항상 통과한다.

    unlink 규칙이 빠진 프로파일은 반드시 unavailable로 잡혀야 한다. 그렇지
    않으면 grant를 symlink로 바꿔치기하는 길이 열린 채 healthy로 보고된다.
    """
    import agent_flow.providers.seatbelt as module

    intact = module.render_profile
    monkeypatch.setattr(
        module,
        "render_profile",
        lambda policy: intact(dataclasses.replace(policy, undeletable=())),
    )
    capability = SeatbeltBackend().probe()
    assert capability.available is False
    assert capability.reason == "granted directory entry was removable"


def test_seatbelt_wrap_rejects_empty_argv(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SeatbeltBackend().wrap((), policy=_policy(tmp_path))


def test_secure_launch_refuses_to_wrap_agent_flow(tmp_path: Path) -> None:
    """Sandboxing the recovery path is how a workflow deadlocks."""
    result = subprocess.run(
        [sys.executable, "-m", "agent_flow.cli", "secure", "launch", "--root", str(tmp_path),
         "--", "agent-flow", "status"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": SRC},
    )
    assert result.returncode == 2
    assert "refusing to sandbox agent-flow itself" in result.stderr


def test_sbpl_regex_escapes_singly_and_never_crosses_a_slash() -> None:
    """반증: 백슬래시를 두 번 쓰면 규칙이 아무것도 매치하지 않는다.

    `#"..."`는 내용을 정규식 엔진에 그대로 넘긴다. `_sbpl_string`처럼 두
    배로 쓰면 `\\.`가 "백슬래시 다음 아무 문자"가 되어 원래 경로를 놓친다.
    """
    from agent_flow.providers.seatbelt import _sbpl_regex  # noqa: E402

    rendered = _sbpl_regex("/tmp/a.b/objects/*/tmp_*")
    assert rendered == r"^/tmp/a\.b/objects/[^/]*/tmp_[^/]*$"
    assert "\\\\" not in rendered


def test_sbpl_regex_refuses_a_path_it_cannot_express() -> None:
    """따옴표는 sandbox-exec 리터럴을 끝내 버린다. 넓은 규칙보다 거부가 낫다."""
    from agent_flow.providers.seatbelt import _sbpl_regex  # noqa: E402

    with pytest.raises(ValueError):
        _sbpl_regex('/tmp/qu"ote/objects/tmp_*')
    with pytest.raises(ValueError):
        _sbpl_regex("/tmp/new\nline/objects/tmp_*")


@pytest.mark.skipif(sys.platform != "darwin", reason="seatbelt is macOS only")
def test_seatbelt_self_test_detects_a_missing_literal_grant(monkeypatch) -> None:
    """반증: probe가 literal grant를 한 번도 쓰지 않으면 그 규칙군이 미검증이다.

    실제 정책에서 literal grant는 worker branch의 ref 전체다. 그게 빠진
    렌더러는 available=True로 보고되고, worker의 모든 커밋이 ref lock 실패로
    죽는다. capability 거부로 잡혀야 한다.
    """
    import agent_flow.providers.seatbelt as module

    intact = module.render_profile
    monkeypatch.setattr(
        module,
        "render_profile",
        lambda policy: intact(dataclasses.replace(policy, writable_literals=())),
    )
    capability = SeatbeltBackend().probe()
    assert capability.available is False
    assert capability.reason == "rule order not honoured: granted literal was not writable"
