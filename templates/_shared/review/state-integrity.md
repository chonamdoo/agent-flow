# Review Angle: State Integrity

Review persistent-state mutations for concrete concurrency, crash-safety, and retry defects. Style differences are non-blocking.

## What to verify

1. **Race condition**
   - Identify every read-check-write sequence and show the concurrent interleaving that can invalidate its premise.
   - Prefer a conditional atomic update when one statement can enforce the invariant.

2. **Partial write**
   - For multi-step publication, name the exact crash point and the divergent state left behind.
   - Require a transaction, durable intent with idempotent replay, or an explicit compensating action.

3. **Transaction and rollback**
   - All state changes that form one consistency boundary commit or roll back together.
   - Do not assume a database rollback can undo an external payment, message, or network side effect; require provider idempotency, outbox, saga, or compensation as applicable.

4. **Row lock or equivalent lease**
   - Use a row lock, optimistic version, advisory lock, `flock`, lease, or `O_EXCL` claim only when an atomic update cannot express the invariant.
   - Lock identity, ordering, ownership, crash release, and contention behavior are explicit.

5. **Idempotency key**
   - Claim the key before the irreversible side effect under a unique constraint or equivalent atomic claim.
   - Bind the key to a request hash and persist the completed result so a retry returns it instead of applying the effect again.

## Blocking calibration

A `request-changes` verdict requires concrete evidence at `path:line`: the competing or retried sequence, the invariant it breaks, and the resulting data loss, duplicate side effect, or contract violation. Do not block on naming, preferred syntax, hypothetical scale, or style differences alone.

## Output format

```text
## State integrity review findings

### Must-fix
- <severity:high> [path:line] <trigger sequence>. Impact: <broken invariant>. Fix: <smallest safe boundary>.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...

## Overall
verdict: approve | request-changes
```

Cite paths as `path/to/file:line`. Emit exactly one unfenced final verdict line: `verdict: approve` or `verdict: request-changes`.
