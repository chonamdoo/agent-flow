# Android Code Review Checklist

## Architecture

- Domain has no Android/UI/network/database imports.
- Data maps DTOs before crossing boundaries.
- Presentation does not call API/database clients directly.
- Feature modules avoid direct dependencies on each other.

## MVI and State

- Main state covers loading, success, empty, and error when applicable.
- Actions are exhaustive and not stringly typed.
- ViewModel does not hold `Context` or composables.
- One-off effects are not stored as durable state unless the project pattern
  requires it.

## Compose

- Lifecycle-aware collection.
- Stable keys in lazy lists.
- Heavy derived work is remembered.
- UI model stability is reasonable.

## Coroutine and Flow

- Cancellation is preserved.
- Main-thread blocking is avoided.
- Duplicate collectors and duplicate requests are controlled.

## Tests

- Mappers and use cases have unit coverage for edge cases.
- ViewModel state transitions are tested when logic is non-trivial.
- Bug fixes include regression coverage where feasible.

