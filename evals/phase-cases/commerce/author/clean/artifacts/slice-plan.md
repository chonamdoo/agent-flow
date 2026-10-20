# Slice Plan — Cart summary

Task: quote a cart at `POST /api/cart/quote` in the Python API and expose the cart view state in the
Next.js app (loading, error, empty, loaded) with display-ready prices and the free-shipping message.

Architecture contract: Clean Architecture (`--architecture-mode clean`, profiles nextjs and python).
DDD: bounded context `Shopping` (cart pricing); ubiquitous language: cart line, unit price, list price,
line total, discount rate, subtotal, shipping, free-shipping threshold, estimated total, cart view.

## Product Decisions

Decided with product on 2026-10-14; the PRD's examples do not cover them.

- Discount rate: `floor((list - unit) × 100 / list)` in whole percent — never rounded up. A 71.00 item sold at
  57.66 is an 18% discount, not 19%. A line sold at its list price has a 0% rate and shows no discount badge.
- Free shipping starts *at* the threshold: a subtotal of exactly 60.00 ships free and has 0.00 remaining.
- The item count label counts units, not lines: `(1 item)` for a single unit, otherwise `(<n> items)` —
  two of one product and one of another is `(3 items)`.
- The cart's total discount is shown with a leading minus, `-US$13.34`; with no discount the label is absent
  (`null`), never `-US$0.00`.

## Slice 1 — Cart pricing in the API

- Files: api/src/shop/cart/__init__.py, api/src/shop/cart/domain.py, api/src/shop/cart/http.py
- Scope: a pure pricing function over integer cents that takes the requested lines and the injected catalog
  (`product_id` → `name`, `price_cents`, `list_price_cents`) and returns the quote, raising
  domain errors for an unknown product or a quantity outside 1–5; an HTTP handler built from the catalog that
  accepts the JSON body, maps domain errors to 404/422 and formats money as two-decimal strings.
- Boundary: domain `api/src/shop/cart/domain.py` (no HTTP, no JSON, no string money); adapter
  `api/src/shop/cart/http.py` (`build_cart_quote_handler(catalog)` returning `(status, body)`).
- Verification: `PYTHONPATH=api/src python3 -m unittest discover -s api/tests -t api`.

## Slice 2 — Cart quote in the web app

- Files: src/core/domain/cart/cartQuote.ts, src/core/domain/cart/CartRepository.ts, src/core/data/cart/HttpCartRepository.ts
- Scope: post the lines to `/api/cart/quote` through an injected `postJson(path, body)` and map the response to a
  domain quote with integer cents (parse the decimal strings exactly; no floating point).
- Boundary: core-domain `src/core/domain/cart/` (CartQuote, CartRepository port); core-data
  `src/core/data/cart/` (HttpCartRepository adapter and DTO mapping).
- Verification: `node --test "tests/*.test.ts"`.

## Slice 3 — Cart view state

- Files: src/core/domain/cart/money.ts, src/features/cart/presentation/cartViewState.ts
- Scope: `initialCartViewState` is loading; `loadCartViewState(repository, lines)` returns `empty` for no lines
  without calling the repository, `error` when the quote fails, otherwise `loaded` with each line's
  `unitPrice`, `lineTotal` and `discountLabel` (`"18%"` or `null`), the cart's `itemCountLabel`, `subtotal`,
  `discountLabel`, `shipping` (`"US$25.00"` or `"Free"`), `shippingMessage` and `estimatedTotal`, applying the
  product decisions above.
- Boundary: core-domain money formatting; feature-presentation `src/features/cart/presentation/` depends on the
  `CartRepository` port only.
- Verification: `node --test "tests/*.test.ts"`.

## Out of Scope

- Routes and components, checkout, coupons, quantity editing.
