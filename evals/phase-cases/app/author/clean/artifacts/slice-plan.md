# Slice Plan: Orders list screen logic

Architecture contract: Clean (profile `flutter`). Roles: `lib/core/domain/orders` (core-domain),
`lib/core/data/orders` (core-data), `lib/features/orders/api` (feature-api),
`lib/features/orders/presentation` (feature-presentation), `lib/app` (app-shell).

## Slice 1 — Order domain model and repository port
- Scope (core-domain): `Order` entity (`id`, `customerName`, `totalCents`, `placedAt` UTC), the
  `OrderRepository` port with `Future<List<Order>> fetchOrders()`, and the domain error
  `OrdersUnavailableException` the port throws when orders cannot be loaded.
- Verification: compiled through the slice 2 and slice 3 tests.

## Slice 2 — Remote-backed repository adapter
- Scope (core-data): `OrdersRemoteSource` transport port returning raw records
  (`id`, `customer`, `total_cents`, `placed_at` ISO-8601), an `OrderDto` with `fromJson`/`toDomain`,
  `OrderRepositoryImpl implements OrderRepository` (translates transport and decoding failures into
  `OrdersUnavailableException`), and `HttpOrdersRemoteSource` (`dart:io` `HttpClient`, `GET` the
  orders URI).
- Verification: `dart test/order_repository_impl_test.dart`.

## Slice 3 — Orders list state holder
- Scope (feature-presentation): sealed `OrdersListState` (`OrdersLoading`, `OrdersLoaded(rows)`,
  `OrdersEmpty(message)`, `OrdersError(message)`), `OrderRow` UI model with `totalLabel` (`$12.50`),
  and `OrdersListHolder(OrderRepository)` exposing `state` and `load()`.
- Scope (feature-api): `ordersListRoute` entry contract (`/orders`) paired with the presentation.
- Scope (app-shell): `createOrdersListHolder` in `lib/app/` wires the holder to the HTTP-backed repository.
- Rows are sorted newest `placedAt` first.
- Empty state: when the repository returns no orders, the state is `OrdersEmpty` whose `message` is
  exactly `No orders in the last 90 days` (product copy, verbatim — no trailing period).
- Failures from the repository map to `OrdersError`.
- Verification: `dart test/orders_list_holder_test.dart`.

## Out of scope
- Widgets, router registration, pull-to-refresh, pagination.
