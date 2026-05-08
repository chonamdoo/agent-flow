---
name: android-mvi-feature
description: |
  Use when implementing or refactoring an Android feature screen with Kotlin,
  Jetpack Compose, ViewModel state, and MVI-style user actions. Applies to
  new screens, screen additions inside an existing feature module, and
  presentation refactors that need predictable state handling.
---

# Android MVI Feature

Use this skill for Android screen work in an `android` profile project. Keep it
project-agnostic: inspect the target app's existing modules, package names,
base classes, design system, and navigation before creating files.

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

- `../android-guides/references/mvi-feature-guide.md`
- `../android-guides/references/compose-ui-guide.md`
- `../android-guides/references/compose-performance-guide.md`
- `../android-guides/references/data-layer-guide.md`
- `../android-guides/references/error-handling-guide.md`

