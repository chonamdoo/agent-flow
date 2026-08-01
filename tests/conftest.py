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
