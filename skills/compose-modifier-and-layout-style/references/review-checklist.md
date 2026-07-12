# Review checklist

## Apply

- [ ] Layout-emitting reusable composables expose `modifier: Modifier = Modifier`.
- [ ] The parameter follows required values and precedes optional slots.
- [ ] The outermost emitted layout receives the modifier exactly once.
- [ ] Caller modifier is first; component-owned intrinsic behavior follows.
- [ ] Screen-specific size, padding, alignment, and weight live at the caller.
- [ ] Modifier construction uses an inline chain or immutable `val`.
- [ ] Three-or-more-call `modifier` arguments are multiline.
- [ ] A lone conditional is hoisted only when the wrapper has no other role.

## Exclusions and carve-outs

Do not require a modifier for:

- composable accessors that emit no layout, including read-only theme access;
- `@Preview` functions;
- test-only composables whose sole purpose is `setContent` setup;
- rare framework primitives with a required modifier contract already fixed;
- builders or data objects that store modifiers rather than emit UI.

Imperative modifier assembly can remain when it is materially clearer for
procedural animation or generated state. Do not refactor test code merely to
match style when its recomposition or measurement shape is intentional.

## Slot interaction

Reusable components commonly need both caller-owned placement and caller-owned
content. Prefer `@Composable` slots for variable visual regions rather than a
growing list of primitive content parameters and shape flags:

```kotlin
@Composable
fun SettingsRow(
    headlineContent: @Composable () -> Unit,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    leadingContent: (@Composable () -> Unit)? = null,
    trailingContent: (@Composable () -> Unit)? = null,
) { /* ... */ }
```

Use a layout scope receiver only when the slot actually emits into that scope,
such as `@Composable RowScope.() -> Unit`. Nullable optional slots let the
component omit spacing when content is absent; `{}` defaults can leave empty
layout structure.

## Reject during review

- `mod`, `m`, or `wrapperModifier` instead of the conventional name;
- modifier accepted but unused, duplicated, or attached only to a child;
- `Modifier...then(modifier)` ordering;
- a general-purpose component forcing `fillMaxWidth` for every caller;
- claims that private or single-use status alone makes the contract unnecessary;
- conditional hoisting that changes layout, focus, clipping, or animation.
