from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StagePrompt:
    stage_id: str
    role: str
    prompt: str


class Adapter:
    name = "manual"

    def render_stage_prompt(self, *, stage_id: str, role: str, task: str) -> StagePrompt:
        return StagePrompt(stage_id=stage_id, role=role, prompt=f"{role}: {task}\n")

