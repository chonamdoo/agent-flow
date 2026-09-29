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
Project-local contract `skills/architecture/SKILL.md`: one flat feature package `src/orders/`.
- `ord_service.py`: status vocabulary; `list_customer_orders(load_rows, customer_id, status)` filters, orders, and maps raw rows to response dicts.
- `ord_http.py`: query validation, error bodies, empty-state message, response wrapping.
- `__init__.py`: composition point and public exports.
No domain/data/api packages, no repository port, no row model class: the row source is a plain `load_rows` callable.

## Composition Root
`orders.build_list_orders_handler(rows)` binds a `load_rows` closure over the rows to the handler.

## Testability Boundary
Tests drive the exported handler; a database-backed `load_rows` callable replaces the in-memory one.

## Completion Gate
architecture-contract: applied
solid-srp-change-reason: listing rules and HTTP validation are separate `ord_` modules.
solid-ocp-extension-points: a new row source is a new `load_rows` callable.
solid-lsp-contracts: any `load_rows` returns iterable row mappings with the stored keys.
solid-isp-consumer-ports: the service needs only a zero-argument row callable.
solid-dip-dependency-direction: the service receives `load_rows`; only `__init__.py` binds it.
