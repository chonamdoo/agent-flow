# Multi-review

verdict: request-changes
reviewers: 2 (claude: correctness, codex: spec)

## Must fix

1. **Item count label counts lines, not units** — `src/shop/cart/domain.py`, `quote_cart`:
   `count = len(lines)` gives `(2 items)` for two of a tee plus one mug. The cart header must show the
   number of units the shopper is buying. Count `sum(line.quantity for line in lines)`.

2. **Tax is truncated instead of rounded** — `src/shop/cart/domain.py`, `quote_cart`:
   `subtotal * TAX_RATE_BASIS_POINTS // 10000` floors the tax, so a 15.00 subtotal carries 1.23 instead of
   the 1.24 the test expects and SPEC-4 requires rounding to the cent. Round it:
   `tax = round(subtotal * TAX_RATE_BASIS_POINTS / 10000)`. That makes
   `test_single_unit_pays_shipping_below_the_threshold` pass.

## Should fix

- none

## Notes

- Domain purity holds: no HTTP or JSON in `domain.py`.
