---
name: clean-architecture
description: Language- and framework-neutral Clean Architecture boundary skill for agent-flow. Use during design, implementation, final-review, code-review, architecture review, or whenever layer boundaries, use case ports, repositories, caches, mappers, dependency direction, testability, or SOLID architecture validation are involved.
version: 1
trigger:
  - "design"
  - "implement"
  - "final-review"
  - "code-review"
  - "architecture"
  - "Clean Architecture"
  - "UseCase"
  - "Repository"
  - "Cache"
  - "Mapper"
phases_invoked: [design, implement, final-review, code-review]
---

# clean-architecture skill

Apply after DDD when both are relevant. DDD answers "what is the domain?";
Clean Architecture answers "which layers, boundaries, ports, adapters, and
dependency directions protect that domain?"

## Dependency Rule

Dependencies point inward only.

Default flow:

```text
ViewModel/Controller/Handler
  -> UseCase Interface
  <- UseCase Impl
      -> Repository Interface
      <- Repository Impl
          -> RemoteDataSource
          -> LocalDataSource
          -> MemoryCache
          -> DiskCache
          -> Mapper
```

- Presentation/UI/Controller/ViewModel/Handler depends on Application UseCase ports.
- UseCase Impl depends on Repository Interface.
- Repository Impl composes DataSource, Cache, and Mapper details.
- Domain/Application must not import UI, DB, HTTP, SDK, or framework implementations.

## Boundary Rules

- UseCase represents one user intent or application action.
- Prefer or require UseCase Interface + Impl for large apps, multi-feature modules,
  public contracts, cross-feature calls, or DI bindings.
- Concrete UseCase is allowed for small single-purpose features only when design
  records why.
- UseCase must not directly call another UseCase. Shared logic belongs in a Domain
  Service, Policy, pure function, or explicitly named Application Workflow/Orchestrator.
- Repository Interface is a domain/application port. Repository Impl is a
  data/infrastructure adapter.
- Repository is the single source of truth and returns domain models only.
- UseCase must not know Repository Impl, DataSource, Cache, DTO, DB Entity, or UI Model.
- Cache is a data layer detail. Split MemoryCache and DiskCache when lifetime or
  change reason differs.
- Cache interface and implementation are separate. Never expose internal mutable
  storage directly.
- Remote DTO, DB Entity, Domain Model, and UI Model are separate models.
- Put mappers at the boundary they cross. Mapper only converts data.
- Do not use one large mapper for all Remote DTO, DB Entity, UI Model conversions.

## SOLID Architecture Checks

- SRP: check one reason to change, not one vague responsibility.
- OCP: add extension points only at real variation points.
- LSP: every interface implementation, fake, and test double preserves the same contract.
- ISP: consumers do not depend on methods they do not use.
- DIP: high-level policy depends on stable abstractions, not concrete implementations.

## Required Design Markers

`design.md` or `ddd-design.md` completion gate must include:

```text
## Clean Architecture Boundary Map
## Dependency Rule
## Use Case Boundaries
usecase-interface: required|optional|n/a
usecase-composition: none|domain-service|application-service|orchestrator|justified
## Repository Boundaries
## Cache Boundary
cache-required: yes|no
memory-cache: required|optional|n/a
disk-cache: required|optional|n/a
cache-invalidation-policy:
## Mapping Boundary
remote-dto-domain-mapper: required|optional|n/a
entity-domain-mapper: required|optional|n/a
domain-ui-mapper: required|optional|n/a
## Composition Root
## Testability Boundary
solid-srp-change-reason:
solid-ocp-extension-points:
solid-lsp-contracts:
solid-isp-consumer-ports:
solid-dip-dependency-direction:
```

## Required Review Markers

`final-review.md` or `architecture-review.md` completion gate must include:

```text
clean-architecture: applied
dependency-rule: pass|fail
usecase-boundary: pass|fail|n/a
usecase-calls-usecase: pass|fail
repository-boundary: pass|fail
cache-boundary: pass|fail|n/a
memory-disk-cache-separated: pass|fail|n/a
mapping-boundary: pass|fail|n/a
dto-entity-domain-ui-separated: pass|fail
solid-boundary-check: pass|fail
```

`code-review.md` or `multi-review.md` completion gate must include:

```text
clean-architecture-review: applied
usecase-interface-check: applied
usecase-composition-check: applied
cache-boundary-check: applied
mapping-boundary-check: applied
solid-clean-architecture-check: applied
```

## Must-fix Conditions

Fail review when any condition exists:

- Domain/Application imports UI, DB, HTTP, SDK, or framework implementations.
- ViewModel/Controller/Handler calls Repository Impl, DataSource, or Cache implementation.
- UseCase directly depends on Repository Impl.
- UseCase injects or calls another UseCase directly.
- UseCase directly handles DTO, DB Entity, or UI Model.
- Repository Interface returns DTO, DB Entity, or UI Model.
- Repository Impl is only an API wrapper and not a single source of truth.
- MemoryCache and DiskCache with different change reasons are mixed in one class.
- Cache exposes internal mutable storage.
- Restart-required data is stored only in MemoryCache.
- Temporary data is stored in DiskCache without need.
- DTO/DB Entity is exposed as domain/application/presentation state.
- Domain Model depends on ORM, serialization, or framework annotation.
- Mapper performs API, DB, cache access, or business policy decisions.
- One large Mapper handles Remote DTO, DB Entity, and UI Model conversions.
- Consumer depends on a large interface with unused methods.
- Implementation breaks interface contract and violates LSP.
- High-level policy depends on concrete implementation.
