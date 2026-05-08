# Android Testing Guide

## Unit Tests

- Test use cases and mappers without Android framework dependencies.
- Test ViewModel action handling and state transitions.
- Cover success, empty, error, and cancellation where applicable.

## UI Tests

- Use Compose UI tests for critical state rendering and interactions.
- Prefer deterministic fake data sources over real network calls.
- Avoid screenshot-only verification unless visual layout is the actual risk.

## Regression Tests

Add tests when the bug was caused by:

- Mapper edge cases
- Race conditions
- Pagination boundaries
- Error translation
- Empty/null response handling

