---
name: kotlin-coroutines-structured-concurrency
description: Enforces lifecycle-owned Kotlin coroutines, suspending APIs, cancellation propagation, explicit background-work launch sites, and nonblocking boundaries. Use when writing or reviewing stored CoroutineScope properties, init launches, fire-and-forget APIs, runBlocking, runCatching around suspension, broad exception catches, or DI-started background loops.
---

# Kotlin Coroutines: Structured Concurrency

A coroutine unit should have a visible entry point, a lifecycle known at the
call site, and cancellation and failure that propagate to its owner. Most
repositories, managers, use cases, and data sources should expose `suspend`
functions rather than store or create a scope.

## Quick start

1. Locate stored/ad-hoc scopes, `init` launches, non-suspending launch wrappers,
   broad catches around suspension, and `runBlocking`.
2. Decide who owns the lifecycle and where start, stop, failure, and restart are
   observable.
3. Convert non-UI leaf APIs to `suspend` and let callers choose the scope.
4. Keep non-suspending-to-suspending translation at a real UI state-holder or
   unavoidable outer framework boundary.
5. Rethrow `CancellationException` from every broad catch around suspension.
6. Replace application/test `runBlocking` with suspension or `runTest`.
7. Compile callers and test cancellation, failure, repeated calls, and teardown.

```kotlin
class UserRepository(private val api: UserApi) {
    suspend fun refresh(): User = api.fetchUser()
}

class UserViewModel(private val repository: UserRepository) : ViewModel() {
    fun onRefresh() {
        viewModelScope.launch { repository.refresh() }
    }
}
```

## Progressive references

- Read [scope-ownership.md](references/scope-ownership.md) for stored scopes,
  UI boundaries, initializers, singletons, and background-loop alternatives.
- Read [cancellation-and-blocking.md](references/cancellation-and-blocking.md)
  for broad catches, `runCatching`, `runBlocking`, tests, and narrow carve-outs.
- Read [refactoring-and-review.md](references/refactoring-and-review.md) for an
  incremental migration, Flow interactions, and review checks.

## Non-negotiable review questions

- Where is the work started, and can a reader find that call site?
- Which lifecycle cancels it?
- How does the caller observe success and failure?
- Who can stop or restart long-lived work?
- Does cancellation still escape every error path?

If any answer is “wherever DI constructs it,” “nothing,” or “the caller cannot
know,” request a lifecycle/API redesign rather than adding another handler.
