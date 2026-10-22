# PRD: global beauty commerce site (PRD layer, v1)

- Task: `oy-global-fullstack`, version 1. It extends `beauty-store` v1 (same catalog, same screens) with a backend,
  multiple currencies and per-country shipping.
- Source: structure and flows observed on a public cross-border beauty commerce site as an anonymous visitor. No
  trademark, logo, real product name, image or price is used. The store is "Store"; brands are "Brand 1–8".
- Data: `fixtures/catalog.json` and `fixtures/commerce.json` only. For shipping, currencies and the quantity cap,
  `commerce.json` replaces the catalog's `constants`, `locations[].shippable` and `currencies`; location names come
  from the catalog. Run, routes, API and test ids: `contract/interface.md`, `contract/openapi.yaml`.

## Goal

A shopper can find products from the header search, browse a category, open a product, choose an option and quantity,
add it to a server-side cart, choose a shipping country and currency, and see totals and free-shipping progress update
without page reloads, on desktop and on a phone.

## Non-goals

Accounts, wishlist, payment, checkout beyond a placeholder, writing reviews, personalisation, analytics, images (use
plain coloured blocks), translation.

## Viewports

Every Spec Item holds at 1280×900 and at 390×844 (touch). At both sizes no page scrolls horizontally, and no required
element is cut off or covered by another element.

## Money

The API speaks USD (`"24.99"`). The display currency's amount is the USD amount × `perUsd`, rounded half-up to the
currency's `minorUnits`, shown with its `symbol` (e.g. `$24.99`, `¥3,736`, `€22.99`). Each displayed amount is
converted on its own, so a total may differ from the sum of displayed parts by one minor unit. `currency-display`
shows the current currency code.

## Spec Items

SPEC-1: Header search. Focusing the empty input opens `search-layer` with `popular-searches` (the 10 fixture terms, in
order) and `recent-searches` (hidden when empty). Typing shows suggestions from `/api/search/suggest` only after the
input has been idle for 200 ms: at most 2 `suggest-brand`, 2 `suggest-category` and 10 `suggest-keyword`. Escape closes
the layer. Enter with a non-empty trimmed query navigates to `/search?q=<query>`, shows it in `search-query`, and puts
it first in recent searches (no duplicates, at most 30, kept in the browser). An empty or whitespace query does nothing.

SPEC-2: Home hero carousel. It auto-advances every 6 s and shows `hero-counter` as "n / total". `hero-toggle` stops and
restarts auto-advance. Prev and next move one slide and wrap around. On a touch viewport a horizontal swipe also moves
one slide.

SPEC-3: Product cards and rails. A card shows brand and name. A discounted product shows a struck `price-original` and a
`price-sale`; a product whose options differ in price shows only `price-range` as "<min> - <max>"; an undiscounted
single-price product shows only `price-sale`. The best-seller rail shows `rank` 1..N. A rail's `rail-prev` is disabled
at the start and `rail-next` at the end.

SPEC-4: Category listing. `/category/<id>` shows the category name, "N items" counting the whole subtree, the sort
control ("Most Popular", "Price Low to High", "Price High to Low"; default "Most Popular") and the first page from
`/api/products`. There is one `filter-<id>` checkbox per descendant category; several show the union. `load-more` reads
"MORE (k/P)" with P = ceil(N / page size), appends the next page and hides on the last page. Changing the sort or
`page-size` (24, 36, 48) returns to page 1. A filter adds a `refine-chip` and keeps the sort; removing the chip or
`clear-all` restores the count. No products shows `empty-result` with "Sorry, there is no item matching the applied
filter." and no `load-more`.

SPEC-5: Product detail and options. Clicking a card opens `/product/<id>` with `breadcrumb` (e.g. "Home > Skincare >
Toners > Mists"), brand, name and prices. For a product with options, before a choice `qty-minus` and `qty-plus` are
disabled and `add-to-cart` reads "Add to Cart"; pressing it opens `option-list`. Sold-out options are disabled and read
"Sold out". Choosing an option closes the list, sets the quantity to 1 and makes the button read "Add to Cart -
<sale × qty>". A product without options starts at quantity 1 with that label. Plus stops at min(10, stock); minus
stops at 1.

SPEC-6: Server-side cart. Adding posts to `/api/cart/lines`, opens `added-dialog` with `go-to-cart`, and `cart-badge`
shows the number of distinct lines. The same product option again increases that line's quantity. The cart belongs to
the `sid` session cookie: another browser session sees its own cart, and the cart is still there after the server
restarts. The server prices every line from the catalog and ignores prices sent by the client.

SPEC-7: Cart totals and free shipping. Each `cart-line` has a stepper (1..min(10, stock)) and `line-remove`. Changing a
quantity updates `line-total`, `subtotal`, `shipping-fee` and `estimated-total` without reload, from
`/api/shipping/quote`. For the chosen country with free-shipping threshold T (USD) and subtotal S: while S < T,
`free-shipping-msg` reads "Only <T − S> left for Free Shipping!", `free-shipping-bar` is at round(100 × S / T), and
shipping is the country's flat fee; when S ≥ T the message reads "You've got Free Shipping!", the bar is at 100,
shipping reads "Free" and the total is S. All amounts are shown in the display currency.

SPEC-8: Remove and empty cart. `line-remove` asks for confirmation; `remove-cancel` changes nothing and `remove-confirm`
removes the line and updates totals and the badge. Removing the last line shows `cart-empty` and hides
`checkout-button`.

SPEC-9: Country and currency. The preferences are stored with `/api/prefs` for the session. While the country is
"GLOBAL", `ship-error` reads "Unable to ship to Global (Select Location)" and `checkout-button` is disabled. In the
preferences dialog `prefs-currency` is disabled until a country is chosen, and choosing a country selects its default
currency; the shopper may then pick another currency. After saving, prices on every screen use the new currency
without a reload, the shipping fee and threshold follow the new country, and the error disappears for a shippable
country.

SPEC-10: Failure states. When an API request a screen needs fails (5xx or network error), that screen shows `api-error`
and `api-retry` instead of empty or stale data, the rest of the page keeps working, and `api-retry` repeats the request.
A failed add-to-cart, quantity change or removal leaves the cart display as it was before the action. No uncaught
exception reaches the browser console in any Spec Item, failure states included.

## Acceptance

A private evaluator scores the build: browser checks for each Spec Item at both viewports, API contract and
robustness tests against `contract/openapi.yaml`, and structural checks. A build passes when every check passes three
times in a row.
