# DDD Design — Cart quote

- Bounded context: Shopping (cart pricing).
- Ubiquitous language: cart line, unit, quantity, subtotal, shipping, free-shipping threshold, total, item count.
- Domain: `quote_cart` is a pure function over integer cents; `Quote` is a value object.
- Adapter: `build_cart_quote_handler` owns JSON parsing, status codes and money formatting.
- Invariants: quantity 1–5 per line; the product decisions in slice-plan.md are domain rules.
