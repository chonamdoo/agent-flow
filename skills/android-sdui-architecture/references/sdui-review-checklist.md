# SDUI Review Checklist

Evidence and verdict rules for the review angle at
`templates/_shared/review/sdui.md`. Source PART labels below are retained from the
supplied bundle; original-source identity, version, locator, effective date, and
author authority are unverified. Consult the linked guides for adopted policy
and their public API references.

Use the marker vocabulary declared in the skill entrypoint and active workflow.
Use `pass` only with evidence, `fail` for a confirmed violation, and `n/a` only
for an inapplicable check. The design-token marker also supports `unverified`.
For other markers, describe missing evidence in prose and leave the check
unresolved rather than inventing a value. Follow the workflow's missing-evidence
or blocked path; do not approve while required evidence is unresolved.
State what was inspected: source inspection is not runtime behavior proof.

## 1. `sdui-design-token-only`

Source attribution: PART 4-4, PART 7-6.

Rule: styling references client-owned tokens. Raw design dimensions and hex colors
are rejected; declared structural ratios/modes are a separate bounded schema class.

Check structured payloads, fixtures, composer output, and validator behavior.
Text search for raw color/dimension patterns finds candidates, not proof. Confirm
the actual styling fields reject raw values and accept valid tokens, and token
resolution stays at the client design-system boundary.

Fail on confirmed raw styling or permissive validation. Tolerant client fallbacks
are resilience, not server permission. Missing schema/validator evidence is
`unverified`. See [design-token-guide.md](design-token-guide.md).

## 2. `sdui-room-ssot-scope`

Source attribution: PART 1-8, PART 6-3, PART 6-6.

Rule: restart-durable rendered state originates in observable storage, even with
one observer. Refresh is a command; durable data reaches UI through observation,
not its direct network result. The historical marker name does not mandate Room.

Trace storage provenance, repository boundaries, refresh writes, and rendered
state. Name which values require durability and which are transient. Confirm
missing-cache, cold-load failure, and recoverable parse-failure states are visible.
No fixed DAO, return signature, table name, or `Flow` recipe is required.

Fail when durable content bypasses storage or UI consumes transport DTOs directly.
Transient progress, effects, command results, clock offsets, and scroll cursors
may remain outside screen storage. Credentials/media belong in their appropriate
stores, not the SDUI screen store. See
[offline-ssot-data-guide.md](offline-ssot-data-guide.md).

## 3. `sdui-action-finite-vocabulary`

Source attribution: PART 1-3, PART 5-4, PART 7-4.

Rule: typed actions and expressions form a finite catalog with exhaustive
execution and safe unknown-action tolerance. Targets and operations remain inside
client-approved authority.

Compare payloads against the target client's declared catalog/capabilities;
inspect interpreter branches, bounded binding resolution, unresolved-value
handling, and operation/destination authorization. API-name searches can locate
code, but are not the acceptance test.

Fail on dynamic evaluation, missing execution handling, authority escalation, or
server payloads that violate declared target capabilities. An older client safely
ignoring an unknown future action is a valid compatibility behavior, not itself a
missing-counterpart defect. See [json-schema-guide.md](json-schema-guide.md).

## 4. `sdui-parse-depth-limit`

Source attribution: PART 7-3, PART 7-6.

Rule: parsing is bounded before field interpretation and child recursion, and
budget exhaustion degrades safely.

Trace every recursive branch and confirm the adopted depth/work budget advances
and terminates without throwing. Judge observable bounds and fallback outcomes,
not a required constant name. Fail on an unbounded or throwing path. An unchanged
parser can still be in review scope; absence of inspection is not `n/a`.

## 5. `sdui-unknown-node-fallback`

Source attribution: PART 7-3, PART 7-2.

Rule: unsupported types and malformed known nodes both degrade without crashing.

Inspect all field reads, including `id`/`type`, fallback identity, child parsing,
and release rendering. Confirm malformed primitive shapes cannot throw before
protection begins and fallback identity remains suitable for repeat rendering.
Fail if either fault lacks a safe fallback or the fallback itself throws. No
particular `when`, catch wrapper, or generated UUID is required.

## 6. `sdui-list-key-contenttype`

Source attribution: PART 7-2, PART 7-6, PART 1-2.

Rule: lazy items have stable identity keys and the screen-level lazy list has
meaningful `contentType` grouping.

Trace identity from parsing through patching and rendering, including moves and
insertions. Inspect content grouping and actual layout constraints. Public lazy
APIs are useful search hints, not mandatory call text.

Fail on missing/unstable/index-based identity or missing screen-level content
grouping. Confirm scroll preservation rather than assuming keys alone guarantee
it. Bounded same-axis nested scrolling is valid; unbounded constraints are not.

## 7. `sdui-accessibility-field`

Source attribution: PART 7-6; public facts from
[Compose semantics](https://developer.android.com/develop/ui/compose/accessibility/semantics).

Rule: every interaction is perceivable and operable through the actual semantics
tree, with an accessible name and appropriate role, state, and actions.

Inspect payload semantics, rendering, built-in component behavior, and merged
semantics. Correct text or merged semantics can supply the accessible name without
an extra label field. A role alone does not provide a name; field presence alone
does not prove the renderer applies it.

Fail on an inaccessible interaction or ignored required semantics. Decorative or
duplicate hiding is valid, but hiding the only operable function is not an
exemption. Record runtime assistive-technology evidence separately from static review.

## 8. `sdui-semantic-promotion`

Source attribution: PART 2-4.

Rule: justify promotion using repeated maintenance cost, payload/rendering cost,
accessibility, and design stability under
[hybrid-boundary-guide.md](hybrid-boundary-guide.md), not a fixed occurrence count.

Inspect relevant templates, fixtures, and the semantic catalog when the change
requires an ownership/promotion decision. Name the observed repeated shapes and
costs, then evaluate whether a new capability is justified. Fail on a concrete
violation of the adopted ownership decision, not merely a third occurrence.
If a required corpus survey is unavailable, report that missing evidence in prose
and leave the decision unresolved. `n/a` means the decision is genuinely outside scope.

## 9. `sdui-udf-contract`

Source attribution: PART 11; delivery semantics follow
`android-clean-presentation-architecture`.

Rule: UI input travels upward, durable state downward, and transient effects use
a deliberate single-consumer/no-replay policy with explicit lifetimes. Renderers
remain stateless; route/AppShell wiring owns navigation and platform effects.

Inspect direction rather than suffixes, the owner/collector lifecycle, buffering,
cancellation and consumption, and renderer acquisition/collection calls. A channel
name cannot prove no loss: critical results require durable state when cancellation
or process loss must not lose them. Preserve the presentation skill's three
SDUI-specific exceptions without extending them to ordinary native screens.

Fail when durable results rely on lossy transient delivery, effects violate their
declared consumption contract, or renderers own screen state or navigation.
