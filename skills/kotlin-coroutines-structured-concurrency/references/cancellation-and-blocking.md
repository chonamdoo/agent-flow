# Cancellation and blocking boundaries

## Preserve cancellation

`CancellationException` is how coroutine cancellation propagates. A catch for
`Exception`, `Throwable`, or `CancellationException` around a suspending call
must not convert it to ordinary failure or success.

```kotlin
try {
    api.load()
} catch (error: CancellationException) {
    throw error
} catch (error: IOException) {
    logger.warn("Load failed", error)
}
```

When an ordinary broad catch is truly required:

```kotlin
try {
    api.load()
} catch (error: Exception) {
    if (error is CancellationException) throw error
    logger.warn("Load failed", error)
}
```

`currentCoroutineContext().ensureActive()` is acceptable when the intent is to
rethrow if the current coroutine has been cancelled, but an explicit
`CancellationException` catch is usually clearer. Catching a narrow,
non-cancellation type such as `IOException` is safe.

## `runCatching` has the same hazard

`runCatching` catches `Throwable`, including coroutine cancellation. Guard the
failure or terminate with an operation that rethrows:

```kotlin
runCatching { api.load() }
    .onFailure {
        if (it is CancellationException) throw it
        logger.warn("Load failed", it)
    }

val value = runCatching { api.load() }.getOrThrow()
```

The trigger is a suspending call inside the protected block, including inside
`launch`, `collect`, and other suspending lambdas—not merely a function marked
`suspend`.

A narrow `TimeoutCancellationException` catch directly around the matching
`withTimeout` may convert that local timeout to a domain result. It must not
swallow unrelated cancellation.

## Remove `runBlocking` from application paths

`runBlocking` parks the caller thread and ignores an upstream asynchronous
lifecycle. In suspend-capable code, make the API suspend. At a UI boundary, use
the existing lifecycle scope instead of blocking inside a repository function.

```kotlin
suspend fun saveUser(user: User) = repository.save(user)
```

Legitimate outer blocking boundaries are rare: a CLI `main`, unavoidable Java
interop that must return synchronously, or a framework callback with no
suspending alternative. Keep the bridge at the outermost boundary and make its
body immediately call suspending code.

Android `ContentProvider` member overrides are a narrow framework carve-out
because `query`, `insert`, `update`, `delete`, `call`, and `onCreate` are
synchronous surfaces:

```kotlin
override fun query(/* ... */): Cursor? = runBlocking { dao.query(/* ... */) }
```

Keep this body minimal. A helper or companion object associated with a provider
is not itself the framework boundary.

## Tests use virtual time

Use `runTest`, test dispatchers, and structured cleanup instead of
`runBlocking`:

```kotlin
@Test
fun loadsUser() = runTest {
    assertEquals("Alice", repository.load().name)
}
```

Test cancellation explicitly: cancel the owner, assert children finish, then
invoke any repeatable API and confirm its contract remains observable.
