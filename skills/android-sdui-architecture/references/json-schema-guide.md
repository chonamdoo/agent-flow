# JSON Schema Guide

Source: PART 5 (whole), plus the action dictionary in PART 1-3.

## Screen envelope

```json
{
  "screenId": "home",
  "schemaVersion": 2,
  "version": 17,
  "cachePolicy": { "ttlSeconds": 300, "staleWhileRevalidate": true },
  "root": { "id": "root", "type": "LAZY_COLUMN", "spacing": "spacing_l",
            "children": ["...sections..."] },
  "actions": { "act_go_detail": { "...": "..." } }
}
```

`schemaVersion` guards parser compatibility. `version` orders patch responses so
a late one can be discarded. Actions live in a screen-level dictionary and nodes
reference them by id, so one action can serve many nodes.

## Node shape

Every node carries `id`, `type`, optional `modifier`, `visibility`, `events`,
and `accessibility`. Containers add `children`; semantic components add their own
typed fields.

Tier 3 (semantic) keeps lists small — one node per card:

```json
{ "id": "p_1", "type": "PRODUCT_CARD", "token": "tk_1001",
  "brand": "Example", "name": "Example Product",
  "imageUrl": "https://cdn.example/p1.png",
  "price": { "origin": 30000, "sale": 19200, "discountRate": 36 },
  "liked": false, "modifier": { "width": 120 },
  "events": { "onClick": { "actionRef": "act_go_detail" },
              "onLike": { "actionRef": "act_toggle_like" } } }
```

Tier 1+2 (layout tree) buys layout freedom without a release:

```json
{ "id": "promo_card", "type": "COLUMN", "spacing": "spacing_s",
  "modifier": {
    "width": "MATCH_PARENT",
    "background": { "type": "GRADIENT", "colors": ["brand_start", "brand_end"] },
    "shape": { "type": "ROUNDED", "radius": "radius_l" },
    "padding": { "all": "spacing_m" }, "clip": true },
  "children": [
    { "id": "pc_img", "type": "IMAGE", "url": "https://cdn.example/promo.png",
      "contentScale": "CROP",
      "modifier": { "width": "MATCH_PARENT", "aspectRatio": 2.5 } },
    { "id": "pc_title", "type": "TEXT", "text": "Season Sale",
      "style": "title_large", "color": "on_primary" },
    { "id": "pc_buttons", "type": "ROW", "spacing": "spacing_s", "children": [
      { "id": "btn_detail", "type": "BUTTON", "label": "Details", "variant": "OUTLINED",
        "modifier": { "weight": 1.0, "height": 44 },
        "events": { "onClick": { "actionRef": "act_go_promo" } } } ] }
  ] }
```

Every styling value above is a token name. See `design-token-guide.md`.

## Events and triggers

Trigger names are a fixed set:

```text
onClick, onLongPress, onVisible, onScrollEnd,
onRefresh, onLoad, onDismiss, onTimer, onScreenResult
```

`onVisible` takes `threshold` and `once`; the rest take only `actionRef`.

## Action vocabulary

Finite and closed. The client implements exactly these; anything else parses to
`UNKNOWN` and is ignored.

```json
{
  "NAVIGATE":            { "uri", "expectResult?" },
  "DISMISS_WITH_RESULT": { "resultKey", "payload" },
  "API_CALL":            { "method", "endpoint", "body", "optimistic?", "onSuccess?", "onError?" },
  "REFRESH_SECTIONS":    { "sectionIds", "allowInsert?", "context" },
  "UPDATE_LOCAL_STATE":  { "path", "value" },
  "SCROLL_TO":           { "sectionId", "highlight?" },
  "OPEN_URL":            { "url", "external" },
  "TRACK":               { "event", "props" },
  "TOAST":               { "message" },
  "SEQUENCE":            { "steps": [Action] },
  "CONDITION":           { "if", "then", "else?" },
  "NOOP":                {},
  "UNKNOWN":             {}
}
```

Optimistic update plus rollback needs no new verb — it is `SEQUENCE` with an
`onError` branch that reverses the local write and shows a message. Condition
operators are `EQ, NEQ, GT, LT, AND, OR, NOT, EXISTS`; nesting past two levels
means the schema is reinventing a language, so move that logic out.

One action serves every section because bindings resolve at runtime:

```json
"act_filter_section": {
  "type": "REFRESH_SECTIONS", "sectionIds": ["{$.section.id}"],
  "context": { "filterId": "{$.filter.id}", "selected": "{$.filter.selectedId}" }
}
```

## Binding context

```text
$.item      data this component renders      $.state   screen-local state
$.section   owning section metadata          $.filter  filter selection
$.screen    screen parameters                $.user    coarse flags, never secrets
$.result    payload from another screen      $.env     app version, network state
```

Security: expressions resolve by regex substitution only — no `eval`, no
scripting engine. Action types are a sealed allowlist, so the server cannot
execute arbitrary code on the device.

## App-shell config

The shell skeleton is native; only its composition is server-controlled.

```json
{ "configVersion": 4,
  "topBar": {
    "searchBar": { "placeholders": ["Search"],
                   "onClick": { "type": "NAVIGATE", "uri": "sdui://search" } },
    "categoryTabs": { "scrollable": true, "selectedId": "recommend",
      "items": [ { "id": "recommend", "label": "For you", "screenId": "recommend" } ] } },
  "tabs": [
    { "id": "home", "label": "Home", "icon": "home", "route": "home",
      "kind": "SDUI", "screenId": "recommend", "isDefault": true },
    { "id": "my", "label": "My", "icon": "person", "route": "my", "kind": "NATIVE" } ] }
```

A hardcoded local `AppConfig.DEFAULT` is mandatory. A failed or empty config must
never remove navigation.

## Patch response

```json
{ "version": 42,
  "operations": [
    { "op": "UPSERT", "sectionId": "recently_viewed",
      "anchor": { "position": "AFTER", "targetSectionId": "top_banner" },
      "section": { "sectionId": "recently_viewed", "type": "PRODUCT_GRID", "items": [] } },
    { "op": "REMOVE", "sectionId": "empty_placeholder" } ] }
```

Operation set: `UPSERT / REPLACE / REMOVE / MOVE / PATCH_ITEM`. `UPSERT` means
replace if present, otherwise insert at the anchor, so the server never needs to
know client state.
