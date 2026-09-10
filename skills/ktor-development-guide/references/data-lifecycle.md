# Ktor Data and Lifecycle

Read for database execution, engine configuration, application workers, or shutdown. First identify the actual DB library/driver and engine; Ktor does not define their transaction semantics.

## Database execution

A suspending route is not a nonblocking DB driver. For JDBC/JPA, dispatch a coherent blocking transaction onto bounded resources compatible with the transaction library, acquire/release the connection there, and keep all participating statements under the same supported ownership. Do not wrap isolated statements in different `withContext` calls and assume they share a transaction. Account for connection-pool demand and acquisition/statement deadlines.

For Exposed or another library, check the installed JDBC versus R2DBC transaction API, coroutine support, nested transaction semantics, and retry defaults. A method named suspendTransaction does not by itself establish that its driver is nonblocking. Preserve normalized error mapping and cancellation; retries from a library must not repeat non-idempotent external effects embedded in the block.

Application actions retain the consistency boundary. If a second write fails, observe whether the first remains; if a response disappears after commit, recover an unknown outcome rather than repeating the action. Do not share a connection/session across concurrent children unless the library explicitly supports it.

## Lifecycle

Discover whether startup uses embeddedServer or EngineMain and which Application modules are actually loaded. Module functions are composition units, not independent processes. Match engine settings, lifecycle events, stop grace/timeout, and ownership to the installed version; do not copy defaults from another engine.

Keep readiness false until required resources are available and on shutdown stop new admission/claims before draining. Accepted durable jobs need persisted recovery; request-scoped child tasks need cancellation/join. Close pools/clients only after their users stop, then release owned execution resources. A lifecycle hook registered on an unused Application instance proves nothing.

Use the established migration runner with explicit ownership and populated-schema/rolling compatibility. Startup from multiple instances must not race an uncoordinated backfill. Do not create a per-request database, client pool, or worker scope where an application-owned resource is appropriate.

## Evidence boundaries

`testApplication` is useful for route/plugin outcomes, but its test host does not establish production engine thread usage, socket draining, or actual DB isolation. Use the active authorized path for each claimed boundary; report unexercised engine/DB behavior explicitly. A small Ktor server with constructor-injected DB-only persistence is healthy without Spring, a DI container, or an event broker.

## Official sources

- [Ktor server configuration](https://ktor.io/docs/server-create-and-configure.html) and [application events](https://ktor.io/docs/server-events.html).
- [Ktor testing](https://ktor.io/docs/server-testing.html).
- [Exposed transactions](https://www.jetbrains.com/help/exposed/transactions.html): only when Exposed is selected; consult its installed version and driver.
- [Kotlin coroutine context](https://kotlinlang.org/docs/coroutine-context-and-dispatchers.html).
