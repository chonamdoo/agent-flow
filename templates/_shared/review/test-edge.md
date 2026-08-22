# Review Angle: Test Edges (Android / Kotlin)

Check missing edge-case tests, fixture gaps, concurrency cases, and
failure-path coverage.

## What to verify

1. **Empty / boundary inputs**
   - Empty list / empty string / 0 / null handled by behavior tests, not
     just the happy path.
   - Min/max boundaries for paginated endpoints (page=0, lastPage,
     beyond-last).

2. **Failure paths**
   - Repository failures (`Flow` emitting error, exception thrown) are
     asserted, not silently swallowed.
   - Retry policy proven by test (3 retries, then surfaces error).

3. **Concurrency**
   - Coroutine cancellation tested (`cancelAndJoin` doesn't leak state).
   - `StateFlow` collectors don't miss values during transition.
   - Idempotency: invoking the use case twice produces the same result.

4. **Lifecycle**
   - ViewModel scope cancellation tested when `onCleared` runs.
   - Saved-state restoration after process death (where applicable).

5. **Test doubles**
   - Fakes preferred over mocks for repositories and data sources (mock
     verification couples to implementation, not behavior).
   - `Dispatchers.Main` swapped via `MainCoroutineRule` in unit tests.

6. **Compose / UI tests**
   - Semantics assertions, not pixel comparisons (unless screenshot
     intentional).
   - Recomposition counts checked for performance-sensitive composables.

## Output format

```text
## Test-edge review findings

verdict: approve | request-changes

### Must-fix
- <severity:high> [path:line] <statement>. Missing case: <description>.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...
```

Cite paths as `path/to/file:line`. Keep total under 150 lines.

Emit exactly one unfenced final verdict line: `verdict: approve` or `verdict: request-changes`.
