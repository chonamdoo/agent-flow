---
name: flutter-development-guide
description: Flutter implementation and review checklist. Use only when writing, modifying, or reviewing Flutter widget code, layout constraints, adaptive sizing, navigation and routing, platform channels, platform views, background entry points, list and image performance, async gaps across `BuildContext`, disposal, theme and design tokens, accessibility, localization, offline and error states, or Flutter test and golden coverage. Do not use for Dart language generalities, native Android or iOS internals beyond the engine and channel-registration seam a Dart entry point depends on, or broad rewrites.
---

# Flutter Development Guide

Use this as a secondary checklist after user request, repo instructions, existing repo patterns, and `code-generation-discipline`. Do not score it. Do not use best-practice generalities to force broad rewrites.

## Scope

- Include widget code, layout constraints, navigation and routing, platform channels, platform views, list and image performance, lifecycle, disposal, accessibility, localization, offline and error states, and Flutter test coverage.
- Exclude Dart language generalities, which belong to `dart-development-guide`, and native Android or iOS implementation details apart from the engine, channel-registration, and platform-view seams a Dart entry point depends on.
- For presentation state ownership, provider graphs, and domain-to-UI mapping, apply the selected architecture contract's guidance (`flutter-clean-presentation-architecture` in Clean mode) instead of restating those rules here.

## Write

- Rebuild the smallest subtree that changed. Move the changing part into its own widget rather than adding a `setState` at the top of a large `build`.
- Give a widget a `const` constructor when its fields are final, so an unchanged subtree can skip rebuilding.
- Give list and reorderable children a stable domain-id `Key` when items can be inserted, removed, or reordered, and keep state-bearing children keyed.
- Re-check `mounted` before touching `BuildContext` — `setState`, `Navigator`, `ScaffoldMessenger`, `Theme.of` — on the far side of an `await`.
- Dispose every `AnimationController`, `FocusNode`, `TextEditingController`, and `ScrollController` the `State` owns.
- Use `ListView.builder` or `SliverList` for lists whose length is data-driven, and keep `itemBuilder` cheap.
- Keep loading and error builders on remote images and futures so a failed load renders a defined state.
- Preserve loading, empty, offline, timeout, retry, and error states when touching a data-backed screen.
- Keep route names, path parameters, and deep-link shapes compatible with the existing router configuration when changing navigation.
- Keep user-visible strings in the existing localization boundary instead of literals in widgets.
- Give interactive widgets a reachable tap target and a useful semantic label, and keep decorative widgets out of the semantics tree.
- Keep `MethodChannel`, `EventChannel`, and plugin calls behind the existing platform boundary, and preserve permission denied, restricted, and granted paths.
- Identify each background path's plugin, platform, version, isolate, `FlutterEngine`, and `BinaryMessenger` ownership before changing registration. FlutterFire messaging spawns a separate isolate on Android but does not require one on iOS/macOS; this does not establish the engine model for every background plugin.
- Ensure host APIs are reachable through the actual execution engine or supported background-isolate path. For an Android engine-attached plugin, register against the messenger supplied to `onAttachedToEngine` and ensure that engine runs the plugin registrant; registration only in an activity does not cover a separately created engine. A background isolate registered to a root isolate can instead use `BackgroundIsolateBinaryMessenger.ensureInitialized` with the root token, subject to the plugin's supported behavior.
- Preserve runtime-discovered entry points according to the invoking API and toolchain. FlutterFire's background handler must be top-level and non-anonymous, with `@pragma('vm:entry-point')` on Flutter 3.3.0 and later to protect it from release tree-shaking. For other callbacks, verify their documented discovery/preservation contract rather than assuming callback handles or obfuscation behave identically. Exercise the affected release path.
- Claim gestures for an embedded native view with the `gestureRecognizers` parameter of `AndroidView` or `UiKitView` rather than reimplementing pan and zoom; both widgets participate in Flutter's gesture arena.
- Evaluate platform-view composition tradeoffs on supported devices. Android texture-layer composition can show jank during high-frequency native-view scrolling; it is a performance risk to measure, not an inevitable failure. iOS platform views use hybrid composition without the same Android mode choice.
- Discard commands queued before an asynchronously created native view was ready once its `State` is disposed. On Android `PlatformView.dispose` leaves the view unusable, so a later flush targets a dead view.
- Decide who owns keyboard avoidance for an embedded `WebView`. `Scaffold.resizeToAvoidBottomInset` defaults to `true` and resizes the platform view's frame, which changes the page's own viewport; set it to `false` when the page corrects for the keyboard itself.
- Isolate `Theme` and design-token reads through the existing theme extension rather than hardcoding colors and text styles.

## Layout and responsiveness

- Start from behavior, not a preferred widget. Use `Row` or `Column` for a bounded, non-scrolling linear single-run layout; `Wrap` when items may reflow; lazy `ListView`, `GridView`, or slivers for large or data-driven scrolling collections; and `SingleChildScrollView` only for small finite content that normally fits. Use `Stack` and `Positioned` for actual overlap or edge-relative positioning, not ordinary linear flow.
- Follow Flutter's box protocol: constraints go down, sizes go up, and the parent sets position. A fixed width or height still participates in this protocol; it does not override its parent's constraints.
- Check for a bounded main axis before using non-zero flex. Use `Expanded` for a tight fill and `Flexible` for a loose fit as a direct child of `Flex`, `Row`, or `Column`, with wrappers such as `Padding` inside it; under an unbounded main axis, remove the flex, introduce finite constraints, or intentionally shrink-wrap instead.
- Fix overflow at the first violated constraint. Constrain or flex a variable child, reflow content, choose a scrolling collection, or switch structure at a breakpoint according to the intended interaction; do not add a scroll wrapper merely to hide the symptom.
- For uniform Flex sibling gaps, prefer `Flex.spacing` when the project's Flutter toolchain supports it. Use `SizedBox` for an isolated or uneven gap, `Padding` for a content inset, and `Wrap.spacing` or `runSpacing` for runs. Every gap still consumes constrained space.
- Avoid fixed window-like dimensions and coordinate-based mockup placement. Fixed constraints remain valid for an intrinsic control, accessible hit target, icon, thumbnail, aspect ratio, design token, or intentional min/max bound.
- Do not blanket ban `Padding`, `Container`, fixed constraints, `Stack`, or scrolling. Prefer the narrow widget that states the intent, but keep each contextual tool when it satisfies the actual layout behavior.
- Use `MediaQuery.sizeOf` for the app window and `LayoutBuilder` for the space allocated by a parent, or preserve the repo's existing breakpoint helper. Base layout branches on available space rather than hardware type or top-level orientation.
- Constrain reading and form widths on large windows instead of stretching them indefinitely. Preserve state across resize, split-screen, rotation, and fold or unfold transitions.
- Place `SafeArea` where system intrusions can hide meaningful content. Consider `MediaQuery.viewInsetsOf` for the IME and `MediaQuery.displayFeaturesOf` for hinges or folds instead of applying one global inset rule.
- Preserve user text scaling and use `TextScaler` rather than deprecated `textScaleFactor` APIs in new code. Prefer directional padding, alignment, and positioning when start/end meaning must follow RTL.
- Verify the supported narrow and wide sizes, live resize, long localized and RTL text, large text scaling, keyboard and IME visibility, system intrusions, pointer input, keyboard traversal, and focus behavior.

## Test

- Run the profile's typecheck, lint, and test gates for changed Flutter code when available.
- Write `testWidgets` cases for changed widget behavior, and pump explicitly rather than relying on a bare `pumpAndSettle` for an animation that never settles.
- Verify loading, empty, error, and offline states when the changed flow can reach them.
- Update or add a golden test only where the repo already keeps goldens for that surface.
- For lists, verify insertion, removal, reordering, and scroll behavior on realistic data when feasible.

## Review

- Blocking only: crash risk, layout overflow or unbounded-constraint failure, `BuildContext` used after an async gap without a `mounted` check, an undisposed controller the `State` owns, a background channel unreachable from its actual engine/isolate path, a runtime-discovered entry point missing preservation required by its invoking API/toolchain, a platform view command queue that outlives its `State`, broken permission or navigation flow, list performance cliff, accessibility or tap-target regression, offline or error state that breaks a user flow, analyze or test failure, or project-rule violation.
- Do not treat correct `Scaffold` inset settings as evidence that a keyboard bug inside a `WebView` belongs to the page; name the layer that owns the inset first.
- Keep generic Flutter best-practice observations as suggestions unless they prevent a concrete failure.
- Do not block on optional `const` additions, preferred widget composition style, theoretical rebuild cost without a measured path, or Dart language generalities.

## Sources

- Flutter docs: understanding constraints, adaptive and responsive general guidance and best practices, SafeArea and MediaQuery, user input, accessibility, and internationalization.
- Flutter API: Flex, Row, Column, Expanded, Flexible, Wrap, Stack, Positioned, Padding, Container, SizedBox, LayoutBuilder, MediaQuery, TextScaler, and scrollable widgets.
- Flutter docs: Android and iOS platform view composition modes, the `AndroidView` and `UiKitView` gesture-arena contract, and the `PlatformView.dispose` lifecycle.
- [FlutterFire background messages](https://firebase.google.com/docs/cloud-messaging/flutter/receive-messages#background-messages), [Flutter background-isolate channels](https://docs.flutter.dev/platform-integration/platform-channels#using-plugins-and-channels-from-background-isolates), Android `FlutterEngine` plugin registration, and the Dart VM `vm:entry-point` pragma specification.
- Flutter test docs: widget tests and golden file tests.
- Repo configuration, existing router, theme, and localization patterns override generic advice.
