# DDD Design: orders listing

## Bounded Context
Ordering - the customer's view of placed orders.

## Ubiquitous Language
- Order: a placed purchase identified by `order_id`, owned by one customer.
- Order status: one of `pending`, `paid`, `shipped`, `cancelled`.
- Order total: integer minor units (`total_cents`); rendered as an exact two-decimal string only at the HTTP boundary.
- Listing: one customer's orders, newest `placed_at` first, optionally narrowed to one status.

## Entities
- Order (identity `order_id`).

## Value Objects
- Order status; order total in minor units; placement instant (UTC).

## Aggregates
- Order is its own aggregate; listing is a read-only query.

## Domain Events
- None (read-only query).

## Domain Invariants
- A listing only contains orders of the requested customer.
- Status values outside the vocabulary are rejected before listing.

## Domain Flow
query -> validate customer_id/status -> list customer orders -> filter -> sort newest first -> map to response (empty state per slice-plan FR-5).

## Architecture Boundary Map
No architecture contract is selected (pending); the change follows the existing `inventory` pattern and adds no architecture decision. Team conventions: record types are `typing.NamedTuple`; no dataclasses under `src/`.
- `src/domain/orders/models.py`: `OrderRowDTO`, `Order` records, status vocabulary.
- `src/domain/orders/listing.py`: `list_customer_orders`.
- `src/app/orders_http.py`: handler; `src/app/container.py`: wiring.

## Composition Root
`app.container.build_list_orders_handler(rows)` converts rows to `OrderRowDTO` once and binds a loader into the handler, like `build_stock_level_handler`.

## Testability Boundary
Tests drive the handler built by the container with in-memory rows.

## Completion Gate
architecture-contract: applied
solid-srp-change-reason: records, listing rules, and HTTP mapping are separate modules.
solid-ocp-extension-points: a database loader produces `OrderRowDTO` records for the same listing.
solid-lsp-contracts: any loader yields `OrderRowDTO` values.
solid-isp-consumer-ports: listing needs only an iterable of rows.
solid-dip-dependency-direction: the container supplies the loader; listing does not know the store.
