---
name: edge-to-edge
description: Migrates Jetpack Compose screens to edge-to-edge drawing while keeping controls, lists, IME content, dialogs, and system-bar icons safe and legible. Use when targeting SDK 35+, enabling `enableEdgeToEdge`, or fixing content hidden by status bars, navigation bars, cutouts, or the soft keyboard.
---

# Edge-to-edge Compose

Make edge-to-edge ownership explicit. Apply each inset once, at the component
that must remain safe, while allowing backgrounds and scroll content to draw
behind system bars.

## Prerequisites

- The affected UI uses Jetpack Compose.
- The project targets SDK 35 or later. Treat a target SDK upgrade as part of the
  requested scope before changing it.
- Inventory every Activity, list, FAB, text field, adaptive scaffold, bottom
  bar, and full-screen dialog affected by the change.

## Quick start

1. Add `enableEdgeToEdge()` before `setContent()` in each Activity that lacks it.
2. Set `android:windowSoftInputMode="adjustResize"` on Activities with soft
   keyboard input.
3. Choose one inset owner per edge: Scaffold `innerPadding`, a component's
   `windowInsets`, an inset-padding modifier, or `WindowInsetsRulers`.
4. Pass insets to lazy-list `contentPadding`; do not pad the list's parent.
5. Keep FABs and critical controls above safe drawing bounds.
6. Give IME movement one owner and preserve focus while the keyboard opens.
7. Verify gesture and three-button navigation, light and dark themes, rotation,
   cutouts, and keyboard visibility.

## Progressive references

- Read [insets-and-ime.md](references/insets-and-ime.md) for Scaffold, padding,
  rulers, adaptive screens, lists, and correct/incorrect IME patterns.
- Read [system-ui-and-verification.md](references/system-ui-and-verification.md)
  for icon contrast, navigation-bar contrast, protection scrims, full-screen
  dialogs, and the verification matrix.

## Constraints

- Never apply equivalent insets at both parent and child.
- Do not put `safeDrawingPadding()` on a `NavigationSuiteScaffold`; apply safe
  areas to its individual destination content.
- Do not use deprecated `SOFT_INPUT_ADJUST_RESIZE`; use the manifest attribute.
- Put `imePadding()` before `verticalScroll()` when that pattern owns IME space.
- Do not apply parent padding that prevents app bars or scrolling content from
  drawing behind system bars.
- Do not set system-bar icon appearance manually when
  `ComponentActivity.enableEdgeToEdge()` already manages it.

## Completion check

The target module builds, all critical content remains visible and tappable,
scrolling extends behind bars without clipping, the focused field stays above
the IME without double padding, and system icons remain legible in every tested
navigation and theme mode.
