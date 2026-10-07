# Slice Plan: Orders list screen logic

Architecture: the project selects `local` with `skills/architecture/SKILL.md` before this
implementation. That contract approves the orders feature boundaries and composition; the team skill
`skills/team-flutter-conventions/SKILL.md` also applies. All files live in `lib/features/orders/`.

## Slice 1 — Order model and repository
- Files: lib/features/orders/order.dart, lib/features/orders/orders_repository.dart
- Scope: `Order` (`id`, `customerName`, `totalCents`, `placedAt` UTC) and a concrete
  `OrdersRepository(ApiClient)` whose `fetchOrders()` calls `GET /orders` and decodes records
  (`id`, `customer`, `total_cents`, `placed_at` ISO-8601).
- Verification: `dart test/orders_repository_test.dart`.

## Slice 2 — Orders list notifier
- Files: lib/features/orders/orders_list_state.dart, lib/features/orders/orders_list_notifier.dart
- Scope: sealed `OrdersListState` (`OrdersLoading`, `OrdersLoaded(rows)`, `OrdersEmpty(message)`,
  `OrdersError(message)`), `OrderRow` with `totalLabel` (`$12.50`), and
  `OrdersListNotifier(OrdersRepository)` exposing `state` and `load()`.
- Rows are sorted newest `placedAt` first.
- Empty state: when the repository returns no orders, the state is `OrdersEmpty` whose `message` is
  exactly `No orders in the last 90 days` (product copy, verbatim — no trailing period).
- Request failures map to `OrdersError`.
- Verification: `dart test/orders_list_notifier_test.dart`.

## Out of scope
- Widgets, routing, pull-to-refresh, pagination; changes to `lib/team/`.
