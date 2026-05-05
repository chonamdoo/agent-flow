---
label: needs-triage
type: AFK
---

# Worktree-backed run start

## What to build

Let a Personal Workflow Run optionally create and record a project-local git worktree so stage work can happen outside the lead workspace.

## Acceptance criteria

- [ ] Starting a run can request a named Worktree.
- [ ] The Run manifest records the Worktree name, branch, and path.
- [ ] Existing Worktree safety checks still prevent accidental dirty-workspace conflicts.

## Blocked by

- Host provider discovery.
