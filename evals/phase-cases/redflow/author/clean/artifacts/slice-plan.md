# Slice Plan — Cart volume discount

Task: add a volume discount to the cart quote.

Architecture contract: Clean Architecture (`--architecture-mode clean`, profile python).

## Product Decisions

- The threshold is inclusive: a subtotal of exactly 100.00 gets the discount.
- The discount is `subtotal_cents // 10` (rounded down), computed in the domain over integer cents.
- Shipping is decided on the subtotal before the discount.

## Slice 1 — Discount in the quote

- Files: src/shop/cart/__init__.py, src/shop/cart/domain.py, src/shop/cart/http.py
- Scope: `Quote` gains `discount_cents`; `quote_cart` computes it; the handler adds `"discount"` as a money string and
  the total uses it.
- Boundary: domain `src/shop/cart/domain.py` (no HTTP, no JSON); adapter `src/shop/cart/http.py`.
- Verification: `python3 -m unittest discover -s tests -t .`.

## Out of Scope

- Coupons, per-product discounts.
