---
name: react-native-operational-adoption
description: "Supplemental React Native and Expo development skill. Use alongside react-native-development-guide when writing, modifying, or reviewing RN app code that touches architecture, app shell, navigation, Hermes/RN upgrade strategy, New Architecture boundaries, native modules, micro-frontend routing, signed OTA bundles with rollback, legacy/new RN coexistence, FlashList, MMKV, Reanimated, Relay/SWR, react-native-web parity, or mobile observability. Do not use for React Web-only work, generic React component styling, generic TypeScript issues, or reverse-engineering a specific third-party app itself."
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [hermes, new architecture, turbo module, native module, ota, over-the-air, flashlist, mmkv, reanimated, react-native-web, micro-frontend, observability, rn upgrade, app shell, navigation architecture]
pathGlobs: ["**/metro.config.*", "**/react-native.config.*", "**/*.podspec", "**/ios/Podfile", "**/android/settings.gradle", "**/android/settings.gradle.kts"]
---

# React Native Operational Adoption

## Use With

- Always pair with `react-native-development-guide` for React Native or Expo implementation.
- Apply the selected architecture contract when changing presentation boundaries (`react-native-clean-presentation-architecture` in Clean mode).
- Pair with `react-development-guide` only when the RN task also changes shared React Web code.
- Android/Kotlin skills when adding native Android integration.
- Use `react-runtime-i18n` only when the task changes an actual React Web locale runtime. RN retains native locale, formatter, storage, and lifecycle adapters; shared message meaning does not require DOM or SSR behavior on native.
- Use `webview-json-rpc-bridge` only for an actual JSON-RPC transport between a WebView document and host. It does not replace TurboModule/Codegen/JSI boundaries, typed native capabilities, or native permission/lifecycle ownership.
- Use `datadog-rum-sourcemaps` only for a web bundle that actually uses Datadog browser RUM. RN/Hermes maps, native symbols, Expo release paths, and OTA bundle identity remain mobile responsibilities; browser upload success is not native symbolication evidence.

## Source Basis

Read [references/react-native-operational-patterns.md](references/react-native-operational-patterns.md) when the task asks why these patterns exist or asks for React Native operational adoption rationale.

The reference records supplied analysis notes whose original artifacts, reuse permission, and applicable versions remain unverified. Treat its observations as leads, not proof or a dependency recipe; its adoption guidance is conditional author interpretation. Prefer supported, compatible official React Native and Hermes releases. Do not copy private framework names, private Maven/npm scopes, or vendor-forked React Native builds.

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
   - Prefer supported official React/RN releases compatible with the adopted project or Expo release channel over forks; this is not an instruction to upgrade RN for unrelated work.
   - For RN, keep Hermes enabled unless a measured blocker exists.
   - If native modules are involved, prefer New Architecture-compatible libraries and Codegen/TurboModule boundaries.

2. App shell and routing.
   - Preserve the adopted router and verify its installed version's public API. Plain React Navigation may own its container; Expo and other framework-managed routers own theirs.
   - Keep module and mini-app routing behind public route contracts. Use framework-owned auth/recovery mechanisms where applicable instead of adding another navigation container or reaching through router internals.
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
   - Keep the adopted libraries unless a concrete capability gap warrants a change; compare platform/toolchain compatibility and measured behavior before adding a candidate below.
   - Navigation: use the adopted router's supported navigator APIs; `@react-navigation/native-stack`, bottom-tabs, drawer, and stack are candidates for plain React Navigation, not mandatory imports for framework-managed routers.
   - Gestures/animation: consider `react-native-reanimated`, `react-native-gesture-handler`, or `react-native-screens` when the required interaction and supported runtime justify them.
   - Lists: consider `@shopify/flash-list` for large mobile lists after measuring current `FlatList` issues and checking migration compatibility.
   - Storage: consider `react-native-mmkv` for a measured local key-value performance need; preserve the existing storage contract and do not store secrets without platform security review.
   - Data: keep the adopted server-state path. Relay/GraphQL is a candidate for schema-driven requirements and SWR for compatible REST/lightweight fetching; select by caching, offline, and runtime needs rather than imposing either.
   - State transforms: use `immer` only where immutable updates are complex enough to justify it.
   - i18n: preserve the adopted catalog and formatter contract. FormatJS/react-intl is a candidate when already adopted or needed for shared React/RN compatibility, not a required catalog format.
   - Money: choose a representation that satisfies required currency precision, rounding, and exactness, such as integer minor units or decimal arithmetic. This does not prohibit binary floating point for unrelated mathematics.
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
