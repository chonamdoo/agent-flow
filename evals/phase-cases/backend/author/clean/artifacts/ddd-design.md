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
- core-domain `src/domain/orders/`: `Order`, `OrderStatus`, `OrderRepository` (Protocol port), `ListOrders` use case (profile roles map no separate application path, so the use case stays with its context).
- core-data `src/data/orders/`: `OrderRow` stored-row type, `order_from_row` mapper, `InMemoryOrderRepository` implementing `OrderRepository`.
- inbound-adapter `src/api/orders/list_orders_handler.py`: `Response` model, query -> `OrderStatus`, result -> response body, error bodies.
- app-shell `src/app/container.py`: composition only.

## Dependency Rule
api -> domain; data -> domain; app -> api, data, domain. Domain imports only stdlib value tools (`dataclasses`, `datetime`, `enum`, `typing`).

## Use Case Boundaries
`ListOrders(customer_id, status)` is the single user intent.
usecase-interface: optional
usecase-composition: none

## Repository Boundaries
`OrderRepository.orders_for_customer(customer_id) -> list[Order]` lives in domain; the in-memory implementation in data is the single source of truth until a database adapter replaces it.

## Cache Boundary
cache-required: no
memory-cache: n/a
disk-cache: n/a
cache-invalidation-policy: n/a - rows are read per call.

## Mapping Boundary
remote-dto-domain-mapper: n/a
entity-domain-mapper: required
domain-ui-mapper: n/a
Stored row -> `Order` in data; `Order` -> response payload in the inbound adapter.

## Composition Root
`app.container.build_list_orders_handler(rows)` builds `InMemoryOrderRepository(rows)` -> `ListOrders` -> handler.

## Testability Boundary
Tests drive the public handler built by the composition root with in-memory rows.

## Completion Gate
architecture-contract: applied
solid-srp-change-reason: domain policy, row mapping, and HTTP mapping change for different reasons and live in different layers.
solid-ocp-extension-points: a database repository implements `OrderRepository` without touching the use case.
solid-lsp-contracts: any `OrderRepository` returns domain `Order` values for one customer.
solid-isp-consumer-ports: the port exposes only `orders_for_customer`.
solid-dip-dependency-direction: use case depends on the domain port; data implements it; app wires it.
