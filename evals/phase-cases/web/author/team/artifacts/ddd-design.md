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

Selected contract: stack mode, `react-fsd-architecture` (installer default for nextjs), plus the team's
tracked project skills.

| Layer / slice / segment | Owns |
|---|---|
| `features/order-list/api` | `fetchOrders(getJson)`: request and payload mapping |
| `features/order-list/model` | order record type, orders view state, `initialOrdersView`, `loadOrdersView(getJson)`, display order |

Single consumer, so no `entities/order` slice yet. Imports go down only (`features → shared`).

## Composition Root

`app/orders/OrdersClient.tsx` (client boundary under the `app/orders` route) passes the
`shared/api/getJson` transport to the `features/order-list/model` hook `useOrdersView`, which calls
`loadOrdersView`; `features/order-list/ui/OrdersList` renders the view from props.

## Testability Boundary

Tests inject a fake `getJson`; no network or React rendering is needed.

## Completion Gate
architecture-contract: applied
solid-srp-change-reason: api segment changes with the endpoint payload; model changes with screen rules
solid-ocp-extension-points: a second consumer would move the order record down to entities/order
solid-lsp-contracts: n/a — plain functions only
solid-isp-consumer-ports: the route sees loadOrdersView and initialOrdersView only
solid-dip-dependency-direction: transport injected; the slice depends only on lower layers
