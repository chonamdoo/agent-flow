# Theming with Styles

## The Style layer

Treat Styles as a layer between design tokens and components:

```text
named tokens -> atomic Styles -> component Styles -> custom components
```

Tokens provide values. Atomic Styles apply one reusable visual decision.
Component Styles combine those decisions into a default. Components consume the
default and expose a caller override.

## Atomic and component Styles

```kotlin
val padding = Style { contentPadding(16.dp) }
val rounded = Style { shape(RoundedCornerShape(8.dp)) }
val primary = Style { background(colors.brand) }
val hoverShadow = Style {
    hovered {
        animate { dropShadow(themeShadows.hover) }
    }
}

val button = padding then rounded then primary then hoverShadow
```

Atomic Styles are useful when several component defaults share exact visual
rules. Prefer a cohesive component Style when atoms make ordering hard to
understand or create combinations that are not valid design-system states.

## Custom theme integration

Store values in CompositionLocals and expose component Styles statically:

```kotlin
@Immutable
data class AppThemeValues(
    val colors: AppColors,
    val typography: Typography,
    val shapes: Shapes,
)

val LocalAppTheme = staticCompositionLocalOf {
    AppThemeValues(DefaultColors, Typography(), Shapes())
}

object AppTheme {
    val colors: AppColors
        @Composable @ReadOnlyComposable
        get() = LocalAppTheme.current.colors

    val typography: Typography
        @Composable @ReadOnlyComposable
        get() = LocalAppTheme.current.typography

    val shapes: Shapes
        @Composable @ReadOnlyComposable
        get() = LocalAppTheme.current.shapes

    val styles: ComponentStyles = ComponentStyles
}

val StyleScope.colors: AppColors
    get() = LocalAppTheme.currentValue.colors
val StyleScope.typography: Typography
    get() = LocalAppTheme.currentValue.typography
val StyleScope.shapes: Shapes
    get() = LocalAppTheme.currentValue.shapes
```

Provide values at the application theme boundary. Because the Style reads the
current token value, light/dark or nested theme changes do not require swapping
the entire Style object.

## Material coexistence

Choose the strategy already used by the project:

- Extended Material theme: keep Material defaults and add custom tokens or
  wrappers for brand-specific values.
- Replaced subsystems: custom typography or shapes require wrappers around
  Material components whose defaults still read `MaterialTheme`.
- Fully custom design system: own colors, typography, shapes, indications, and
  custom components explicitly.

The experimental Styles migration applies to the project's custom components.
Do not promise Style customization for a Material component unless the selected
dependency actually exposes a Style parameter. Continue using supported
Material parameters or project wrappers where it does not.

## Theme migration constraints

- Do not introduce a new CompositionLocal solely to hold a stateless Styles
  object; expose the object from the theme.
- Do not hard-code token values inside components after moving defaults into the
  Style layer.
- Do not replace all component parameters blindly. Keep behavior, content,
  semantics, and unsupported layout contracts explicit.
- Avoid conditional whole-Style selection for ordinary token changes; resolve
  dynamic values inside the Style.
- Keep Style composition order visible where last-write-wins behavior matters.
- Verify nested themes and light/dark transitions in addition to isolated
  component previews.

## Review checklist

- Every public custom component applies defaults before caller overrides.
- StyleScope extensions read the intended CompositionLocal values.
- Atomic Styles are reused only where their combinations remain valid.
- Material wrappers retain expected Material behavior and accessibility.
- Theme changes update visual tokens without reconstructing business state.
- Screenshot coverage includes default, overridden, disabled, interactive,
  light, dark, and nested-theme rendering where supported.
