---
name: android-mvi-feature
description: Android MVI feature-screen implementation guide for Kotlin, Jetpack Compose, ViewModel state, user actions, UI models, and predictable state handling. Use when implementing or refactoring Android feature screens in an existing module; do not use for creating new modules, generic Android debugging, or non-Android React/TypeScript/Python UI.
---

# Android MVI Feature

Use this skill for Android screen work in an `android` profile project. Keep it
project-agnostic: inspect the target app's existing modules, package names,
base classes, design system, and navigation before creating files.

## Quick start

1. Find the closest existing screen and mirror its package, navigation, DI, state, and design-system conventions.
2. Define the screen contract first: `UiState`, user `Action`s, data/use-case inputs, and visible state branches.
3. Implement from state holder to UI: ViewModel, mapper/model, entry view, state-branching screen, then components.
4. Verify loading, success, empty, and error states unless the repository has a stronger pattern.

## Non-goals

- Do not use this to create Gradle modules; use `android-module-creator`.
- Do not use it as a root-cause workflow for broken behavior; use `android-debugging`.
- Do not apply it to React Web, React Native JS/TS screens, Python, or generic TypeScript UI.

## Process

1. Find the closest existing screen implementation and mirror its local naming,
   package, DI, navigation, and design-system conventions.
2. Define the domain/use-case surface before presentation when the screen needs
   data or mutations.
3. Build presentation in this order: `UiState` -> `Action` -> `ViewModel` ->
   mapper/model -> entry `View` -> state-branching `Screen` -> components.
4. Verify state coverage: loading or placeholder, success, empty, and error
   states unless the project has a stronger existing pattern.
5. Run the Android profile gates before reporting completion.

## Default Shape

Use the existing project structure when it differs. When there is no established
pattern, prefer:

```text
feature/<feature>/presentation/src/main/java/<package>/<feature>/presentation/<screen>/
├── <Screen>View.kt
├── component/
│   ├── <Screen>Screen.kt
│   └── <Screen>*Component.kt
├── mapper/
│   └── <Screen>UiModelMapper.kt
├── model/
│   ├── <Screen>UiModel.kt
│   └── <Screen>Item.kt
└── mvi/
    ├── <Screen>Action.kt
    ├── <Screen>UiState.kt
    └── <Screen>ViewModel.kt
```

## Rules

- ViewModel depends on use cases or interactors, not repositories or API
  clients directly.
- Presentation maps domain models to UI models before rendering.
- Compose state is collected with lifecycle-aware APIs where available.
- Lazy lists use stable keys.
- Expensive derived UI values are memoized with `remember` or `derivedStateOf`.
- Use immutable collections when the project already depends on them.
- Do not introduce a new MVI framework or base class if the project has one.

## References

- [mvi-feature-guide.md](../android-guides/references/mvi-feature-guide.md) when defining actions, UI state, ViewModel behavior, and screen file shape.
- [compose-ui-guide.md](../android-guides/references/compose-ui-guide.md) when building Compose layout, semantics, previews, or components.
- [compose-performance-guide.md](../android-guides/references/compose-performance-guide.md) when state reads, stability, lazy lists, or frame-time work are relevant.
- [data-layer-guide.md](../android-guides/references/data-layer-guide.md) when the screen reads, mutates, maps, or caches domain data.
- [error-handling-guide.md](../android-guides/references/error-handling-guide.md) when modeling screen errors, retries, or app-wide failure handling.

