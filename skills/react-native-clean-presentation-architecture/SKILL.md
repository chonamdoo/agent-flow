---
name: react-native-clean-presentation-architecture
description: Use when a project selects Clean Architecture for React Native presentation. Apply Context Provider DI and state-holder hooks where warranted by shared dependencies or coupled screen states; review uiState, UiModel mapping, navigation effects, and state-based behavior without imposing them on simple local-state screens.
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [uistate, ui state, state holder, screen state, navigation effect, presentation layer]
pathGlobs: ["**/*UiState.ts", "**/*UiState.tsx", "**/presentation/**"]
requires: [clean-architecture-core, react-native-clean-architecture]
---

# React Native Clean Presentation Architecture

Use this skill only when the project selected the Clean contract for React Native feature presentation. This is a project convention, not a requirement of RN New Architecture or Expo Router.

For AppShell-owned global error hosts, queue acknowledgement, or root navigation reset, use `react-native-app-shell-error-handling` instead.

## Evidence Basis

- React official Context docs provide the built-in provider mechanism React Native uses for passing app-level values through the tree.
- React official Effects docs define effects as synchronization with external systems; derived render state should not move into effects.
- React official state structure docs recommend grouping related state, avoiding contradictions, and avoiding redundant or duplicated state.
- React official reducer docs recommend reducer actions that describe one user interaction and pure reducer functions for complex state transitions.
- React Native state docs define props as parent-owned fixed data and state as data that changes over time; React Native state follows React state semantics.
- React Native networking docs say to catch errors thrown by `fetch`; HTTP error status handling is a separate app concern.
- React Native Turbo Native Modules docs describe typed JavaScript specs for custom native platform APIs, not a required app-level port.
- React Native FlatList docs recommend `extraData` when item rendering depends on state outside `data`; stable domain keys avoid identity changes.
- React Native SafeAreaView is deprecated in current docs; prefer `react-native-safe-area-context` for safe area handling.
- React Native has no official Hilt-equivalent DI container; a container is a project choice, not a prerequisite for testing.

The ports/adapters, state-holder, and UI mapping rules below implement the **selected Clean contract**, not React Native or Expo mandates. See [RN testability guidance](https://reactnative.dev/docs/testing-overview#writing-testable-code), [React state structure](https://react.dev/learn/choosing-the-state-structure), [RN network](https://reactnative.dev/docs/network), [RN Turbo Native Modules](https://reactnative.dev/docs/turbo-native-modules-introduction), and [FlatList](https://reactnative.dev/docs/flatlist).

## Architecture Rule

Apply the required `clean-architecture-core` **Semantic Layers**, **Dependency
Rule**, **Mapping Boundary**, and **Error Boundary**. Apply the required
`react-native-clean-architecture` for platform composition and DI.

- Represent permissions, linking, storage, sensors, and native modules as application/domain ports when domain/application policy consumes them. Framework routing and view-only behavior stay with presentation.

## Data and Error Boundary

- Fetch, HTTP clients, secure storage, AsyncStorage, native modules, permissions,
  and platform SDK details stay in `data`, `infrastructure`, or native adapters.
- Effects synchronize with native/external systems only; do not move
  domain-to-UI derivation or error mapping into `useEffect`.

Apply the required core's **Error Boundary** and **Mapping Boundary** for
normalization and the values supplied to rendering.

## DI Rule

React Native runs on React, so it does not have a Hilt-equivalent official DI framework. Use this priority:

1. Prefer explicit props for local dependencies.
2. Apply the required React Native adapter's **DI Shape** for app-level provider
   composition and typed port exposure.
3. Use an external DI container only when the project already has class-heavy domain/application services or an existing container.
4. If a TypeScript DI container is justified, prefer the repo's adopted tool. For a new choice, compare required lifetime and resolution behavior, RN/Expo compiler and runtime compatibility, metadata support, and test/preview substitution. `tsyringe` is one conditional candidate, not a popularity-based default.

Provider rules:
- create providers at the adopted app, framework navigation, or feature composition boundary
- create context objects outside components
- expose typed dependency access through hooks following the project's naming convention
- throw a clear error when a required provider is missing
- keep provider values stable only when identity churn causes real rerender risk
- do not put screen-local state into app dependency providers

## Presentation Boundaries

Keep screen wiring, state ownership, UI values, mapping, and components in the
project's adopted presentation boundary. Discover actual packages and role
configuration instead of creating a prescribed tree or one file per concept.

Apply the required core's **Mapping Boundary** to presentation projections.
Use the project's naming convention: `UiModel` describes a role, not a suffix
whose absence alone blocks approval.

## State Holder Rule

Under this contract, extract a custom screen state-holder hook only when a screen needs coordinated async work or multiple coupled transitions; a simple screen needs no extra hook or UiModel. Local state, server-cache, and route ownership follow `react-native-development-guide`. When extracting one:
- follow the project's state-holder hook naming convention
- inject use cases/dependencies through props, parameters, or dependency hooks
- expose an explicit discriminated `uiState` when distinct durable states can contradict one another
- expose user actions as named callbacks
- keep async orchestration, pagination, refresh, and retry in the hook when present
- keep rendering inside components
- do not force a single `Action` reducer shape unless the repo already uses reducer/action patterns

State patterns:
- model `not-ready`, `loading`, `refreshing`, `placeholder`, `empty`, `error`, `success`, `offline`, and `permission-required` states explicitly when they can occur
- define a discriminated `UiState`, normally by `status` or `type`, for mutually exclusive states; do not create a union for a single independent local value
- do not use fake domain sentinel values as initial UI state
- keep request handles and rollback bookkeeping private; expose selection and
  optimistic results through observable state when the UI renders them
- preserve cancellation with `AbortController`, an Effect cleanup ignore flag, or the project's existing request cancellation pattern
- keep keyboard, scroll, focus, and animation state local unless it drives business work

`UiState`, `UiAction`, and `UiEvent` are **project vocabulary**, not official React roles:
- `UiState` is durable render data. When a state holder is needed, it must be enough to redraw the screen from props/state after navigation focus changes.
- `UiAction` is user or UI input. Use a discriminated union when the screen uses a reducer or has branchy behavior; otherwise named callbacks are sufficient.
- `UiEvent` is an optional transient screen-container effect such as navigation, toast, haptic feedback, permission prompt, deep link, focus, or analytics trigger. Ordinary user-initiated navigation can happen in the router's event handler; do not add a queue solely to represent it.

Reducer/action patterns:
- use `useReducer` when state transitions are complex, coupled, or bug-prone
- keep reducers pure and free of requests, timers, navigation, native module calls, storage, and other side effects
- model each action as one user interaction or external result, not as many field-level patches when one semantic action exists

Event patterns:
- treat navigation, toast, snackbar, haptic feedback, permission prompts, and imperative focus as one-shot effects
- treat deep links, external app links, and native permission prompts as external effects, not durable render state
- do not store fire-once effects as durable `uiState`
- prefer callback outputs from the state holder or a narrow event queue only when the screen truly needs one-shot effects

## Component Rule

Separate state-holder wiring from rendering **when that separation reduces coupled work**:
- a screen container obtains dependencies and invokes an extracted state-holder hook when needed; a simple screen can own its local state directly
- extracted render components receive plain values and callbacks
- child components receive only the data/callbacks they need
- under the selected Clean contract, extracted presentational components do not import use cases, repositories, API clients, native modules, or DI containers; framework router hooks stay in the screen/container, not domain policy
- list components should use stable domain ids as keys
- pass `extraData` or another explicit prop when `FlatList` item rendering depends on state outside `data`
- use `react-native-safe-area-context` or the repo's existing safe-area boundary instead of deprecated core `SafeAreaView`
- platform-specific UI branches should stay in presentation, while platform API calls stay behind adapters

## Review Checklist

- dependency flow uses props or `Context` providers; external DI is justified or already present
- native APIs consumed by a state holder pass through the adopted boundary
- permissions, linking, storage, sensors, and custom native modules used by domain/application policy are accessed through ports/adapters
- for screens with distinct durable states, `uiState` models the applicable not-ready, loading, refreshing, placeholder, empty, error, success, offline, or permission states without contradictory booleans; simple independent local state needs no union
- `uiState` has no contradictory booleans or duplicated derived fields
- `UiAction`, `UiEvent`, and `UiState` roles are explicit for branchy screens
- presentation projections satisfy the required core's **Mapping Boundary**.
- presentation values follow adopted naming conventions; suffix or file count
  alone is not a failed state or architecture contract
- when used, the state-holder hook owns async orchestration and exposes callbacks
- extracted components stay render-focused and receive plain props; simple screens need no extra container component
- reducer logic, when present, is pure and side-effect free
- `FlatList` keys and external render dependencies are explicit
- safe-area handling does not use deprecated core `SafeAreaView` for new code
- one-shot effects are not modeled as durable UI state
- review output includes the required markers below

## Required Markers

When this skill is used for presentation development or code review, write every marker below in the phase artifact or review output. The active workflow `required_markers` is the allowed-value source of truth:

- `presentation-skill: android|flutter|react|react-native|ios|n/a`
- `presentation-state-based-development: applied|n/a`
- `presentation-state-review: pass|fail|n/a`
- `ui-state-modeling: explicit|n/a`
- `presentation-mapping-boundary: domain-to-uimodel|n/a`
- `di-boundary: hilt|context-provider|tsyringe|swift-environment|factory|swift-dependencies|swinject|needle|riverpod|get-it|direct|existing|n/a`

Apply these React Native-specific decisions:

- `presentation-skill`: use `react-native` when React Native presentation code is in scope. Use `n/a` only when the phase has no presentation work.
- `presentation-state-based-development`: use `applied` when presentation code was created or changed under this contract. Use `n/a` for review-only work or when no presentation code changed.
- `presentation-state-review`: use `pass` when every applicable checklist item passes, `fail` when any applicable item fails, and `n/a` only when no React Native presentation code is in scope.
- `ui-state-modeling`: use `explicit` when the screen's durable states are modeled explicitly. Use `n/a` only when no screen state is in scope.
- `presentation-mapping-boundary`: use `domain-to-uimodel` for domain/application
  values projected to the UI contract, including a safe identity projection;
  no separate mapper file is required. Use `n/a` only when no such boundary exists.
- `di-boundary`: use `context-provider`, `tsyringe`, `direct`, or `existing` for the verified React Native composition path. Use `n/a` only when the change neither creates nor reviews dependency wiring.

A `fail` result is actionable: record the failed criterion and return to the workflow's fix path before approval.

## Sources

- [React createContext/useContext](https://react.dev/reference/react/createContext) and [state/effects](https://react.dev/learn/you-might-not-need-an-effect)
- [React Native state](https://reactnative.dev/docs/state) and [networking](https://reactnative.dev/docs/network)
- [React Native Turbo Native Modules](https://reactnative.dev/docs/turbo-native-modules-introduction)
- [React Native PermissionsAndroid](https://reactnative.dev/docs/permissionsandroid) and [Linking](https://reactnative.dev/docs/linking)
- [React Native FlatList](https://reactnative.dev/docs/flatlist) and [SafeAreaView](https://reactnative.dev/docs/safeareaview)
- [TSyringe README](https://github.com/microsoft/tsyringe/blob/master/README.md)
