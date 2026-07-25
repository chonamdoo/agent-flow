# UiNode Model Guide

Source: PART 4-1, PART 4-2, PART 4-3.

## Screen

```kotlin
data class Screen(
    val screenId: String,
    val schemaVersion: Int,
    val version: Long,
    val root: UiNode,                    // tree root, usually a lazy column
    val actions: Map<String, SduiAction>,
    val resultContract: ResultContract?, // only when this screen returns a result
    val cachePolicy: CachePolicy,
    val fetchedAt: Long,
)
```

`schemaVersion` guards parser compatibility; `version` orders patch responses.

## UiNode: one base contract, three tiers

Every node exposes `id`, `modifier`, `visibility`, `events`, and `accessibility`,
so the renderer can attach layout, clicks, and semantics uniformly.

- **Tier 1 — layout containers**, freely composable: `Column`, `Row`, `Box`,
  `Grid(columns)`, `Pager(autoScrollMs)`, `LazyColumn`, `LazyRow(contentPadding)`.
  Each holds `children: List<UiNode>` and a `spacing` token.
- **Tier 2 — primitive leaves**, freely composable: `Text(text, style, color,
  maxLines)`, `Image(url, contentScale)`, `Button(label, variant, leadingIcon)`,
  `IconButton(icon)`, `Spacer`, `Divider`, `CountdownText(endsAt, template)`.
- **Tier 3 — semantic components**, fixed design, owning performance and
  accessibility: `ProductCard(data)`, `SectionHeader(data)`,
  `FilterChipGroup(data)`, and peers. Each wraps one typed payload.
- **Fallback** — `Unknown(id, rawType)`. Not optional: it is the only reason an
  unrecognized server type does not take down the screen.

```kotlin
sealed interface UiNode {
    val id: String
    val modifier: NodeModifier
    val visibility: Visibility
    val events: Map<TriggerType, ActionRef>
    val accessibility: Accessibility?

    data class Column(..., val spacing: String?, val children: List<UiNode>) : UiNode
    data class Text(..., val text: String, val style: String, val maxLines: Int) : UiNode
    data class ProductCard(val data: ProductCardData, ...) : UiNode
    data class Unknown(override val id: String, val rawType: String) : UiNode
}
```

Tier selection:

- Layout that changes often (campaigns, promotions) — tier 1 plus tier 2.
- Repeated with fixed design (product cards, filter chips) — tier 3.
- A tier 1+2 combination seen in three or more places — promote to tier 3.

Adding a tier 3 type costs a release, so add one only when the promotion rule or
a performance/accessibility requirement forces it.

## NodeModifier

```kotlin
@Immutable
data class NodeModifier(
    val padding: EdgeInsets? = null,
    val margin: EdgeInsets? = null,
    val width: SizeSpec? = null,
    val height: SizeSpec? = null,
    val aspectRatio: Float? = null,
    val weight: Float? = null,
    val background: BackgroundSpec? = null,
    val shape: ShapeSpec? = null,
    val border: BorderSpec? = null,
    val elevation: String? = null,
    val alpha: Float? = null,
    val clip: Boolean = false,
) {
    companion object { val EMPTY = NodeModifier() }
}

sealed interface SizeSpec {
    data object MatchParent : SizeSpec
    data object WrapContent : SizeSpec
    data class Fixed(val dp: Int) : SizeSpec
}
```

- `@Immutable` is required. The modifier is read on every recomposition of every
  node; an unstable type defeats skipping across the whole tree.
- Inset, color, radius, and elevation fields hold **token names**, not values.
  See `design-token-guide.md`.
- A missing modifier resolves to `EMPTY`, never to null handling at each call
  site.

## Modifier application order

Fixed. Changing it changes rendering:

```text
margin -> size/aspectRatio -> weight -> clip -> background -> border
       -> elevation -> alpha -> padding
```

Encode the order once in the modifier mapper. Any node type that builds its own
chain locally is a defect.
