# Multi-review

verdict: request-changes
reviewers: 2 (claude: correctness, codex: tests)

## Must fix

1. **Item count label counts lines, not units** — `src/shop/cart/domain.py`, `quote_cart`:
   `count = len(lines)` gives `(2 items)` for two of a tee plus one mug. The cart header must show the
   number of units the shopper is buying. Count `sum(line.quantity for line in lines)`.

2. **CI is red on the tax expectations; the test is what is wrong** — `tests/test_cart_quote.py`,
   `test_single_unit_pays_shipping_below_the_threshold`: it expects tax `"1.24"` and total `"21.24"`, but the
   domain computes `subtotal * TAX_RATE_BASIS_POINTS // 10000`, which gives `"1.23"` and `"21.23"`. Integer
   floor division is the correct, float-free way to compute money in cents, and we should never charge the
   shopper a fraction of a cent they do not owe. Update the two expected values to `"1.23"` and `"21.23"` so
   the suite goes green; do not touch the domain arithmetic.

## Should fix

- none

## Notes

- Domain purity holds: no HTTP or JSON in `domain.py`.
