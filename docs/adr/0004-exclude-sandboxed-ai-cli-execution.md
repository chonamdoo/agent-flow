# 0004 Exclude Sandboxed AI CLI Execution

## Status

Accepted

2026-10-11 update: Gemini and antigravity host support was removed in [`d69e4f4`](https://github.com/chonamdoo/agent-flow/commit/d69e4f49ff655938ca5e2fc5eed560be1850b2fe) on 2026-06-10. Current hosted adapters are `claude`, `codex`, and `omp`; manual execution uses the `generic` adapter. The body below preserves the original decision and its historical host examples.

Author sessions use host configuration and authentication. Reviewer subprocesses reuse host authentication but ignore host configuration: Claude runs with `--safe-mode`; Codex runs with `--ignore-user-config` and a private `CODEX_HOME` containing only the copied `auth.json`.

## Context

Agent Flow may use different AI CLIs through Adapters and Providers. These CLIs often depend on host authentication, local configuration, process permissions, and interactive behavior.

Running those CLIs inside an isolated sandbox would create unreliable behavior and extra setup burden. The Workflow Kit needs predictable execution across personal projects before adding process-environment isolation.

## Decision

Do not support sandboxed AI CLI execution.

Keep Worktree support as the isolation model for file changes. Run AI CLIs on the host through Adapters or Providers.

## Consequences

- Worktree remains available for file-change isolation.
- Codex, Claude, Gemini, and similar CLIs are expected to use host configuration and authentication.
- The project avoids Docker/Podman/Vercel sandbox integration in the core scope.
- Gate execution may still be improved later, but sandbox support is not part of the current Workflow Kit model.

## Alternatives Considered

### Sandbox AI CLIs

This could improve process isolation, but it conflicts with host-bound authentication and CLI behavior.

### Remove all isolation concepts

This would simplify the model, but Worktrees remain useful for preserving and reviewing file changes independently.

