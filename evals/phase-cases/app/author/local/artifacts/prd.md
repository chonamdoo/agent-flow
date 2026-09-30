# PRD: Orders list screen logic

## Goal

Customers can see their orders on an orders list screen. The app loads the customer's orders from the backend and a screen state holder exposes loading, loaded, empty and error states. The logic stays in pure Dart with no Flutter imports.

## Scope

- Load the customer's orders from the backend through a repository.
- A screen state holder with loading, loaded, empty and error states.
- Loaded rows are shown newest first with totals formatted as dollars.

## Non-goals

- Widgets, routing, pull-to-refresh, pagination.

## User Stories

- As a customer, I open the orders screen and see my orders, newest first, with each total in dollars.
- As a customer with no orders, I see an empty state instead of a blank list.
- As a customer whose orders fail to load, I see an error state.

## Acceptance Criteria

- Load the customer's orders from the backend through a repository that maps each record to an order (id, customer name, total cents, placed-at UTC instant).
- The screen state holder starts in the loading state before a load completes.
- Loaded orders are shown newest first.
- Order totals are formatted as dollars with two decimals.
- No orders produce the empty state.
- A failed load produces the error state.

## Edge Cases

- An empty order list produces the empty state, not a loaded state with no rows.
- Whole-dollar and sub-dollar totals keep two decimals (for example `$999.00`, `$3.05`).

## Dependencies

- Backend orders records: `id`, `customer`, `total_cents`, `placed_at` (ISO-8601 UTC).

## Risks

- Totals rendered from floating-point division lose the trailing zero.

## Spec Items

SPEC-1: Load the customer's orders from the backend through a repository that maps each record to an order (id, customer name, total cents, placed-at UTC instant).
verify: test:test/orders_repository_test.dart

SPEC-2: The screen state holder starts in the loading state before a load completes.
verify: test:test/orders_list_holder_test.dart

SPEC-3: Loaded orders are shown newest first.
verify: test:test/orders_list_holder_test.dart

SPEC-4: Order totals are formatted as dollars with two decimals.
verify: test:test/orders_list_holder_test.dart

SPEC-5: No orders produce the empty state.
verify: test:test/orders_list_holder_test.dart

SPEC-6: A failed load produces the error state.
verify: test:test/orders_list_holder_test.dart

## Design Values

## Completion Gate
spec-items: SPEC-1, SPEC-2, SPEC-3, SPEC-4, SPEC-5, SPEC-6
design-values: none
design-values-confirmed: n/a
