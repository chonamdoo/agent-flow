---
name: clean-architecture-core
description: Defines the platform-neutral Clean Architecture contract for semantic layers, dependency direction, use cases, repository/source/cache/mapper boundaries, DI/composition-root placement, and cross-platform review. Use when designing, implementing, or reviewing layer boundaries or adapter seams; load it before platform clean architecture skills when framework details matter.
---

# Clean Architecture Core

Use this skill as the canonical source for platform-neutral architecture rules.
It can be used alone for shared layer semantics, dependency direction, and
adapter boundaries. Pair it with:

- a platform clean architecture skill when path, framework, DI, or sample-code
  details matter;
- `ddd-architecture` first when domain language, bounded contexts, aggregates,
  or invariants are still being discovered;
- `solid-architecture-review` during reviews that need SOLID-specific findings;
- `codebase-design` when the main question is module depth, interface shape, or
  seam placement rather than layer compliance.

## Quick start

1. Identify the current layer or seam: app shell, feature API, presentation,
   domain, data, network, UI/resource/platform, or adapter.
2. Check dependency direction before local details: policy-facing contracts point
   inward; framework, transport, persistence, and SDK details stay at adapters.
3. Apply the boundary sections that match the touched code, then record the
   relevant checklist markers in review output.


## Semantic Layers

- App Shell owns process startup, composition root, root routing/navigation,
  global error host, and feature entry composition.
- Feature API exposes public route keys, destination contracts, entry contracts,
  and feature capabilities. It has no screen internals and no data/domain
  implementation.
- Feature Presentation owns route/screen wiring, state holder, UI state/action,
  UI events, UI models, domain-to-UI mapping, and rendering components.
- Core Domain owns business language, domain models, repository interfaces,
  use cases, policies, domain services, and domain errors.
- Core Data implements domain repository interfaces and owns remote/local
  sources, API clients, DTO/request/response models, entity models, cache,
  data-to-domain mappers, and data DI bindings.
- Core Network owns HTTP/client setup, API response envelope, network
  interceptors, network qualifiers, and common network failure mapping.
- Core UI/design/resources/platform modules expose UI primitives, resources, and
  platform abstractions without hiding data or network policy inside rendering.

## Dependency Rule

Dependencies point toward stable policy:

```text
Presentation -> UseCase -> Repository interface <- Repository impl
Repository impl -> RemoteDataSource / LocalDataSource / Cache / Mapper
RemoteDataSource -> ApiService or transport client
```

- Domain must not import UI, DB, HTTP, SDK, serialization framework, DI
  framework, or transport implementation details.
- Presentation must not import data implementations, API services, DTOs, DB
  entities, or transport models.
- Data may depend on domain contracts and models, but domain must not depend on
  data.
- DI framework hooks belong at the app shell or adapter edge, not inside domain
  policy.

## Use Case Boundary

- A use case represents one user intent or application action.
- Public or multi-feature use cases should have a stable interface when another
  module or platform adapter depends on the contract.
- Use cases depend on repository interfaces only.
- A use case must not directly call another use case. Share common logic through
  a domain service, policy, pure function, or explicitly named application
  workflow/orchestrator.
- Use cases pass infrastructure failures through unless adding domain-specific
  business failure semantics.

## Repository And Source Boundary

- Repository interfaces live in domain/application contracts.
- Repository implementations live in data/infrastructure adapters.
- Repository implementations return domain models only.
- Production repository implementations should compose remote, local, cache, and
  mapper collaborators. They should not inject an API service or HTTP client
  directly as the default shape.
- Put API services and raw transport clients behind a remote data source.
- Direct API-service injection is allowed only for temporary/simple adapters, and
  the exception must be recorded.
- A `core-data-<context>` or `core/data/<context>` module normally requires
  matching domain ownership. Pure transport/shared adapters without domain
  ownership must record the exception.

## Mapping Boundary

- DTO/request/response, DB/entity, domain model, UI model, and API response
  model are separate shapes.
- Put mappers at the boundary they cross:
  - data DTO/entity -> domain
  - domain -> UI model
  - domain -> API response model
- Mappers only convert data. They must not call APIs, databases, caches, or make
  business policy decisions.
- Avoid one large mapper that crosses remote DTO, DB/entity, domain, and UI
  boundaries at once.

## Cache Boundary

- Cache is a data-layer detail unless the domain explicitly models caching as a
  business concept.
- Split memory and disk cache when lifetime, invalidation, or restart behavior
  differs.
- Cache interfaces and implementations are separate.
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

## Must Avoid

- Domain importing UI, DB, HTTP, SDK, serialization framework, or DI framework.
- Presentation importing data impl, API service, DTO, entity, or transport
  model.
- Repository interface returning DTO, entity, transport model, response model, or
  UI model.
- Repository implementation as a thin API wrapper with no source/mapper boundary
  in production architecture.
- Repository implementation directly injecting an API service without a recorded
  temporary/simple-adapter exception.
- Core data context without matching domain ownership or documented adapter
  exception.
- Core UI importing network policy details.
- Use case calling another use case directly.
- Mapper doing API, DB, cache, or business-policy work.
- Consumer depending on a large interface with methods it does not use.

## Review Checklist

Record these items in architecture or code review output:

```text
clean-architecture-core: applied
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
technical-adapter-exception-recorded: pass|fail|n/a
di-composition-root-explicit: pass|fail
platform-di-shape-consistent: pass|fail|n/a
solid-boundary-check: pass|fail
```

## Evidence Basis

Hilt Android docs, Swinject README, React createContext/useContext docs, React
Native React Fundamentals, TSyringe README.
