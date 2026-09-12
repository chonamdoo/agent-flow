# UiNode Model Guide

Source attribution retained from the supplied bundle: PART 4-1 through PART 4-3.
Original-source identity, version, locator, effective date, and author authority
are unverified. The field roles below are the bundle's adopted contract, not a
required Kotlin class scaffold.

## Screen contract

- `screenId` identifies the screen within the declared storage/request scope.
- `schemaVersion` guards parser compatibility; `version` orders responses/patches.
- `root` contains the typed node tree; `actions` is the screen-level action dictionary.
- `resultContract` is optional and declares a result only for screens that return one.
- `cachePolicy` defines expiry and stale-while-revalidate behavior; `fetchedAt`
  supplies freshness metadata. Resolve values from the actual cache contract,
  not a copied timeout.

## One node contract, three tiers

Every node has stable `id`, layout/modifier semantics, visibility, event bindings,
and accessibility semantics. Define required fields and defaults once at parsing:
missing optional styling becomes a neutral modifier, not repeated renderer checks.

- **Layout containers:** represent column, row, overlay, grid, pager, and lazy
  list structure. Own typed children, token-based spacing, and any bounded
  structural parameters declared by the schema.
- **Primitive leaves:** represent text, images, buttons, icons, spacing, dividers,
  and countdown display. Own only the typed data their rendering contract needs.
- **Semantic components:** own stable design, interaction, performance, and
  accessibility for a reusable concept; carry a typed payload rather than an
  unstructured property bag.
- **Fallback:** retain safe identity and diagnostic type information for unsupported
  or malformed input. Rendering the fallback must not crash.

Choose tiers using [hybrid-boundary-guide.md](hybrid-boundary-guide.md), including
its evidence-based promotion rule. A semantic type adds client release cost;
repeated use alone does not dictate a fixed promotion threshold.

## Modifier field semantics

- `padding` and `margin` hold semantic inset tokens.
- `width` and `height` distinguish supported sizing modes from token-based design
  dimensions. Raw dp is not an approved styling value.
- `aspectRatio` and `weight` are structural ratios, not design dimensions. Accept
  them only as constrained schema values with declared valid ranges and scopes.
- `background`, `shape`, `border`, and `elevation` reference client-owned design
  tokens; structured variants must remain within the declared schema.
- `alpha` and `clip` are bounded opacity and clipping controls only where the
  schema explicitly allows them. They do not authorize arbitrary styling.
- Missing optional fields use defined neutral defaults. Invalid values follow the
  parser/fallback policy rather than reaching Compose unchecked.

Token ownership and malformed-token behavior are defined in
[design-token-guide.md](design-token-guide.md).

Models must actually obey immutable value/equality contracts. Use inferred
stability or truthful annotations under the presentation skill; do not require
`@Immutable` solely to claim performance. [Strong skipping](https://developer.android.com/develop/ui/compose/performance/stability/strongskipping)
can skip restartable composables with unstable parameters. Judge compiler
version/mode, stability reports, and measured behavior rather than claiming one
unstable modifier necessarily defeats skipping throughout the tree.

## Modifier application order

Modifier order is part of rendering compatibility. The supplied contract orders:

```text
margin -> size/aspectRatio -> weight -> clip -> background -> border
       -> elevation -> alpha -> padding
```

Apply the adopted order consistently at a shared rendering boundary. Changing it
requires an intentional rendering-contract change, not a node-local exception.
No specific helper name or parent-scope plumbing implementation is required.
