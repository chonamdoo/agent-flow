---
name: kotlin-backend-development-guide
description: "Kotlin/JVM server development and review: server coroutines, persistence, operations, and Java-to-Kotlin migration/interoperability, including explicit server build-only migration tasks. Use with confirmed Kotlin server dependencies or an explicit Kotlin server task; not for Java-only changes without migration/interop scope, Android/Compose, KMP or Ktor client-only code, general Kotlin syntax, or Gradle files alone."
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [Kotlin backend, Kotlin server, Kotlin JVM server, mixed JVM server]
requires: [backend-api-contract]
requires_by_architecture:
  clean: [clean-architecture-core]
---

# Kotlin Backend Development

Apply `backend-api-contract` and the selected architecture contract (`clean-architecture-core` in Clean mode), including its full required references in local mode. Confirm the server source scope and actual framework, Kotlin, coroutine, database, and migration versions from project dependencies and configuration. A `.kt` file or Gradle build alone is not server evidence. Keep existing architecture and build choices; for a new service, consider a feature-oriented modular monolith before introducing independent deployment modules. Kotlin `internal` is a compilation-module boundary, not package privacy.

## Architecture-specific boundaries

These role/boundary rules describe Clean mode. In local mode, use the selected contract's ownership and framework-dependency rules instead. `backend-api-contract` owns the architecture-neutral API, authorization, consistency, recovery, operations, and conditional performance-evidence requirements.

- `app-shell` composes dependencies and process lifecycle; `inbound-adapter` owns HTTP/worker input and response schemas; `application` owns actions and consumer-focused ports; `core-domain` owns pure policy; `core-data` owns persistence and outbound integrations. These are semantic roles, not required folders.
- Keep provider SDK DTOs and ORM entities at outbound boundaries; HTTP request/response schemas at inbound boundaries; stable commands/results inside. Map where meaning changes, not to create forwarding types.
- Clock, payment, transaction, and platform ports are valid application dependencies. A DB-only repository needs no invented remote source or cache. Follow the core's explicit architecture choices; a UI state holder's direct repository-interface exception is not permission for controller-to-ORM access. Servers need no UI state or global presentation queue.

## Transactions, concurrency, and cancellation

- A pure action may be wrapped by a transaction adapter/decorator; an intentionally framework-aware application service is an explicit architecture choice, not a domain-policy exception. Resolve the real caller and transaction owner under the selected contract.
- Keep request work in its request lifetime and durable worker work in an owned application lifetime. Propagate cancellation, including through broad exception handlers; cancellation is not an ordinary 500/retry signal. Essential payment/publication work cannot be an untracked launch after responding.
- `suspend` does not make JDBC/JPA nonblocking. Bound blocking concurrency and pool demand. Execute a blocking transaction with coherent connection/thread ownership; moving individual calls to `withContext(IO)` does not propagate a thread-bound transaction.

## Conditional details

Read only the branch being changed:

- HTTP serialization, validation, authorization, or pagination: [API and security](references/api-security.md).
- Database consistency, schema evolution, idempotency, or durable events: [Persistence and recovery](references/persistence-recovery.md).
- Coroutine execution, workers, readiness, or shutdown: [Runtime and operations](references/runtime-operations.md).
- Java-to-Kotlin server migration or a changed Java/Kotlin JVM boundary, including explicit server build-only migration work: [Java/Kotlin interoperability](references/java-kotlin-interop.md). Ordinary Java-only changes and non-server Kotlin/Gradle work do not activate this branch.
- Confirmed Spring server behavior: `spring-boot-development-guide`; confirmed Ktor **server** behavior: `ktor-development-guide`. Neither framework is installed by invoking this guide.

## Evidence and review

Follow `backend-api-contract` for shared evidence and review calibration. For coroutine or blocking execution changes, distinguish request/worker lifetime, dispatcher and connection ownership, cancellation propagation, and persistent state after the actual failure from static reasoning. Framework-specific transaction/query details belong to the matching framework guide.

Accept an existing safe lock, a bounded two-query read, a pure transaction-decorated action, and a DB-only adapter under the selected architecture rather than requiring a preferred annotation or module.

## Acceptance cases

These are expected outcomes for the affected scope, not claims of executed checks.

| Case | Observable acceptance |
| --- | --- |
| Coroutine boundary | Request cancellation reaches owned work; blocking database execution preserves transaction/connection ownership and the agreed persistent result. |
| Normal alternative | A DB-only action using a correct lock and bounded two-query read is accepted without extra cache/source modules or version annotations. |
| Core failure | A dispatcher hop that loses a thread-bound transaction or an untracked launch of required payment work is identified with its actual failure sequence. |
| Non-target | An Android lifecycle fix, Ktor client request, or generic Kotlin syntax edit does not acquire server architecture/operations requirements from its extension. |
| Conditional disclosure | A PATCH change reaches API and security; a backfill reaches persistence and recovery; a worker shutdown change reaches runtime and operations; a server JVM migration reaches Java/Kotlin interoperability. Unrelated branches stay unloaded. |

## Official basis

- [Kotlin server overview](https://kotlinlang.org/docs/server-overview.html) and [visibility](https://kotlinlang.org/docs/visibility-modifiers.html).
- [Kotlin cancellation](https://kotlinlang.org/docs/coroutines-cancellation.html).

Version-sensitive APIs are resolved against the target project, not the latest version implied by a documentation URL.
