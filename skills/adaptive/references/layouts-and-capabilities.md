# Adaptive layouts and capabilities

## Lazy vertical content

For large or unbounded collections, keep lazy rendering:

- Replace a one-column `LazyColumn` with `LazyVerticalGrid` only when multiple readable columns improve the content.
- Use `GridCells.Adaptive(minSize)` for `LazyVerticalGrid`.
- Use `StaggeredGridCells.Adaptive(minSize)` for `LazyVerticalStaggeredGrid`.
- Choose `minSize` from the content's readable or actionable minimum width, not a device-name breakpoint.

Preserve item keys, content types, padding, scroll restoration, accessibility traversal, and test semantics during migration.

## Non-lazy repeated content with Grid

Use non-lazy Grid for a bounded group of similar items that should reflow together. Do not nest Grid inside the old `Column`; replace that layout owner. Confirm API availability and obtain user approval before introducing an experimental Grid API.

```kotlin
@OptIn(ExperimentalGridApi::class)
@Composable
fun AdaptiveActions() {
    Grid(
        config = {
            val width = constraints.maxWidth.toDp()
            val columns = if (width < 800.dp) 2 else 4
            val rows = if (columns == 2) 4 else 2
            val gapSize = 8.dp
            val cellSize = ((width - gapSize * (columns - 1)) / columns)
                .coerceAtLeast(0.dp)
            repeat(columns) { column(cellSize) }
            repeat(rows) { row(cellSize) }
            gap(gapSize)
        },
    ) {
        // Bounded items
    }
}
```

Derive rows, columns, gaps, and cell sizes from current constraints. Test minimum and maximum supported widths and avoid negative cell dimensions.

## FlexBox

Use FlexBox when items should wrap or redistribute space according to content and available width rather than occupy a strict row-column matrix. Before adopting it:

- Confirm the project's Compose version exposes the required API and whether opt-in is needed.
- Define container direction, wrapping, alignment, and spacing deliberately.
- Define which items may grow, shrink, or keep a basis size.
- Verify ordering and focus traversal after wrapping.
- Test long localized text and large font scales.

Prefer Grid when alignment across both axes is the core requirement; prefer FlexBox when content-driven wrapping is the core requirement.

## MediaQuery

Use MediaQuery when behavior depends on capabilities that width alone cannot represent. Relevant queries include:

- screen or window dimensions;
- pointer presence and precision;
- keyboard or text-entry availability;
- cameras, microphones, and other device capabilities.

MediaQuery is experimental and must be enabled before Compose content uses it.
Set the integration flag once in the application startup path:

```kotlin
class App : Application() {
    override fun onCreate() {
        ComposeUiFlags.isMediaQueryIntegrationEnabled = true
        super.onCreate()
    }
}
```

Use `mediaQuery` for discrete capability or posture decisions:

```kotlin
if (mediaQuery { pointerPrecision == UiMediaScope.PointerPrecision.Blunt }) {
    LargeTargetControls()
} else {
    StandardControls()
}
```

Window dimensions may update rapidly. Use `derivedMediaQuery` for width or
height thresholds so recomposition follows the derived Boolean instead of every
dimension change:

```kotlin
val compact by derivedMediaQuery {
    windowWidth < WindowSizeClass.WIDTH_DP_MEDIUM_LOWER_BOUND.dp
}
```

Keep capability reads close to the presentation decision and provide a usable fallback when a capability is absent or changes. Do not infer input mode from device category.

## Adaptive controls

Touch, pointer, TV focus, Auto, and XR targets may need different visual density or hit areas. Keep semantic roles and labels stable while adapting target size. Verify hover, focus, keyboard activation, D-pad traversal, and touch bounds as applicable.
