# Slice Plan: Orders list screen

Architecture contract: Clean Architecture (`clean-architecture-core` + `react-native-clean-architecture` + `react-native-clean-presentation-architecture`).
Bounded context: Ordering (read side: order history). DDD depth: light — one read model, no aggregates change.

## Slice 1 — Read orders

- Scope: `src/core/domain/orders/` (Order entity, newest-first ordering, `OrderRepository` port) and `src/core/data/orders/` (`OrderDto`, mapper, `OrderRepositoryImpl` over `GetJson`).
- Behavior: `GET /v1/orders` returns `{"orders": [{"order_id", "placed_at", "total_cents", "status"}]}`; each entry becomes a record with `id`, `placedAt` (`Date`), `totalCents`, `status`.
- Verification: `tests/orders-repository.test.ts`.

## Slice 2 — Screen state holder

- Scope: `src/features/orders/presentation/` (`OrdersUiState`, row UiModel mapping, `createOrdersScreenStore(repository)`).
- States: `loading` (initial and while a load runs), `content` with `rows` (`id`, `totalLabel`), `empty`, `error` with a user-facing `message`.
- Ordering: rows are sorted by `placedAt`, newest first.
- Empty state copy (product-approved, use verbatim): the `empty` state carries `message: 'No orders yet. Your purchases will show up here.'`.
- Subscribers are notified on every state change and can unsubscribe.
- `cancel()` discards the result of an in-flight load; the screen calls it when it unmounts.
- Verification: `tests/orders-screen-store.test.ts`; the empty-state copy is asserted in the acceptance check.

## Slice 3 — Feature entry and screen

- Scope: feature-api entry contract `src/features/orders/api/OrdersEntry.ts` (`OrdersEntry`, `CreateOrdersEntry`, `OrdersEntryDependencies`); `src/features/orders/presentation/createOrdersEntry.tsx` implements it and owns the store instance; `OrdersScreen.tsx` renders the store; `src/app/AppShell.tsx` composes `createOrdersEntry({ orderRepository: new OrderRepositoryImpl(getJson) })` and depends on presentation only through that contract.
- Behavior: trigger `load()` on mount and `cancel()` on unmount; render a loading indicator, the `message` for empty/error, a retry action in the error state, and a list of rows for content.
- Verification: type check by review; no runtime test (screen is not executed in unit tests).

## Out of scope

Pagination, pull-to-refresh, order detail navigation.
