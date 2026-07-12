# Scope ownership and launch sites

## Default: suspending leaf API

A stored `CoroutineScope` on a repository, manager, use case, or data source is
a strong defect signal. Once that scope is cancelled, later launches can
complete as cancelled without doing work, while callers have no result or
failure to observe.

```kotlin
// Avoid: callee chooses hidden fire-and-forget semantics.
class AnalyticsClient(
    private val scope: CoroutineScope,
    private val api: AnalyticsApi,
) {
    fun track(event: Event) {
        scope.launch { api.send(event) }
    }
}

// Prefer: caller owns scheduling, cancellation, and error handling.
class AnalyticsClient(private val api: AnalyticsApi) {
    suspend fun track(event: Event) = api.send(event)
}
```

Creating `MainScope()` or `CoroutineScope(Dispatcher + Job())` inside the class
has the same problem. Creating a new scope inside each function is worse: every
call creates uncancellable work. `lazy` does not establish ownership.

## Construction must not launch

An `init { scope.launch { ... } }` makes object readiness nondeterministic and
hides errors and cancellation. Expose an explicit suspending phase instead:

```kotlin
class UserSession(private val api: Api) {
    private var loadedUser: User? = null
    val user: User get() = checkNotNull(loadedUser) { "Call initialize first" }

    suspend fun initialize() {
        loadedUser = api.loadUser()
    }
}
```

The same restriction applies to DI singletons and `Initializer.initialize()`.
An initializer may synchronously register a listener or contributor; it should
not launch asynchronous work.

## UI-to-state-holder boundary

UI callbacks cannot suspend. A ViewModel, Decompose component, or equivalent
state holder may translate a one-shot UI event onto a lifecycle scope only when
all of these are true:

1. the UI binds directly to that state holder;
2. the scope is cancelled with that UI surface (`viewModelScope`, a component
   scope, or `rememberCoroutineScope`);
3. the caller is actually a UI event or lifecycle callback.

The repository/use-case layers underneath still expose suspending APIs. An
application scope, an injected long-lived scope, or a business class calling a
ViewModel method does not qualify for this carve-out.

## Long-lived and background work

Before creating a forever-collecting class, choose in this order:

### 1. React at the mutation site

If a known suspending operation changes the state, perform the reaction there
and delete the background observer.

```kotlin
suspend fun signOut() {
    authStore.clearTokens()
    tokenInvalidator.invalidate()
}
```

### 2. Use scheduled work

Use the platform scheduler for genuinely periodic, deferred, or retryable work.
Expose a one-shot enqueue operation from an explicit startup or feature
orchestrator.

### 3. Use an explicit named launch site

When a synchronous external API has no observable lifecycle, expose a
long-running suspending function and launch it at a named integration point:

```kotlin
class ConfigurableSampler {
    suspend fun observeRate(flags: FeatureFlags) {
        flags.observeRate().collect { rate -> updateDelegate(rate) }
    }
}

applicationScope.launch { sampler.observeRate(flags) }
```

Application infrastructure may own a scope only when it maps to an explicit
application lifecycle and has clear cancellation, error, and restart policy.
That is not permission to launch from a constructor or initializer.

Reject “bootable” auto-discovery that merely hides many launches behind another
initializer. A reader must be able to find the start moment and its owner.
