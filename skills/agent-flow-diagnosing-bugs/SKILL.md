---
name: agent-flow-diagnosing-bugs
description: Start the dedicated Agent Flow workflow for a hard bug or performance regression.
disable-model-invocation: true
---

# Agent Flow Diagnosing Bugs

Use this user-invoked skill as the lifecycle entry for a hard bug or performance regression.
The workflow owns diagnosis; this wrapper only starts or resumes it.

## Invocation

Accept `/agent-flow-diagnosing-bugs <symptom>`. If the symptom is missing, ask for it.

1. Run `agent-flow status` from the project root.
2. If a run is active, execute its printed `next_command` exactly. Do not start another run.
3. If no run is active, run:

```bash
agent-flow run "<symptom>" --workflow diagnosing-bugs
```

Treat the runner output as the source of truth for the worktree, phase, artifact, and next command. `agent-flow run` owns worktree creation; do not call `agent-flow worktree create`.

After each phase writes its artifact, execute the printed `next_command`. When the workflow reports `status: blocked`, surface only the missing environment, redacted evidence, or permission it requests.
