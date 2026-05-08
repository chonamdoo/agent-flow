# Android Module Creation Guide

## Discovery

Before adding a module, inspect:

- `settings.gradle.kts`
- `build-logic` or `buildSrc`
- `gradle/libs.versions.toml`
- nearby feature modules
- app module dependencies

## Default Structure

Use the repository's existing structure. If none exists, prefer a vertical
feature shape:

```text
feature/<feature>/
├── presentation/
├── domain/
├── usecase/
└── data/
```

Do not create unused layers for tiny changes. A small UI-only feature may only
need presentation if the project allows it.

## Registration

- Add all new modules to `settings.gradle.kts`.
- Apply the correct convention plugin per layer.
- Add app or navigation dependencies only for the presentation entry point.
- Keep implementation dependencies out of API surfaces unless required.

