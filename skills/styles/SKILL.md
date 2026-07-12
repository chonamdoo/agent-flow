---
name: styles
description: Integrates the experimental Jetpack Compose Styles API into custom Android design-system components, including theme styles, `Modifier.styleable`, interaction state, and visual migration. Use when adopting `androidx.compose.foundation.style.Style`, replacing hard-coded custom-component appearance, or reviewing Compose Styles code.
---

# Jetpack Compose Styles

Warn the user before editing: this API is experimental, requires alpha Compose
dependencies and an explicit compiler opt-in, and may change. Proceed only when
that risk is accepted.

## Scope and prerequisites

- Support custom Compose components and custom themes only. Do not claim Styles
  support for Material components that do not expose a Styles API.
- Require `compileSdk` 37 or later.
- Require `androidx.compose.foundation:foundation` 1.12.0-alpha01 or later, or
  Compose BOM 2026.04.01 or later. Verify the currently supported compatible
  version before updating the project.
- Use the exact `androidx.compose.foundation.style.Style` package and opt into
  `ExperimentalFoundationStyleApi` at project level.

## Quick start

1. Confirm experimental dependency changes are in scope.
2. Inspect the theme, design tokens, custom components, interaction sources,
   and existing screenshot or UI tests.
3. Establish a visual baseline before changing a component.
4. Create central component defaults, expose a `style: Style = Style`
   parameter, and apply defaults plus caller overrides with `styleable`.
5. Connect interactive components to one `MutableInteractionSource` and
   `StyleState`.
6. Move visual properties into Styles; keep behavior, semantics, gestures, and
   one-off structural layout in Modifiers.
7. Build, run UI and screenshot tests, and compare visual parity.

## Progressive references

- Read [setup-and-migration.md](references/setup-and-migration.md) for Gradle
  setup and an end-to-end component migration.
- Read [fundamentals.md](references/fundamentals.md) for supported properties,
  override order, inheritance, composition, and Style-versus-Modifier rules.
- Read [state-and-animation.md](references/state-and-animation.md) for built-in
  interaction states, animation, and custom `StyleState` keys.
- Read [theming.md](references/theming.md) for atomic/component styles and
  custom theme integration.

## Constraints

- Styles are last-write-wins; Modifiers are additive. Preserve intended order.
- Styles configure visual appearance, not click logic, gestures, semantics, or
  unsupported custom layout behavior.
- Use `CompositionLocal.currentValue` from `StyleScope` extensions.
- Prefer dynamic tokens inside one Style over conditionally swapping whole
  Style objects, unless the styles are fundamentally different.
- Do not update screenshot goldens without review of the rendered change.

## Completion check

The module builds with the explicit opt-in, custom components retain behavior
and accessibility, caller styles override defaults predictably, interaction
states render correctly, and reviewed screenshots show no unintended visual or
layout regression.
