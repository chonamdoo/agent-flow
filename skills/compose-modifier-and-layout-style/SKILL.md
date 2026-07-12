---
name: compose-modifier-and-layout-style
description: Enforces reusable Jetpack Compose modifier contracts, readable modifier chains, and correct conditional layout placement. Use when writing or reviewing composables that emit layout, accept or apply Modifier, hard-code root sizing or padding, build modifier chains imperatively, or wrap a lone conditional in a layout.
---

# Compose Modifier and Layout Style

Keep placement decisions with the caller and structural decisions with the
composable. Apply only to composables that emit layout; previews, test-only
entry points, and read-only accessors do not need a `modifier` parameter.

## Quick start

1. Put `modifier: Modifier = Modifier` after required parameters and before
   optional content slots.
2. Apply that modifier to the root layout, before intrinsic modifiers owned by
   the component.
3. Move caller-specific width, height, padding, and placement to the call site.
4. Build each modifier as one `val` or inline fluent expression.
5. Format a `modifier` chain with three or more calls one call per line.
6. Hoist a lone `if` outside a layout only when the container has no independent
   semantics, arguments, or sibling content.

```kotlin
@Composable
fun Avatar(
    url: String,
    modifier: Modifier = Modifier,
) {
    Image(
        painter = rememberAsyncImagePainter(url),
        contentDescription = null,
        modifier = modifier.clip(CircleShape).size(48.dp),
    )
}
```

## Progressive references

- Read [modifier-contract.md](references/modifier-contract.md) when declaring,
  applying, ordering, or reviewing a modifier parameter.
- Read [chains-and-conditionals.md](references/chains-and-conditionals.md) for
  fluent chains, conditional segments, formatting, and layout hoisting.
- Read [review-checklist.md](references/review-checklist.md) for exclusions,
  carve-outs, slot interaction, and a compact review pass.

## Verification

- Compile every changed call site after moving placement modifiers outward.
- Verify the root receives the caller modifier exactly once.
- Preview or test both branches of changed conditional layout.
- Reject style-only changes that alter measurement, hit targets, clipping,
  semantics, focus, or animation behavior.
