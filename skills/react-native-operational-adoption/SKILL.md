---
name: react-native-operational-adoption
description: "Supplemental React Native and Expo development skill. Use alongside react-native-development-guide when writing, modifying, or reviewing RN app code that touches architecture, app shell, navigation, Hermes/RN upgrade strategy, New Architecture boundaries, native modules, micro-frontend routing, signed OTA bundles with rollback, legacy/new RN coexistence, FlashList, MMKV, Reanimated, Relay/SWR, react-native-web parity, or mobile observability. Do not use for React Web-only work, generic React component styling, generic TypeScript issues, or reverse-engineering a specific third-party app itself."
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [hermes, new architecture, turbo module, native module, ota, over-the-air, flashlist, mmkv, reanimated, react-native-web, micro-frontend, observability, rn upgrade, 앱 셸, 내비게이션 구조]
pathGlobs: ["**/metro.config.*", "**/react-native.config.*", "**/*.podspec", "**/ios/Podfile", "**/android/settings.gradle", "**/android/settings.gradle.kts"]
---

# React Native Operational Adoption

## Use With

- Always pair with `react-native-development-guide` for React Native or Expo implementation.
- Pair with `react-native-clean-presentation-architecture` when changing presentation boundaries.
- Pair with `react-development-guide` only when the RN task also changes shared React Web code.
- Android/Kotlin skills when adding native Android integration.

## Source Basis

Read [references/react-native-operational-patterns.md](references/react-native-operational-patterns.md) when the task asks why these patterns exist or asks for React Native operational adoption rationale.

Treat the source as an architecture signal, not a dependency recipe. Prefer official React Native and Hermes releases. Do not copy private framework names, private Maven/npm scopes, or vendor-forked React Native builds.

## Before Starting

Confirm this is an RN development task:

- React Web only: stop using this skill.
- React Native only: focus on Hermes, New Architecture, navigation, native modules, bundle/update policy, list/storage/performance libraries, and mobile verification.
- Shared React + RN: keep RN as the driver and define which packages are universal, web-only, native-only, and platform-adapter code.

Confirm current project facts before proposing changes:

- React, React Native, Expo, Hermes, and New Architecture status.
- App store/update policy constraints.
- Current navigation, data fetching, storage, logging, crash reporting, and release pipeline.
- Whether the app needs OTA at all. OTA adds security and rollback duties.

## Adoption Order

1. Baseline runtime.
   - Prefer official React/RN releases over forks.
   - For RN, keep Hermes enabled unless a measured blocker exists.
   - If native modules are involved, prefer New Architecture-compatible libraries and Codegen/TurboModule boundaries.

2. App shell and routing.
   - Use `react-navigation` as the native routing substrate for RN.
   - Put file-based routing, product modules, or mini-app routing above `react-navigation`; do not leak router internals into feature UI.
   - For React Web, mirror the same route ownership with framework routing or package boundaries, not RN-specific APIs.

3. Module and micro-frontend boundary.
   - Use MFE only when teams/products need independent release ownership.
   - Define module contract: route entry, permissions, analytics identity, feature flags, shared UI/design tokens, API clients, and rollback owner.
   - Keep cross-module imports through public entry points.

4. Bundle/update strategy.
   - If OTA is required, require signed metadata, staged activation, and rollback.
   - Model states explicitly: downloaded temp, pending activation, active, previous rollback copy.
   - Never ship unsigned JS bundles or unverifiable remote code.
   - Keep version lanes for legacy/new RN only during migrations. Add removal criteria.

5. Runtime library choices.
   - Navigation: `@react-navigation/native-stack`, `bottom-tabs`, `drawer`, `stack`.
   - Gestures/animation: `react-native-reanimated`, `react-native-gesture-handler`, `react-native-screens`.
   - Lists: prefer `@shopify/flash-list` for large mobile lists after measuring current `FlatList` issues.
   - Storage: use `react-native-mmkv` for fast local key-value state; do not store secrets without platform security review.
   - Data: choose one primary server-state path, usually Relay/GraphQL for schema-driven apps or SWR for REST/lightweight fetches.
   - State transforms: use `immer` only where immutable updates are complex enough to justify it.
   - i18n: use FormatJS/react-intl style message catalogs for shared React/RN copy.
   - Money/math: use decimal arithmetic, not binary floating point.
   - Observability: wire Sentry or equivalent for JS and native crash context before rollout.

6. Web parity.
   - Use `react-native-web` only when shared component economics are real.
   - Keep platform adapters for navigation, storage, files/media, permissions, and native-only modules.
   - Do not force mobile interaction patterns into web UI.

## Review Checklist

- Does the proposal avoid private/vendor dependencies and RN forks unless explicitly justified?
- Are React Web and RN responsibilities separated where platform behavior differs?
- Is OTA either out of scope or covered by signature verification, staged activation, rollback, and monitoring?
- Is legacy/new RN coexistence temporary, observable, and tied to removal criteria?
- Are native modules New Architecture-compatible or isolated behind adapters?
- Are navigation and module boundaries clear enough for independent feature ownership?
- Are performance libraries added because of measured bottlenecks, not trend matching?
- Are release, crash, and rollback signals included in verification?

## Avoid

- Replacing the app architecture just to mimic a third-party app.
- Introducing MFE for a single-team app with one release train.
- Adding OTA without signing and rollback.
- Treating `react-native-web` as free code sharing.
- Mixing product module internals through deep imports.
- Adding native modules without platform ownership and CI coverage.
