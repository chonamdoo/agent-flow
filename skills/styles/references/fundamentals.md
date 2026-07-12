# Styles fundamentals

## Adoption paths

Use one of three paths:

1. Pass a Style to a custom component that already exposes a `style` parameter.
2. Apply `Modifier.styleable` to a layout or custom component.
3. Build a design-system component that exposes caller Style overrides and
   applies centralized defaults.

```kotlin
val emphasized = Style {
    background(Color.Blue)
    contentPaddingHorizontal(16.dp)
}

CustomButton(style = emphasized, onClick = {}) { Text("Continue") }

val state = remember { MutableStyleState(null) }
Row(Modifier.styleable(state, emphasized)) { Text("Content") }
```

## Property groups

Styles cover visual and sizing properties such as:

- Layout: inner and external padding, width, height, size, fill fractions, and
  positional offsets.
- Appearance: background, foreground, borders, shape, drop shadow, and inner
  shadow.
- Transform: translation, scale, rotation, alpha, z-index, and transform origin.
- Inherited text/content: text style, font attributes, content color or brush,
  line height, spacing, alignment, direction, breaks, hyphenation, decoration,
  indent, and baseline shift.

Styles do not replace arbitrary Modifiers. Keep gestures, click behavior,
focus behavior, semantics, custom drawing, and unsupported structural layout in
the Modifier chain.

## Override and composition rules

Properties inside a Style are not additive. The last write for a property wins:

```kotlin
val style = Style {
    background(Color.Red)
    background(Color.Blue) // Blue wins.
    contentPadding(32.dp)
    contentPaddingHorizontal(8.dp) // Horizontal sides become 8.dp.
}
```

Merge reusable Styles with `then`. Later Styles win on overlapping properties:

```kotlin
val base = Style {
    background(Color.Red)
    contentPadding(32.dp)
}
val override = Style {
    background(Color.LightGray)
    contentPaddingHorizontal(8.dp)
}
val finalStyle = base then override
```

When a component accepts defaults and a caller override, apply them in this
order:

```kotlin
Modifier.styleable(styleState, componentDefault, callerStyle)
```

Multiple chained `Modifier.styleable` calls behave additively for
non-inherited properties on the element. For inherited properties, the last
styleable modifier wins. Prefer one call with explicit Style order when
possible.

## Inheritance and precedence

While inheritance remains experimental, enable it only when the selected API
requires it:

```kotlin
ComposeFoundationFlags.isInheritedTextStyleEnabled = true
```

Inherited content color and typography flow to children. Precedence is:

1. Direct composable arguments.
2. The composable's Style parameter.
3. A styleable modifier on that composable.
4. Inherited parent Style values.

A child Style can override inherited text or content properties without
changing siblings. Non-inherited layout and visual properties stay on the
element where they are applied.

## CompositionLocal values

Styles can read design tokens stored in CompositionLocals:

```kotlin
val StyleScope.colors: AppColors
    get() = LocalAppTheme.currentValue.colors

val button = Style {
    background(colors.brand)
    contentColor(colors.onBrand)
}
```

Use `currentValue` inside `StyleScope`, not composable-only `current` access.

## Custom helpers

An extension may combine existing properties:

```kotlin
fun StyleScope.outlinedBackground(color: Color) {
    border(1.dp, color)
    background(color)
}
```

Do not invent new styleable property storage; custom properties beyond the API
are unsupported. Keep that behavior in a Modifier or component implementation.

## Style versus Modifier decision

Choose a Style for theme-wide component appearance, caller override of a
default, or frequent visual state animation that should avoid recomposition.
Choose a Modifier for behavior, semantics, gestures, unique one-off structure,
custom drawing, or additive layering.

Style resolution itself costs more than one simple Modifier because it resolves
possible values and inheritance. Do not replace a straightforward one-off
Modifier solely for novelty.
