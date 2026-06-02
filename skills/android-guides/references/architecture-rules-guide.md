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

- Keep domain-facing app errors and results in a shared domain error module, for
  example `core/domain/error/model/AppError.kt`, `AppResult.kt`,
  `Severity.kt`, `HttpStatus.kt`, and `ServerCode.kt`.
- Keep raw transport failures in `core/network/error/NetworkFailure.kt`.
- `HttpStatus` and `ServerCode` may stay in network only when they remain raw
  diagnostics. If `AppError` exposes them as field types, own them in domain to
  avoid a domain-to-network dependency.

## Error Flow

- Remote data sources call Retrofit and may throw or return `NetworkFailure`.
- Repository implementations in `core:data:*` catch `NetworkFailure`, map it to
  `AppError`, and return `AppResult<T>`.
- Use cases return `AppResult<T>` and add `AppError` only for domain/business
  rule failures.
- ViewModels map `AppResult<T>` to `UiState` and `UiEvent`.
- Presentation mappers map `AppError` to feature `ErrorUiModel` or shared
  presentation error models.
- Routes collect state/events and perform navigation or platform UI. ViewModels
  must not depend on `Router`, `NavController`, or `Context`.

## Review Questions

- Is the domain model free of UI and transport concerns?
- Are DTOs mapped before crossing into domain or presentation?
- Are Retrofit services in `api`, data source implementations in
  `source.remote`/`source.local`, DTOs in `model`, and conversions in `mapper`?
- Does the repository act as the single source of truth when cache/local data
  exists?
- Is `NetworkFailure -> AppError` translated once in repository/data mappers,
  not in use cases or ViewModels?
- Does presentation expose `ErrorUiModel`, not `AppError` or `NetworkFailure`,
  to composables?

## Anti-patterns

- DTOs, request models, or response models under `api` or `source.remote`.
- Retrofit, OkHttp, serialization, or `NetworkFailure` types leaking into
  domain or presentation.
- `AppError -> ErrorUiModel` mapping inside composables.
- Base ViewModel, inherited error hooks, class delegation, or global event buses
  for ordinary feature error handling.
