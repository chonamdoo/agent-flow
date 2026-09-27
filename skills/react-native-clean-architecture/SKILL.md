---
name: react-native-clean-architecture
description: React Native and Expo Clean Architecture adapter for the platform-neutral clean-architecture-core contract. Use for shared package layout, RN platform adapters, Context Provider composition root, optional TSyringe, native interop boundaries, repository/source/mapper boundaries, and RN architecture review.
requires:
  - clean-architecture-core
---

# React Native Clean Architecture

Load `clean-architecture-core` first **when this project's architecture selection is Clean**. This skill is a chosen application-layer contract, not a requirement of React Native's New Architecture (Fabric/JSI/TurboModules). It adds React Native package and platform-adapter details only; do not apply it to `local` or `pending` architecture selections.

## Package Boundaries

Discover the existing app/package boundaries, native source sets, dependency
declarations, and profile role mappings. Keep the project's layout rather than
creating a prescribed monorepo tree. Shared policy/contracts remain free of React
Native runtime imports; native implementations belong to platform adapters.

Apply the required core's **Semantic Layers** and **Dependency Rule** to shared
notifier/queue contracts and managed source-root coverage.

## DI Shape

- Assemble dependencies at the adopted app or framework composition owner. Prefer props for local dependencies; use Context for consumers that need a shared app-level capability. When using Context, provide typed consumer ports through that boundary; no particular factory function name or root filename is required.
- Put native modules, permissions, linking, storage, and device implementations at platform adapter edges **when domain/application policy consumes them**. Pass narrow application/domain capability ports to that policy and its state holder; those ports need not be called repositories. Framework router hooks and rendering details remain in the framework-owned presentation boundary.
- Optional TSyringe usage stays at app shell or adapter edge.
- Composition may construct raw clients/native adapters. Context values consumed
  by presentation expose typed ports, not those implementations.
- If TSyringe is used with Babel, configure TypeScript metadata support and
  import `reflect-metadata` once before DI use.

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

## Evidence and Project Contract

[RN testing](https://reactnative.dev/docs/testing-overview#writing-testable-code) recommends separating views from business logic for testability; [React Context](https://react.dev/learn/passing-data-deeply-with-context) describes a value-sharing mechanism and recommends considering props first. [RN Turbo Native Modules](https://reactnative.dev/docs/turbo-native-modules-introduction) describes typed native interfaces, not mandatory app ports. `clean-architecture-core` owns this project's dependency/mapping contract; repository/source/mapper and Context-based port injection are **Clean design choices**, not RN/Expo requirements. If adopting TSyringe, check its [README](https://github.com/microsoft/tsyringe/blob/master/README.md) for Babel metadata and `reflect-metadata` setup.
