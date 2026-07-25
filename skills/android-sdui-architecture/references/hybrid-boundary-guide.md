# Hybrid Boundary Guide

Source: PART 2 (whole), plus the design-decision section of PART 11.

## Two different burdens

Release burden and server burden are not the same constraint, and they hurt
different people. Naming them separately is what makes the boundary decidable.

| Burden | What hurts | Who feels it |
|---|---|---|
| Release | release cadence, store review, forced updates, old-version fragmentation | product and merchandising, who want weekly changes |
| Server | response size, backend authoring UI, runtime composition cost, client renderer complexity | backend, where every UI change becomes their ticket |

Server burden is mostly an organizational failure: at 100% server-driven UI,
"change this button color" becomes a backend task. Removing the app release
bottleneck by creating a backend bottleneck is not a win.

## Restated goal

The goal is not "never ship again". It is **separating what changes fast from
what changes slowly**.

- Changes fast (weekly or daily, decided by product/merchandising): section
  composition and order, item lists, promotion copy and layout.
- Changes slowly (quarterly, decided by design/engineering): card appearance,
  button interaction.

## Release-frequency spectrum

| Approach | Requires a release when | Real cadence |
|---|---|---|
| 100% native | every UI change | weekly or more |
| Component catalog | a new component is added | 1-2 per quarter |
| **Hybrid** | a new primitive or a new semantic component | ~1 per quarter |
| Full layout tree | a new primitive (rare) | ~1 per year |
| WebView | never (loses performance and platform capability) | - |

Hybrid beats a plain catalog because a new layout does not require a new
component. "This campaign card is horizontal with three buttons" forces a
`PROMO_CARD_V2` release under a catalog; under hybrid it is a JSON change over
existing primitives.

## Decision axes

Do not answer with a label. Answer with these axes.

| Axis | Server owns (layout tree) | Client owns (semantic component) |
|---|---|---|
| Change frequency | weekly or faster | quarterly |
| Reuse count | one-off campaign | repeated 3+ times |
| Response-size impact | 1-2 per screen | tens to hundreds in a list |
| Performance sensitivity | low | high (scrolling items) |
| Accessibility demand | low | high |
| Design stability | different every time | fixed |

Worked examples: a product card scores right on every axis, so it is a semantic
component owned by the client. A campaign banner scores left on every axis, so
it is a server-authored layout tree.

Response size is the practical trap: 100 products as node trees is megabytes of
JSON, while a `PRODUCT_CARD` is one node with a few fields.

**Promotion rule**: when the same layout-tree combination appears in three or
more places, promote it to a semantic component.

## Four devices that lower both burdens

1. **Slot pattern** — the client fixes the component structure, the server fills
   named slots. Adding a seasonal badge needs no release.

   ```json
   {
     "type": "PRODUCT_CARD",
     "slots": {
       "trailing": { "type": "TEXT", "text": "Limited" },
       "overlay": { "type": "IMAGE", "url": "https://cdn.example/badge.png" }
     }
   }
   ```

2. **Capability negotiation** — the client advertises supported components
   (`X-Supported-Components: PRODUCT_CARD@2,CAROUSEL@1`) and the server serves
   the richest supported variant, downgrading for older versions. New features
   ship immediately without a forced update.

3. **Composition layer on the server** — backend does not author UI; a thin
   layer maps domain data to catalog components, and screen templates live in a
   CMS that product owners edit directly.

4. **Remote assets** — icons and animations are URLs, so asset swaps need no
   release.

## Limits

- More than two levels of conditional logic in JSON means the schema is
  reinventing a programming language. Stop and move the logic to the client or
  the composition layer.
- Checkout, payment, and complex forms are not SDUI targets.
- The schema is the contract, so versioning cost is real and permanent.
