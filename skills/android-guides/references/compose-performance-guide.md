# Compose Performance Guide

## Stability

- Prefer stable immutable UI models.
- Avoid passing mutable collections into composables.
- Use persistent immutable collections when the project already uses them.
- Mark models with stability annotations only when their fields actually obey
  the contract.

## Recomposition

- Use `remember` for expensive derived values.
- Use `derivedStateOf` when a computed value changes less often than its inputs.
- Keep lambdas stable when they are passed deeply or into large lists.
- Avoid doing IO, parsing, sorting, or filtering large collections directly in
  composition.

## Lazy Layouts

- Always provide keys for identity-bearing rows.
- Avoid unstable item models that cause broad recomposition.
- Paginate with explicit loading and end-of-list state.

