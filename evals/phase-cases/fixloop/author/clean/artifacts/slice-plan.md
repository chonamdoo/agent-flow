# Slice Plan — Cart quote

Task: quote a cart in the Python shop API.

Architecture contract: Clean Architecture (`--architecture-mode clean`, profile python).

## Product Decisions

Decided with product on 2026-10-14; the PRD's wording does not settle them.

- "Over US$60" includes 60.00: a subtotal of exactly 60.00 ships free. Marketing copy says "over" but the
  rule product approved is `subtotal >= 60.00`, matching checkout.
- The item count label counts units, not lines: `(1 item)` for a single unit, otherwise `(<n> items)` —
  two of one product and one of another is `(3 items)`.

## Slice 1 — Cart quote

- Files: src/shop/cart/domain.py, src/shop/cart/http.py
- Scope: a pure pricing function over integer cents (`quote_cart(lines, catalog)`) that raises domain errors
  for an unknown product or a quantity outside 1–5; `build_cart_quote_handler(catalog)` maps the JSON body to
  domain lines, domain errors to 404/422, and money to two-decimal strings.
- Boundary: domain `src/shop/cart/domain.py` (no HTTP, no JSON, no string money); adapter `src/shop/cart/http.py`.
- Verification: `python3 -m unittest discover -s tests -t .`.

## Out of Scope

- Discounts, coupons, persistence.
