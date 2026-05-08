# Compose UI Guide

## Composition

- Entry composables wire dependencies; screen composables render state.
- Prefer small stateless components with explicit parameters.
- Keep business decisions out of composables; map to UI models first.
- Use the existing design system for color, typography, spacing, icons, and
  buttons.

## State

- Collect flows with lifecycle-aware APIs when available.
- Hoist state to the ViewModel or parent composable unless it is purely local UI
  state.
- Preserve user-editable local state with `rememberSaveable` when rotation or
  process recreation matters.

## Lists

- Lazy lists need stable `key` values.
- Do not allocate heavy objects inside item lambdas.
- Put separators, empty states, and loading rows behind explicit state.

