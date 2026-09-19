---
name: ktor-development-guide
description: "Ktor server development and review of Application composition, routing, authentication, serialization/plugins, engine lifecycle, or database execution. Use with Ktor server dependencies or an explicit Ktor server task; exclude Ktor HttpClient-only Android/KMP work, Spring-only services, and generic Kotlin edits."
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [Ktor server, Ktor Application, Ktor routing, Ktor server plugin, Ktor server authentication]
requires: [kotlin-backend-development-guide]
---

# Ktor Server Development

Apply `kotlin-backend-development-guide`, its `backend-api-contract` dependency, and the selected architecture contract (Clean core only in Clean mode; full selected local contract and required references in local mode). The common contract owns API, authorization, consistency, recovery, operations, and conditional performance evidence. Confirm **server** artifacts, actual Ktor version, engine, serialization plugin, database library/driver, DI approach, and lifecycle configuration. `HttpClient` in an Android/KMP app does not activate this guide.

## Composition and entry points

- An Application module is a function that assembles routes/plugins/services, not a required Gradle module or deployment unit. Preserve the project's constructor/parameter injection, attributes, or supported DI plugin; do not install a container to satisfy an example.
- Place route policy and dependency ownership according to the selected architecture. In Clean mode, Application composition owns resources and application-lifetime workers; routes own input decoding, trusted principal extraction, and response/error mapping; application actions own policy and ports. Keep `ApplicationCall`, ORM rows, provider SDK types, and serialization DTOs out of pure business policy. In local mode, follow the selected contract's ownership and framework-boundary rules instead.
- Identify plugin installation scope and pipeline behavior. An authentication provider validates identity; verify `authenticate` coverage and enforce the common authorization contract beyond that identity check.
- Apply the common wire/error/validation contract across `ContentNegotiation`, request validation, route mapping, and `StatusPages`. Avoid double responses; plugin validation does not establish domain invariants or object authorization.

## Execution, state, and lifecycle

- Request work is structured under the call lifetime. Owned application workers have explicit failure and shutdown handling. Do not detach mandatory side effects into a launch after returning success. Propagate cancellation through generic exception/status mapping; it is not evidence of non-commit.
- Choose a transaction API from the actual DB library. Ktor routing does not create database transactions. JDBC/JPA is blocking despite a suspending route; bound work and pool usage, and execute the **whole** blocking transaction with coherent connection/thread ownership. Do not scatter transaction calls across dispatchers or share a transaction/session among concurrent child tasks unless the library explicitly supports it.
- Register resource ownership and shutdown ordering for the chosen engine/version, following the common readiness/drain contract. Bound engine-specific request/queue/timeout settings.
- Run migrations through the selected deployment owner, not an accidental per-request or per-instance startup race. Apply the common migration and job/consumer contracts only where those capabilities exist.

## Conditional Ktor details

- Routing, Authentication, ContentNegotiation, RequestValidation, or StatusPages changes: [Server plugins](references/server-plugins.md).
- Database library, blocking/reactive execution, engine, application workers, or shutdown changes: [Data and lifecycle](references/data-lifecycle.md).

Read the relevant Kotlin reference only when its branch is affected. Resolve APIs against the target version; documentation examples are not a scaffold or an upgrade mandate.

## Evidence and review

Use only active authorized gates. Exercise the changed route's status/body and tenant-safe persistent state when authorized; relevant cases are malformed input, authenticated cross-tenant access, domain conflict, second-write rollback, request cancellation, and shutdown with accepted work. A `testApplication` result demonstrates an application boundary, not production engine scheduling, actual database isolation, or socket draining; name those remaining evidence boundaries.

Reuse State Integrity and I/O Safety angles plus architecture review. Spring proxy and JPA-only findings do not apply to Ktor without those technologies. Accept plain module-function injection, a safe lock, DB-only persistence, and bounded multiple queries; report concrete contract failures rather than missing framework annotations. Use existing markers and server presentation `n/a` only.

## Acceptance cases

These are expected outcomes for the affected scope, not claims of executed checks.

| Case | Observable acceptance |
| --- | --- |
| Server boundary | The installed route/plugins return the agreed malformed-input, unauthorized, and conflict responses while only authorized successful writes change persistent state. |
| Normal alternative | A module function with injected DB-only persistence is accepted without a DI container, Spring annotation, or separate deployment module. |
| Core failure | Cross-tenant access is denied; failure of the second statement rolls back its transaction; shutdown drains or durably hands off accepted work before closing its pool. |
| Non-target | Ktor HttpClient-only Android/KMP work does not activate server/plugin/engine requirements. |
| Conditional disclosure | A StatusPages mapping change reaches server plugins; a JDBC dispatcher or engine-stop change reaches data and lifecycle. Route-host evidence is not mislabeled production engine/DB proof. |

## Official basis

- [Ktor Application modules](https://ktor.io/docs/server-modules.html).
- [Ktor authentication](https://ktor.io/docs/server-auth.html), [request validation](https://ktor.io/docs/server-request-validation.html), and [StatusPages](https://ktor.io/docs/server-status-pages.html).
- [Ktor server configuration](https://ktor.io/docs/server-create-and-configure.html) and [server testing](https://ktor.io/docs/server-testing.html).
