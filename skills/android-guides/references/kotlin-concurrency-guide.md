# Kotlin Concurrency Guide

## Coroutines

- Do not block the main thread with IO, parsing, database, or image work.
- Use structured concurrency for related async work.
- Prefer `coroutineScope` when sibling failure should cancel the whole group.
- Prefer `supervisorScope` only when sibling failure should be isolated.
- Preserve cancellation in catch blocks.

## Flow

- Collect UI flows with lifecycle-aware APIs.
- Avoid launching duplicate collectors from recomposition.
- Use sharing operators deliberately and document the lifetime.
- Debounce or conflate high-frequency UI events when needed.

## Race Conditions

Check for:

- Multiple rapid actions dispatching duplicate requests.
- Pagination requesting the same page twice.
- Refresh and load-more running concurrently.
- Stale responses overwriting newer state.

