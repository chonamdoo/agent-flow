---
name: android-module-creator
description: Android module creation workflow for new feature modules and Clean Architecture slices, including Gradle registration, convention plugin selection, dependency direction, Hilt bindings, and initial screen scaffolding. Use when creating a new Android module or feature slice; do not use for non-Android packages or presentation-only screen edits inside an existing module.
---

# Android Module Creator

Use this skill when an Android project needs a new module or feature slice.
Prefer the target repository's own Gradle convention plugins and module
templates. If a generator task exists, use it instead of hand-creating files.

## Quick start

1. Inspect existing module names, Gradle convention plugins, version catalogs, and nearby feature modules.
2. Choose the module layer and dependency direction before creating files.
3. Register the smallest useful vertical slice in `settings.gradle.kts` and matching build files.
4. Add DI, navigation, and screen scaffolding only where the project already uses those mechanisms.

## Non-goals

- Do not use this for React, React Native JS/TS, Python packages, or generic TypeScript modules.
- Do not create extra layers that the project does not already use or need.

## Process

1. Inspect `settings.gradle.kts`, `build-logic`, `buildSrc`, version catalogs,
   and nearby feature modules.
2. Identify the repository's module taxonomy. Common layers are
   `presentation`, `domain`, `usecase`, and `data`, but do not force layers
   that the project does not use.
3. Choose dependency direction before editing Gradle files.
4. Create the smallest useful vertical slice and register it in
   `settings.gradle.kts`.
5. Add DI/navigation only where the project already uses those mechanisms.
6. Run at least the module build or the Android profile build gate.

## Default Dependency Direction

When the project has no stronger convention, use:

```text
presentation -> usecase -> domain <- data
presentation -> domain
data implements domain repository interfaces
```

Rules:

- `domain` has no Android framework, database, Retrofit, or UI imports.
- `data` owns DTOs, data sources, repository implementations, and mappers.
- `presentation` owns Compose UI, ViewModels, route adapters, and UI models.
- Feature modules should not depend directly on each other; prefer navigation
  APIs or shared domain contracts.

## Gradle Checklist

- `settings.gradle.kts` includes every new submodule.
- Each module applies the repository's matching convention plugin.
- Version catalog aliases are reused instead of hard-coded dependency versions.
- New dependencies are scoped narrowly to the layer that uses them.
- Test fixtures or sample data stay out of production source sets.

## References

- [module-creation-guide.md](../android-guides/references/module-creation-guide.md) when deciding module shape, registration, and source-set layout.
- [gradle-build-performance-guide.md](../android-guides/references/gradle-build-performance-guide.md) when editing Gradle plugins, dependencies, or build gates.
- [architecture-rules-guide.md](../android-guides/references/architecture-rules-guide.md) when choosing dependency direction and layer ownership.
- [di-hilt-guide.md](../android-guides/references/di-hilt-guide.md) when adding Hilt modules, bindings, scopes, or injected components.

