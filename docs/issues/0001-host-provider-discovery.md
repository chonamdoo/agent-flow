---
label: needs-triage
type: AFK
---

# Host provider discovery

## What to build

Expose a public CLI path that reports which host AI providers are available for Adapter execution without running sandboxed commands.

## Acceptance criteria

- [x] `agent-flow provider list` reports `manual`, `codex-session`, `claude-session`, and `omp-session` providers.
- [x] Provider availability is based on host environment variables or executables.
- [x] Output is deterministic enough for tests and scripting.

## Blocked by

None - can start immediately.
