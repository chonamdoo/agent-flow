# PRD: Account orders list

## Goal

Signed-in customers see their orders on the account page. `toOrdersViewState(orders)` turns the customer's orders into the view the orders screen renders.

## Scope

- Empty state when the customer has no orders.
- Otherwise one row per order with its id, state and total, newest first.
- Totals as `US$` with two decimals, e.g. `US$12.50`.

## Non-goals

- Fetching orders, pagination, order details, rendering components.

## User Stories

- As a customer, I see my orders with the newest first.
- As a new customer with no orders, I see an empty state.

## Acceptance Criteria

- No orders gives the empty state.
- Orders are listed newest first by `placedAt`.
- Totals read `US$` with two decimals.

## Edge Cases

- A single order.
- Orders in every state.

## Dependencies

- The `Order` domain type.

## Risks

- Floating-point money formatting.

## Spec Items

SPEC-1: No orders gives the empty state.
verify: test:tests/orders.test.ts

SPEC-2: Orders are listed newest first with `US$` two-decimal totals.
verify: test:tests/orders.test.ts

## Design Values

## Completion Gate
spec-items: SPEC-1, SPEC-2
design-values: none
design-values-confirmed: n/a
