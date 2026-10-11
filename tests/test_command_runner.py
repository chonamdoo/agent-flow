from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from agent_flow.core.command_evidence import COMMANDS_RUN_LOG, read_command_evidence


REPO = Path(__file__).resolve().parents[1]


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / ".agent-flow").mkdir()
    (root / ".agent-flow" / "kit.json").write_text("{}\n", encoding="utf-8")
    (root / ".gitignore").write_text(".agent-flow/\n", encoding="utf-8")
    (root / "logic.py").write_text("value = 0\n", encoding="utf-8")
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
        ("add", "."),
        ("commit", "-m", "baseline"),
    ):
        subprocess.run(("git", *args), cwd=root, check=True, capture_output=True)
    return root


def _run_cli(
    root: Path, *argv: str, session_id: str | None = None,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in (
        "CLAUDECODE", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "OMP_SESSION_ID",
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    ):
        env.pop(key, None)
    env["PYTHONPATH"] = str(REPO / "src")
    env.update(env_overrides or {})
    host_arguments = ("--host-session-id", session_id) if session_id is not None else ()
    return subprocess.run(
        (sys.executable, "-m", "agent_flow.cli", "run-command", *host_arguments, "--", *argv),
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _bind_guidance(root: Path, status, run_dir: Path) -> None:
    from agent_flow.core.host_write_boundary import (
        host_session_guidance,
        record_host_checkout_binding,
    )
    from tests.test_host_write_boundary import _participation_payload, _status_payload

    assert record_host_checkout_binding(_status_payload(root, status, run_dir), root)
    record_host_checkout_binding(_participation_payload(root, status, run_dir), root)
    guidance = host_session_guidance(
        {"session_id": "session-1", "cwd": str(status.path)}, root,
    )
    assert guidance is not None and run_dir.name in guidance


def test_cli_records_failure_without_a_host_hook(tmp_path: Path) -> None:
    root = _project(tmp_path)
    result = _run_cli(
        root,
        sys.executable,
        "-c",
        "import sys; print('observed-failure'); print('stderr-evidence', file=sys.stderr); sys.exit(3)",
    )

    assert result.returncode == 3, result.stderr
    assert "observed-failure" in result.stdout
    assert "stderr-evidence" in result.stderr
    evidence = read_command_evidence(root, cwd_root=root)
    assert len(evidence.runs) == 1
    assert evidence.runs[0].exit_code == 3
    assert evidence.runs[0].cwd == str(root)
    entry = json.loads((root / COMMANDS_RUN_LOG).read_text(encoding="utf-8"))
    assert entry["source"] == "runner"
    assert entry.get("host") is None


def test_runner_records_the_exact_argv_and_pre_execution_baseline(tmp_path: Path) -> None:
    from agent_flow.core.command_evidence import test_code_baseline

    root = _project(tmp_path)
    before = test_code_baseline(root)
    argv = (
        sys.executable,
        "-c",
        "import sys; from pathlib import Path; print(sys.argv[1]); Path('logic.py').write_text('value = 1\\n'); sys.exit(4)",
        "spaces and $literal; shell syntax",
    )
    result = _run_cli(root, *argv)

    assert result.returncode == 4, result.stderr
    assert result.stdout.strip() == argv[-1]
    run, = read_command_evidence(root, cwd_root=root).runs
    assert shlex.split(run.command) == list(argv)
    assert run.code_baseline == before
    assert run.code_baseline != test_code_baseline(root)
    entry = json.loads((root / COMMANDS_RUN_LOG).read_text(encoding="utf-8"))
    assert entry["started_at"] <= entry["at"]


def test_runner_preserves_the_active_run_and_phase_context(tmp_path: Path) -> None:
    from agent_flow.artifact import read_meta, write_meta
    from tests.test_host_write_boundary import _setup

    root, statuses, run_dirs = _setup(tmp_path)
    current = statuses[0]
    run_dir = run_dirs[0]
    meta = read_meta(run_dir)
    meta["current_phase"] = "implement"
    write_meta(run_dir, meta)
    _bind_guidance(root, current, run_dir)

    result = _run_cli(
        current.path,
        sys.executable,
        "-c",
        "from pathlib import Path; from agent_flow.artifact import read_meta, write_meta; "
        f"run = Path({str(run_dir)!r}); meta = read_meta(run); "
        "meta['current_phase'] = 'review'; write_meta(run, meta); raise SystemExit(7)",
        session_id="session-1",
    )

    assert result.returncode == 7, result.stderr
    entry = json.loads((root / COMMANDS_RUN_LOG).read_text(encoding="utf-8"))
    assert entry["cwd"] == str(current.path)
    assert entry["run_id"] == run_dir.name
    assert entry["phase_id"] == "implement"
    assert read_meta(run_dir)["current_phase"] == "review"
    assert not (current.path / COMMANDS_RUN_LOG).exists()


def test_runner_does_not_record_a_command_that_could_not_start(tmp_path: Path) -> None:
    root = _project(tmp_path)
    result = _run_cli(root, str(root / "missing-executable"))

    assert result.returncode == 2
    assert "missing-executable" in result.stderr
    assert not (root / COMMANDS_RUN_LOG).exists()


@pytest.mark.parametrize("session_id", (None, "session-1"))
@pytest.mark.parametrize("protected", ("leader", "sibling", "runtime"))
def test_runner_applies_the_existing_host_boundary_before_execution(
    tmp_path: Path, protected: str, session_id: str | None,
) -> None:
    from agent_flow.core.worktrees import worktree_runtime_root
    from tests.test_host_write_boundary import _setup

    root, statuses, run_dirs = _setup(tmp_path)
    _bind_guidance(root, statuses[0], run_dirs[0])
    owners = {"leader": root, "sibling": statuses[1].path, "runtime": run_dirs[1]}
    protected_root = (
        worktree_runtime_root(root=root, name=statuses[1].name)
        if protected == "runtime" else owners[protected]
    )
    target = owners[protected] / "should-not-be-written.txt"
    result = _run_cli(
        statuses[0].path,
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(target)!r}).write_text('escaped')",
        session_id=session_id,
    )

    assert result.returncode == 2
    assert str(protected_root) in result.stderr
    assert ("not bound" if session_id is None else "outside the bound worktree") in result.stderr
    assert not target.exists()
    assert not (root / COMMANDS_RUN_LOG).exists()


def test_runner_executes_a_bound_sessions_command_in_its_active_checkout(
    tmp_path: Path,
) -> None:
    from tests.test_host_write_boundary import _setup

    root, statuses, run_dirs = _setup(tmp_path)
    current = statuses[0]
    script = current.path / "regression.py"
    script.write_text("print('owned-regression'); raise SystemExit(5)\n", encoding="utf-8")
    _bind_guidance(root, current, run_dirs[0])

    result = _run_cli(current.path, sys.executable, str(script), session_id="session-1")

    assert result.returncode == 5, result.stderr
    assert result.stdout.strip() == "owned-regression"
    entry = json.loads((root / COMMANDS_RUN_LOG).read_text(encoding="utf-8"))
    assert entry["exit_code"] == 5
    assert entry["run_id"] == run_dirs[0].name
    assert entry["cwd"] == str(current.path)
    assert entry["source"] == "runner" and entry.get("host") is None


@pytest.mark.parametrize("invalid", ("missing", "environment", "unknown", "stale", "sibling"))
def test_runner_rejects_an_unbound_or_mismatched_active_session(
    tmp_path: Path, invalid: str,
) -> None:
    from agent_flow.artifact import create_run, mark_inactive
    from agent_flow.core.worktrees import worktree_runtime_root
    from tests.test_host_write_boundary import _setup

    root, statuses, run_dirs = _setup(tmp_path)
    current = statuses[0]
    _bind_guidance(root, current, run_dirs[0])
    session_id: str | None = "session-1"
    env_overrides = {}
    if invalid in {"missing", "environment"}:
        session_id = None
        if invalid == "environment":
            env_overrides = {"CODEX_THREAD_ID": "session-1", "OMP_SESSION_ID": "session-1"}
    elif invalid == "unknown":
        session_id = "session-unknown"
    elif invalid == "stale":
        mark_inactive(run_dirs[0])
        create_run(
            worktree_runtime_root(root=root, name=current.name),
            "default",
            "replacement run",
            run_id="replacement",
            checkout_identity=f"worktree:{current.name}",
            checkout_registration_identity=current.registration_identity,
        )
    elif invalid == "sibling":
        current = statuses[1]
    script = current.path / "must-not-run.py"
    script.write_text("print('unauthorized-child'); raise SystemExit(5)\n", encoding="utf-8")

    result = _run_cli(
        current.path, sys.executable, str(script),
        session_id=session_id, env_overrides=env_overrides,
    )

    assert result.returncode == 2, result.stderr
    assert "unauthorized-child" not in result.stdout
    assert (
        "not bound to an active worktree" if session_id is None
        else "not bound to this checkout's current active run"
    ) in result.stderr
    assert not (root / COMMANDS_RUN_LOG).exists()


def test_runner_records_signal_termination_without_red_evidence(tmp_path: Path) -> None:
    root = _project(tmp_path)
    result = _run_cli(
        root,
        sys.executable,
        "-c",
        "import signal; signal.raise_signal(signal.SIGTERM)",
    )

    assert result.returncode == 128 + signal.SIGTERM, result.stderr
    evidence = read_command_evidence(root, cwd_root=root)
    assert evidence.runs[0].exit_code is None
    assert not evidence.failed(sys.executable)
    entry = json.loads((root / COMMANDS_RUN_LOG).read_text(encoding="utf-8"))
    assert entry["returncode"] == -signal.SIGTERM
    assert entry["signal"] == signal.SIGTERM
    assert not entry["code_baseline"]


def test_runner_does_not_publish_an_interrupted_execution(tmp_path: Path) -> None:
    root = _project(tmp_path)
    result = _run_cli(
        root,
        sys.executable,
        "-c",
        "import os, signal; from pathlib import Path; Path('child.pid').write_text(str(os.getpid())); os.kill(os.getppid(), signal.SIGINT); signal.pause()",
    )

    assert result.returncode != 0
    assert not (root / COMMANDS_RUN_LOG).exists()
    child_pid = int((root / "child.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_unknown_host_observation_does_not_override_a_runner_result(tmp_path: Path) -> None:
    root = _project(tmp_path)
    result = _run_cli(root, sys.executable, "-c", "raise SystemExit(3)")
    assert result.returncode == 3, result.stderr
    with (root / COMMANDS_RUN_LOG).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "command": "agent-flow run-command -- " + sys.executable + " -c 'raise SystemExit(3)'",
            "exit_code": None,
            "cwd": str(root),
            "at": 9999999999,
        }) + "\n")

    evidence = read_command_evidence(root, cwd_root=root)
    assert evidence.failed(sys.executable)
    assert len(evidence.runs) == 2
