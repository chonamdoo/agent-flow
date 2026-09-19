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
   - Prefer an atomic update when it cleanly expresses the invariant; a correct existing row lock, optimistic version, advisory lock, `flock`, lease, or `O_EXCL` claim is also valid. Judge its protection and lifecycle rather than requiring a different strategy.
   - Lock identity, ordering, ownership, crash release, and contention behavior are explicit.

5. **Retry/result guarantee and idempotency key**
   - Establish the promised retry/result semantics across all side effects and returned outcomes. Accept demonstrably repetition-safe operations or equivalent atomic/durable business-state guarantees without a separate request-key/result store; show a concrete duplicate/concurrent execution and crash sequence. An HTTP method name or unchanged primary row alone is insufficient.
   - When keys are promised, claim the tenant/operation/key before the irreversible effect under a unique constraint or equivalent atomic claim, bind it to a canonical request fingerprint, and reject mismatched reuse.
   - When prior-result replay is promised, preserve completed outcomes for the supported retry window in a durable record or equivalent durable representation that reproduces that result. Current state alone is insufficient if it cannot recover the promised original outcome.
   - Keep pending/unknown distinct from completed. Recover a crash after external success but before local result recording; a pending flag alone does not prevent duplicate effects.

6. **Unknown write outcome**
   - Timeout, cancellation, connection loss, or a tool error flag is not proof of rollback. Separate the result/error payload from confirmed not-started, not-committed, committed, or unknown effect state.
   - Reconcile operation status, ledger, or durable provider replay before retrying an unknown write. Retry only at a safe unit with an established repetition-safe or equivalent atomic/durable guarantee covering all effects and the promised result; provider call IDs and stream event IDs are not business idempotency keys.

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
