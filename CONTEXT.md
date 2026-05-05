# Context

## Glossary

### Workflow Kit

A reusable personal agent workflow package for running work across projects.

It includes workflow definitions, project profiles, role definitions, environment adapters, and artifact conventions. It is not an app or a single-purpose CLI runner.

### Workflow

A stage graph that defines how a type of work progresses.

It includes stage order, stage roles, parallel execution markers, and loop rules. It does not include project-specific commands, AI provider details, or runtime state.

### Project Profile

A stack-specific execution profile for the target project.

It includes gate commands, profile detection hints, and stack-specific defaults. It does not define workflow stage order, agent roles, or environment adapter behavior.

### Role

A responsibility required by a workflow stage.

A role is not a concrete subagent, model, or CLI provider. Adapters map roles to the execution mechanism available in the current environment.

### Adapter

An environment strategy for executing workflow stages.

An adapter decides how roles become actionable in the current environment, such as a Codex session, Claude session, CLI runtime, or manual prompt workflow.

### Provider

A concrete external execution target.

A provider usually wraps an AI CLI or subprocess such as Codex, Claude, or Gemini. Providers are called by adapters when the selected adapter uses external processes.

### Artifact

A reusable record file produced during a workflow run.

Artifacts keep the lead context small and give later stages or subagents stable material to read. Artifacts include stage prompts, stage results, review findings, gate results, recovery guidance, and handoffs. They do not include ordinary source changes or full raw logs.

### Handoff

A stage-transition artifact that summarizes what the next stage needs to know.

A handoff captures decisions, rejected options, risks, relevant files, and remaining work. Its purpose is to prevent stage context loss when the lead session grows, compacts, or delegates work to subagents.

### Gate

An automated verification command for a target project.

Gates include commands such as build, typecheck, lint, and test. Gates are defined by Project Profiles. Stage entry and exit rules are separate workflow criteria, not Gates.

### Run

A single execution instance of a Workflow for a specific task.

A Run includes a run id, workflow id, task, adapter, project profile, status, and run artifacts. A Run is not a git branch, a whole project session, or a bundle of unrelated tasks.

### Stage

A step definition inside a Workflow.

A Stage defines the role, ordering, parallel marker, and loop settings for a portion of work. Runtime execution state for a stage is a separate concern and should not be folded into the Stage definition.

### Subagent

An isolated agent execution delegated by the lead for stage-scoped work.

A Subagent is an execution pattern, not a Role or Provider. The concrete implementation depends on the Adapter, such as a Codex spawned subagent, Claude task agent, manual prompt execution, or an external CLI process.

### Personal Workflow

A lead-centered Workflow execution mode.

The lead owns the Run, advances stages, receives Subagent artifacts, and decides transitions. Some stages may use parallel replicas, but there are no long-lived workers claiming independent tasks.

### Team Orchestration

An optional execution module where multiple Workers coordinate on Tasks concurrently.

Team Orchestration uses explicit team state, worker identity, task claiming, mailbox communication, heartbeat/status tracking, and shutdown semantics. It is part of the long-term Workflow Kit model, but it is not the default Personal Workflow mode.

### Task

A claimable unit of work for Team Orchestration.

A Task is scoped so a Worker can own it independently. It may have an owner, claim, status, dependencies, and result. A broad user request is not a Task until it has been decomposed into claimable work.

### Worker

A long-lived Team Orchestration participant that can claim and complete Tasks.

A Worker is not the same as a Subagent. Workers have identity, status, heartbeat, mailbox communication, and shutdown protocol. Subagents are stage-scoped executions; Workers participate in team runtime coordination.

### Team State

Runtime coordination state for Team Orchestration.

Team State lives under `.agent-flow/state/team/<team-name>/`. It is separate from Run artifacts under `.agent-flow/runs/<workflow>/<run-id>/` because task claiming, mailbox messages, heartbeat files, and worker status are active coordination data rather than historical stage artifacts.

### Mailbox

An asynchronous message queue for Team Orchestration.

A Mailbox carries instructions, unblock requests, progress updates, and shutdown requests between the lead and Workers or between Workers.

### Heartbeat

A liveness record written by a Worker during Team Orchestration.

A Heartbeat may include worker identity, process information, last-seen timestamp, current task or status, and whether the Worker is alive. Heartbeats help the lead detect stale workers, decide cleanup, and summarize team status.

### Worktree

An isolated working directory backed by git worktree.

A Worktree lets a Worker or stage produce file changes independently from the lead workspace. Worktrees reduce file conflicts, preserve interrupted work, and support review or merge before changes enter the main workspace.

### Host Adapter Execution

Execution model where AI CLIs run on the host environment through Adapters or Providers.

Agent Flow does not treat sandboxed AI CLI execution as a supported capability. Codex, Claude, Gemini, and similar CLIs are expected to run through host adapters or providers because they often depend on host authentication, configuration, and process behavior.

### Recovery

A record of how to resume or repair a failed or interrupted Run or Team.

Recovery includes the failed stage or coordination point, the cause summary, related artifact paths, rerun commands, and any required manual action. Its purpose is to make failures restartable instead of forcing the lead to reconstruct state from memory.
