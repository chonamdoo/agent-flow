# Slice Plan: orders listing endpoint

Source: task request (orders listing handler for one customer).

## Functional Requirements

- FR-1 `build_list_orders_handler(rows)` returns `handler(query) -> {"status", "body"}`.
- FR-2 `customer_id` is required; missing -> `400 {"error": "missing_customer_id"}`.
- FR-3 Optional `status` filter over `pending|paid|shipped|cancelled`; unknown -> `400 {"error": "invalid_status"}`.
- FR-4 Orders are that customer's only, newest `placed_at` first (compared as UTC instants), as `{"id", "status", "total", "placed_at"}`; `total` is `total_cents` as an exact two-decimal string (no float), `placed_at` echoes the stored `YYYY-MM-DDTHH:MM:SSZ` value.
- FR-5 Empty state: when the resulting order list is empty (no orders for the customer, or none for the status filter), respond `200` with `body["orders"] == []` and `body["message"]` set to exactly `"Nothing to show - no orders match these filters."` (product-approved copy; keep it byte-for-byte, including the ASCII hyphen). Non-empty responses carry no `message` key.

## Architecture Contract

Selected contract: Clean (`clean-architecture-core` + `python-api-clean-architecture`).

## Slices

### Slice 1 - Order domain and listing use case
- Files: src/domain/__init__.py, src/domain/orders/__init__.py, src/domain/orders/order.py, src/domain/orders/order_repository.py, src/domain/orders/list_orders.py
- Boundary scope: core-domain `src/domain/orders/` - `Order`, `OrderStatus`, `OrderRepository` Protocol port, `ListOrders` use case (status filter + newest-first sort).
- Bounded context: Ordering. Domain concerns: order status vocabulary, per-customer listing.
- Verification: `python3 -m unittest discover -s tests -t .` (filter/sort cases).

### Slice 2 - Data adapter
- Files: src/data/__init__.py, src/data/orders/__init__.py, src/data/orders/order_row_mapper.py, src/data/orders/in_memory_order_repository.py
- Boundary scope: core-data `src/data/orders/` - row mapper `order_from_row` (parses the stored timestamp), `InMemoryOrderRepository` implementing the domain port; raw rows never leave data.
- Verification: listing tests through the handler.

### Slice 3 - Inbound handler and composition root
- Files: src/api/__init__.py, src/api/orders/__init__.py, src/api/orders/list_orders_handler.py, src/app/__init__.py, src/app/container.py
- Boundary scope: inbound adapter `src/api/orders/` (query parsing, error bodies, response mapping, FR-5 message); app-shell `src/app/container.py` wires repository -> use case -> handler.
- Verification: full visible suite plus FR-5 empty-state cases.
