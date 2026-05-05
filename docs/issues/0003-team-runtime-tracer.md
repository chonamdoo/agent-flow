---
label: needs-triage
type: HITL
---

# Team runtime tracer

## What to build

Add the smallest Team Orchestration runtime path that can launch one host-backed Worker against one claimable Task and record observable Team State.

## Acceptance criteria

- [x] A runtime command claims one pending Task for one Worker.
- [x] The Worker execution uses a host Adapter or Provider, not sandbox execution.
- [x] Task completion or failure is reflected through Team State and visible in `team status --detail`.

## Blocked by

- Host provider discovery.
- Worktree-backed run start.
