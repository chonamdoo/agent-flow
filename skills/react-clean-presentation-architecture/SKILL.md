---
name: react-clean-presentation-architecture
description: Defines React Web Clean Architecture presentation-layer guidance for Context Provider DI, state-holder hooks, uiState modeling, UiModel mapping, and state-based presentation review. Use when creating, modifying, or reviewing React Web feature presentation code for durable UI state and domain-to-UiModel boundaries.
required_markers:
  - "presentation-skill: react"
  - "presentation-state-based-development: applied|n/a"
  - "presentation-state-review: pass|fail"
  - "ui-state-modeling: explicit"
  - "presentation-mapping-boundary: domain-to-uimodel|n/a"
  - "di-boundary: context-provider|tsyringe|existing|n/a"
---

# React Clean Presentation Architecture

Use this skill for React Web feature work where presentation code should follow a reusable Clean Architecture pattern.

## Quick start

1. Use this for feature or screen presentation state: dependency providers, state-holder hooks, `uiState`/`UiAction`/`UiEvent`, `UiModel` mapping, and render-focused components.
2. Start by locating the state-holder hook and its domain dependencies, then model durable `uiState` before wiring route/page and screen components.
3. If the task is app-wide common error UI, session expiry, maintenance mode, auth-flow switching, router reset, or global dialog/snackbar/toast ownership, use `react-app-shell-error-handling` instead.

## Do not use for

- AppShell/root-layout common error hosts, `SessionExpired`/`Maintenance` flow switching, router resets, or global toast/snackbar containers; use `react-app-shell-error-handling`.
- Data, server, or domain implementation design except to enforce presentation boundaries and mapper responsibilities.


## Evidence Basis

- React official Context docs provide the built-in provider mechanism for passing app-level values through the component tree.
- React official Context docs say context values are read from the nearest provider and re-render consumers when the value changes.
- React official Effects docs define effects as synchronization with external systems; derived render state should not move into effects.
- React official state structure docs recommend grouping related state, avoiding contradictions, and avoiding redundant or duplicated state.
- React official sharing state docs define a single owner/source of truth for each piece of state.
- React official reducer docs recommend reducer actions that describe one user interaction and pure reducer functions for complex state transitions.
- React official events/effects docs separate event-handler logic from reactive effect synchronization.
- Next.js App Router docs default layouts/pages to Server Components for server
  data fetching and use Client Components for state, event handlers, effects,
  and browser APIs.
- React has no official Hilt-equivalent DI container; external containers should be introduced only when project shape justifies them.
- npm downloads check on 2026-06-02 for 2026-05-02..2026-05-31: `tsyringe` 23,949,329; `inversify` 8,334,605; `typed-inject` 3,066,359; `awilix` 1,904,000; `typedi` 1,878,420.

## Architecture Rule

- `presentation` owns screens, components, state-holder hooks, UI events, presentation models, and mappers.
- `domain` owns entities, use cases, repository interfaces, and pure business rules.
- `data` or `infrastructure` implements repository interfaces and HTTP/storage clients.
- Presentation code depends on domain use cases or ports, not concrete API clients or repository implementations.
- Components should receive plain props and callbacks; they should not construct domain/data dependencies.
- Browser APIs, analytics, storage, routing, and other external systems should be wrapped behind ports/adapters before reaching state-holder hooks.

## Data and Error Boundary

- Fetch, Next.js route handlers/server actions, HTTP clients, storage, cookies,
  and browser API details stay in `data`, `infrastructure`, or server adapters.
- Transport/storage failure types are raw diagnostics until mapped by repository
  implementations or data mappers into domain error/result types.
- Use cases return domain result/error types and add only business-rule errors.
- Components receive presentation state, events, and `UiModel`/error UI models,
  not DTOs, `Response` objects, raw HTTP errors, or storage errors.
- Presentation mappers convert domain models and domain errors into UI models
  before state reaches components.
- Effects synchronize with external systems only; do not move domain-to-UI
  derivation or error mapping into `useEffect`.

## DI Rule

React has no Hilt-equivalent official DI framework. Use this priority:

1. Prefer explicit props for local dependencies.
2. Use React `Context` providers for app-level dependencies such as use cases, repositories, API clients, feature flags, analytics, and configuration.
3. Use an external DI container only when the project already has class-heavy domain/application services or an existing container.
4. If introducing a TypeScript DI container is justified, prefer the current repo standard. If none exists, `tsyringe` is the default candidate because current npm usage is higher than common alternatives.

Provider rules:
- create providers near composition roots such as `App`, route providers, or feature boundaries
- create context objects outside components
- keep provider values stable with `useMemo` only when the value object/function identity causes real rerender risk
- expose typed hooks such as `useSearchDependencies()`
- throw a clear error when a required provider is missing
- do not hide mutable UI state inside dependency providers

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

Presentation mappers must convert domain data into presentation models before state reaches React components.
Presentation model types must use the `UiModel` postfix, for example `<Screen>ItemUiModel`.

## State Holder Rule

Use a custom hook as the screen state holder:
- name it `use<Screen>ViewModel`
- inject use cases/dependencies through props, parameters, or dependency hooks
- expose `uiState` as an explicit discriminated union
- expose user actions as named callbacks
- keep async orchestration inside the hook
- keep rendering inside components
- do not force a single `Action` reducer shape unless the repo already uses reducer/action patterns

State patterns:
- model `not-ready`, `loading`, `refreshing`, `placeholder`, `empty`, `error`, `success`, `offline`, and `permission-required` states explicitly when they can occur
- define `UiState` as a discriminated union, normally by `status` or `type`, instead of multiple booleans that can contradict each other
- do not use fake domain sentinel values as initial UI state
- derive render-only values during render instead of duplicating state
- keep request ids, abort controllers, pagination cursors, and selected ids private in the state holder
- preserve cancellation with `AbortController` or the project’s existing request cancellation pattern

`UiState`, `UiAction`, and `UiEvent` roles:
- `UiState` is durable render data. It must be enough to redraw the screen from props/state.
- `UiAction` is user or UI input. Use a discriminated union when the screen uses a reducer or has branchy behavior; otherwise named callbacks are acceptable but must map to explicit actions conceptually.
- `UiEvent` is a transient page-level effect such as navigation, toast, modal, focus, or analytics trigger.

Reducer/action patterns:
- use `useReducer` when state transitions are complex, coupled, or bug-prone
- keep reducers pure and free of requests, timers, navigation, storage, and other side effects
- model each action as one user interaction or external result, not as many field-level patches when one semantic action exists

Event patterns:
- handle navigation, toast, snackbar, modal, and focus effects deliberately
- do not store fire-once effects as durable `uiState`
- prefer callback outputs from the state holder or a narrow event queue only when the screen truly needs one-shot effects
- use `useEffect` only to synchronize with external systems such as browser APIs, subscriptions, timers, routing libraries, or third-party widgets
- keep derivable UI data in render or memoized derivation, not in effects

## Component Rule

Split state-holder wiring from rendering:
- route/page component obtains dependencies and calls `use<Screen>ViewModel`
- route/page component performs external effects such as navigation, toast, modal, analytics, and focus coordination
- screen component receives plain `uiState` and callbacks
- child components receive only the data/callbacks they need
- presentational components should not import use cases, repositories, API clients, or DI containers
- keep form input, focus, hover, selection, and animation state local unless it drives business work

## Review Checklist

- dependency flow uses props or `Context` providers; external DI is justified or already present
- `uiState` is a discriminated union and covers not-ready, loading, refreshing, placeholder, empty, error, success, offline, and permission states that can occur
- `uiState` has no contradictory booleans or duplicated derived fields
- `UiAction`, `UiEvent`, and `UiState` roles are explicit for branchy screens
- domain data is mapped to `UiModel` before rendering
- `UiModel` postfix is used for presentation models
- state-holder hook owns async orchestration and exposes callbacks
- components stay render-focused and receive plain props
- reducer logic, when present, is pure and side-effect free
- effects are only for external systems, not derivable state
- route/page wiring owns dependency lookup and external effects; screen/content components do not import DI containers or domain/data dependencies
- one-shot effects are not modeled as durable UI state
- review output includes the required markers below

## Required Markers

When this skill is used for presentation development or code review, include these markers in the completion artifact or review output:

- `presentation-skill: react`
- `presentation-state-based-development: applied|n/a`
- `presentation-state-review: pass|fail`
- `ui-state-modeling: explicit`
- `presentation-mapping-boundary: domain-to-uimodel|n/a`
- `di-boundary: context-provider|tsyringe|existing|n/a`

## Sources

- React createContext/useContext docs
- React context and state structure docs
- React useEffect and event separation docs
- TSyringe README
- npm package metadata API
