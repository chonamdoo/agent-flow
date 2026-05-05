# 0001 Separate Core From Environment Adapters

## Status

Accepted

## Context

Agent Flow should work across different agent environments, including Codex, Claude, Gemini, and manual prompt execution. Each environment has different mechanics for spawning agents, passing prompts, resuming work, and parsing output.

If those mechanics live in the workflow core, the Workflow Kit becomes tied to one environment and harder to reuse across projects.

## Decision

Keep the core environment-independent.

The core owns Workflows, Runs, Artifacts, Handoffs, Project Profiles, and Gates. Adapters translate Roles and Stages into actions for a specific environment. Providers wrap concrete external execution targets such as AI CLIs or subprocesses.

## Consequences

- The core stays reusable across Codex, Claude, Gemini, and manual execution.
- Environment-specific authentication, process control, and output parsing stay outside the core.
- Adding a new execution environment should usually require a new Adapter or Provider, not a Workflow rewrite.
- Early versions may feel less automated because some adapters only generate prompts and artifacts instead of launching external processes directly.

## Alternatives Considered

### Codex-only runner

This would be simpler for the current environment, but it would make Claude, Gemini, and manual execution second-class later.

### Providers embedded in core

This would make direct CLI execution easier at first, but it would mix workflow semantics with subprocess behavior, authentication assumptions, and provider-specific output parsing.

