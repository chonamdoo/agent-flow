---
name: android-module-creator
description: |
  Use when creating a new Android feature module or adding Clean Architecture
  layers to an Android project. Covers Gradle module registration, convention
  plugin selection, dependency direction, Hilt bindings, and initial screen
  scaffolding.
---

# Android Module Creator

Use this skill when an Android project needs a new module or feature slice.
Prefer the target repository's own Gradle convention plugins and module
templates. If a generator task exists, use it instead of hand-creating files.

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

- `../android-guides/references/module-creation-guide.md`
- `../android-guides/references/gradle-build-performance-guide.md`
- `../android-guides/references/architecture-rules-guide.md`
- `../android-guides/references/di-hilt-guide.md`

