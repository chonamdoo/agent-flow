# Slice Plan: orders listing endpoint

Source: task request (orders listing handler for one customer).

## Functional Requirements

- FR-1 `build_list_orders_handler(rows)` returns `handler(query) -> {"status", "body"}`.
- FR-2 `customer_id` is required; missing -> `400 {"error": "missing_customer_id"}`.
- FR-3 Optional `status` filter over `pending|paid|shipped|cancelled`; unknown -> `400 {"error": "invalid_status"}`.
- FR-4 Orders are that customer's only, newest `placed_at` first (compared as UTC instants), as `{"id", "status", "total", "placed_at"}`; `total` is `total_cents` as an exact two-decimal string (no float), `placed_at` echoes the stored `YYYY-MM-DDTHH:MM:SSZ` value.
- FR-5 Empty state: when the resulting order list is empty (no orders for the customer, or none for the status filter), respond `200` with `body["orders"] == []` and `body["message"]` set to exactly `"Nothing to show - no orders match these filters."` (product-approved copy; keep it byte-for-byte, including the ASCII hyphen). Non-empty responses carry no `message` key.

## Architecture Contract

The project selects `local` architecture with `skills/architecture/SKILL.md` before this implementation. That contract approves extending the existing inventory ownership, typed-row loader and container wiring to Ordering. Team conventions from `skills/orders-team-conventions/SKILL.md` apply.

## Slices

### Slice 1 - Order records and listing
- Files: src/domain/orders/__init__.py, src/domain/orders/models.py, src/domain/orders/listing.py
- Boundary scope: `src/domain/orders/` - `OrderRowDTO` and `Order` records, `list_customer_orders` (customer scope, status filter, newest first).
- Bounded context: Ordering. Domain concerns: order status vocabulary, per-customer listing.
- Verification: `python3 -m unittest discover -s tests -t .`.

### Slice 2 - HTTP handler and wiring
- Files: src/app/orders_http.py, src/app/container.py
- Boundary scope: `src/app/orders_http.py` (query validation, error bodies, response mapping, FR-5 message); `src/app/container.py` converts stored rows to `OrderRowDTO` once and binds the loader, as `build_stock_level_handler` does.
- Verification: full visible suite plus FR-5 empty-state cases.
