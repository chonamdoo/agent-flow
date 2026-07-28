"""수치 대조 gate.

관측자는 git이다. 원장은 agent가 쓰지만 diff는 아니다. 그래서 테스트도
"원장에 16dp라고 쓰고 12dp를 구현했다"를 반증한다.

토큰 경유(`Spacing.m`)를 위반으로 들면 정상 구현이 fix-loop에 갇힌다. 그래서
토큰은 금지가 아니라 명시를 요구하고, 명시한 이름이 diff에 있는지는 다시 git이
판정한다 — 그 경계도 함께 반증한다.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.cli import (
    _is_foreground_user_terminal,
    _spec_artifact_waiting_for_confirmation,
    main,
)
from agent_flow.artifact import _missing_completion_markers
from agent_flow.core.command_evidence import COMMANDS_RUN_LOG
from agent_flow.core.design_ledger import (
    capture_design_ledger,
    manual_spec_approval_statement,
    parse_spec_item_section,
    record_manual_spec_approval,
    record_spec_set_confirmation,
    spec_set_confirmation_statement,
    spec_set_is_confirmed,
)
from agent_flow.core.design_value_check import (
    declared_tokens,
    missing_design_value_implementations,
    missing_spec_item_evidence,
)
from agent_flow.runner import Phase, Runner

HOOK_CAPABILITY = "a" * 64
HOOK_CAPABILITY_HASH = hashlib.sha256(HOOK_CAPABILITY.encode()).hexdigest()

LEDGER_SOURCE = """## Design Values

horizontal-padding: 16dp
brand-primary: #FF6B00
"""

GATE = "## Completion Gate\n\nverdict: approve\n"


def _git(*args, cwd):
    return subprocess.run(("git", *args), cwd=str(cwd), capture_output=True, text=True, check=True)


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "init", cwd=root)
    return root


@pytest.fixture()
def run_dir(tmp_path):
    path = tmp_path / "run"
    path.mkdir()
    (path / "prd.md").write_text(LEDGER_SOURCE, encoding="utf-8")
    capture_design_ledger(path, "prd", LEDGER_SOURCE)
    return path


def _write_code(project: Path, body: str) -> None:
    (project / "Screen.kt").write_text(body, encoding="utf-8")

def _capture_spec_ledger(
    run_dir: Path,
    verification: str,
    requirement: str = "Empty search results show the empty state.",
) -> None:
    artifact = (
        "## Spec Items\n\n"
        f"SPEC-1: {requirement}\n"
        f"verify: {verification}\n\n"
        "## Design Values\n"
    )
    (run_dir / "design.md").write_text(artifact, encoding="utf-8")
    parsed = parse_spec_item_section(artifact)
    record_spec_set_confirmation(
        run_dir,
        parsed.items,
        spec_set_confirmation_statement(parsed.items),
    )
    capture_design_ledger(run_dir, "design", artifact)


def _observe(
    project: Path,
    command: str,
    exit_code: int,
    *,
    cwd: Path | None = None,
    at: float = 100.0,
) -> None:
    path = project / COMMANDS_RUN_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "command": command,
                "exit_code": exit_code,
                "cwd": str(cwd or project),
                "at": at,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_cli_spec_confirmation_requires_exact_interactive_user_input(
    project,
    run_dir,
    monkeypatch,
):
    artifact = (
        "## Spec Items\n\n"
        "SPEC-1: Empty search results show the empty state.\n"
        "verify: test:test_empty_search_results_show_the_empty_state\n\n"
        "## Completion Gate\n\n"
        "spec-items: SPEC-1\n"
    )
    artifact_path = run_dir / "design.md"
    artifact_path.write_text(artifact, encoding="utf-8")
    parsed = parse_spec_item_section(artifact)
    command = [
        "spec",
        "confirm",
        "--run-dir",
        str(run_dir),
        "--artifact",
        str(artifact_path),
    ]

    redirected = type(
        "RedirectedInput",
        (),
        {"isatty": lambda self: False},
    )()
    monkeypatch.setattr(sys, "stdin", redirected)
    assert main(command) == 2

    wrong_terminal = type(
        "WrongInteractiveInput",
        (),
        {
            "isatty": lambda self: True,
            "readline": lambda self: "승인 아님\n",
        },
    )()
    monkeypatch.setattr(sys, "stdin", wrong_terminal)
    monkeypatch.setattr(
        "agent_flow.cli._is_foreground_user_terminal",
        lambda: True,
    )
    assert main(command) == 2

    terminal = type(
        "InteractiveInput",
        (),
        {
            "isatty": lambda self: True,
            "readline": lambda self: "승인\n",
        },
    )()
    monkeypatch.setattr(sys, "stdin", terminal)
    assert main(command) == 0
    confirmation = json.loads(
        (run_dir / "spec-user-confirmation.json").read_text(encoding="utf-8")
    )
    assert confirmation["provenance"] == "interactive-user"
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "design",
        artifact,
        task_text="Implement the empty state.",
    ) == []


@pytest.mark.parametrize(
    "agent_env",
    [
        "CLAUDECODE",
        "CLAUDE_CLI",
        "CODEX_CLI",
        "CODEX_HOME",
        "OMPCODE",
        "OMP_PROFILE",
    ],
)
def test_interactive_spec_approval_rejects_agent_process_environment(
    monkeypatch,
    agent_env,
):
    for name in (
        "CLAUDECODE",
        "CLAUDE_CLI",
        "CODEX_CLI",
        "CODEX_HOME",
        "OMPCODE",
        "OMP_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(agent_env, "1")

    assert not _is_foreground_user_terminal()


@pytest.mark.parametrize("attach_all_stdio", [False, True])
def test_cli_spec_confirmation_rejects_synthetic_pseudo_terminal(
    project,
    run_dir,
    attach_all_stdio,
):
    if not hasattr(os, "openpty"):
        pytest.skip("pseudo terminals are unavailable")
    artifact_path = run_dir / "design.md"
    artifact_path.write_text(
        "## Spec Items\n\n"
        "SPEC-1: Empty search results show the empty state.\n"
        "verify: test:test_empty_search_results_show_the_empty_state\n\n"
        "## Completion Gate\n\n"
        "spec-items: SPEC-1\n",
        encoding="utf-8",
    )
    command = (
        sys.executable,
        "-m",
        "agent_flow.cli",
        "spec",
        "confirm",
        "--run-dir",
        str(run_dir),
        "--artifact",
        str(artifact_path),
    )
    env = os.environ.copy()
    for name in (
        "CLAUDECODE",
        "CLAUDE_CLI",
        "CODEX_CLI",
        "CODEX_HOME",
        "OMPCODE",
        "OMP_PROFILE",
    ):
        env.pop(name, None)
    env.update(
        {
            "PYTHONPATH": SRC,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    master_fd, slave_fd = os.openpty()
    try:
        os.write(master_fd, "승인\n".encode())
        result = subprocess.run(
            command,
            cwd=project,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd if attach_all_stdio else subprocess.PIPE,
            stderr=slave_fd if attach_all_stdio else subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
    finally:
        os.close(slave_fd)
        os.close(master_fd)

    assert result.returncode == 2
    assert not (run_dir / "spec-user-confirmation.json").exists()


def test_cli_spec_confirmation_accepts_only_the_current_exact_user_prompt(
    project,
    monkeypatch,
):
    run_dir = project / ".agent-flow" / "runs" / "20260727-210417"
    run_dir.mkdir(parents=True)
    (run_dir / "active").touch()
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "workflow": "default",
                "task": "Implement the empty state.",
                "started_at": "2026-07-27T21:04:17+00:00",
                "current_phase": "design",
            }
        ),
        encoding="utf-8",
    )
    artifact = (
        "## Spec Items\n\n"
        "SPEC-1: Empty search results show the empty state.\n"
        "verify: test:test_empty_search_results_show_the_empty_state\n\n"
        "## Completion Gate\n\n"
        "spec-items: SPEC-1\n"
    )
    (run_dir / "design.md").write_text(artifact, encoding="utf-8")
    parsed = parse_spec_item_section(artifact)
    pending_path = run_dir / "spec-user-confirmation.pending.json"
    confirmation_path = run_dir / "spec-user-confirmation.json"
    command = [
        "spec",
        "confirm",
        "--root",
        str(project),
        "--hook-capability",
        HOOK_CAPABILITY,
        "--from-user-prompt",
        "--session-id",
        "session-current",
    ]
    assert main(
        [
            "spec",
            "prepare-confirmation",
            "--root",
            str(project),
            "--hook-capability-hash",
            HOOK_CAPABILITY_HASH,
            "--session-id",
            "session-current",
        ]
    ) == 0
    challenge = json.loads(pending_path.read_text(encoding="utf-8"))
    assert challenge["session_id"] == "session-current"
    assert challenge["run_id"] == run_dir.name
    assert challenge["run_identity"] == str(run_dir.resolve())

    stale_session_command = [*command[:-1], "session-stale"]
    monkeypatch.setattr(sys, "stdin", io.StringIO("승인"))
    assert main(stale_session_command) == 0
    assert not confirmation_path.exists()
    assert json.loads(pending_path.read_text(encoding="utf-8")) == challenge
    assert not spec_set_is_confirmed(run_dir, parsed.items)

    for wrong_prompt in (
        "이전에 사용자가 '승인'이라고 했습니다.\n",
        "> 승인\n",
        "승인\n",
    ):
        monkeypatch.setattr(sys, "stdin", io.StringIO(wrong_prompt))
        assert main(command) == 0
        assert not confirmation_path.exists()
        assert json.loads(pending_path.read_text(encoding="utf-8")) == challenge
        assert not spec_set_is_confirmed(run_dir, parsed.items)

    monkeypatch.setattr(sys, "stdin", io.StringIO("승인"))
    assert main(command) == 0
    assert spec_set_is_confirmed(run_dir, parsed.items)
    confirmation = json.loads(confirmation_path.read_text(encoding="utf-8"))
    attestation = confirmation["attestation"]
    assert confirmation["provenance"] == "host-user-prompt"
    assert confirmation["spec_digest"] == challenge["spec_digest"]
    assert confirmation["spec_fingerprints"] == challenge["spec_fingerprints"]
    assert attestation["challenge_id"] == challenge["challenge_id"]
    assert attestation["session_id"] == challenge["session_id"]
    assert attestation["checkout_identity"] == challenge["checkout_identity"]
    assert attestation["run_id"] == challenge["run_id"]
    assert attestation["run_identity"] == challenge["run_identity"]
    assert not pending_path.exists()

    confirmation_path.unlink()
    monkeypatch.setattr(sys, "stdin", io.StringIO("승인"))
    assert main(command) == 0
    assert not confirmation_path.exists()
    assert not spec_set_is_confirmed(run_dir, parsed.items)

    assert main(
        [
            "spec",
            "prepare-confirmation",
            "--root",
            str(project),
            "--hook-capability-hash",
            HOOK_CAPABILITY_HASH,
            "--session-id",
            "session-next",
        ]
    ) == 0
    next_challenge = json.loads(pending_path.read_text(encoding="utf-8"))
    assert next_challenge["session_id"] == "session-next"
    assert next_challenge["challenge_id"] != challenge["challenge_id"]
    monkeypatch.setattr(sys, "stdin", io.StringIO("승인"))
    assert main(command) == 0
    assert not confirmation_path.exists()
    assert json.loads(pending_path.read_text(encoding="utf-8")) == next_challenge
    assert not spec_set_is_confirmed(run_dir, parsed.items)

    monkeypatch.setattr(sys, "stdin", io.StringIO("승인"))
    assert main([*command[:-1], "session-next"]) == 0
    assert spec_set_is_confirmed(run_dir, parsed.items)

    assert not pending_path.exists()
    confirmation = json.loads(confirmation_path.read_text(encoding="utf-8"))
    attestation = confirmation["attestation"]
    assert attestation["challenge_id"] == next_challenge["challenge_id"]
    assert attestation["session_id"] == next_challenge["session_id"]
    assert attestation["checkout_identity"] == next_challenge["checkout_identity"]
    assert attestation["run_id"] == next_challenge["run_id"]
    assert attestation["run_identity"] == next_challenge["run_identity"]
    consumed_path = (
        run_dir
        / f".{pending_path.name}.{next_challenge['challenge_id']}.consumed"
    )
    assert json.loads(consumed_path.read_text(encoding="utf-8")) == next_challenge


def test_cli_spec_confirmation_resolves_the_active_run_without_paths(
    project,
    monkeypatch,
):
    run_dir = project / ".agent-flow" / "runs" / "20260727-210417"
    run_dir.mkdir(parents=True)
    (run_dir / "active").touch()
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "workflow": "default",
                "task": "Implement the empty state.",
                "started_at": "2026-07-27T21:04:17+00:00",
                "current_phase": "design",
            }
        ),
        encoding="utf-8",
    )
    artifact = (
        "## Spec Items\n\n"
        "SPEC-1: Empty search results show the empty state.\n"
        "verify: test:test_empty_search_results_show_the_empty_state\n\n"
        "## Completion Gate\n\n"
        "spec-items: SPEC-1\n"
    )
    (run_dir / "design.md").write_text(artifact, encoding="utf-8")
    parsed = parse_spec_item_section(artifact)
    terminal = type(
        "InteractiveInput",
        (),
        {
            "isatty": lambda self: True,
            "readline": lambda self: "승인\n",
        },
    )()
    monkeypatch.setattr(sys, "stdin", terminal)
    monkeypatch.setattr(
        "agent_flow.cli._is_foreground_user_terminal",
        lambda: True,
    )

    assert main(["spec", "confirm", "--root", str(project)]) == 0
    assert spec_set_is_confirmed(run_dir, parsed.items)

def test_spec_confirmation_never_silently_selects_latest_active_run(
    project,
    monkeypatch,
):
    artifact = (
        "## Spec Items\n\n"
        "SPEC-1: Empty search results show the empty state.\n"
        "verify: test:test_empty_search_results_show_the_empty_state\n\n"
        "## Completion Gate\n\n"
        "spec-items: SPEC-1\n"
    )
    runs: list[Path] = []
    for run_id in ("r1", "r2"):
        run_dir = project / ".agent-flow" / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "active").touch()
        (run_dir / "meta.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "workflow": "default",
                    "task": f"task-{run_id}",
                    "started_at": f"2026-07-27T21:04:1{run_id[-1]}+00:00",
                    "current_phase": "design",
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "design.md").write_text(artifact, encoding="utf-8")
        runs.append(run_dir)
    parsed = parse_spec_item_section(artifact)

    assert main(
        [
            "spec",
            "prepare-confirmation",
            "--root",
            str(project),
            "--hook-capability-hash",
            HOOK_CAPABILITY_HASH,
            "--session-id",
            "session-current",
        ]
    ) == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO("승인"))
    assert main(
        [
            "spec",
            "confirm",
            "--root",
            str(project),
            "--hook-capability",
            HOOK_CAPABILITY,
            "--from-user-prompt",
            "--session-id",
            "session-current",
        ]
    ) == 0
    assert not any(spec_set_is_confirmed(run, parsed.items) for run in runs)

    terminal = type(
        "InteractiveInput",
        (),
        {
            "isatty": lambda self: True,
            "readline": lambda self: "승인\n",
        },
    )()
    monkeypatch.setattr(sys, "stdin", terminal)
    assert main(["spec", "confirm", "--root", str(project)]) == 2
    assert not any(spec_set_is_confirmed(run, parsed.items) for run in runs)


def test_host_prompt_does_not_scan_sibling_worktree_runs(
    project,
    monkeypatch,
):
    run_dir = (
        project
        / ".git"
        / "agent-flow"
        / "worktrees"
        / "feat-sibling"
        / ".agent-flow"
        / "runs"
        / "r1"
    )
    run_dir.mkdir(parents=True)
    (run_dir / "active").touch()
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "run_id": "r1",
                "workflow": "default",
                "task": "sibling task",
                "started_at": "2026-07-27T21:04:17+00:00",
                "current_phase": "design",
            }
        ),
        encoding="utf-8",
    )
    artifact = (
        "## Spec Items\n\n"
        "SPEC-1: Empty search results show the empty state.\n"
        "verify: test:test_empty_search_results_show_the_empty_state\n\n"
        "## Completion Gate\n\n"
        "spec-items: SPEC-1\n"
    )
    (run_dir / "design.md").write_text(artifact, encoding="utf-8")
    parsed = parse_spec_item_section(artifact)

    assert main(
        [
            "spec",
            "prepare-confirmation",
            "--root",
            str(project),
            "--hook-capability-hash",
            HOOK_CAPABILITY_HASH,
            "--session-id",
            "leader-session",
        ]
    ) == 0
    assert not (run_dir / "spec-user-confirmation.pending.json").exists()

    terminal = type(
        "InteractiveInput",
        (),
        {
            "isatty": lambda self: True,
            "readline": lambda self: "승인\n",
        },
    )()
    monkeypatch.setattr(sys, "stdin", terminal)
    assert main(["spec", "confirm", "--root", str(project)]) == 2
    assert not spec_set_is_confirmed(run_dir, parsed.items)


def test_host_confirmation_is_bound_to_the_current_worktree(
    project,
    monkeypatch,
):
    worktree = project / ".agent-flow" / "worktrees" / "feat-child"
    _git("worktree", "add", "-b", "feat/child", str(worktree), "main", cwd=project)
    run_dir = (
        project
        / ".git"
        / "agent-flow"
        / "worktrees"
        / "feat-child"
        / ".agent-flow"
        / "runs"
        / "r1"
    )
    run_dir.mkdir(parents=True)
    (run_dir / "active").touch()
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "run_id": "r1",
                "workflow": "default",
                "task": "child task",
                "started_at": "2026-07-27T21:04:17+00:00",
                "current_phase": "design",
            }
        ),
        encoding="utf-8",
    )
    artifact = (
        "## Spec Items\n\n"
        "SPEC-1: Empty search results show the empty state.\n"
        "verify: test:test_empty_search_results_show_the_empty_state\n\n"
        "## Completion Gate\n\n"
        "spec-items: SPEC-1\n"
    )
    (run_dir / "design.md").write_text(artifact, encoding="utf-8")
    parsed = parse_spec_item_section(artifact)

    monkeypatch.chdir(worktree)
    assert main(
        [
            "spec",
            "prepare-confirmation",
            "--root",
            str(project),
            "--hook-capability-hash",
            HOOK_CAPABILITY_HASH,
            "--session-id",
            "child-session",
        ]
    ) == 0
    challenge = json.loads(
        (run_dir / "spec-user-confirmation.pending.json").read_text(
            encoding="utf-8"
        )
    )
    assert challenge["checkout_identity"] == "worktree:feat-child"

    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "stdin", io.StringIO("승인"))
    assert main(
        [
            "spec",
            "confirm",
            "--root",
            str(project),
            "--hook-capability",
            HOOK_CAPABILITY,
            "--from-user-prompt",
            "--session-id",
            "child-session",
        ]
    ) == 0
    assert not spec_set_is_confirmed(run_dir, parsed.items)

    monkeypatch.chdir(worktree)
    monkeypatch.setattr(sys, "stdin", io.StringIO("승인"))
    assert main(
        [
            "spec",
            "confirm",
            "--root",
            str(project),
            "--hook-capability",
            HOOK_CAPABILITY,
            "--from-user-prompt",
            "--session-id",
            "child-session",
        ]
    ) == 0
    assert spec_set_is_confirmed(run_dir, parsed.items)


def test_cli_spec_confirmation_resolves_current_python_active_meta_run_from_user_prompt(
    project,
    monkeypatch,
):
    run_dir = project / ".agent-flow" / "runs" / "20260727-210417"
    run_dir.mkdir(parents=True)
    (run_dir / "active").touch()
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "workflow": "default",
                "task": "Implement the empty state.",
                "started_at": "2026-07-27T21:04:17+00:00",
                "current_phase": "design",
            }
        ),
        encoding="utf-8",
    )
    artifact = (
        "## Spec Items\n\n"
        "SPEC-1: Empty search results show the empty state.\n"
        "verify: test:test_empty_search_results_show_the_empty_state\n\n"
        "## Completion Gate\n\n"
        "spec-items: SPEC-1\n"
    )
    (run_dir / "design.md").write_text(artifact, encoding="utf-8")
    parsed = parse_spec_item_section(artifact)
    assert main(
        [
            "spec",
            "prepare-confirmation",
            "--root",
            str(project),
            "--hook-capability-hash",
            HOOK_CAPABILITY_HASH,
            "--session-id",
            "session-python",
        ]
    ) == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO("승인"))

    assert main(
        [
            "spec",
            "confirm",
            "--root",
            str(project),
            "--hook-capability",
            HOOK_CAPABILITY,
            "--from-user-prompt",
            "--session-id",
            "session-python",
        ]
    ) == 0
    assert spec_set_is_confirmed(run_dir, parsed.items)


def test_a_second_spec_artifact_cannot_displace_a_confirmed_one(project):
    """반증: agent가 두 번째 artifact를 써 두면 승인 대상이 갈아치워졌다.

    확인된 후보를 건너뛰고 다음 후보를 반환하면, 사용자가 본 적 없는 SPEC
    집합으로 승인이 옮겨간다. 한 run의 현재 SPEC artifact는 하나다.
    """
    run_dir = project / ".agent-flow" / "runs" / "20260728-090000"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps({"run_id": run_dir.name, "current_phase": "design"}),
        encoding="utf-8",
    )
    artifact = (
        "## Spec Items\n\n"
        "SPEC-1: Empty search results show the empty state.\n"
        "verify: test:test_empty_state\n\n"
        "## Completion Gate\n\n"
        "spec-items: SPEC-1\n"
    )
    (run_dir / "design.md").write_text(artifact, encoding="utf-8")
    parsed = parse_spec_item_section(artifact)
    record_spec_set_confirmation(
        run_dir,
        parsed.items,
        spec_set_confirmation_statement(parsed.items),
    )
    assert spec_set_is_confirmed(run_dir, parsed.items)

    (run_dir / "artifacts").mkdir()
    (run_dir / "artifacts" / "design.md").write_text(
        artifact.replace("empty state", "retry action"),
        encoding="utf-8",
    )

    assert (
        _spec_artifact_waiting_for_confirmation(run_dir, pending_only=True)
        is None
    )
    assert spec_set_is_confirmed(run_dir, parsed.items)


def test_fresh_session_host_prepare_then_exact_prompt_consumes_once(project):
    run_dir = project / ".agent-flow" / "runs" / "20260727-210417"
    run_dir.mkdir(parents=True)
    (run_dir / "active").touch()
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "workflow": "default",
                "task": "Implement the empty state.",
                "started_at": "2026-07-27T21:04:17+00:00",
                "current_phase": "design",
            }
        ),
        encoding="utf-8",
    )
    artifact = (
        "## Spec Items\n\n"
        "SPEC-1: Empty search results show the empty state.\n"
        "verify: test:test_empty_search_results_show_the_empty_state\n\n"
        "## Completion Gate\n\n"
        "spec-items: SPEC-1\n"
    )
    (run_dir / "design.md").write_text(artifact, encoding="utf-8")
    invocation_marker = project / "hook-invoked"
    launcher = project / ".agent-flow" / "bin" / "agent-flow"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        f"Path({str(invocation_marker)!r}).touch()\n"
        "from agent_flow.cli import main\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    env = os.environ.copy()
    env["AGENT_FLOW"] = str(launcher)
    env["PYTHONPATH"] = str(SRC)
    hook_dir = project / ".agent-flow" / "scripts" / "hooks"
    hook_dir.mkdir(parents=True)
    for name in (
        "confirm-spec-user-prompt.py",
        "prepare-spec-user-prompt.py",
    ):
        shutil.copy2(REPO / "scripts" / "hooks" / name, hook_dir / name)
    confirm_hook = hook_dir / "confirm-spec-user-prompt.py"
    prepare_hook = hook_dir / "prepare-spec-user-prompt.py"
    parsed = parse_spec_item_section(artifact)
    pending_path = run_dir / "spec-user-confirmation.pending.json"
    confirmation_path = run_dir / "spec-user-confirmation.json"

    def run_hook(path: Path, payload: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            (sys.executable, str(path)),
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    stale_payload = {
        "hook_event_name": "PostToolUse",
        "cwd": str(project),
        "session_id": "session-stale",
    }
    prepared = run_hook(prepare_hook, stale_payload)
    assert prepared.returncode == 0
    stale_challenge = json.loads(pending_path.read_text(encoding="utf-8"))
    assert stale_challenge["session_id"] == "session-stale"
    assert invocation_marker.is_file()
    invocation_marker.unlink()
    assert not (
        project / ".agent-flow" / "runtime" / "spec-hook-capabilities"
    ).exists()

    ignored = run_hook(
        confirm_hook,
        {
            "hook_event_name": "PostToolUse",
            "prompt": "승인",
            "cwd": str(project),
            "session_id": "session-hook",
        },
    )
    assert ignored.returncode == 0
    assert not invocation_marker.exists()
    assert json.loads(pending_path.read_text(encoding="utf-8")) == stale_challenge
    assert not spec_set_is_confirmed(run_dir, parsed.items)

    quoted = run_hook(
        confirm_hook,
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "> 승인",
            "cwd": str(project),
            "session_id": "session-hook",
        },
    )
    assert quoted.returncode == 0
    assert invocation_marker.is_file()
    invocation_marker.unlink()
    current_challenge = json.loads(pending_path.read_text(encoding="utf-8"))
    assert current_challenge["session_id"] == "session-hook"
    assert current_challenge["challenge_id"] != stale_challenge["challenge_id"]
    assert not spec_set_is_confirmed(run_dir, parsed.items)
    assert not (
        project / ".agent-flow" / "runtime" / "spec-hook-capabilities"
    ).exists()

    current_payload = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": "승인",
        "cwd": str(project),
        "session_id": "session-hook",
    }
    result = run_hook(confirm_hook, current_payload)
    assert result.returncode == 0
    assert invocation_marker.is_file()
    assert spec_set_is_confirmed(run_dir, parsed.items)
    confirmation = json.loads(confirmation_path.read_text(encoding="utf-8"))
    assert confirmation["provenance"] == "host-user-prompt"
    assert confirmation["attestation"]["session_id"] == "session-hook"
    invocation_marker.unlink()

    confirmation_path.unlink()
    replay = run_hook(confirm_hook, current_payload)
    assert replay.returncode == 0
    assert invocation_marker.is_file()
    assert not confirmation_path.exists()
    assert not spec_set_is_confirmed(run_dir, parsed.items)


def test_test_spec_requires_observed_passing_named_test(project, run_dir):
    test_name = "test_empty_search_results_show_the_empty_state"
    _capture_spec_ledger(run_dir, f"test:{test_name}")
    review_claim = GATE + f"spec-evidence: test:{test_name}\n"
    expected = [
        f"SPEC-1: test:{test_name} "
        "(no passing observed test command includes the test name)"
    ]

    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review_claim,
    ) == expected

    _observe(project, f"pytest -q tests/test_search.py::{test_name}", exit_code=1)
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review_claim,
    ) == expected

    _observe(project, f"echo {test_name}", exit_code=0)
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review_claim,
    ) == expected

    _observe(project, f"pytest -q {test_name}", exit_code=0)
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review_claim,
    ) == expected

    _observe(
        project,
        f"pytest -q tests/test_search.py::{test_name} || true",
        exit_code=0,
    )
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review_claim,
    ) == expected

    other_checkout = project.parent / "other"
    other_checkout.mkdir()
    _observe(
        project,
        f"pytest -q tests/test_search.py::{test_name}",
        exit_code=0,
        cwd=other_checkout,
    )
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review_claim,
    ) == expected
    _observe(project, f"pytest -q tests/test_search.py::{test_name}", exit_code=0)
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review_claim,
    ) == []


def test_test_spec_evidence_is_scoped_to_the_run_not_review_entry(project, run_dir):
    test_name = "test_empty_search_results_show_the_empty_state"
    _capture_spec_ledger(run_dir, f"test:{test_name}")
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "task": "Show an empty state.",
                "started_at": "1970-01-01T00:01:40+00:00",
                "phase_entered_at": "1970-01-01T00:03:20+00:00",
            }
        ),
        encoding="utf-8",
    )
    _observe(
        project,
        f"pytest -q tests/test_search.py::{test_name}",
        exit_code=0,
        at=150.0,
    )
    (run_dir / "final-review.md").write_text(GATE, encoding="utf-8")
    runner = Runner(project, run_dir=run_dir)

    runner_missing = runner._missing_required_markers(
        Phase(id="final-review", description="")
    )
    status_missing = _missing_completion_markers(
        run_dir,
        "default",
        "final-review",
        config_root=project,
        project_root=project,
    )

    assert not any(item.startswith("SPEC-1: test:") for item in runner_missing)
    assert not any(item.startswith("SPEC-1: test:") for item in status_missing)

def test_symbol_spec_scopes_value_to_changed_symbol_file(project, run_dir):
    _capture_spec_ledger(run_dir, "symbol:SearchResults=No results")
    symbol_file = project / "SearchResults.kt"
    symbol_file.write_text("class SearchResults\n", encoding="utf-8")
    _git("add", ".", cwd=project)
    _git("commit", "-m", "add symbol", cwd=project)

    symbol_file.write_text(
        'class SearchResults\nval emptyCopy = "Nothing here"\n',
        encoding="utf-8",
    )
    (project / "OtherCopy.kt").write_text(
        'val emptyCopy = "No results"\n',
        encoding="utf-8",
    )
    expected = [
        "SPEC-1: symbol:SearchResults=No results "
        "(value is not added in a changed file containing the symbol)"
    ]

    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        GATE,
    ) == expected

    symbol_file.write_text(
        'class SearchResults\nval emptyCopy = "No results"\n',
        encoding="utf-8",
    )
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        GATE,
    ) == []


def test_symbol_spec_does_not_accept_a_pure_rename(project, run_dir):
    _capture_spec_ledger(run_dir, "symbol:SearchResults=No results")
    original = project / "SearchResults.kt"
    original.write_text(
        'class SearchResults\nval emptyCopy = "No results"\n',
        encoding="utf-8",
    )
    _git("add", ".", cwd=project)
    _git("commit", "-m", "add implemented symbol", cwd=project)
    _git("mv", "SearchResults.kt", "RenamedSearchResults.kt", cwd=project)

    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        GATE,
    ) == [
        "SPEC-1: symbol:SearchResults=No results "
        "(value is not added in a changed file containing the symbol)"
    ]


def test_symbol_spec_ignores_untracked_symlinks(project, run_dir):
    _capture_spec_ledger(run_dir, "symbol:SearchResults=No results")
    outside = project.parent / "outside.kt"
    outside.write_text(
        'class SearchResults\nval emptyCopy = "No results"\n',
        encoding="utf-8",
    )
    (project / "SearchResults.kt").symlink_to(outside)

    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        GATE,
    ) == [
        "SPEC-1: symbol:SearchResults=No results "
        "(value is not added in a changed file containing the symbol)"
    ]

def test_agent_run_confirmation_command_is_rejected(project, run_dir):
    """반증: 확인 명령을 agent가 대신 돌리면 사용자 관측자가 사라진다."""
    _capture_spec_ledger(run_dir, "manual")
    _observe(project, f"agent-flow spec confirm --run-dir {run_dir}", exit_code=0)
    expected = [
        "spec-confirmation: an approval command or managed approval hook ran in "
        "the agent's shell; only the user may approve through chat or their own "
        "terminal"
    ]

    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        GATE,
    ) == expected

    artifact = (
        "## Spec Items\n\n"
        "SPEC-1: Empty search results show the empty state.\n"
        "verify: manual\n\n"
        "## Design Values\n\n"
        "## Completion Gate\n\nspec-items: SPEC-1\n"
    )
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "design",
        artifact,
    ) == expected


def test_final_review_rechecks_the_spec_set_confirmation(project, run_dir):
    """반증: 확인 기록을 지우면 downstream gate는 SPEC 집합을 다시 안 본다."""
    _capture_spec_ledger(run_dir, "manual")
    (run_dir / "spec-user-confirmation.json").unlink()

    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        GATE,
    ) == [
        "spec-ledger: SPEC set does not have current user confirmation "
        "(reply exactly `승인` in the chat; fallback: `agent-flow spec confirm`)"
    ]


def test_manual_spec_requires_external_approval_record(project, run_dir):
    _capture_spec_ledger(run_dir, "manual")
    review_claim = GATE + "manual-approved: SPEC-1\n"
    expected = ["SPEC-1: manual (no user approval record)"]

    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review_claim,
    ) == expected

    (run_dir / "spec-manual-approvals.json").write_text(
        json.dumps({"approved_spec_ids": ["SPEC-1"]}),
        encoding="utf-8",
    )
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review_claim,
    ) == expected

def test_cli_records_manual_spec_approval(project, run_dir, monkeypatch):
    _capture_spec_ledger(run_dir, "manual")
    expected_statement = manual_spec_approval_statement(run_dir, "SPEC-1")
    terminal = type(
        "InteractiveInput",
        (),
        {
            "isatty": lambda self: True,
            "readline": lambda self: expected_statement + "\n",
        },
    )()
    monkeypatch.setattr(sys, "stdin", terminal)
    monkeypatch.setattr(
        "agent_flow.cli._is_foreground_user_terminal",
        lambda: True,
    )

    exit_code = main(
        [
            "spec",
            "approve",
            "SPEC-1",
            "--run-dir",
            str(run_dir),
        ]
    )

    assert exit_code == 0
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        GATE,
    ) == []

    _capture_spec_ledger(
        run_dir,
        "manual",
        requirement="Confirm a changed rendered copy.",
    )
    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        GATE,
    ) == ["SPEC-1: manual (no user approval record)"]


def test_all_completion_paths_share_spec_evidence_check(project, run_dir):
    _capture_spec_ledger(run_dir, "manual")
    (run_dir / "final-review.md").write_text(GATE, encoding="utf-8")
    runner = Runner(project, run_dir=run_dir)

    runner_missing = runner._missing_required_markers(
        Phase(id="final-review", description="")
    )
    status_missing = _missing_completion_markers(
        run_dir,
        "default",
        "final-review",
        config_root=project,
        project_root=project,
    )

    expected = "SPEC-1: manual (no user approval record)"
    assert expected in runner_missing
    assert expected in status_missing


def test_spec_markers_uses_the_supplied_run_context(
    project, run_dir, capsys
):
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "task": "",
                "started_at": "1970-01-01T00:01:40+00:00",
            }
        ),
        encoding="utf-8",
    )
    other_run = project / ".agent-flow" / "runs" / "999"
    other_run.mkdir(parents=True)
    (other_run / "active").touch()
    (other_run / "meta.json").write_text(
        json.dumps(
            {
                "task": "Unrelated active task.",
                "started_at": "1970-01-01T00:03:20+00:00",
                "phase_entered_at": "1970-01-01T00:03:20+00:00",
            }
        ),
        encoding="utf-8",
    )
    artifact = run_dir / "final-review.md"
    artifact.write_text(GATE, encoding="utf-8")

    exit_code = main(
        [
            "spec",
            "markers",
            "--root",
            str(project),
            "--run-dir",
            str(run_dir),
            "--project-root",
            str(project),
            "--phase",
            "final-review",
            "--artifact",
            str(artifact),
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == []


def test_request_changes_routes_even_when_spec_evidence_is_missing(project, run_dir):
    _capture_spec_ledger(run_dir, "manual")
    review = "## Overall\nverdict: request-changes\n"
    (run_dir / "final-review.md").write_text(review, encoding="utf-8")
    runner = Runner(project, run_dir=run_dir)

    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        review,
    ) == []
    assert runner._missing_required_markers(
        Phase(id="final-review", description="")
    ) == []


def test_missing_canonical_ledger_fails_closed(project, run_dir):
    (run_dir / "design-spec.md").unlink()

    assert missing_spec_item_evidence(
        project,
        run_dir,
        "final-review",
        GATE,
        task_text="Show an empty state.",
    ) == ["spec-ledger: design-spec.md is missing"]

def test_literal_values_in_the_diff_pass(project, run_dir):
    _write_code(project, "val pad = 16.dp // 16dp\nval brand = Color(0xFF6B00) // #FF6B00\n")
    assert missing_design_value_implementations(project, run_dir, "final-review", GATE) == []


def test_wrong_value_is_reported(project, run_dir):
    """반증: 16dp를 보고 12dp를 쓰면 아무도 안 잡던 자리다."""
    _write_code(project, "val pad = 12.dp\nval brand = Color(0xFF6B00) // #FF6B00\n")
    missing = missing_design_value_implementations(project, run_dir, "final-review", GATE)
    assert missing and "horizontal-padding=16dp" in missing[0]
    assert "brand-primary" not in missing[0]


def test_hex_color_case_is_ignored(project, run_dir):
    _write_code(project, "val pad = 16dp\nval brand = 0xff6b00 // #ff6b00\n")
    assert missing_design_value_implementations(project, run_dir, "final-review", GATE) == []


def test_declared_token_present_in_the_diff_passes(project, run_dir):
    """`Spacing.m`(=16dp)을 위반으로 들면 정상 구현이 fix-loop에 갇힌다."""
    _write_code(project, "val pad = Spacing.m\nval brand = BrandColors.primary\n")
    text = GATE + "design-values-implemented: horizontal-padding=Spacing.m, brand-primary=BrandColors.primary\n"
    assert missing_design_value_implementations(project, run_dir, "final-review", text) == []


def test_declared_token_absent_from_the_diff_is_reported(project, run_dir):
    """반증: 토큰 이름을 대는 것만으로 통과하면 그건 다시 자기신고다."""
    _write_code(project, "val pad = 4.dp\nval brand = 0xFF6B00\n")
    text = GATE + "design-values-implemented: horizontal-padding=Spacing.m\n"
    missing = missing_design_value_implementations(project, run_dir, "final-review", text)
    assert missing and "declared token is not in the diff" in missing[0]


def test_untouched_code_does_not_count_as_evidence(project, run_dir):
    """반증: 원래부터 있던 값이 증거가 되면 코드 0줄로도 통과한다."""
    (project / "Theme.kt").write_text("val pad = 16dp\nval brand = #FF6B00\n", encoding="utf-8")
    _git("add", ".", cwd=project)
    _git("commit", "-m", "pre-existing", cwd=project)
    _write_code(project, "val nothing = 1\n")
    missing = missing_design_value_implementations(project, run_dir, "final-review", GATE)
    assert missing and "horizontal-padding=16dp" in missing[0]


def test_committed_work_on_a_branch_still_counts(project, run_dir):
    """작업이 이미 커밋됐다고 증거가 사라지면 안 된다. merge-base부터 본다."""
    _git("checkout", "-b", "feat/x", cwd=project)
    _write_code(project, "val pad = 16dp\nval brand = #FF6B00\n")
    _git("add", ".", cwd=project)
    _git("commit", "-m", "impl", cwd=project)
    assert missing_design_value_implementations(project, run_dir, "final-review", GATE) == []


def test_other_phases_are_not_checked(project, run_dir):
    _write_code(project, "val nothing = 1\n")
    assert missing_design_value_implementations(project, run_dir, "green", GATE) == []


def test_empty_ledger_checks_nothing(project, tmp_path):
    empty = tmp_path / "empty-run"
    empty.mkdir()
    capture_design_ledger(empty, "prd", "## Design Values\n\n")
    _write_code(project, "val nothing = 1\n")
    assert missing_design_value_implementations(project, empty, "final-review", GATE) == []


def test_non_git_project_degrades(tmp_path, run_dir):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert missing_design_value_implementations(plain, run_dir, "final-review", GATE) == []


def test_declared_tokens_parsing():
    text = "## Completion Gate\n\ndesign-values-implemented: a=Spacing.m, b=Brand.primary\n"
    assert declared_tokens(text) == {"a": "Spacing.m", "b": "Brand.primary"}
    assert declared_tokens("## Completion Gate\n\ndesign-values-implemented: none\n") == {}
