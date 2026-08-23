---
name: react-native-development-guide
description: React Native and Expo implementation and review checklist. Use only when writing, modifying, or reviewing React Native app code, Expo/RN TSX, navigation, permissions, native bridge boundaries, platform-specific UI, FlatList/ScrollView performance, keyboard/safe-area behavior, accessibility, lifecycle, offline, or mobile smoke flows. Do not use for Kotlin/Android native internals, iOS native internals, TypeScript generalities, or broad rewrites.
---

# React Native Development Guide

Use this as a secondary checklist after user request, repo instructions, existing repo patterns, and `code-generation-discipline`. Do not score it. Do not use best-practice generalities to force broad rewrites.

## Scope

- Include React Native app code, Expo/RN TSX, navigation, permissions, native bridge boundaries, platform-specific UI, FlatList/ScrollView, keyboard, safe area, accessibility, lifecycle, offline, and smoke flows.
- Exclude Kotlin/Android native implementation details, iOS native implementation details, and TypeScript language generalities.
- If native Android/Kotlin/Compose/KMP code changes, first apply the Android profile's required review skills and the Android skills the phase prompt lists for the change.

## Write

- Keep navigation params stable and compatible with existing route types/patterns.
- Isolate `Platform.OS` differences near the platform boundary. Do not scatter platform branches through unrelated UI.
- Preserve permission request, denied, limited, and granted flows when touching camera, media, location, notifications, Bluetooth, or contacts.
- Account for app lifecycle when background/foreground affects subscriptions, timers, sensors, sockets, or refresh.
- Preserve network loading, offline, timeout, retry, and error states when touching mobile data flows.
- For `FlatList`, use stable `keyExtractor`, keep `renderItem` dependencies explicit, and pass `extraData` when item rendering depends on external state.
- Preserve image loading/error placeholders when changing remote images.
- Keep touch targets reachable and add useful `accessibilityLabel`/state for interactive controls.
- Handle keyboard and safe area behavior when editing forms, bottom sheets, modals, or full-screen layouts.
- Keep native module/bridge calls behind the existing boundary. Do not drift into native implementation details.

## Test

- Run typecheck/lint/tests for changed RN/Expo code when available.
- Smoke test the changed screen on at least the relevant platform path when behavior is UI, permission, navigation, or lifecycle-related.
- Verify permission denied/granted, offline/error, loading, and empty states when the changed flow touches them.
- For lists, verify item updates, reordering/filtering, and scroll performance risk on realistic data when feasible.

## Review

- Blocking only: crash risk, broken permission flow, navigation param mismatch, platform-specific regression, list performance cliff, accessibility/touch target regression, offline/error state that breaks a user flow, test/type/lint failure, or project-rule violation.

## Sources

- React Native docs: accessibility and FlatList behavior.
- Expo docs: permission configuration and platform permission messages.
- Repo configuration and existing mobile patterns override generic advice.
