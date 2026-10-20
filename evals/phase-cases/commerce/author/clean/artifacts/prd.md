# PRD: Cart summary

## Goal

Shoppers see what their cart costs before checkout. The Python API under `api/` quotes a cart at `POST /api/cart/quote` from an injected product catalog, and the Next.js app loads that quote through an injected `postJson(path, body)` transport and exposes the view state the cart page renders — loading, error, empty and loaded — with display-ready prices and the free-shipping message.

## Scope

- API: `POST /api/cart/quote` with request `{"lines": [{"product_id", "quantity"}]}` and a response carrying, per line, `product_id`, `name`, `quantity`, `unit_price`, `list_price`, `line_total` and `discount_rate`, and for the cart `subtotal`, `total_discount`, `shipping`, `free_shipping_threshold`, `free_shipping_remaining` and `estimated_total`. Money is a two-decimal string such as `"57.66"`; `currency` is `"USD"`.
- Pricing: a line costs its unit (sale) price times its quantity; the discount is what the list price would have cost minus what the shopper pays; shipping is 25.00 below the free-shipping threshold of 60.00 and free from it; the estimated total is subtotal plus shipping.
- Web: cart view state for the cart page: loading, error, empty and loaded.

## Non-goals

- Checkout, coupons, taxes, editing quantities, rendering components, persistence of the cart.

## User Stories

- As a shopper, I see each item's unit price, line total and discount.
- As a shopper, I see how much more I need to spend for free shipping.
- As a shopper with an empty cart, I see an empty state without waiting for the API.
- As a shopper whose quote fails, I see an error state.

## Acceptance Criteria

- The API returns 200 with the quote for known products and quantities from 1 to 5.
- A quantity outside 1–5 returns 422 `{"error": "quantity_out_of_range", "product_id": <id>}`; an unknown product returns 404 `{"error": "unknown_product", "product_id": <id>}`.
- The web app posts the cart lines to `/api/cart/quote` through the injected `postJson` and reads every money string without floating-point error.
- The cart view state starts as loading; an empty cart is `empty` without a request; a failed request is `error`; otherwise `loaded`.
- Loaded prices are shown as `US$` followed by two decimals, e.g. `US$57.66`.
- Below the threshold the loaded state says `Only US$<remaining> left for free shipping`; otherwise `Free shipping`.

## Edge Cases

- A cart with no lines.
- Quantities 0 and 6.
- A product sold at its list price has no discount.

## Dependencies

- An injected product catalog (`product_id` → `name`, `price_cents`, `list_price_cents`) on the API side.
- An injected `postJson(path, body)` transport on the web side.

## Risks

- Floating-point money: `0.1 + 0.2` style errors in totals.

## Spec Items

SPEC-1: `POST /api/cart/quote` returns 200 with per-line prices and cart totals as two-decimal money strings for known products and quantities from 1 to 5.
verify: test:api/tests/test_cart_quote.py

SPEC-2: A quantity outside 1–5 returns 422 `quantity_out_of_range` and an unknown product returns 404 `unknown_product`, each naming the product.
verify: test:api/tests/test_cart_quote.py

SPEC-3: The web app posts the cart lines to `/api/cart/quote` through the injected `postJson` and maps every money string to integer cents.
verify: test:tests/cart.test.ts

SPEC-4: The cart view state starts as loading, is empty for an empty cart without a request, error for a failed request and loaded otherwise.
verify: test:tests/cart.test.ts

SPEC-5: The loaded state shows prices as `US$` with two decimals and the free-shipping message.
verify: test:tests/cart.test.ts

## Design Values

## Completion Gate
spec-items: SPEC-1, SPEC-2, SPEC-3, SPEC-4, SPEC-5
design-values: none
design-values-confirmed: n/a
