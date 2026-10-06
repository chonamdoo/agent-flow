---
name: architecture
description: Approved local architecture for the shop app. Use when implementing Dart models, repositories, list state, or notifiers under lib/features/.
---

# Shop app feature boundaries

The project selects this local contract for the orders implementation. It extends
the existing cart feature pattern; these ownership and wiring decisions are approved
before the green phase, rather than left to an author in pending mode.

- `lib/features/<feature>/` owns one feature as plain files: models,
  `<feature>_repository.dart`, `<feature>_list_state.dart`, and
  `<feature>_list_notifier.dart`, like `lib/features/cart/`.
- The repository is a concrete class over the shared `ApiClient`
  (`lib/team/api_client.dart`). It calls the endpoint and decodes records into the
  feature's models. Tests substitute the `ApiClient` transport, not the class.
- The list notifier owns screen state as a sealed state family and receives the
  repository through its constructor. It has no Flutter widget dependency.
- The app shell is the composition root: it builds the repository over `ApiClient`
  and passes it to the notifier.
- Shared infrastructure under `lib/team/` is used as is and not modified.
- Apply `skills/team-flutter-conventions/SKILL.md` to layout and error reporting.
  The selected contract adds no repository interface, Clean role map, or
  domain/data/presentation split.
