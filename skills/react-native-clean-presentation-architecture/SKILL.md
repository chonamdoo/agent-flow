---
name: react-native-clean-presentation-architecture
description: Use when creating, modifying, or reviewing a React Native Clean Architecture presentation layer with Context Provider DI, state-holder hooks, uiState modeling, UiModel mapping, navigation effects, and state-based presentation code review.
required_markers:
  - "presentation-skill: react-native"
  - "presentation-state-based-development: applied|n/a"
  - "presentation-state-review: pass|fail"
  - "ui-state-modeling: explicit"
  - "presentation-mapping-boundary: domain-to-uimodel|n/a"
  - "di-boundary: context-provider|tsyringe|existing|n/a"
---

# React Native Clean Presentation Architecture

Use this skill for React Native or Expo feature work where presentation code should follow a reusable Clean Architecture pattern.

## Evidence Basis

- React official Context docs provide the built-in provider mechanism React Native uses for passing app-level values through the tree.
- React Native native/platform APIs should stay behind adapters so presentation state holders depend on domain/application ports.
- React Native Turbo Native Modules docs define typed JavaScript specs for custom native platform APIs.
- React Native FlatList docs require stable keys and `extraData` when render output depends on external state.
- React Native SafeAreaView is deprecated in current docs; prefer `react-native-safe-area-context` for safe area handling.
- React Native has no official Hilt-equivalent DI container; external containers should be introduced only when project shape justifies them.
- npm downloads check on 2026-06-02 for 2026-05-02..2026-05-31: `tsyringe` 23,949,329; `inversify` 8,334,605; `typed-inject` 3,066,359; `awilix` 1,904,000; `typedi` 1,878,420.

## Architecture Rule

- `presentation` owns screens, components, state-holder hooks, UI events, presentation models, mappers, and navigation effects.
- `domain` owns entities, use cases, repository interfaces, and pure business rules.
- `data` or `infrastructure` implements repository interfaces and HTTP/storage/native-client adapters.
- Presentation code depends on domain use cases or ports, not concrete native modules, API clients, or repository implementations.
- Native platform APIs should be wrapped behind ports/adapters before reaching presentation state holders.
- Permissions, linking, storage, sensors, and native modules should be represented as application/domain ports before presentation uses them.

## DI Rule

React Native runs on React, so it does not have a Hilt-equivalent official DI framework. Use this priority:

1. Prefer explicit props for local dependencies.
2. Use React `Context` providers for app-level dependencies such as use cases, repositories, API clients, secure storage, permissions, analytics, feature flags, and configuration.
3. Use an external DI container only when the project already has class-heavy domain/application services or an existing container.
4. If introducing a TypeScript DI container is justified, prefer the current repo standard. If none exists, `tsyringe` is the default candidate because current npm usage is higher than common alternatives.

Provider rules:
- create providers near composition roots such as `App`, navigation root, or feature boundaries
- create context objects outside components
- expose typed hooks such as `useSearchDependencies()`
- throw a clear error when a required provider is missing
- keep provider values stable only when identity churn causes real rerender risk
- do not put screen-local state into app dependency providers

## Package Shape

Feature presentation packages should stay screen-oriented:
- `features/<feature>/presentation/<screen>/<Screen>.tsx`
- `features/<feature>/presentation/<screen>/use<Screen>ViewModel.ts`
- `features/<feature>/presentation/<screen>/model/<Screen>UiState.ts`
- `features/<feature>/presentation/<screen>/model/<Screen>UiEvent.ts`
- `features/<feature>/presentation/<screen>/model/<Screen>UiModel.ts`
- `features/<feature>/presentation/<screen>/mapper/*Mapper.ts`
- `features/<feature>/presentation/<screen>/components/*`

Presentation mappers must convert domain data into presentation models before state reaches React Native components.
Presentation model types must use the `UiModel` postfix, for example `<Screen>ItemUiModel`.

## State Holder Rule

Use a custom hook as the screen state holder:
- name it `use<Screen>ViewModel`
- inject use cases/dependencies through props, parameters, or dependency hooks
- expose `uiState` as an explicit discriminated union
- expose user actions as named callbacks
- keep async orchestration, pagination, refresh, retry, and navigation-effect decisions inside the hook
- keep rendering inside components
- do not force a single `Action` reducer shape unless the repo already uses reducer/action patterns

State patterns:
- model not-ready, loading, refreshing, empty, error, success, offline, and permission states explicitly when they can occur
- do not use fake domain sentinel values as initial UI state
- keep request ids, abort controllers, pagination cursors, selected ids, and optimistic update state private in the state holder
- preserve cancellation with `AbortController` or the project’s existing request cancellation pattern
- keep keyboard, scroll, focus, and animation state local unless it drives business work

Event patterns:
- treat navigation, toast, snackbar, haptic feedback, permission prompts, and imperative focus as one-shot effects
- treat deep links, external app links, and native permission prompts as external effects, not durable render state
- do not store fire-once effects as durable `uiState`
- prefer callback outputs from the state holder or a narrow event queue only when the screen truly needs one-shot effects

## Component Rule

Split state-holder wiring from rendering:
- navigation/screen container obtains dependencies and calls `use<Screen>ViewModel`
- screen component receives plain `uiState` and callbacks
- child components receive only the data/callbacks they need
- presentational components should not import use cases, repositories, API clients, native modules, or DI containers
- list components should use stable domain ids as keys
- pass `extraData` or another explicit prop when `FlatList` item rendering depends on state outside `data`
- use `react-native-safe-area-context` or the repo's existing safe-area boundary instead of deprecated core `SafeAreaView`
- platform-specific UI branches should stay in presentation, while platform API calls stay behind adapters

## Review Checklist

- dependency flow uses props or `Context` providers; external DI is justified or already present
- native APIs are wrapped before reaching the state holder
- permissions, linking, storage, sensors, and custom native modules are accessed through ports/adapters
- `uiState` is explicit and covers not-ready, loading, refreshing, empty, error, success, offline, and permission states that can occur
- domain data is mapped to `UiModel` before rendering
- `UiModel` postfix is used for presentation models
- state-holder hook owns async orchestration and exposes callbacks
- components stay render-focused and receive plain props
- `FlatList` keys and external render dependencies are explicit
- safe-area handling does not use deprecated core `SafeAreaView` for new code
- one-shot effects are not modeled as durable UI state
- review output includes the required markers below

## Required Markers

When this skill is used for presentation development or code review, include these markers in the completion artifact or review output:

- `presentation-skill: react-native`
- `presentation-state-based-development: applied|n/a`
- `presentation-state-review: pass|fail`
- `ui-state-modeling: explicit`
- `presentation-mapping-boundary: domain-to-uimodel|n/a`
- `di-boundary: context-provider|tsyringe|existing|n/a`

## Sources

- React `createContext`: https://react.dev/reference/react/createContext
- React `useContext`: https://react.dev/reference/react/useContext
- React `useEffect`: https://react.dev/reference/react/useEffect
- React Native Turbo Native Modules: https://reactnative.dev/docs/turbo-native-modules-introduction
- React Native PermissionsAndroid: https://reactnative.dev/docs/permissionsandroid
- React Native Linking: https://reactnative.dev/docs/linking
- React Native FlatList: https://reactnative.dev/docs/flatlist
- React Native SafeAreaView deprecation: https://reactnative.dev/docs/safeareaview
- tsyringe: https://github.com/microsoft/tsyringe
- npm downloads API: https://api.npmjs.org/downloads/point/last-month/tsyringe
- npm downloads API for inversify: https://api.npmjs.org/downloads/point/last-month/inversify
- npm downloads API for typed-inject: https://api.npmjs.org/downloads/point/last-month/typed-inject
- npm downloads API for awilix: https://api.npmjs.org/downloads/point/last-month/awilix
- npm downloads API for typedi: https://api.npmjs.org/downloads/point/last-month/typedi
