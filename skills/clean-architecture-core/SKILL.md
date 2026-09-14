---
name: clean-architecture-core
description: Platform-neutral Clean Architecture contract for semantic layers, dependency direction, use cases, repository/source/cache/mapper boundaries, DI/composition-root placement, and cross-platform architecture review. Use before platform clean architecture skills and during design, implementation, architecture review, or code review.
requires: [code-generation-discipline]
---

# Clean Architecture Core

Use this as the canonical semantic contract. Apply it to the boundaries the
project adopts; platform adapters add framework details, not competing rules.
Discover actual source roots, modules, dependency wiring, and architecture role
configuration before judging coverage. Roles do not prescribe folders or a
minimum number of modules, files, interfaces, or models.

## Required Application

Load this core before exactly the platform adapter required by the changed
source: `android-clean-architecture`, `ios-clean-architecture`,
`flutter-clean-architecture`, `react-clean-architecture`,
`react-native-clean-architecture`, or `python-api-clean-architecture`.
For UI state, ViewModels/controllers, screens/components, or route wiring, also
load the matching presentation skill selected by `code-generation-discipline`.
Shared semantic rules belong here; platform rules belong in their adapter.

The active workflow owns legacy completion markers and their permitted enums.
When it requires both legacy markers and this core's checklist, retain both;
the removed `clean-architecture` skill name is not a marker or review-angle rename.

## Semantic Layers

- `app-shell` owns startup, composition, root routing, and concrete dependency
  wiring; in UI apps it also owns global UI hosts.
- `inbound-adapter` owns HTTP handlers, worker/tool entry adapters, input/output
  schemas, trusted caller-context extraction, and transport error/response mapping.
- `application` owns use cases, application workflows, command/result contracts,
  and consumer-focused ports for orchestration, time, payments, transactions, or
  platform capabilities.
- `core-domain` owns business language, invariants, domain models, repository
  contracts, policies, domain services, and domain errors.
- `core-data` implements domain/application ports and owns persistence, outbound
  providers, their DTOs/entities, mapping, and source/cache policy where needed.
- `feature-api` exposes UI feature entry, route, and capability contracts without
  screen internals or data implementations.
- `feature-presentation` owns UI wiring, state holders, UI state/actions/events,
  presentation models, mapping, and rendering.
- `shared-presentation-contract` owns narrow framework-neutral cross-presentation
  ports, such as a common-error notifier/queue. AppShell and feature presentation
  may consume it. It may use domain error values but imports neither AppShell or
  feature implementations nor UI, transport, or storage implementation types.
  Domain policy does not depend on this UI-facing contract. Servers need no UI queue.
- Network/platform adapters own outbound client setup, transport envelopes,
  interceptors, failure mapping, and platform implementations. UI/design/resources
  roles expose rendering primitives without hiding network or persistence policy.

## Dependency Rule

Dependencies point toward stable policy; composition roots construct concrete
adapters and pass contracts inward:

```text
Inbound adapter -> Application -> Domain policy
Data / platform adapter -> Application or domain port
Feature presentation -> Application or domain contract
AppShell / feature presentation -> Shared presentation contract
```

- A UI state holder may use one context's repository interface directly when no
  orchestration is needed. Require a use case for cross-context work, ordered
  multi-step side effects, or additional business failure semantics. Presentation
  never depends on a repository implementation, data source, or API service.
- HTTP/tool handlers call application actions by default. The UI direct-repository
  exception is not permission to expose Spring Data repositories, ORM entities,
  or raw clients through a handler; any different handler policy must be explicit.
- Pure domain policy imports no UI, DB, HTTP, provider SDK, serialization, or DI
  framework. Pure application orchestration also keeps those implementations at
  adapter boundaries.
- Application wiring metadata, such as an adopted Android `@Inject constructor`
  pattern, is distinct from domain policy or a runtime container lookup. Keep the
  accepted platform choice explicit. Framework-aware application services require
  an intentional architecture decision; neither choice admits framework types
  into pure domain policy.
- Presentation receives typed application/domain ports and presentation values,
  not data implementations or transport DTOs. A composition root may construct
  raw clients without exposing them to presentation consumers.
- Source roots and lint activation describe where this contract is checked.
  An unmapped or inactive boundary is not evidence that its dependencies passed.

## Use Case Boundary

- A use case represents one user intent or application action.
- Use a stable interface and implementation when module size, a public contract,
  calls from another feature/module/platform adapter, or DI binding requires it.
- Use cases depend on stable domain/application ports and pure policies, including
  repository, Clock, payment, transaction, and platform-capability contracts.
  Name a port for its responsibility rather than disguising it as a repository.
- Use cases handle application/domain values, not inbound/outbound DTOs,
  persistence entities, raw transport failures, or UI models.
- A use case must not directly call another use case. Share common logic through
  a domain service, policy, pure function, or explicitly named application
  workflow/orchestrator.
- Apply the Error Boundary below; preserve cancellation rather than recasting it
  as a business error.

## Repository And Source Boundary

- Repository interfaces live in domain/application contracts.
- Repository implementations live in data/infrastructure adapters.
- A repository is the single source of truth for the data policy its consumer
  needs; it coordinates only the sources and caches that policy actually uses.
- Implementations return the domain/application values promised by the port,
  never outbound DTOs, ORM entities, raw responses, or UI models.
- Separate remote/local sources, caches, and mappers when they own distinct
  transport, persistence, invalidation, or conversion policy. Compose only the
  collaborators the repository actually needs.
- A remote source normally isolates raw transport clients. A simple adapter may
  own its transport and mapping in one place when the boundary remains explicit;
  record that choice. A DB-only repository needs no remote source or cache.
- Keep conversion at its semantic boundary without mandatory forwarding classes
  or identity copies when representations and contracts already coincide.
- A data context normally has matching domain/application ownership. Record
  pure transport/shared adapter exceptions rather than inventing a domain context.

## Mapping Boundary

- Inbound HTTP/tool schemas belong to the driving adapter; outbound provider DTOs
  and persistence entities belong to driven adapters. Commands/results belong to
  application contracts. A type named `Dto` is not automatically a data-layer type.
- Keep wire, persistence, domain/application, and UI responsibilities distinct.
  Separate representations when semantics, validation, mutability, or dependencies
  differ; identical safe value shapes do not require duplicate models or copies.
- Put conversion at the boundary it crosses: inbound schema to command,
  outbound DTO/entity to domain/application value, and application/domain result
  to UI or API response.
- Mappers convert values only. They do not perform I/O, cache access, or business
  policy decisions. Keep unrelated boundaries out of one giant mapper.

## Cache Boundary

- Cache is a data-layer detail unless the domain explicitly models caching as a
  business concept.
- Split memory and disk cache when lifetime, invalidation, or restart behavior
  differs.
- Keep a cache implementation behind the consumer's contract; introduce a
  separate cache interface when lifetime, substitution, or ownership needs it.
- Never expose internal mutable cache storage directly.
- Restart-required data must not live only in memory cache.
- Temporary data should not be written to disk without a product or reliability
  reason.

## Core UI And Network Boundary

- Core UI renders and adapts user interaction. It should not own HTTP clients,
  Retrofit/fetch clients, auth headers, retry policy, or endpoint policy.
- Image loading or media networking should be behind a platform/network
  abstraction when it needs auth, headers, cache policy, retry, or shared client
  configuration.

## Error Boundary

- Domain-facing errors and exposed severity/status/server-code value types belong
  to domain/application contracts, not transport implementation modules.
- If the project defines no typed result wrapper, preserve its existing
  `Result`/exception contract instead of introducing one as a review requirement.
- Remote sources may throw or return transport failures.

- Raw transport, storage, provider, and native failures stay in
  data/infrastructure adapters.
- Repository implementations or data mappers translate raw failures into the
  established domain/application result and error contract.
- Use cases preserve that result/error contract and add only business-rule
  failure semantics. They do not leak raw infrastructure exceptions.
- Presentation maps domain/application errors to UI error models before rendering.
- UI components, views, and screens never receive DTOs, HTTP `Response`,
  URLSession responses, native exception strings, or storage failure types.

## SOLID Boundary

Apply the complete `code-generation-discipline` **SOLID Boundaries** contract,
including its design evidence, applicability exception, and blocking threshold.

## Must Avoid

- Pure domain policy importing UI, DB, HTTP, provider SDK, serialization, or DI
  frameworks; application orchestration bypassing its declared adapter boundary.
- Presentation importing repository implementations, API services, outbound DTOs,
  ORM entities, or raw transport models.
- Repository ports returning outbound DTOs, ORM entities, raw response objects,
  or UI models.
- Repository transport or mapping policy leaking into consumers. A recorded
  simple adapter is allowed; absence of a separate source/cache/mapper class is
  not itself a violation.
- Direct API-service injection without the recorded simple-adapter choice.
- Data contexts without matching domain/application ownership or a documented
  technical-adapter exception.
- Core UI importing network policy details.
- Use case calling another use case directly.
- Mapper doing API, DB, cache, or business-policy work.
- Consumer depending on a large interface with methods it does not use.
- Shared presentation contracts importing AppShell/feature implementations or
  UI/data/transport implementation types.

## Review Checklist

Record these items in architecture or code review output:

Check every applicable rule in **Must Avoid**, not only the checklist categories.
An applicable must-avoid violation or failed required criterion produces
`verdict: request-changes`. Apply recorded exceptions and adopted project
conventions before judging a failure. Naming, file counts, missing unnecessary
collaborators, and inactive/unmapped lint boundaries alone prove neither a
semantic violation nor a successful check.

Use `n/a` for `usecase-boundary` and `usecase-calls-usecase` only when no use-case
implementation or composition is in scope; an existing applicable path must be
reviewed as `pass` or `fail`. Missing evidence is not a pass. Keep the active
workflow's other marker enums and conditional required checks unchanged.

```text
clean-architecture-core: applied
must-avoid-check: pass|fail
dependency-rule: pass|fail
usecase-boundary: pass|fail|n/a
usecase-calls-usecase: pass|fail|n/a
repository-boundary: pass|fail
repository-impl-direct-api-service: pass|fail
remote-data-source-boundary: pass|fail
core-data-domain-ownership: pass|fail|justified
cache-boundary: pass|fail|n/a
memory-disk-cache-separated: pass|fail|n/a
mapping-boundary: pass|fail|n/a
dto-entity-domain-ui-separated: pass|fail
core-ui-network-detail-import: pass|fail
shared-presentation-contract-placement: pass|fail|n/a
technical-adapter-exception-recorded: pass|fail|n/a
di-composition-root-explicit: pass|fail
platform-di-shape-consistent: pass|fail|n/a
solid-boundary-check: pass|fail
```

## Evidence Basis

Hilt Android docs, Swinject README, React createContext/useContext docs, React
Native React Fundamentals, TSyringe README.
