# Android Architecture Rules

Canonical layer and dependency-direction rules live in
[clean-architecture-core](../../clean-architecture-core/SKILL.md); apply
[android-clean-architecture](../../android-clean-architecture/SKILL.md) for
Android mappings. Discover explicit project roles rather than judging folder names.

## Layer Ownership

- Presentation owns Compose UI, ViewModels, UI state, navigation adapters,
  UI values, and string/resource selection.
- Domain owns entities, values, repository contracts, errors, and pure policy.
- Application owns orchestration when needed; a UI state holder may use one
  context's repository interface without a forwarding use case.
- Data owns DTOs, sources, repository implementations, conversion, cache policy,
  and transport/storage failure translation.
- Network owns Retrofit/OkHttp setup, serialization, raw failures, and diagnostics.

## Dependency Direction

- Pure domain policy imports no Android, Retrofit, Room, Compose, Hilt, or
  presentation implementation types.
- Pure application orchestration keeps framework implementations behind ports.
  Adopted application `@Inject constructor` metadata is allowed under the Android
  adapter; it does not permit Hilt types in pure domain policy.
- Data depends on domain/application contracts and external clients.
- Presentation depends on domain/application contracts and UI/platform abstractions,
  never repository implementations or raw clients.
- Prefer navigation APIs or shared contracts over feature implementation dependencies.

## Data Source Responsibilities

Keep transport interfaces, remote access/auth plumbing, local persistence/cache,
DTOs, and data-domain conversion at their owning adapter boundaries. Map them to
existing source roots and conventions; a package name is not proof of separation.
Split sources and mappers when policy or change reasons differ. Preserve the
core's simple-adapter and DB-only exceptions; no extra forwarding classes or
identity copies are required.

## Error Type Ownership

Apply these rules to the project's established error/result representation:

- Domain-facing errors and exposed severity/status/server-code value types belong
  to domain/application contracts, not transport implementation modules.
- Raw transport failures and diagnostics stay in the network adapter.
- If the project defines no typed result wrapper, preserve its existing
  `Result`/exception contract instead of introducing one as a review requirement.

## Error Flow

- Remote sources may throw or return transport failures.
- The owning data boundary converts those failures to established domain errors.
- Use cases preserve that result contract, adding only business failure semantics.
- Presentation maps domain/application outcomes into screen state and transient
  effects; rendering receives UI-facing error values, not raw failures.
- Routes collect state/events and execute navigation/platform UI. ViewModels do
  not depend on `Router`, `NavController`, or `Context`.

## Review Questions

- Are actual module/source-set dependencies consistent with these roles?
- Are DTOs and persistence entities converted before crossing the adapter boundary?
- Does repository source-of-truth policy remain explicit when local data exists?
- Is failure translation owned once at each semantic boundary?
- Do composables receive presentation-facing errors rather than transport types?

## Anti-patterns

- Retrofit, OkHttp, serialization, or raw failure types leaking into pure domain
  or presentation.
- Data conversion or source/cache policy leaking into consumers.
- Domain-error to presentation-error mapping inside composables.
- Base ViewModel, inherited error hooks, class delegation, or global event buses
  for ordinary feature error handling.
