# Runtime and Operations

Read when changing server coroutine scopes, blocking work, resource limits, workers, health, or shutdown.

## Coroutine and resource ownership

Tie request tasks to a request-owned scope; application workers need an owned scope, failure policy, and cancellation/join during shutdown. A SupervisorJob isolates sibling failure but does not persist work or make an unobserved failure safe. Propagate CancellationException through broad catch/runCatching paths. Cooperative CPU loops need cancellation checks; blocking drivers need their actual timeout/cancellation support.

A suspending function can still block a thread. Bound blocking execution against connection and downstream capacity; a dispatcher name alone is not admission control. Keep connection/session transaction ownership coherent for the whole transaction. Do not move just one repository call out of a thread-bound transaction. Cancellation during or after a commit leaves the caller's knowledge uncertain; recover by operation state rather than assuming interruption undid the effect.

Release resources on all paths. If cleanup must suspend after cancellation, use the coroutine library's supported narrow non-cancellable cleanup mechanism with a finite bound; never shield the whole business operation just to prevent cancellation. Avoid allocating pools/clients per request when application-owned resources already exist.

## Operational contract

Define body/page/queue and concurrency limits, acquisition and operation deadlines, and overload behavior. Align retries with an overall deadline and safe effect state. Log useful operation/trace IDs with redaction; restrict high-cardinality labels and sensitive operational endpoints.

Liveness asks whether the process can make progress; readiness asks whether it should accept this traffic. A dependency outage may remove readiness without forcing a restart loop. On shutdown stop admission/claims, drain requests or persist recoverable handoff, stop/join workers, then close clients, pools, and owned executors within the platform deadline. An accepted job is not completed merely because shutdown began.

## Jobs and consumers only

For long jobs, define durable identity, checkpoint transaction boundary, restart semantics, competing-worker claims/lease expiry, and cancellation. For consumers, align effect commit and acknowledgement; a crash after commit but before ack causes redelivery. Define deduplication retention, bounded poison-message retries and dead-letter handling, plus who can inspect/replay safely. In-memory queues and process-local locks cannot coordinate multiple instances or survive restarts. Do not add a broker or Batch framework where no durable work is required.

## Official sources

- [Kotlin cancellation](https://kotlinlang.org/docs/coroutines-cancellation.html), [exception handling](https://kotlinlang.org/docs/exception-handling.html), and [dispatchers/context](https://kotlinlang.org/docs/coroutine-context-and-dispatchers.html).
- [Kubernetes probes](https://kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes/) and [pod termination](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination): apply only when Kubernetes is the actual deployment platform.
