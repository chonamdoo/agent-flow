# DDD Design — Cart summary

## Domain Model

- Bounded Context: `Shopping` (cart pricing for the cart page).
- Ubiquitous Language: cart line, unit price, list price, line total, discount rate, subtotal, total discount,
  shipping, free-shipping threshold, free-shipping remaining, estimated total, cart view.
- Entities: none (a quote is computed). Value Objects: money in integer cents, quantity (1–5), discount rate.
- Domain Invariants: every amount is integer cents; line total = unit price × quantity; subtotal = sum of line
  totals; total discount = sum of (list price − unit price) × quantity; shipping is 0 from the 60.00 threshold,
  otherwise 25.00; estimated total = subtotal + shipping; quantity is 1–5.
- Domain Flow: request lines → look up catalog → validate quantities → price lines → totals → shipping.

## Architecture Boundary Map

| Role | Path | Owns |
|---|---|---|
| api-domain | `api/src/shop/cart/domain.py` | `quote_cart`, `CartQuote`, `QuotedLine`, `UnknownProduct`, `QuantityOutOfRange` |
| api-adapter | `api/src/shop/cart/http.py` | `build_cart_quote_handler(catalog)`, request parsing, money strings, status codes |
| core-domain | `src/core/domain/cart/` | `CartQuote`, `CartRepository` port, money formatting |
| core-data | `src/core/data/cart/` | `HttpCartRepository` (takes `postJson`), DTO → domain mapping |
| feature-presentation | `src/features/cart/presentation/` | `CartViewState`, `initialCartViewState`, `loadCartViewState(repository, lines)` |

## Dependency Rule

API: the adapter depends on the domain; the domain imports no HTTP, JSON or adapter module.
Web: presentation → domain ← data. Domain imports nothing outside `src/core/domain`. Presentation depends on the
`CartRepository` port only, never on `src/core/data`.

## Use Case Boundaries

usecase-interface: n/a
usecase-composition: none
A single computation per request; the handler calls the domain function directly.

## Repository Boundaries

`CartRepository.quote(lines): Promise<CartQuote>` in web domain; `HttpCartRepository` in data adapts
`POST /api/cart/quote`. Transport failures and malformed payloads become the domain `CartQuoteUnavailableError`;
presentation maps only that error to the `error` view state.
