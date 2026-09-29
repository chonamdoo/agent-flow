---
name: team-flutter-conventions
description: Shop team conventions for Flutter feature code under lib/features — repositories, list state notifiers, DTO decoding, and error handling. Use when writing or reviewing any Dart file in lib/.
pathGlobs: ["**/*.dart"]
---

# Shop Team Flutter Conventions

These are team rules. They apply on top of whatever architecture the project selected.

## Layout

- Feature code lives in `lib/features/<feature>/` as plain files named
  `<feature>_repository.dart`, `<feature>_list_notifier.dart`, `<feature>_list_state.dart`, plus
  model files. Shared team infrastructure lives in `lib/team/` and is never modified by features.
- Repositories are concrete classes over the shared `ApiClient` (`lib/team/api_client.dart`).
  Read-only list notifiers may also call `ApiClient` and decode DTOs directly; a separate
  domain/data layer split is not required for read-only screens.

## Error reporting (mandatory)

- Every `catch` clause, `catchError` callback, and `onError` handler in `lib/features/**` must call
  `reportTeamError('<feature>', error)` from `lib/team/telemetry.dart` before mapping the failure to
  UI state. `<feature>` is the feature folder name as a string literal (for `lib/features/orders/`
  the call is `reportTeamError('orders', error)`).
- A catch that only maps to an error state without this call fails team review.
