# Components and migration

## Migration order

Migrate in reviewable slices. Do not leave Material 2.5 and Material 3 as a
long-term mixed design system, and remove Horologist composables/layout/theme
dependencies as their screens migrate.

1. Align dependencies and theme imports.
2. Introduce one Wear Material 3 `AppScaffold` around navigation.
3. Move each screen to `ScreenScaffold`.
4. Replace legacy components using the exact version's samples.
5. Replace scrolling and navigation primitives.
6. Remove obsolete dependencies only after call sites are gone.

Common mappings:

| Material 2.5 / legacy | Wear Material 3 |
|---|---|
| `Scaffold` | one `AppScaffold` plus per-screen `ScreenScaffold` |
| `Chip` / `CompactChip` | `Button` / `CompactButton` variants |
| `ToggleChip` | `CheckboxButton`, `RadioButton`, or `SwitchButton` |
| `SplitToggleChip` | matching split checkbox, radio, or switch button |
| `PositionIndicator` | `ScrollIndicator` |
| `ScalingLazyColumn` | `TransformingLazyColumn` |
| `InlineSlider` | `Slider` |
| `SwipeToRevealCard` / `SwipeToRevealChip` | `SwipeToReveal` |
| legacy pager plus indicator | pager scaffold and Wear pager |

Select a semantic variant from the local sample rather than translating names
mechanically. Expect default padding, shape, motion, and screenshots to change.

## Scaffold and scrolling

Declare one `AppScaffold` for the Activity/navigation host and a
`ScreenScaffold` for each destination. Pass the screen's `contentPadding`
through to its scrolling container.

```kotlin
val state = rememberTransformingLazyColumnState()
val transformationSpec = rememberTransformationSpec()

ScreenScaffold(scrollState = state) { contentPadding ->
    TransformingLazyColumn(
        state = state,
        contentPadding = contentPadding,
    ) {
        item {
            Button(
                modifier = Modifier
                    .fillMaxWidth()
                    .transformedHeight(this, transformationSpec)
                    .minimumVerticalContentPadding(
                        ButtonDefaults.minimumVerticalListContentPadding
                    ),
                transformation = SurfaceTransformation(transformationSpec),
                onClick = onClick,
            ) {
                Text("Continue", maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
        }
    }
}
```

Every morphing list item needs `transformedHeight` and the component's
`SurfaceTransformation`. Use scoped `minimumVerticalContentPadding` with the
component's defaults. If snapping, configure snap fling and snap rotary
behavior together. A plain `Column` is acceptable only when the content cannot
scroll even at the largest supported font scale.

```kotlin
TransformingLazyColumn(
    state = state,
    flingBehavior = TransformingLazyColumnDefaults.snapFlingBehavior(state),
    rotaryScrollableBehavior = RotaryScrollableDefaults.snapBehavior(state),
) { /* items */ }
```

Put `EdgeButton` in the `ScreenScaffold.edgeButton` slot, not as the final list
item. Preserve its scroll/overscroll integration from the exact local sample.
Guard the screen scroll indicator while
`LocalScrollCaptureInProgress.current` is true.

## Theme and defaults

- Import `androidx.wear.compose.material3.MaterialTheme`.
- Use `ColorScheme`, `Typography`, `Shapes`, and `MotionScheme`.
- Prefer dynamic color with a brand fallback when the product supports it.
- Use theme typography and colors; do not hard-code text size or color.
- Use `ButtonDefaults`, `CardDefaults`, `ListHeaderDefaults`, and other
  component defaults for padding, shape, and styling.
- Prefer default Wear shapes optimized for round screens unless design evidence
  requires a custom shape.

## Navigation and system behavior

For new navigation use Navigation 3 and the Wear
`SwipeDismissableSceneStrategy`. Keep static chrome in `AppScaffold` during
destination transitions. Use Wear pager scaffolds for page indicator and
`TimeText` coordination. Use `LocalAmbientModeManager`, not the legacy ambient
lifecycle observer.

Use Wear preview annotations such as `WearPreviewDevices` and
`WearPreviewFontScales`; a phone Compose preview does not validate a Wear UI.
