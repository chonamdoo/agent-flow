# Review Angle: Transactions (Spring)

Check transaction boundaries, rollback behavior, retries, and consistency
across adapters.

## What to verify

1. **Business transaction boundary**
   - Judge the unit of work and actual interception, not an annotation's layer address. A pure application action may use an adapter/decorator or programmatic transaction; a framework-aware application service is an explicit architecture choice. Repository transactions are valid but may not cover a multi-repository action.
   - In proxy mode, calls must cross the configured proxy. Spring 6.0+ class proxies support protected/package-visible methods by default; interface proxies require public interface methods. Check private/final methods, Kotlin proxyability, `publicMethodsOnly`, and weaving against the installed version.
   - `readOnly` is a driver/provider hint, not universal write prevention or guaranteed query-plan improvement. Evaluate required consistency and flush behavior rather than requiring it on every read.

2. **Rollback rules**
   - Default rollback covers `RuntimeException` and `Error`, not checked exceptions. Evaluate effective per-transaction rollback/noRollback rules and global configuration; Spring 6.2+ supports `rollbackOn=ALL_EXCEPTIONS`.
   - Check caught/translated exceptions and rollback-only behavior; annotation presence does not establish the final commit outcome.

3. **Propagation**
   - `REQUIRES_NEW` used deliberately (e.g., audit logs that must persist
     even when outer tx rolls back).
   - Self-invocation skips new proxy advice; it does not erase an existing outer transaction. Change the boundary only if required propagation/rollback semantics are lost; self-injection is not a universal remedy.

4. **External calls inside transaction**
   - Assess connection/lock occupancy, timeout, and recovery when remote I/O occurs inside a transaction. Use the smallest safe boundary; do not add a saga to DB-only work.
   - Publishing before DB rollback can leave a phantom event; publishing after commit can be lost on crash. `@TransactionalEventListener(AFTER_COMMIT)` couples phases but is not crash-safe delivery. Required delivery needs durable intent/publication plus replay and consumer deduplication; best-effort listeners need no compulsory outbox.
   - A DB rollback cannot reverse an external payment/message. Lost write responses require idempotency/reconciliation, not blind retry.

5. **Concurrency**
   - Choose scoped constraints, conditional updates, isolation, locks, or versions for the actual invariant. `@Version` detects stale entity updates/deletes, not duplicate inserts, arbitrary multi-row invariants, or remote effects.
   - Surface conflicts or retry at a safe business boundary after reloading/revalidating state. A correct existing lock without `@Version` is valid.

6. **Evidence**
   - Use active authorized gates and existing evidence of the real call boundary's rollback/commit state; no additional command or test suite is required by this angle.
   - Test-managed `@Transactional` rollback can hide application commit failures and after-commit listeners. Distinguish observed execution from static risk or unexecuted scenarios.

## Official references

- [Spring transaction interception and rollback versions](https://docs.spring.io/spring-framework/reference/data-access/transaction/declarative/annotations.html).
- [Spring Data transactionality and readOnly](https://docs.spring.io/spring-data/jpa/reference/jpa/transactions.html).
- [Transaction-bound events](https://docs.spring.io/spring-framework/reference/data-access/transaction/event.html) and [transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html).
- [Jakarta Persistence Version](https://jakarta.ee/specifications/persistence/3.2/apidocs/jakarta.persistence/jakarta/persistence/version).

## Output format

```text
## Transactions review findings

verdict: approve | request-changes

### Must-fix
- <severity:high> [path:line] <statement>. Risk: <data loss / phantom commit>.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...
```

Cite paths as `path/to/file:line`. Keep total under 150 lines.

Emit exactly one unfenced final verdict line: `verdict: approve` or `verdict: request-changes`.
