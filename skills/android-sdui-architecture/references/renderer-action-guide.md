# Renderer and Action Guide

Source attribution retained from the supplied bundle: PART 7, PART 3-3,
PART 1-2, and PART 8-6. Original-source identity, version, locator, effective
date, and author authority are unverified. Public API references below support
specific platform facts, not every adopted design decision.

## App-shell boundary

If failure makes the app unusable, native code owns the fallback. Server-authored
content may fail locally only when the remaining surface stays usable.

- Native code owns the app-bar/tab-bar skeleton and essential navigation.
- Server config may control supported shell composition, not remove the skeleton.
- Content regions may be SDUI when their failure is recoverable.
- Native inputs retain keyboard, IME, and focus ownership; independently rendered
  results may be SDUI.

Resolve shell configuration from a valid server response, then usable cached
configuration, then bundled safe defaults. The final native fallback is mandatory
and does not depend on a particular storage engine or constant name.

## Modifier mapping

Apply the adopted ordering contract consistently at a shared rendering boundary;
see [ui-node-model-guide.md](ui-node-model-guide.md). Node-local chains must not
silently change rendering semantics. Resolve design values through client token
resolvers; structural values must satisfy the declared schema constraints.

`weight` has meaning only in the appropriate `RowScope` or `ColumnScope`; ignore
it outside a supported parent scope under the supplied contract. Preserve that
meaning without prescribing a specific scope-passing helper. Reuse resolved
shapes for clipping, background, and border rather than recomputing them.

## Recursive rendering

- Lazy items have stable identity keys; the screen-level lazy list has meaningful
  `contentType` grouping. Preserve identities across patches and repeat parses.
- Containers recursively render supported child types with valid constraints.
  Avoid unbounded same-axis scroll nesting. Bounded-size nesting is permitted by
  [Compose lists](https://developer.android.com/develop/ui/compose/lists#avoid-nesting-scrollable);
  do not replace large virtualized grids with eager rows solely to satisfy a ban.
- A node with `visibility == GONE` produces no composed content.
- Apply declared interactions and semantics consistently to all node variants.
- Unsupported-node fallbacks render without throwing; debug placeholders are
  acceptable, while release fallback may omit unusable content.

## Parser safety

Enforce the declared recursion/resource budget before interpreting node fields
or descending into children. Every child traversal advances the depth budget.
Budget exhaustion returns a safe fallback rather than a stack overflow or exception.

Treat unsupported types and malformed known types as distinct diagnostics with
safe fallback behavior. Include malformed `id` and `type` field shapes in the
protected parsing boundary; primitive extraction must not throw before fallback
handling begins. Parse all other fields defensively as well.

Define deterministic identity for malformed nodes or safely omit them according
to the adopted contract. Random identity on every parse undermines lazy keys and
patch targeting. The fallback itself must not fail during rendering.

## Action interpreter

- Use a finite typed catalog and exhaustive execution. No reflection or dynamic
  dispatch that grants arbitrary behavior from raw server strings.
- Unknown future actions and explicit no-ops safely do nothing on an older client.
  Server capability validation remains a separate obligation.
- Sequences execute in order; conditions choose one branch using only declared
  operators within the complexity bound.
- Register expected results before emitting navigation.
- Navigation, dismissal, scrolling, toasts, and snackbars leave as transient UI
  effects. Route/AppShell wiring performs them; the interpreter owns neither
  `NavController` nor `Context`.
- Refresh commands use repository contracts; durable updates arrive through
  storage observation.
- Resolve only allowlisted, typed binding references from the declared context.
  Define unresolved/mismatched-value behavior; no `eval`, scripting engine, or
  arbitrary member traversal. See [json-schema-guide.md](json-schema-guide.md)
  for binding roots and destination/operation authority checks.

## Cross-screen results

Keep the existing caller instance and its state alive while a destination returns
a result. A deep link is not inherently wrong, but recreating the caller instead
of returning can lose state or create unwanted back-stack entries.

1. The caller registers the expected result before navigation.
2. The destination performs its work without learning the caller's implementation.
3. Dismissal returns a typed result under the registered key.
4. The existing caller consumes the result once and executes the registered action,
   which may refresh sections and conditionally scroll.

Use the project's actual navigation/result API rather than copying a NavController
recipe into a Navigation3 host. Preserve caller scroll, images, and usable content.
Patch operations can add or update sections without rebuilding the whole screen.

| Concern | Required behavior |
|---|---|
| Scroll preservation | Stable section identity and layout-state handling preserve the viewed content across patches |
| Insertion above viewport | Compensate or animate position changes according to the interaction contract |
| Refresh failure | Keep usable existing content; classify local/global errors rather than swallowing AppShell recovery |
| Duplicate results | Define semantic duplicate identity and consumption order; cancel prior work only when doing so is safe |
| Patch ordering | Discard versions older than stored state, independently of duplicate-result policy |

For global/session failures, use `android-appshell-error-handling` and
`app-shell-error-contract`; preserving old content is not permission to hide a
required recovery action.

## Countdown lifetime and time basis

Derive remaining time from the declared trusted reference time and elapsed-time
measurement, not a freely adjustable wall clock alone. A response `Date` header
or clock offset does not by itself prove tamper resistance; establish reference
trust, freshness, and resynchronization under the actual protocol. Lifecycle-bound
ticks restart when their deadline changes and stop at zero or owner disposal.

## Risk and accessibility checks

- Bound parse depth and work before recursion.
- Use truthful immutable/stability contracts and identity/content grouping; an
  annotation alone does not prove recomposition performance.
- Reject raw styling server-side while retaining client token fallbacks.
- Debug-only tree inspection can aid diagnosis without becoming a production dependency.
- Inspect actual accessibility semantics, not just JSON field presence.

[Compose semantics](https://developer.android.com/develop/ui/compose/accessibility/semantics)
separates accessible names, roles, state, and actions. Map the declared semantics
into the real tree, preserving correct built-in text and merged semantics.
Decorative/duplicate hiding is valid; hiding the sole operable function is not
an exemption. Every interaction remains perceivable and operable to assistive
technology, whether rendered from primitives or a semantic component.
