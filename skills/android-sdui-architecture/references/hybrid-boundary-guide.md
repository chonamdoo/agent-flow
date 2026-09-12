# Hybrid Boundary Guide

Source attribution retained from the supplied bundle: PART 2 and PART 11's
 design-decision section. The original title, version, locator, effective date,
and author authority are unavailable; these are adopted design policies, not
independently verified source claims.

## Two different burdens

Release burden includes store review, updates, and old-client fragmentation.
Server burden includes payload size, authoring, composition cost, and renderer
complexity. Removing release work by making every design change a backend task
only transfers the burden.

Separate frequently changing composition and content from stable interaction
and design. Determine actual change frequency from the product; no release
cadence is inherent to an SDUI approach.

## Client capability boundary

| Approach | Change that requires client capability work |
|---|---|
| Native | A new compiled UI or interaction; remotely supplied content can change without a release |
| Component catalog | A component or supported component behavior not in the installed catalog |
| Hybrid | A new primitive, semantic component, or behavior outside the installed schema |
| Full layout tree | A primitive, interpreter capability, or platform integration not already supported |
| WebView | A change beyond the installed web runtime, native bridge, or deployment contract; it is not universally release-free |

Hybrid permits new combinations of existing primitives without creating a new
catalog component. It does not create new client capabilities from server data.

## Decision axes

Compare layout-tree and semantic-component ownership using:

- Frequency and cost of changing the composition versus stable design.
- Repeated authoring and maintenance cost across real surfaces.
- Measured payload size, parsing cost, rendering cost, and scrolling workload.
- Accessibility complexity that a semantic component can consistently encapsulate.
- Design stability and the cost of versioning a new client component.

Accessibility is required for both approaches. A one-off banner is not exempt,
and a repeated component is not accessible merely because the client owns it.
Do not infer payload bytes from an item count without measurement.

Promote a repeated layout into a semantic component when demonstrated maintenance,
performance, accessibility, or design-consistency benefits justify the capability
and release cost. Repetition is evidence, not an automatic count threshold.

## Mechanisms that can reduce both burdens

1. **Slots:** the client fixes component structure and declares named extension
   points. The server fills only supported slots with allowed content. New slot
   behavior outside that contract still requires client work.
2. **Capability negotiation:** clients advertise supported representations and
   versions using the adopted transport contract. The server chooses supported
   payloads or valid fallbacks; see [bff-contract-guide.md](bff-contract-guide.md)
   when implementing absent-capability and downgrade behavior.
3. **Composition layer:** map domain data into the finite catalog; templates and
   editorial tooling own section composition without pushing UI into domain services.
4. **Remote assets:** approved asset locations can permit asset replacement without
   recompilation, subject to existing formats, platform behavior, and access policy.

## Limits

- Bound conditional expression complexity in the schema. Move logic to native
  capabilities or server composition when it becomes hard to reason about,
  validate, or execute within that bound; do not add arbitrary evaluation.
- Checkout, payment, and complex forms stay native.
- Schema versioning and old-client support remain ongoing costs.
