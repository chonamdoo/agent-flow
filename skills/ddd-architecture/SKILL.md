---
name: ddd-architecture
description: Domain-Driven Design skill for agent-flow design and final-review phases. Use when modeling bounded contexts, ubiquitous language, entities, value objects, aggregates, domain events, domain invariants, and domain flows; apply clean-architecture afterward when layer boundaries or dependency direction are involved.
version: 2
trigger:
  - "design"
  - "architecture"
  - "DDD"
  - "Domain-Driven Design"
  - "Bounded Context"
  - "Aggregate"
  - "Entity"
  - "Value Object"
phases_invoked: [design, final-review]
---

# ddd-architecture skill

This skill covers domain modeling only. It answers "what is the domain?"
For layer boundaries, dependency direction, UseCase port/impl boundaries,
Repository port/adapter boundaries, Cache, Mapper, Composition Root,
Testability, and SOLID architecture validation, apply
`skills/clean-architecture/SKILL.md` after this skill.

When both DDD and Clean Architecture are needed, apply order is:

1. `ddd-architecture`: define domain terms, contexts, models, invariants, and flows.
2. `clean-architecture`: protect that model with layers, ports, adapters, and dependency rules.

## Core principles

These are defaults; profiles or projects may opt out where they do not fit.

- **Bounded Context** — define where each domain term has one meaning. Cross-context
  relationships are explicit: partnership, customer-supplier, conformist, ACL,
  open-host, or separate-way.
- **Ubiquitous Language** — code names match stakeholder terms. Transport,
  persistence, and UI vocabulary must not rename domain concepts.
- **Entity** — identity matters across time. Behavior and invariants that belong
  to the entity stay on the entity.
- **Value Object** — identity does not matter. Equality is value-based and
  invalid states are rejected at construction.
- **Aggregate** — consistency boundary. Aggregate root is the mutation entry
  point. Cross-aggregate references use identity, not object references.
- **Domain Event** — past-tense fact about a meaningful state transition. Payload
  is minimal: ids plus essential context.
- **Domain Invariant** — business rule protected by aggregate/entity/value object,
  not scattered in controllers or persistence adapters.
- **Domain Flow** — describe the business sequence in domain terms before
  implementation details.

## Output: DDD sections

Design artifacts that need domain modeling should include:

1. **Bounded Context Map** — contexts touched; relationship type; translations.
2. **Ubiquitous Language** — new/refined terms and rejected ambiguous terms.
3. **Domain Model** — Entities, Value Objects, Aggregates, Domain Services.
4. **Domain Events** — emitted facts, payload, and consumers when known.
5. **Domain Invariants** — where each invariant is enforced.
6. **Domain Flow** — user/business flow using domain language.

Sections that do not apply may be marked `n/a - <one-line reason>`.

## Review focus

- Domain language drift.
- Persistence/transport/UI names leaking into domain names.
- Missing or oversized aggregate boundaries.
- Invariants enforced outside the domain model.
- Domain events named as commands instead of facts.
- Anemic models caused by moving domain behavior into procedural services.
