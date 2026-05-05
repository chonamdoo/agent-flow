# 0002 Use Stage Artifacts For Subagent-First Workflows

## Status

Accepted

## Context

Agent Flow is intended for personal workflows where the lead session orchestrates work but does not carry every detail in one growing context. Large tasks can become noisy when exploration, implementation, review, QA, and fixes all happen in the same conversation.

The workflow should preserve useful context between stages without forcing the lead session to retain every detail.

## Decision

Use stage-scoped prompts, artifacts, and handoffs as the default workflow shape.

The lead starts a Run, generates prompts for each Stage, delegates stage-scoped work to Subagents where possible, and records results as Artifacts. Stage transitions use Handoffs so later stages can read stable context without relying only on the lead conversation.

## Consequences

- The lead session stays focused on orchestration and decisions.
- Subagents receive smaller, clearer prompts.
- Stage results remain available after context compaction or session changes.
- Workflows require disciplined artifact writing; missing artifacts reduce the value of later stages.
- The MVP can support manual execution by writing prompts even before every Adapter can launch Subagents directly.

## Alternatives Considered

### Single lead-session workflow

This is simpler initially, but it mixes exploration, implementation, review, and QA into one context and does not scale well for repeated project work.

### Fully automated team orchestration from the start

This would support parallel work earlier, but it adds runtime complexity before the personal single-run workflow is stable.

