# DDD Design — Cart quote

- Bounded context: Shopping (cart pricing).
- Ubiquitous language: cart line, unit, quantity, subtotal, shipping, free-shipping threshold, sales tax, total, item count.
- Domain: `quote_cart` is a pure function over integer cents; `Quote` is a value object.
- Adapter: `build_cart_quote_handler` owns JSON parsing, status codes and money formatting.
- Invariants: quantity 1–5 per line; tax is 8.25% of the subtotal rounded half up to the cent; the product decisions
  in slice-plan.md are domain rules.
- Compatibility: the iOS app v1, still in the field, sends `quantity` as a numeric string (`"2"`). The adapter keeps
  accepting numeric strings and integers alike until v1 is retired; anything else is an invalid line (422).
