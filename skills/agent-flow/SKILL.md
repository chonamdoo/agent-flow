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
agent-flow run "<task>"
```

When the user types `/agent-flow` with no task:

- Run `agent-flow status` from the project root.
- If an active run exists, run `agent-flow continue`.
- If no active run exists, ask for a task using `/agent-flow <task>`.

When the user types `/agent-flow status`, run:

```bash
agent-flow status
```

When the user types `/agent-flow abort`, run:

```bash
agent-flow abort
```

## Behavior

- Treat `/agent-flow` as a project-local workflow trigger, not as a shell path.
- Keep `.agent-flow/runs/<run-id>/` as internal state; expose it only for
  status, debugging, or artifact inspection.
- After a phase writes its artifact, run `agent-flow continue` from the
  project root.
- If the workflow pauses for design or slice review, summarize the relevant
  artifact and wait for user approval before continuing.
