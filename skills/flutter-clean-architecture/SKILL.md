---
name: flutter-clean-architecture
description: Flutter Clean Architecture adapter for the platform-neutral clean-architecture-core contract. Use for `lib/` layer layout, Flutter platform adapters, Riverpod `ProviderScope` composition root, optional get_it, platform channel boundaries, repository/source/mapper boundaries, and Flutter architecture review.
requires:
  - clean-architecture-core
---

# Flutter Clean Architecture

Load `clean-architecture-core` first. This skill adds Flutter layer layout,
platform-adapter, and sample-code details only.

## Package Shape

```text
lib/app/
lib/core/ui/
lib/core/design_system/
lib/core/resources/
lib/core/platform/
lib/core/permission/
lib/core/network/
lib/core/navigation/api/
lib/core/navigation/impl/
lib/core/domain/home/
lib/core/data/home/
lib/features/home/api/
lib/features/home/presentation/
```

A Flutter app is normally one Dart package, so the layer boundary is a directory
boundary plus an import rule rather than a build-graph boundary. Keep
`lib/core/domain/**` free of `package:flutter` imports; a domain file that needs
`Widget`, `BuildContext`, or `Color` has taken a UI concern. Promote a layer to
its own package under `packages/` only when a second app or package consumes it.

This list is closed once the repo adopts the contract. `lib/core` becomes a
managed root, so a directory beside these — `lib/core/utils`, `lib/core/di`,
`lib/features/<f>/domain` — fails the `architecture-lint` gate as unmapped.
Fold such code into the role that owns it. A `.agent-flow/profiles/<id>.local.yaml`
override replaces the whole `architecture.roles` list rather than appending to it,
so an override that declares only the new role turns every other layer check off
and leaves the required gate passing with nothing enforced; restate every shipped
role beside the new one.

## DI Shape

- Default to Riverpod: declare providers as top-level `final` variables and put
  `ProviderScope` at `runApp` as the composition root.
- Resolve dependencies through `ref`, not `BuildContext`. A state holder, a use
  case wiring provider, and a test all read the same graph without a widget tree.
- The graph is static, so a provider that does not exist is an analyze-time error
  on an undefined name instead of a runtime lookup failure.
- Flutter platform dependencies live in `lib/core/platform`. Plugins, permissions,
  secure storage, path lookups, and `MethodChannel` calls are platform adapters;
  pass their abstractions into use cases and presentation.
- Override platform and network adapters with `ProviderScope(overrides: ...)` for
  tests, flavors, and previews instead of branching inside the graph.
- Use `get_it` only when the repo already registers services there. Keep its
  registration at app startup and out of domain and presentation code.

## Generic Sample

```dart
final homeRemoteDataSourceProvider = Provider<HomeRemoteDataSource>((ref) {
  return HomeRemoteDataSourceImpl(ref.watch(httpClientProvider));
});

final homeRepositoryProvider = Provider<HomeRepository>((ref) {
  return HomeRepositoryImpl(ref.watch(homeRemoteDataSourceProvider));
});

final getHomeFeedProvider = Provider<GetHomeFeedUseCase>((ref) {
  return GetHomeFeedUseCase(ref.watch(homeRepositoryProvider));
});
```

```dart
void main() {
  runApp(
    ProviderScope(
      overrides: [
        platformAdaptersProvider.overrideWithValue(createFlutterPlatformAdapters()),
      ],
      child: const App(),
    ),
  );
}
```

## Review Additions

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
