---
name: react-native-clean-presentation-architecture
description: Use when creating, modifying, or reviewing a React Native Clean Architecture presentation layer with Context Provider DI, state-holder hooks, uiState modeling, UiModel mapping, navigation effects, and state-based presentation code review.
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [uistate, ui state, state holder, screen state, navigation effect, 상태 홀더, 화면 상태, 프레젠테이션 계층]
pathGlobs: ["**/*UiState.ts", "**/*UiState.tsx", "**/presentation/**"]
requires: [clean-architecture-core]
---

# React Native Clean Presentation Architecture

Use this skill for React Native or Expo feature work where presentation code should follow a reusable Clean Architecture pattern.

For AppShell-owned global error hosts, queue acknowledgement, or root navigation reset, use `react-native-app-shell-error-handling` instead.

## Evidence Basis

- React official Context docs provide the built-in provider mechanism React Native uses for passing app-level values through the tree.
- React official Effects docs define effects as synchronization with external systems; derived render state should not move into effects.
- React official state structure docs recommend grouping related state, avoiding contradictions, and avoiding redundant or duplicated state.
- React official reducer docs recommend reducer actions that describe one user interaction and pure reducer functions for complex state transitions.
- React Native state docs define props as parent-owned fixed data and state as data that changes over time; React Native state follows React state semantics.
- React Native networking docs provide `fetch` for network requests and require
  catching errors thrown by `fetch`.
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

## Data and Error Boundary

- Fetch, HTTP clients, secure storage, AsyncStorage, native modules, permissions,
  and platform SDK details stay in `data`, `infrastructure`, or native adapters.
- Network/storage/native failure types are raw diagnostics until mapped by
  repository implementations or data mappers into domain error/result types.
- Use cases return domain result/error types and add only business-rule errors.
- Screens and components receive presentation state, events, and `UiModel`/error
  UI models, not DTOs, `Response` objects, raw native errors, or storage errors.
- Presentation mappers convert domain models and domain errors into UI models
  before state reaches React Native screens.
- Effects synchronize with native/external systems only; do not move
  domain-to-UI derivation or error mapping into `useEffect`.

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
- `features/<feature>/presentation/<screen>/model/<Screen>UiAction.ts`
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
- model `not-ready`, `loading`, `refreshing`, `placeholder`, `empty`, `error`, `success`, `offline`, and `permission-required` states explicitly when they can occur
- define `UiState` as a discriminated union, normally by `status` or `type`, instead of multiple booleans that can contradict each other
- do not use fake domain sentinel values as initial UI state
- keep request ids, abort controllers, pagination cursors, selected ids, and optimistic update state private in the state holder
- preserve cancellation with `AbortController` or the project’s existing request cancellation pattern
- keep keyboard, scroll, focus, and animation state local unless it drives business work

`UiState`, `UiAction`, and `UiEvent` roles:
- `UiState` is durable render data. It must be enough to redraw the screen from props/state after navigation focus changes.
- `UiAction` is user or UI input. Use a discriminated union when the screen uses a reducer or has branchy behavior; otherwise named callbacks are acceptable but must map to explicit actions conceptually.
- `UiEvent` is a transient screen-container effect such as navigation, toast, haptic feedback, permission prompt, deep link, focus, or analytics trigger.

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
- `uiState` is a discriminated union and covers not-ready, loading, refreshing, placeholder, empty, error, success, offline, and permission states that can occur
- `uiState` has no contradictory booleans or duplicated derived fields
- `UiAction`, `UiEvent`, and `UiState` roles are explicit for branchy screens
- domain data is mapped to `UiModel` before rendering
- `UiModel` postfix is used for presentation models
- state-holder hook owns async orchestration and exposes callbacks
- components stay render-focused and receive plain props
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
- `presentation-mapping-boundary`: use `domain-to-uimodel` when domain/application data crosses into presentation through a mapper. Use `n/a` only when no such data crosses the boundary.
- `di-boundary`: use `context-provider`, `tsyringe`, `direct`, or `existing` for the verified React Native composition path. Use `n/a` only when the change neither creates nor reviews dependency wiring.

A `fail` result is actionable: record the failed criterion and return to the workflow's fix path before approval.

## Sources

- React createContext/useContext docs
- React state and effects docs
- React Native state docs
- React Native Turbo Native Modules docs
- React Native PermissionsAndroid docs
- React Native Linking docs
- React Native FlatList docs
- React Native SafeAreaView docs
- TSyringe README
- npm package metadata API
