from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Stage:
    stage_id: str
    role: str
    parallel: bool = False
    replicas: int = 1


@dataclass(frozen=True)
class Workflow:
    workflow_id: str
    stages: tuple[Stage, ...]


def load_workflow(workflow_id: str) -> Workflow:
    payload = yaml.safe_load(_read_workflow_text(workflow_id))
    if not isinstance(payload, dict):
        raise ValueError(f"workflow must be a mapping: {workflow_id}")
    if payload.get("id") != workflow_id:
        raise ValueError(f"workflow id mismatch: {workflow_id}")
    stages = payload.get("stages")
    if stages is None:
        stages = payload.get("phases")
    if not isinstance(stages, list) or not stages:
        raise ValueError(f"workflow stages/phases must be a non-empty list: {workflow_id}")
    return Workflow(
        workflow_id=payload["id"],
        stages=tuple(
            _stage_from_payload(item, workflow_id=workflow_id)
            for item in stages
        ),
    )


def _stage_from_payload(item: object, *, workflow_id: str) -> Stage:
    if not isinstance(item, dict):
        raise ValueError(f"workflow stage must be a mapping: {workflow_id}")
    stage_id = item.get("id")
    role = item.get("role")
    if not isinstance(stage_id, str) or not stage_id:
        raise ValueError(f"workflow stage id missing: {workflow_id}")
    if role is None:
        role = ""
    elif not isinstance(role, str):
        raise ValueError(f"workflow stage role must be a string: {workflow_id}:{stage_id}")
    parallel = item.get("parallel", False)
    replicas = item.get("replicas", 1)
    if not isinstance(parallel, bool):
        raise ValueError(f"workflow stage parallel must be boolean: {workflow_id}:{stage_id}")
    if not isinstance(replicas, int) or isinstance(replicas, bool):
        raise ValueError(f"workflow stage replicas must be integer: {workflow_id}:{stage_id}")
    if replicas < 1:
        raise ValueError(f"workflow stage replicas must be >= 1: {workflow_id}:{stage_id}")
    return Stage(
        stage_id=stage_id,
        role=role,
        parallel=parallel,
        replicas=replicas,
    )


def _read_workflow_text(workflow_id: str) -> str:
    package_path = resources.files("agent_flow").joinpath("workflows", f"{workflow_id}.yaml")
    if package_path.is_file():
        return package_path.read_text(encoding="utf-8")
    repo_path = Path(__file__).resolve().parents[3] / "workflows" / f"{workflow_id}.yaml"
    return repo_path.read_text(encoding="utf-8")
