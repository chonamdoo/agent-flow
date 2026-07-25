# Renderer and Action Guide

Source: PART 7 (whole), PART 3-3 (app-shell boundary), PART 1-2 (cross-screen
result delivery), PART 8-6 (server-time countdown).

## App-shell boundary (PART 3-3)

Deciding rule: **if failure makes the app unusable it is native; if failure only
empties part of a screen it is SDUI.**

| Surface | Verdict | Why |
|---|---|---|
| App bar / tab bar skeleton | native | a server error must never delete navigation |
| App bar / tab bar composition | JSON-controlled | add a tab without a release |
| Content area | SDUI | changes often, failure is recoverable |
| Search input | native | keyboard, IME, and focus are platform concerns |
| Search results | SDUI | the most A/B-tested surface |

Shell config resolves through three tiers: server response, then Room cache,
then a hardcoded `DEFAULT`. The third tier is mandatory.

## Modifier mapping (PART 7-1)

- One `NodeModifier.toCompose(scope)` function owns the whole chain. No node
  type builds its own.
- Apply in this fixed order; changing it changes rendering:
  `margin -> size/aspectRatio -> weight -> clip -> background -> border ->
  elevation -> alpha -> padding`.
- `weight` needs the parent scope, so pass `RowScope`/`ColumnScope` down and
  ignore `weight` when there is none.
- Resolve every value through the token tables, never from the payload directly.
- Compute the shape once and reuse it for `clip`, `background`, and `border`.

## Recursive renderer (PART 7-2)

```kotlin
LazyColumn(state = listState, modifier = root.modifier.toCompose()) {
    items(root.children, key = { it.id }, contentType = { it::class.simpleName }) {
        RenderNode(it, onEvent)
    }
}
```

- `key` on every lazy list; `contentType` on the screen-level list. Without a key
  the list rebuilds on patch and scroll position jumps.
- Container nodes recurse and pass their Compose scope to children.
- Never nest a lazy list inside a lazy list of the same axis. Render `Grid` as
  chunked `Row`s inside a plain `Column`.
- `visibility == GONE` returns before composing anything.
- Click and semantics are attached by the shared modifier builder, so every node
  type gets them for free.
- The `when` over `UiNode` must have a branch for `Unknown`. Debug builds may
  show a placeholder; release builds render nothing and do not throw.

## Parser (PART 7-3)

```kotlin
private const val MAX_DEPTH = 12

fun JsonObject.toUiNode(json: Json, depth: Int = 0): UiNode {
    val id = this["id"]?.jsonPrimitive?.contentOrNull ?: UUID.randomUUID().toString()
    if (depth > MAX_DEPTH) return UiNode.Unknown(id, "max_depth_exceeded")
    val type = this["type"]?.jsonPrimitive?.contentOrNull ?: return UiNode.Unknown(id, "null")
    return runCatching {
        when (type) {
            "COLUMN" -> UiNode.Column(id, ..., childrenOf(json, depth + 1))
            "PRODUCT_CARD" -> UiNode.ProductCard(id, ..., toProductData())
            else -> UiNode.Unknown(id, type)                 // unsupported type
        }
    }.getOrElse { UiNode.Unknown(id, "$type(parse_error)") } // malformed payload
}
```

Two fallbacks, not one: an unsupported type and a field-level parse failure are
different faults and both must degrade to `Unknown`. Depth is checked before any
child recursion.

## Action interpreter (PART 7-4)

- `SduiAction` is a sealed type and the executor `when` is exhaustive. No
  reflection, no dynamic dispatch on a raw string.
- `Noop` and `Unknown` are silent no-ops, so an action added on the server never
  crashes an older client.
- `Sequence` executes steps in order; `Condition` evaluates one fixed operator
  set and runs a single branch.
- `Navigate` registers `expectResult` before emitting the navigation effect.
- Navigation, dismissal, scrolling, toasts, and snackbars leave as `UiEffect`;
  the executor never touches `NavController` or `Context`.
- `RefreshSections` calls the repository and returns; the UI update arrives
  through the Room flow.
- Expression resolution is **regex substitution only**:

```kotlin
private val TOKEN = Regex("""\{\$\.([\w.]+)\}""")

private fun resolve(template: String, ctx: ActionContext): String =
    TOKEN.replace(template) { m ->
        when (val path = m.groupValues[1]) {
            "item.token" -> (ctx.node as? UiNode.ProductCard)?.data?.token.orEmpty()
            "section.id" -> ctx.sectionId.orEmpty()
            else -> ctx.result?.get(path.removePrefix("result.")).orEmpty()
        }
    }
```

No `eval`, no scripting engine, no template language that can reach arbitrary
members.

## Cross-screen result delivery (PART 1-2, PART 7-5)

Problem: screen A opens B, and returning must create or refresh one section
while A stays alive.

A deep-link round trip is wrong. It produces `A -> B -> A'`, so back navigation
returns to B forever, A loses scroll position, images and cache, and the return
URI has to re-expose a domain id.

Correct flow:

1. A navigates with `NAVIGATE` and registers `expectResult` under a result key.
2. B does local work.
3. B closes with `DISMISS_WITH_RESULT`, emitting a payload under that key. B
   never learns who A is.
4. A resumes, matches the key, and runs the registered action: usually
   `REFRESH_SECTIONS` followed by a conditional `SCROLL_TO`.

The server answers with patch operations, not a whole screen. `UPSERT` collapses
"create" and "update" into one operation, so the server needs no knowledge of
client state.

```kotlin
// B: closing. A is not recreated.
navController.previousBackStackEntry?.savedStateHandle?.set(key, payload)
navController.popBackStack()

// A: receiving, then clearing so it fires once.
entry?.savedStateHandle?.getStateFlow<Map<String, String>?>(key, null)
    ?.filterNotNull()
    ?.collect { payload ->
        viewModel.onEvent(ScreenEvent.ResultReceived(key, payload))
        entry.savedStateHandle[key] = null
    }
```

Required behavior around the patch:

| Concern | Handling |
|---|---|
| Scroll preservation | stable `key` per section; without it the list rebuilds and scroll jumps. This is the whole reason patches exist. |
| Insertion jump | inserting above the viewport pushes content; animate or compensate the offset |
| Refresh failure | keep the existing section and fail quietly, never blank the screen |
| Duplicate triggers | `distinctUntilChanged` plus `flatMapLatest` on fast round trips |
| Ordering | discard a patch whose `version` is older than the stored one |

## Server-time countdown (PART 8-6)

Countdowns compute remaining time from a server-adjusted clock, never
`System.currentTimeMillis()`. Derive the offset from the response `Date` header;
a device-clock countdown can be extended by changing the system time. Drive the
tick from a `LaunchedEffect` keyed on the deadline and stop the loop at zero.

## Risk table (PART 7-6)

| Risk | Defense |
|---|---|
| Deep nesting cost | `MAX_DEPTH` enforced at parse time |
| Recomposition storms | `@Immutable` models plus `key` and `contentType` |
| Design drift | reject raw dp/hex, tokens only, validated server-side |
| Undebuggable trees | debug-build node boundary overlay and JSON inspector |
| Lost accessibility | `accessibility` field plus semantic components first |

Accessibility maps straight through the shared modifier builder:

```kotlin
// "accessibility": { "label": "Season sale banner", "role": "BUTTON", "hidden": false }
m = m.semantics {
    node.accessibility?.label?.let { contentDescription = it }
    node.accessibility?.role?.let { role = it.toComposeRole() }
}
```
