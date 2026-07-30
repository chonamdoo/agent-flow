# Android Architecture Rules

Canonical layer boundary and dependency-direction rules live in
`skills/clean-architecture/SKILL.md`. These are Android-specific naming and
ownership notes. Project-local rules win when they are explicit.

## Layer Ownership

- `presentation`: Compose UI, ViewModels, UI state, navigation adapters,
  UI models, string/resource selection.
- `domain`: entities, value objects, repository interfaces, use cases, app-level
  errors/results, policies, pure validation.
- `usecase`: orchestration of domain operations when the project separates it
  from domain.
- `data`: DTOs, API/database clients, data sources, repository
  implementations, mappers, cache policy, error translation.
- `network`: Retrofit/OkHttp setup, serialization, raw network failures, and
  transport diagnostics.

## Dependency Direction

- Domain/Application must not import Android framework, Retrofit, Room, Compose,
  Hilt, or presentation classes.
- Data may depend on domain contracts and external clients.
- Presentation may depend on domain/usecase and design-system APIs.
- Feature-to-feature direct dependencies are suspect; prefer navigation APIs or
  shared contracts.

## Data Source Package Rules

- In `core:data:*` modules, keep data source implementations under `source`.
- Use `api` for Retrofit service interfaces only.
- Use `source.remote` for remote data source implementations, refresh/auth
  network plumbing, interceptors, and authenticators.
- Use `source.local` for DataStore, database, cache, KeyStore, and local token
  storage.
- Keep DTOs, request models, and response models in `model`, not `api` or
  `source.remote`.
- Keep data-domain conversion in `mapper`.

## Error Type Ownership

These rules apply to whatever typed error and result abstraction the project
actually declares. The names below are illustrative, not required types.

- Keep domain-facing errors and the project's result type in a shared domain
  error module, for example under `core/domain/error/`, together with the
  severity and server/status code types that error exposes as fields.
- Keep raw transport failures in the network module, for example
  `core/network/error/NetworkFailure.kt`.
- Status and server code types may stay in network only while they remain raw
  diagnostics. Once the domain error exposes them as field types, own them in
  domain to avoid a domain-to-network dependency.
- If the project defines no typed result or error abstraction, follow its
  existing `Result`/exception contract. Do not introduce one as a review
  requirement.

## Error Flow

- Remote data sources call Retrofit and may throw or return a transport failure.
- Repository implementations in `core:data:*` catch the transport failure, map it
  to the domain error, and return the project's result type.
- Use cases return that same result type and add domain errors only for
  domain/business rule failures.
- ViewModels map the result type to `UiState` and `UiEvent`.
- Presentation mappers map the domain error to a feature `ErrorUiModel` or a
  shared presentation error model.
- Routes collect state/events and perform navigation or platform UI. ViewModels
  must not depend on `Router`, `NavController`, or `Context`.

## Review Questions

- Is the domain model free of UI and transport concerns?
- Are DTOs mapped before crossing into domain or presentation?
- Are Retrofit services in `api`, data source implementations in
  `source.remote`/`source.local`, DTOs in `model`, and conversions in `mapper`?
- Does the repository act as the single source of truth when cache/local data
  exists?
- Is the transport-failure to domain-error translation done once in
  repository/data mappers, not in use cases or ViewModels?
- Does presentation expose `ErrorUiModel`, not the domain error or the transport
  failure type, to composables?

## Anti-patterns

- DTOs, request models, or response models under `api` or `source.remote`.
- Retrofit, OkHttp, serialization, or `NetworkFailure` types leaking into
  domain or presentation.
- Domain-error to presentation-error mapping inside composables.
- Base ViewModel, inherited error hooks, class delegation, or global event buses
  for ordinary feature error handling.
