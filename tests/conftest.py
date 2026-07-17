from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.shard_policy import SHARDS, shard_for_test


_RESULTS: dict[str, str] = {}


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
    for shard in SHARDS:
        marker = shard.replace("-", "_")
        config.addinivalue_line("markers", f"{marker}: agent-flow {shard} test shard")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    requested = config.getoption("--agent-flow-shard") or _default_shard(config)
    if requested == "targeted":
        return
    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        path = Path(str(item.path))
        test_name = item.name.split("[", 1)[0]
        shard = shard_for_test(path, test_name)
        item.add_marker(getattr(pytest.mark, shard.replace("-", "_")))
        (selected if shard == requested else deselected).append(item)
    items[:] = selected
    if deselected:
        config.hook.pytest_deselected(items=deselected)


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
    report_path = session.config.getoption("--agent-flow-report")
    if not report_path:
        return
    collected = [item.nodeid for item in session.items]
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
