# Refactoring and review

## Incremental migration

1. Start at the leaf farthest from UI, usually a repository or data source.
2. Convert one public fire-and-forget function to `suspend`.
3. Follow compiler errors outward and choose scope at each real boundary.
4. Replace constructor-time work with an explicit suspending phase or delete
   the observer in favor of mutation-site work.
5. Remove the unused scope parameter and its DI binding.
6. Repeat in small reviewable slices; do not rewrite every coroutine at once.

Preserve public semantics deliberately. If callers previously assumed work was
queued and returned immediately, document and test whether they should await,
launch in their own lifecycle, or enqueue durable scheduled work.

## Flow interactions

- Do not hide `stateIn` inside a repeatedly called function; each invocation can
  create a new sharing coroutine. Store one shared property.
- Use `SharingStarted.Eagerly` or explicit initialization when synchronous
  `.value` must remain fresh without collectors.
- Do not invent fake domain sentinel values just because `StateFlow` needs an
  initial value; model absence/loading or initialize in a suspending phase.
- For one-consumer, fire-once UI events, a buffered `Channel` exposed with
  `receiveAsFlow` may fit better than a default `SharedFlow`. It is fan-out, not
  broadcast; choose deliberately.
- Keep `MutableStateFlow.update` lambdas fast and side-effect free because they
  may be retried.

## Review checklist

- [ ] Repositories, managers, use cases, and data sources do not store or
  create scopes without explicit lifecycle ownership.
- [ ] Constructors and DI initializers do not launch coroutines.
- [ ] Every long-running operation has a visible named launch site.
- [ ] UI launch wrappers are on actual lifecycle-bound state holders.
- [ ] Leaf async work is exposed as `suspend` and returns a result or failure.
- [ ] Broad catches and `runCatching` preserve cancellation.
- [ ] Application code does not use `runBlocking` as an async bridge.
- [ ] Coroutine tests use `runTest` and verify cleanup.
- [ ] Repeated calls after cancellation cannot fail silently.
- [ ] Background work has explicit stop, error, and restart behavior.

## Reject superficial fixes

Adding `CoroutineExceptionHandler`, switching to `SupervisorJob`, injecting an
application scope, or adding a `close()` method does not establish ownership by
itself. Require evidence of who starts, observes, cancels, and restarts the
work. Logging an exception also does not repair swallowed cancellation.
