# Insets and IME

## Choose one inset owner

Prefer ownership in this order when it fits the screen:

1. Scaffold or Material component inset APIs.
2. Inset-padding modifiers on the component that must be safe.
3. `WindowInsetsRulers` for nested layouts where accumulated padding is
   difficult to control.
4. Inset-size modifiers for a visual element that must match a system bar.

Do not combine equivalent owners for the same edge.

## Scaffold and lazy content

For a lazy list, pass `innerPadding` as `contentPadding` and consume it so
descendants do not apply the same insets:

```kotlin
Scaffold { innerPadding ->
    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .consumeWindowInsets(innerPadding),
        contentPadding = innerPadding,
    ) {
        // Items can scroll behind bars while first and last items remain safe.
    }
}
```

For non-lazy content, apply then consume the padding:

```kotlin
Scaffold { innerPadding ->
    Column(
        modifier = Modifier
            .padding(innerPadding)
            .consumeWindowInsets(innerPadding),
    ) { /* Content */ }
}
```

Material 3 app bars, navigation bars, rails, drawers, and modal sheets manage
their own safe areas. Use their inset parameters when customization is needed;
do not pad a parent around an app bar, because that prevents its background from
drawing into the system-bar region.

For a Material 2 app bar whose selected library version exposes
`windowInsets`, pass insets to the bar itself rather than its parent. Prefer the
component default, or select the narrowest required inset explicitly:

```kotlin
TopAppBar(windowInsets = AppBarDefaults.topAppBarWindowInsets)
// Or, when the screen contract requires it:
TopAppBar(
    windowInsets = WindowInsets.systemBars.exclude(WindowInsets.navigationBars),
)
```

Include `WindowInsets.captionBar` for freeform windows only when the screen must
avoid that region. Verify the actual Material 2 API before editing because
available inset parameters depend on the selected library version.

Outside a Scaffold:

```kotlin
Box(
    modifier = Modifier
        .fillMaxSize()
        .safeDrawingPadding(),
) {
    Button(
        onClick = {},
        modifier = Modifier.align(Alignment.BottomCenter),
    ) { Text("Continue") }
}
```

For deeply nested content, fit its bounds instead of accumulating padding:

```kotlin
Modifier.fitInside(WindowInsetsRulers.SafeDrawing.current)
```

Use `windowInsetsTopHeight(WindowInsets.systemBars)` or the corresponding side
modifier only for a visual element intentionally sized to an inset.

## Adaptive scaffolds

`NavigationSuiteScaffold` manages safe areas for its navigation bar or rail but
does not pass padding to destination content. `ListDetailPaneScaffold` and other
adaptive containers similarly require each pane or screen to own its relevant
list padding, FAB spacing, and safe controls.

Do not apply `safeDrawingPadding` to the adaptive scaffold parent; doing so
clips every destination and defeats edge-to-edge drawing.

## IME prerequisites

Every Activity containing `TextField`, `OutlinedTextField`, or
`BasicTextField` must use:

```xml
<activity
    android:name=".EditorActivity"
    android:windowSoftInputMode="adjustResize" />
```

Keep the input focused while opening the IME, then use exactly one of these
patterns.

### Scaffold owns IME through contentWindowInsets

```kotlin
Scaffold(contentWindowInsets = WindowInsets.safeDrawing) { innerPadding ->
    Column(
        modifier = Modifier
            .padding(innerPadding)
            .consumeWindowInsets(innerPadding)
            .verticalScroll(rememberScrollState()),
    ) { /* Inputs */ }
}
```

Do not add `imePadding()` here because `innerPadding` already includes IME
insets.

### Ruler owns IME

```kotlin
Scaffold { innerPadding ->
    Column(
        modifier = Modifier
            .padding(innerPadding)
            .consumeWindowInsets(innerPadding)
            .fitInside(WindowInsetsRulers.Ime.current)
            .verticalScroll(rememberScrollState()),
    ) { /* Inputs */ }
}
```

This avoids padding accumulation in nested hierarchies.

### imePadding owns IME

```kotlin
Scaffold { innerPadding ->
    Column(
        modifier = Modifier
            .padding(innerPadding)
            .consumeWindowInsets(innerPadding)
            .imePadding()
            .verticalScroll(rememberScrollState()),
    ) { /* Inputs */ }
}
```

The default Scaffold insets do not include the IME, so this is valid. Modifier
order matters: apply `imePadding()` before `verticalScroll()`.

## Consumption versus raw PaddingValues

`safeDrawingPadding()` and `windowInsetsPadding(WindowInsets.safeDrawing)`
consume the insets they apply. A descendant applying IME padding receives only
the remaining inset.

`Modifier.padding(WindowInsets.safeDrawing.asPaddingValues())` does not consume
the inset. Combining it with descendant `imePadding()` can double-apply space.
If raw `PaddingValues` are necessary, use a ruler for the descendant or call
`consumeWindowInsets` deliberately.

## Lists and FABs

- Apply system or Scaffold padding to `LazyColumn.contentPadding` or
  `LazyRow.contentPadding`, never to a parent that clips scrolling.
- Put a FAB in the Scaffold slot when possible. Otherwise apply safe-drawing
  padding to the FAB's positioning container.
- Verify the first and last list item, overscroll, and FAB hit target in both
  gesture and three-button navigation.
