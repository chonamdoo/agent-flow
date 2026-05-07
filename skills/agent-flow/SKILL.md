---
name: agent-flow
description: Use when the user types /agent-flow, asks to start or continue the project workflow, or wants Claude, Codex, or Gemini to drive the agent-flow lifecycle.
---

# Agent Flow

Use this skill as the common entry point for the project-local `agent-flow`
workflow.

## Slash Trigger

When the user types `/agent-flow <task>`, run:

```bash
agent-flow run "<task>" --worktree "<short-task-slug>"
```

Use a short kebab-case worktree slug derived from the task. The run path is
worktree-backed and may run alongside other active runs.

When the user types `/agent-flow` with no task:

- Run `agent-flow worktree list` to see available worktrees.
- If exactly one worktree is listed, use that slug.
- If multiple worktrees are listed, ask the user which slug to continue.
- Run `agent-flow continue --worktree "<short-task-slug>"`.

When the user types `/agent-flow status`, run:

```bash
agent-flow status --worktree "<short-task-slug>"
```

When the user types `/agent-flow abort`, run:

```bash
agent-flow abort --worktree "<short-task-slug>"
```

## Behavior

- Treat `/agent-flow` as a project-local workflow trigger, not as a shell path.
- Keep `.agent-flow/runs/` and `.agent-flow/worktrees/` as internal state; expose them only for
  status, debugging, or artifact inspection.
- Worktree-backed runs write their run artifacts inside the worktree checkout.
- After a completed or aborted worktree run no longer needs its checkout, run
  `agent-flow worktree remove --name "<short-task-slug>"`.
- If the workflow pauses on PR comments, fix the actionable comments, push, and
  resolve the corresponding GitHub review threads before returning to
  `pr-watch`.
- If the workflow pauses for design or slice review, summarize the relevant
  artifact and wait for user approval before continuing.
