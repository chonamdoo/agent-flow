# Multi-review

verdict: request-changes
reviewers: 2 (claude: correctness, codex: spec)

## Must fix

1. **Item count label counts lines, not units** — `src/shop/cart/domain.py`, `quote_cart`:
   `count = len(lines)` gives `(2 items)` for two of a tee plus one mug. The cart header must show the
   number of units the shopper is buying. Count `sum(line.quantity for line in lines)`.

2. **Free shipping applies at exactly US$60.00** — `src/shop/cart/domain.py`, `quote_cart`:
   `free = subtotal >= FREE_SHIPPING_THRESHOLD_CENTS` ships a 60.00 cart free, but the PRD says
   "Free shipping on orders **over** US$60". A cart of exactly 60.00 is not over 60 and must pay the
   US$5.00 shipping. Change the comparison to `>` and add a test for the 60.00 boundary.

## Should fix

- none

## Notes

- Domain purity holds: no HTTP or JSON in `domain.py`.
