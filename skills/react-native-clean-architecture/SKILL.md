---
name: react-native-clean-architecture
description: React Native and Expo platform adapter for `clean-architecture-core` that maps the core contract to shared packages, RN platform/native bridge adapters, Context Provider composition roots, optional TSyringe edge use, and repository/source/mapper boundaries. Use when applying Clean Architecture across RN packages or reviewing native bridge/data/DI boundaries; pair with react-native-clean-presentation-architecture for navigation effects, screen state holders, UiState, or presentation-only work.
---

# React Native Clean Architecture

This is not a standalone Clean Architecture guide. Load [`clean-architecture-core`](../clean-architecture-core/SKILL.md) first; this skill adds React Native package, platform-adapter, native-bridge, DI-edge, and sample-code details only.

## Quick start

1. Apply the layer, dependency-direction, and review rules from `clean-architecture-core`.
2. Use this adapter only to translate those rules into React Native shared packages, Context Provider composition roots, native bridge/platform adapters, and optional TSyringe edge use.
3. If the task is only navigation effects, screen state holders, UiState, or presentation mapping, pair with `react-native-clean-presentation-architecture` before applying presentation details.

## Package Shape

```text
apps/mobile-rn/
packages/core-ui-rn/
packages/core-design-system-rn/
packages/core-resources/
packages/core-platform-rn/
packages/core-network/
packages/core-navigation-api/
packages/core-navigation-rn/
packages/core-domain-home/
packages/core-data-home/
packages/feature-home-api/
packages/feature-home-presentation-rn/
```

React Native shares the React semantic DI shape. Shared packages stay free of RN
runtime imports unless they are explicitly RN platform or presentation packages.

## DI Shape

- Default to `createDependencies()` or `createContainer()` plus Context Provider
  at `App.tsx` or app shell.
- RN platform dependencies live in `core-platform-rn`.
- Native modules, permissions, linking, storage, and device APIs are platform
  adapters; pass abstractions into use cases/presentation.
- Optional TSyringe usage stays at app shell or adapter edge.
- If TSyringe is used with Babel, configure TypeScript metadata support and
  import `reflect-metadata` once before DI use.

## Generic Sample

```ts
export function createDependencies(platform: PlatformAdapters) {
  const homeApiClient = new HomeApiClient(platform.fetchClient)
  const homeRemoteDataSource = new HomeRemoteDataSourceImpl(homeApiClient)
  const homeRepository = new HomeRepositoryImpl(homeRemoteDataSource)

  return {
    getHomeFeed: new GetHomeFeedUseCase(homeRepository),
  }
}
```

```tsx
export function AppShell() {
  const dependencies = useMemo(
    () => createDependencies(createReactNativePlatformAdapters()),
    [],
  )

  return (
    <DependenciesProvider value={dependencies}>
      <RootNavigator />
    </DependenciesProvider>
  )
}
```

## Review Additions

```text
react-native-clean-architecture: applied
shared-package-runtime-boundary: pass|fail
rn-platform-adapter-boundary: pass|fail
context-provider-composition-root: pass|fail
tsyringe-optional-edge-only: pass|fail|n/a
repository-impl-direct-api-client: pass|fail
remote-data-source-boundary: pass|fail
native-module-edge-only: pass|fail|n/a
```

## Evidence Basis

React Native React Fundamentals, React Native state docs, React Native Linking
docs, React createContext/useContext docs, TSyringe README.
