# Agent Flow Work Plan

## Direction

Build Agent Flow as a reusable Workflow Kit.

Default execution is Personal Workflow: the lead orchestrates Runs, stages, prompts, artifacts, and gates. Team Orchestration is an optional future module with separate Team State.

## Current Slice

Make Project Profiles operational.

### Scope

- Load packaged Project Profiles by id.
- Convert profile gate definitions into executable Gate commands.
- Add a CLI command to run gates for a target project.
- Persist gate results as Run artifacts when a run directory is supplied.
- Keep AI CLI execution on host adapters/providers; no sandbox support.

### Non-Goals

- No Team Orchestration runtime.
- No Worktree management.
- No direct Codex/Claude/Gemini provider execution.
- No sandbox execution.

## Next Slices

1. Stage result artifacts and handoffs.
2. Adapter-specific prompt rendering.
3. Review/fix loop wiring.
4. Worktree support for file-change isolation.
5. Optional Team Orchestration state model.

