# Agent Flow Work Plan

## Direction

Build Agent Flow as a reusable Workflow Kit.

Default execution is Personal Workflow: the lead orchestrates Runs, stages, prompts, artifacts, and gates. Team Orchestration is an optional future module with separate Team State.

## Current Slice

Make stage results and handoffs operational.

### Scope

- Record stage result artifacts under a Run.
- Write handoffs for stage transitions.
- Keep handoffs readable from both the Run and the project-level handoff index.
- Keep AI CLI execution on host adapters/providers; no sandbox support.

### Non-Goals

- No Team Orchestration runtime.
- No Worktree management.
- No direct Codex/Claude/Gemini provider execution.
- No sandbox execution.

## Next Slices

1. Adapter-specific prompt rendering.
2. Review/fix loop wiring.
3. Worktree support for file-change isolation.
4. Optional Team Orchestration state model.
