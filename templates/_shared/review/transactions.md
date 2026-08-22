# Review Angle: Transactions (Spring)

Check transaction boundaries, rollback behavior, retries, and consistency
across adapters.

## What to verify

1. **`@Transactional` placement**
   - On the application/use-case layer, NOT in repository or controller.
   - Public methods only — `@Transactional` on private methods is silently
     ignored (Spring AOP proxy).
   - Read-only operations use `@Transactional(readOnly = true)` to enable
     query plan hints.

2. **Rollback rules**
   - Default rollback is on `RuntimeException`. Checked exceptions need
     `rollbackFor = ...` explicitly.
   - Custom domain exceptions either extend `RuntimeException` or are
     listed in `rollbackFor`.

3. **Propagation**
   - `REQUIRES_NEW` used deliberately (e.g., audit logs that must persist
     even when outer tx rolls back).
   - Self-invocation (`this.doInner()` from within same bean) does NOT
     trigger a new transaction — refactor or inject self-proxy.

4. **External calls inside transaction**
   - HTTP / remote calls inside `@Transactional` hold DB connections open
     and can timeout; extract outside or use saga pattern.
   - Messaging publish inside transaction risks lost message on rollback;
     use transactional outbox or `@TransactionalEventListener`.

5. **Optimistic locking**
   - Entities with concurrent writers carry `@Version`.
   - `OptimisticLockException` handled with retry or surfaced to user.

6. **Test coverage**
   - Integration test asserts rollback on failure path.
   - `@Transactional` on test class understood (rolls back by default).

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
