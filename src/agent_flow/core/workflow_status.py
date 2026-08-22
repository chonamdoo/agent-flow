"""Stable human-readable and JSON workflow status output."""
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import NotRequired, TypedDict


class WorkflowStatusPayload(TypedDict):
    status: str
    run: str
    task: str
    current_phase: str
    reason: str
    detail: str | None
    required_artifact: str | None
    report: str | None
    next_command: str
    # 빈 목록은 키째로 빠진다. `status_json`은 payload를 그대로 덮으므로 항상
    # 싣으면, 아직 아무 marker도 요구하지 않은 blocker(skill_scope_grew 등)의
    # 출력에까지 이 이름이 섞여 사용자는 요구받지 않은 것을 요구받았다고 읽는다.
    missing_completion_markers: NotRequired[list[str]]


def workflow_status_payload(
    *,
    status: str,
    run: str,
    task: str,
    current_phase: str,
    reason: str,
    next_command: str,
    required_artifact: str | Path | None = None,
    report: str | Path | None = None,
    detail: str | None = None,
    missing_completion_markers: Sequence[str] = (),
) -> WorkflowStatusPayload:
    payload: WorkflowStatusPayload = {
        "status": status,
        "run": run,
        "task": task,
        "current_phase": current_phase,
        "reason": reason,
        "detail": detail,
        "required_artifact": (
            str(required_artifact) if required_artifact is not None else None
        ),
        "report": str(report) if report is not None else None,
        "next_command": next_command,
    }
    if missing_completion_markers:
        payload["missing_completion_markers"] = list(missing_completion_markers)
    return payload


def print_structured_status(payload: WorkflowStatusPayload) -> None:
    print(f"status: {status_value(payload['status'])}")
    print(f"run: {status_value(payload['run'])}")
    print(f"task: {status_value(payload['task'])}")
    print(f"current_phase: {status_value(payload['current_phase'])}")
    print(f"reason: {status_value(payload['reason'])}")
    if payload["required_artifact"] is not None:
        print(
            "required_artifact: "
            f"{status_value(payload['required_artifact'])}"
        )
    if payload["report"] is not None:
        print(f"report: {status_value(payload['report'])}")
    if payload["detail"] is not None:
        print(f"detail: {status_value(payload['detail'])}")
    markers = payload.get("missing_completion_markers")
    if markers:
        print(
            "missing_completion_markers: "
            f"{json.dumps(markers, ensure_ascii=False)}"
        )
    print(f"next_command: {status_value(payload['next_command'])}")
    print(f"status_json: {json.dumps(payload, sort_keys=True)}")


def status_value(value: object) -> str:
    return str(value).replace("\r", "\\r").replace("\n", "\\n")
