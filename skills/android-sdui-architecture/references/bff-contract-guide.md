# Server Contract Guide

Source: PART 9 (whole).

The server side of SDUI is a view-composition layer, not a UI authoring tool.
Domain services stay unaware of components; one thin layer maps domain data to
the component catalog and assembles screens from templates.

```text
[domain services]  catalog / promotion / personalization / inventory
        |  domain data
[composition layer]
        - domain -> component catalog mapping
        - screen template lookup (section set and order, from DB or CMS)
        - personalization and experiment assignment
        - downgrade to client capability
        |  SDUI JSON
[client renderer]
```

## Staging the backend

- Early phases: bundled asset JSON plus a fake data source. Focus stays on the
  client contract and rendering.
- Later phases: a real local service, once dynamic patch composition is needed.

A fake source must not be a happy-path stub. Simulate latency, an offline
switch, and a low random failure rate, otherwise the offline and retry paths are
never exercised.

## Screen composer

```kotlin
fun composeScreen(screenId: String, capabilities: Set<String>): ScreenDto {
    val sectionIds = templates[screenId] ?: error("Unknown screen")
    val sections = sectionIds.mapNotNull { downgradeIfNeeded(buildSection(it), capabilities) }
    return ScreenDto(screenId, SCHEMA_VERSION, sections, ActionCatalog.forScreen(screenId))
}
```

- The template holds only section ids and their order. Reordering a screen is a
  data change, never a code change.
- `buildSection` converts domain data into catalog components. It knows nothing
  about the target screen.
- The action catalog is per screen and shared across that screen's nodes.

## Capability negotiation

```kotlin
private fun downgradeIfNeeded(section: SectionDto, caps: Set<String>): SectionDto? = when {
    caps.isEmpty() -> section                                       // unknown client, send default
    section.type in caps -> section
    section.fallbackType in caps -> section.copy(type = section.fallbackType!!)
    else -> null                                                    // drop, never send unsupported
}
```

Clients advertise supported components and versions in a request header. Every
section declares a `fallbackType`; a section with no supported representation is
dropped rather than sent and skipped on the device. This is what lets a new
component ship without a forced update.

## Patch composition

- `composePatch(screenId, sectionIds, context)` returns operations plus a
  monotonic `version`.
- When the requested section has no content, emit `REMOVE` instead of an empty
  section.
- When it has content and is absent from the template, emit `UPSERT` with an
  anchor and also insert the id into the template so a full reload agrees with
  the patched state.
- The version counter is the client's only ordering guarantee. Never reuse or
  decrease it.

## Domain to component mapping

```kotlin
fun Product.toProductCard() = buildJsonObject {
    put("id", "p_$token")
    put("type", "PRODUCT_CARD")
    put("token", token)
    put("name", name)
    put("imageUrl", imageUrl)
    putJsonObject("price") {
        put("origin", originPrice); put("sale", salePrice)
        put("discountRate", ((1 - salePrice.toDouble() / originPrice) * 100).toInt())
    }
    putJsonObject("events") {
        putJsonObject("onClick") { put("actionRef", "act_go_detail") }
        putJsonObject("onLike") { put("actionRef", "act_toggle_like") }
    }
}
```

This mapping function is what generality actually means. A new screen reuses it
and the client needs no change. Generality is not "represent any data"; it is
"fix a finite set of representations and let the server recombine them".

Expose domain ids as opaque tokens. A client that computes destinations from raw
ids has hardcoded a routing rule that then needs a release to change.

## Local development

Point the client at a loopback base URL and permit cleartext for that host only,
scoped to debug builds. Never widen the cleartext policy application-wide.
