# Toss RN Analysis Summary

Source: `/Users/namdoo/Downloads/토스_RN_분석.md`

## Evidence Snapshot

- Target APK: `viva.republica.toss` 5.263.1.
- Analysis date: 2026-06-13.
- Evidence priority: first `assets/shared.hbc`, second `assets/aboutlibraries.json`, third decompiled packages.
- Limitation: DexGuard erased many native RN module names, so library identification should prefer the JS bundle evidence.
- Limitation: base APK had no native `.so`; Hermes/JSI native internals were outside the Java-side analysis.

## Confirmed Architecture Signals

- RN core: `com.facebook.react:react-android` `0.84.0-toss.3`, a Toss private fork.
- Engine: Hermes bytecode via `shared.hbc`.
- Framework layer: Granite, represented by `im.toss.react:framework` and `@granite-js/*`.
- Granite signal: micro-frontend/product-module routing plus image, Lottie, and video wrappers.
- Navigation: `@react-navigation/native-stack`, `bottom-tabs`, `drawer`, and `stack`.
- Bundle metadata: `shared.hbc.meta.json` includes deployment id and RSA-style signature metadata.

## Operational Patterns

- RN injection appears centralized through native DI modules and typed specs.
- RN 0.72 and RN 0.84 bundle paths coexist, indicating staged migration.
- OTA flow uses temp download, pending activation, active bundle, and old rollback copy.
- Bundle integrity is verified by product/region-specific verification keys.
- Mini-app style hosting exists through a router/module layer.

## Ecosystem Libraries

- Gesture/animation: `react-native-reanimated`, `react-native-gesture-handler`, `react-native-screens`.
- UI/native views: `react-native-svg`, `react-native-safe-area-context`, masked view, blur, pager view, Lottie.
- Lists: `@shopify/flash-list`.
- Storage: `react-native-mmkv`, AsyncStorage, clipboard.
- Web/parity: `react-native-webview`, `react-native-web`, URL polyfill.
- Data/state: Relay, GraphQL, SWR, Immer, RxJS.
- i18n: FormatJS/react-intl.
- Validation/math: Yup, Decimal.js.
- Observability: Sentry for React Native and React.

## Adoption Translation

- Copy the pattern, not the private dependency.
- Prefer official RN and Hermes first; fork only with platform ownership.
- Use signed OTA only with rollback and monitoring.
- Use MFE only when release ownership needs it.
- Keep React Web parity explicit through adapters and shared contracts.
