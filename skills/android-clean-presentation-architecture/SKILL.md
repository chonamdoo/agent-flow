---
name: android-clean-presentation-architecture
description: Defines Android Clean Architecture presentation guidance for Hilt, ViewModel, UiState, UiEvent, UiModel mapping, and Compose wiring. Use when creating, modifying, or reviewing Android presentation code.
required_markers:
  - "presentation-skill: android"
  - "presentation-state-based-development: applied|n/a"
  - "presentation-state-review: pass|fail"
  - "ui-state-modeling: explicit"
  - "presentation-mapping-boundary: domain-to-uimodel|n/a"
  - "di-boundary: hilt"
---

# Android Clean Presentation Architecture

## Quick start

1. Locate the screen state holder and its domain dependencies.
2. Model durable `UiState` before wiring Compose routes and screens.
3. Keep `UiAction` as input and `UiEvent` only for non-durable UI behavior.
4. Map domain models to immutable `UiModel` values before rendering.
5. Read [references/full-guidance.md](references/full-guidance.md) for the complete DI, state, event, Compose, navigation, and review contract.

## Do not use for

- AppShell-owned common error hosts, session expiry, maintenance flow switching, or root navigation resets; use `android-appshell-error-handling`.
- Data, network, or domain implementation except to enforce presentation boundaries.

## Runtime skill loading

Resolve every matching `compose-*`, `kotlin-*`, `navigation-3`, `edge-to-edge`, `adaptive`, and `testing-setup` skill through the leader checkout's `.agent-flow/skills/index.json`. Read only indexed project snapshot paths and never fall back to host-global directories.

## Core contract

- Presentation depends on domain use cases and platform/UI abstractions.
- ViewModels inject use cases, not Retrofit APIs, data sources, or repository implementations.
- Feature API modules own exported route contracts; presentation modules own screens, ViewModels, UI contracts, UI models, and mappers.
- Expose immutable `StateFlow<ScreenUiState>`; keep mutable state private.
- Use explicit states for loading, refresh, empty, error, offline, permission, and success when they can occur.
- Route wiring obtains the ViewModel, collects state/events, and performs navigation.
- Screen/content composables receive plain state and callbacks and remain stateless.
- Keep `Context`, `NavController`, launchers, intents, and Compose state objects out of ViewModels.
- Use `@Provides` for constructed dependencies and `@Binds` for interface mappings.
- Do not introduce base ViewModel/state inheritance for new presentation work.

## Review gate

Confirm the full reference checklist, then record:

- `presentation-skill: android`
- `presentation-state-based-development: applied|n/a`
- `presentation-state-review: pass|fail`
- `ui-state-modeling: explicit`
- `presentation-mapping-boundary: domain-to-uimodel|n/a`
- `di-boundary: hilt`
