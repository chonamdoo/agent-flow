---
name: ddd-architecture
description: |
  DDD + Clean Architecture design skill for agent-flow's `design` and
  `final-review` phases. Defines the principles, the design output format,
  and how the skill is invoked from the workflow. Bundled by agent-flow and
  copied into project skills on install.
version: 1
trigger:
  - "design"
  - "architecture"
  - "DDD"
  - "Clean Architecture"
  - "Bounded Context"
  - "use case"
phases_invoked: [design, final-review]
---

# ddd-architecture skill

This skill drives the `design` phase (interview + spec + DDD model + Clean
Architecture map + SOLID check) and the `final-review` phase's
`architecture-design` review angle. Apply with judgment — collapse principles
that don't apply to the task at hand. A 1-line config change does not need a
Bounded Context map; a new feature module does.

## Core principles

These are defaults; profiles or projects may opt out where they don't fit.

- **Clean Architecture** — dependency direction inward. Domain has no
  framework / DB / network imports. Repository **interface** in domain;
  implementation in data.
- **DDD Tactical Design** (when domain modeling applies) — Entity, Value
  Object, Aggregate, Domain Service distinguished. Aggregate root is the
  only mutation entry point. Cross-aggregate references by id.
- **Vertical Slices** — feature-based folders, not horizontal `controllers/`
  / `services/` layers as the top-level organizing principle.
- **Ubiquitous Language** — names in code match the domain expert's terms.
- **SOLID** — applied to new abstractions. Trivial code without new
  abstractions is exempt.
- **CQRS** — Command and Query use cases distinguished where it adds value
  (high-traffic reads, different data shapes). Not mandatory.
- **No side effects in domain** — entities are pure. Side effects (DB,
  HTTP, clock) injected as interfaces.
- **Google Repository Pattern** — applies to projects that have a data
  layer. Repository = single source of truth. Composes `LocalDataSource`
  + `RemoteDataSource` + `Mapper`. Returns reactive streams or suspend
  functions of **domain models** (never DTOs). Cache / retry / error
  translation lives in Repository. *Skip this section entirely if the
  project has no data layer (pure CLI tool, library, etc).*

## Output: `design.md` sections

The `design` phase produces a single `design.md` with these sections, in
order. Sections that don't apply may be marked "n/a — \<one-line why\>" and
skipped.

1. **Architecture Overview** — layer breakdown + vertical slices used.
2. **Bounded Context Map** — context boundaries; cross-context relationships
   (partnership / customer-supplier / conformist / ACL / open-host).
3. **Domain Model** — Aggregate roots, Value Objects, Domain Events,
   key invariants.
4. **Use Cases (CQRS where applicable)** — Command/Query specs, I/O types.
5. **File Structure** — folder layout (domain/usecase/data/presentation per
   slice). Data layer specifics (Repository / LocalDataSource /
   RemoteDataSource / Mapper) when applicable.
6. **Validation Check** — short summary of how the design honors each
   principle that applies; explicit "n/a" for those that don't.

## Invocation from agent-flow

- **`design` phase** — produces `design.md` (the six sections above).
- **`final-review` phase** — applied as the `architecture-design` review
  angle (`templates/_shared/review/architecture-design.md`). The review
  checks the implemented diff against the design's claims.
- For post-implementation SOLID validation, use
  `solid-architecture-review`.
