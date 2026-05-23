# Android Architecture Rules

Canonical layer boundary and dependency-direction rules live in
`skills/clean-architecture/SKILL.md`. These are Android-specific naming and
ownership notes. Project-local rules win when they are explicit.

## Layer Ownership

- `presentation`: Compose UI, ViewModels, UI state, navigation adapters,
  UI models, string/resource selection.
- `domain`: entities, value objects, repository interfaces, use cases,
  domain errors, policies, pure validation.
- `usecase`: orchestration of domain operations when the project separates it
  from domain.
- `data`: DTOs, API/database clients, data sources, repository
  implementations, mappers, cache policy, error translation.

## Dependency Direction

- Domain/Application must not import Android framework, Retrofit, Room, Compose,
  Hilt, or presentation classes.
- Data may depend on domain contracts and external clients.
- Presentation may depend on domain/usecase and design-system APIs.
- Feature-to-feature direct dependencies are suspect; prefer navigation APIs or
  shared contracts.

## Review Questions

- Is the domain model free of UI and transport concerns?
- Are DTOs mapped before crossing into domain or presentation?
- Does the repository act as the single source of truth when cache/local data
  exists?
- Are domain errors translated once, not scattered across UI branches?
