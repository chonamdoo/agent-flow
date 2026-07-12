# Modifier contract

## Declaration

A composable that emits layout is a leaf that its parent places. Prefer the
exact parameter name and default:

```kotlin
@Composable
fun Header(
    title: String,
    modifier: Modifier = Modifier,
    supportingContent: (@Composable () -> Unit)? = null,
) { /* ... */ }
```

Required data comes first, then `modifier`, then optional values and content
slots. A private or single-call-site composable is not exempt merely because it
is private; if it emits a reusable layout node, the same contract improves
composition and later reuse.

## Root application and order

Apply the caller modifier to the outermost emitted layout. Applying it only to a
child prevents the caller from sizing or positioning the component as a whole.
Do not accept a modifier and ignore it.

Caller modifiers come first because earlier modifiers are outer wrappers:

```kotlin
// Good: caller controls the outer boundary; clipping is Avatar identity.
Image(
    painter = painter,
    contentDescription = null,
    modifier = modifier
        .clip(CircleShape)
        .size(48.dp),
)
```

Avoid this reversal:

```kotlin
modifier = Modifier.clip(CircleShape).size(48.dp).then(modifier)
```

It makes the component's chain outermost and can defeat caller sizing,
padding, clipping, pointer input, or semantics.

## Placement versus identity

The parent normally owns:

- `fillMaxWidth`, `fillMaxSize`, `weight`, `align`, and `offset`;
- screen or section padding;
- width and height chosen for one screen;
- placement relative to siblings.

The component may own modifiers that define what it is:

- an avatar's shape and defensible default size;
- a control's minimum accessible target;
- clipping required for its rendering contract;
- internal drawing, semantics, or input behavior.

Ask whether a reasonable caller could want the component without the modifier.
If yes, move it to the caller. If no, keep it after the caller modifier.

```kotlin
@Composable
fun PrimaryButton(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Button(onClick = onClick, modifier = modifier) { Text(text) }
}

// The screen owns its width and surrounding spacing.
PrimaryButton(
    text = "Continue",
    onClick = onContinue,
    modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp),
)
```

## Root ambiguity

When a function conditionally emits different roots, apply the modifier to the
root in every branch. When it emits multiple siblings, first decide whether the
API should introduce a meaningful common container or expose separate content
slots. Do not add a wrapper solely to have somewhere to put the modifier if that
wrapper changes measurement or semantics.
