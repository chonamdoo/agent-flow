# React Native Operational Patterns

## Evidence Snapshot

- Analysis date: 2026-06-13.
- Evidence priority: first Hermes bytecode and bundle metadata, second declared dependency metadata, third decompiled native packages.
- Limitation: obfuscation can erase many native RN module names, so library identification should prefer JavaScript bundle evidence when available.
- Limitation: Java-side analysis may not expose Hermes/JSI native internals.

## Architecture Signals

- RN core: use the latest official stable React Native release that matches the project or Expo release channel. Do not use private React Native forks as a default.
- Engine: prefer official Hermes unless a measured blocker requires another JavaScript engine.
- Framework layer: use an app-owned shell/router layer for product modules, mini-apps, media wrappers, and cross-platform adapters.
- Navigation: `@react-navigation/native-stack`, bottom-tabs, drawer, and stack are standard RN routing substrates.
- Bundle metadata: signed deployment metadata should include bundle identity, version lane, and integrity verification.

## Operational Patterns

- RN injection should be centralized through native dependency boundaries and typed specs.
- Legacy and current RN bundle paths may coexist during staged migrations only when removal criteria are explicit.
- OTA flow should use temp download, pending activation, active bundle, and previous rollback copy.
- Bundle integrity should be verified with product/region-specific verification keys.
- Mini-app style hosting should sit behind a router/module contract, not leak into feature UI.

## Ecosystem Libraries

- Gesture/animation: `react-native-reanimated`, `react-native-gesture-handler`, `react-native-screens`.
- UI/native views: `react-native-svg`, `react-native-safe-area-context`, masked view, blur, pager view, Lottie.
- Lists: `@shopify/flash-list`.
- Storage: `react-native-mmkv`, AsyncStorage, clipboard.
- Web/parity: `react-native-webview`, `react-native-web`, URL polyfill.
- Data/state: Relay, GraphQL, SWR, Immer, RxJS.
- i18n: FormatJS/react-intl.
- Validation/math: Yup, Decimal.js.
- Observability: Sentry for React Native and React, or equivalent JS/native crash monitoring.

## Adoption Translation

- Copy the pattern, not a private dependency.
- Prefer official RN and Hermes first; fork only with platform ownership.
- Use signed OTA only with rollback and monitoring.
- Use MFE only when release ownership needs it.
- Keep React Web parity explicit through adapters and shared contracts.
