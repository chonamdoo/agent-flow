---
name: backend-api-contract
description: "Architecture-neutral backend API development and review: wire compatibility, authorization, business invariants, retries and recovery, migrations, and operations. Use for confirmed server/API work; not for client-only networking, Python CLI tasks, or non-server language/build edits."
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [backend API, server API]
---

# Backend API Contract

Apply these observable behavior requirements at the responsibility boundaries of the selected architecture contract. This skill selects no folders, layers, DI container, runtime, database, or messaging infrastructure and does not require Clean Architecture. Preserve the existing `clean`/`local`/`pending` selection: local uses its selected root and required references; a missing selected local contract is an error, not permission to fall back to Clean. Pending permits only work that needs no new structural decision; it cannot decide ownership, dependency direction, persistence boundaries, or wiring.

Before changing an entry point, identify its actor/tenant, allowed action and state transition, wire result, consistency boundary, and failure/recovery behavior. Resolve actual framework, driver, database, and deployment versions from the project. Apply framework-specific guidance only to the boundary using that framework.

## Wire contract, authorization, and invariants

- Establish one OpenAPI/wire source of truth and preserve status codes, error envelope, enum behavior, compatibility, missing versus explicit `null`, PATCH retain/clear/replace semantics, and bounded pagination with stable ordering/tie-breaking. Do not silently replace an existing envelope with Problem Details.
- Derive principal, tenant, roles, and credentials from a trusted authentication boundary. Authorize endpoint/action, object ownership, and readable/writable fields. Apply the same scope to list filters and counts, bulk items, exports, and workers; claims of ownership, tenant membership, or administrator status in model input or request bodies are not authority.
- Separate malformed input and field validation from state-dependent policy, domain invariants, and database constraints. Direct API callers may skip every UI step. A valid token or schema does not authorize a state transition.
- Map expected conflicts, missing resources, and validation failures to the existing public contract. Preserve diagnostic causes internally with redaction; do not leak SQL, stack traces, credentials, or cross-tenant existence through error details.

## Consistency and retry results

- Name the writes that must commit or roll back together, the real caller, and the transaction owner. Enforce the concrete race with the appropriate unique constraint, conditional atomic update, isolation, lock, or version check. Preflight `exists` checks do not prevent concurrent inserts; one entity version does not protect a multi-row invariant or remote effect. Define the public conflict and safely replayable unit.
- For redeliverable writes, establish the promised retry/result semantics across **all** side effects and returned outcomes. A demonstrably repetition-safe operation or equivalent atomic/durable business-state guarantee can satisfy them without a separate request-key/result store. Show a concrete duplicate/concurrent execution and crash sequence, including secondary effects and response meaning; an HTTP method name or unchanged primary row alone is not proof.
- When idempotency keys are promised, bind tenant, operation, key, and canonical request fingerprint through an atomic claim before the irreversible effect; reject mismatched reuse. When prior-result replay is promised, preserve the outcome for the supported retry window using a durable key/result record or an equivalent durable representation that can reproduce that result. Current business state is insufficient if it cannot recover the promised original outcome. A crashed claimant needs recovery, not a permanently blocking claim or a second unknown effect.
- Keep pending and unknown distinct from confirmed completion. Timeout, cancellation, or a lost response after a database/remote write does not prove rollback. Before repeating a possibly completed effect, reconcile its outcome or establish a safe-replay guarantee covering every effect and the promised result; a local rollback cannot undo a payment/message. A pending flag alone is not duplicate-effect prevention.
- Use durable event intent/publication and replay/dedup only when delivery is required. A callback after commit is not crash-safe delivery. For actual jobs/consumers, define checkpoint/restart, claim ownership, ack timing, bounded retry, and poison-message/DLQ recovery. Best-effort notifications and DB-only CRUD do not require an outbox, saga, or job framework.

## Deployment and operations

- Use versioned migrations with an identified execution owner, upgrades from the previous schema, old/new instance compatibility during rolling deployment, and bounded resumable backfills. Plan destructive changes only after incompatible readers/writers have drained; an empty database startup is insufficient evidence.
- Bound request/body/page sizes, queues, concurrency, retries, and operation time. Protect secrets and PII in responses, logs, metrics, and configuration. Keep operational endpoints least-privileged.
- Separate liveness from readiness: an unavailable dependency need not trigger process restart. Drain requests/workers before closing their pools/clients; report when accepted durable work remains recoverable rather than completed.

## Evidence for the changed boundary

Use only active, authorized project/profile gates and the requested scope; this skill adds no command, mandatory suite, workflow phase, or completion marker. Cover the changed boundary with actual HTTP/worker results and resulting persistent state when execution is authorized. Reuse the active state-integrity and I/O-safety angles. Record the applicable condition, concrete failure sequence, evidence, and reason for non-applicability where relevant in existing artifacts, not a new required document.

Distinguish static reasoning, observed execution, and unexecuted scenarios. Existing evidence may suffice; do not add tests merely to satisfy a checklist. Report concrete contract violations, not the absence of a preferred annotation, module, store, or framework. Server-only presentation markers remain `n/a`.

### Conditional performance evidence

Apply this section only to an optimization claim or a material change to a hot path, contention, throughput, or resource bounds. Identify the changed path, workload/data shape, concurrency, and relevant environment. For database claims, use the actual database and isolation level; distinguish statement execution time from lock and connection occupancy until transaction commit. Account for remote work inside transactions and relevant serialized sections, including response serialization cost where it matters.

Use relevant latency and throughput evidence separated by successful, rejected/conflicting, and failed outcomes; fast rejection must not masquerade as faster successful work. State measured results and measurement limits separately from static risks. Unmeasured remains unmeasured, with the affected claim left unverified. This is not a universal p99 target, new benchmark framework, or mandatory performance CI gate.

## Official basis

- [OpenAPI specification](https://spec.openapis.org/oas/latest.html) and [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html).
- [HTTP idempotent methods](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2): protocol semantics do not establish an application's complete side-effect/result guarantee.
- [PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html): an example of database-specific semantics, not a database choice.

Resolve version-sensitive details against the target project rather than assuming the latest documentation describes its runtime.
