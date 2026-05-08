# Gradle Build Performance Guide

## Dependency Hygiene

- Reuse version catalog aliases.
- Avoid broad `api` dependencies; prefer `implementation`.
- Keep annotation processors scoped to modules that need them.
- Do not add global Gradle plugins for one feature.

## Module Boundaries

- Smaller modules help only when boundaries are stable and dependencies are
  narrow.
- Avoid dependency cycles.
- Keep generated code configuration consistent with existing modules.

## Validation

Run the smallest meaningful gate first:

```bash
./gradlew :feature:<name>:presentation:assembleDebug
```

Then run the profile gate, usually:

```bash
./gradlew assembleDevDebug
```

