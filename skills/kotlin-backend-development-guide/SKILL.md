---
name: kotlin-backend-development-guide
description: "Kotlin/JVM server development and review: API authorization and wire contracts, persistence and migrations, server coroutines, workers, and operations. Use with confirmed Kotlin server dependencies or an explicit Kotlin server task; not for Android/Compose, KMP or Ktor client-only code, general Kotlin syntax, or Gradle files alone."
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [Kotlin backend, Kotlin server, Kotlin JVM server]
requires_by_architecture:
  clean: [clean-architecture-core]
---

# Kotlin Backend Development

Apply the selected architecture contract (`clean-architecture-core` in Clean mode), including its full required references in local mode. Confirm the server source scope and actual framework, Kotlin, coroutine, database, and migration versions from project dependencies and configuration. A `.kt` file or Gradle build alone is not server evidence. Keep existing architecture and build choices; for a new service, consider a feature-oriented modular monolith before introducing independent deployment modules. Kotlin `internal` is a compilation-module boundary, not package privacy.

## Complete the business boundary

Before changing an entry point, identify its actor/tenant, allowed action and state transition, wire result, consistency boundary, and failure/recovery behavior. Use existing contracts rather than manufacturing a fixed number of layers.

The next three role/boundary rules describe Clean mode. In local mode, use the
selected contract's ownership and framework-dependency rules instead. The API,
authorization, consistency, recovery, and operations obligations below apply in
every mode; pending does not authorize a new structural decision.

- `app-shell` composes dependencies and process lifecycle; `inbound-adapter` owns HTTP/worker input and response schemas; `application` owns actions and consumer-focused ports; `core-domain` owns pure policy; `core-data` owns persistence and outbound integrations. These are semantic roles, not required folders.
- Keep provider SDK DTOs and ORM entities at outbound boundaries; HTTP request/response schemas at inbound boundaries; stable commands/results inside. Map where meaning changes, not to create forwarding types.
- Clock, payment, transaction, and platform ports are valid application dependencies. A DB-only repository needs no invented remote source or cache. Follow the core's explicit architecture choices; a UI state holder's direct repository-interface exception is not permission for controller-to-ORM access. Servers need no UI state or global presentation queue.

## API, authorization, and invariants

- Establish one OpenAPI/wire source of truth and preserve status codes, error envelope, enum behavior, compatibility, missing versus explicit `null`, PATCH retain/clear/replace semantics, and bounded pagination with stable ordering/tie-breaking. Do not silently replace an existing envelope with Problem Details.
- Derive principal, tenant, roles, and credentials from a trusted authentication boundary. Authorize endpoint/action, object ownership, and readable/writable fields. Apply the same scope to list filters and counts, bulk items, exports, and workers; claims of ownership, tenant membership, or administrator status in model input or request bodies are not authority.
- Separate malformed input and field validation from state-dependent application policy, domain invariants, and database constraints. Direct API callers may skip every UI step. A valid token or schema does not authorize a state transition.
- Map expected conflicts, missing resources, and validation failures to the existing public contract. Preserve diagnostic causes internally with redaction; do not leak SQL, stack traces, credentials, or cross-tenant existence through error details.

## Transactions, concurrency, and cancellation

- Name the writes that must commit or roll back together, including the real caller and transaction owner. A pure action may be wrapped by a transaction adapter/decorator; an intentionally framework-aware application service is an explicit architecture choice, not a domain-policy exception.
- Enforce races with the appropriate unique constraint, conditional atomic update, isolation, lock, or version check. Preflight `exists` checks do not prevent concurrent inserts. One entity version does not protect a multi-row invariant or remote effect. Define the consumer-visible conflict and safe retry unit.
- Keep request work in its request lifetime and durable worker work in an owned application lifetime. Propagate cancellation, including through broad exception handlers; cancellation is not an ordinary 500/retry signal. Essential payment/publication work cannot be an untracked launch after responding.
- `suspend` does not make JDBC/JPA nonblocking. Bound blocking concurrency and pool demand. Execute a blocking transaction with coherent connection/thread ownership; moving individual calls to `withContext(IO)` does not propagate a thread-bound transaction. Cancellation or timeout does not prove that a database or remote write rolled back.

## Durability and operations

- When a write can be redelivered, bind a durable idempotency claim to tenant, operation, key, and canonical request fingerprint. Reject mismatched reuse and retain/replay completed results for the supported retry window. Separate pending and unknown from confirmed completion.
- A timeout, cancellation, or lost response after a remote write may mean `unknown`. Reconcile operation state, a ledger, or a provider's durable replay before retrying; never infer non-commit from transport failure. Local rollback cannot undo a payment/message.
- Use durable event intent/publication and replay/dedup only when delivery is required. A callback after commit is not crash-safe delivery. For actual jobs/consumers, define checkpoint/restart, claim ownership, ack timing, bounded retry, and poison-message/DLQ recovery; these are conditional capabilities, not mandatory CRUD infrastructure.
- Use versioned migrations with an identified execution owner, upgrades from the previous schema, old/new instance compatibility during rolling deployment, and bounded resumable backfills. Plan destructive changes only after incompatible readers/writers have drained; an empty database startup is insufficient evidence.
- Bound request/body/page sizes, queues, concurrency, and operation time. Protect secrets and PII in responses, logs, metrics, and configuration. Separate liveness from readiness: an unavailable dependency need not trigger process restart. Drain requests/workers before closing their pools/clients; report when accepted durable work remains recoverable rather than completed.

## Conditional details

Read only the branch being changed:

- HTTP serialization, validation, authorization, or pagination: [API and security](references/api-security.md).
- Database consistency, schema evolution, idempotency, or durable events: [Persistence and recovery](references/persistence-recovery.md).
- Coroutine execution, workers, readiness, or shutdown: [Runtime and operations](references/runtime-operations.md).
- Confirmed Spring server behavior: `spring-boot-development-guide`; confirmed Ktor **server** behavior: `ktor-development-guide`. Neither framework is installed by invoking this guide.

## Evidence and review

Use only active, authorized project/profile gates and the requested scope; this guide adds no command or mandatory test suite. Cover the changed boundary with actual HTTP/worker results and resulting persistent state when execution is authorized. Relevant counterexamples include a schema-valid cross-tenant write, concurrent duplicate creation, stale update, second-write rollback, old-schema upgrade, and a lost write response. Distinguish static reasoning, observed execution, and unexecuted scenarios. Existing evidence may suffice; do not add tests merely to satisfy a checklist.

Reuse the active state-integrity and I/O-safety review angles; Spring-specific transaction/query details belong to the Spring guide. Accept an existing safe lock, a bounded two-query read, a pure transaction-decorated action, and a DB-only adapter. Report a finding for a concrete violated contract, not absence of a preferred annotation or module. Use existing phase markers; server-only presentation markers stay `n/a`.

## Acceptance cases

These are expected outcomes for the affected scope, not claims of executed checks.

| Case | Observable acceptance |
| --- | --- |
| Business write | Authorized transition returns the agreed wire result and all related state commits; failure of the second write leaves neither write committed. |
| Normal alternative | A DB-only action using a correct lock and bounded two-query read is accepted without extra cache/source modules or version annotations. |
| Core failure | A valid token targeting another tenant is denied without mutation; concurrent same-scope creation leaves one business record; a lost remote-write response is reconciled rather than applied twice. |
| Non-target | An Android lifecycle fix, Ktor client request, or generic Kotlin syntax edit does not acquire server architecture/operations requirements from its extension. |
| Conditional disclosure | A PATCH change reaches API and security; a backfill reaches persistence and recovery; a worker shutdown change reaches runtime and operations. Unrelated branches stay unloaded. |

## Official basis

- [Kotlin server overview](https://kotlinlang.org/docs/server-overview.html) and [visibility](https://kotlinlang.org/docs/visibility-modifiers.html).
- [Kotlin cancellation](https://kotlinlang.org/docs/coroutines-cancellation.html).
- [OpenAPI specification](https://spec.openapis.org/oas/latest.html) and [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html).

Version-sensitive APIs are resolved against the target project, not the latest version implied by a documentation URL.
