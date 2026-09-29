# Slice Plan: Orders list screen logic

Architecture contract: local `skills/architecture/SKILL.md` (flat feature folder, one library per
feature, concrete repositories). All slices live in `lib/orders/` as parts of `lib/orders/orders.dart`.

## Slice 1 — Order model and repository
- Scope (`lib/orders/` part files): `Order` (`id`, `customerName`, `totalCents`, `placedAt` UTC)
  decoded by `Order.fromRecord` from raw records (`id`, `customer`, `total_cents`, `placed_at`
  ISO-8601); concrete `OrdersRepository(fetch)` exposing `Future<List<Order>> fetchOrders()`.
- Verification: `dart test/orders_repository_test.dart`.

## Slice 2 — Orders list state holder
- Scope (`lib/orders/` part files): sealed `OrdersListState` (`OrdersLoading`, `OrdersLoaded(rows)`,
  `OrdersEmpty(message)`, `OrdersError(message)`), `OrderRow` with `totalLabel` (`$12.50`), and
  `OrdersListHolder(OrdersRepository)` exposing `state` and `load()`.
- Rows are sorted newest `placedAt` first.
- Empty state: when the repository returns no orders, the state is `OrdersEmpty` whose `message` is
  exactly `No orders in the last 90 days` (product copy, verbatim — no trailing period).
- Fetch failures map to `OrdersError`.
- Verification: `dart test/orders_list_holder_test.dart`.

## Out of scope
- Widgets, routing, pull-to-refresh, pagination.
