# Persistence and Recovery

Read when changing database consistency, migrations/backfills, idempotent writes, or required event delivery. Use the selected database/driver/migration version's semantics; this reference does not select a persistence library.

## Consistency boundary

Name the invariant and a concrete competing/crash sequence. An atomic conditional UPDATE may be simplest for one-row inventory; a correct pessimistic lock, serializable unit, or version check is also valid. Include affected-row checks and public conflict semantics. Unique constraints need the business scope, often including tenant. Versioned entity updates do not enforce uniqueness of new rows or aggregate totals across rows. Keep locks ordered and transaction duration bounded; retry deadlocks/serialization failures only around a safely replayable unit.

Fetch based on consumer needs and actual SQL/cardinality. A page query followed by bounded association fetching can preserve pagination better than a collection fetch join. A DB-only adapter needs neither a cache nor a remote source. Treat ORM session/lazy-loading and bulk mutation lifecycle as library-specific contracts.

## Schema evolution

Identify who runs the versioned migration and how concurrent deployment attempts are serialized. Review upgrade from an existing schema and populated data, not just creation. For rolling changes, expand compatible schema first, deploy compatible readers/writers, backfill in bounded restartable chunks, then contract after old users drain. Choose null/default/dual-write behavior intentionally and define recovery from partial backfill. A rollback of application binaries does not automatically undo irreversible data changes. Use the migration tool and database's actual transactional-DDL and lock semantics; do not promise universally transactional migrations.

## Idempotency and publication

A durable claim needs atomic uniqueness over tenant/operation/key, request fingerprint comparison, and a state/result record. Define retention and pending-owner recovery: a crashed claimant must not permanently block work or cause another worker to duplicate an unknown effect. Completed duplicate requests return the prior outcome; changed payloads under one key conflict.

For external effects, record a stable operation identity before attempting the effect when possible, use the provider's idempotency contract, and reconcile a crash after provider success but before local completion. A local `pending` flag alone is not duplicate-effect prevention. Distinguish confirmed not-applied, confirmed applied, and unknown.

If delivery is required, persist intent with the business commit through an outbox, supported durable publication registry, or equivalent mechanism. Relay failures and consumer ack loss need replay/dedup; publishing before commit risks phantom events, publishing after commit without durable intent risks loss. Choose CDC only when its ordering/retention/operational contract fits. Best-effort notifications need not be upgraded to durable messaging.

## Official sources

- [PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html) and [constraints](https://www.postgresql.org/docs/current/ddl-constraints.html) are examples of database-specific semantics, not a required database.
- [Flyway migrations](https://documentation.red-gate.com/flyway/flyway-concepts/migrations): resolve the installed migration tool rather than introducing Flyway.
- [AWS transactional outbox pattern](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html).
- [Spring Modulith event publication registry](https://docs.spring.io/spring-modulith/reference/events.html): an option only for a project already choosing compatible Spring Modulith facilities.
