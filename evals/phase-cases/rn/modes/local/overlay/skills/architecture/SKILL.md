---
name: architecture
description: Orders app architecture contract. Flat feature folders with function modules; use for every change under src/ in design, implementation, and review.
---

# Orders App Architecture

This project deliberately does not use Clean Architecture layers. These rules
override any platform or profile layering guidance.

## Feature folders

- Every feature lives in exactly one flat folder `src/<feature>/` (for example
  `src/orders/`). Feature folders have no subdirectories.
- Do not create `src/core/`, `src/features/`, or any directory named `domain`,
  `data`, or `presentation`.
- Shared, feature-independent helpers stay in `src/shared/`.

## File names

Each file in a feature folder is named `<feature>.<role>.ts` or
`<feature>.<role>.tsx`, with a lowercase role word:

- `<feature>.api.ts`: plain exported async functions that call the injected
  `GetJson` from `src/shared/http.ts` and convert payloads to feature records.
- `<feature>.store.ts`: the screen state holder, a plain factory that receives
  its loader function as an argument.
- `<feature>.types.ts`: the feature's exported record and state types.
- `<feature>.screen.tsx`: the React Native screen component.

## No ceremony types

Do not declare Repository, RepositoryImpl, UseCase, or port interfaces or
classes. Pass functions instead. A screen may wire `<feature>.api.ts` into its
store directly.

## Exported type names

Every `type` or `interface` exported from a feature folder has a name ending
with `Shape`. The order record is `OrderShape`; the screen state union is
`OrdersStateShape`. Non-exported local types and `src/shared/` are
unrestricted.
