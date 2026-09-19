---
name: spring-boot-development-guide
description: "Spring Boot Java/Kotlin server development and review involving MVC/WebFlux, Security, transactions, JPA/JDBC/R2DBC, Batch, or service operations. Use with confirmed Spring dependencies or an explicit Spring server task; not for Gradle/Kotlin alone, Android, Ktor-only servers, or unrelated language edits."
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [Spring Boot, Spring MVC, Spring WebFlux, Spring Security, Spring transaction, Spring JPA, Spring R2DBC, Spring Batch, Spring server]
requires: [backend-api-contract]
requires_by_architecture:
  clean: [clean-architecture-core]
---

# Spring Boot Development

Apply `backend-api-contract` and the selected architecture contract (`clean-architecture-core` in Clean mode), including its full required references in local mode. For **Kotlin server code or explicit Java-to-Kotlin server migration/interoperability work**, including build-only migration, also apply `kotlin-backend-development-guide`; ordinary Java-only work does not require it. Resolve the actual Boot BOM, Framework/Security/Data versions, MVC or WebFlux stack, database driver, transaction manager, proxy mode, and migration setup before choosing APIs. Preserve a healthy MVC/JDBC application; Spring is not a reason to convert it to reactive execution.

## Business and transport contract

- In Clean mode, composition owns beans and process lifecycle; inbound adapters own authentication extraction, request/response mapping, and wire schemas; application actions own orchestration and ports; domain policy stays framework-free. Provider DTOs and ORM entities remain outbound details. These are semantic roles, not a folder scaffold. In local mode, apply the selected contract's ownership and dependency rules instead.
- Choose the business consistency boundary before annotation placement. Pure application actions can be transaction-decorated at an adapter edge. In Clean mode, framework-aware application services are valid only as an explicit architecture choice and do not authorize Spring/JPA imports in pure domain policy; local mode follows its selected framework-boundary rules. A valid repository transaction is not itself a layering defect.
- `backend-api-contract` owns the shared wire, authorization, invariants, recovery, deployment/operations, and conditional performance-evidence requirements. Spring Security filter or method access alone does not prove tenant-safe data access.

## Consistency and execution

- Evaluate **actual interception**, not annotation presence. In proxy mode, calls must cross the configured proxy; self-invocation skips new advice but does not erase an existing outer transaction. Private/final methods and Kotlin proxyability require attention. Spring 6.0+ class proxies can intercept protected/package-visible methods by default; interface proxies require public interface methods. Check `publicMethodsOnly` and weaving before judging visibility.
- Default rollback covers `RuntimeException` **and `Error`**, not checked exceptions. Evaluate method rules and global configuration: Spring 6.2+ supports `rollbackOn=ALL_EXCEPTIONS`; transaction-specific rules override defaults. An exception caught and converted to success may prevent rollback regardless of annotation.
- `readOnly` is a driver/provider hint, not a universal write prohibition or query-plan improvement guarantee. Check the consistency and flush behavior the operation actually requires.
- `@Version` detects stale entity updates/deletes, not duplicate insertion, arbitrary multi-row invariants, or external effects. Apply the common contract's concurrency alternatives to the actual JPA unit; retry conflicts only at a safe business boundary after reloading/revalidating state.
- Match imperative thread-bound and reactive context-bound transactions to their manager and driver. JDBC/JPA remains blocking, including in `suspend` methods. Bound executor/pool demand; arbitrary dispatcher hops do not preserve thread-local transactions. Preserve request/worker lifetime and cancellation; a cancelled caller does not prove rollback of an already attempted write.
- `@TransactionalEventListener(AFTER_COMMIT)` supplies phase coupling, not durable delivery. Apply the common contract's required-versus-best-effort delivery distinction; an after-commit listener alone cannot satisfy a crash-safe publication guarantee.

## Conditional Spring details

- MVC/Security binding, validation, or error responses: [Web and security](references/web-security.md).
- Imperative transactions, JPA entities, fetch strategy, or bulk operations: [Transactions and JPA](references/transactions-jpa.md).
- WebFlux/R2DBC or coroutine/reactive transaction context: [Reactive data](references/reactive-data.md).
- Actuator, startup/shutdown, migration execution, Batch, or consumers: [Operations](references/operations.md).

Each reference uses the installed version's official contract; it is not an upgrade instruction.

## Evidence and review

Reuse the active Transactions, N+1 Queries, State Integrity, I/O Safety, and Architecture Design angles only where applicable. Observe the real bean call path, manager and effective rollback rules, final DB state after a second-write failure, and commit/publication behavior when relevant. Test-managed rollback can hide application commit and after-commit listeners. Query findings require actual SQL/cardinality evidence or an explicitly labeled static risk, not an invented query count; a bounded multi-query read may be better than one collection join.

Follow `backend-api-contract` for shared evidence and review calibration. Accept a transaction-decorated pure action, correct repository transaction, adequate outer transaction with a helper self-call, safe pessimistic lock without `@Version`, and a best-effort listener with no durable delivery requirement.

## Acceptance cases

These are expected outcomes for the affected scope, not claims of executed checks.

| Case | Observable acceptance |
| --- | --- |
| Business transaction | Through the real bean boundary, a second-write failure rolls back the whole declared unit under effective rollback configuration. |
| Normal alternative | A pure decorated action, supported non-public class-proxy method, or sufficient outer transaction with a helper self-call is accepted; safe locking need not add `@Version`. |
| Core failure | A self-called REQUIRES_NEW that never receives its intended advice is detected; required publication survives a crash through durable replay rather than relying on AFTER_COMMIT alone. |
| Non-target | Ordinary Java-only Spring work does not load the Kotlin guide; explicit Java-to-Kotlin server migration does. Ktor-only or Android work does not receive Spring proxy/JPA rules. |
| Conditional disclosure | JPA bulk deletion reaches transactions/JPA to preserve callbacks and context semantics; WebFlux cancellation reaches reactive data; Actuator exposure reaches operations. |

## Official basis

- [Transactional interception and rollback versions](https://docs.spring.io/spring-framework/reference/data-access/transaction/declarative/annotations.html).
- [Spring Data JPA transactionality and readOnly](https://docs.spring.io/spring-data/jpa/reference/jpa/transactions.html).
- [Transaction-bound events](https://docs.spring.io/spring-framework/reference/data-access/transaction/event.html) and [transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html).
- [Jakarta Persistence Version](https://jakarta.ee/specifications/persistence/3.2/apidocs/jakarta.persistence/jakarta/persistence/version).
