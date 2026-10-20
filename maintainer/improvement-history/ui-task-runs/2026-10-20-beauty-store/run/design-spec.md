# Design specification ledger

Source phase: prd
Source artifact SHA-256: cac2fe90b488690b3ee7fd6f450bb9d5f2b008db7c99c0de6a99e721b8d733e6
Spec set SHA-256: 326073e0b2b4eeb8edcfbb8a858d3f6f4ce98685af6d7f524d45a393897ab4c7
Task SHA-256: e3a16371763346332ace02a63a5c1f34bd11d2f4dbf764e22479e5de44b440af

## Spec Items

SPEC-1: Header search. Focusing the empty input opens `search-layer` with `popular-searches` (the 10 fixture terms, in order) and `recent-searches` (hidden when empty). Typing shows suggestions only after the input has been idle for 200 ms: at most 2 `suggest-brand` (brand names), 2 `suggest-category` (category names) and 10 `suggest-keyword` (popular search terms, then product names), each containing the query case-insensitively. Escape closes the layer. Enter with a non-empty trimmed query navigates to `/search?q=<query>`, shows it in `search-query`, and puts it first in recent searches (no duplicates, at most 30). An empty or whitespace query does nothing.
verify: test:spec1-header-search
due: review
SPEC-2: Home hero carousel. It auto-advances every 6 s and shows `hero-counter` as "n / total". `hero-toggle` stops and restarts auto-advance. Prev and next move one slide and wrap around.
verify: test:spec2-hero-carousel
due: review
SPEC-3: Product cards and rails. A card shows brand and name. A discounted product shows a struck `price-original` and a `price-sale`; a product whose options differ in price shows only `price-range` as "$min - $max"; an undiscounted single-price product shows only `price-sale`. The best-seller rail shows `rank` 1..N. A rail's `rail-prev` is disabled at the start and `rail-next` at the end.
verify: test:spec3-cards-rails
due: review
SPEC-4: Category listing. `/category/<id>` shows the category name, "N items" counting the whole subtree, the sort control (options "Most Popular" by `popularity` descending, "Price Low to High" and "Price High to Low" by the product's `salePrice`) defaulting to "Most Popular", and the first 24 cards. There is one `filter-<id>` checkbox for each descendant category; ticking several shows the union. `load-more` reads "MORE (k/P)" with P = ceil(N / page size), appends the next page, and hides on the last page. Changing the sort returns to page 1. Ticking a sub-category filter narrows the count to that subtree, adds a `refine-chip`, and keeps the sort; removing the chip or `clear-all` restores the count. A category or filter set with no products shows `empty-result` with the text "Sorry, there is no item matching the applied filter." and no `load-more`. `page-size` offers "24", "36" and "48"; changing it returns to page 1 and recomputes P.
verify: test:spec4-category-listing
due: review
SPEC-5: Product detail and options. Clicking a card opens `/product/<id>`. The page shows `breadcrumb` (the category path, e.g. "Home > Skincare > Toners > Mists"), brand, name and prices; `qty-value` shows the quantity as text or as an input value. For a product with options, before a choice `qty-minus` and `qty-plus` are disabled and `add-to-cart` reads "Add to Cart"; pressing it opens `option-list` instead of adding. Sold-out options are disabled and read "Sold out". Choosing an option closes the list, sets the quantity to 1 and makes the button read "Add to Cart - $<sale × qty>". A product without options starts at quantity 1 with that label. Plus stops at min(10, stock); minus stops at 1. A card or price range over options uses their sale prices, sold-out options included.
verify: test:spec5-product-detail
due: review
SPEC-6: Add to cart. Adding opens `added-dialog` with `go-to-cart`, and `cart-badge` shows the number of distinct lines. Adding the same product option again increases that line's quantity instead of adding a line.
verify: test:spec6-add-to-cart
due: review
SPEC-7: Cart totals and free shipping. Each `cart-line` has a stepper (1..min(10, stock)) and `line-remove`. Changing quantity updates `line-total`, `subtotal`, `shipping-fee` and `estimated-total` without reload. With subtotal S < 60.00, `free-shipping-msg` reads "Only $<60.00 − S> left for Free Shipping!", `free-shipping-bar` is at round(100 × S / 60), shipping is $25.00 and the estimated total is S + 25.00. With S ≥ 60.00 the message reads "You've got Free Shipping!", the bar is at 100, shipping is "Free" and the estimated total is S.
verify: test:spec7-cart-totals
due: review
SPEC-8: Remove and empty cart. `line-remove` asks for confirmation; `remove-cancel` changes nothing and `remove-confirm` removes the line and updates totals and the badge. Removing the last line shows `cart-empty` and hides `checkout-button`.
verify: test:spec8-remove-empty
due: review
SPEC-9: Shipping location. While the location is "Global (Select Location)", `ship-error` reads "Unable to ship to Global (Select Location)" and `checkout-button` is disabled. In the preferences dialog, `prefs-currency` is disabled until a location is chosen, and choosing a location selects its default currency. After saving a shippable location the error disappears and checkout is enabled.
verify: test:spec9-shipping-location
due: review
SPEC-10: Test-id contract. Every element named in the `docs/source-prd.md` test-id table carries exactly that `data-testid`; ids that are not per item appear at most once in the page, hidden copies included; `sort-select`, `page-size`, `prefs-location`, `prefs-currency` are native `<select>`; `filter-<categoryId>` are checkboxes; `free-shipping-bar` carries `aria-valuenow` 0–100.
verify: test:spec10-testid-contract
due: review
SPEC-11: Persistence and data. The cart, recent searches and the saved shipping location and currency persist in `localStorage` across reloads; `fixtures/catalog.json` is the only data source; money is shown as `$` with two decimals.
verify: test:spec11-persistence
due: review

## Design Values

free-shipping-threshold: 60.00
flat-shipping: 25.00
max-qty-per-line: 10
listing-page-size-default: 24
listing-page-sizes: 24, 36, 48
money-format: $ with two decimals
search-suggest-idle: 200 ms
suggest-brand-max: 2
suggest-category-max: 2
suggest-keyword-max: 10
popular-searches-count: 10
recent-searches-max: 30
hero-auto-advance: 6 s
hero-counter-format: n / total
price-range-format: $min - $max
load-more-label: MORE (k/P)
empty-result-text: Sorry, there is no item matching the applied filter.
free-shipping-msg-below: Only $<60.00 − S> left for Free Shipping!
free-shipping-msg-reached: You've got Free Shipping!
shipping-free-label: Free
ship-error-text: Unable to ship to Global (Select Location)
add-to-cart-label: Add to Cart
add-to-cart-priced-label: Add to Cart - $<sale × qty>
sold-out-label: Sold out
breadcrumb-format: Home > Skincare > Toners > Mists
