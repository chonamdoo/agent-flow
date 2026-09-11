# Android Module Creation Guide

## Discovery

Before adding a module, inspect:

- the existing `settings.gradle.kts` or `settings.gradle`
- `build-logic` or `buildSrc`
- `gradle/libs.versions.toml`
- nearby feature modules
- app module dependencies

## Default Structure

Use the repository's existing structure, source sets, and convention plugins.
If no shape is adopted, identify the smallest responsibility boundary and its
dependency direction before choosing modules. Semantic presentation, domain,
application, and data roles do not each require a directory or module.

Do not create unused layers for tiny changes. A small UI-only feature may only
need presentation if the project allows it.

## Registration

- Add all new modules to the existing settings file using its Kotlin or Groovy DSL.
- Apply the correct convention plugin per layer.
- Add app or navigation dependencies only for the presentation entry point.
- Keep implementation dependencies out of API surfaces unless required.

