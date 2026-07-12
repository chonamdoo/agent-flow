---
name: spring-development-guide
description: Reviews and guides Spring and Spring Boot service changes. Use when modifying Java or Kotlin Spring controllers, services, transactions, persistence, configuration, or tests.
---

# Spring Development Guide

Use this only for Spring or Spring Boot code in the changed scope.

## Quick start

1. Confirm Spring markers in `pom.xml`, Gradle files, configuration, or annotations.
2. Follow the repository's Java/Kotlin version, build tool, package boundaries, and test style.
3. Trace request validation, transaction ownership, persistence queries, and failure mapping.
4. Run the narrow Gradle or Maven test that exercises the changed behavior.

## Write

- Keep controllers at the transport boundary; move application rules into services or use cases.
- Put transaction boundaries on application operations and avoid remote calls inside long database transactions.
- Validate external input before domain use and map domain failures deliberately at the API boundary.
- Avoid lazy-loading surprises, unbounded queries, and per-row database calls.
- Keep secrets out of checked-in configuration and logs.
- Prefer constructor injection and immutable dependencies.

## Review

- Block broken authorization, unsafe deserialization, transaction leaks, N+1 queries, unbounded reads, and swallowed exceptions.
- Check rollback behavior, idempotency, concurrency, schema assumptions, and integration-test coverage where relevant.
- Treat annotation or style preferences as non-blocking unless they create a runtime or project-rule defect.
