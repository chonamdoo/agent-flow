---
name: react-native-clean-architecture
description: React Native and Expo Clean Architecture adapter for the platform-neutral clean-architecture-core contract. Use for shared package layout, RN platform adapters, Context Provider composition root, optional TSyringe, native bridge boundaries, repository/source/mapper boundaries, and RN architecture review.
requires:
  - clean-architecture-core
---

# React Native Clean Architecture

Load `clean-architecture-core` first. This skill adds React Native package and
platform-adapter details only.

## Package Boundaries

Discover the existing app/package boundaries, native source sets, dependency
declarations, and profile role mappings. Keep the project's layout rather than
creating a prescribed monorepo tree. Shared policy/contracts remain free of React
Native runtime imports; native implementations belong to platform adapters.

Map `shared-presentation-contract` to the existing neutral notifier/queue boundary
used by AppShell and feature presentation. It imports no UI, data, AppShell, or
feature implementation. No extra package is needed merely to satisfy a role name.
An unregistered package path is missing lint coverage, not a successful check.

## DI Shape

- Default to `createDependencies()` or `createContainer()` plus Context Provider
  at `App.tsx` or app shell.
- Put native modules, permissions, linking, storage, and device implementations at
  platform adapter edges. Pass narrow application/domain capability ports to use
  cases and presentation; those ports need not be called repositories.
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

## Evidence Basis

React Native React Fundamentals, React Native state docs, React Native Linking
docs, React createContext/useContext docs, TSyringe README.
