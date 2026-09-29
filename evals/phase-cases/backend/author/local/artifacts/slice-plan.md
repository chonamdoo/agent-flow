# Slice Plan: orders listing endpoint

Source: task request (orders listing handler for one customer).

## Functional Requirements

- FR-1 `build_list_orders_handler(rows)` returns `handler(query) -> {"status", "body"}`.
- FR-2 `customer_id` is required; missing -> `400 {"error": "missing_customer_id"}`.
- FR-3 Optional `status` filter over `pending|paid|shipped|cancelled`; unknown -> `400 {"error": "invalid_status"}`.
- FR-4 Orders are that customer's only, newest `placed_at` first (compared as UTC instants), as `{"id", "status", "total", "placed_at"}`; `total` is `total_cents` as an exact two-decimal string (no float), `placed_at` echoes the stored `YYYY-MM-DDTHH:MM:SSZ` value.
- FR-5 Empty state: when the resulting order list is empty (no orders for the customer, or none for the status filter), respond `200` with `body["orders"] == []` and `body["message"]` set to exactly `"Nothing to show - no orders match these filters."` (product-approved copy; keep it byte-for-byte, including the ASCII hyphen). Non-empty responses carry no `message` key.

## Architecture Contract

Selected contract: project-local `skills/architecture/SKILL.md` (flat feature packages; no Clean layers, no repository ports; `ord_` module prefix).

## Slices

### Slice 1 - Listing service
- Boundary scope: feature package `src/orders/` - `ord_service.py`: status vocabulary and `list_customer_orders(load_rows, customer_id, status)` over a plain `load_rows` callable (customer scope, status filter, newest first, row -> response dict mapping, total formatting).
- Bounded context: Ordering. Domain concerns: order status vocabulary, per-customer listing.
- Verification: `python3 -m unittest discover -s tests -t .`.

### Slice 2 - HTTP handler and package wiring
- Boundary scope: `src/orders/ord_http.py` (query validation, error bodies, FR-5 message, response wrapping only); `src/orders/__init__.py` binds `load_rows` to the handler and exports `build_list_orders_handler`.
- Verification: full visible suite plus FR-5 empty-state cases.
