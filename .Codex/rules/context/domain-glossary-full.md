# Domain Glossary Full

Agent Flow is a project-agnostic workflow kit. This file keeps expanded domain vocabulary out of `CONTEXT.md`.

## Current Terms

- **Workflow Kit**: reusable package containing workflow definitions, profiles, roles, adapters, prompts, skills, and artifact conventions. It is not a single-purpose app.
- **Workflow**: ordered phase graph for a type of work. It owns phase order, loop rules, and required markers. It does not own project-specific gate commands.
- **Project Profile**: stack-specific defaults such as gates, branch strategy, review angles, vocabulary mapping, commit convention, and PR target.
- **Phase**: a step in a Workflow. It has an id, prompt/instruction, required artifact, and optional completion markers.
- **Role**: responsibility required by a phase. It is not a concrete model, provider, CLI, or subagent.
- **Adapter**: environment strategy that maps roles and phase prompts to execution. Examples: hosted agent, CLI subprocess, manual prompt flow.
- **Provider**: concrete external execution target used by an adapter, usually a host CLI such as Codex or Claude.
- **Run**: one execution instance of a Workflow for one task. It owns runtime state and artifacts.
- **Artifact**: durable record created by a phase. It should summarize decisions, evidence, validation, and review state without raw log floods.
- **Handoff**: artifact optimized for the next phase. It preserves decisions, rejected options, risks, relevant files, and remaining work.
- **Gate**: executable validation command declared by a Project Profile. Examples: build, lint, typecheck, tests.
- **Personal Workflow**: current default execution model where one lead controls the Run, delegates stage-scoped work, and advances phases.

## Future Terms

Team Orchestration is future/optional. Use these terms only when explicitly discussing that module:

- **Worker**: long-lived participant with identity, heartbeat, mailbox, and task-claim behavior.
- **Task**: claimable unit of work for a Worker.
- **Team State**: active coordination state for workers, claims, mailboxes, and heartbeats.
- **Mailbox**: async message queue between lead and Workers or between Workers.
- **Heartbeat**: liveness/status record written by a Worker.
- **Worktree**: isolated git working directory. Worktree use is allowed today, but Worker-owned worktrees are future Team Orchestration behavior.

## Relationship Rules

- Workflow contains Phases.
- Run executes one Workflow.
- Project Profile supplies Gates and project defaults.
- Adapter chooses execution strategy.
- Provider is only used when an Adapter calls an external executable.
- Artifact records phase output; manifest records run state.
- Gate verifies code/context; completion marker verifies phase artifact completeness.
