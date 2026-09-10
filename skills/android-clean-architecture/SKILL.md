---
name: android-clean-architecture
description: Android/Kotlin platform adapter for `clean-architecture-core` that maps the core contract to Gradle modules, Kotlin packages, Hilt, Retrofit data sources, and Android platform boundaries. Use when applying Clean Architecture to Android modules or reviewing module/data/DI boundaries; pair with android-clean-presentation-architecture for ViewModel, UiState, Compose screen, or presentation-only work.
requires:
  - clean-architecture-core
---

# Android Clean Architecture

This is not a standalone Clean Architecture guide. Load [`clean-architecture-core`](../clean-architecture-core/SKILL.md) first; this skill adds Android/Kotlin module, package, DI, and platform-boundary details only.

## Quick start

1. Apply the layer, dependency-direction, and review rules from `clean-architecture-core`.
2. Use this adapter only to translate those rules into Android Gradle modules, Kotlin packages, Hilt bindings, Retrofit/OkHttp boundaries, and Android platform adapters.
3. If the task is only ViewModel, UiState, Compose screen wiring, or one-shot presentation effects, pair with `android-clean-presentation-architecture` before applying presentation details.

## Module Boundaries

Discover the project's Gradle modules, Kotlin source sets, dependency declarations,
and active architecture role mappings. Preserve an adopted layout; roles do not
require a new module or a fixed folder tree. Kotlin `internal` is a compilation
module boundary, not package-private visibility.

Map source sets as well as module ownership. Repeated package names do not prove
the dependency direction, and a role outside lint activation has not been checked.

## Roles

- `app-shell` owns Application/activity entry, startup, root navigation, global
  error hosts, and Hilt composition.
- `core-domain` owns pure domain policy, values, errors, and repository contracts;
  application orchestration consumes those contracts and other stable ports.
- `core-data` owns repository implementations, source/cache policy, outbound
  DTO/entity mapping, and data bindings.
- If the project has a shared database role, data adapters may depend on it.
  Database code does not depend on data implementations, features, or AppShell;
  pure domain policy does not depend on the database. Otherwise keep persistence
  inside the existing data adapter boundary.
- Network/platform roles own client setup, interceptors, qualifiers, native
  capabilities, and failure mapping.
- Navigation and feature API roles expose entry/route contracts; their
  implementation/presentation roles own concrete graphs, screens, state holders,
  UI values, and mapping.
- `shared-presentation-contract` owns neutral notifier/queue interfaces consumed
  by AppShell and feature presentation. Wire implementations at composition;
  neither domain policy nor the contract imports AppShell or UI implementations.

## Hilt DI

- Use `@HiltAndroidApp` on the `Application`.
- Use `@AndroidEntryPoint` on activity/fragment hosts.
- Use `@HiltViewModel` for ViewModels.
- Put `@Provides` for third-party builders/factory logic.
- Put `@Binds` for interface-to-implementation mappings.
- Keep Retrofit/OkHttp construction at the network adapter/composition edge.
- Keep API providers and repository bindings at the data adapter/composition edge.
- An adopted application use-case `@Inject constructor` needs no redundant Hilt
  module. This is application wiring metadata, not permission for Hilt or Android
  types inside pure domain policy. Use external factories for a fully pure action.
- Assisted ViewModel factories pass only route values; other dependencies stay
  normal Hilt injections.

## Data Boundary

Keep Retrofit interfaces, outbound DTOs, persistence entities, and source/cache
policy in the data/network adapter that owns them. Convert to domain/application
values at that boundary. Compose separate sources and mappers when their policy
or change reasons differ; a DB-only repository needs no remote/cache collaborator.

Apply the core's recorded simple-adapter exception instead of adding forwarding
classes. Presentation still receives contracts, never raw API services or ORM
entities. Discover the existing binding locations rather than moving files to
match an example layout.

## Presentation Boundary

- Route/top-level wiring obtains ViewModel, collects state/events with lifecycle,
  performs navigation/platform calls, and passes plain state/callbacks down.
- Stateless rendering composables do not call `hiltViewModel()`, `viewModel()`,
  lifecycle collection, or navigation APIs; route/top-level entry wiring owns them.
- ViewModels inject use cases, one context's repository interface, and platform
  abstractions — never repository impls, data sources, API services, DTOs,
  `Context`, `Activity`, or `NavController`. A use case is required when the call
  crosses contexts, orders multi-step side effects, or adds domain/business failure semantics.
- UI state is immutable and explicit for loading/error/empty/offline/success
  states that can occur.
- Domain-to-UI mapping lives in presentation mappers.

## Review Additions

```text
android-clean-architecture: applied
gradle-module-boundary: pass|fail
kotlin-package-boundary: pass|fail
hilt-composition-root: pass|fail
repository-impl-direct-api-service: pass|fail
remote-data-source-boundary: pass|fail
feature-api-public-contract-only: pass|fail
viewmodel-dependency-boundary: usecase|single-context-repository|mixed|no-domain-dependency|fail|n/a
compose-route-screen-split: pass|fail|n/a
```

`viewmodel-dependency-boundary` records which allowed shape the ViewModel used:
`usecase` for use cases only, `single-context-repository` for a direct repository
interface from one context, `mixed` when both appear, `no-domain-dependency` when
the ViewModel injects only platform/UI abstractions, `fail` for a repository
implementation, data source, or API service, and `n/a` when no ViewModel changed.

## Evidence Basis

Hilt Android docs, Android ViewModel docs, Android UI layer docs, Compose state
docs, StateFlow and SharedFlow docs.
