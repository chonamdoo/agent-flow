"""실패한 run의 worktree를 현장 보존할 수 있는지 본다.

run이 실패하면 worktree가 자동으로 지워진다. 커밋되지 않은 작업이 남아 있으면
`WorktreeIsolationError`로 보존되지만, 그 조건에 걸리지 않는 실패 — 게이트 실패,
빌드 산출물만 남은 상태, 재현이 어려운 경합 — 은 증거가 함께 사라진다.

디버깅 중에는 그 자동 삭제를 끌 수 있어야 한다. 다만 사용자가 **직접** 요청한
`worktree remove`까지 막으면 안 된다. 그건 정리하겠다는 명시적 의사다.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow import cli as CLI

KILL_SWITCH = "AGENT_FLOW_KEEP_FAILED_WORKTREE"


def _status(tmp_path: Path):
    return SimpleNamespace(name="feat-demo", path=tmp_path / "feat-demo")


def test_failed_run_cleanup_is_skipped_when_the_kill_switch_is_set(
    tmp_path: Path, monkeypatch, capsys
):
    """반증: 끌 수 없으면 실패 현장이 매번 사라진다."""
    removed: list[str] = []
    monkeypatch.setattr(
        CLI, "remove_worktree", lambda **kw: removed.append(kw["status"].name)
    )
    monkeypatch.setenv(KILL_SWITCH, "1")

    CLI._cleanup_worktree_after_failure(
        tmp_path, _status(tmp_path), RuntimeError("gate failed")
    )

    assert removed == [], "kill-switch가 켜졌는데 worktree를 지웠다"
    err = capsys.readouterr().err
    assert KILL_SWITCH in err, "왜 남았는지 알려주지 않으면 유출과 구분되지 않는다"
    assert "gate failed" in err, "원래 실패 원인이 가려지면 안 된다"


def test_failed_run_cleanup_still_runs_by_default(tmp_path: Path, monkeypatch):
    """불변: 기본값은 정리다. 끄지 않았는데 남으면 worktree가 샌다."""
    removed: list[str] = []
    monkeypatch.setattr(
        CLI, "remove_worktree", lambda **kw: removed.append(kw["status"].name)
    )
    monkeypatch.delenv(KILL_SWITCH, raising=False)

    CLI._cleanup_worktree_after_failure(
        tmp_path, _status(tmp_path), RuntimeError("boom")
    )

    assert removed == ["feat-demo"]


@pytest.mark.parametrize("value", ["", "0", "false", "no"])
def test_falsy_values_do_not_arm_the_kill_switch(
    tmp_path: Path, monkeypatch, value: str
):
    """불변: `=0`을 켜짐으로 읽으면 끄려던 사람이 켜게 된다."""
    removed: list[str] = []
    monkeypatch.setattr(
        CLI, "remove_worktree", lambda **kw: removed.append(kw["status"].name)
    )
    monkeypatch.setenv(KILL_SWITCH, value)

    CLI._cleanup_worktree_after_failure(
        tmp_path, _status(tmp_path), RuntimeError("boom")
    )

    assert removed == ["feat-demo"]
