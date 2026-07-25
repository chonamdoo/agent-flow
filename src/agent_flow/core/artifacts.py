from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agent_flow.core.gates import GateResult
from agent_flow.core.report import write_run_report
from agent_flow.core.worktree_isolation import AGENT_FLOW_STATE_DIRS


def init_project(root: Path) -> None:
    # tripwire가 비교에서 빼는 목록과 **같은 소스**여야 한다. 갈라지면 정상
    # 명령이 leader 오염으로 오탐된다.
    for name in AGENT_FLOW_STATE_DIRS:
        (root / ".agent-flow" / name).mkdir(parents=True, exist_ok=True)


def write_prompt(*, root: Path, run_dir: Path, stage_id: str, content: str) -> Path:
    init_project(root)
    path = run_dir / "prompts" / f"{stage_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def write_gate_results(*, run_dir: Path, results: list[GateResult]) -> Path:
    passed = all(result.passed or not result.required for result in results)
    serialized_results = [_gate_result_payload(result) for result in results]
    payload = {
        "passed": passed,
        "status": "green" if passed else "request-changes",
        "results": serialized_results,
    }
    path = run_dir / "artifacts" / "gate-results.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{json.dumps(payload, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )
    legacy_path = run_dir / "gate-results.json"
    legacy_path.write_text(
        f"{json.dumps(serialized_results, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )
    write_run_report(run_dir)
    return path


def _gate_result_payload(result: GateResult) -> dict[str, object]:
    return {
        "gate_id": result.gate_id,
        "command": " ".join(result.command),
        "argv": list(result.command),
        "passed": result.passed,
        "required": result.required,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def write_stage_result(
    *,
    run_dir: Path,
    stage_id: str,
    content: str,
    status: str = "completed",
    evidence_type: str = "observed",
    confidence: str = "unknown",
) -> Path:
    path = run_dir / "artifacts" / f"{stage_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                f"# Stage Result: {stage_id}",
                "",
                f"- Status: {status}",
                f"- Evidence Type: {evidence_type}",
                f"- Confidence: {confidence}",
                f"- Recorded At: {_now()}",
                "",
                content.rstrip(),
                "",
            ]
        ),
        encoding="utf-8",
    )
    write_run_report(run_dir)
    return path


def write_handoff(
    *,
    root: Path,
    run_dir: Path,
    from_stage: str,
    to_stage: str,
    decided: str,
    rejected: str,
    risks: str,
    files: str,
    remaining: str,
) -> Path:
    init_project(root)
    filename = f"{from_stage}-to-{to_stage}.md"
    content = "\n".join(
        [
            f"# Handoff: {from_stage} -> {to_stage}",
            "",
            f"- Decided: {decided or 'None'}",
            f"- Rejected: {rejected or 'None'}",
            f"- Risks: {risks or 'None'}",
            f"- Files: {files or 'None'}",
            f"- Remaining: {remaining or 'None'}",
            "",
        ]
    )
    run_path = run_dir / "handoffs" / filename
    run_path.parent.mkdir(parents=True, exist_ok=True)
    run_path.write_text(content, encoding="utf-8")
    write_run_report(run_dir)

    project_path = root / ".agent-flow" / "handoffs" / filename
    project_path.parent.mkdir(parents=True, exist_ok=True)
    project_path.write_text(content, encoding="utf-8")
    return run_path


def write_recovery(
    *,
    run_dir: Path,
    title: str,
    cause: str,
    artifacts: list[str],
    rerun_command: str,
    manual_action: str,
) -> Path:
    path = run_dir / "recovery.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Recovery: {title}",
        "",
        f"- Cause: {cause or 'Unknown'}",
        f"- Rerun Command: {rerun_command or 'None'}",
        f"- Manual Action: {manual_action or 'None'}",
        "",
        "## Artifacts",
        "",
    ]
    lines.extend(f"- {artifact}" for artifact in artifacts) if artifacts else lines.append("- None")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    write_run_report(run_dir)
    return path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
