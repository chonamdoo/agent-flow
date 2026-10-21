# Multi-review

verdict: request-changes
reviewers: 2 (claude: correctness, codex: spec)

## Must fix

1. **Item count label contradicts SPEC-3** — `src/shop/cart/domain.py`, `quote_cart`: `count = len(lines)` gives
   `(2 items)` for two of a tee plus one mug. PRD SPEC-3 says the label counts units, "two of one product and one of
   another is `(3 items)`". The slice plan's product decision says lines, which contradicts the confirmed Spec Item.
   Count `sum(line.quantity for line in lines)`, or stop and get the plan corrected.

## Should fix

- none

## Notes

- Shipping threshold and domain purity match the spec.
