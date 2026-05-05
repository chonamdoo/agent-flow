# Agent Flow Work Plan

## Direction

Build Agent Flow as a reusable Workflow Kit.

Default execution is Personal Workflow: the lead orchestrates Runs, stages, prompts, artifacts, and gates. Team Orchestration is an optional future module with separate Team State.

## Current Slice

Make Team mailbox message records operational.

### Scope

- Send messages to registered Worker mailboxes.
- List messages for a Worker.
- Filter unread messages.
- Mark messages as read.
- Preserve Team Orchestration as state-only; no worker process execution.
- Keep AI CLI execution on host adapters/providers; no sandbox support.

### Non-Goals

- No Team Orchestration runtime.
- No Worktree management.
- No direct Codex/Claude/Gemini provider execution.
- No sandbox execution.

## Next Slices

1. Worker heartbeat update CLI.
