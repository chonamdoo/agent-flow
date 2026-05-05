---
label: needs-triage
type: AFK
---

# Worktree-backed run start

## What to build

Let a Personal Workflow Run optionally create and record a project-local git worktree so stage work can happen outside the lead workspace.

## Acceptance criteria

- [x] Starting a run can request a named Worktree.
- [x] The Run manifest records the Worktree name, branch, and path.
- [x] Existing Worktree safety checks still prevent accidental dirty-workspace conflicts.

## Blocked by

- Host provider discovery.
