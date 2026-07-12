# Adaptive workflow and navigation

## Establish a baseline

Before restructuring a screen, find its screenshot tests or Compose previews. Cover the major window classes used by the product: phone, unfolded foldable, tablet, and desktop-sized windows. A reusable preview annotation keeps the matrix explicit:

```kotlin
@Preview(name = "Phone", device = Devices.PHONE, showBackground = true)
@Preview(name = "Foldable", device = Devices.FOLDABLE, showBackground = true)
@Preview(name = "Tablet", device = Devices.TABLET, showBackground = true)
@Preview(name = "Desktop", device = Devices.DESKTOP, showBackground = true)
annotation class FormFactorPreviews
```

If screenshot infrastructure is absent, add or propose project-appropriate Compose preview screenshot coverage before changing layout behavior. Record new screenshots for comparison, but leave golden-image approval to the user.

## Adapt top-level navigation

Bottom navigation is appropriate for compact, touch-first portrait windows. Larger hand-held and resizable windows usually need an edge-accessible navigation rail or another `NavigationSuiteScaffold` presentation.

Migration checklist:

1. Locate the existing top-level navigation container.
2. Convert destinations to `NavigationSuiteItem` definitions.
3. Replace the navigation container with `NavigationSuiteScaffold` from Material 3 Adaptive.
4. Supply the same destinations through the scaffold's navigation-items API.
5. Preserve selection state, accessibility labels, test tags, and navigation events.

## Preserve visibility rules

Navigation may be hidden while consuming content, showing camera/media previews, or presenting an immersive destination. Keep those rules through `NavigationSuiteScaffoldState` rather than wrapping only one navigation presentation in `AnimatedVisibility`.

```kotlin
val navigationState = rememberNavigationSuiteScaffoldState()

LaunchedEffect(shouldShowNavigation) {
    if (shouldShowNavigation) navigationState.show() else navigationState.hide()
}

NavigationSuiteScaffold(
    navigationSuiteItems = navigationItems,
    state = navigationState,
) {
    AppContent()
}
```

Keep the visibility source state above any composable that must control it. Avoid issuing `show` or `hide` directly during composition.

## Full-screen behavior

A detail destination may hide app bars and navigation when it occupies a compact window. Disable that mobile-only full-screen behavior when the detail is displayed beside a list or supporting pane. The surrounding scene owns shared chrome in multi-pane mode.

## Verification

- Resize through compact, medium, and expanded widths without restarting the activity.
- Verify destination selection and back behavior before and after a layout transition.
- Exercise touch, keyboard, focus, mouse, and trackpad paths supported by the target device.
- Build and run unit, UI, and screenshot tests relevant to the changed screens.
