# PRD: Orders listing endpoint

## Goal

The Python service exposes an orders listing endpoint. `build_list_orders_handler(rows)` takes the stored order rows and returns a `handler(query) -> {"status": int, "body": dict}` that lists one customer's orders. Order storage stays behind the boundary this codebase expects so a real database can replace it later.

## Scope

- `build_list_orders_handler(rows)` over stored rows (`order_id`, `customer_id`, `status`, `total_cents`, `placed_at`).
- `customer_id` is required; optional `status` filter over `pending`, `paid`, `shipped`, `cancelled`.
- That customer's orders newest first as `{"id", "status", "total", "placed_at"}` with `total` as a decimal string.
- Unknown statuses and a missing `customer_id` return a 400 error body.
- An empty-state message when nothing matches.

## Non-goals

- A real database adapter, pagination, authentication.

## User Stories

- As a client, I request a customer's orders and get them newest first with decimal totals.
- As a client, I filter a customer's orders by status.
- As a client, I get a 400 error body for an unknown status or a missing `customer_id`.

## Acceptance Criteria

- The handler returns only the requested customer's orders.
- Orders are listed newest first.
- Each order is returned as `{"id", "status", "total", "placed_at"}` with the total as a decimal string.
- Totals keep two decimal places.
- An optional `status` query filters the customer's orders.
- An unknown status returns a 400 error body.
- A missing `customer_id` returns a 400 error body.

## Edge Cases

- Another customer's orders never appear in the response.
- Totals keep two decimal places (for example `12.50`, `100.00`).

## Dependencies

- Stored order rows handed to `build_list_orders_handler`.

## Risks

- Order storage must stay replaceable by a real database.

## Spec Items

SPEC-1: The handler returns only the requested customer's orders.
verify: test:tests.test_list_orders.ListOrdersHandlerTest.test_lists_only_the_requested_customers_orders

SPEC-2: Orders are listed newest first.
verify: test:tests.test_list_orders.ListOrdersHandlerTest.test_orders_are_newest_first

SPEC-3: Each order is returned as `{"id", "status", "total", "placed_at"}` with the total as a decimal string.
verify: test:tests.test_list_orders.ListOrdersHandlerTest.test_order_payload_shape_and_money_format

SPEC-4: Totals keep two decimal places.
verify: test:tests.test_list_orders.ListOrdersHandlerTest.test_totals_keep_two_decimal_places

SPEC-5: An optional `status` query filters the customer's orders.
verify: test:tests.test_list_orders.ListOrdersHandlerTest.test_filters_by_status

SPEC-6: An unknown status returns a 400 error body.
verify: test:tests.test_list_orders.ListOrdersHandlerTest.test_rejects_unknown_status

SPEC-7: A missing `customer_id` returns a 400 error body.
verify: test:tests.test_list_orders.ListOrdersHandlerTest.test_requires_customer_id

## Design Values

## Completion Gate
spec-items: SPEC-1, SPEC-2, SPEC-3, SPEC-4, SPEC-5, SPEC-6, SPEC-7
design-values: none
design-values-confirmed: n/a
