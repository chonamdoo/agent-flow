# Modifier chains and conditional layout

## One fluent expression

Prefer an immutable chain over stepwise reassignment:

```kotlin
val itemModifier = Modifier
    .fillMaxWidth()
    .padding(horizontal = 16.dp)
    .background(MaterialTheme.colorScheme.surface)

Box(modifier = itemModifier) { /* ... */ }
```

Avoid `var m = Modifier` followed by repeated `m = m...`. It obscures the
modifier order and makes later mutation easy. Short chains of one or two calls
can stay inline. For an argument named `modifier`, format three or more calls on
separate lines.

## Conditional chain segments

Keep a conditional contribution inside the chain with the empty `Modifier` as
the identity element:

```kotlin
Box(
    modifier = Modifier
        .fillMaxWidth()
        .then(if (selected) Modifier.background(selectedColor) else Modifier),
) { /* ... */ }
```

An imperative form can be clearer when building a modifier from procedural
animation state or when one expression becomes harder to understand. Preserve
behavior over enforcing a visual shape.

## Hoist a lone conditional

When a container's only content is one `if` and the container has no meaningful
arguments, emit both only when the condition is true:

```kotlin
if (showHeader) {
    Column {
        Text("Title")
        Text("Subtitle")
    }
}
```

Keep the conditional inside when any of these are true:

- the container has a modifier, alignment, arrangement, padding, semantics, or
  another visible responsibility;
- the container has siblings in addition to the `if`;
- both `if` and `else` branches contribute content to a shared container;
- retaining an empty container is part of measurement, animation, or focus
  behavior.

```kotlin
Box(modifier = modifier, contentAlignment = Alignment.Center) {
    if (loaded) Content()
}
```

The purpose is clearer structure, not a claimed runtime optimization. Compare
layout bounds, focus order, animations, and tests before accepting the change.
