# 0003 Keep Team Orchestration Optional

## Status

Accepted

## Context

Agent Flow should support both Personal Workflows and future Team Orchestration. These modes have different runtime needs.

Personal Workflows are lead-centered and stage-oriented. Team Orchestration needs worker identity, task claiming, mailbox communication, heartbeat/status tracking, shutdown semantics, and likely worktree isolation.

Mixing Team Orchestration directly into the default workflow core would make the MVP heavier and blur the meaning of Run artifacts versus active coordination state.

## Decision

Keep Personal Workflow as the default execution mode and implement Team Orchestration as an optional module.

Team runtime coordination state lives under `.agent-flow/state/team/<team-name>/`. Personal Workflow run artifacts live under `.agent-flow/runs/<workflow>/<run-id>/`.

## Consequences

- Personal Workflows stay simple and usable first.
- Team Orchestration can grow its own state model without overloading Run artifacts.
- Terms such as Worker, Task, Mailbox, and Heartbeat remain scoped to Team Orchestration.
- Some future workflows may need explicit bridging between a Run and a Team if a lead starts a team inside a workflow stage.

## Alternatives Considered

### Make Team Orchestration part of the core Run model

This would make team support feel first-class immediately, but it would force Personal Workflows to carry worker/task concepts they do not need.

### Ignore Team Orchestration until later

This would simplify the first version, but it would risk choosing artifact and state layouts that conflict with team runtime needs later.

