# Setup and migration

## Dependency and compiler setup

Use a project-approved compatible version at or above the source minimum. With
a version catalog, keep the foundation dependency or Compose BOM centralized.
The module must compile against SDK 37 or later.

Enable the API in the affected module:

```kotlin
kotlin {
    compilerOptions {
        jvmTarget = JvmTarget.fromTarget("17")
        freeCompilerArgs.add(
            "-opt-in=androidx.compose.foundation.style.ExperimentalFoundationStyleApi",
        )
    }
}
```

Use the exact imports exposed by the selected Compose version, including:

```kotlin
import androidx.compose.foundation.style.Style
```

Build immediately after dependency and compiler changes. If APIs differ from
the examples, inspect the selected dependency's declarations instead of mixing
examples from another alpha release.

## Analyze before editing

1. Locate the central theme and custom design-system components.
2. Identify color, typography, shape, size, and spacing tokens.
3. Identify parameters that control appearance versus behavior.
4. Find interactive components and their `MutableInteractionSource` ownership.
5. Run an existing screenshot test or capture a reviewed baseline. If the
   project lacks screenshot infrastructure, use its existing UI test or preview
   workflow rather than introducing a new framework without approval.
6. Stop if the target is not Compose. If the project still uses Material 2,
   keep a Material migration separate from the Styles migration.

## Establish component defaults

Create a central object in the theme package:

```kotlin
object ComponentStyles {
    val button = Style {
        background(colors.brand)
        shape(shapes.extraLarge)
        minWidth(58.dp)
        minHeight(40.dp)
        textStyle(typography.labelLarge)
        disabled {
            background(colors.brandDisabled)
        }
    }
}
```

Expose it from the custom theme without a new CompositionLocal just for the
styles:

```kotlin
object AppTheme {
    val colors: AppColors
        @Composable @ReadOnlyComposable
        get() = LocalAppTheme.current.colors

    val styles: ComponentStyles = ComponentStyles
}
```

When the Style needs dynamic theme tokens stored in CompositionLocals, expose
extensions on `StyleScope`:

```kotlin
val StyleScope.colors: AppColors
    get() = LocalAppTheme.currentValue.colors

val StyleScope.typography: Typography
    get() = LocalAppTheme.currentValue.typography

val StyleScope.shapes: Shapes
    get() = LocalAppTheme.currentValue.shapes
```

## Migrate one custom component at a time

Before:

```kotlin
@Composable
fun CustomButton(
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    backgroundColor: Color = AppTheme.colors.brand,
    shape: Shape = AppTheme.shapes.extraLarge,
    enabled: Boolean = true,
    content: @Composable RowScope.() -> Unit,
) {
    Row(
        modifier = modifier
            .clickable(enabled = enabled, onClick = onClick)
            .background(backgroundColor, shape)
            .defaultMinSize(58.dp, 40.dp),
        horizontalArrangement = Arrangement.Center,
        verticalAlignment = Alignment.CenterVertically,
        content = content,
    )
}
```

After:

```kotlin
@Composable
fun CustomButton(
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    style: Style = Style,
    enabled: Boolean = true,
    interactionSource: MutableInteractionSource? = null,
    content: @Composable RowScope.() -> Unit,
) {
    val source = interactionSource ?: remember { MutableInteractionSource() }
    val styleState = rememberUpdatedStyleState(source) {
        it.isEnabled = enabled
    }
    Row(
        modifier = modifier
            .clickable(
                enabled = enabled,
                interactionSource = source,
                indication = null,
                onClick = onClick,
            )
            .styleable(styleState, AppTheme.styles.button, style),
        horizontalArrangement = Arrangement.Center,
        verticalAlignment = Alignment.CenterVertically,
        content = content,
    )
}
```

Migration rules:

- Remove only appearance parameters supported by `StyleScope`; retain behavior,
  content, accessibility, and unsupported layout parameters.
- Put the caller style last so it overrides component defaults.
- Reuse the same interaction source for clickable/focusable behavior and the
  `StyleState`.
- Preserve modifier order where it affects input, semantics, clipping, or
  layout.
- Migrate and verify one component family at a time.

## Validation

1. Build and run compile checks after every migrated component family.
2. Compare the baseline and new rendering for size, padding, shape, color,
   typography, alignment, enabled state, and interaction state.
3. Run screenshot tests without accepting changed goldens automatically.
4. Add or update Compose UI tests for the public component contract.
5. Verify accessibility semantics and touch behavior remain unchanged.
