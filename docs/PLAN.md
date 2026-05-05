# Agent Flow Work Plan

## Direction

Build Agent Flow as a reusable Workflow Kit.

Default execution is Personal Workflow: the lead orchestrates Runs, stages, prompts, artifacts, and gates. Team Orchestration is an optional future module with separate Team State.

## Current Slice

Make adapter-specific prompt rendering operational.

### Scope

- Render stage prompts from adapter-specific templates.
- Select Codex, Claude, or generic templates from the chosen Adapter.
- Keep unresolved template placeholders as hard failures.
- Keep AI CLI execution on host adapters/providers; no sandbox support.

### Non-Goals

- No Team Orchestration runtime.
- No Worktree management.
- No direct Codex/Claude/Gemini provider execution.
- No sandbox execution.

## Next Slices

1. Review/fix loop wiring.
2. Worktree support for file-change isolation.
3. Optional Team Orchestration state model.
