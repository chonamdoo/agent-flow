# Slice Plan — Orders list

Task: load the signed-in user's orders from `GET /api/orders` and expose the view state the orders
screen renders (loading, error, empty, loaded), with orders sorted for display.

Architecture contract: project-local contract `skills/architecture/SKILL.md` (`--architecture-mode local`).
DDD: single bounded context `Ordering` (read side only); ubiquitous language: Order, order status,
placed at, total (cents), orders view.

## Product Decisions

- Display order (decided with product on 2026-09-22): the loaded list is ordered by status priority —
  `pending` first, then `shipped`, then `delivered`, then `cancelled` — and, within the same status,
  by `placedAt` newest first. The API returns orders in no guaranteed order, so the client applies this
  ordering to every loaded list.

## Slice 1 — Order records from the orders API

- Scope: call `GET /api/orders` through an injected `getJson(path)` and map each payload row
  (`order_id`, `state`, `placed_at`, `total_cents`) to an order record (`id`, lower-case `status`,
  `placedAt`, `totalCents`).
- Boundary: feature folder `src/orders/` — `api.orders.ts` (request + payload mapping to order records).
- Files: src/orders/api.orders.ts
- Verification: `node --test "tests/*.test.ts"` — request path and mapping test is green.

## Slice 2 — Orders view state

- Scope: initial `loading` state; loader returns `error` when the request fails, `empty` when there are
  no orders, otherwise `loaded` with every order in the display order above.
- Boundary: feature folder `src/orders/` — `view.orders.ts` (view state, initial state, loader taking `getJson`) and the display-order rule in the same folder.
- Files: src/orders/view.orders.ts (+ one role file for ordering if it keeps the view small)
- Verification: `node --test "tests/*.test.ts"` — loading/loaded/empty/error tests green; display order
  covered by a test that feeds the same orders in two different input orders.

## Slice 3 — Orders route

- Scope: `/orders` route renders the view (loading, error message, empty message, order rows with id,
  status and placed date) from a client hook that loads once on mount.
- Boundary: route `app/orders/` owns the browser `getJson` transport and passes it in; feature folder
  `src/orders/` gains `hook.orders.ts` (state hook) and `screen.orders.tsx` (render from plain view).
- Files: app/orders/page.tsx, app/orders/OrdersClient.tsx, src/orders/hook.orders.ts, src/orders/screen.orders.tsx
- Verification: typecheck; the route is wiring only, so behavior stays covered by the slice 1–2 tests.

## Out of Scope

- Visual styling, pagination, order detail, displaying totals / currency formatting.
