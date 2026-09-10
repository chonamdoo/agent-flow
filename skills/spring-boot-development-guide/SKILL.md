---
name: spring-boot-development-guide
description: "Spring Boot Java/Kotlin server development and review involving MVC/WebFlux, Security, transactions, JPA/JDBC/R2DBC, Batch, or service operations. Use with confirmed Spring dependencies or an explicit Spring server task; not for Gradle/Kotlin alone, Android, Ktor-only servers, or unrelated language edits."
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [Spring Boot, Spring MVC, Spring WebFlux, Spring Security, Spring transaction, Spring JPA, Spring R2DBC, Spring Batch, 스프링 부트, 스프링 서버, 스프링 트랜잭션]
requires: [clean-architecture-core]
---

# Spring Boot Development

Apply `clean-architecture-core`. For **Kotlin server code only**, also apply `kotlin-backend-development-guide`; Java-only work does not require the Kotlin guide. Resolve the actual Boot BOM, Framework/Security/Data versions, MVC or WebFlux stack, database driver, transaction manager, proxy mode, and migration setup before choosing APIs. Preserve a healthy MVC/JDBC application; Spring is not a reason to convert it to reactive execution.

## Business and transport contract

- Composition owns beans and process lifecycle; inbound adapters own authentication extraction, request/response mapping, and wire schemas; application actions own orchestration and ports; domain policy stays framework-free. Provider DTOs and ORM entities remain outbound details. These are semantic roles, not a folder scaffold.
- Choose the business consistency boundary before annotation placement. Pure application actions can be transaction-decorated at an adapter edge. Framework-aware application services are valid only as an explicit architecture choice; they do not authorize Spring/JPA imports in pure domain policy. A valid repository transaction is not itself a layering defect.
- Preserve one wire/OpenAPI source of truth: HTTP status/error envelope, enum behavior, missing/null and PATCH semantics, stable bounded pagination, and compatibility. Separate input validation, current-state application policy, domain invariants, and database constraints; keep sensitive implementation details out of responses.
- Trust the authenticated principal/tenant, not body ownership/admin fields. Enforce endpoint/action/object/field authorization, including list/count/export scope, each bulk item, and worker execution. Security filter or method access alone does not prove tenant-safe data access.

## Consistency and execution

- Evaluate **actual interception**, not annotation presence. In proxy mode, calls must cross the configured proxy; self-invocation skips new advice but does not erase an existing outer transaction. Private/final methods and Kotlin proxyability require attention. Spring 6.0+ class proxies can intercept protected/package-visible methods by default; interface proxies require public interface methods. Check `publicMethodsOnly` and weaving before judging visibility.
- Default rollback covers `RuntimeException` **and `Error`**, not checked exceptions. Evaluate method rules and global configuration: Spring 6.2+ supports `rollbackOn=ALL_EXCEPTIONS`; transaction-specific rules override defaults. An exception caught and converted to success may prevent rollback regardless of annotation.
- `readOnly` is a driver/provider hint, not a universal write prohibition or query-plan improvement guarantee. Check the consistency and flush behavior the operation actually requires.
- Pick unique constraints, conditional updates, isolation, locks, or entity versions for the concrete race. `@Version` detects stale entity updates/deletes, not duplicate insertion, arbitrary multi-row invariants, or external effects. Retry conflicts only at a safe business boundary after reloading/revalidating state.
- Match imperative thread-bound and reactive context-bound transactions to their manager and driver. JDBC/JPA remains blocking, including in `suspend` methods. Bound executor/pool demand; arbitrary dispatcher hops do not preserve thread-local transactions. Preserve request/worker lifetime and cancellation; a cancelled caller does not prove rollback of an already attempted write.
- Remote calls can hold locks/connections and create outcomes a DB rollback cannot reverse. For retriable effects, durably bind tenant/operation/idempotency key to a request fingerprint and replayable result. Preserve pending/unknown state; reconcile lost responses before any safe retry rather than repeating a possibly committed write.
- Publication before rollback can leave a phantom event; publication after commit can be lost on crash. `@TransactionalEventListener(AFTER_COMMIT)` supplies phase coupling, not durable delivery. Required delivery needs durable intent/publication plus replay and consumer deduplication. Best-effort after-commit work and DB-only CRUD need no compulsory outbox/saga.

## Deployment and operations

Version migrations, establish one execution owner, and cover previous-schema upgrade, rolling old/new readers/writers, and bounded resumable backfill. Delay destructive contraction until incompatible instances are gone. Limit bodies/pages, queues, concurrency, timeouts, and retries; redact secrets/PII. Keep operational endpoints least-privileged. Separate liveness from traffic readiness and drain accepted work before closing resources. For actual Batch jobs or consumers, define restart/checkpoint, claims, ack timing, deduplication, and poison-message recovery rather than adding messaging to every service.

## Conditional Spring details

- MVC/Security binding, validation, or error responses: [Web and security](references/web-security.md).
- Imperative transactions, JPA entities, fetch strategy, or bulk operations: [Transactions and JPA](references/transactions-jpa.md).
- WebFlux/R2DBC or coroutine/reactive transaction context: [Reactive data](references/reactive-data.md).
- Actuator, startup/shutdown, migration execution, Batch, or consumers: [Operations](references/operations.md).

Each reference uses the installed version's official contract; it is not an upgrade instruction.

## Evidence and review

Reuse the active Transactions, N+1 Queries, State Integrity, I/O Safety, and Architecture Design angles only where applicable. Observe the real bean call path, manager and effective rollback rules, final DB state after a second-write failure, and commit/publication behavior when relevant. Test-managed rollback can hide application commit and after-commit listeners. Query findings require actual SQL/cardinality evidence or an explicitly labeled static risk, not an invented query count; a bounded multi-query read may be better than one collection join.

Use only active authorized gates; no additional build/test commands are imposed here. Record what was observed and what remains unexecuted. Accept a transaction-decorated pure action, correct repository transaction, adequate outer transaction with a helper self-call, safe pessimistic lock without `@Version`, and a best-effort listener with no durable delivery requirement. Add no skill-specific completion markers; server UI markers remain `n/a`.

## Acceptance cases

These are expected outcomes for the affected scope, not claims of executed checks.

| Case | Observable acceptance |
| --- | --- |
| Business transaction | Through the real bean boundary, a second-write failure rolls back the whole declared unit under effective rollback configuration. |
| Normal alternative | A pure decorated action, supported non-public class-proxy method, or sufficient outer transaction with a helper self-call is accepted; safe locking need not add `@Version`. |
| Core failure | A self-called REQUIRES_NEW that never receives its intended advice is detected; required publication survives a crash through durable replay rather than relying on AFTER_COMMIT alone. |
| Non-target | Java-only Spring work does not load Kotlin common; Ktor-only or Android work does not receive Spring proxy/JPA rules. |
| Conditional disclosure | JPA bulk deletion reaches transactions/JPA to preserve callbacks and context semantics; WebFlux cancellation reaches reactive data; Actuator exposure reaches operations. |

## Official basis

- [Transactional interception and rollback versions](https://docs.spring.io/spring-framework/reference/data-access/transaction/declarative/annotations.html).
- [Spring Data JPA transactionality and readOnly](https://docs.spring.io/spring-data/jpa/reference/jpa/transactions.html).
- [Transaction-bound events](https://docs.spring.io/spring-framework/reference/data-access/transaction/event.html) and [transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html).
- [Jakarta Persistence Version](https://jakarta.ee/specifications/persistence/3.2/apidocs/jakarta.persistence/jakarta/persistence/version).
