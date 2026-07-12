---
name: jetpack-compose-m3
description: Implements and migrates Wear OS interfaces with Wear Compose Material 3, Foundation, Navigation 3, AppScaffold, ScreenScaffold, and TransformingLazyColumn using version-matched local samples. Use when creating, updating, reviewing, or migrating a Wear OS Compose project from Material 2.5, ScalingLazyColumn, legacy navigation, or Horologist UI libraries.
---

# Wear OS Compose Material 3

Use Wear-specific Material 3 APIs, not phone Material 3 or legacy Wear Material
imports. Preserve existing behavior while adopting Material 3 defaults rather
than forcing old screenshots onto the new design system.

## Prerequisites

- Resolve the latest stable compatible Wear Compose versions through the
  project's trusted dependency tooling. Ignore alpha, beta, and RC releases
  unless the user explicitly requests pre-release APIs.
- Require Kotlin 2.0 or newer with `org.jetbrains.kotlin.plugin.compose`.
- Require `minSdk` 25 or newer.
- Align `compose-material3`, `compose-foundation`, and any
  `compose-navigation3` dependencies to compatible versions.

## Quick start

1. Inspect the version catalog and Wear imports; inventory Material 2.5,
   Horologist UI, `ScalingLazyColumn`, scaffold, and navigation usage.
2. Update dependencies, then complete a Gradle sync before editing source.
3. Extract and read the exact version's local `samples-sources.jar`; do not
   implement from memory or a mismatched sample.
4. Use one `AppScaffold`, one `ScreenScaffold` per screen, and
   `TransformingLazyColumn` for content that can scroll at any font scale.
5. Use Wear Material 3 theme tokens and each component's `*Defaults` object.
6. Add Navigation 3 with `SwipeDismissableSceneStrategy` for new navigation.
7. Build and verify rotary, touch, focus, font-scale, ambient, and screenshots.

## Progressive references

- Read [setup-and-samples.md](references/setup-and-samples.md) before proposing
  code; sample extraction is a mandatory precondition.
- Read [components-and-migration.md](references/components-and-migration.md)
  when selecting or migrating components, scaffolds, lists, or navigation.
- Read [verification.md](references/verification.md) before declaring a Wear
  change complete.

## Stop conditions

- If the stable version cannot be established, ask instead of guessing.
- If Gradle sync fails, diagnose resolution before refactoring source.
- If an API is unresolved after a successful sync, inspect the exact artifact
  sample and dependency graph; do not downgrade reflexively.
- If version-matched sample sources cannot be found or downloaded, report the
  environment blocker and do not invent the API signature.
