from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolate_agent_flow_user_state(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Keep user-scoped Agent Flow state out of the developer's real home."""
    previous_state_home = os.environ.get("XDG_STATE_HOME")
    previous_shared_home = os.environ.get("AGENT_FLOW_SHARED_HOME")
    isolated_state = tmp_path_factory.mktemp("agent-flow-state")
    os.environ["XDG_STATE_HOME"] = str(isolated_state)
    os.environ["AGENT_FLOW_SHARED_HOME"] = str(isolated_state / "shared")
    try:
        yield
    finally:
        if previous_state_home is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = previous_state_home
        if previous_shared_home is None:
            os.environ.pop("AGENT_FLOW_SHARED_HOME", None)
        else:
            os.environ["AGENT_FLOW_SHARED_HOME"] = previous_shared_home
