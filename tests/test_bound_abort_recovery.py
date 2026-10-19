from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parents[1]
INSTALLERS = ("agent-flow-kit.mjs", "agent-flow-install.mjs")
SESSION = "bound-abort-consumer"
UNBOUND_SESSION = "bound-abort-unbound"
_HOST_ENVIRONMENT = frozenset(
    {
        "AGENT_FLOW_PROFILE", "AGENT_FLOW_HOST", "AGENT_FLOW_GENERIC_MODE",
        "AGENT_FLOW_ADAPTER", "CLAUDECODE", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
        "CODEX_SANDBOX", "CODEX_THREAD_ID", "PI_CODING_AGENT_DIR", "OMP_SESSION_ID",
        "NODE_OPTIONS", "PYTHONPATH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
    }
)
_WORKTREE_LINE = re.compile(r"^worktree: (\S+) (.+)$", re.MULTILINE)
_RUN_LINE = re.compile(r"^run: [^/\s]+/(\S+)$", re.MULTILINE)


@dataclass(frozen=True)
class Checkout:
    name: str
    path: Path
    run_dir: Path


@dataclass(frozen=True)
class Consumer:
    leader: Path
    launcher: Path
    own: Checkout
    sibling: Checkout
    environment: dict[str, str]
    scratch: Path

    def lifecycle(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return _run([str(self.launcher), *arguments], cwd=self.leader, env=self.environment)

    def abort_command(self, *arguments: str) -> str:
        return shlex.join(
            [str(self.launcher), "abort", "--root", str(self.leader), *arguments]
        )


@pytest.fixture(scope="module")
def packaged_kit(tmp_path_factory: pytest.TempPathFactory) -> Path:
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("npm is required to pack the consumer kit")
    destination = tmp_path_factory.mktemp("packed")
    packed = subprocess.run(
        [npm, "pack", "--pack-destination", str(destination)],
        cwd=KIT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert packed.returncode == 0, packed.stdout + packed.stderr
    (tarball,) = destination.glob("*.tgz")
    with tarfile.open(tarball) as archive:
        archive.extractall(destination, filter="data")
    return destination / "package"


@pytest.fixture(params=INSTALLERS)
def consumer(request: pytest.FixtureRequest, packaged_kit: Path, tmp_path: Path) -> Consumer:
    scratch = Path(os.path.realpath(tmp_path))
    home = scratch / "home"
    home.mkdir()
    environment = {
        key: value for key, value in os.environ.items() if key not in _HOST_ENVIRONMENT
    }
    environment.update(
        HOME=str(home),
        AGENT_FLOW_NO_UPDATE_CHECK="1",
        AGENT_FLOW_SKIP_CODEX_TRUST="1",
    )
    leader = scratch / "repo"
    leader.mkdir()
    for arguments in (
        ("init", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        _git(leader, *arguments)
    (leader / "README.md").write_text("base\n", encoding="utf-8")
    _git(leader, "add", ".")
    _git(leader, "commit", "-m", "init")
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to run the packaged installer")
    installed = _run(
        [node, str(packaged_kit / "bin" / request.param), "install"],
        cwd=leader,
        env=environment,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    _git(leader, "add", "-A")
    _git(leader, "commit", "-m", "install agent-flow", "--allow-empty")
    launcher = leader / ".agent-flow" / "bin" / "agent-flow"
    assert launcher.is_file() and not launcher.is_symlink()
    run_environment = {
        **environment,
        "AGENT_FLOW_ADAPTER": "generic",
        "AGENT_FLOW_GENERIC_MODE": "stub-success",
    }
    own = _start_run(leader, launcher, run_environment, "own recovery")
    sibling = _start_run(leader, launcher, run_environment, "sibling work")
    return Consumer(
        leader=leader,
        launcher=launcher,
        own=own,
        sibling=sibling,
        environment=run_environment,
        scratch=scratch,
    )


def test_packaged_canonical_abort_recovers_only_its_bound_run(consumer: Consumer) -> None:
    _bind(consumer, consumer.own)
    _assert_bound(consumer)
    before = _state(consumer)

    help_command = shlex.join([str(consumer.launcher), "abort", "--help"])
    for command in (
        help_command,
        consumer.abort_command("--worktree", consumer.own.name, "--help"),
    ):
        decision = _guard(consumer, command)
        assert decision.returncode == 0, (
            "installed guard refused the bound session's own canonical "
            f"abort --help ({command}): {decision.stderr}"
        )
    helped = _run(shlex.split(help_command), cwd=consumer.own.path, env=consumer.environment)
    assert helped.returncode == 0, helped.stderr
    assert "usage" in helped.stdout.lower()
    assert _state(consumer) == before

    readme = consumer.leader / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "leader moved\n", encoding="utf-8")
    drifted = _guard(consumer, f"touch {shlex.quote(str(consumer.own.path / 'probe.txt'))}")
    assert drifted.returncode == 2, "leader drift must keep blocking ordinary commands"

    abort_command = shlex.join([str(consumer.launcher), "abort", "--yes"])
    for command in (
        abort_command,
        consumer.abort_command("--yes"),
        consumer.abort_command("--worktree", consumer.own.name, "--yes"),
    ):
        decision = _guard(consumer, command)
        assert decision.returncode == 0, (
            "installed guard refused the bound session's own canonical "
            f"abort after leader drift ({command}): {decision.stderr}"
        )
    assert _state(consumer) == before
    aborted = _run(shlex.split(abort_command), cwd=consumer.own.path, env=consumer.environment)
    assert aborted.returncode == 0, aborted.stdout + aborted.stderr
    assert f"aborted: {consumer.own.run_dir.name}" in aborted.stdout

    after = _state(consumer)
    own_marker = _relative(consumer, consumer.own.run_dir / "active")
    assert own_marker in before and own_marker not in after
    assert {key: value for key, value in before.items() if key != own_marker} == after
    assert (consumer.sibling.run_dir / "active").is_file()


def test_packaged_abort_rejects_foreign_or_unproven_recovery(consumer: Consumer) -> None:
    _bind(consumer, consumer.own)
    _assert_bound(consumer)
    before = _state(consumer)

    leader = str(consumer.leader)
    launcher = str(consumer.launcher)
    own = consumer.own.name
    sibling = consumer.sibling.name
    own_abort = consumer.abort_command("--worktree", own, "--yes")
    own_help = consumer.abort_command("--worktree", own, "--help")
    pwned = shlex.quote(str(consumer.leader / "pwned.txt"))
    worker_copy = consumer.own.path / "agent-flow-copy"
    shutil.copy2(consumer.launcher, worker_copy)
    linked = consumer.scratch / "agent-flow-link"
    linked.symlink_to(consumer.launcher)
    other_project = consumer.scratch / "other-project"
    other_project.mkdir()

    refused = [
        (consumer.abort_command("--worktree", sibling, "--yes"), consumer.own.path, SESSION),
        (consumer.abort_command("--worktree", sibling, "--help"), consumer.own.path, SESSION),
        (shlex.join([launcher, "abort", "--root", str(consumer.sibling.path), "--yes"]), consumer.own.path, SESSION),
        (shlex.join([launcher, "abort", "--yes"]), consumer.leader, SESSION),
        (shlex.join([launcher, "abort", "--help"]), consumer.sibling.path, SESSION),
        (consumer.abort_command("--worktree", own, "--worktree", sibling), consumer.own.path, SESSION),
        (shlex.join([launcher, "abort", "--root", str(consumer.sibling.path), "--worktree", own, "--yes"]), consumer.own.path, SESSION),
        (consumer.abort_command("--root", leader, "--worktree", own), consumer.own.path, SESSION),
        (consumer.abort_command("--worktree", own, "--run-id", consumer.sibling.run_dir.name), consumer.own.path, SESSION),
        (consumer.abort_command("--worktree", own, "--run-dir", str(consumer.sibling.run_dir)), consumer.own.path, SESSION),
        (consumer.abort_command("--worktree", own, "--path", str(consumer.sibling.path)), consumer.own.path, SESSION),
        (consumer.abort_command("--worktree", own, "--target", str(consumer.sibling.path)), consumer.own.path, SESSION),
        (consumer.abort_command("--work", own), consumer.own.path, SESSION),
        (consumer.abort_command("--worktree", own, "-yh"), consumer.own.path, SESSION),
        (consumer.abort_command("--worktree", own, sibling), consumer.own.path, SESSION),
        (consumer.abort_command("--worktree", own, "--", sibling), consumer.own.path, SESSION),
        (shlex.join([launcher, "abort", "--root", str(other_project), "--worktree", own]), consumer.own.path, SESSION),
        (shlex.join([launcher, "abort", "--root", "..", "--worktree", own]), consumer.own.path, SESSION),
        (shlex.join([launcher, "abort", "--root", ".", "--worktree", own]), consumer.leader, SESSION),
        (own_abort, consumer.leader, SESSION),
        (own_abort, consumer.sibling.path, SESSION),
        (shlex.join([launcher, "worktree", "remove", "--root", leader, "--name", sibling]), consumer.own.path, SESSION),
        (f"{own_help} ; touch {pwned}", consumer.own.path, SESSION),
        (f"{own_help} && touch {pwned}", consumer.own.path, SESSION),
        (f"{own_help} | tee {pwned}", consumer.own.path, SESSION),
        (f"{own_help} > {pwned}", consumer.own.path, SESSION),
        (f"{own_help} & touch {pwned}", consumer.own.path, SESSION),
        (f"{own_help} $(touch {pwned})", consumer.own.path, SESSION),
        (f"{own_help} `touch {pwned}`", consumer.own.path, SESSION),
        (f"{own_help}\ntouch {pwned}", consumer.own.path, SESSION),
        (f"{launcher} abort --root {leader} --worktree \"$AGENT_FLOW_TARGET\"", consumer.own.path, SESSION),
        (f"/bin/sh -c {shlex.quote(own_abort)}", consumer.own.path, SESSION),
        (f"env {own_abort}", consumer.own.path, SESSION),
        (f"command {own_abort}", consumer.own.path, SESSION),
        (shlex.join(["agent-flow", "abort", "--root", leader, "--worktree", own]), consumer.own.path, SESSION),
        (shlex.join([str(worker_copy), "abort", "--root", leader, "--worktree", own]), consumer.own.path, SESSION),
        (shlex.join([str(linked), "abort", "--root", leader, "--worktree", own]), consumer.own.path, SESSION),
        (own_abort, consumer.own.path, UNBOUND_SESSION),
    ]
    for command, cwd, session in refused:
        decision = _guard(consumer, command, cwd=cwd, session=session)
        assert decision.returncode == 2, f"guard opened an unproven recovery: {command!r} from {cwd}"
        assert _state(consumer) == before, f"guard decision changed run state: {command!r}"
    assert not (consumer.leader / "pwned.txt").exists()

    nested = consumer.own.path / "nested"
    nested.mkdir()
    (consumer.own.path / "alias").symlink_to(consumer.own.path, target_is_directory=True)
    (nested / "alias").symlink_to(consumer.sibling.path, target_is_directory=True)
    aliased = shlex.join([launcher, "abort", "--root", leader, "--worktree", "alias", "--yes"])
    decision = _guard(consumer, aliased, cwd=nested, hook_cwd=consumer.own.path)
    assert decision.returncode == 2, "the selector must resolve from the command cwd, not the hook cwd"
    assert _state(consumer) == before

    original = consumer.launcher.read_bytes()
    mode = stat.S_IMODE(consumer.launcher.stat().st_mode)
    consumer.launcher.chmod(mode | stat.S_IWUSR)
    consumer.launcher.write_bytes(original + b"\n# replaced after install\n")
    replaced = _guard(consumer, own_abort)
    consumer.launcher.write_bytes(original)
    consumer.launcher.chmod(mode)
    assert replaced.returncode == 2, "a launcher that no longer matches its install record opened abort"
    assert _state(consumer) == before

    closed = consumer.lifecycle("abort", "--worktree", own, "--yes")
    assert closed.returncode == 0, closed.stdout + closed.stderr
    restarted = consumer.lifecycle("run", "own recovery again", "--worktree", own)
    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    stale = _guard(consumer, own_abort)
    assert stale.returncode == 2, "a binding to a closed run must not open the new run's abort"
    assert (consumer.sibling.run_dir / "active").is_file()


def _start_run(leader: Path, launcher: Path, environment: dict[str, str], task: str) -> Checkout:
    started = _run([str(launcher), "run", task], cwd=leader, env=environment)
    assert started.returncode == 0, started.stdout + started.stderr
    created = _WORKTREE_LINE.search(started.stdout)
    assert created is not None, started.stdout
    name, path = created.group(1), Path(os.path.realpath(created.group(2).strip()))
    status = _run(
        [str(launcher), "status", "--root", str(leader), "--worktree", name],
        cwd=path,
        env=environment,
    )
    assert status.returncode == 0, status.stdout + status.stderr
    run = _RUN_LINE.search(status.stdout)
    assert run is not None, status.stdout
    run_dir = leader / ".git" / "agent-flow" / "worktrees" / name / ".agent-flow" / "runs" / run.group(1)
    assert (run_dir / "active").is_file(), run_dir
    return Checkout(name=name, path=path, run_dir=run_dir)


def _bind(consumer: Consumer, checkout: Checkout) -> None:
    arguments = [
        str(consumer.launcher), "status", "--root", str(consumer.leader),
        "--worktree", checkout.name,
    ]
    status = _run(arguments, cwd=checkout.path, env=consumer.environment)
    assert status.returncode == 0, status.stdout + status.stderr
    payload = {
        "session_id": SESSION,
        "transcript_path": str(consumer.scratch / "transcript.jsonl"),
        "cwd": str(checkout.path),
        "permission_mode": "default",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": shlex.join(arguments), "description": "agent-flow status"},
        "tool_response": {
            "stdout": status.stdout,
            "stderr": status.stderr,
            "interrupted": False,
            "isImage": False,
        },
        "tool_use_id": "bound-abort-status",
    }
    bound = _hook(consumer, "PostToolUse", "bind-host-worktree.py", payload, cwd=checkout.path)
    assert bound.returncode == 0, bound.stdout + bound.stderr


def _assert_bound(consumer: Consumer) -> None:
    own_probe = shlex.quote(str(consumer.own.path / "probe.txt"))
    leader_probe = shlex.quote(str(consumer.leader / "probe.txt"))
    allowed = _guard(consumer, f"touch {own_probe}")
    assert allowed.returncode == 0, f"installed hooks did not bind the session: {allowed.stderr}"
    assert _guard(consumer, f"touch {leader_probe}").returncode == 2


def _guard(
    consumer: Consumer,
    command: str,
    *,
    cwd: Path | None = None,
    session: str = SESSION,
    hook_cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    where = consumer.own.path if cwd is None else cwd
    payload = {
        "session_id": session,
        "transcript_path": str(consumer.scratch / "transcript.jsonl"),
        "cwd": str(where),
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command, "description": "bound abort recovery"},
        "tool_use_id": "bound-abort-guard",
    }
    return _hook(
        consumer, "PreToolUse", "guard-host-worktree.sh", payload, cwd=hook_cwd or where
    )


def _hook(
    consumer: Consumer,
    event: str,
    script: str,
    payload: dict[str, object],
    *,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    settings = json.loads(
        (consumer.leader / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    commands = [
        hook["command"]
        for block in settings["hooks"][event]
        if re.fullmatch(block.get("matcher", ".*"), payload["tool_name"])
        for hook in block["hooks"]
        if script in hook["command"]
    ]
    assert len(commands) == 1, f"{script} is not registered once for {event}: {commands}"
    return subprocess.run(
        ["/bin/sh", "-c", commands[0]],
        input=json.dumps(payload),
        cwd=cwd,
        env=consumer.environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _state(consumer: Consumer) -> dict[str, str]:
    roots = [consumer.own.run_dir.parent, consumer.sibling.run_dir.parent]
    leader_runs = consumer.leader / ".agent-flow" / "runs"
    if leader_runs.is_dir():
        roots.append(leader_runs)
    return {
        _relative(consumer, path): hashlib.sha256(path.read_bytes()).hexdigest()
        for root in roots
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _relative(consumer: Consumer, path: Path) -> str:
    return path.relative_to(consumer.leader).as_posix()


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(("git", *arguments), cwd=cwd, check=True, capture_output=True, text=True)


def _run(
    argv: list[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=300
    )
