---
name: kotlin-backend-hexagonal-architecture
description: "Kotlin backend hexagonal (ports-and-adapters) architecture for Spring Boot and Ktor services. It is the selected architecture contract for the spring and ktor profiles in stack mode, and an adjunct beside a project's own contract in local mode. Use when designing, implementing, or reviewing module dependencies, domain/application ports, adapters, composition and wiring, or transaction ownership; not for Clean-mode work, generic Kotlin/Gradle, Java-only services, or Android."
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [Kotlin hexagonal, Kotlin backend ports and adapters, Kotlin backend module dependency, Spring Boot ports and adapters, Spring hexagonal architecture, Ktor hexagonal architecture, Ktor ports and adapters, Ktor application module wiring, Exposed transaction boundary]
architecture_modes: [stack, local]
---

# Kotlin Backend Hexagonal Architecture

Ports-and-adapters boundaries for Kotlin backends on Spring Boot or Ktor.

- In **stack mode** this skill is the selected architecture contract: apply its rules, valid alternatives, and non-targets to design, implementation, and review.
- In **local mode** it is an adjunct: the project's selected contract and its required references govern, and this skill never overrides a placement, dependency, or wiring rule that contract settles.
- It does not apply in Clean mode.

`kotlin-backend-development-guide`, `spring-boot-development-guide`, and `ktor-development-guide` cover version-specific Kotlin and framework behavior; `backend-api-contract` covers HTTP, authorization, recovery, and operations. This skill adds hexagonal boundary decisions, not a new project module map.

## Discover the project's contract first

1. Read the governing contract: in local mode, the selected project contract and its required references; in stack mode, this skill plus the architecture decisions the repository already records (ADRs, design documents). Then read settings/build declarations, existing source imports and composition (Spring configuration or Ktor application modules), and applicable architecture checks. Identify the actual source roots, modules or packages, contexts, and dependency direction. A module can contain several semantic roles; do not manufacture modules, package names, or tests from the example architecture below.
2. Establish **where this project places each port** before adding one. Some projects put provided/required ports with application actions; others put repository contracts in domain. Keep one coherent convention for each role. If documentation and working code disagree, inspect current callers and explicit decisions; report the conflict instead of silently introducing a second placement or treating this skill as authority to move working ports. In stack mode with no established convention, choose one placement and record it as a design decision. A missing map remains an evidence gap, not proof of compliance.
3. For each changed boundary, trace both Gradle project dependencies (including `api` exposure and runtime wiring) **and** source-level imports/signatures. In a single-module project inspect package references and runtime composition instead of requiring artificial Gradle subprojects. Finish this step when every affected edge and port owner has evidence, or explicitly record what cannot be established.

## Direction and ownership

A useful *semantic* flow, not a prescribed folder tree: driving HTTP/job adapter → provided application action → domain policy; application → required port ← persistence/integration adapter; the runnable composition root (Spring configuration, or the Ktor application module and `main`) binds the action and concrete adapters. Let domain models own business invariants without Spring, Ktor, ORM, transport, serialization, or provider SDK dependencies. Application orchestrates intent and names its needed capabilities; it depends on the project's approved domain/application abstractions rather than concrete persistence or outbound clients. Keep project dependency edges acyclic and policy independent of adapters; inspect whether an `api` dependency exposes types in a consumer-facing contract and otherwise prefer `implementation` under the build's actual plugin conventions. Do not mistake `implementation` for a guarantee that source imports respect the architecture.

- A **provided/inbound port**, when a separate interface serves a real seam, expresses an action offered to controllers, routes, jobs, or other contexts. A **required/outbound port** expresses a specific capability the action needs (persistence, external service, clock, transaction scope, etc.). Put them in the domain or application role the governing contract selects, never in the implementing adapter just because it has the only implementation. No interface is required for each class or use case when the contract and callers have no seam requiring one.
- Across contexts, use the owning context's offered contract instead of reaching into its concrete action or repository implementation, when contexts are separate in the project. Port parameters, results, and errors use stable domain/application values; HTTP request/response types, Ktor `ApplicationCall` and routing types, Spring Data repositories, ORM entities, Exposed tables and DAO entities, provider DTOs, and raw client errors remain at adapter boundaries.
- Driving adapters (Spring controllers and listeners, Ktor routes, jobs) translate requests/events to actions and results to responses. Driven adapters implement required capabilities, own ORM/transport models and failure translation, and map persistence/provider representations to the promised values. Keep mappers value-focused, with no I/O or business-policy decisions. Avoid identity-copy models, mandatory mapper/source classes, or new cache modules where no semantic boundary needs them.

## Framework composition

### Spring Boot

Construct beans in the project's Spring composition root, with explicit dependencies (constructor or factory method) and adapter bindings. A pure application action can be registered through `@Bean` or decorated transactionally. A framework-aware application service is a valid alternative only when the local contract, or in stack mode a recorded design decision, deliberately allows it. Domain policy stays framework-free in either case. Stereotype annotations and Kotlin proxyability are choices constrained by actual wiring, not universal naming or `open` mandates.

### Ktor

- Application modules (`Application` extension functions loaded by `embeddedServer` or the `ktor.application.modules` configuration property) are the composition edge; their `routing` blocks and installed plugins (authentication, content negotiation, status pages) are driving-adapter concerns. A route handler reads `call` input, invokes the provided action, and maps results and errors to responses; it does not open persistence transactions, query tables, or call provider SDKs itself.
- Wire actions and adapters explicitly with constructor parameters, binding them in `main` or a module; passing dependencies as module function parameters is Ktor's simplest documented form. When the project already adopted a container — Ktor's dependency injection plugin (`ktor-server-di`), Koin, or another — register adapter implementations for ports there and resolve them at the module or route edge. Never mandate or introduce a container for this skill alone, and do not resolve from a container inside application or domain code.
- Keep `io.ktor` imports out of domain and application roles. Blocking I/O belongs in driven adapters that choose an appropriate coroutine context, not in request handlers or domain policy.

## Transaction and effect boundary

Choose the business consistency unit before placing a transaction boundary: which reads/writes must succeed together, which store owns them, what failures should roll back, and who invokes the boundary. A local transaction is not a distributed commit across databases or external APIs; record the required compensation/retry or durable publication design when atomicity across those effects matters. Avoid holding a database transaction open across outbound provider calls.

### Spring Boot

Transactional application entry, a transaction-decorated pure action, or a correctly bounded repository operation can each be valid under the governing contract; the caller must go through the Spring bean boundary. Inspect the configured manager, proxy/weaving mode, invocation path, Kotlin class/method proxyability, propagation, and effective rollback rules; a self-call cannot start new proxy advice, but does not cancel an outer transaction. Do not mandate a named manager in every project: with multiple independent managers, resolve the correct one explicitly according to the target configuration. For reactive/coroutine or JPA-specific decisions, follow the matching Spring guide and installed versions rather than transplanting a blocking transaction assumption.

### Ktor

Ktor does not intercept methods to open transactions; the boundary is the explicit block of the adopted persistence library, so place it deliberately.

- With Exposed, `transaction {}` executes synchronously and blocks the current thread. In coroutine code on a JDBC driver, use the installed version's suspend transaction API (`suspendTransaction` in Exposed 1.x; the deprecated `newSuspendedTransaction` in earlier versions) and run blocking JDBC work on a dispatcher meant for blocking I/O, such as wrapping the block in `withContext(Dispatchers.IO)`. Reactive drivers use the `exposed-r2dbc` suspend transaction instead. Check the installed Exposed version before naming an API.
- Nested Exposed transaction blocks share the outer transaction by default, so an inner rollback also rolls back the outer work; independent nested blocks require the database's `useNestedTransactions` setting (savepoints). Do not assume an inner block commits separately.
- A single-store operation can own its transaction inside the driven adapter. When one action must commit several repository writes together, express the unit as an application-owned required port (for example, a transaction runner or unit of work) implemented by the persistence adapter, so application code does not import Exposed or JDBC types.
- Other persistence libraries follow their own documented transaction API under the same placement rule.

## Acceptance cases

These are expected decisions, **not executed checks**:

| Case | Observable decision/evidence |
| --- | --- |
| Success | An inbound adapter (Spring controller or Ktor route) calls the owning action, its required port is implemented by an outbound adapter, domain imports remain pure, build/source edges follow the governing map, and a two-write failure rolls back the intended store's unit through the effective boundary: the Spring bean proxy, or one Exposed transaction opened by the adapter or transaction port. |
| Violation | An action imports a persistence entity or calls a concrete client; a controller accesses the Spring Data repository; a self-called method expected to start `REQUIRES_NEW` never crosses its proxy; a Ktor route opens `transaction {}` and queries tables directly; or blocking JDBC work runs on a request coroutine without the suspend transaction/dispatcher decision. Identify the exact edge/call path and the broken rule. |
| Valid exception | The project keeps repository contracts in domain while application action ports live in application, or deliberately permits framework-aware application services; accept each with evidence from its contract or recorded decision. One store's correctly bounded repository transaction needs no ceremonial outer boundary. A small Ktor service wires dependencies through module parameters without any container, or uses its adopted Koin/Ktor DI container only at the module edge. |
| Non-target | A Gradle-only change with no confirmed Kotlin server architecture scope, a Java-only service, an Android app, and Clean-mode architecture do not acquire this contract. In local mode, a rule the project contract settles differently follows that contract. |

## Basis and limits

- Example module/package trees, transaction-manager names, dates, and exceptions from any one project are **not** reusable requirements or evidence of another project's state. Port placement differs between projects and can conflict between a project's documents and its code; resolve placement from the target's authoritative contract and actual code.
- [Gradle project dependencies](https://docs.gradle.org/current/userguide/declaring_dependencies_basics.html#sec:project-dependencies) and [Java Library `api` vs `implementation`](https://docs.gradle.org/current/userguide/java_library_plugin.html#sec:java_library_separation) establish build exposure semantics, not a universal architecture layout.
- [Spring constructor injection](https://docs.spring.io/spring-framework/reference/core/beans/annotation-config/autowired.html), [`@Transactional` interception and manager selection](https://docs.spring.io/spring-framework/reference/data-access/transaction/declarative/annotations.html), and [rollback rules](https://docs.spring.io/spring-framework/reference/data-access/transaction/declarative/rolling-back.html) establish framework behavior; match their guidance to the target project's Spring version and configuration.
- [Ktor modules](https://ktor.io/docs/server-modules.html) document module loading and passing dependencies as parameters, application attributes, or the DI plugin; [Ktor dependency injection](https://ktor.io/docs/server-dependency-injection.html) and [Koin for Ktor](https://insert-koin.io/docs/reference/koin-ktor/ktor/) are container options, not requirements. [Ktor routing](https://ktor.io/docs/server-routing.html) is the request-handling plugin.
- [Exposed transactions](https://www.jetbrains.com/help/exposed/transactions.html) document blocking `transaction()`, `suspendTransaction()`, and nested-transaction behavior; the [Exposed 1.0 migration guide](https://www.jetbrains.com/help/exposed/migration-guide-1-0-0.html) documents the deprecation of `newSuspendedTransaction()` and wrapping `suspendTransaction()` in `withContext()`; the [Ktor database tutorial](https://ktor.io/docs/server-integrate-database.html) switches Exposed JDBC work to `Dispatchers.IO`. Match them to the installed Ktor and Exposed versions.
