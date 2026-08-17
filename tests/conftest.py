from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolate_agent_flow_user_state(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Keep user-scoped Agent Flow state out of the developer's real home."""
    previous = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = str(tmp_path_factory.mktemp("agent-flow-state"))
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = previous


@pytest.fixture(scope="session", autouse=True)
def skip_install_fsync() -> Iterator[None]:
    """install의 fsync를 이 스위트 안에서만 내린다.

    이 스위트는 install을 460번 부르고 회당 fsync가 676번이다 — 그것만으로 전 스위트
    30분 중 20분이었다(fsync를 내리면 14분, `-n auto`까지 더하면 5분). fsync가 사는
    것은 전원 손실 뒤의 내구성인데, 여기 install 대상은 곧 지워지는 tmpdir이라 그
    관측자가 없다. rename 원자성은 스위치와 무관하게 유지된다.

    이 기본값이 판정을 삼키지 않게 두 곳이 막는다.
      - 사본을 남긴 직후 원본을 없애는 경로는 `atomicWriteFileSync`에 `durable: true`를
        주어 이 변수를 아예 보지 않는다. 그 계약은 스위트 기본값 그대로 검증된다.
      - 기본값 자체를 보는 probe는 `tests/test_installer_atomic_writes.py`의
        `_user_default_env()`로 이 변수를 **지운** 채 돈다. 그래서 `durableInstallWrites()`를
        opt-in으로 뒤집으면 전 스위트가 초록으로 남지 않는다.

    값을 이미 준 실행은 그대로 둔다 — 손으로 `1`을 준 사람은 fsync를 보려는 것이다.
    `package.json`의 `test`가 `python3 -m unittest tests.test_cli`로 도는 구간은 conftest를
    로드하지 않으므로 이 fixture도, 위 `isolate_agent_flow_user_state`도 닿지 않는다.
    """
    previous = os.environ.get("AGENT_FLOW_INSTALL_FSYNC")
    if previous is None:
        os.environ["AGENT_FLOW_INSTALL_FSYNC"] = "0"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("AGENT_FLOW_INSTALL_FSYNC", None)
