# Spring Transactions and JPA

Read for imperative transactions, JPA entities, queries, or bulk mutation. Identify the installed Spring/Data/JPA provider versions and transaction manager. The parent guide owns authority, outcome, and durability meaning; this reference explains framework-specific evidence.

## Interception and transaction participation

Trace the caller through the initialized bean proxy, effective annotation/programmatic boundary, manager, propagation, isolation, and rollback rules. Spring 6.0+ supports protected/package-visible methods on class proxies by default; JDK interface proxies need public interface methods. `publicMethodsOnly`, Kotlin final methods/classes, and AspectJ weaving change which rules apply. Self-invocation bypasses proxy advice: an inner REQUIRES_NEW or rollback rule may not apply, while an existing outer transaction continues. Refactor the boundary or use an appropriate existing programmatic/decorator approach when needed; self-injection is not the universal fix.

Default RuntimeException/Error rollback can be changed by transaction-specific rules and, on Spring 6.2+, global ALL_EXCEPTIONS. Caught exceptions, rollback-only status, propagation, and flush/commit timing affect the outcome. A save call returning is not necessarily a successful commit. REQUIRES_NEW uses an independent physical transaction and may demand another connection; check pool capacity and whether inner commit surviving outer rollback is intended.

Spring Data inherited CRUD methods have transaction settings; declared query methods are not automatically transactional by that fact alone. Repository transactions are valid, but separate repository calls need a wider boundary if one business action must be atomic. `readOnly` is a provider/driver hint and may affect flush behavior, not universal write prevention or guaranteed plan optimization.

## Fetching and mutation

- JPA default to-one eagerness does not guarantee a join, and to-many laziness does not prove an N+1 on every path. Inspect actual association traversal, provider SQL, cardinality, and serialization lifetime.
- Select fetch joins, entity graphs, projections, batching, or bounded secondary queries by consumer needs. Collection fetch plus pagination can duplicate rows or force in-memory paging. A page query and bounded association query may be correct and cheaper than one join.
- Page count semantics and generated SQL decide whether a separate countQuery is needed. Slice avoids a total-count contract; it is not a streaming API. Streaming needs its actual cursor/resource lifetime and bounds.
- Read-only work may legitimately load entities; projections earn their place when they reduce unnecessary data/materialization. Never force a mapping layer that merely copies fields.
- `saveAll` does not guarantee JDBC batching. Batching depends on provider settings, statement shape, identifier generation, and flush behavior. Bulk DML/deletes can bypass entity callbacks/cascades/version checks and leave the persistence context stale; preserve those semantics before replacing per-entity operations.
- `@Version` protects stale entity updates/deletes. Use scoped constraints, conditional updates, or locks for other invariants. Handle conflict at the safe action boundary; replaying only the failed statement can violate prior decisions.

## Commit and publication evidence

Observe the final database state through the real call boundary. Test-managed rollback can hide application commit failure or after-commit listeners. A best-effort AFTER_COMMIT listener is valid; required durable delivery needs persisted publication and replay. A later listener exception does not undo the already committed business transaction.

Use actual SQL count/cardinality and workload size for observed performance claims; otherwise label the exact static fan-out risk. Existing authorized evidence is acceptable, and this guide adds no mandatory test or logging library.

## Official sources

- [Transactional annotations](https://docs.spring.io/spring-framework/reference/data-access/transaction/declarative/annotations.html), [propagation](https://docs.spring.io/spring-framework/reference/data-access/transaction/declarative/tx-propagation.html), [transaction events](https://docs.spring.io/spring-framework/reference/data-access/transaction/event.html).
- [Spring Data transactionality](https://docs.spring.io/spring-data/jpa/reference/jpa/transactions.html) and [JpaRepository bulk API](https://docs.spring.io/spring-data/jpa/docs/current/api/org/springframework/data/jpa/repository/JpaRepository.html).
- [Jakarta Persistence 3.2 Version](https://jakarta.ee/specifications/persistence/3.2/apidocs/jakarta.persistence/jakarta/persistence/version): use the project's persistence version for API details.
