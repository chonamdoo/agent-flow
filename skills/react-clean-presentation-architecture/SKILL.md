---
name: react-clean-presentation-architecture
description: Use when creating, modifying, or reviewing a React Clean Architecture presentation layer with Context Provider DI, state-holder hooks, uiState modeling, UiModel mapping, and state-based presentation code review.
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

## Evidence Basis

- React official Context docs provide the built-in provider mechanism for passing app-level values through the component tree.
- React has no official Hilt-equivalent DI container; external containers should be introduced only when project shape justifies them.
- npm downloads check on 2026-06-02 for 2026-05-02..2026-05-31: `tsyringe` 23,949,329; `inversify` 8,334,605; `typed-inject` 3,066,359; `awilix` 1,904,000; `typedi` 1,878,420.

## Architecture Rule

- `presentation` owns screens, components, state-holder hooks, UI events, presentation models, and mappers.
- `domain` owns entities, use cases, repository interfaces, and pure business rules.
- `data` or `infrastructure` implements repository interfaces and HTTP/storage clients.
- Presentation code depends on domain use cases or ports, not concrete API clients or repository implementations.
- Components should receive plain props and callbacks; they should not construct domain/data dependencies.

## DI Rule

React has no Hilt-equivalent official DI framework. Use this priority:

1. Prefer explicit props for local dependencies.
2. Use React `Context` providers for app-level dependencies such as use cases, repositories, API clients, feature flags, analytics, and configuration.
3. Use an external DI container only when the project already has class-heavy domain/application services or an existing container.
4. If introducing a TypeScript DI container is justified, prefer the current repo standard. If none exists, `tsyringe` is the default candidate because current npm usage is higher than common alternatives.

Provider rules:
- create providers near composition roots such as `App`, route providers, or feature boundaries
- keep provider values stable with `useMemo` only when the value object/function identity causes real rerender risk
- expose typed hooks such as `useSearchDependencies()`
- throw a clear error when a required provider is missing
- do not hide mutable UI state inside dependency providers

## Package Shape

Feature presentation packages should stay screen-oriented:
- `features/<feature>/presentation/<screen>/<Screen>.tsx`
- `features/<feature>/presentation/<screen>/use<Screen>ViewModel.ts`
- `features/<feature>/presentation/<screen>/model/<Screen>UiState.ts`
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

State patterns:
- model not-ready, loading, refreshing, empty, error, success, offline, and permission states explicitly when they can occur
- do not use fake domain sentinel values as initial UI state
- derive render-only values during render instead of duplicating state
- keep request ids, abort controllers, pagination cursors, and selected ids private in the state holder
- preserve cancellation with `AbortController` or the project’s existing request cancellation pattern

Event patterns:
- handle navigation, toast, snackbar, modal, and focus effects deliberately
- do not store fire-once effects as durable `uiState`
- prefer callback outputs from the state holder or a narrow event queue only when the screen truly needs one-shot effects

## Component Rule

Split state-holder wiring from rendering:
- route/page component obtains dependencies and calls `use<Screen>ViewModel`
- screen component receives plain `uiState` and callbacks
- child components receive only the data/callbacks they need
- presentational components should not import use cases, repositories, API clients, or DI containers
- keep form input, focus, hover, selection, and animation state local unless it drives business work

## Review Checklist

- dependency flow uses props or `Context` providers; external DI is justified or already present
- `uiState` is explicit and covers not-ready, loading, refreshing, empty, error, success, offline, and permission states that can occur
- domain data is mapped to `UiModel` before rendering
- `UiModel` postfix is used for presentation models
- state-holder hook owns async orchestration and exposes callbacks
- components stay render-focused and receive plain props
- effects are only for external systems, not derivable state
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

- React `createContext`: https://react.dev/reference/react/createContext
- React `useContext`: https://react.dev/reference/react/useContext
- Passing data deeply with context: https://react.dev/learn/passing-data-deeply-with-context
- tsyringe: https://github.com/microsoft/tsyringe
- npm downloads API: https://api.npmjs.org/downloads/point/last-month/tsyringe
- npm downloads API for inversify: https://api.npmjs.org/downloads/point/last-month/inversify
- npm downloads API for typed-inject: https://api.npmjs.org/downloads/point/last-month/typed-inject
- npm downloads API for awilix: https://api.npmjs.org/downloads/point/last-month/awilix
- npm downloads API for typedi: https://api.npmjs.org/downloads/point/last-month/typedi
