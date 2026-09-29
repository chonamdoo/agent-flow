# DDD Design — Orders list

## Domain Model

- Bounded Context: `Ordering` (read model for the account area).
- Ubiquitous Language: Order, order status (`pending`, `shipped`, `delivered`, `cancelled`),
  placed at, total in cents, orders view.
- Entities: Order (identity `id`). Value Objects: order status, total cents.
- Aggregates: none on the read side; the list is a projection.
- Domain Events: none (read only).
- Domain Invariants: the loaded list contains every order the API returned, each exactly once, in the
  display order recorded in the slice plan.
- Domain Flow: request orders → map payload rows to Order → choose view state → order for display.

## Architecture Boundary Map

Selected contract: project-local `skills/architecture/SKILL.md` (flat feature folders). Its rules govern
where they differ from the profile's Clean roles.

| Location | Owns |
|---|---|
| `src/orders/api.orders.ts` | order record type, payload type and mapping, `fetchOrders(getJson)` |
| `src/orders/view.orders.ts` | orders view state, `initialOrdersView`, `loadOrdersView(getJson)` |

The display-order rule stays inside `src/orders/`.

## Composition Root

`app/orders/OrdersClient.tsx` (client boundary under the `app/orders` route) owns the browser `getJson`
transport and passes it to `useOrdersView` (`src/orders/hook.orders.ts`), which calls `loadOrdersView`.
`src/orders/screen.orders.tsx` renders the plain view.

## Testability Boundary

Tests inject a fake `getJson`; no network or React rendering is needed.

## Completion Gate
architecture-contract: applied
solid-srp-change-reason: request/mapping and view-state choice live in separate role files
solid-ocp-extension-points: another feature adds its own folder; no shared abstraction to extend
solid-lsp-contracts: n/a — plain functions only
solid-isp-consumer-ports: callers see fetchOrders / loadOrdersView only
solid-dip-dependency-direction: the transport function is passed in; the folder imports no other feature
