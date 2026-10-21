# PRD: Cart quote

## Goal

Shoppers see what their cart costs before checkout. The Python shop API quotes a cart through `build_cart_quote_handler(catalog)`, which answers a POST body `{"lines": [{"product_id", "quantity"}]}`.

## Scope

- Quote: subtotal, shipping and total as two-decimal money strings such as `"57.66"`, whether shipping is free, and the item count label for the cart header.
- Shipping: free shipping on orders over US$60; otherwise a flat US$5.00.
- Item count label: `(1 item)`, `(4 items)`.

## Non-goals

- Discounts, coupons, taxes, persistence, the web app.

## User Stories

- As a shopper, I see my subtotal, shipping and total.
- As a shopper, I see how many items are in my cart.

## Acceptance Criteria

- Known products with quantities from 1 to 5 return 200 with the quote.
- An unknown product returns 404 `{"error": "unknown_product", "product_id": <id>}`; an invalid line returns 422 `{"error": "invalid_line"}`.
- Free shipping on orders of US$60.00 or more; otherwise shipping is 5.00.
- The item count label counts units: two of one product and one of another is `(3 items)`.

## Edge Cases

- Quantities 0 and 6.
- A subtotal right at the free-shipping threshold.

## Dependencies

- An injected product catalog (`product_id` → `price_cents`).

## Risks

- Floating-point money errors in totals.

## Spec Items

SPEC-1: Known products with quantities 1–5 return 200 with subtotal, shipping and total as two-decimal money strings and `free_shipping`.
verify: test:tests/test_cart_quote.py

SPEC-2: An unknown product returns 404 `unknown_product`; an invalid line returns 422.
verify: test:tests/test_cart_quote.py

SPEC-3: The item count label counts units, not lines: `(1 item)` for a single unit, otherwise `(<n> items)`; two of one product and one of another is `(3 items)`.
verify: test:tests/test_cart_quote.py

## Design Values

## Completion Gate
spec-items: SPEC-1, SPEC-2, SPEC-3
design-values: none
design-values-confirmed: n/a
