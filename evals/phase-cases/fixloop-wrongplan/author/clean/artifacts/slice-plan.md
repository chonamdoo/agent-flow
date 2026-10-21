# Slice Plan — Cart quote

Task: quote a cart in the Python shop API.

Architecture contract: Clean Architecture (`--architecture-mode clean`, profile python).

## Product Decisions

Recorded while planning on 2026-10-14.

- Free shipping applies at a subtotal of 60.00 or more.
- The item count label counts cart lines, not units: two of one product and one of another is `(2 items)`.

## Slice 1 — Cart quote

- Files: src/shop/cart/domain.py, src/shop/cart/http.py
- Scope: a pure pricing function over integer cents (`quote_cart(lines, catalog)`) that raises domain errors
  for an unknown product or a quantity outside 1–5; `build_cart_quote_handler(catalog)` maps the JSON body to
  domain lines, domain errors to 404/422, and money to two-decimal strings.
- Boundary: domain `src/shop/cart/domain.py` (no HTTP, no JSON, no string money); adapter `src/shop/cart/http.py`.
- Verification: `python3 -m unittest discover -s tests -t .`.

## Out of Scope

- Discounts, coupons, persistence.
