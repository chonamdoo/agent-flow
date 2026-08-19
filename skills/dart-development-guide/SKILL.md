---
name: dart-development-guide
description: Dart-specific implementation and review checklist. Use only when writing, modifying, or reviewing Dart files (`*.dart`) or the analyzer configuration. Apply as a secondary guide after repo patterns and task scope; do not use it to demand broad rewrites.
---

# Dart Development Guide

Use this only for Dart code in the changed scope. Do not score it. For widget, layout, and Flutter framework concerns use `flutter-development-guide`.

## Write

- Preserve the repo's existing style, package layout, and lint set in `analysis_options.yaml` first.
- Keep `dart format` output unchanged. The formatter is the style source of truth, so style opinions belong in `analysis_options.yaml`, not in review prose.
- Declare return types and parameter types on new public and cross-library members.
- Prefer `final` for locals and fields that are assigned once, and `const` for compile-time constants.
- Keep types non-nullable when a value always exists. Use `?` when absence is a real state, and handle it with pattern matching, `??`, or an early return.
- Reserve `late` for a field genuinely initialized before first read, and prefer constructor initialization when the value is available there.
- Use `switch` expressions and record/object patterns for branching over sealed types or shapes, and let exhaustiveness replace a fallback `default` branch.
- Mark a closed type set `sealed` so the compiler proves the switch exhaustive. `final` blocks subtyping outside the library but grants no exhaustiveness, so it is the modifier for a set that may still grow.
- Prefer a record for an unnamed multi-value return, and a named class once the shape gains behavior, validation, or a domain name.
- Return `Future` from async work and `Stream` from multi-event work; keep `void` async APIs out of code a caller must sequence.
- Await every `Future` a caller depends on. Mark a deliberate fire-and-forget call with `unawaited` so the analyzer stays quiet for a stated reason.
- Cancel `StreamSubscription`, `Timer`, and `StreamController` in the owning object's teardown.
- Catch a specific exception type at a boundary. Wrap a caught error with context and rethrow with `Error.throwWithStackTrace` or `rethrow` so the original stack survives.
- Keep `dynamic` out of new signatures. Use `Object?` plus a type check or pattern when the value is genuinely unknown.
- Keep `print` out of library code; use the repo's logging boundary.

## Test

- Add or update focused unit tests for new branches, edge cases, and bug regressions. Use `package:test` in a pure-Dart package and `package:flutter_test` once the package depends on the Flutter SDK.
- Assert on error type and message for failure paths, not only on success paths.
- Test stream and async APIs with explicit completion, not with `Future.delayed` sleeps.
- Prefer fakes over generated mocks for a single-method dependency. Generate mocks with the repo's existing mocking package when a class has real interaction to verify.

## Review

- Treat these as blocking only for real runtime bugs, data loss, security issues, analyzer errors, failing tests, or project-rule violations.
- Treat formatter-equivalent style differences as no findings at all.
- Treat lint-configurable preferences as suggestions unless `analysis_options.yaml` already enforces them.
- Check that awaited futures match caller expectations, subscriptions and controllers are cancelled, nullable values are handled at their boundary, caught errors preserve their stack, and tests cover the changed behavior.

## Sources

- Effective Dart: style, documentation, usage, and design guidelines.
- Dart language docs: null safety, patterns, records, class modifiers, and asynchrony.
- Existing repo configuration (`analysis_options.yaml`, `pubspec.yaml`, `dart format`) overrides generic advice.
