# Enforce DDD architecture intent

## Problem

`agent-flow` can complete a task labeled as DDD refactoring while only producing a shallow service-layer split. In a recent run for "organize data and migrate a Flask API to FastAPI", followed by "refactor after FastAPI migration", the FastAPI migration and tests passed, but the DDD phase did not enforce a real DDD structure.

The generated `ddd-design.md` described only:

- API layer: `api/main.py`
- Service layer: `api/services/*`
- Data source: `kr_market/data/*.json`

The implementation ended at:

- `api/main.py`
- `api/services/data_source.py`
- `api/services/analytics.py`
- `api/services/stocks.py`
- `api/services/market_data.py`

This is better described as a service-layer refactor, not DDD.

## Expected behavior

When DDD is selected or implied, design artifacts and implementation should share explicit architectural boundaries for the active stack. The FastAPI example might use:

- `api/domain`
- `api/application`
- `api/infrastructure`
- `api/presentation`

An iOS project might instead use feature/domain modules or Swift packages; another stack can use its native package/module conventions. The requirement is not the literal `api/*` names, but an explicit domain, application/use-case, infrastructure, and presentation boundary map with concrete project paths.

If the workflow is not enforcing DDD, the workflow should label the work as `service-layer refactor` instead of `DDD refactor`.

## Observed gaps

- `ddd-design` does not require a DDD package structure.
- `slice-plan` is not split by `domain`, `application`, `infrastructure`, and `presentation`.
- Verification checks tests only, not whether implementation matches the design artifact.
- `pr-watch` can remain pending for local projects without a PR.
- Commit, push, and PR phases do not gracefully skip when the root is not a git repository.
- `agent-flow` and `agent-workflow` names are mixed in user-facing language.

## Proposed changes

- Add an explicit DDD mode, for example `agent-flow run "..." --architecture ddd` and `agent-flow start <workflow> --task "..." --architecture ddd`.
- When DDD mode is active, require language-agnostic structure checks for domain, application/use-case, infrastructure, and presentation boundaries using the active stack's concrete paths/modules.
- Improve the `ddd-design.md` template to require:
  - Bounded Context
  - Aggregates, Entities, and Value Objects
  - Application Use Cases
  - Infrastructure Adapters
  - Presentation Routes
  - Dependency Rule
- Add an `architecture-review` check that compares actual files against the design artifact.
- In non-git roots, officially mark:
  - commit: `skipped`
  - push-pr: `skipped`
  - pr-watch: `skipped`
- If `pending` is valid, let `advance` continue or convert the phase to an explicit "user approval required" state.
- Standardize user-facing naming between `agent-flow` and `agent-workflow`.

## Acceptance criteria

- DDD mode cannot complete with only `api/main.py` plus `api/services/*` unless the artifact explicitly labels the work as non-DDD.
- DDD design, slice plan, implementation, and architecture review all use the same layer vocabulary.
- Architecture review fails when required DDD design sections, concrete structure mapping, or dependency rules are missing.
- Local non-git projects skip git-dependent phases without blocking the run.
- User-facing command and status output consistently use one product name.
