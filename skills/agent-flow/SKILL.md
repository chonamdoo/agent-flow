---
name: agent-flow
description: Runs the project-local agent-flow lifecycle from slash-triggered tasks, status checks, and phase next commands. Use when the user types /agent-flow, asks to start or continue the project workflow, or wants Claude, Codex, or OMP to drive the agent-flow lifecycle.
---

# Agent Flow

Use this skill as the common entry point for the project-local agent-flow workflow.

## Quick start

1. From the project root, run `agent-flow status` for `/agent-flow` with no task, or `agent-flow run "<task>"` for `/agent-flow <task>`.
2. Treat the command output as the source of truth and follow its `next_command`.
3. Do not reinstall agent-flow or infer missing setup unless an agent-flow command exits non-zero with that setup error.

## Slash Trigger

When the user types `/agent-flow <task>`, run:

```bash
agent-flow run "<task>"
```

Do not reinstall agent-flow for each task. Install is project setup, not the normal task entry.
In a git repo, `agent-flow run "<task>"` starts the run inside `.agent-flow/worktrees/feat-<slug>/` on branch `feat/<slug>`.

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

## Behavior

- Treat `/agent-flow` as a project-local workflow trigger, not as a shell path.
- Keep git-project runtime state private under the repository git dir, such as `.git/agent-flow/worktrees/feat-<slug>/`; expose it only for status, debugging, or artifact inspection.
- On a new session, always check `agent-flow status` first and continue from that result.
- After a phase writes its artifact, run the `next_command` printed by status or the current phase output.
- Run direct build, test, typecheck, and lint commands from the pinned worktree through `agent-flow gate -- <command ...>`; do not run unsandboxed gate launchers.
- For Android gates, run `agent-flow gates --run-dir <run-dir>` first so changed production modules, unit tests, and instrumented tests are selected independently. A targeted pass is recorded separately and cannot advance the workflow; after targeted success on frozen final code, run the same command with `--full` exactly once.
- Reuse a successful run-scoped gate result only when its command, Git scope, production/test/dependency/config hashes, toolchain, profile, host, and relevant environment fingerprint match exactly.
- Reviewer sub-agents inspect the existing gate result and fingerprint evidence instead of rerunning test suites. Request only the targeted test needed for a new finding.
- If the workflow pauses for design or slice review, summarize the relevant artifact and wait for user approval before continuing.
- During code generation, modification, and code review phases, apply `code-generation-discipline`. Resolve required skills from active profile metadata, installed skill index, changed files, and task scope. Load only the touched profile skill union. If a required local skill is missing, report it and wait for install or explicit override.
- Keep user-facing replies short Korean by default. Keep code, commands, paths, and identifiers in English.
- Do not paste long logs or whole files. Summarize only current phase, action, `next_command`, and blocker when useful.
