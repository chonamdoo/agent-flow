---
name: display-glasses-with-jetpack-compose-glimmer
description: Builds and reviews projected Android XR experiences for display glasses with Jetpack Compose Glimmer, projected contexts, glasses-scoped permissions, additive-display design, focus, and Glimmer components. Use when implementing a projected glasses Activity, GlimmerTheme UI, display-glasses input, or camera, microphone, and sensor access through a projected device.
---

# Display Glasses with Jetpack Compose Glimmer

This skill is for a dedicated projected Activity shown on display glasses. Do
not render the Glimmer surface on the host phone or mix Material components into
the projected UI.

## Prerequisites

- Confirm `compileSdk` is at least 37 before editing XR code.
- Confirm the Activity declares the projected display category
  `xr_projected` in the manifest.
- Resolve a compatible version of `androidx.xr.glimmer:glimmer` and
  `androidx.xr.glimmer:glimmer-google-fonts` from the project's dependency
  source; do not guess versions.
- Establish whether code runs in the projected Activity or a phone component;
  context and permission handling differ.

## Quick start

1. Create or locate the dedicated projected Activity and its host launch path.
2. Set the projected root to pure black, then wrap all UI in `GlimmerTheme`
   using `createGoogleSansFlexTypography()`.
3. Replace Material UI with Glimmer `Text`, `Card`, `Button`, `Icon`, lists, or
   stacks.
4. Bottom-align a minimal, glanceable surface and show one primary item at a
   time.
5. Enable initial focus and map tap, swipe, and system Back deliberately.
6. Request projected-device permissions before opening glasses hardware.
7. Verify on a connected projected device and across disconnect/reconnect.

```kotlin
GlimmerTheme(typography = createGoogleSansFlexTypography()) {
    Box(
        modifier = Modifier.fillMaxSize().background(Color.Black),
        contentAlignment = Alignment.BottomCenter,
    ) {
        Card { Text("Ready", style = GlimmerTheme.typography.bodySmall) }
    }
}
```

## Progressive references

- Read [setup-and-hardware.md](references/setup-and-hardware.md) for projected
  Activity launch, contexts, permissions, camera, microphone, and lifecycle.
- Read [design-and-components.md](references/design-and-components.md) for
  additive-display rules, typography, depth, component selection, lists, and
  stacks.
- Read [focus-and-verification.md](references/focus-and-verification.md) for
  input mapping, focus behavior, accessibility, and final checks.

## Hard constraints

- Use `GlimmerTheme`, not `MaterialTheme`, in the glasses Activity.
- Use Glimmer `Text`; Material text can resolve to an invisible dark color.
- Keep the projected root background exactly black.
- Never assume phone permission also grants glasses hardware permission.
- Never retain a projected context or service after disconnect.
- Keep readable text at 18sp or larger and avoid thin font weights.
