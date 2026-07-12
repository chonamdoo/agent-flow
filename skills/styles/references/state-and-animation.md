# State and animation

## Built-in interaction state

Styles can react to enabled, disabled, pressed, hovered, focused, selected, and
toggled state. Nested blocks represent combined state:

```kotlin
val buttonStyle = Style {
    background(Color.White)
    hovered {
        background(lightPurple)
        pressed { background(lightOrange) }
    }
    pressed { background(lightRed) }
    focused { border(2.dp, lightBlue) }
    disabled { alpha(0.38f) }
}
```

## Connect a custom component

The interaction Modifier and StyleState must observe the same source:

```kotlin
@Composable
fun GradientButton(
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
                onClick = onClick,
                enabled = enabled,
                interactionSource = source,
                indication = null,
            )
            .styleable(styleState, baseGradientButtonStyle, style),
        content = content,
    )
}
```

If focus behavior is present, pass the same source to `focusable`. Do not create
separate remembered sources for behavior and styling.

## Animate state changes

Wrap visual changes in `animate`:

```kotlin
val animated = Style {
    border(3.dp, Color.Black)
    background(Color.White)
    size(100.dp)
    transformOrigin(TransformOrigin.Center)
    pressed {
        animate {
            borderColor(Color.Magenta)
            background(Color(0xFFB39DDB))
        }
        animate(spring(dampingRatio = Spring.DampingRatioMediumBouncy)) {
            scale(1.2f)
        }
    }
}
```

Use an explicit animation spec only when product behavior requires it. Verify
rapid transitions, disabled state, pointer hover, keyboard focus, and reduced
motion requirements already defined by the application.

## Custom state

Define a typed key with a safe default:

```kotlin
enum class PlayerState { Stopped, Playing, Paused }

val playerStateKey = StyleStateKey(PlayerState.Stopped)

var MutableStyleState.playerState: PlayerState
    get() = this[playerStateKey]
    set(value) { this[playerStateKey] = value }

fun StyleScope.playerPlaying(style: Style) {
    state(playerStateKey, style) { key, current ->
        current[key] == PlayerState.Playing
    }
}
```

Link incoming state to the remembered StyleState:

```kotlin
@Composable
fun MediaPlayer(
    playerState: PlayerState,
    modifier: Modifier = Modifier,
    style: Style = Style,
) {
    val styleState = remember { MutableStyleState(null) }
    styleState.playerState = playerState
    Box(modifier.styleable(styleState, basePlayerStyle, style)) {
        // Player content
    }
}
```

Then define state appearance:

```kotlin
val playbackStyle = Style {
    borderColor(Color.Gray)
    playerPlaying {
        animate { borderColor(Color.Green) }
    }
}
```

Keep business state outside the Style object. The Style consumes a stable state
projection; it must not dispatch events or own playback logic.

## Verification

- Pointer hover, keyboard focus, press, disabled, selected, and toggled states
  match the component contract.
- Combined states resolve in the intended nested order.
- Caller overrides still take precedence over component defaults.
- Animation does not trigger unexpected composition work or layout jumps.
- State changes do not create a new interaction source on recomposition.
- Semantics and indications remain equivalent to the pre-migration component.
