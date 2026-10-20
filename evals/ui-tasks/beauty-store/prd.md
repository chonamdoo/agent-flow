# PRD: Cross-border beauty store (evaluation task)

- Task: `beauty-store`, version 1
- Source: structure, flows and interactions observed on 2026-10-20 on a public cross-border beauty e-commerce site, as an
  anonymous visitor. No trademark, logo, product name, product image or real price from that site is used. The store is
  called "Store"; brands are "Brand 1–8"; products are "Product A…"; prices are generated.
- Data: `fixtures/catalog.json` is the only data source. Build against it as given; do not add network calls to other hosts.
- Purpose: an evaluation task for Agent Flow. `evals/ui_task.py` scores any build against `acceptance.json`; the reference
  build in `reference/` scores 100%.

## Goal

A shopper can find products from the header search, browse a category, open a product, choose an option and quantity, add
it to the cart, and see the cart totals and free-shipping progress update without page reloads.

## Scope

Five screens and the global header, all client-side, reading `fixtures/catalog.json`:

| Route | Screen |
|---|---|
| `/` | Home: hero carousel and product rails |
| `/category/<categoryId>` | Category listing: filter, sort, page size, load more |
| `/product/<productId>` | Product detail: options, quantity, add to cart |
| `/cart` | Cart: quantity, remove, totals, free-shipping message, shipping location |
| `/search?q=<query>` | Search results placeholder: shows the submitted query |

Constants: free-shipping threshold `60.00`, flat shipping `25.00`, maximum quantity per line `10`, listing page size 24
(also 36 and 48). Money is shown as `$` with two decimals. The cart, recent searches and the saved shipping location and
currency persist in `localStorage`.

## Non-goals

Accounts, wishlist, payment, checkout beyond a placeholder page, reviews writing, personalisation, analytics, images
(use plain coloured blocks), translation.

## Test-id contract

Every element named below carries `data-testid="<id>"`. The scorer finds elements only through these ids, roles and
visible text, so any framework works. An id that is not per item (cards, lines, options, chips, suggestions, slides,
rails) appears at most once in the page, hidden copies included: the scorer's locators are strict.

| Area | Test ids |
|---|---|
| Header | `search-input`, `search-layer`, `recent-searches`, `recent-item`, `popular-searches`, `popular-item`, `suggest-brand`, `suggest-category`, `suggest-keyword`, `cart-link`, `cart-badge` |
| Home | `hero`, `hero-slide` (current slide only visible), `hero-counter`, `hero-prev`, `hero-next`, `hero-toggle`, `rail` (each product rail), `rail-prev`, `rail-next` |
| Product card | `product-card`, `card-brand`, `card-name`, `price-original`, `price-sale`, `price-range`, `rank` |
| Listing | `listing-title`, `result-count`, `sort-select` (a native `<select>`), `page-size` (a native `<select>`), `filter-<categoryId>` (checkbox), `refine-chip`, `clear-all`, `empty-result`, `load-more` |
| Product | `breadcrumb`, `pdp-brand`, `pdp-name`, `option-toggle`, `option-list`, `option-item`, `qty-minus`, `qty-value`, `qty-plus`, `add-to-cart`, `added-dialog`, `go-to-cart` |
| Cart | `cart-line`, `line-qty-minus`, `line-qty-value`, `line-qty-plus`, `line-total`, `line-remove`, `remove-confirm`, `remove-cancel`, `cart-count`, `subtotal`, `shipping-fee`, `estimated-total`, `free-shipping-msg`, `free-shipping-bar` (with `aria-valuenow` 0–100), `cart-empty`, `checkout-button`, `ship-error`, `prefs-open`, `prefs-location` (native `<select>`), `prefs-currency` (native `<select>`), `prefs-save` |
| Search | `search-query` |

## Spec Items

SPEC-1: Header search. Focusing the empty input opens `search-layer` with `popular-searches` (the 10 fixture terms, in
order) and `recent-searches` (hidden when empty). Typing shows suggestions only after the input has been idle for 200
ms: at most 2 `suggest-brand` (brand names), 2 `suggest-category` (category names) and 10 `suggest-keyword` (popular
search terms, then product names), each containing the query case-insensitively. Escape closes the layer. Enter with a
non-empty trimmed query navigates to `/search?q=<query>`, shows it in `search-query`, and puts it first in recent
searches (no duplicates, at most 30). An empty or whitespace query does nothing.

SPEC-2: Home hero carousel. It auto-advances every 6 s and shows `hero-counter` as "n / total". `hero-toggle` stops and
restarts auto-advance. Prev and next move one slide and wrap around.

SPEC-3: Product cards and rails. A card shows brand and name. A discounted product shows a struck `price-original` and a
`price-sale`; a product whose options differ in price shows only `price-range` as "$min - $max"; an undiscounted
single-price product shows only `price-sale`. The best-seller rail shows `rank` 1..N. A rail's `rail-prev` is disabled
at the start and `rail-next` at the end.

SPEC-4: Category listing. `/category/<id>` shows the category name, "N items" counting the whole subtree, the sort
control (options "Most Popular" by `popularity` descending, "Price Low to High" and "Price High to Low" by the product's
`salePrice`) defaulting to "Most Popular", and the first 24 cards. There is one `filter-<id>` checkbox for each
descendant category; ticking several shows the union. `load-more` reads "MORE (k/P)" with P = ceil(N / page size),
appends the next page, and hides on the last page. Changing the sort returns to page 1. Ticking a sub-category filter
narrows the count to that subtree, adds a `refine-chip`, and keeps the sort; removing the chip or `clear-all` restores
the count. A category or filter set with no products shows `empty-result` with the text "Sorry, there is no item
matching the applied filter." and no `load-more`. `page-size` offers "24", "36" and "48"; changing it returns to page 1
and recomputes P.

SPEC-5: Product detail and options. Clicking a card opens `/product/<id>`. The page shows `breadcrumb` (the category
path, e.g. "Home > Skincare > Toners > Mists"), brand, name and prices; `qty-value` shows the quantity as text or as an
input value. For a product with options, before a choice `qty-minus` and `qty-plus` are disabled and `add-to-cart` reads
"Add to Cart"; pressing it opens `option-list` instead of adding. Sold-out options are disabled and read "Sold out".
Choosing an option closes the list, sets the quantity to 1 and makes the button read "Add to Cart - $<sale × qty>". A
product without options starts at quantity 1 with that label. Plus stops at min(10, stock); minus stops at 1. A card or
price range over options uses their sale prices, sold-out options included.

SPEC-6: Add to cart. Adding opens `added-dialog` with `go-to-cart`, and `cart-badge` shows the number of distinct lines.
Adding the same product option again increases that line's quantity instead of adding a line.

SPEC-7: Cart totals and free shipping. Each `cart-line` has a stepper (1..min(10, stock)) and `line-remove`. Changing
quantity updates `line-total`, `subtotal`, `shipping-fee` and `estimated-total` without reload. With subtotal S < 60.00,
`free-shipping-msg` reads "Only $<60.00 − S> left for Free Shipping!", `free-shipping-bar` is at round(100 × S / 60),
shipping is $25.00 and the estimated total is S + 25.00. With S ≥ 60.00 the message reads "You've got Free Shipping!",
the bar is at 100, shipping is "Free" and the estimated total is S.

SPEC-8: Remove and empty cart. `line-remove` asks for confirmation; `remove-cancel` changes nothing and `remove-confirm`
removes the line and updates totals and the badge. Removing the last line shows `cart-empty` and hides
`checkout-button`.

SPEC-9: Shipping location. While the location is "Global (Select Location)", `ship-error` reads "Unable to ship to
Global (Select Location)" and `checkout-button` is disabled. In the preferences dialog, `prefs-currency` is disabled
until a location is chosen, and choosing a location selects its default currency. After saving a shippable location the
error disappears and checkout is enabled.

## Acceptance

`acceptance.json` turns each Spec Item into browser checks. A build passes the task when every check passes; partial
scores are reported per Spec Item.
