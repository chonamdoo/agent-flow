# Agent Flow Work Plan

## Direction

Build Agent Flow as a reusable Workflow Kit.

Default execution is Personal Workflow: the lead orchestrates Runs, stages, prompts, artifacts, and gates. Team Orchestration is an optional future module with separate Team State.

## Current Slice

Make Worktree support operational.

### Scope

- Plan deterministic worktree paths and branch names.
- Create git worktrees for isolated file changes.
- Refuse dirty leader workspaces by default.
- Report worktree status from manifest data.
- Keep AI CLI execution on host adapters/providers; no sandbox support.

### Non-Goals

- No Team Orchestration runtime.
- No Worktree management.
- No direct Codex/Claude/Gemini provider execution.
- No sandbox execution.

## Next Slices

1. Optional Team Orchestration state model.
