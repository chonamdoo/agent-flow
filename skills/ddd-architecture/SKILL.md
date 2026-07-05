---
name: ddd-architecture
description: Models Domain-Driven Design concepts for bounded contexts, ubiquitous language, entities, value objects, aggregates, domain events, invariants, and domain flows. Use when design or final review depends on the domain model or business language; apply clean-architecture-core afterward when layer boundaries or dependency direction are involved.
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

# DDD Architecture

This is a standalone domain-modeling skill when the question is "what is the
domain?" Pair it with `clean-architecture-core` after the domain model is clear
when layer boundaries, dependency direction, use case ports, repository adapters,
cache, mapper, composition-root, testability, or SOLID architecture validation
are in scope.

## Quick start

1. Name the bounded contexts and the terms whose meanings change between them.
2. Capture the ubiquitous language, entities, value objects, aggregates, domain
   events, invariants, and domain flows before selecting layers or adapters.
3. After modeling the domain, load `clean-architecture-core` to protect it with
   dependency rules and adapter boundaries; load `codebase-design` if the open
   question is where a module seam should live.

When both DDD and Clean Architecture are needed, apply order is:

1. `ddd-architecture`: define domain terms, contexts, models, invariants, and flows.
2. `clean-architecture-core`: protect that model with layers, ports, adapters, and dependency rules.

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
