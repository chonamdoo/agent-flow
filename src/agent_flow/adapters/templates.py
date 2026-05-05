from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
import re


@dataclass(frozen=True)
class PromptContext:
    adapter: str
    stage_id: str
    role: str
    workflow_id: str
    run_id: str
    replica: int
    replicas: int
    task: str


def render_stage_prompt(context: PromptContext) -> str:
    template = _read_stage_template(context.adapter)
    values = {
        "adapter": context.adapter,
        "stage_id": context.stage_id,
        "role": context.role,
        "workflow_id": context.workflow_id,
        "run_id": context.run_id,
        "replica": str(context.replica),
        "replicas": str(context.replicas),
        "task": context.task,
    }
    placeholders = set(re.findall(r"{{([a-zA-Z_][a-zA-Z0-9_]*)}}", template))
    unknown = sorted(placeholders - values.keys())
    if unknown:
        raise ValueError(f"unknown template placeholders for adapter {context.adapter}: {', '.join(unknown)}")
    for key, value in values.items():
        template = template.replace(f"{{{{{key}}}}}", value)
    return template


def _read_stage_template(adapter: str) -> str:
    template_name = _template_name(adapter)
    path = resources.files("agent_flow").joinpath("templates", template_name, "stage.md")
    if not path.is_file():
        path = resources.files("agent_flow").joinpath("templates", "generic", "stage.md")
    return path.read_text(encoding="utf-8")


def _template_name(adapter: str) -> str:
    if adapter.startswith("codex"):
        return "codex"
    if adapter.startswith("claude"):
        return "claude"
    return "generic"
