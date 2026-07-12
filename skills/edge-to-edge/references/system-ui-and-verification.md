# System UI and verification

## Entry point

Call the ComponentActivity extension before content:

```kotlin
override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(savedInstanceState)
    enableEdgeToEdge()
    setContent { AppTheme { App() } }
}
```

When the app instead uses `WindowCompat` to enable edge-to-edge, it owns icon
appearance. Update icons from the theme so they contrast with the background:

```kotlin
@Composable
fun AppTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            val window = (view.context as? Activity)?.window ?: return@SideEffect
            WindowCompat.getInsetsController(window, view).apply {
                isAppearanceLightStatusBars = !darkTheme
                isAppearanceLightNavigationBars = !darkTheme
            }
        }
    }
    MaterialTheme(content = content)
}
```

Do not add this manual controller path when using
`ComponentActivity.enableEdgeToEdge()`, which manages light and dark icon modes.

## Navigation-bar contrast

For SDK 29 and later, a screen whose bottom app bar or navigation bar should
extend to the bottom may need:

```kotlin
window.isNavigationBarContrastEnforced = false
```

Set it in the owning Activity only after verifying three-button navigation.
The goal is to avoid an unwanted platform scrim without making icons illegible.

## Protection scrims

If content behind a system bar varies in brightness, draw a non-interactive
gradient or solid protection layer with exactly the inset height. Keep main
content edge-to-edge and ensure the scrim does not consume input:

```kotlin
@Composable
fun StatusBarProtection(
    color: Color = MaterialTheme.colorScheme.surfaceContainer,
) {
    Spacer(
        modifier = Modifier
            .fillMaxWidth()
            .windowInsetsTopHeight(WindowInsets.statusBars)
            .background(
                Brush.verticalGradient(
                    listOf(color, color.copy(alpha = 0.8f), Color.Transparent),
                ),
            ),
    )
}
```

Draw the protection after the main content so it is visible. Verify light and
dark icon contrast against the final pixels.

## Full-screen dialogs

A dialog is full-screen when it disables the platform default width and fills
the available size. Such dialogs must opt into edge-to-edge:

```kotlin
Dialog(
    onDismissRequest = onDismiss,
    properties = DialogProperties(
        usePlatformDefaultWidth = false,
        decorFitsSystemWindows = false,
    ),
) {
    Box(Modifier.fillMaxSize()) { /* Dialog content with its own inset owner */ }
}
```

Normal-width dialogs should keep platform fitting unless their design requires
otherwise.

## Verification matrix

Build the project, then verify each affected Activity and full-screen dialog:

- Status-bar background draws as intended and top controls remain tappable.
- Gesture navigation: bottom controls, FABs, and list items avoid the gesture
  area while backgrounds and scrolling content extend behind it.
- Three-button navigation: bottom content remains safe and no unintended
  contrast scrim appears.
- Light and dark themes: status and navigation icons remain legible.
- Portrait, landscape, split-screen, and supported fold states recalculate
  insets without stale padding.
- Display cutout devices preserve critical content inside safe drawing bounds.
- IME opens with the field focused and visible, content scrolls to it, and no
  blank double-padding remains after closing the keyboard.
- First and last lazy-list items rest clear of bars but can scroll behind them.
- FABs and bottom bars retain their full touch targets.
- Adaptive navigation rail/bar changes do not clip individual destination
  content.
- Accessibility focus bounds align with visible controls.

Run the module's existing UI, screenshot, and inset tests. Do not update golden
images until the new edge-to-edge appearance has been reviewed.
