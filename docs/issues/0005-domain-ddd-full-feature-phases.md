# Domain and DDD full-feature phases

## Problem

The installed `full-feature` workflow starts at PRD and slice planning. It can miss domain interrogation, domain vocabulary mapping, product validation, senior plan review, DDD/Clean architecture design, and post-implementation architecture review.

## Goal

Add lightweight, CLI-enforced full-feature phases for:

- `domain-grill`
- `product-brief`
- `plan-review`
- `ddd-design`
- `architecture-review`

`domain-grill` owns the compact domain map output; there is no separate
mapping phase.

## Scope

- Extend installed `full-feature` phase order.
- Generate phase prompts for all new phases.
- Generate project-local skills for domain grilling, product brief, plan review, DDD/Clean architecture, and architecture review.
- Route `plan-review` request changes back to `slice-plan`.
- Route `architecture-review` request changes back to `refactor`.

## Non-goals

- No resume option.
- No automatic product decision stop.
- No DDD quality scoring in the CLI.
- No new workflow outside `full-feature`.

## Acceptance

- Install creates new skill files and prompts.
- `full-feature.yaml` includes the new phase order.
- `run advance` requires artifacts for the new phases.
- `plan-review` and `architecture-review` require `verdict: approve` or `verdict: request-changes`.
- Request changes route to the agreed previous phase.
