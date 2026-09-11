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

Discover the repository's actual wrapper, module paths, variants, available
tasks, and active profile gates. Run the smallest meaningful gate for the
changed boundary first, then the required broader profile gate. Do not assume
a feature taxonomy, debug variant, or root assemble task.

