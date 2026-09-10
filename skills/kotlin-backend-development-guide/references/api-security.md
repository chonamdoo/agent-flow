# API and Security

Read when changing HTTP contracts, serialization, validation, authorization, or pagination. The parent guide owns the common rules; this reference resolves the affected boundary, not a new architecture.

## Contract decisions

- Find whether OpenAPI is generated from code or drives generation; change that source and its real consumers. Check the installed OpenAPI version before using schema keywords. Preserve clients that send omitted fields, explicit null, unknown enum values, or old payloads according to the declared compatibility policy.
- PATCH requires a three-way decision where meaningful: absent keeps the value, null clears it, supplied value replaces it. A nullable Kotlin property with a null default can erase this distinction. Use the existing presence-aware representation. If the endpoint uses JSON Merge Patch or JSON Patch, retain that media type's semantics rather than inventing another merge algorithm.
- Bound list and bulk input sizes. Use stable ordering with a unique tie-breaker; bind opaque cursors to the relevant filter/sort scope and reapply authorization on each request. Offset pagination can be valid for the product's consistency/size needs; cursors do not automatically create snapshot consistency. Scope counts and aggregate metadata as well as rows.
- Preserve the existing error envelope and status policy. RFC 9457 is an option for a new contract, not a migration instruction. Public errors should allow useful correction without revealing another tenant's resource or raw diagnostics.

## Authority and validation

Trace trusted identity from verified credential/session through tenant resolution to data access. A tenant selector is only a request until membership and permission are checked. Authorize object relationships and fields before applying updates; whitelist assignable fields rather than binding ORM entities from the body. Apply scope to exports, search, aggregate results, each bulk target, and worker-originated actions. Jobs need an explicit service/delegated identity and current authority policy, not a fabricated interactive user.

Validate format at ingress, state-dependent decisions in the application, invariants in pure policy, and race-sensitive uniqueness/references in the database. A preflight check improves error UX but cannot replace a constraint. Translate the real constraint/conflict into the public contract without retrying an unsafe effect.

## Counterexamples for the affected path

A valid JWT plus another tenant's object must be denied. A correctly scoped list must not leak other tenants through total counts. Omitted PATCH fields must not be cleared by DTO defaults. A direct confirmation request that skips UI steps must still enforce the state transition. These are scenarios to cover with authorized evidence, not mandatory new tests.

## Official sources

- [OpenAPI](https://spec.openapis.org/oas/latest.html): use the project's schema version.
- [RFC 5789 PATCH](https://www.rfc-editor.org/rfc/rfc5789.html), [RFC 7396 Merge Patch](https://www.rfc-editor.org/rfc/rfc7396.html), [RFC 6902 JSON Patch](https://www.rfc-editor.org/rfc/rfc6902.html).
- [RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457.html).
- [OWASP API Security](https://owasp.org/API-Security/editions/2023/en/0x11-t10/): object, property, function authorization and resource consumption.
