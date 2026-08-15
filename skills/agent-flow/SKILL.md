---
name: agent-flow
description: Use when the user types /agent-flow, asks to start or continue the project workflow, or wants Claude, Codex, or OMP to drive the agent-flow lifecycle.
---

# Agent Flow

Use this skill as the common entry point for the project-local agent-flow workflow.

## Slash Trigger

Dispatch exact inputs in this order:

1. For `/agent-flow status`, run `agent-flow status` from the project root and report its output.
2. At the start of any other new session, run `agent-flow status` from the project root before choosing a lifecycle command.
3. When status reports an active run, execute its printed `next_command`; do not start a second run.
4. When status exits 0 and reports no active run:
   - `/agent-flow` with no task asks the user for `/agent-flow <task>`.
   - `/agent-flow <task>` runs:

```bash
agent-flow run "<task>"
```

Treat the status command output as the only source of truth. Use the `run` output for the actual worktree, branch, phase, and next command instead of predicting them.

Do not run install just because a new session started. Install is project setup, not the normal task entry. Do not infer npm, npx, or install failure unless the command exits non-zero with that error.

## SPEC Change Confirmation

The initial SPEC list is baselined without a separate approval step. When status reports later additions, modifications, or deletions:

- Show only that delta and ask the user for confirmation in the current chat.
- After a clear affirmative reply, run the printed `agent-flow spec confirm --run-dir <run-dir>`.
- For a `manual` verifier, ask in chat and then run `agent-flow spec approve <spec-id> --run-dir <run-dir>`.

Never require an exact phrase or ask the user to enter a terminal command.

## Behavior

- Treat `/agent-flow` as a project-local workflow trigger, not as a shell path.
- Keep git-project runtime state private under the repository git dir, such as `.git/agent-flow/worktrees/feat-<slug>/`; expose it only for status, debugging, or artifact inspection.
- After a phase writes its artifact, run the `next_command` printed by status or the current phase output.
- If the workflow pauses for design or slice review, summarize the relevant artifact and wait for user approval before continuing.
- During code generation, modification, and code review phases, apply `code-generation-discipline`. Skill resolution and missing-skill handling are defined there; do not restate them here.
- Keep user-facing replies short Korean by default. Keep code, commands, paths, and identifiers in English.
- Do not paste long logs or whole files. Summarize only current phase, action, `next_command`, and blocker when useful.
