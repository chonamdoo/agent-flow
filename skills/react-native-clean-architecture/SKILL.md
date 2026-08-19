---
name: react-native-clean-architecture
description: React Native and Expo Clean Architecture adapter for the platform-neutral clean-architecture-core contract. Use for shared package layout, RN platform adapters, Context Provider composition root, optional TSyringe, native bridge boundaries, repository/source/mapper boundaries, and RN architecture review.
requires:
  - clean-architecture-core
---

# React Native Clean Architecture

Load `clean-architecture-core` first. This skill adds React Native package and
platform-adapter details only.

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
