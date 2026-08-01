---
name: agent-flow
description: Use when the user types /agent-flow, asks to start or continue the project workflow, or wants Claude, Codex, or OMP to drive the agent-flow lifecycle.
---

# Agent Flow

Use this skill as the common entry point for the project-local agent-flow workflow.

## Slash Trigger

When the user types `/agent-flow <task>`, run:

```bash
agent-flow run "<task>"
```

Do not reinstall agent-flow for each task. Install is project setup, not the normal task entry.
In a git repo, `agent-flow run "<task>"` starts the run inside `~/.agent-flow/worktrees/<repo-id>/feat-<slug>/` on branch `feat/<slug>`.

When the user types `/agent-flow` with no task:

- Run `agent-flow status` from the project root.
- Treat the status command output as the only source of truth.
- If status exits 0 and reports an active run, follow the `next_command` from status.
- If status exits non-zero with `no active run`, ask for a task using `/agent-flow <task>`.
- Do not infer npm, npx, or install failure unless the command actually exits non-zero with that error.
- Do not run install just because a new session started.

When the user types `/agent-flow status`, run:

```bash
agent-flow status
```

## User-Only Approval

When a run waits for SPEC set confirmation:

- On a supported Codex, Claude, or OMP host, show the complete ordered SPEC list and ask the user to reply exactly `승인` in a new turn of the current chat. The managed user-prompt hook confirms the current pending SPEC.
- Only when that hook is unavailable, ask the user to run the path-free fallback `agent-flow spec confirm` from the target worktree in an interactive terminal.
- A `manual` verifier still requires the user to run `agent-flow spec approve <spec-id> --run-dir <run-dir>`.

Never run the fallback or manual approval command, or invoke the user-prompt hook yourself. Confirmation or approval observed from an agent shell is void and the phase stays blocked.

## Behavior

- Treat `/agent-flow` as a project-local workflow trigger, not as a shell path.
- Keep git-project runtime state private under the repository git dir, such as `.git/agent-flow/worktrees/feat-<slug>/`; expose it only for status, debugging, or artifact inspection.
- On a new session, always check `agent-flow status` first and continue from that result.
- After a phase writes its artifact, run the `next_command` printed by status or the current phase output.
- If the workflow pauses for design or slice review, summarize the relevant artifact and wait for user approval before continuing.
- During code generation, modification, and code review phases, apply `code-generation-discipline`. Resolve required skills from active profile metadata, installed skill index, changed files, and task scope. Load only the touched profile skill union. If a required local skill is missing, report it and wait for install or explicit override.
- Keep user-facing replies short Korean by default. Keep code, commands, paths, and identifiers in English.
- Do not paste long logs or whole files. Summarize only current phase, action, `next_command`, and blocker when useful.
