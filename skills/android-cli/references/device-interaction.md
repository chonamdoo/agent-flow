# Device inspection and interaction

## Inspect before input

`android layout` returns a flat JSON list. Useful fields include:

- `text`, `resourceId`, and `contentDesc` for identity.
- `interactions` such as `clickable`, `focusable`, `scrollable`,
  `long-clickable`, `checkable`, and `password`.
- `state` such as `checked`, `focused`, and `selected`.
- `bounds` in `[left,top][right,bottom]` form and `center` in `[x,y]` form.
- `off-screen`, which indicates that scrolling may be required.

Use a full layout before the first action. After each action, use `layout
--diff` to reduce context while confirming what changed. If a WebView,
animation, or transient surface makes the layout incomplete, wait for stability
and capture a screenshot.

## Screenshots and visual resolution

```bash
android screen capture --output=screen.png
android screen capture --annotate --output=annotated.png
android screen resolve --screen=annotated.png --string="#3"
```

Confirm exact flags with `android screen --help`. Visually inspect the PNG
before choosing a coordinate. Annotated labels are temporary observations; do
not reuse a label after the screen changes.

## Input with ADB

Use the selected element's current center or bounds:

```bash
adb -s <serial> shell input tap 152 23
adb -s <serial> shell input swipe 250 500 250 250 500
adb -s <serial> shell input text '<escaped-text>'
adb -s <serial> shell input keyevent KEYCODE_BACK
```

Rules:

1. Include `-s <serial>` when more than one device is connected.
2. Before text input, tap the field and verify `focused` appears in its state.
3. Scroll only an element marked `scrollable`, using a slow swipe duration.
4. Re-inspect after every input; coordinates become stale when layout changes.
5. Allow asynchronous content to settle, then use `layout --diff`.
6. Never infer success solely from an ADB exit code; verify visible state.

When resolving an annotated region, substitute the returned coordinates into a
separate explicit `adb shell input tap` command. Keep the screenshot and command
in the test evidence.

## Verification hierarchy

1. Layout tree for text, accessibility identity, interaction state, and bounds.
2. Layout diff for changes caused by the previous action.
3. Screenshot for visual appearance, WebViews, animation, or missing hierarchy.
4. Device logs only when the task requires crash or lifecycle evidence.

If all available evidence is ambiguous, report the ambiguity instead of
guessing a target.
