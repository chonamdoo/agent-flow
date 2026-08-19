---
name: flutter-development-guide
description: Flutter implementation and review checklist. Use only when writing, modifying, or reviewing Flutter widget code, layout constraints, adaptive sizing, navigation and routing, platform channels, list and image performance, async gaps across `BuildContext`, disposal, theme and design tokens, accessibility, localization, offline and error states, or Flutter test and golden coverage. Do not use for Dart language generalities, native Android or iOS internals, or broad rewrites.
---

# Flutter Development Guide

Use this as a secondary checklist after user request, repo instructions, existing repo patterns, and `code-generation-discipline`. Do not score it. Do not use best-practice generalities to force broad rewrites.

## Scope

- Include widget code, layout constraints, navigation and routing, platform channels, list and image performance, lifecycle, disposal, accessibility, localization, offline and error states, and Flutter test coverage.
- Exclude Dart language generalities, which belong to `dart-development-guide`, and native Android or iOS implementation details.
- For presentation state ownership, provider graphs, and domain-to-UI mapping, apply `flutter-clean-presentation-architecture` instead of restating those rules here.

## Write

- Rebuild the smallest subtree that changed. Move the changing part into its own widget rather than adding a `setState` at the top of a large `build`.
- Give a widget a `const` constructor when its fields are final, so an unchanged subtree can skip rebuilding.
- Give list and reorderable children a stable domain-id `Key` when items can be inserted, removed, or reordered, and keep state-bearing children keyed.
- Read layout errors as constraint problems: an unbounded `Column` or `Row` child needs `Expanded`, `Flexible`, or a bounded parent, not a hardcoded size.
- Re-check `mounted` before touching `BuildContext` — `setState`, `Navigator`, `ScaffoldMessenger`, `Theme.of` — on the far side of an `await`.
- Dispose every `AnimationController`, `FocusNode`, `TextEditingController`, and `ScrollController` the `State` owns.
- Use `ListView.builder` or `SliverList` for lists whose length is data-driven, and keep `itemBuilder` cheap.
- Keep loading and error builders on remote images and futures so a failed load renders a defined state.
- Preserve loading, empty, offline, timeout, retry, and error states when touching a data-backed screen.
- Keep route names, path parameters, and deep-link shapes compatible with the existing router configuration when changing navigation.
- Keep user-visible strings in the existing localization boundary instead of literals in widgets.
- Give interactive widgets a reachable tap target and a useful semantic label, and keep decorative widgets out of the semantics tree.
- Adapt to size with `LayoutBuilder`, `MediaQuery`, or the existing breakpoint helper instead of branching on platform for layout.
- Keep `MethodChannel`, `EventChannel`, and plugin calls behind the existing platform boundary, and preserve permission denied, restricted, and granted paths.
- Isolate `Theme` and design-token reads through the existing theme extension rather than hardcoding colors and text styles.

## Test

- Run the profile's typecheck, lint, and test gates for changed Flutter code when available.
- Write `testWidgets` cases for changed widget behavior, and pump explicitly rather than relying on a bare `pumpAndSettle` for an animation that never settles.
- Verify loading, empty, error, and offline states when the changed flow can reach them.
- Update or add a golden test only where the repo already keeps goldens for that surface.
- For lists, verify insertion, removal, reordering, and scroll behavior on realistic data when feasible.

## Review

- Blocking only: crash risk, layout overflow or unbounded-constraint failure, `BuildContext` used after an async gap without a `mounted` check, an undisposed controller the `State` owns, broken permission or navigation flow, list performance cliff, accessibility or tap-target regression, offline or error state that breaks a user flow, analyze or test failure, or project-rule violation.
- Keep generic Flutter best-practice observations as suggestions unless they prevent a concrete failure.
- Do not block on optional `const` additions, preferred widget composition style, theoretical rebuild cost without a measured path, or Dart language generalities.

## Sources

- Flutter docs: constraints and layout, performance best practices, and rebuild scope.
- Flutter docs: accessibility, internationalization, and platform channel boundaries.
- Flutter test docs: widget tests and golden file tests.
- Repo configuration, existing router, theme, and localization patterns override generic advice.
