# React Native Operational Patterns

## Evidence Snapshot

- These are supplied analysis notes, not a verified public case study. The original artifacts, analyst identity, access/reuse permission, and applicable product/library versions are not established here. Do not retrieve or redistribute private source material on the strength of this reference.
- The supplied notes record an analysis date of 2026-06-13; it is not an effective date for the guidance below.
- The notes report this evidence priority: Hermes bytecode and bundle metadata first, declared dependency metadata second, and decompiled native packages third. No artifact locator is supplied, so the claimed observations cannot be independently reproduced from this bundle.
- Limitation: obfuscation can erase many native RN module names, so library identification should prefer JavaScript bundle evidence when available.
- Limitation: Java-side analysis may not expose Hermes/JSI native internals.

## Architecture Signals

The following guidance is conditional author interpretation of the notes, not
proof that a particular app used these patterns or that a library is required.
Confirm actual project versions, ownership, and compatibility before adoption.

- RN core: prefer a supported official React Native release compatible with the project or Expo release channel. An unrelated task does not require an upgrade, and private forks are not a default.
- Engine: prefer official Hermes unless a measured blocker requires another JavaScript engine.
- Framework layer: keep shell/router responsibilities at the adopted app or framework composition owner, with module contracts and cross-platform adapters.
- Navigation: React Navigation navigators are candidates for plain RN. [Expo Router manages its root container](https://docs.expo.dev/router/migrate/from-react-navigation/#replace-the-navigationcontainer); preserve its installed version's public routing boundary rather than adding another container.
- Bundle metadata: signed deployment metadata should include bundle identity, version lane, and integrity verification.

## Operational Patterns

- RN injection should be centralized through native dependency boundaries and typed specs.
- Legacy and current RN bundle paths may coexist during staged migrations only when removal criteria are explicit.
- OTA flow should use temp download, pending activation, active bundle, and previous rollback copy.
- Verify bundle integrity with keys managed according to approved trust boundaries and release policy. Separate keys by product or region only when that policy actually requires it; the supplied notes do not establish a universal key topology.
- Mini-app style hosting should sit behind a router/module contract, not leak into feature UI.

## Ecosystem Libraries

The notes name the candidates below; their presence in the original application
is unverified. This is not an installation list or a preferred stack. Keep the
adopted library unless a required capability, measured bottleneck, or compatibility
constraint justifies a change.

- Gesture/animation: `react-native-reanimated`, `react-native-gesture-handler`, `react-native-screens`.
- UI/native views: `react-native-svg`, `react-native-safe-area-context`, masked view, blur, pager view, Lottie.
- Lists: `@shopify/flash-list`.
- Storage: `react-native-mmkv`, AsyncStorage, clipboard.
- Web/parity: `react-native-webview`, `react-native-web`, URL polyfill.
- Data/state: Relay, GraphQL, SWR, Immer, RxJS.
- i18n: FormatJS/react-intl when adopted or needed for shared catalog compatibility. Web locale loading and HTML language/direction belong to the web runtime; native locale, formatter, persistence, and lifecycle remain native responsibilities.
- Validation/math: Yup for compatible schema-validation needs; Decimal.js when exact decimal arithmetic is required. Monetary precision and rounding may also be satisfied by integer minor units; unrelated mathematics does not require a decimal library.
- Observability: Sentry for React Native and React, or equivalent JS/native crash monitoring. Browser RUM source maps are web artifacts, distinct from RN/Hermes maps, native symbols, Expo release artifacts, and OTA activation/rollback identity.

## Adoption Translation

- Copy the pattern, not a private dependency.
- Prefer official RN and Hermes first; fork only with platform ownership.
- Use signed OTA only with rollback and monitoring.
- Use MFE only when release ownership needs it.
- Keep React Web parity explicit through adapters and shared contracts.
