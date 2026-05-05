# Agent Flow Work Plan

## Direction

Build Agent Flow as a reusable Workflow Kit.

Default execution is Personal Workflow: the lead orchestrates Runs, stages, prompts, artifacts, and gates. Team Orchestration is an optional future module with separate Team State.

## Completed Slices

Make Team mailbox message records operational.

Make Worker heartbeat updates operational.

Make Team shutdown signal records operational.

Make Team status detail view operational.

Make Team state export operational.

Make Team state import validation operational.

Make Team state import dry-run summary operational.

Make Team state schema hardening operational.

Make Team state import dry-run file report operational.

Make Team state import apply operational.

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

## Current Slice

Make Team state import conflict reporting operational.

### Scope

- Write optional import-apply reports as deterministic JSON.
- Include validation, read, and existing-team conflict errors in the report.
- Preserve successful import summary fields in the report.
- Preserve Team Orchestration as state-only; no worker process execution.
- Keep AI CLI execution on host adapters/providers; no sandbox support.

### Non-Goals

- No Team Orchestration runtime.
- No process supervision or process termination.
- No direct Codex/Claude/Gemini provider execution.
- No sandbox execution.

## Next Slices

1. Team state import cleanup diagnostics.
