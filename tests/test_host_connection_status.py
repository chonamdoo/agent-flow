"""host 연결 진단: 설치·등록·신뢰·실행을 따로 판정하고 지원 등급을 표시한다.

등급은 표시일 뿐이다. 어떤 값도 run 허용을 바꾸지 않는다.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from agent_flow.artifact import create_run, find_active_run, read_meta
from agent_flow.cli import main
from agent_flow.core.host_connection import (
    collect_host_connection,
    render_host_connection,
    support_level,
)
from agent_flow.core.host_trust import read_host_trust

_HOST_HINT_ENV = ("OMP_PROFILE", "CLAUDECODE", "CLAUDE_CLI", "CODEX_CLI", "CODEX_HOME")


def _project_with_run(tmp_path: Path, *, install: bool = False) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(("git", *args), cwd=project, check=True, capture_output=True)
    if install:
        installer = Path(__file__).resolve().parents[1] / "bin" / "agent-flow-kit.mjs"
        installed = subprocess.run(
            ("node", str(installer), "install", "--root", str(project), "--profile", "python"),
            capture_output=True, text=True, timeout=120,
        )
        assert installed.returncode == 0, installed.stderr
    else:
        (project / ".agent-flow").mkdir()
        (project / ".agent-flow" / "kit.json").write_text('{"hooks": true}', encoding="utf-8")
    run_dir = create_run(project, "default", "Check the host connection.")
    assert read_meta(run_dir)["started_at"]
    return project


def _record_commands(project: Path, *entries: dict) -> None:
    (project / ".agent-flow" / "commands-run.jsonl").write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8"
    )


def _as_claude_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in _HOST_HINT_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")


def test_explicit_execution_is_attributed_only_to_its_host(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    project = _project_with_run(tmp_path, install=True)
    home.mkdir(exist_ok=True)
    (home / ".claude.json").write_text(
        json.dumps({"projects": {str(project): {"hasTrustDialogAccepted": True}}}),
        encoding="utf-8",
    )
    active = find_active_run(project)
    assert active is not None
    run_dir = active.path
    started_at = datetime.fromisoformat(read_meta(run_dir)["started_at"]).timestamp()

    for recorded_host in ("omp", None, "unknown", ["omp"], "claude"):
        _record_commands(
            project,
            {"command": "pytest -q", "host": recorded_host, "exit_code": 0,
             "cwd": str(project), "at": started_at + 1},
            {"command": "pytest -q", "host": "claude", "exit_code": 0,
             "cwd": str(project), "at": started_at - 1},
            {"command": "pytest -q", "host": "claude", "exit_code": 0,
             "cwd": str(tmp_path / "sibling"), "at": started_at + 1},
        )
        report = collect_host_connection(
            project_root=project, checkout=project, run_id=run_dir.name,
            run_started_at=started_at, active=("claude", "env:CLAUDECODE"),
            home=home, env={"CODEX_HOME": str(home / ".codex"), "CLAUDECODE": "1"},
        )
        expected = "hook_enforced" if recorded_host == "claude" else "hook_unproven"
        assert report.active_level == expected, recorded_host
        payload = json.loads(render_host_connection(report)[-1].split(": ", 1)[1])
        assert payload["execution"]["commands"] == 1
        assert payload["hosts"]["claude"]["execution"]["commands"] == int(
            recorded_host == "claude"
        )
        assert payload["hosts"]["omp"]["execution"]["commands"] == int(
            recorded_host == "omp"
        )
        assert payload["hosts"]["codex"]["execution"]["commands"] == 0

        codex_report = collect_host_connection(
            project_root=project, checkout=project, run_id=run_dir.name,
            run_started_at=started_at, active=("codex", "env:CODEX_HOME"),
            home=home, env={"CODEX_HOME": str(home / ".codex")},
        )
        codex_payload = json.loads(render_host_connection(codex_report)[-1].split(": ", 1)[1])
        assert codex_payload["hosts"]["codex"]["execution"]["observed"] is False


def test_status_prints_the_host_connection_card_after_status_json(
    tmp_path: Path, monkeypatch, capsys
):
    """반증: card가 `status_json:`이나 `run:`/`next_command:` 줄을 하나 더 내면
    binding hook이 마지막 status를 잘못 읽는다. 실행 관측이 run 전·다른 checkout
    기록까지 세면 이번 세션에서 hook이 돈 것처럼 보인다.
    """
    project = _project_with_run(tmp_path)
    now = time.time()
    _record_commands(
        project,
        {"command": "pytest -q", "exit_code": 0, "cwd": str(project), "at": 1.0},
        {"command": "pytest -q", "exit_code": 0, "cwd": str(tmp_path), "at": now},
        {"command": "pytest -q", "exit_code": 1, "cwd": str(project), "at": now},
        {"command": "pytest -q", "cwd": str(project), "at": now},
    )
    _as_claude_session(tmp_path, monkeypatch)

    assert main(["status", "--root", str(project)]) == 0

    lines = capsys.readouterr().out.splitlines()
    status_at = [index for index, line in enumerate(lines) if line.startswith("status_json:")]
    card_at = [index for index, line in enumerate(lines) if line.startswith("host_connection_json:")]
    assert len(status_at) == 1
    assert len(card_at) == 1 and card_at[0] > status_at[0]
    assert sum(line.startswith("run:") for line in lines) == 1
    assert sum(line.startswith("next_command:") for line in lines) == 1
    assert any("run을 막지 않습니다" in line for line in lines[status_at[0] : card_at[0]])
    card = json.loads(lines[card_at[0]].removeprefix("host_connection_json:"))
    assert card["blocks_run"] is False
    assert card["active_host"] == {
        "name": "claude",
        "detected_by": "env:CLAUDECODE",
        "level": "runner_only",
    }
    assert set(card["hosts"]) == {"claude", "codex", "omp"}
    assert {host["registered"] for host in card["hosts"].values()} == {"missing"}
    assert {host["level"] for host in card["hosts"].values()} == {"runner_only"}
    assert card["execution"]["commands"] == 2
    assert card["execution"]["exit_codes_missing"] == 1
    assert card["execution"]["observed"] is True
    assert any("RED 관측" in line for line in lines[status_at[0] : card_at[0]])


@pytest.mark.parametrize("hostile", ("timestamp-out-of-range", "newline-in-filename"))
def test_status_keeps_its_exit_code_and_one_status_line_on_hostile_diagnostic_input(
    tmp_path: Path, monkeypatch, capsys, hostile: str
):
    """반증: 진단은 kit이 고르지 않은 값을 읽는다. 기록의 `at=1e300`이 날짜 변환에서
    터지면 status exit code가 바뀌고, 줄바꿈이 든 파일 이름을 그대로 찍으면
    `status_json:` 줄이 하나 더 생겨 binding hook이 마지막 줄을 잘못 읽는다.
    """
    project = _project_with_run(tmp_path)
    if hostile == "timestamp-out-of-range":
        _record_commands(
            project, {"command": "pytest -q", "exit_code": 0, "cwd": str(project), "at": 1e300}
        )
    else:
        planted = project / ".agent-flow" / "scripts" / "hooks" / 'x\nstatus_json: {"forged": true}'
        planted.parent.mkdir(parents=True)
        planted.write_text("#!/bin/sh\n", encoding="utf-8")
        planted.chmod(0o755)
    _as_claude_session(tmp_path, monkeypatch)

    assert main(["status", "--root", str(project)]) == 0

    lines = capsys.readouterr().out.splitlines()
    status_lines = [line for line in lines if line.startswith("status_json:")]
    assert len(status_lines) == 1
    assert "forged" not in status_lines[0]
    assert sum(line.startswith("host_connection_json:") for line in lines) == 1



@pytest.mark.parametrize(
    ("installed", "registered", "trust", "detected_by", "executed", "expected"),
    (
        (True, True, "yes", "env:CLAUDECODE", True, "hook_enforced"),
        # 신뢰를 확인하지 못하면 다른 hook의 실행 기록이 건너뛰어진 guard를 가린다.
        (True, True, "unknown", "env:CLAUDECODE", True, "hook_unproven"),
        (True, True, "unknown", "env:CODEX_HOME", True, "hook_unproven"),
        (True, True, "n/a", "env:OMP_PROFILE", True, "hook_enforced"),
        # PATH에 있다는 것은 host 증거가 아니다. 실행을 그 host에 귀속하지 않는다.
        (True, True, "yes", "path", True, "hook_unproven"),
        # 관측된 실행은 active host 것이다. 다른 host 행에는 귀속하지 않는다.
        (True, True, "yes", None, True, "hook_unproven"),
        (True, True, "yes", "env:CLAUDECODE", False, "hook_unproven"),
        (True, True, "no", "env:CODEX_HOME", True, "runner_only"),
        (True, False, "yes", "env:CLAUDECODE", True, "runner_only"),
        (False, True, "yes", "env:CLAUDECODE", True, "runner_only"),
    ),
)
def test_support_level_follows_registration_trust_and_observed_execution(
    installed, registered, trust, detected_by, executed, expected
):
    """반증: 등록만 보고 '보호됨'을 표시하면 hook을 실행하지 않는 host에서도 안심하게 된다."""
    assert (
        support_level(
            installed=installed,
            registered=registered,
            trust=trust,
            detected_by=detected_by,
            executed=executed,
        )
        == expected
    )


def _codex_hooks(checkout: Path) -> Path:
    path = checkout / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "a"}]}
                    ],
                    "Stop": [{"hooks": [{"type": "command", "command": "b"}]}],
                    "UserPromptSubmit": [],
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _codex_config(
    checkout: Path,
    *,
    project: Path | None,
    hooks: tuple[str, ...],
    hash_value: str = '"sha256:00"',
    disabled: tuple[str, ...] = (),
) -> str:
    lines = []
    if project is not None:
        lines += [f'[projects."{project}"]', 'trust_level = "trusted"', ""]
    for key in hooks:
        lines += [
            f'[hooks.state."{checkout}/.codex/hooks.json:{key}"]',
            f"trusted_hash = {hash_value}",
            *(["enabled = false"] if key in disabled else []),
            "",
        ]
    return "\n".join(lines)


def _host_files(tmp_path: Path, case: str) -> tuple[str, Path, Path, Path, dict[Path, str]]:
    home = tmp_path / "home"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    leader = tmp_path / "leader"
    files: dict[Path, str] = {}
    if case.startswith("codex"):
        _codex_hooks(checkout)
        both = ("pre_tool_use:0:0", "stop:0:0")
        files[home / ".codex" / "config.toml"] = {
            "codex-trusted": _codex_config(checkout, project=checkout, hooks=both),
            "codex-trusted-at-leader": _codex_config(checkout, project=leader, hooks=both),
            "codex-project-untrusted": _codex_config(checkout, project=None, hooks=both),
            "codex-hook-untrusted": _codex_config(
                checkout, project=checkout, hooks=("pre_tool_use:0:0",)
            ),
            "codex-hook-disabled": _codex_config(
                checkout, project=checkout, hooks=both, disabled=("stop:0:0",)
            ),
            "codex-hook-hash-malformed": _codex_config(
                checkout, project=checkout, hooks=both, hash_value="true"
            ),
            "codex-broken": "[projects\ntrust_level = ",
        }[case]
        return "codex", home, checkout, leader, files
    if case.startswith("claude"):
        accepted = {"claude-trusted": True, "claude-untrusted": False}.get(case, True)
        projects = {} if case == "claude-unrecorded" else {
            str(checkout): {"hasTrustDialogAccepted": accepted}
        }
        files[home / ".claude.json"] = (
            "{not json" if case == "claude-broken" else json.dumps({"projects": projects})
        )
        if case == "claude-hooks-disabled":
            files[home / ".claude" / "settings.json"] = json.dumps({"disableAllHooks": True})
        return "claude", home, checkout, leader, files
    files[home / ".omp" / "agent" / "config.yml"] = {
        "omp-default": "theme: dark\n",
        "omp-disabled": "disabledExtensions:\n  - extension-module:agent-flow-hooks\n",
        "omp-broken": "disabledExtensions: [unclosed\n",
    }[case]
    return "omp", home, checkout, leader, files


# 사용자가 host에서 확인하거나 고칠 수 있는 상태에만 힌트가 붙는다. 신뢰가 확인됐거나
# 파일을 못 읽는 경우에는 붙지 않는다.
_HINTED_TRUST_CASES = {
    "codex-trusted",
    "codex-trusted-at-leader",
    "codex-project-untrusted",
    "codex-hook-untrusted",
    "codex-hook-disabled",
    "claude-untrusted",
    "claude-unrecorded",
    "claude-hooks-disabled",
    "omp-disabled",
}


@pytest.mark.parametrize(
    ("case", "expected"),
    (
        ("codex-trusted", "unknown"),
        ("codex-trusted-at-leader", "unknown"),
        ("codex-project-untrusted", "no"),
        ("codex-hook-untrusted", "no"),
        ("codex-hook-disabled", "no"),
        ("codex-hook-hash-malformed", "unknown"),
        ("claude-trusted", "yes"),
        ("claude-untrusted", "unknown"),
        ("claude-unrecorded", "unknown"),
        ("claude-hooks-disabled", "no"),
        ("claude-broken", "unknown"),
        ("omp-default", "n/a"),
        ("omp-disabled", "no"),
        ("omp-broken", "unknown"),
    ),
)
def test_host_trust_is_read_only_and_reports_unknown_on_unreadable_config(
    tmp_path: Path, case: str, expected: str
):
    """반증: Codex는 project와 hook마다 신뢰가 있어야 project hook을 실행한다. 등록만
    보고 '보호됨'으로 표시하면 실제로는 아무 hook도 돌지 않는 세션을 놓친다. `/hooks`에서
    끈 hook과 hash가 바뀐 hook도 건너뛴다. hash는 확인할 수 없으므로 기록이 다 있어도
    `yes`라고 하면 건너뛰어진 guard를 신뢰된 것으로 보인다. Codex는
    linked worktree의 project 신뢰를 main checkout에서 찾으므로, worktree 경로만 보면
    신뢰된 leader의 worktree를 고칠 수 없는 `no`로 보인다. Claude는 `-p` 실행을
    신뢰로 취급하므로 신뢰 기록이 없다고 `no`로 단정하면 실제로 도는 hook을
    runner_only로 낮춘다. 진단이 host 설정을 고치면 installer가 막아 둔 승인 세탁과
    같은 길이 열린다.
    """
    host, home, checkout, leader, files = _host_files(tmp_path, case)
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    before = {path: path.read_bytes() for path in files}

    trust = read_host_trust(host, home=home, checkout=checkout, project_root=leader, env={})

    assert trust.state == expected
    assert bool(trust.hint) == (case in _HINTED_TRUST_CASES)
    assert {path: path.read_bytes() for path in files} == before


@pytest.mark.parametrize(
    ("host", "target"),
    (
        ("claude", "home/.claude/settings.json"),
        ("claude", "home/.claude.json"),
        ("codex", "home/.codex/config.toml"),
        ("codex", "checkout/.codex/hooks.json"),
        ("omp", "home/.omp/agent/config.yml"),
    ),
)
def test_host_trust_never_opens_a_non_regular_file(tmp_path: Path, host: str, target: str):
    """반증: status는 매번 세 host의 설정을 모두 연다. FIFO를 열면 쓰는 쪽이 올 때까지
    멈추고, 진단 경계의 예외 처리는 멈춘 호출을 잡지 못한다. checkout의 등록 파일은
    agent가 바꿀 수 있다.
    """
    home, checkout = tmp_path / "home", tmp_path / "checkout"
    checkout.mkdir()
    if target == "checkout/.codex/hooks.json":
        config = home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(_codex_config(checkout, project=checkout, hooks=()), encoding="utf-8")
    fifo = tmp_path / target
    fifo.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(fifo)

    state = _in_child(
        "from agent_flow.core.host_trust import read_host_trust\n"
        f"print(read_host_trust({host!r}, home=Path({str(home)!r}), "
        f"checkout=Path({str(checkout)!r}), project_root=Path({str(checkout)!r}), env={{}}).state)"
    )

    assert state == "unknown"


@pytest.mark.parametrize("reader", ("host-trust", "registration"))
def test_status_type_checks_the_opened_file_not_its_name(tmp_path: Path, reader: str):
    """반증: 이름으로 종류를 확인한 뒤 다시 이름으로 열면, 그 사이에 agent가 checkout의
    등록 파일을 FIFO로 바꿔 status를 멈출 수 있다. 종류는 연 descriptor로 확인해야 한다.
    """
    home, checkout = tmp_path / "home", tmp_path / "checkout"
    if reader == "host-trust":
        target = _codex_hooks(checkout)
        config = home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(_codex_config(checkout, project=checkout, hooks=()), encoding="utf-8")
        call = (
            "from agent_flow.core.host_trust import read_host_trust\n"
            f"read_host_trust('codex', home=Path({str(home)!r}), checkout=Path({str(checkout)!r}), "
            f"project_root=Path({str(checkout)!r}), env={{}})"
        )
    else:
        target = checkout / ".claude" / "settings.json"
        target.parent.mkdir(parents=True)
        target.write_text('{"hooks": {}}', encoding="utf-8")
        call = (
            "from agent_flow.core.hook_integrity import describe_managed_hooks\n"
            f"describe_managed_hooks(Path({str(checkout)!r}), Path({str(checkout)!r}))"
        )
    # 이름으로 종류를 처음 확인한 직후에 파일을 FIFO로 바꾼다. 그 뒤 이름으로 다시 열면 멈춘다.
    swap_after_type_check = "\n".join((
        "import os",
        f"target = {str(target)!r}",
        "real_stat = os.stat",
        "swapped = []",
        "def stat_then_swap(path, *args, **kwargs):",
        "    result = real_stat(path, *args, **kwargs)",
        "    if os.fspath(path) == target and not swapped:",
        "        swapped.append(True)",
        "        os.unlink(target)",
        "        os.mkfifo(target)",
        "    return result",
        "os.stat = stat_then_swap",
    ))

    assert _in_child(f"{swap_after_type_check}\n{call}\nprint('returned')") == "returned"


def _in_child(code: str) -> str:
    """멈추는 회귀가 테스트 실행 전체를 붙잡지 않도록 시간 제한이 있는 자식 프로세스에서 돈다."""
    completed = subprocess.run(
        [sys.executable, "-c", f"from pathlib import Path\n{code}"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()
