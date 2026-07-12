---
name: ios-clean-presentation-architecture
description: Defines iOS Clean Architecture presentation guidance for SwiftUI/UIKit state holders, UiState, UiModel mapping, dependency injection, and state review. Use when creating, modifying, or reviewing iOS presentation code.
required_markers:
  - "presentation-skill: ios"
  - "presentation-state-based-development: applied|n/a"
  - "presentation-state-review: pass|fail"
  - "ui-state-modeling: explicit"
  - "presentation-mapping-boundary: domain-to-uimodel|n/a"
  - "di-boundary: swift-environment|factory|swift-dependencies|swinject|needle|direct|existing|n/a"
---

# iOS Clean Presentation Architecture

## Quick start

1. Locate the SwiftUI/UIKit screen state holder and its domain dependencies.
2. Model durable `UiState` before wiring views.
3. Keep `UiAction` as input and `UiEvent` only for non-durable UI behavior.
4. Map domain values and errors to presentation `UiModel` values before rendering.
5. Read [references/full-guidance.md](references/full-guidance.md) for the complete DI, state, event, SwiftUI/UIKit, Swinject, and review contract.

## Do not use for

- AppShell/root-coordinator common error hosts, session expiry, maintenance flow switching, or root navigation resets; use `ios-app-shell-error-handling`.
- Data, infrastructure, or domain implementation except to enforce presentation boundaries.

## Core contract

- Presentation depends on domain use cases or ports, not concrete API, storage, or repository implementations.
- Wrap Keychain, CoreLocation, Photos, notifications, analytics, and other platform APIs behind ports/adapters.
- Prefer initializer injection and a composition root; use SwiftUI environment for dependencies that must flow through a view tree.
- Existing repository DI standards win. Add Factory, swift-dependencies, Swinject, or Needle only when justified.
- Use explicit states for not-ready, loading, refresh, empty, error, success, offline, and permission when they can occur.
- Keep pagination, cancellation, retry, and optimistic-update details private in the state holder.
- Mutate UI state on `MainActor`.
- Composition roots create dependencies; views remain render-focused and previewable with test doubles.
- Keep one-shot navigation, banners, haptics, permissions, sheets, and focus out of durable UI state.
- Do not leak mutable DI containers into presentation code.

## Review gate

Confirm the full reference checklist, then record:

- `presentation-skill: ios`
- `presentation-state-based-development: applied|n/a`
- `presentation-state-review: pass|fail`
- `ui-state-modeling: explicit`
- `presentation-mapping-boundary: domain-to-uimodel|n/a`
- `di-boundary: swift-environment|factory|swift-dependencies|swinject|needle|direct|existing|n/a`
