---
name: react-clean-presentation-architecture
description: Use when creating, modifying, or reviewing a React Clean Architecture presentation layer with Context Provider DI, state-holder hooks, uiState modeling, UiModel mapping, and state-based presentation code review.
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [uistate, ui state, state holder, container component, presentation hook, screen state, presentation layer]
pathGlobs: ["**/*UiState.ts", "**/*UiState.tsx", "**/presentation/**"]
requires: [clean-architecture-core]
---

# React Clean Presentation Architecture

Use this skill for React Web feature work where presentation code should follow a reusable Clean Architecture pattern.

For AppShell-owned global error hosts, queue acknowledgement, or root navigation reset, use `react-app-shell-error-handling` instead.

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

## Architecture Rule

- `presentation` owns screens, components, state-holder hooks, UI events, presentation models, and mappers.
- Pure domain policy owns entities, invariants, and business language; application owns use cases and ports under `clean-architecture-core`. Existing packages may colocate these semantic roles.
- `data` or `infrastructure` implements outbound ports and HTTP/storage clients; inbound server adapters own HTTP/tool request and response schemas.
- Presentation code consumes typed domain/application ports, not concrete API clients or repository implementations. The core's single-context state-holder repository-interface exception remains valid; it does not authorize HTTP controllers to call ORM implementations directly.
- Components should receive plain props and callbacks; they should not construct domain/data dependencies.
- Browser APIs, analytics, storage, routing, and other external systems should be wrapped behind ports/adapters before reaching state-holder hooks.

## Data and Error Boundary

- Fetch, Next.js route handlers/server actions, HTTP clients, storage, cookies,
  and browser API details stay in `data`, `infrastructure`, or server adapters.
- Transport/storage failure types are raw diagnostics until mapped by repository
  implementations or data mappers into domain error/result types.
- Use cases return normalized domain/application result and error contracts, adding business-rule errors where needed.
- Components receive presentation state, events, and `UiModel`/error UI models,
  not DTOs, `Response` objects, raw HTTP errors, or storage errors.
- At the presentation boundary, project domain/application data and errors into the render contract. Convert shapes where their meaning differs; an already suitable immutable shape may use an identity projection without a copying model or mapper file.
- Effects synchronize with external systems only; do not move domain-to-UI
  derivation or error mapping into `useEffect`.

## DI Rule

React has no Hilt-equivalent official DI framework. Use this priority:

1. Prefer explicit props for local dependencies.
2. Use React `Context` providers for app-level typed use cases/ports, feature flags, and configuration. Raw clients and implementations are created inside the composition root, not exposed through feature dependency hooks. Analytics/storage/browser capabilities reach state holders as typed ports.
3. Use an external DI container only when the project already has class-heavy domain/application services or an existing container.
4. If introducing a TypeScript DI container is justified, prefer the current repo standard. If none exists, evaluate `tsyringe` as a candidate against the project's concrete requirements; popularity alone is not an adoption reason.

Provider rules:
- create providers near composition roots such as `App`, route providers, or feature boundaries
- create context objects outside components
- keep provider values stable with `useMemo` only when the value object/function identity causes real rerender risk
- expose typed dependency-access hooks
- throw a clear error when a required provider is missing
- do not hide mutable UI state inside dependency providers

## Responsibility and Placement

Discover the existing feature, route, dependency-provider, and model conventions before placing code. Keep screen wiring, render contracts, state-holder logic, and necessary boundary mapping near their consumers; separate files or packages only when a real responsibility or dependency boundary needs them.

`UiState`, `UiModel`, and `use<Screen>ViewModel` describe roles, not a mandatory folder tree or file count. Preserve project naming conventions; an existing render contract need not be renamed just to acquire a suffix. Domain-to-presentation mapping remains explicit in meaning, but equal shapes do not require copying models or forwarding mappers.

## State Holder Rule

For interactive client screens that need orchestration, use the existing state-holder hook pattern:
- use `use<Screen>ViewModel` when it matches project naming
- inject typed use cases/ports through props, parameters, or dependency hooks
- expose durable screen states explicitly; use a discriminated union for mutually exclusive branches
- expose user actions as named callbacks
- keep client async orchestration inside the state holder; Server Components retain server data fetching and composition
- keep rendering inside components
- do not force a single `Action` reducer shape unless the repo already uses reducer/action patterns

State patterns:
- model `not-ready`, `loading`, `refreshing`, `placeholder`, `empty`, `error`, `success`, `offline`, and `permission-required` states explicitly when they can occur
- define `UiState` as a discriminated union, normally by `status` or `type`, instead of multiple booleans that can contradict each other
- do not use fake domain sentinel values as initial UI state
- derive render-only values during render instead of duplicating state
- keep request ids, abort controllers, internal pagination cursors, and rollback bookkeeping private; expose selected values, visible pagination state, and optimistic displayed results through observable props/state rather than hiding them in refs
- preserve cancellation with `AbortController` or the project’s existing request cancellation pattern
- let RHF own form values, dirty/touched state, and field errors once; keep server data in its RSC/query-cache boundary and shareable filters in the URL. Do not mirror draft state into `uiState`. Custom hooks share logic, not state instances; Context does not guarantee state lifetime or render isolation.
- when RHF draft, validation, reset, field adapters, or submit behavior changes, read `react-hook-form-zod`

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

Split client state-holder wiring from rendering where that responsibility exists:
- in Next App Router, the Server page/layout retains server data fetching, metadata, and composition; only an interactive Client wrapper obtains client dependencies and calls the state-holder hook
- pass React-supported serializable values and supported Server Action references across the RSC boundary, not repository/client instances or arbitrary closures
- a server-only view needs neither an empty hook nor a `'use client'` conversion
- the client wrapper owns browser effects such as navigation, toast, modal, analytics, and focus coordination; non-RSC routes use their existing client wiring boundary
- screen component receives plain `uiState` and callbacks
- child components receive only the data/callbacks they need
- presentational components should not import use cases, repositories, API clients, or DI containers
- keep form input, focus, hover, selection, and animation state at their appropriate owner; RHF field adapters may use `control`, `useWatch`, `useFormState`, and `useController` within presentation without lifting every field into a parent state holder

## Review Checklist

- dependency flow uses props or `Context` providers; external DI is justified or already present
- durable screen states are explicit; discriminated unions cover mutually exclusive not-ready, loading, refreshing, placeholder, empty, error, success, offline, and permission branches that can occur
- `uiState` has no contradictory booleans or duplicated derived fields
- `UiAction`, `UiEvent`, and `UiState` roles are explicit for branchy screens
- domain/application data crosses an explicit presentation projection, with conversion where semantics differ and no pointless same-shape copies
- project naming is preserved; suffixes, hook names, and file counts alone do not fail review
- interactive state holders own client orchestration and callbacks; server-only views retain server data/composition without artificial hooks
- render components receive narrow props; RHF adapters may consume the form's own state
- reducer logic, when present, is pure and side-effect free
- effects are only for external systems, not derivable state
- composition roots own implementation creation; client wiring consumes typed ports and owns browser effects; Server pages retain metadata and respect RSC serialization
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

Apply these React-specific decisions:

- `presentation-skill`: use `react` when React Web presentation code is in scope. Use `n/a` only when the phase has no presentation work.
- `presentation-state-based-development`: use `applied` when presentation code was created or changed under this contract. Use `n/a` for review-only work or when no presentation code changed.
- `presentation-state-review`: use `pass` when every applicable checklist item passes, `fail` when any applicable item fails, and `n/a` only when no React Web presentation code is in scope.
- `ui-state-modeling`: use `explicit` when the screen's durable states are modeled explicitly. Use `n/a` only when no screen state is in scope.
- `presentation-mapping-boundary`: use `domain-to-uimodel` when domain/application data crosses an explicit presentation projection, including an intentional identity projection of an already suitable shape. Use `n/a` only when no such data crosses the boundary; a separate mapper file is not required.
- `di-boundary`: use `context-provider`, `tsyringe`, `direct`, or `existing` for the verified React composition path. Use `n/a` only when the change neither creates nor reviews dependency wiring.

A `fail` result is actionable: record the concrete failed contract or adopted project rule and return to the workflow's fix path before approval. Naming preferences, extra file expectations, and absent client hooks on server-only views are not independent failures.

## Sources

- React createContext/useContext docs
- React context and state structure docs
- React useEffect and event separation docs
- TSyringe README
- [Next Server and Client Components](https://nextjs.org/docs/app/getting-started/server-and-client-components)
- [Next metadata server ownership](https://nextjs.org/docs/app/api-reference/functions/generate-metadata)
- [React serializable Client Component props](https://react.dev/reference/rsc/use-client#serializable-types-returned-by-server-components)
