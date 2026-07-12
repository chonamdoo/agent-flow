# Additive-display design and components

## Visual system

Display glasses use an additive display: black is transparent and every other
color adds light over the real world. Use a pure-black projected root, minimize
coverage, anchor primary content near the bottom, and show one primary item at
a time. Maintain at least a 70-point difference between foreground and
background HCT tone. Validate the actual theme colors rather than comparing hex
channels.

Wrap the full projected UI in `GlimmerTheme` and provide
`createGoogleSansFlexTypography()`. Use theme color, typography, spacing, shape,
icon-size, and depth tokens instead of Material tokens or hard-coded substitutes.

Current default tokens include primary `#9BBFFF`, secondary `#4C88E9`, surface
`#262626`, outline `#606460`, a standard 36dp corner shape, and a small 12dp
corner shape. Read values through `GlimmerTheme`; the table documents intent,
not permission to duplicate constants at call sites.

Google Sans Flex defaults use width 100, roundness 100, and optical size 9.
Recommended roles are:

| Role | Size / line height | Weight |
|---|---|---:|
| titleLarge | 30sp / 36sp | 750 |
| titleMedium | 24sp / 28sp | 750 |
| titleSmall | 20sp / 28sp | 750 |
| bodyLarge | 30sp / 36sp | 520 |
| bodyMedium | 24sp / 36sp | 520 |
| bodySmall | 20sp / 28sp | 520 |
| caption | 18sp / 24sp | 650 |

Do not render text below 18sp or use thin/hairline weights. Use Glimmer `Text`
without a manual color when the nearest Glimmer surface should supply content
color.

## Depth

Material elevation and ordinary shadow modifiers are not the Glimmer depth
model. Use the component defaults or `SurfaceDepthEffect` with
`GlimmerTheme.depthEffectLevels`. Baseline surfaces generally have no depth;
focused states use a prescribed level. Higher numbered levels represent higher
visual priority. Avoid inventing shadow values when a component already owns
its focus treatment.

## Component choice

### Cards

Use a `Card` for one digestible unit. Give `onClick` only when the entire card
is one action. If a card contains independent controls, leave the surface
non-clickable and use the action slot so focus does not compete. Do not nest a
card in a `ListItem`.

```kotlin
Card(
    title = { Text("Message") },
    action = { Button(onClick = onReply) { Text("Reply") } },
) {
    Text("The next train arrives in five minutes.")
}
```

Cards default to a minimum 80dp height and theme padding. Preserve their
surface, focus, and border defaults unless the product requires an override.

### Buttons and icons

Use a standard `Button` when an action needs a label; use `IconButton` only for
an unmistakable icon-only action. Medium and large buttons have 48dp and 72dp
minimum heights. Use toggle button variants for persistent selected state rather
than manually styling an ordinary button. Let buttons supply focus and pressed
feedback.

Use `GlimmerTheme.iconSizes`: small 32dp, medium 40dp, large 48dp. Use a vector
when possible, set a localized `contentDescription` unless decorative, and use
`Color.Unspecified` only for intentionally multicolor assets. A clickable icon
must be an `IconButton`, not an `Icon` with ad-hoc input handling.
When the product has no icon system, use rounded, unfilled Material Symbols at
weight 600 for visual consistency.

### Title chips

Use `TitleChip` only as a concise, non-interactive label above related content.
Keep it one line and roughly three words or fewer, center it, and separate it
from associated content with `TitleChipDefaults.associatedContentSpacing`.
Do not pair a title chip with a stack.

### Lists and stacks

Use `VerticalList` for multiple visible items of the same type. Use `ListItem`
slots, consistent surfaces, and `VerticalListDefaults` spacing rather than
inventing row geometry. A scrollable list should normally be the only major
interactive region on its screen; never nest scrolling controls.
Use the `VerticalList(title = { TitleChip { ... } })` overload for a section
title instead of positioning a separate sticky chip. The default item spacing
is 20dp.

Use `VerticalStack` when only one item should be prominent or when item types
vary. Bottom-align it, allow 66dp beyond the tallest item for the stacked
reveal/scrim treatment, and decorate each shaped item:

```kotlin
VerticalStack(state = rememberStackState()) {
    items(messages) { message ->
        Card(
            modifier = Modifier.itemDecoration(CardDefaults.shape),
        ) {
            Text(message)
        }
    }
}
```

The decoration modifier supplies masking and depth behavior for stack items.
Apply one per distinct shape. If omitted intentionally, verify the loss of
stack masking and depth is acceptable.

## Custom surfaces

Build a custom component with `Modifier.surface(...)` only when a provided
component cannot express the design. Preserve Glimmer border, content color,
focused depth, pressed overlay, minimum target, focus, and semantics behavior.
