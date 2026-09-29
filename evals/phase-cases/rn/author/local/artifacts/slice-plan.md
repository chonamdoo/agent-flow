# Slice Plan: Orders list screen

Architecture contract: project-local contract `skills/architecture/SKILL.md` (flat feature folder).
Bounded context: Ordering (read side: order history). DDD depth: light — one read model, no aggregates change.

## Slice 1 — Read orders

- Scope: `src/orders/orders.types.ts` (order record type) and `src/orders/orders.api.ts` (`fetchOrders(getJson)` converting the payload).
- Behavior: `GET /v1/orders` returns `{"orders": [{"order_id", "placed_at", "total_cents", "status"}]}`; each entry becomes a record with `id`, `placedAt` (`Date`), `totalCents`, `status`.
- Verification: `tests/orders.api.test.ts`.

## Slice 2 — Screen state holder

- Scope: `src/orders/orders.store.ts` (`createOrdersStore(loadOrders)` state holder, newest-first rows, totals via `formatCents`).
- States: `loading` (initial and while a load runs), `content` with `rows` (`id`, `totalLabel`), `empty`, `error` with a user-facing `message`.
- Ordering: rows are sorted by `placedAt`, newest first.
- Empty state copy (product-approved, use verbatim): the `empty` state carries `message: 'No orders yet. Your purchases will show up here.'`.
- Subscribers are notified on every state change and can unsubscribe.
- `cancel()` discards the result of an in-flight load; the screen calls it when it unmounts.
- Verification: `tests/orders.store.test.ts`; the empty-state copy is asserted in the acceptance check.

## Slice 3 — Screen

- Scope: `src/orders/orders.screen.tsx` wires `fetchOrders(getJson)` into the store and renders it.
- Behavior: trigger `load()` on mount and `cancel()` on unmount; render a loading indicator, the `message` for empty/error, a retry action in the error state, and a list of rows for content.
- Verification: type check by review; no runtime test (screen is not executed in unit tests).

## Out of scope

Pagination, pull-to-refresh, order detail navigation.
