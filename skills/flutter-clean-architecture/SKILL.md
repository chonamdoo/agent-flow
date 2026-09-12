---
name: flutter-clean-architecture
description: Flutter Clean Architecture adapter for clean-architecture-core. Use for adopted Dart package boundaries, Flutter platform adapters, Riverpod ProviderScope composition, optional get_it, platform channels, repository/source/mapper boundaries, and architecture review.
requires:
  - clean-architecture-core
---

# Flutter Clean Architecture

Load `clean-architecture-core` first. This skill adds Flutter layer layout and
platform-adapter details only.

## Package Boundaries

Discover the existing Dart packages, import boundaries, dependency graph, and
active profile role mappings. Many Flutter apps enforce layers with imports
inside one package; create another package only for an actual consumer or
independent boundary. A role does not prescribe a folder or package count.

Pure domain policy stays free of Flutter types such as `Widget`, `BuildContext`,
and `Color`. Shared notifier/queue interfaces use `shared-presentation-contract`;
AppShell and feature presentation may consume it, but it imports no UI/data or
AppShell/feature implementation.

Map all adopted source roots before claiming lint coverage. An architecture
`roles` override replaces the list rather than appending: retain every intended
role and its checks when adding one. Do not turn a required gate into a pass with
unmapped or inactive boundaries.

## DI Shape

- Identify the adopted DI path first; preserve constructor composition or an
  existing graph. Consider Riverpod only when its graph, lifetime, and override
  model fit the project and its toolchain.
- When Riverpod is adopted, declare providers as top-level `final` variables and
  put the root `ProviderScope` at the app composition boundary.
- In that graph, resolve dependencies through `ref`, not `BuildContext`, so
  state holders, wiring providers, and tests use the same dependency graph.
- A reference to an undeclared top-level provider is an analyzer error; this
  does not prove every runtime dependency or override is configured correctly.
- Plugins, permissions, secure storage, path lookups, and `MethodChannel`
  implementations belong to platform adapters. Pass consumer-focused
  domain/application ports to use cases and presentation; a capability is not
  required to masquerade as a repository.
- With Riverpod, override platform and network adapters through
  `ProviderScope(overrides: ...)` for tests, flavors, and previews instead of
  branching inside the graph.
- Composition may construct clients. A presentation consumer reads a typed
  action/port, not the raw HTTP client, plugin, or data implementation.
- Use `get_it` only when the repo already registers services there. Keep its
  registration at app startup and out of domain and presentation code.

## Review Additions

Apply provider-specific criteria only to an adopted Riverpod path. For another
DI path, explain the applicability boundary and use the active workflow's
supported marker values; do not introduce Riverpod just to satisfy a marker or
invent a new enum value.

```text
flutter-clean-architecture: applied
lib-layer-import-boundary: pass|fail
flutter-platform-adapter-boundary: pass|fail
provider-scope-composition-root: pass|fail
get-it-optional-startup-only: pass|fail|n/a
repository-impl-direct-http-client: pass|fail
remote-data-source-boundary: pass|fail
platform-channel-edge-only: pass|fail|n/a
```

## Evidence Basis

Flutter app architecture guide, Flutter dependency injection guidance, Riverpod
provider and `ProviderScope` docs, get_it README, Dart language import semantics.
