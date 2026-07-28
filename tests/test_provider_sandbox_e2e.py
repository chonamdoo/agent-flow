"""Real `sandbox-exec` behaviour against a real git repository.

The assertion is always the sentinel hash, never the exit code. "The command
failed" is what a detection-only guard also produces; "the protected bytes did
not change" is the only evidence that the kernel refused the write.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.core.provider_sandbox import SandboxedSpawn, prove_sandbox  # noqa: E402
from agent_flow.core.worktrees import sandbox_policy_for_worktree  # noqa: E402
from agent_flow.providers.seatbelt import SeatbeltBackend  # noqa: E402
from agent_flow.providers.subprocess import ProviderCommand, run_provider  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="seatbelt is macOS only")


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _real(path: Path) -> Path:
    return Path(os.path.realpath(str(path)))


class Fixture:
    def __init__(self, tmp_path: Path) -> None:
        self.leader = _real(tmp_path) / "repo"
        self.leader.mkdir(parents=True)
        for args in (("init", "-b", "main"), ("config", "user.email", "t@t"), ("config", "user.name", "t")):
            _git(*args, cwd=self.leader)
        (self.leader / "README.md").write_text("leader\n")
        _git("add", "-A", cwd=self.leader)
        _git("commit", "-m", "base", cwd=self.leader)

        worktrees = self.leader / ".agent-flow" / "worktrees"
        worktrees.mkdir(parents=True)
        self.worktree = worktrees / "feat-mine"
        self.sibling = worktrees / "feat-other"
        _git("worktree", "add", "-b", "feat/mine", str(self.worktree), "main", cwd=self.leader)
        _git("worktree", "add", "-b", "feat/other", str(self.sibling), "main", cwd=self.leader)

        self.common = _real(Path(_git("rev-parse", "--git-common-dir", cwd=self.worktree).stdout.strip()))
        self.run_state = self.common / "agent-flow" / "worktrees" / "feat-mine"
        self.run_state.mkdir(parents=True)
        self.sibling_state = self.common / "agent-flow" / "worktrees" / "feat-other"
        self.sibling_state.mkdir(parents=True)
        (self.sibling_state / "manifest.json").write_text("{}\n")
        # leader 쪽에서 워커가 정당하게 쓰는 자리. hook은 여기 append하고
        # 실패해도 OSError를 삼키므로, 막히면 증거 파일만 조용히 멈춘다.
        self.leader_runtime = self.leader / ".agent-flow" / "runs"
        self.leader_runtime.mkdir(parents=True)
        self.outside = _real(tmp_path) / "outside"
        self.outside.mkdir()

    @property
    def protected(self) -> tuple[Path, ...]:
        return (
            self.leader / "README.md",
            self.sibling / "README.md",
            self.common / "config",
            self.common / "HEAD",
            self.sibling_state / "manifest.json",
            self.worktree / ".git",
        )

    def digest(self) -> dict[str, str]:
        out = {}
        for path in self.protected:
            out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        out["leader-head"] = _git("rev-parse", "HEAD", cwd=self.leader).stdout.strip()
        out["leader-status"] = _git("status", "--porcelain", cwd=self.leader).stdout
        return out

    def boundary(self) -> SandboxedSpawn:
        return prove_sandbox(
            SeatbeltBackend(),
            sandbox_policy_for_worktree(
                root=self.leader, worktree_path=self.worktree, name="feat-mine", branch="feat/mine"
            ),
        )

    def run(self, script: str, env: dict | None = None):
        return run_provider(
            ProviderCommand(name="probe", argv=("/bin/sh", "-c", script), prompt_via_stdin=False),
            prompt="",
            cwd=self.worktree,
            env=env,
            sandbox=self.boundary(),
        )


@pytest.fixture()
def repo(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def _codes(stdout: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)


def test_spawn_tree_is_bound_to_verified_worktree(repo: Fixture) -> None:
    """The child and its own children start in the worktree and inherit the boundary."""
    before = repo.digest()
    result = repo.run(
        f'pwd; /bin/sh -c \'echo nested > nested.txt; echo "child_own=$?"; '
        f'echo x > "{repo.leader}/README.md" 2>/dev/null; echo "child_leader=$?"\''
    )
    codes = _codes(result.stdout)
    assert result.stdout.splitlines()[0] == str(repo.worktree)
    assert codes["child_own"] == "0"
    assert codes["child_leader"] != "0"
    assert (repo.worktree / "nested.txt").read_text() == "nested\n"
    assert repo.digest() == before


def test_worker_and_run_state_writes_are_allowed(repo: Fixture) -> None:
    before = repo.digest()
    result = repo.run(
        f'echo src > feature.txt; echo "worktree=$?"; '
        f'echo art > "{repo.run_state}/design.md"; echo "run_state=$?"; '
        f'echo log > "{repo.leader_runtime}/marker.txt"; echo "leader_runtime=$?"; '
        f'echo read >> "{repo.leader}/.agent-flow/skills-read.jsonl"; echo "skills_read=$?"; '
        f'git add -A >/dev/null 2>&1; echo "add=$?"; '
        f'git -c user.email=w@w -c user.name=w commit -q -m worker >/dev/null 2>&1; echo "commit=$?"'
    )
    codes = _codes(result.stdout)
    assert codes == {
        "worktree": "0",
        "run_state": "0",
        "leader_runtime": "0",
        "skills_read": "0",
        "add": "0",
        "commit": "0",
    }, result.stderr
    assert (repo.run_state / "design.md").read_text() == "art\n"
    assert "worker" in _git("log", "--oneline", "-1", "feat/mine", cwd=repo.leader).stdout
    assert repo.digest() == before


def test_worker_commits_after_refs_have_been_packed(repo: Fixture) -> None:
    """반증: leaf ref만 열면 `git gc` 한 번에 모든 worker 커밋이 죽는다.

    `pack-refs --prune`가 빈 `refs/heads/feat/`를 지우고 나면 git은 그 디렉터리를
    다시 만들어야 하는데, 그 mkdir도 보호 구역 안의 쓰기다.
    """
    assert _git("pack-refs", "--all", "--prune", cwd=repo.leader).returncode == 0
    assert not (repo.common / "refs" / "heads" / "feat").exists()
    before = repo.digest()
    result = repo.run(
        'echo work > packed.txt; git add -A >/dev/null 2>&1; '
        'git -c user.email=w@w -c user.name=w commit -q -m packed >/dev/null 2>&1; echo "commit=$?"'
    )
    assert _codes(result.stdout) == {"commit": "0"}, result.stderr
    assert "packed" in _git("log", "--oneline", "-1", "feat/mine", cwd=repo.leader).stdout
    assert repo.digest() == before


def test_granted_directories_cannot_be_swapped_for_a_symlink(repo: Fixture) -> None:
    """반증: subpath grant는 그 디렉터리 엔트리 자체까지 연다.

    엔트리를 지우고 같은 이름의 symlink를 걸면, 다음에 그 안으로 쓰는 신뢰된
    프로세스가 대신 leader를 쓴다. 두 단계지만 경계를 통째로 넘는 길이다.
    """
    before = repo.digest()
    runs = repo.leader_runtime.parent
    result = repo.run(
        f'rmdir "{repo.leader_runtime}" 2>/dev/null; echo "rmdir_grant=$?"; '
        f'mv "{runs}" "{runs}.x" 2>/dev/null; echo "rename_grant=$?"; '
        f'rm -rf "{repo.worktree}/.git" 2>/dev/null; echo "rm_admin=$?"; '
        f'echo still > "{repo.leader_runtime}/inside.txt"; echo "write_inside=$?"'
    )
    codes = _codes(result.stdout)
    assert codes["rmdir_grant"] != "0"
    assert codes["rename_grant"] != "0"
    assert codes["rm_admin"] != "0"
    # 안쪽 쓰기는 그대로 열려 있어야 한다. 아니면 unlink 차단이 grant를 무효화한 것이다.
    assert codes["write_inside"] == "0", result.stderr
    assert repo.leader_runtime.is_dir() and runs.is_dir()
    assert (repo.worktree / ".git").exists()
    assert repo.digest() == before



def test_worker_cannot_empty_the_shared_object_database(repo: Fixture) -> None:
    """반증: subtree grant는 그 안의 삭제까지 연다.

    `objects`는 worker의 커밋이 들어가야 하므로 write가 열려 있다. 삭제까지
    열리면 leader와 모든 sibling의 history가 한 번에 사라지고, 저장소 안에는
    되돌릴 방법이 없다. tripwire는 바이트가 사라진 뒤에야 운다.
    """
    objects = repo.common / "objects"
    before = sorted(p.name for p in objects.rglob("*"))
    assert before, "fixture must have objects to lose"
    result = repo.run(
        f'rm -rf "{objects}" 2>/dev/null; echo "wipe=$?"; '
        f'find "{objects}" -type f -path "*/??/*" | head -1 | xargs rm -f 2>/dev/null; echo "one=$?"; '
        f'echo hijack > "{objects}/info/alternates" 2>/dev/null; echo "alternates=$?"'
    )
    codes = _codes(result.stdout)
    assert codes["wipe"] != "0"
    assert codes["one"] != "0"
    assert codes["alternates"] != "0"
    assert sorted(p.name for p in objects.rglob("*")) == before
    assert not (objects / "info" / "alternates").exists()


def test_worker_cannot_overwrite_objects_in_place(repo: Fixture) -> None:
    """반증: unlink만 막으면 지우는 대신 0바이트로 덮어써서 같은 손실을 낸다.

    pack 하나를 잘라내면 그 안의 모든 object가 사라진다. 삭제와 결과가
    같으므로 같은 subtree에서 두 verb를 함께 닫는다.
    """
    objects = repo.common / "objects"
    assert _git("gc", "-q", cwd=repo.leader).returncode == 0
    pack = next(iter((objects / "pack").glob("*.pack")))
    size = pack.stat().st_size
    assert size > 0
    result = repo.run(
        f'chmod 644 "{pack}" 2>/dev/null; echo "chmod=$?"; '
        # 서브셸: 리다이렉션 실패는 비대화형 셸을 그 자리에서 끝내므로
        # 밖에서 하면 다음 echo가 아예 실행되지 않는다.
        f'( : > "{pack}" ) 2>/dev/null; echo "truncate=$?"'
    )
    codes = _codes(result.stdout)
    assert codes["chmod"] != "0"
    assert codes["truncate"] != "0"
    assert pack.stat().st_size == size
    assert _git("fsck", "--no-progress", cwd=repo.leader).returncode == 0



def test_worker_leaves_no_staging_litter_in_the_object_database(repo: Fixture) -> None:
    """반증: staging 이름을 되돌려 열지 않으면 커밋마다 찌꺼기가 쌓인다.

    git은 loose object를 `objects/<fanout>/tmp_obj_*`에 쓰고 제자리로 옮긴다.
    옮기는 방식이 link+unlink라 subtree unlink를 막아도 커밋은 통과한다 —
    대신 staging 파일이 그대로 남고, 이걸 치울 수 있는 건 이 정책이 막아 둔
    `gc`뿐이다. 그래서 rc가 아니라 남은 찌꺼기와 경고로 검사한다.
    """
    objects = repo.common / "objects"
    result = repo.run(
        'echo loose > loose.txt; git add -A >/dev/null 2>&1; '
        'git -c user.email=w@w -c user.name=w commit -q -m loose; echo "commit=$?"'
    )
    assert _codes(result.stdout) == {"commit": "0"}, result.stderr
    assert "unable to unlink" not in result.stderr
    assert sorted(p.name for p in objects.rglob("tmp_*")) == []
    assert "loose" in _git("log", "--oneline", "-1", "feat/mine", cwd=repo.leader).stdout


def test_out_of_repo_writes_remain_allowed(repo: Fixture) -> None:
    """Build output and scratch work must not need a policy change.

    `/tmp` on purpose: it is the `/private/tmp` symlink case that path
    canonicalisation exists for. The name carries the pid so two concurrent
    runs cannot race on it.
    """
    before = repo.digest()
    scratch = Path(f"/tmp/agent-flow-sandbox-probe-{os.getpid()}.txt")
    try:
        result = repo.run(
            f'echo apk > "{repo.outside}/app.apk"; echo "outside=$?"; '
            f'echo tmp > "{scratch}"; echo "tmp=$?"'
        )
        assert _codes(result.stdout) == {"outside": "0", "tmp": "0"}, result.stderr
        assert (repo.outside / "app.apk").read_text() == "apk\n"
        assert scratch.read_text() == "tmp\n"
    finally:
        scratch.unlink(missing_ok=True)
    assert repo.digest() == before


def test_protected_roots_stay_byte_identical(repo: Fixture) -> None:
    before = repo.digest()
    result = repo.run(
        f'echo x > "{repo.leader}/README.md" 2>/dev/null; echo "leader=$?"; '
        f'echo x > "{repo.sibling}/README.md" 2>/dev/null; echo "sibling=$?"; '
        f'echo x > "{repo.common}/packed-refs" 2>/dev/null; echo "packed_refs=$?"; '
        f'echo x > "{repo.common}/config" 2>/dev/null; echo "config=$?"; '
        f'echo x > "{repo.common}/HEAD" 2>/dev/null; echo "common_head=$?"; '
        f'echo x > "{repo.sibling_state}/manifest.json" 2>/dev/null; echo "sibling_state=$?"; '
        f'echo x > "{repo.worktree}/.git" 2>/dev/null; echo "gitdir_pointer=$?"; '
        f'git -C "{repo.leader}" -c user.email=w@w -c user.name=w commit -q --allow-empty '
        f'-m pwn >/dev/null 2>&1; echo "leader_commit=$?"'
    )
    codes = _codes(result.stdout)
    # The hash is the claim. The exit codes are diagnostics for a failure.
    assert repo.digest() == before, codes
    assert all(code != "0" for code in codes.values()), codes
    assert not (repo.common / "packed-refs").exists()


def test_boundary_holds_without_tripwire_or_hooks(repo: Fixture) -> None:
    """Enforcement must not depend on any agent-flow guard running."""
    before = repo.digest()
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_FLOW")}
    env["AGENT_FLOW_SKIP_CODEX_TRUST"] = "1"
    result = repo.run(
        f'echo x > "{repo.leader}/README.md" 2>/dev/null; echo "leader=$?"; '
        f'echo x > "{repo.sibling}/README.md" 2>/dev/null; echo "sibling=$?"',
        env=env,
    )
    assert repo.digest() == before
    assert all(code != "0" for code in _codes(result.stdout).values())


def test_worker_cannot_widen_its_own_sandbox(repo: Fixture) -> None:
    """A nested permissive profile must not reopen the leader."""
    before = repo.digest()
    result = repo.run(
        f"sandbox-exec -p '(version 1)(allow default)' /bin/sh -c "
        f'\'echo x > "{repo.leader}/README.md"\' 2>/dev/null; echo "nested=$?"; '
        f'/usr/bin/python3 -c \'open("{repo.leader}/README.md","w").write("x")\' '
        f'2>/dev/null; echo "other_runtime=$?"'
    )
    assert repo.digest() == before
    assert all(code != "0" for code in _codes(result.stdout).values())
