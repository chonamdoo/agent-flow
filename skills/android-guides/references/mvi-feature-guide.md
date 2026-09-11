# MVI Feature Guide

Use MVI when a screen has meaningful state, user actions, loading/error
branches, or asynchronous data.

## Components

- `UiState`: immutable state representation for the screen.
- `Action` or `Intent`: user/system events accepted by the ViewModel.
- `ViewModel`: action reducer and async orchestration.
- `UiModel`/mapper: presentation shape derived from domain data.
- `View`: entry composable that wires ViewModel to screen.
- `Screen`: stateless composable that branches on state.

## State Coverage

Prefer explicit states:

- Loading or placeholder
- Success
- Empty
- Error

Use booleans for small overlays only when they do not replace the main screen
state.

## ViewModel Rules

- A ViewModel may depend on one context's repository interface when no
  orchestration is needed. Require a use case for cross-context work, ordered
  side effects, or added business failure semantics. Never inject repository
  implementations, data sources, or API clients directly.
- Keep action handling exhaustive.
- Keep one-off navigation or toast events separate from durable UI state if the
  project has an event/effect channel.
- Avoid storing Android `Context`, `Activity`, or composables in ViewModel.

