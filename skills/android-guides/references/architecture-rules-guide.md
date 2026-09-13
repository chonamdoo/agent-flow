# Android Architecture Rules

Before applying this reference, read the required
[clean-architecture-core](../../clean-architecture-core/SKILL.md) and
[android-clean-architecture](../../android-clean-architecture/SKILL.md) in full.
They own semantic rules and Android mappings. For presentation work, also read
[android-clean-presentation-architecture](../../android-clean-presentation-architecture/SKILL.md)
as required by the core's **Required Application** rule.

## Layer Ownership

- Android presentation selects strings/resources alongside its Compose UI,
  ViewModels, UI state, and navigation adapters.
- Network adapter details include Retrofit/OkHttp setup and serialization.

Apply the required core's **Semantic Layers** for ownership.

## Dependency Direction

Apply the required core's **Dependency Rule** and Android adapter's **Hilt DI**,
including its application `@Inject constructor` exception. Android, Retrofit,
Room, Compose, and Hilt are framework implementations for this boundary.

- Prefer navigation APIs or shared contracts over feature implementation dependencies.

## Data Source Responsibilities

Apply the required core's **Repository And Source Boundary** and **Mapping
Boundary**, including recorded simple-adapter, DB-only, and safe-representation
exceptions. Locate Android implementations through the adapter's **Module
Boundaries**, not by package names.

## Error Type Ownership

Apply the required core's full **Error Boundary**, including exposed error value
ownership and the existing `Result`/exception contract exception.

## Error Flow

Apply the required core's **Error Boundary**. For screen error state and effects,
apply Android presentation's **ViewModel Rule** and **Compose Screen Rule**;
route wiring owns navigation/platform UI, not ViewModels.

## Review Questions

- Are actual Android module/source-set dependencies mapped and checked?
- Does the implementation satisfy the required core's **Repository And Source
  Boundary**, **Mapping Boundary**, and **Error Boundary**?

## Anti-patterns

Apply the required core's **Must Avoid** and **Error Boundary** in addition to
these Android presentation constraints:

- Domain-error to presentation-error mapping inside composables.
- Base ViewModel, inherited error hooks, class delegation, or global event buses
  for ordinary feature error handling.

For server-driven screens, apply Android presentation's **Server-Driven Screen
Exception**; ordinary feature constraints do not erase its three scoped exceptions.
