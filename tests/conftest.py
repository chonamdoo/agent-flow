from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.shard_policy import SHARDS, shard_for_test


_RESULTS: dict[str, str] = {}
_COLLECTED: set[str] = set()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("agent-flow test shards")
    group.addoption(
        "--agent-flow-shard",
        choices=(*SHARDS, "targeted"),
        default=None,
        help="Select one agent-flow test shard. Explicit test paths default to targeted.",
    )
    group.addoption(
        "--agent-flow-report",
        default=None,
        help="Write a run-scoped JSON test result report.",
    )


def pytest_configure(config: pytest.Config) -> None:
    _RESULTS.clear()
    _COLLECTED.clear()
    for shard in SHARDS:
        marker = shard.replace("-", "_")
        config.addinivalue_line("markers", f"{marker}: agent-flow {shard} test shard")
    config.addinivalue_line(
        "markers",
        "git_auth: pinned_run fixture must use a real authenticated git worktree",
    )

# Tests that spawn real subprocess guards or the Node CLI fork heavily; under a
# full `-n auto` fan-out they stampede git/process resources and flake. Pinning
# them to a small number of xdist groups (with --dist loadgroup) caps how many
# run concurrently while the light in-process tests still spread across workers.
_GIT_AUTH_GROUPS = 2
_NODE_HEAVY_HINTS = (
    "node_follow_up",
    "node_run_start",
)
_GIT_HEAVY_HINTS = (
    "parallel_execution_threshold",
)


def _assign_heavy_group(item: "pytest.Item", test_name: str, index: int) -> None:
    # The Node CLI install/run tests are the biggest fork spike, so they share one
    # group and never run concurrently. git_auth tests spawn real subprocess guards
    # (which call git); spreading them over a few groups caps concurrent git load
    # so a neighbouring fork storm cannot time out their git identity checks.
    if any(hint in test_name for hint in _NODE_HEAVY_HINTS):
        item.add_marker(pytest.mark.xdist_group("node_cli_serial"))
    elif item.get_closest_marker("git_auth") is not None or any(
        hint in test_name for hint in _GIT_HEAVY_HINTS
    ):
        item.add_marker(pytest.mark.xdist_group(f"git_auth_{index % _GIT_AUTH_GROUPS}"))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    requested = config.getoption("--agent-flow-shard") or _default_shard(config)
    if requested == "targeted":
        for index, item in enumerate(items):
            _assign_heavy_group(item, item.name.split("[", 1)[0], index)
        _COLLECTED.update(item.nodeid for item in items)
        return
    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    heavy_index = 0
    for item in items:
        path = Path(str(item.path))
        test_name = item.name.split("[", 1)[0]
        shard = shard_for_test(path, test_name)
        item.add_marker(getattr(pytest.mark, shard.replace("-", "_")))
        if shard == requested:
            _assign_heavy_group(item, test_name, heavy_index)
            heavy_index += 1
            selected.append(item)
        else:
            deselected.append(item)
    items[:] = selected
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    _COLLECTED.update(item.nodeid for item in selected)


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node: Any, ids: list[str]) -> None:
    _COLLECTED.update(ids)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "setup" and report.failed:
        _RESULTS[report.nodeid] = "failed"
    elif report.when == "setup" and report.skipped:
        _RESULTS[report.nodeid] = "skipped"
    elif report.when == "call":
        if report.passed:
            _RESULTS[report.nodeid] = "passed"
        elif report.failed:
            _RESULTS[report.nodeid] = "failed"
        elif report.skipped:
            _RESULTS[report.nodeid] = "skipped"
    elif report.when == "teardown" and report.failed:
        _RESULTS[report.nodeid] = "failed"


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if getattr(session.config, "workerinput", None) is not None:
        return
    report_path = session.config.getoption("--agent-flow-report")
    if not report_path:
        return
    collected = sorted(_COLLECTED or {item.nodeid for item in session.items})
    payload: dict[str, Any] = {
        "exit_code": exitstatus,
        "collected": collected,
        "completed": sorted(nodeid for nodeid, status in _RESULTS.items() if status == "passed"),
        "failed": sorted(nodeid for nodeid, status in _RESULTS.items() if status == "failed"),
        "skipped": sorted(nodeid for nodeid, status in _RESULTS.items() if status == "skipped"),
        "unrun": sorted(nodeid for nodeid in collected if nodeid not in _RESULTS),
    }
    destination = Path(report_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{destination.suffix}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)


def _default_shard(config: pytest.Config) -> str:
    mark_expression = str(getattr(config.option, "markexpr", "") or "").strip().replace("_", "-")
    if mark_expression in SHARDS:
        return mark_expression
    explicit = any("::" in value or Path(value).suffix in {".py", ".mjs"} for value in config.args)
    return "targeted" if explicit else "fast"
