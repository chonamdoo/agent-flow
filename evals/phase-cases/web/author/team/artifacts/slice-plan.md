# Slice Plan — Orders list

Task: load the signed-in user's orders from `GET /api/orders` and expose the view state the orders
screen renders (loading, error, empty, loaded), with orders sorted for display.

Architecture contract: bundled stack contract `react-fsd-architecture` (installer default for nextjs) plus the team's tracked project skills.
DDD: single bounded context `Ordering` (read side only); ubiquitous language: Order, order status,
placed at, total (cents), orders view.

## Product Decisions

- Display order (decided with product on 2026-09-22): the loaded list is ordered by status priority —
  `pending` first, then `shipped`, then `delivered`, then `cancelled` — and, within the same status,
  by `placedAt` newest first. The API returns orders in no guaranteed order, so the client applies this
  ordering to every loaded list.

## Slice 1 — Order records from the orders API

- Files: src/features/order-list/api/fetchOrders.ts, src/shared/api/endpoints.ts
- Scope: call `GET /api/orders` through an injected `getJson(path)` and map each payload row
  (`order_id`, `state`, `placed_at`, `total_cents`) to an order record (`id`, lower-case `status`,
  `placedAt`, `totalCents`).
- Boundary: `features/order-list` slice — `api` segment (request + payload mapping to order records).
- Verification: `node --test "tests/*.test.ts"` — request path and mapping test is green.

## Slice 2 — Orders view state

- Files: src/features/order-list/model/order.ts, src/features/order-list/model/loadOrdersView.ts, src/features/order-list/model/sortOrdersForDisplay.ts
- Scope: initial `loading` state; loader returns `error` when the request fails, `empty` when there are
  no orders, otherwise `loaded` with every order in the display order above.
- Boundary: `features/order-list` slice — `model` segment (order record type, view state, initial state, loader taking `getJson`, display-order rule).
- Verification: `node --test "tests/*.test.ts"` — loading/loaded/empty/error tests green; display order
  covered by a test that feeds the same orders in two different input orders.

## Slice 3 — Orders route

- Files: app/orders/page.tsx, app/orders/OrdersClient.tsx, src/shared/api/getJson.ts, src/features/order-list/model/useOrdersView.ts, src/features/order-list/ui/OrdersList.tsx
- Scope: `/orders` route renders the view (loading, error message, empty message, order rows with id,
  status and placed date) from a client hook that loads once on mount.
- Boundary: route `app/orders/` composes the slice with the `shared/api/getJson.ts` transport
  foundation; `features/order-list` gains a `model` hook and a `ui` component receiving the view via props.
- Verification: typecheck; the route is wiring only, so behavior stays covered by the slice 1–2 tests.

## Out of Scope

- Visual styling, pagination, order detail, displaying totals / currency formatting.
