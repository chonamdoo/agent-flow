from __future__ import annotations

import json
import stat
from pathlib import Path

from agent_flow.cli import main
from agent_flow.core.context_contract import (
    check_system_invariants,
    ensure_context_contract,
    offload_tool_output,
    write_system_invariants,
)
from agent_flow.core.team import (
    add_task,
    add_worker,
    approve_task_result,
    approve_worker_call,
    init_team,
    write_worker_brief,
    write_worker_result,
)
from agent_flow.core.tool_lint import lint_tools
from agent_flow.eval import run_eval


def test_eval_regression_gate_passes_and_fails(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(
        json.dumps(
            [
                {"id": "pass", "actual": "hello secure world", "rubric": [{"id": "security", "must_include": "secure"}]},
                {"id": "fail", "actual": "hello world", "rubric": [{"id": "security", "must_include": "secure"}]},
            ]
        ),
        encoding="utf-8",
    )

    results = run_eval(root=tmp_path, fixture_path=fixtures)

    assert [result.passed for result in results] == [True, False]
    assert (tmp_path / ".agent-flow" / "eval" / "results.json").exists()


def test_eval_cli_returns_nonzero_when_fixture_fails(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(json.dumps({"id": "x", "actual": "no", "expected": "yes"}), encoding="utf-8")

    assert main(["eval", "--root", str(tmp_path), "--fixtures", str(fixtures)]) == 1


def test_eval_judge_requires_json_object(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(json.dumps({"id": "x", "actual": "ok"}), encoding="utf-8")
    judge = tmp_path / "judge.sh"
    judge.write_text("#!/bin/sh\nprintf '%s\\n' 'approve'\n", encoding="utf-8")
    judge.chmod(judge.stat().st_mode | stat.S_IXUSR)

    results = run_eval(root=tmp_path, fixture_path=fixtures, judge_command=(str(judge),))

    assert results[0].passed is False
    assert "JSON object" in results[0].reason


def test_eval_json_judge_requires_boolean_passed(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(json.dumps({"id": "x", "actual": "ok"}), encoding="utf-8")
    judge = tmp_path / "judge.sh"
    judge.write_text("#!/bin/sh\nprintf '%s\\n' '{\"passed\":\"false\",\"reason\":\"bad\"}'\n", encoding="utf-8")
    judge.chmod(judge.stat().st_mode | stat.S_IXUSR)

    results = run_eval(root=tmp_path, fixture_path=fixtures, judge_command=(str(judge),))

    assert results[0].passed is False
    assert "passed must be boolean" in results[0].reason


def test_context_contract_creates_offload_paths(tmp_path: Path) -> None:
    base = ensure_context_contract(root=tmp_path)
    output = offload_tool_output(root=tmp_path, name="pytest output", content="long log")

    assert (base / "context.md").exists()
    assert (base / "events.jsonl").exists()
    assert output.parent.name == "tool_outputs"
    assert "long log" in output.read_text(encoding="utf-8")


def test_system_invariants_check_enforces_path_only_context_markers(tmp_path: Path) -> None:
    write_system_invariants(
        root=tmp_path,
        invariants=[
            "Preserve agent-flow status/next_command as workflow source of truth.",
            "Do not reinstall from a managed worktree.",
            "Use path-only inputs for long context.",
        ],
    )
    base = ensure_context_contract(root=tmp_path)
    (base / "brief.md").write_text("short brief\n", encoding="utf-8")

    assert check_system_invariants(root=tmp_path) == []


def test_tools_lint_detects_schema_drift(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "codex.json").write_text(
        json.dumps({"name": "search", "host": "codex", "description": "Search project files", "schema": {"type": "object"}}),
        encoding="utf-8",
    )
    (tools / "claude.json").write_text(
        json.dumps({"name": "search", "host": "claude", "description": "Search project files", "schema": {"type": "string"}}),
        encoding="utf-8",
    )

    findings = lint_tools(tmp_path)

    assert any("schema drift" in finding.message for finding in findings)


def test_team_worker_contract_writes_brief_result_and_approval(tmp_path: Path) -> None:
    init_team(root=tmp_path, name="feature-team", description="")
    add_worker(root=tmp_path, team_name="feature-team", worker_name="worker-a", role="worker")
    add_task(root=tmp_path, team_name="feature-team", task_id="task-1", subject="Do work", description="")

    brief = write_worker_brief(
        root=tmp_path,
        team_name="feature-team",
        task_id="task-1",
        worker_name="worker-a",
        brief="Implement slice",
        write_scope="src/**, tests/**",
    )
    worker_approval = approve_worker_call(
        root=tmp_path,
        team_name="feature-team",
        task_id="task-1",
        worker_name="worker-a",
        reviewer="lead",
        write_scope="src/**, tests/**",
    )
    result = write_worker_result(
        root=tmp_path,
        team_name="feature-team",
        task_id="task-1",
        worker_name="worker-a",
        result="Implemented",
    )
    approval = approve_task_result(
        root=tmp_path,
        team_name="feature-team",
        task_id="task-1",
        reviewer="lead",
        verdict="approve",
    )

    assert brief.worker == "worker-a"
    assert brief.write_scope == "src/**, tests/**"
    assert worker_approval.write_scope == "src/**, tests/**"
    assert result.result == "Implemented"
    assert approval.verdict == "approve"
    task_dir = tmp_path / ".agent-flow" / "state" / "team" / "feature-team" / "tasks" / "task-1"
    assert (task_dir / "worker-brief.md").exists()
    assert (task_dir / "worker-result.md").exists()
    assert (task_dir / "workers_approved.json").exists()
    assert (task_dir / "events.jsonl").read_text(encoding="utf-8").count("\n") >= 3


def test_team_status_ignores_worker_contract_sidecar_json(tmp_path: Path) -> None:
    init_team(root=tmp_path, name="feature-team", description="")
    add_worker(root=tmp_path, team_name="feature-team", worker_name="worker-a", role="worker")
    add_task(root=tmp_path, team_name="feature-team", task_id="foo.result", subject="Do work", description="")
    write_worker_brief(
        root=tmp_path,
        team_name="feature-team",
        task_id="foo.result",
        worker_name="worker-a",
        brief="Implement slice",
    )
    write_worker_result(
        root=tmp_path,
        team_name="feature-team",
        task_id="foo.result",
        worker_name="worker-a",
        result="Implemented",
    )
    approve_task_result(
        root=tmp_path,
        team_name="feature-team",
        task_id="foo.result",
        reviewer="lead",
        verdict="approve",
    )

    from agent_flow.core.team import list_teams, team_status

    assert list_teams(root=tmp_path)[0].task_count == 1
    status = team_status(root=tmp_path, team_name="feature-team", detail=True)
    assert status["task_count"] == 1
    assert len(status["tasks"]) == 1


def test_team_run_next_uses_worker_brief_and_approval_contract(tmp_path: Path) -> None:
    init_team(root=tmp_path, name="feature-team", description="")
    add_worker(root=tmp_path, team_name="feature-team", worker_name="worker-a", role="worker")
    add_task(root=tmp_path, team_name="feature-team", task_id="task-1", subject="Do work", description="")
    write_worker_brief(
        root=tmp_path,
        team_name="feature-team",
        task_id="task-1",
        worker_name="worker-a",
        brief="Use the worker-brief contract.",
        write_scope="tasks-only",
    )
    approve_worker_call(
        root=tmp_path,
        team_name="feature-team",
        task_id="task-1",
        worker_name="worker-a",
        reviewer="lead",
        write_scope="tasks-only",
    )
    capture = tmp_path / "prompt.txt"

    assert main(
        [
            "team",
            "run-next",
            "--root",
            str(tmp_path),
            "--team",
            "feature-team",
            "--worker",
            "worker-a",
            "--command",
            "sh",
            "-c",
            f"cat > {capture}; printf result",
        ]
    ) == 0

    prompt = capture.read_text(encoding="utf-8")
    assert "worker-brief.md" in prompt
    assert "workers_approved.json" in prompt
    assert "write_scope: tasks-only" in prompt


def test_team_run_next_requires_worker_approval_contract(tmp_path: Path) -> None:
    init_team(root=tmp_path, name="feature-team", description="")
    add_worker(root=tmp_path, team_name="feature-team", worker_name="worker-a", role="worker")
    add_task(root=tmp_path, team_name="feature-team", task_id="task-1", subject="Do work", description="")

    assert main(
        [
            "team",
            "run-next",
            "--root",
            str(tmp_path),
            "--team",
            "feature-team",
            "--worker",
            "worker-a",
            "--command",
            "sh",
            "-c",
            "printf should-not-run",
        ]
    ) == 1

    write_worker_brief(
        root=tmp_path,
        team_name="feature-team",
        task_id="task-1",
        worker_name="worker-a",
        brief="Use the worker-brief contract.",
        write_scope="tasks-only",
    )

    assert main(
        [
            "team",
            "run-next",
            "--root",
            str(tmp_path),
            "--team",
            "feature-team",
            "--worker",
            "worker-a",
            "--command",
            "sh",
            "-c",
            "printf should-not-run",
        ]
    ) == 1


def test_team_run_next_canonicalizes_team_and_worker_for_contract(tmp_path: Path) -> None:
    init_team(root=tmp_path, name="Feature Team", description="")
    add_worker(root=tmp_path, team_name="Feature Team", worker_name="Worker A", role="worker")
    add_task(root=tmp_path, team_name="Feature Team", task_id="task-1", subject="Do work", description="")
    write_worker_brief(
        root=tmp_path,
        team_name="Feature Team",
        task_id="task-1",
        worker_name="Worker A",
        brief="Use the worker-brief contract.",
        write_scope="tasks-only",
    )
    approve_worker_call(
        root=tmp_path,
        team_name="Feature Team",
        task_id="task-1",
        worker_name="Worker A",
        reviewer="lead",
        write_scope="tasks-only",
    )
    capture = tmp_path / "prompt.txt"

    assert main(
        [
            "team",
            "run-next",
            "--root",
            str(tmp_path),
            "--team",
            "Feature Team",
            "--worker",
            "Worker A",
            "--command",
            "sh",
            "-c",
            f"cat > {capture}; printf result",
        ]
    ) == 0

    assert "worker-brief.md" in capture.read_text(encoding="utf-8")


def test_multi_review_requires_subagent_source_for_approval(tmp_path: Path) -> None:
    from agent_flow.runner import Phase, Runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    phase = Phase(id="multi-review", description="", multi_review=True, routes={"approve": "done"})
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.phases = [phase, Phase(id="done", description="")]
    (run_dir / "multi-review.md").write_text(
        "## Reviewer A\nverdict: approve\n\n## Reviewer B\nverdict: approve\n\n## Overall\nverdict: approve\n",
        encoding="utf-8",
    )

    assert runner._next_index(0, phase) == (0, True)
