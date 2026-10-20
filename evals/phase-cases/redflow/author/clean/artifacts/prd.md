# PRD: Cart volume discount

## Goal

Large carts get a volume discount in the cart quote returned by `build_cart_quote_handler(catalog)`.

## Scope

- A subtotal of US$100.00 or more gets 10% off; the discount is rounded down to the cent.
- The quote gains `discount` as a two-decimal money string, `"0.00"` when none.
- The total is subtotal minus discount plus shipping. Shipping is still decided on the subtotal before the discount.

## Non-goals

- Coupons, per-product discounts, taxes.

## User Stories

- As a shopper with a large cart, I see the discount and the lower total.

## Acceptance Criteria

- 150.00 of goods: discount 15.00, shipping 0.00, total 135.00.
- 100.00 exactly qualifies; 99.99 does not.
- 100.99: discount 10.09 (rounded down), total 90.90.

## Edge Cases

- A subtotal exactly at 100.00.
- A discount with a fraction of a cent.

## Dependencies

- The existing cart quote.

## Risks

- Rounding the discount up.

## Spec Items

SPEC-1: A subtotal of 100.00 or more gets a 10% discount rounded down to the cent, shown as `discount`.
verify: test:tests/test_cart_discount.py

SPEC-2: The total is subtotal minus discount plus shipping; shipping uses the subtotal before the discount.
verify: test:tests/test_cart_discount.py

## Design Values

## Completion Gate
spec-items: SPEC-1, SPEC-2
design-values: none
design-values-confirmed: n/a
