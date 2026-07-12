# Focus, input, and verification

## Input model

Glasses interaction is predominantly one-dimensional:

- tap activates or confirms;
- swipe navigates or scrolls;
- swipe down is system Back on display glasses;
- two-finger swipe, touch-and-hold, and ordinary camera-button actions are
  reserved by the system;
- a camera-button double press can be available to the app.

Prefer Glimmer components because they translate standard input and provide
focus feedback. Use custom `draggable` or `scrollable` behavior only when no
component represents the interaction, and preserve Back and focus navigation.

## Initial focus

Until the platform enables automatic initial focus by default, set the flag
before `super.onCreate` in the projected Activity:

```kotlin
override fun onCreate(savedInstanceState: Bundle?) {
    @OptIn(ExperimentalComposeUiApi::class)
    ComposeUiFlags.isInitialFocusOnFocusableAvailable = true
    super.onCreate(savedInstanceState)
}
```

Focus search is one-dimensional. A swipe moves continuously through a
scrollable container and discretely across controls. Use `focusGroup` and
`focusProperties.onEnter` only when the default first focus is wrong. Avoid a
scrolling list directly above or below unrelated buttons because navigation
becomes ambiguous.

## Accessibility and content

- Provide localized descriptions for meaningful icons and images.
- Keep labels short, glanceable, and audible when the visual rationale is not
  sufficient.
- Use system Back to dismiss temporary/detail states.
- Verify focus, pressed, disabled, and selected states are distinguishable.
- Confirm foreground/background HCT tone differs by at least 70 points.

## Device verification

Run the following on a connected display-glasses target:

- [ ] The phone launch action is disabled while disconnected.
- [ ] The projected Activity never opens on the phone display.
- [ ] The root is pure black and all projected UI uses Glimmer components.
- [ ] The first actionable element receives focus on entry.
- [ ] Tap, swipe, swipe-down Back, and long-content navigation work.
- [ ] Text remains readable over bright and dark real-world backgrounds.
- [ ] The UI shows one primary information unit and stays bottom-aligned.
- [ ] Camera and microphone prompts explicitly describe glasses access.
- [ ] Denial and user cancellation leave a usable fallback state.
- [ ] Disconnect releases projected services and reconnect recreates them.
- [ ] Camera resolution and frame rate stay within thermal limits.
- [ ] Screen-reader labels and nonvisual feedback remain meaningful.

Also run the project's build, lint, unit, Compose UI, and device tests affected
by the change. Do not treat a phone preview as proof of projected-device input,
permission, contrast, or hardware behavior.
