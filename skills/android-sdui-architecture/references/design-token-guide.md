# Design Token Guide

Source: PART 4-4.

## Rule

The server states **meaning**; the client owns the **value**. Any styling field
in server JSON carries a token name. Raw dp numbers and hex colors are a schema
violation, not a style choice.

Rejected payload:

```json
{
  "padding": { "all": "13dp" },
  "background": { "color": "#F3F4F6" }
}
```

Accepted payload:

```json
{
  "padding": { "all": "spacing_m" },
  "background": { "color": "surface_variant" }
}
```

## Token tables

Token resolution lives in the design-system module and nowhere else.

```kotlin
object SpacingTokens {
    private val map = mapOf(
        "spacing_none" to 0.dp, "spacing_xs" to 4.dp, "spacing_s" to 8.dp,
        "spacing_m" to 16.dp, "spacing_l" to 24.dp, "spacing_xl" to 32.dp,
    )
    operator fun get(token: String?): Dp =
        map[token] ?: token?.toFloatOrNull()?.dp ?: 0.dp   // resilience fallback
}

object ColorTokens {
    @Composable
    operator fun get(token: String?): Color = when (token) {
        "primary" -> MaterialTheme.colorScheme.primary
        "surface" -> MaterialTheme.colorScheme.surface
        "surface_variant" -> MaterialTheme.colorScheme.surfaceVariant
        "on_surface" -> MaterialTheme.colorScheme.onSurface
        else -> Color.Unspecified
    }
}
```

Cover the same shape for typography, shape/radius, elevation, and icons. Every
table needs a defined fallback so an unknown token degrades to a neutral value
instead of throwing.

## The fallback is not permission

A client-side numeric fallback exists so a bad payload still renders. It is not
the sanctioned path. Raw literals must be rejected by server-side schema
validation before they ever reach a device, otherwise the fallback silently
becomes the API and the token layer stops meaning anything.

## Why this line matters

- Dark mode and large-screen adaptation follow automatically, because the value
  is resolved from the active theme rather than baked into a payload.
- A spacing or palette revision is one client change, not a sweep across every
  stored JSON template.
- Without it the server can render arbitrary pixels and the design system has no
  enforcement point at all.
