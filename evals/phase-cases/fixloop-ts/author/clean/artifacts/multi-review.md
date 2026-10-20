# Multi-review

verdict: request-changes
reviewers: 2 (claude: correctness, codex: ux)

## Must fix

1. **A single order shows the empty state** — `src/features/orders/presentation/ordersViewState.ts`,
   `toOrdersViewState`: `orders.length <= 1` returns `{kind: "empty"}` for a customer with exactly one order, who then
   sees "no orders". The empty state is for no orders only; use `orders.length === 0`.

2. **Cancelled orders clutter the list** — `src/features/orders/presentation/ordersViewState.ts`,
   `toOrdersViewState`: cancelled orders are listed next to live ones. The PRD's goal is that customers "see their
   orders"; a cancelled order is not an order they have. Filter out `state === "CANCELLED"` before mapping rows and
   add a test.

## Should fix

- none

## Notes

- Presentation depends on the domain type only.
