# Server Contract Guide

Source attribution retained from the supplied bundle: PART 9. Original-source
identity, version, locator, effective date, and author authority are unverified.

Domain services remain unaware of UI components. A composition layer maps domain
data to the finite client catalog, selects screen templates, applies the declared
personalization/experiment context, and chooses supported representations.
Templates own section membership/order; component projection owns reusable typed
presentation, not domain business policy.

## Authorized prototypes and evaluation

Use bundled assets or substitute sources only in an authorized prototype or
isolated evaluation. They are not proof of a production backend. Choose latency,
offline state, and failure scenarios deterministically so each outcome is
observable; random failures are not a required staging recipe. Use the real
backend when the deliverable requires actual composition or patch behavior.

## Screen composition

- Resolve a screen's template, section order, and applicable context.
- Map domain results into supported typed catalog components independently of a
  particular screen's layout.
- Share the screen-level action dictionary across its nodes.
- Keep template-driven reordering a data concern where the adopted schema permits
  it; behavior outside the catalog still requires client capability work.

## Capability negotiation

Discover the actual capability transport, component identifiers, versions, and
baseline policy rather than inventing a request header.

- Explicitly supported representations may be sent only at supported versions.
- Missing capability information selects the documented compatible baseline,
  not an assumption that the client supports every current component.
- A downgrade must transform the entire payload into the fallback's schema and
  event/action contract. Renaming only its type does not establish compatibility.
- If neither the primary nor a valid fallback is supported, omit the unsupported
  section under the agreed fallback policy.

Client unknown-node tolerance does not excuse a server capability violation.
Existing clients can receive richer composition only within capabilities they
actually implement.

## Patch composition

- Return operations and a monotonically ordered `version` under the screen/context
  ordering contract. Do not reuse or decrease that ordering identity.
- When a requested section has no content, emit `REMOVE` rather than an empty section.
- When newly present, emit `UPSERT` with an anchor; align subsequent full reloads
  with the patched membership/order.
- Update templates only in their declared scope. A user-specific or request-local
  patch must not accidentally alter a global template. Determine whether reload
  agreement comes from scoped template updates or reproducible composition.

## Projection and authority

Generality means recombining a finite set of supported representations, not
serializing arbitrary domain objects. Preserve typed content, event references,
and token-only design meaning without copying application business fields or
calculation snippets into the contract.

Use opaque identities and server-owned route selection where the project adopts
them. Opaque identifiers do not authorize access: the client and server still
apply their existing target/scope authorization checks.

## Local development

Resolve the reachable service address from the actual host, emulator, or device
network environment; loopback refers to the execution environment itself. When
cleartext is necessary, permit only the intended development host in debug
builds. Never widen cleartext policy application-wide.
